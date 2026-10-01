"""Frame peek: extract still frames and a contact sheet, entirely locally.

Public API
----------
``extract(path, *, times=None, every=None, scene=None, outdir, max_frames=200,
overwrite=False, outdir_create=False) -> list[{"t", "frame_index", "path"}]``
    Exactly one of ``times`` (list of seconds), ``every`` (seconds between
    frames, starting at 0) or ``scene`` (threshold 0..1 for
    ``select='gt(scene,thr)'``).  Writes one PNG per frame, named
    ``frame_t<seconds>_f<index>.png`` (zero-padded, so a directory listing
    sorts chronologically).
``contact_sheet(frames, dst, *, cols=4, width=320, overwrite=False) -> str``
    Tile extracted frames (dicts from ``extract`` or plain paths) into one PNG
    with ffmpeg's ``scale`` + ``tile`` filters.

Time base: every ``t`` in and out is **normalized** seconds (see
``media_cli.media.probe``).  Each requested time is snapped to the frame
presented at that time, so the returned ``t`` is that frame's own normalized
time and round-trips: ``probe.to_frame_index(path, t) == frame_index`` and
``probe.to_seconds(path, frame_index) == t``.

Seeking: ``-ss`` goes *before* ``-i`` (fast keyframe seek; ffmpeg then decodes
up to the target, which is frame-accurate).  ffmpeg measures an input ``-ss``
from the *container* start time, not from raw 0, so the raw timestamp from
``probe.to_source_seconds`` has ``info.start_time`` taken back off before it is
handed to ``-ss`` (see ``_seek_arg``); the seek lands 0.5 ms before the wanted
frame so float noise cannot skip it.  Scene discovery reads raw presentation
times with ``-copyts`` and converts them with ``probe.from_source_seconds``.

Writes are atomic per file (hidden temp in ``outdir`` then rename; all temps
are created first and renamed together at the end), so a failure leaves no
partial or temp file.  ``output.plan_output`` is not used: it plans media
*containers* from a source (and would rewrite a ``.png`` destination to
``.mkv``), which does not fit still images.

Runs fully offline and never imports ``media_cli.media.senses``.  All ffmpeg
calls go through ``media_cli.media._tools``.
"""

from __future__ import annotations

import contextlib
import math
import os
import re
import tempfile
import uuid
from typing import Any, Sequence

from media_cli.media import _tools, output, probe
from media_cli.media.errors import MediaInputError, ffmpeg_failure

INPUT_BAD_FRAME_REQUEST = "input.bad_frame_request"
INPUT_TOO_MANY_FRAMES = "input.too_many_frames"
INPUT_OUTPUT_EXISTS = output.INPUT_OUTPUT_EXISTS
INPUT_OUTPUT_DIR_MISSING = output.INPUT_OUTPUT_DIR_MISSING

_SEEK_LEAD = 0.0005  # seconds before the wanted frame's own time
_TIMEOUT = 120
_PTS_RE = re.compile(r"pts_time:\s*(-?[0-9.]+)")


def _bad(message: str, remediation: str) -> MediaInputError:
    return MediaInputError(INPUT_BAD_FRAME_REQUEST, message, remediation)


def _seek_arg(info: probe.MediaInfo, t: float) -> float:
    """Value for ``-ss`` that lands just before normalized time ``t``."""
    raw = probe.to_source_seconds(info, t)
    return max(raw - info.start_time - _SEEK_LEAD, 0.0)


def _name(t: float, index: int) -> str:
    return f"frame_t{t:010.3f}_f{index:05d}.png"


def _discard(*paths: str) -> None:
    for p in paths:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(p)


def _tmp_for(dst: str) -> str:
    d, base = os.path.split(os.path.abspath(dst))
    return os.path.join(d, f".{base}.{uuid.uuid4().hex[:12]}.tmp.png")


def _check_dst(dst: str, overwrite: bool) -> None:
    if os.path.lexists(dst) and not overwrite:
        raise MediaInputError(
            INPUT_OUTPUT_EXISTS,
            f"output already exists: {dst}",
            "choose another directory or pass overwrite=True (--overwrite)",
        )


