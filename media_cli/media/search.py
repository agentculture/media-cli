"""Semantic query over a media file, with evidence.

Public API
----------
``query(path, text, *, modality="frames", index_params=None, client=None, batch_size=8,
threshold=0.5, build_if_missing=False, role="senses", language="en", cache_dir=None)
-> list[hit]``

``modality`` is ``frames`` (cached frame captions), ``speech`` (chunked transcript) or
``all`` (both, sorted by ``start``).  Matching is a yes/no + confidence judgement made by the
senses model for every caption / transcript segment against *text* (batched, one chat call
per ``batch_size`` items) -- never substring matching.  A reply that is not exactly one
well-formed verdict per item raises ``env.sense_unavailable``; no partial result is returned.

Hit schema (times are normalized seconds, see ``probe``)::

    {"start", "end", "score", "evidence": {...}, "samples": [...], "frame_index"?}

* frames: ``frame_index`` (of the first matching sample); ``evidence`` =
  ``{kind: "frame", frame_path, caption, model}`` of the highest-confidence sample;
  ``samples`` = every matching sample ``{t, frame_index, confidence, evidence}``.
* speech: ``evidence`` = ``{kind: "speech", transcript, text, model}`` where ``transcript``
  is the cached transcript file; ``samples`` = matching segments ``{start, end, confidence,
  evidence}``.  No ``frame_index``.
* ``score`` = mean confidence of the merged samples.  ``model`` in frame evidence is the
  captioning model stored in the index; in speech evidence it is the model that judged.

Range rule (frames): consecutive matching samples (adjacent in the index) merge into
``[t of the first, t of the sample after the last]`` -- a sample stands for the interval up
to the next sample, so the range covers every frame between matching samples.  If the last
matching sample is the final one the range ends at the media end (probe).  With sparse
sampling (scene mode) the range therefore over-covers rather than under-covers.  Speech
segments are time spans already: consecutive matching segments merge into
``[start of first, end of last]``.

Requests: with ``frames`` the index is read via ``load_index`` (zero gateway calls); the only
non-matching traffic is the client's one ``GET /capabilities`` for ``ensure_local`` (none if
the client already holds a verdict).  Sense locality (o4) holds: ``chat`` / ``transcribe``
run ``ensure_local`` before anything leaves; a dead gateway is ``env.sense_unavailable``.
No index and ``build_if_missing=False`` is ``input.index_missing`` (the CLI submits the index
build as a daemon job).  ``build_if_missing=True`` calls ``build_index`` (verify=False, so the
query path stays free of the d6 verification call).

Transcript cache: ``<cache root>/.transcripts/<fingerprint16>-<language>.json`` (dot-prefixed
so ``index`` eviction and ``purge`` ignore it; it is not size-bounded or purged by them).
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from media_cli.media import index, probe, speech
from media_cli.media.errors import ENV_SENSE_UNAVAILABLE, MediaEnvError, MediaInputError
from media_cli.media.senses import SensesClient

INPUT_INDEX_MISSING = "input.index_missing"
INPUT_BAD_QUERY = "input.bad_query"
MODALITIES = ("frames", "speech", "all")
DEFAULT_THRESHOLD = 0.5
TRANSCRIPTS_DIR = ".transcripts"
_JSON_FORMAT = {"type": "json_object"}

_PROMPT = (
    "Query: {query}\n"
    "Below are {n} numbered {what}. For each, decide whether it matches the query. Reply with "
    'a JSON object {{"matches": [{{"i": <number>, "match": true|false, "confidence": '
    "<0..1>}}, ...]}} with exactly one entry per number.\n{items}"
)


def _bad_query(message: str, remediation: str) -> MediaInputError:
    return MediaInputError(INPUT_BAD_QUERY, message, remediation)


def _malformed(why: str) -> MediaEnvError:
    return MediaEnvError(
        ENV_SENSE_UNAVAILABLE,
        f"senses model returned a malformed match reply ({why})",
        "retry; if it persists the model is not following the yes/no prompt",
    )


def _parse_verdicts(content: str, n: int) -> list[tuple[bool, float]]:
    try:
        data = json.loads(content)
    except ValueError as exc:
        raise _malformed("not JSON") from exc
    rows = data.get("matches") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise _malformed("no 'matches' list")
    out: dict[int, tuple[bool, float]] = {}
    for r in rows:
        if not isinstance(r, dict):
            raise _malformed("entry is not an object")
        i, m, c = r.get("i"), r.get("match"), r.get("confidence")
        if isinstance(i, bool) or not isinstance(i, int) or not 0 <= i < n or i in out:
            raise _malformed("bad or duplicate index")
        if not isinstance(m, bool):
            raise _malformed("'match' is not a boolean")
        if isinstance(c, bool) or not isinstance(c, (int, float)) or not 0 <= c <= 1:
            raise _malformed("'confidence' is not within 0..1")
        out[i] = (m, float(c))
    if len(out) != n:
        raise _malformed(f"wanted {n} verdicts, got {len(out)}")
    return [out[i] for i in range(n)]


def _judge(
    client: SensesClient, role: str, text: str, items: list[str], what: str, batch_size: int
) -> list[tuple[bool, float]]:
    verdicts: list[tuple[bool, float]] = []
    for s in range(0, len(items), batch_size):
        chunk = items[s : s + batch_size]
        body = "\n".join(f"[{i}] {' '.join(t.split())}" for i, t in enumerate(chunk))
        prompt = _PROMPT.format(query=text, n=len(chunk), what=what, items=body)
        content = client.chat(
            [{"role": "user", "content": prompt}], role=role, response_format=_JSON_FORMAT
        )
        verdicts.extend(_parse_verdicts(content, len(chunk)))
    return verdicts


def _runs(flags: list[bool]) -> list[tuple[int, int]]:
    runs, start = [], None
    for i, f in enumerate(flags + [False]):
        if f and start is None:
            start = i
        elif not f and start is not None:
            runs.append((start, i - 1))
            start = None
    return runs


def _query_frames(path, text, params, client, role, batch_size, threshold, build, cache_dir):
    idx = index.load_index(path, cache_dir=cache_dir, role=role, **params)
    if idx is None:
        if not build:
            raise MediaInputError(
                INPUT_INDEX_MISSING,
                f"no frame index for {path} with {params}",
                "build the index first (the CLI submits indexing as a job), or pass "
                "build_if_missing=True",
            )
        idx = index.build_index(
            path, client=client, role=role, verify=False, cache_dir=cache_dir, **params
        )
    entries = idx["entries"]
    if not entries:
        return []
    verdicts = _judge(
        client, role, text, [e["caption"] for e in entries], "frame captions", batch_size
    )
    flags = [m and c >= threshold for m, c in verdicts]
    runs = _runs(flags)
    end_of_media = probe.probe(path).end if runs and runs[-1][1] == len(entries) - 1 else 0.0
    hits = []
    for a, b in runs:
        samples = []
        for k in range(a, b + 1):
            e = entries[k]
            samples.append(
                {
                    "t": e["t"],
                    "frame_index": e["frame_index"],
                    "confidence": verdicts[k][1],
                    "evidence": {
                        "kind": "frame",
                        "frame_path": e["frame_path"],
                        "caption": e["caption"],
                        "model": e["model"],
                    },
                }
            )
        end = entries[b + 1]["t"] if b + 1 < len(entries) else end_of_media
        if end == float("inf") or end < entries[b]["t"]:
            end = entries[b]["t"]
        best = max(samples, key=lambda s: s["confidence"])
        hits.append(
            {
                "start": entries[a]["t"],
                "end": end,
                "frame_index": entries[a]["frame_index"],
                "evidence": best["evidence"],
                "samples": samples,
                "score": sum(s["confidence"] for s in samples) / len(samples),
            }
        )
    return hits


def _transcript(path, client, language, cache_dir) -> tuple[list[dict], str]:
    root = os.path.join(cache_dir or index.default_cache_dir(), TRANSCRIPTS_DIR)
    fp = hashlib.sha256(json.dumps(index.fingerprint(path), sort_keys=True).encode()).hexdigest()
    file = os.path.join(root, f"{fp[:16]}-{language}.json")
    try:
        with open(file, encoding="utf-8") as fh:
            segs = json.load(fh)
        if isinstance(segs, list):
            return segs, file
    except (OSError, ValueError):
        pass
    info = probe.probe(path)
    segs = speech.transcribe_media(
        str(path),
        transcriber=client.transcribe,
        language=language,
        duration=info.duration,
        start_offset=info.start_time - info.origin,
    )
    os.makedirs(root, mode=0o700, exist_ok=True)
    os.chmod(root, 0o700)
    tmp = f"{file}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(segs, fh)
    os.replace(tmp, file)
    return segs, file


def _query_speech(path, text, client, role, batch_size, threshold, language, cache_dir):
    segs, file = _transcript(path, client, language, cache_dir)
    if not segs:
        return []
    verdicts = _judge(
        client, role, text, [s["text"] for s in segs], "transcript segments", batch_size
    )
    model = client.served_model(role)
    flags = [m and c >= threshold for m, c in verdicts]
    hits = []
    for a, b in _runs(flags):
        samples = [
            {
                "start": segs[k]["start"],
                "end": segs[k]["end"],
                "confidence": verdicts[k][1],
                "evidence": {
                    "kind": "speech",
                    "transcript": file,
                    "text": segs[k]["text"],
                    "model": model,
                },
            }
            for k in range(a, b + 1)
        ]
        best = max(samples, key=lambda s: s["confidence"])
        hits.append(
            {
                "start": segs[a]["start"],
                "end": segs[b]["end"],
                "evidence": best["evidence"],
                "samples": samples,
                "score": sum(s["confidence"] for s in samples) / len(samples),
            }
        )
    return hits


def query(
    path: str | os.PathLike,
    text: str,
    *,
    modality: str = "frames",
    index_params: dict[str, Any] | None = None,
    client: SensesClient | None = None,
    batch_size: int = index.DEFAULT_BATCH_SIZE,
    threshold: float = DEFAULT_THRESHOLD,
    build_if_missing: bool = False,
    role: str = "senses",
    language: str = "en",
    cache_dir: str | None = None,
) -> list[dict[str, Any]]:
    """Find where *text* occurs in *path*; see the module docstring for the hit schema."""
    if not text or not text.strip():
        raise _bad_query("query text is empty", "describe what to look for")
    if modality not in MODALITIES:
        raise _bad_query(f"unknown modality {modality!r}", f"use one of {', '.join(MODALITIES)}")
    if batch_size < 1 or not 0 <= threshold <= 1:
        raise _bad_query("batch_size must be >= 1 and threshold within 0..1", "fix the argument")
    params = dict(index_params or {"fps": 0.5})
    unknown = set(params) - {"fps", "scene", "batch_size", "prompt_version"}
    if unknown:
        raise _bad_query(f"unknown index_params {sorted(unknown)}", "use fps or scene")
    src = os.fspath(path)
    client = client or SensesClient()
    hits: list[dict[str, Any]] = []
    if modality in ("frames", "all"):
        hits += _query_frames(
            src, text, params, client, role, batch_size, threshold, build_if_missing, cache_dir
        )
    if modality in ("speech", "all"):
        hits += _query_speech(src, text, client, role, batch_size, threshold, language, cache_dir)
    return sorted(hits, key=lambda h: (h["start"], h["end"]))
