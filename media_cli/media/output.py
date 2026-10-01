"""Output writer: decide copy/re-encode per stream, and commit atomically.

Planning (``plan_output``)
--------------------------
* The output container is the source container (by extension, then by
  ffprobe ``format_name``).  Untouched streams are stream-copied.
* A *touched* stream (one the edit re-renders) keeps its source codec when this
  ffmpeg has an encoder for it; otherwise video becomes H.264 (``libx264``) and
  audio AAC.  Attached cover art and subtitle/data streams are never
  re-rendered (always ``copy``).  ``drop_streams`` are left out of the output.
* If the container cannot carry the resulting codecs, the result falls back to
  Matroska: ``dst``'s extension becomes ``.mkv`` and ``container_fallback``
  states why (``allow_fallback=False`` raises instead).

Container/codec compatibility (deliberately conservative -- a codec not listed
for mp4/mov/webm/avi is treated as "cannot carry"; matroska carries anything)::

    mp4   video h264 hevc av1 vp9 mpeg4 mjpeg png | audio aac mp3 ac3 alac
          (no opus/vorbis/flac/pcm) | subs mov_text
    mov   video h264 hevc mpeg4 mjpeg prores png  | audio aac mp3 alac
          pcm_s16le                                | subs mov_text
    webm  video vp8 vp9 av1                        | audio vorbis opus
          | subs webvtt
    avi   video h264 mpeg4 mjpeg                   | audio mp3 aac ac3 pcm_s16le
    matroska  everything

Committing (obligation o5)
--------------------------
ffmpeg writes to ``OutputPlan.tmp_path`` (hidden, unique, same directory and
extension as ``dst``).  ``commit`` renames it into place; ``atomic_output``
commits on success and discards the temp on *any* exception (including
KeyboardInterrupt/SystemExit).  ``dst`` is therefore either the complete result
or absent (an existing ``dst`` is untouched unless ``overwrite=True``, and even
then is only replaced by the atomic rename).  The source is never opened for
writing, and ``dst == src`` (same path, symlink, or hard link) is always refused.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from dataclasses import dataclass
from typing import Any, Iterator, Sequence

from media_cli.media import _tools, probe
from media_cli.media.errors import ENV_FFMPEG_FAILED, MediaEnvError, MediaInputError

INPUT_OUTPUT_IS_SOURCE = "input.output_is_source"
INPUT_OUTPUT_EXISTS = "input.output_exists"
INPUT_OUTPUT_DIR_MISSING = "input.output_dir_missing"
INPUT_CONTAINER_INCOMPATIBLE = "input.container_incompatible"
INPUT_OUTPUT_CONTAINER_MISMATCH = "input.output_container_mismatch"

_EXT_CONTAINER = {
    ".mp4": "mp4",
    ".m4v": "mp4",
    ".mov": "mov",
    ".mkv": "matroska",
    ".webm": "webm",
    ".avi": "avi",
}
_CONTAINER_EXTS = {
    "mp4": (".mp4", ".m4v", ".m4a"),
    "mov": (".mov",),
    "matroska": (".mkv",),
    "webm": (".webm",),
    "avi": (".avi",),
}
_CONTAINER_EXT = {"mp4": ".mp4", "mov": ".mov", "matroska": ".mkv", "webm": ".webm", "avi": ".avi"}

_VIDEO_ENCODERS = {
    "h264": "libx264",
    "hevc": "libx265",
    "vp8": "libvpx",
    "vp9": "libvpx-vp9",
    "av1": "libsvtav1",
    "mjpeg": "mjpeg",
}
_AUDIO_ENCODERS = {
    "aac": "aac",
    "opus": "libopus",
    "mp3": "libmp3lame",
    "vorbis": "libvorbis",
    "flac": "flac",
    "pcm_s16le": "pcm_s16le",
}
_DEFAULT_VIDEO = ("h264", "libx264")
_DEFAULT_AUDIO = ("aac", "aac")

# container -> media type -> carried codecs.  Missing container/type == carries all.
_CARRIES: dict[str, dict[str, frozenset[str]]] = {
    "mp4": {
        "video": frozenset({"h264", "hevc", "av1", "vp9", "mpeg4", "mjpeg", "png"}),
        "audio": frozenset({"aac", "mp3", "ac3", "alac"}),
        "subtitle": frozenset({"mov_text"}),
    },
    "mov": {
        "video": frozenset({"h264", "hevc", "mpeg4", "mjpeg", "prores", "png"}),
        "audio": frozenset({"aac", "mp3", "alac", "pcm_s16le"}),
        "subtitle": frozenset({"mov_text"}),
    },
    "webm": {
        "video": frozenset({"vp8", "vp9", "av1"}),
        "audio": frozenset({"vorbis", "opus"}),
        "subtitle": frozenset({"webvtt"}),
    },
    "avi": {
        "video": frozenset({"h264", "mpeg4", "mjpeg"}),
        "audio": frozenset({"mp3", "aac", "ac3", "pcm_s16le"}),
        "subtitle": frozenset(),
    },
}


@dataclass(frozen=True)
class StreamDecision:
    index: int
    type: str
    action: str  # 'copy' | 'encode' | 'drop'
    codec: str | None  # output codec name (source codec when copied)
    encoder: str | None
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "type": self.type,
            "action": self.action,
            "codec": self.codec,
            "encoder": self.encoder,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class OutputPlan:
    src: str
    dst: str
    tmp_path: str
    container: str
    overwrite: bool
    streams: tuple[StreamDecision, ...]
    container_fallback: str | None = None
    requested_dst: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "src": self.src,
            "dst": self.dst,
            "requested_dst": self.requested_dst or self.dst,
            "container": self.container,
            "container_fallback": self.container_fallback,
            "overwrite": self.overwrite,
            "streams": [s.to_dict() for s in self.streams],
        }

    def ffmpeg_output_args(self, map_labels: dict[int, str] | None = None) -> list[str]:
        """``-map`` / ``-c:<out>`` / ``-f`` args (no filtergraph, no input/output path).

        ``map_labels`` maps a source stream index to a filtergraph output label
        (e.g. ``{0: "[v0]"}``) for streams the compiler re-renders.
        """
        labels = map_labels or {}
        kept = [d for d in self.streams if d.action != "drop"]
        args: list[str] = []
        for d in kept:
            args += ["-map", labels.get(d.index, f"0:{d.index}")]
        for pos, d in enumerate(kept):
            args += [f"-c:{pos}", d.encoder if d.action == "encode" and d.encoder else "copy"]
        return [*args, "-f", self.container]


def _container_of(path: str, format_name: str) -> str | None:
    ext = os.path.splitext(path)[1].lower()
    if ext in _EXT_CONTAINER:
        return _EXT_CONTAINER[ext]
    names = set(format_name.split(","))
    if "matroska" in names:
        return "webm" if "webm" in names and "matroska" not in names else "matroska"
    if "mov" in names or "mp4" in names:
        return "mp4"
    if "avi" in names:
        return "avi"
    return None


def _carries(container: str, typ: str, codec: str | None) -> bool:
    table = _CARRIES.get(container)
    if table is None or typ not in table:
        return True  # matroska, or a stream type we do not police (data/attachment)
    return codec in table[typ]


def _encoder_for(typ: str, codec: str | None) -> tuple[str, str, str]:
    """(output codec, encoder, reason) for a re-rendered stream."""
    table = _VIDEO_ENCODERS if typ == "video" else _AUDIO_ENCODERS
    default = _DEFAULT_VIDEO if typ == "video" else _DEFAULT_AUDIO
    if codec in table:
        enc = table[codec]
        if _tools.has_encoder(enc):
            return codec, enc, f"re-encode with source codec via {enc}"
        why = f"no encoder {enc} for {codec}; using {default[1]}"
    else:
        why = f"no known encoder for {codec!r}; using {default[1]}"
    return default[0], default[1], why


def _same_file(a: str, b: str) -> bool:
    if os.path.realpath(a) == os.path.realpath(b):
        return True
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _check_dst(src: str, dst: str, overwrite: bool) -> None:
    if _same_file(src, dst):
        raise MediaInputError(
            INPUT_OUTPUT_IS_SOURCE,
            f"output path is the source file: {dst}",
            "choose a different output path; the source is never overwritten",
        )
    d = os.path.dirname(os.path.abspath(dst))
    if not os.path.isdir(d):
        raise MediaInputError(
            INPUT_OUTPUT_DIR_MISSING,
            f"output directory does not exist: {d}",
            "create the directory or choose another output path",
        )
    if os.path.lexists(dst) and not overwrite:
        raise MediaInputError(
            INPUT_OUTPUT_EXISTS,
            f"output already exists: {dst}",
            "choose another path or pass overwrite=True (--overwrite)",
        )


def _check_extension(dst: str, container: str | None) -> None:
    """Refuse a requested output name whose extension is not the source container's."""
    exts = _CONTAINER_EXTS.get(container or "")
    if exts is None or os.path.splitext(dst)[1].lower() in exts:
        return
    want = " or ".join(f"'{e}'" for e in exts)
    raise MediaInputError(
        INPUT_OUTPUT_CONTAINER_MISMATCH,
        f"output extension of {dst!r} does not match the source container ({container}); "
        f"expected {want}",
        f"name the output with {want}; the output container is always the source container",
    )