def _check_dir(outdir: str, create: bool) -> None:
    if os.path.isdir(outdir):
        return
    if create and not os.path.lexists(outdir):
        os.makedirs(outdir)
        return
    raise MediaInputError(
        INPUT_OUTPUT_DIR_MISSING,
        f"output directory does not exist: {outdir}",
        "create the directory first or pass outdir_create=True",
    )


def _commit_all(pairs: Sequence[tuple[str, str]], overwrite: bool) -> None:
    """Move every temp to its destination; never clobbers unless ``overwrite``."""
    temps = [t for t, _ in pairs]
    try:
        for tmp, dst in pairs:
            _check_dst(dst, overwrite)
            if overwrite:
                os.replace(tmp, dst)
            else:
                try:  # link() fails if dst appeared meanwhile: never clobbers
                    os.link(tmp, dst)
                except FileExistsError:
                    _check_dst(dst, False)
                except OSError:
                    os.rename(tmp, dst)
                _discard(tmp)
    except BaseException:
        _discard(*temps)
        raise


def _scene_times(info: probe.MediaInfo, path: str, thr: float) -> list[float]:
    video = info.video
    cp = _tools.run(
        "ffmpeg",
        [
            *("-hide_banner", "-copyts", "-i", path, "-map", f"0:{video.index}"),
            *("-an", "-sn", "-vf", f"select='gt(scene,{thr})',showinfo"),
            *("-f", "null", "-"),
        ],
        timeout=_TIMEOUT,
    )
    out: list[float] = []
    for line in (cp.stderr or "").splitlines():
        if "showinfo" not in line:
            continue
        m = _PTS_RE.search(line)
        if m:
            out.append(probe.from_source_seconds(info, float(m.group(1))))
    return out


def _requested_times(
    info: probe.MediaInfo,
    path: str,
    times: Sequence[float] | None,
    every: float | None,
    scene: float | None,
    max_frames: int,
) -> list[float]:
    chosen = [m for m, v in (("times", times), ("every", every), ("scene", scene)) if v is not None]
    if len(chosen) != 1:
        raise _bad(
            "give exactly one of times, every or scene",
            "pass one selector, e.g. times=[1.5, 4.0], every=2.0 or scene=0.3",
        )
    if times is not None:
        if not times:
            raise _bad("times is empty", "pass at least one timestamp in seconds")
        out = [float(t) for t in times]
        for t in out:
            if not math.isfinite(t):
                raise MediaInputError(
                    probe.INPUT_TIMESTAMP_OUT_OF_RANGE,
                    f"timestamp {t} is not a finite number of seconds",
                    "use a time in seconds from the first presented frame",
                )
        return out
    if every is not None:
        if not (math.isfinite(every) and every > 0):
            raise _bad(f"every must be a positive number of seconds, got {every}", "use e.g. 2.0")
        # Times 0, every, 2*every ... strictly before the end of the media.
        n = math.ceil((min(info.end, 1e9) - probe.EPSILON) / every)
        n = max(n, 1)
        if n > max_frames:
            raise _too_many(n, max_frames)
        return [i * every for i in range(n)]
    if not (0.0 <= scene <= 1.0):
        raise _bad(f"scene threshold must be within 0..1, got {scene}", "use e.g. 0.3")
    return _scene_times(info, path, scene)


def _too_many(n: int, max_frames: int) -> MediaInputError:
    return MediaInputError(
        INPUT_TOO_MANY_FRAMES,
        f"{n} frames requested, above the limit of {max_frames}",
        "ask for fewer frames (larger every, higher scene threshold) or raise max_frames",
    )


def _render_one(info: probe.MediaInfo, path: str, t: float, tmp: str) -> None:
    _tools.run(
        "ffmpeg",
        [
            *("-hide_banner", "-loglevel", "error", "-y"),
            *("-ss", f"{_seek_arg(info, t):.6f}", "-i", path),
            *("-map", f"0:{info.video.index}", "-frames:v", "1"),
            *("-an", "-sn", "-c:v", "png", "-f", "image2", "-update", "1", tmp),
        ],
        timeout=_TIMEOUT,
    )
    if not os.path.isfile(tmp) or os.path.getsize(tmp) == 0:
        raise ffmpeg_failure("", f"no frame decoded at {t:.3f}s")


