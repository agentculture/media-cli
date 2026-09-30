"""Speech search: chunked speech-to-text with time offsets.

The gateway returns text only (no word timestamps), so each chunk's time span is
reported as the time range of the text it produced: a hit is located to within
its chunk (<= 30 s), not to the word.

Public API
----------
``plan_chunks(duration, chunk=30.0, overlap=2.0) -> list[(start, end)]``
    Pure planner.  Chunks are at most ``chunk`` seconds; consecutive chunks
    overlap by ``overlap`` seconds; the last chunk may be short.
``transcribe_media(path, *, transcriber=None, language="en", workdir=None,
duration=None, start_offset=0.0) -> list[{"start", "end", "text"}]``
    Extract each chunk to 16 kHz mono WAV (via the ``_tools`` ffmpeg seam into a
    temp dir that is always removed), transcribe it, offset times by the chunk
    start, drop empty transcripts, and merge overlap duplicates.  ``transcriber``
    defaults to ``SensesClient().transcribe`` (called as ``t(wav, language=...)``).

Time base: times are seconds from the start of the audio the chunks are cut
from (``ffmpeg -ss`` on the input).  Callers whose time base differs (e.g. the
first presented frame is not at t=0) pass ``start_offset`` to convert; it is
added to every start and end.

Overlap merge heuristic: the 2 s overlap means the tail words of chunk i are
usually repeated as the head words of chunk i+1.  Words are compared
case-insensitively with punctuation stripped; the longest run (>= 2 words, at
most 40) that is both a suffix of chunk i's words and a prefix of chunk i+1's is
dropped from chunk i+1.  Limits: STT may transcribe the same audio differently
on each side of a cut (cut-through words, different spelling), in which case
the duplicate survives; a genuine repeated phrase at a boundary may be
over-trimmed; a single duplicated word is deliberately not merged.
"""

from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path
from typing import Callable

from media_cli.media import _tools
from media_cli.media.errors import INPUT_UNREADABLE, MediaInputError

CHUNK_SECONDS = 30.0
OVERLAP_SECONDS = 2.0
MIN_OVERLAP_WORDS = 2
MAX_OVERLAP_WORDS = 40

Transcriber = Callable[..., str]


def plan_chunks(
    duration: float, chunk: float = CHUNK_SECONDS, overlap: float = OVERLAP_SECONDS
) -> list[tuple[float, float]]:
    """Plan ``(start, end)`` chunk spans covering ``[0, duration]``."""
    if chunk <= 0 or overlap < 0 or overlap >= chunk:
        raise ValueError("require chunk > 0 and 0 <= overlap < chunk")
    if duration <= 0:
        return []
    step = chunk - overlap
    spans: list[tuple[float, float]] = []
    start = 0.0
    while True:
        end = min(start + chunk, duration)
        spans.append((round(start, 6), round(end, 6)))
        if end >= duration:
            return spans
        start += step


def _probe_duration(path: str) -> float:
    cp = _tools.run(
        "ffprobe",
        ["-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
    )
    try:
        return float(cp.stdout.strip().splitlines()[0])
    except (ValueError, IndexError) as exc:
        raise MediaInputError(
            INPUT_UNREADABLE,
            f"cannot determine duration of {path}",
            "pass a readable audio/video file with an audio stream",
        ) from exc


def _norm(word: str) -> str:
    return re.sub(r"[^\w]", "", word.lower())


def _strip_overlap(prev_words: list[str], words: list[str]) -> list[str]:
    """Drop from ``words`` the longest head that repeats the tail of ``prev_words``."""
    a = [_norm(w) for w in prev_words]
    b = [_norm(w) for w in words]
    top = min(len(a), len(b), MAX_OVERLAP_WORDS)
    for k in range(top, MIN_OVERLAP_WORDS - 1, -1):
        if a[-k:] == b[:k]:
            return words[k:]
    return words


def _extract(path: str, start: float, length: float, out: Path) -> None:
    _tools.run(
        "ffmpeg",
        [
            "-v", "error", "-y",
            "-ss", f"{start:.3f}", "-t", f"{length:.3f}",
            "-i", path,
            "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
            str(out),
        ],
    )  # fmt: skip


def transcribe_media(
    path: str,
    *,
    transcriber: Transcriber | None = None,
    language: str = "en",
    workdir: str | None = None,
    duration: float | None = None,
    start_offset: float = 0.0,
) -> list[dict]:
    """Transcribe ``path`` in overlapping chunks; return ``[{start, end, text}]``."""
    path = str(path)
    if not Path(path).is_file():
        raise MediaInputError(INPUT_UNREADABLE, f"not a readable file: {path}", "check the path")
    if transcriber is None:
        from media_cli.media.senses import SensesClient

        transcriber = SensesClient().transcribe
    if duration is None:
        duration = _probe_duration(path)
    spans = plan_chunks(duration)

    tmp = tempfile.mkdtemp(prefix="media-speech-", dir=workdir)
    segments: list[dict] = []
    try:
        prev_words: list[str] = []
        for i, (start, end) in enumerate(spans):
            wav = Path(tmp) / f"chunk_{i:04d}.wav"
            _extract(path, start, end - start, wav)
            words = (transcriber(str(wav), language=language) or "").split()
            wav.unlink(missing_ok=True)
            kept = _strip_overlap(prev_words, words) if prev_words else words
            prev_words = words
            if kept:
                segments.append(
                    {
                        "start": round(start + start_offset, 3),
                        "end": round(end + start_offset, 3),
                        "text": " ".join(kept),
                    }
                )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return segments
