"""ffprobe facts and THE single time base for every media-cli timestamp.

Time base (one definition, used by frames, edit lists, search hits, compiler)
-----------------------------------------------------------------------------
Every time this module returns or accepts is **seconds from the first
presented frame**:

    normalized = raw_container_pts_seconds - origin

where ``origin`` is the raw presentation time of the first presented frame of
the first real video stream (its ``start_time``; attached cover art is
ignored).  A file with no video uses the container ``start_time`` instead.
So the first video frame is always at ``0.0``, whatever the container's
start offset (e.g. a file whose audio starts at 1.477s and video at 1.5s has
``origin == 1.5``; the raw ``MediaInfo.start_time`` of 1.477 is reported for
transparency but is *not* the origin).

A timestamp produced by search (or any other task) in this base can be put
unchanged into an edit list and selects the same frame: ``to_frame_index``
is the frame presented at ``t`` -- the last frame whose time is ``<= t`` (plus
a 0.1 ms tolerance for ffprobe's microsecond rounding) -- and ``to_seconds``
inverts it exactly.

Talking to ffmpeg: ``-ss``, ``-to``, ``trim`` and ``select=gte(t,..)`` work on
**raw** timestamps.  Convert with ``to_source_seconds(info, t)`` (adds
``origin``) before building such an argument, and ``from_source_seconds(info,
ts)`` (subtracts it) for any raw time read back out of ffmpeg/ffprobe output.

VFR: frame times are read lazily with ``ffprobe -show_entries
frame=pts_time`` only when a frame-index mapping is requested, sorted into
presentation order, normalized, and cached per (path, mtime, size).

Editable range: ``to_dict()`` carries ``"editable": {"start": 0.0, "end": <MediaInfo.end>,
"frame_interval": <nominal 1/fps of the video, or null>}`` -- the exact bound edit
planning uses (all in the base above).  ``end`` is the container span, which can run past
the last video frame (e.g. an audio tail); an edit-list end within one ``frame_interval``
of it is clamped to it and counts as the whole file (see ``editlist.validate`` and
``compile._full_range``).

Every ffprobe call goes through ``media_cli.media._tools.run``.
"""

from __future__ import annotations

import bisect
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from media_cli.media import _tools
from media_cli.media.errors import (
    ENV_FFMPEG_FAILED,
    INPUT_TIMESTAMP_OUT_OF_RANGE,
    INPUT_UNREADABLE,
    MediaEnvError,
    MediaInputError,
)

EPSILON = 1e-4  # seconds; ffprobe prints 6 decimals
_PROBE_TIMEOUT = 60
_DISPOSITION_KEYS = ("default", "attached_pic", "forced", "comment")


@dataclass(frozen=True)
class StreamInfo:
    index: int
    type: str
    codec: str | None
    duration: float | None = None
    start_time: float | None = None  # raw container seconds
    fps: float | None = None
    width: int | None = None
    height: int | None = None
    sample_rate: int | None = None
    channels: int | None = None
    disposition: dict[str, int] = field(default_factory=dict)
    tags: dict[str, str] = field(default_factory=dict)

    @property
    def attached_pic(self) -> bool:
        return bool(self.disposition.get("attached_pic"))