def plan_output(
    src: str | os.PathLike,
    dst: str | os.PathLike,
    touched_streams: Sequence[int] | set[int],
    *,
    info: probe.MediaInfo | None = None,
    drop_streams: Sequence[int] = (),
    overwrite: bool = False,
    allow_fallback: bool = True,
) -> OutputPlan:
    """Decide per-stream copy/encode/drop, the container, and the temp path."""
    src_s, dst_s = os.fspath(src), os.fspath(dst)
    info = info or probe.probe(src_s)
    touched, dropped = set(touched_streams), set(drop_streams)

    container = _container_of(src_s, info.format_name)
    _check_extension(dst_s, container)
    fallback: str | None = None
    decisions: list[StreamDecision] = []
    for s in info.streams:
        if s.index in dropped:
            decisions.append(StreamDecision(s.index, s.type, "drop", None, None, "dropped"))
        elif s.index in touched and s.type in ("video", "audio") and not s.attached_pic:
            codec, enc, why = _encoder_for(s.type, s.codec)
            decisions.append(StreamDecision(s.index, s.type, "encode", codec, enc, why))
        else:
            why = "untouched; stream-copied"
            if s.index in touched:
                why = "not re-renderable (cover art/subtitle/data); stream-copied"
            decisions.append(StreamDecision(s.index, s.type, "copy", s.codec, None, why))

    kept = [d for d in decisions if d.action != "drop"]
    bad = [d for d in kept if container and not _carries(container, d.type, d.codec)]
    if container is None or bad:
        what = ", ".join(f"{d.type}:{d.codec}" for d in bad) or f"format {info.format_name!r}"
        reason = f"{container or 'source container'} cannot carry {what}; falling back to matroska"
        if not allow_fallback:
            raise MediaInputError(
                INPUT_CONTAINER_INCOMPATIBLE,
                reason.replace("falling back to matroska", "and fallback is disabled"),
                "allow the matroska fallback or pick a container that carries these codecs",
            )
        container, fallback = "matroska", reason
        root = os.path.splitext(dst_s)[0]
        dst_s = root + ".mkv"
    _check_dst(src_s, dst_s, overwrite)

    d = os.path.dirname(os.path.abspath(dst_s))
    ext = os.path.splitext(dst_s)[1] or _CONTAINER_EXT.get(container, "")
    tmp = os.path.join(d, f".{os.path.basename(dst_s)}.{uuid.uuid4().hex[:12]}.tmp{ext}")
    return OutputPlan(
        src=src_s,
        dst=dst_s,
        tmp_path=tmp,
        container=container,
        overwrite=overwrite,
        streams=tuple(decisions),
        container_fallback=fallback,
        requested_dst=os.fspath(dst),
    )


