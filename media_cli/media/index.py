"""Search index: sampled-frame captions, cached on disk, with a sense-call budget.

Public API
----------
``build_index(path, *, fps=None, scene=None, dry_run=False, client=None, role="senses",
batch_size=8, max_calls=600, verify=False, prompt_version=PROMPT_VERSION, cache_dir=None,
max_cache_bytes=DEFAULT_MAX_CACHE_BYTES) -> dict``
    Sample frames (``fps`` -> one frame every ``1/fps`` s, or ``scene`` threshold) with
    ``media_cli.media.frames``, caption them in batches through the senses gateway and store
    ``{t, frame_index, frame_path, caption, model, prompt_version}`` entries.  With
    ``dry_run`` nothing is captioned: it returns the exact planned sense-call count.  A plan
    above ``max_calls`` raises ``input.budget_exceeded`` before the first gateway request.
``load_index(path, *, fps|scene, batch_size, role, prompt_version, cache_dir)``
    Zero-gateway-call read of a cached index (or ``None``).
``purge(path, *, cache_dir=None) -> int``  /  ``fingerprint(path) -> dict``

Cache layout (``$XDG_CACHE_HOME/media-cli/index``, fallback ``~/.cache/...``; dirs 0700)::

    <fp16>-<params16>/index.json   schema_version, fingerprint, identity, entries[...]
    <fp16>-<params16>/frames/*.png

Cache key and the o11 trade-off
-------------------------------
The directory key is file fingerprint (size, mtime_ns, inode, sha256 of the first and last
1 MiB -- never a full-file hash) plus ``(role, prompt_version, sampling params)``.  The
*served model* is stored inside ``index.json`` rather than in the key, because learning it
costs a ``GET /capabilities`` and an unchanged file must cost zero gateway requests.  So a
model change is detected only when the identity is checked: ``build_index(verify=True)``
compares the stored model to ``served_model()`` and re-indexes on a difference.  Without
``verify`` (and for ``load_index``) the cached index is served from its stored identity.

Writes are atomic (staging dir, then rename); a failure caches nothing.  Total size is
bounded by LRU eviction on ``index.json`` mtime (touched on every read).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

from media_cli.media import frames, probe
from media_cli.media.errors import (
    ENV_SENSE_UNAVAILABLE,
    INPUT_UNREADABLE,
    MediaEnvError,
    MediaInputError,
)
from media_cli.media.senses import SensesClient

SCHEMA_VERSION = 1
PROMPT_VERSION = "1"
INPUT_BUDGET_EXCEEDED = "input.budget_exceeded"
INPUT_BAD_INDEX_REQUEST = "input.bad_index_request"
DEFAULT_MAX_CALLS = 600
DEFAULT_BATCH_SIZE = 8
DEFAULT_MAX_CACHE_BYTES = 2 * 1024**3
SAMPLE_BYTES = 1024 * 1024
INDEX_FILE = "index.json"
FRAMES_DIR = "frames"

_PROMPTS = {
    "1": (
        "You will see {n} video frames, in order. Write one concise caption (one sentence, "
        "what is visible) for each frame. Reply with a JSON object "
        '{{"captions": [...]}} holding exactly {n} strings, in the same order as the frames.'
    )
}
_JSON_FORMAT = {"type": "json_object"}


def default_cache_dir() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "media-cli", "index")


def _bad(message: str, remediation: str) -> MediaInputError:
    return MediaInputError(INPUT_BAD_INDEX_REQUEST, message, remediation)


def fingerprint(path: str | os.PathLike) -> dict[str, Any]:
    """Cheap file identity: stat fields plus a hash of the first and last 1 MiB."""
    p = Path(path)
    try:
        st = p.stat()
        head = hashlib.sha256()
        tail = hashlib.sha256()
        with p.open("rb") as fh:
            head.update(fh.read(SAMPLE_BYTES))
            fh.seek(max(st.st_size - SAMPLE_BYTES, 0))
            tail.update(fh.read(SAMPLE_BYTES))
    except OSError as exc:
        raise MediaInputError(
            INPUT_UNREADABLE, f"cannot read {p}: {exc.strerror or exc}", "check the path"
        ) from exc
    return {
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
        "inode": st.st_ino,
        "head_sha256": head.hexdigest(),
        "tail_sha256": tail.hexdigest(),
    }


def _digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()[:16]


def _sampling(fps: float | None, scene: float | None, batch_size: int) -> dict[str, Any]:
    if (fps is None) == (scene is None):
        raise _bad("give exactly one of fps or scene", "pass fps=0.5 or scene=0.3")
    if fps is not None and not (math.isfinite(fps) and fps > 0):
        raise _bad(f"fps must be positive, got {fps}", "use e.g. 0.5 (one frame per 2 s)")
    if scene is not None and not (0.0 <= scene <= 1.0):
        raise _bad(f"scene must be within 0..1, got {scene}", "use e.g. 0.3")
    if batch_size < 1:
        raise _bad(f"batch_size must be >= 1, got {batch_size}", "use e.g. 8")
    return {
        "mode": "fps" if fps is not None else "scene",
        "value": fps or scene,
        "batch": batch_size,
    }


def _identity(role: str, prompt_version: str, sampling: dict[str, Any]) -> dict[str, Any]:
    return {"role": role, "prompt_version": prompt_version, "sampling": sampling}


def _location(root: str, fp: dict[str, Any], identity: dict[str, Any]) -> tuple[str, str]:
    return _digest(fp), os.path.join(root, f"{_digest(fp)}-{_digest(identity)}")


def _private_dir(path: str) -> None:
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)


def _read_index(directory: str) -> dict[str, Any] | None:
    f = os.path.join(directory, INDEX_FILE)
    try:
        with open(f, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        return None
    with contextlib.suppress(OSError):
        os.utime(f)  # LRU access stamp
    base = os.path.join(directory)
    for e in data.get("entries", []):
        e["frame_path"] = os.path.join(base, e["frame_path"])
    return data


def load_index(
    path: str | os.PathLike,
    *,
    fps: float | None = None,
    scene: float | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    role: str = "senses",
    prompt_version: str = PROMPT_VERSION,
    cache_dir: str | None = None,
) -> dict[str, Any] | None:
    """Return the cached index for *path* with zero gateway requests, or ``None``."""
    root = cache_dir or default_cache_dir()
    ident = _identity(role, prompt_version, _sampling(fps, scene, batch_size))
    _, directory = _location(root, fingerprint(path), ident)
    return _read_index(directory)


def _plan_times(path: str, fps: float | None, scene: float | None) -> list[float]:
    info = probe.probe(path)
    if info.video is None:
        raise _bad(f"{path} has no video stream", "only video can be frame-indexed")
    every = (1.0 / fps) if fps is not None else None
    requested = frames._requested_times(info, path, None, every, scene, 10**9)
    by_index: dict[int, float] = {}
    for t in requested:
        idx = probe.to_frame_index(info, t)
        by_index.setdefault(idx, probe.to_seconds(info, idx))
    return [by_index[i] for i in sorted(by_index)]


def _budget(n_frames: int, batches: int, max_calls: int) -> None:
    if batches > max_calls:
        raise MediaInputError(
            INPUT_BUDGET_EXCEEDED,
            f"indexing needs {batches} sense calls ({n_frames} frames), above the cap of "
            f"{max_calls}",
            "sample fewer frames (lower fps, higher scene threshold), raise batch_size, "
            "or raise max_calls",
        )


def _caption_batch(client: SensesClient, role: str, paths: list[str], pv: str) -> list[str]:
    prompt = _PROMPTS.get(pv, _PROMPTS[PROMPT_VERSION]).format(n=len(paths))
    reply = client.describe_images(paths, prompt, role=role, response_format=_JSON_FORMAT)
    caps = reply.get("captions") if isinstance(reply, dict) else None
    if (
        not isinstance(caps, list)
        or len(caps) != len(paths)
        or not all(isinstance(c, str) for c in caps)
    ):
        raise MediaEnvError(
            ENV_SENSE_UNAVAILABLE,
            f"senses model returned malformed captions (wanted {len(paths)} strings)",
            "retry; if it persists the model is not following the caption prompt",
        )
    return caps


def _dir_size(directory: str) -> int:
    total = 0
    for dp, _dn, fn in os.walk(directory):
        for name in fn:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(dp, name)).st_size
    return total


def _evict(root: str, max_bytes: int, keep: str) -> None:
    """Delete least-recently-used index dirs until the cache fits; never the *keep* dir."""
    items = []
    for name in os.listdir(root):
        d = os.path.join(root, name)
        if not os.path.isdir(d) or name.startswith("."):
            continue
        try:
            stamp = os.stat(os.path.join(d, INDEX_FILE)).st_mtime
        except OSError:
            stamp = 0.0
        items.append((stamp, d, _dir_size(d)))
    total = sum(s for _, _, s in items)
    for _stamp, d, size in sorted(items):
        if total <= max_bytes:
            break
        if os.path.abspath(d) == os.path.abspath(keep):
            continue
        shutil.rmtree(d, ignore_errors=True)
        total -= size


def build_index(
    path: str | os.PathLike,
    *,
    fps: float | None = None,
    scene: float | None = None,
    dry_run: bool = False,
    client: SensesClient | None = None,
    role: str = "senses",
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_calls: int = DEFAULT_MAX_CALLS,
    verify: bool = False,
    prompt_version: str = PROMPT_VERSION,
    cache_dir: str | None = None,
    max_cache_bytes: int = DEFAULT_MAX_CACHE_BYTES,
) -> dict[str, Any]:
    """Build (or reuse) the caption index for *path*; see the module docstring."""
    src = os.fspath(path)
    root = cache_dir or default_cache_dir()
    sampling = _sampling(fps, scene, batch_size)
    ident = _identity(role, prompt_version, sampling)
    fp = fingerprint(src)
    _, final = _location(root, fp, ident)
    cached = _read_index(final)

    if dry_run:
        times = _plan_times(src, fps, scene)
        batches = math.ceil(len(times) / batch_size)
        calls = 0 if cached is not None else batches
        _budget(len(times), calls, max_calls)
        return {
            "frames": len(times),
            "batches": batches,
            "sense_calls": calls,
            "cap": max_calls,
            "cached": cached is not None,
            "identity": ident,
        }

    client = client or SensesClient()
    if cached is not None:
        if not verify:
            return cached
        if cached["identity"].get("served_model") == client.served_model(role):
            return cached

    # Plan and budget-check before the first gateway request for a fresh index.
    times = _plan_times(src, fps, scene)
    batches = math.ceil(len(times) / batch_size)
    _budget(len(times), batches, max_calls)
    if not times:
        raise _bad("no frames were selected to index", "lower the scene threshold or use fps")

    model = client.served_model(role)
    _private_dir(root)
    stage = os.path.join(root, f".stage-{uuid.uuid4().hex}")
    try:
        _private_dir(stage)
        shots = frames.extract(
            src,
            times=times,
            outdir=os.path.join(stage, FRAMES_DIR),
            max_frames=len(times),
            outdir_create=True,
        )
        entries: list[dict[str, Any]] = []
        for i in range(0, len(shots), batch_size):
            chunk = shots[i : i + batch_size]
            caps = _caption_batch(client, role, [s["path"] for s in chunk], prompt_version)
            for shot, cap in zip(chunk, caps):
                entries.append(
                    {
                        "t": shot["t"],
                        "frame_index": shot["frame_index"],
                        "frame_path": os.path.join(FRAMES_DIR, os.path.basename(shot["path"])),
                        "caption": cap,
                        "model": model,
                        "prompt_version": prompt_version,
                    }
                )
        doc = {
            "schema_version": SCHEMA_VERSION,
            "source": os.path.realpath(src),
            "fingerprint": fp,
            "identity": {**ident, "served_model": model},
            "entries": entries,
        }
        with open(os.path.join(stage, INDEX_FILE), "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        if os.path.isdir(final):
            trash = os.path.join(root, f".trash-{uuid.uuid4().hex}")
            os.rename(final, trash)
            shutil.rmtree(trash, ignore_errors=True)
        os.rename(stage, final)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    _evict(root, max_cache_bytes, final)
    result = _read_index(final)
    if result is None:
        raise MediaEnvError(ENV_SENSE_UNAVAILABLE, "index vanished after write", "retry")
    return result


def purge(path: str | os.PathLike, *, cache_dir: str | None = None) -> int:
    """Delete every cached artifact for *path*; returns the number of index dirs removed.

    Removes every key sharing the file's current fingerprint, plus any older index whose
    recorded source is this same file (left behind when the file changed).
    """
    root = cache_dir or default_cache_dir()
    if not os.path.isdir(root):
        return 0
    real = os.path.realpath(os.fspath(path))
    fp16 = None
    with contextlib.suppress(MediaInputError):
        fp16 = _digest(fingerprint(path))
    removed = 0
    for name in os.listdir(root):
        d = os.path.join(root, name)
        if not os.path.isdir(d) or name.startswith("."):
            continue
        hit = fp16 is not None and name.startswith(fp16 + "-")
        if not hit:
            try:
                with open(os.path.join(d, INDEX_FILE), encoding="utf-8") as fh:
                    hit = json.load(fh).get("source") == real
            except (OSError, ValueError, AttributeError):
                hit = False
        if hit:
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    return removed