@dataclass(frozen=True)
class MediaInfo:
    path: str
    format_name: str
    duration: float | None  # raw container duration, seconds
    start_time: float  # raw container start_time (transparency only)
    streams: tuple[StreamInfo, ...]
    tags: dict[str, str] = field(default_factory=dict)

    @property
    def video(self) -> StreamInfo | None:
        return next((s for s in self.streams if s.type == "video" and not s.attached_pic), None)

    @property
    def audio(self) -> StreamInfo | None:
        return next((s for s in self.streams if s.type == "audio"), None)

    @property
    def origin(self) -> float:
        """Raw seconds of the first presented frame (see module docstring)."""
        v = self.video
        if v is not None and v.start_time is not None:
            return v.start_time
        return self.start_time

    @property
    def end(self) -> float:
        """Normalized end of the media (seconds from the first presented frame)."""
        if self.duration is None:
            return float("inf")
        return self.start_time + self.duration - self.origin

    @property
    def frame_interval(self) -> float | None:
        """Nominal seconds per video frame (1/fps), or None without a video/fps."""
        v = self.video
        return 1.0 / v.fps if v is not None and v.fps else None

    def to_source_seconds(self, t: float) -> float:
        return t + self.origin

    def from_source_seconds(self, ts: float) -> float:
        return ts - self.origin

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["streams"] = [asdict(s) | {"attached_pic": s.attached_pic} for s in self.streams]
        d["video"] = None if self.video is None else d["streams"][self.streams.index(self.video)]
        d["audio"] = None if self.audio is None else d["streams"][self.streams.index(self.audio)]
        d["origin"] = self.origin
        d["editable"] = {"start": 0.0, "end": self.end, "frame_interval": self.frame_interval}
        return d


def _unreadable(path: str, why: str) -> MediaInputError:
    return MediaInputError(
        INPUT_UNREADABLE,
        f"cannot read media file {path}: {why}",
        "check the path exists and is an intact audio/video file (not truncated or corrupt)",
    )


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    n = _num(value)
    return None if n is None else int(n)


def _fps(value: Any) -> float | None:
    try:
        num, den = str(value).split("/")
        return float(num) / float(den) if float(den) else None
    except (ValueError, TypeError):
        return _num(value)


def _stream(raw: dict[str, Any]) -> StreamInfo:
    kind = raw.get("codec_type") or "unknown"
    disp = {k: int(v) for k, v in (raw.get("disposition") or {}).items() if k in _DISPOSITION_KEYS}
    fps = _fps(raw.get("avg_frame_rate")) if kind == "video" else None
    return StreamInfo(
        index=int(raw.get("index", 0)),
        type=kind,
        codec=raw.get("codec_name"),
        duration=_num(raw.get("duration")),
        start_time=_num(raw.get("start_time")),
        fps=fps or None,
        width=_int(raw.get("width")),
        height=_int(raw.get("height")),
        sample_rate=_int(raw.get("sample_rate")),
        channels=_int(raw.get("channels")),
        disposition=disp,
        tags=dict(raw.get("tags") or {}),
    )


def _run_ffprobe(path: str, args: list[str]) -> str:
    try:
        cp = _tools.run("ffprobe", args, check=False, timeout=_PROBE_TIMEOUT)
    except MediaEnvError as exc:
        if exc.kind == ENV_FFMPEG_FAILED and "timed out" in exc.message:
            raise _unreadable(path, "ffprobe timed out") from exc
        raise
    if cp.returncode != 0:
        tail = (cp.stderr or "").strip().splitlines()[-1:] or ["ffprobe failed"]
        raise _unreadable(path, tail[0])
    return cp.stdout


def _key(path: Path) -> tuple[str, int, int]:
    st = path.stat()
    return (str(path.resolve()), st.st_mtime_ns, st.st_size)


_INFO_CACHE: dict[tuple[str, int, int], MediaInfo] = {}
_TIMES_CACHE: dict[tuple[str, int, int], tuple[float, ...]] = {}


def clear_cache() -> None:
    _INFO_CACHE.clear()
    _TIMES_CACHE.clear()


def _checked(path: str | os.PathLike) -> Path:
    p = Path(path)
    if not p.is_file():
        raise _unreadable(str(path), "not a file" if p.exists() else "no such file")
    return p