def discard(plan: OutputPlan) -> None:
    """Remove the temp file if present (idempotent)."""
    with contextlib.suppress(FileNotFoundError):
        os.unlink(plan.tmp_path)


def commit(plan: OutputPlan) -> str:
    """Atomically move the finished temp file to ``dst``; returns ``dst``.

    On any failure the temp file is removed and ``dst`` is left as it was.
    """
    try:
        if not os.path.isfile(plan.tmp_path) or os.path.getsize(plan.tmp_path) == 0:
            raise MediaEnvError(
                ENV_FFMPEG_FAILED,
                "no output was produced",
                f"expected a non-empty file at {plan.tmp_path}",
            )
        _check_dst(plan.src, plan.dst, plan.overwrite)
        if plan.overwrite:
            os.replace(plan.tmp_path, plan.dst)
        else:
            try:  # link() fails if dst appeared meanwhile: never clobbers
                os.link(plan.tmp_path, plan.dst)
            except FileExistsError:
                _check_dst(plan.src, plan.dst, False)
            except OSError:
                os.rename(plan.tmp_path, plan.dst)  # fs without hard links
            discard(plan)
    except BaseException:
        discard(plan)
        raise
    return plan.dst


@contextlib.contextmanager
def atomic_output(plan: OutputPlan) -> Iterator[str]:
    """Yield ``tmp_path``; commit on success, discard on any exception."""
    try:
        yield plan.tmp_path
    except BaseException:
        discard(plan)
        raise
    commit(plan)