def extract(
    path: str | os.PathLike,
    *,
    times: Sequence[float] | None = None,
    every: float | None = None,
    scene: float | None = None,
    outdir: str | os.PathLike,
    max_frames: int = 200,
    overwrite: bool = False,
    outdir_create: bool = False,
) -> list[dict[str, Any]]:
    """Extract PNG frames; returns ``[{"t", "frame_index", "path"}]`` in time order."""
    src = os.fspath(path)
    out_dir = os.path.abspath(os.fspath(outdir))
    info = probe.probe(src)
    if info.video is None:
        raise _bad(f"{src} has no video stream", "frames can only be taken from video")
    requested = _requested_times(info, src, times, every, scene, max_frames)

    # Snap every request to the frame presented at that time; dedupe frames.
    by_index: dict[int, float] = {}
    for t in requested:
        idx = probe.to_frame_index(info, t)  # raises input.timestamp_out_of_range
        by_index.setdefault(idx, probe.to_seconds(info, idx))
    if len(by_index) > max_frames:
        raise _too_many(len(by_index), max_frames)
    plan = [(idx, by_index[idx]) for idx in sorted(by_index)]

    _check_dir(out_dir, outdir_create)
    dsts = [os.path.join(out_dir, _name(t, idx)) for idx, t in plan]
    for dst in dsts:
        _check_dst(dst, overwrite)

    pairs: list[tuple[str, str]] = []
    try:
        for (_idx, t), dst in zip(plan, dsts):
            tmp = _tmp_for(dst)
            pairs.append((tmp, dst))
            _render_one(info, src, t, tmp)
    except BaseException:
        _discard(*(t for t, _ in pairs))
        raise
    _commit_all(pairs, overwrite)
    return [{"t": t, "frame_index": idx, "path": dst} for (idx, t), dst in zip(plan, dsts)]


def _frame_path(frame: Any) -> str:
    return os.fspath(frame["path"] if isinstance(frame, dict) else frame)


def _concat_quote(p: str) -> str:
    return "'" + p.replace("'", "'\\''") + "'"


def contact_sheet(
    frames: Sequence[Any],
    dst: str | os.PathLike,
    *,
    cols: int = 4,
    width: int = 320,
    overwrite: bool = False,
) -> str:
    """Tile ``frames`` into one PNG (``scale`` + ``tile``); returns ``dst``."""
    paths = [os.path.abspath(_frame_path(f)) for f in frames]
    if not paths:
        raise _bad("no frames to tile", "pass the list returned by extract()")
    if cols < 1 or width < 1:
        raise _bad("cols and width must be positive", "use e.g. cols=4, width=320")
    for p in paths:
        if not os.path.isfile(p):
            raise _bad(f"frame file not found: {p}", "pass files produced by extract()")
    out = os.path.abspath(os.fspath(dst))
    if not os.path.isdir(os.path.dirname(out)):
        raise MediaInputError(
            INPUT_OUTPUT_DIR_MISSING,
            f"output directory does not exist: {os.path.dirname(out)}",
            "create the directory first",
        )
    _check_dst(out, overwrite)
    _tools.require_filter("tile")

    cols = min(cols, len(paths))
    rows = math.ceil(len(paths) / cols)
    tmp = _tmp_for(out)
    try:
        with tempfile.TemporaryDirectory() as td:
            lst = os.path.join(td, "frames.txt")
            with open(lst, "w", encoding="utf-8") as fh:
                for p in paths:
                    fh.write(f"file {_concat_quote(p)}\nduration 1\n")
            _tools.run(
                "ffmpeg",
                [
                    *("-hide_banner", "-loglevel", "error", "-y"),
                    *("-f", "concat", "-safe", "0", "-i", lst),
                    *("-vf", f"scale={int(width)}:-2,tile={cols}x{rows}"),
                    *("-frames:v", "1", "-c:v", "png", "-f", "image2", "-update", "1", tmp),
                ],
                timeout=_TIMEOUT,
            )
        if not os.path.isfile(tmp) or os.path.getsize(tmp) == 0:
            raise ffmpeg_failure("", "contact sheet produced no output")
        _commit_all([(tmp, out)], overwrite)
    except BaseException:
        _discard(tmp)
        raise
    return out