def probe(path: str | os.PathLike) -> MediaInfo:
    """Run ``ffprobe -print_format json`` and return format + per-stream facts."""
    p = _checked(path)
    key = _key(p)
    cached = _INFO_CACHE.get(key)
    if cached is not None:
        return cached
    out = _run_ffprobe(
        str(path),
        ["-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(p)],
    )
    try:
        data = json.loads(out)
        fmt = data["format"]
    except (ValueError, KeyError) as exc:
        raise _unreadable(str(path), "ffprobe returned no usable format data") from exc
    streams = tuple(_stream(s) for s in data.get("streams") or [])
    if not streams:
        raise _unreadable(str(path), "no audio/video streams found")
    info = MediaInfo(
        path=str(path),
        format_name=fmt.get("format_name", ""),
        duration=_num(fmt.get("duration")),
        start_time=_num(fmt.get("start_time")) or 0.0,
        streams=streams,
        tags=dict(fmt.get("tags") or {}),
    )
    _INFO_CACHE[key] = info
    return info


def _info(info_or_path: MediaInfo | str | os.PathLike) -> MediaInfo:
    return info_or_path if isinstance(info_or_path, MediaInfo) else probe(info_or_path)


def origin(info_or_path: MediaInfo | str | os.PathLike) -> float:
    """Raw container seconds of the first presented frame (the time-base zero)."""
    return _info(info_or_path).origin


def to_source_seconds(info_or_path: MediaInfo | str | os.PathLike, t: float) -> float:
    """Normalized seconds -> raw container seconds (for ffmpeg -ss / trim)."""
    return _info(info_or_path).to_source_seconds(t)


def from_source_seconds(info_or_path: MediaInfo | str | os.PathLike, ts: float) -> float:
    """Raw container seconds -> normalized seconds."""
    return _info(info_or_path).from_source_seconds(ts)


def _times(info: MediaInfo) -> tuple[float, ...]:
    video = info.video
    if video is None:
        raise _unreadable(info.path, "no video stream, so there are no frames to index")
    p = _checked(info.path)
    key = _key(p)
    cached = _TIMES_CACHE.get(key)
    if cached is not None:
        return cached
    out = _run_ffprobe(
        info.path,
        [
            "-v",
            "error",
            "-select_streams",
            str(video.index),
            "-show_entries",
            "frame=pts_time,best_effort_timestamp_time",
            "-of",
            "csv=p=0",
            str(p),
        ],
    )
    raw: list[float] = []
    for line in out.splitlines():
        vals = [_num(c) for c in line.split(",") if c.strip()]
        vals = [v for v in vals if v is not None]
        if vals:
            raw.append(vals[0])
    if not raw:
        raise _unreadable(info.path, "no decodable video frames")
    base = info.origin
    times = tuple(sorted(r - base for r in raw))
    _TIMES_CACHE[key] = times
    return times


def frame_times(info_or_path: MediaInfo | str | os.PathLike) -> list[float]:
    """Presentation-ordered frame times in the normalized base (lazy, cached)."""
    return list(_times(_info(info_or_path)))


def to_frame_index(info_or_path: MediaInfo | str | os.PathLike, t: float) -> int:
    """Index of the frame presented at ``t``: the last frame with time <= t."""
    info = _info(info_or_path)
    if t < -EPSILON or t > info.end + EPSILON:
        raise MediaInputError(
            INPUT_TIMESTAMP_OUT_OF_RANGE,
            f"timestamp {t:.3f}s is outside the media (0 to {info.end:.3f}s)",
            "use a time in seconds from the first presented frame",
        )
    times = _times(info)
    return max(bisect.bisect_right(times, t + EPSILON) - 1, 0)


def to_seconds(info_or_path: MediaInfo | str | os.PathLike, frame_index: int) -> float:
    """Normalized presentation time of frame ``frame_index``."""
    times = _times(_info(info_or_path))
    if not 0 <= frame_index < len(times):
        raise MediaInputError(
            INPUT_TIMESTAMP_OUT_OF_RANGE,
            f"frame index {frame_index} is outside 0..{len(times) - 1}",
            "use a frame index within the file's frame count",
        )
    return times[frame_index]
