"""Edit-list schema, parsing, and validation against probe facts.

Schema (closed: an unknown key anywhere is rejected)
----------------------------------------------------
::

    {
      "input": "in.mp4",                 # str path (non-empty, no NUL, not starting "-")
      "output": "out.mp4",               # str path, same rules
      "overwrite": false,                # bool, default false
      "keep": ["subtitles"],             # enum subset of subtitles|attachments|data|metadata
      "segments": [                      # non-empty
        {"start": 0.0, "end": 4.5,       # numbers; NORMALIZED seconds (see below)
         "ops": [                        # default []; applied in order
           {"op": "crop",  "x": 0, "y": 0, "w": 160, "h": 120},   # ints; x,y>=0; w,h>0
           {"op": "speed", "factor": 2},                          # number 0.25..4.0
           {"op": "fade",  "direction": "in", "duration": 1},     # in|out; 0 < d <= length
           {"op": "box",   "regions": [REGION], "fill": "black"}, # black-box redaction
           {"op": "blur",  "regions": [REGION], "strength": 5}    # int 1..50
         ]}
      ],
      "transitions": [                   # length 0 or len(segments)-1
        {"type": "xfade",                # xfade | acrossfade
         "style": "fade",                # fade|dissolve|wipeleft|slideleft, default fade
         "duration": 0.5}                # number > 0 and < both adjacent segment lengths
      ]
    }

    REGION = {"x": int, "y": int, "w": int, "h": int, "start"?: number, "end"?: number}

Types are strict (first half of the filter-injection defence): numbers are
``int``/``float`` only -- ``bool``, ``str`` (even ``"5"``), ``NaN``, ``inf`` and
``None`` are rejected.  Pixel fields must be integral (``10.0`` is accepted and
coerced to ``10``; ``10.5`` is not).  Every string that could reach a
filtergraph is a closed enum (``op``, ``direction``, ``fill``, transition
``type``/``style``, ``keep`` items); ``input``/``output`` are paths handed to
ffmpeg as argv, never interpolated into a filtergraph.

Time base (approved deviation d1)
---------------------------------
All times are **normalized seconds**: seconds from the first presented video
frame, as defined in :mod:`media_cli.media.probe`.  Validation bounds them to
``[0, info.end]`` (``editable`` in ``media probe --json``); a segment end up to one
nominal frame past ``info.end`` is clamped to it.  The container ``start_time`` is never
added here; the compiler converts to raw timestamps with ``probe.to_source_seconds()``.
Region ``start``/``end`` are in the same absolute normalized base and must lie
inside their segment.  Segment "length" in fade/transition checks is the
*source* length ``end - start`` (before any ``speed`` op).  Regions and crops
are bounded by the frame size at that point in the op chain (a ``crop``
shrinks the frame seen by later ops of the same segment).

Errors are :class:`MediaInputError` whose message starts with a JSON-path-like
pointer, e.g. ``segments[1].ops[0].regions[2].w: ...``.  Kinds:
``input.timestamp_out_of_range`` for times, ``input.region_outside_frame`` for
regions/crops outside the frame, and ``input.editlist_invalid``
(``INPUT_EDITLIST_INVALID``, local to this module) for schema errors.

API: ``parse`` (structure only, no probe), ``validate`` (pure: no I/O, no
subprocess), ``load_and_validate`` (reads the JSON file and probes the input).
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, replace
from typing import Any

from media_cli.media.errors import (
    INPUT_REGION_OUTSIDE_FRAME,
    INPUT_TIMESTAMP_OUT_OF_RANGE,
    INPUT_UNREADABLE,
    MediaInputError,
)
from media_cli.media.probe import EPSILON, MediaInfo

INPUT_EDITLIST_INVALID = "input.editlist_invalid"

KEEP_VALUES = ("subtitles", "attachments", "data", "metadata")
OP_NAMES = ("crop", "speed", "fade", "box", "blur")
FADE_DIRECTIONS = ("in", "out")
BOX_FILLS = ("black",)
TRANSITION_TYPES = ("xfade", "acrossfade")
TRANSITION_STYLES = ("fade", "dissolve", "wipeleft", "slideleft")
SPEED_MIN, SPEED_MAX = 0.25, 4.0
BLUR_MIN, BLUR_MAX = 1, 50


@dataclass(frozen=True)
class Region:
    x: int
    y: int
    w: int
    h: int
    start: float | None = None
    end: float | None = None


@dataclass(frozen=True)
class Crop:
    x: int
    y: int
    w: int
    h: int
    op: str = "crop"


@dataclass(frozen=True)
class Speed:
    factor: float
    op: str = "speed"


@dataclass(frozen=True)
class Fade:
    direction: str
    duration: float
    op: str = "fade"


@dataclass(frozen=True)
class Box:
    regions: tuple[Region, ...]
    fill: str = "black"
    op: str = "box"


@dataclass(frozen=True)
class Blur:
    regions: tuple[Region, ...]
    strength: int
    op: str = "blur"


Op = Crop | Speed | Fade | Box | Blur


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    ops: tuple[Op, ...] = ()


@dataclass(frozen=True)
class Transition:
    type: str
    duration: float
    style: str = "fade"


@dataclass(frozen=True)
class EditList:
    input: str
    output: str
    segments: tuple[Segment, ...]
    overwrite: bool = False
    keep: tuple[str, ...] = ()
    transitions: tuple[Transition, ...] = ()


# ---------------------------------------------------------------- errors


def _invalid(path: str, msg: str) -> MediaInputError:
    return MediaInputError(
        INPUT_EDITLIST_INVALID,
        f"{path}: {msg}",
        "fix the edit list at that JSON path (see `media explain` for the schema)",
    )


def _time_err(path: str, msg: str, remediation: str | None = None) -> MediaInputError:
    return MediaInputError(
        INPUT_TIMESTAMP_OUT_OF_RANGE,
        f"{path}: {msg}",
        remediation
        or "use normalized seconds (from the first presented frame) within the media and segment",
    )


def _region_err(path: str, msg: str) -> MediaInputError:
    return MediaInputError(
        INPUT_REGION_OUTSIDE_FRAME,
        f"{path}: {msg}",
        "keep x,y >= 0 and x+w, y+h within the frame size at that point in the op chain",
    )


# ---------------------------------------------------------------- parse helpers


def _join(path: str, key: str | int) -> str:
    if isinstance(key, int):
        return f"{path}[{key}]"
    return f"{path}.{key}" if path else key


def _obj(value: Any, path: str, required: tuple[str, ...], optional: tuple[str, ...]) -> dict:
    if not isinstance(value, dict):
        raise _invalid(path or "$", "expected an object")
    for k in value:
        if k not in required and k not in optional:
            raise _invalid(_join(path, str(k)), "unknown key")
    for k in required:
        if k not in value:
            raise _invalid(_join(path, k), "missing required key")
    return value


def _list(value: Any, path: str) -> list:
    if not isinstance(value, list):
        raise _invalid(path, "expected a list")
    return value


def _number(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _invalid(path, f"expected a number, got {type(value).__name__}")
    if not math.isfinite(value):
        raise _invalid(path, "expected a finite number")
    return value


def _pixels(value: Any, path: str, *, minimum: int = 0) -> int:
    n = _number(value, path)
    if n != int(n):
        raise _invalid(path, "expected an integer number of pixels")
    n = int(n)
    if n < minimum:
        raise _invalid(path, f"must be >= {minimum}")
    return n


def _enum(value: Any, path: str, allowed: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise _invalid(path, f"must be one of {', '.join(allowed)}")
    return value


def _path_str(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise _invalid(path, "expected a non-empty path string")
    if "\x00" in value:
        raise _invalid(path, "path must not contain NUL")
    if value.startswith("-"):
        raise _invalid(path, "path must not start with '-'")
    return value


def _region(value: Any, path: str) -> Region:
    d = _obj(value, path, ("x", "y", "w", "h"), ("start", "end"))
    return Region(
        x=_pixels(d["x"], _join(path, "x")),
        y=_pixels(d["y"], _join(path, "y")),
        w=_pixels(d["w"], _join(path, "w"), minimum=1),
        h=_pixels(d["h"], _join(path, "h"), minimum=1),
        start=_number(d["start"], _join(path, "start")) if "start" in d else None,
        end=_number(d["end"], _join(path, "end")) if "end" in d else None,
    )


def _regions(value: Any, path: str) -> tuple[Region, ...]:
    items = _list(value, path)
    if not items:
        raise _invalid(path, "must not be empty")
    return tuple(_region(r, _join(path, i)) for i, r in enumerate(items))


def _op(value: Any, path: str) -> Op:
    if not isinstance(value, dict):
        raise _invalid(path, "expected an object")
    name = _enum(value.get("op"), _join(path, "op"), OP_NAMES)
    if name == "crop":
        d = _obj(value, path, ("op", "x", "y", "w", "h"), ())
        return Crop(
            x=_pixels(d["x"], _join(path, "x")),
            y=_pixels(d["y"], _join(path, "y")),
            w=_pixels(d["w"], _join(path, "w"), minimum=1),
            h=_pixels(d["h"], _join(path, "h"), minimum=1),
        )
    if name == "speed":
        d = _obj(value, path, ("op", "factor"), ())
        f = _number(d["factor"], _join(path, "factor"))
        if not SPEED_MIN <= f <= SPEED_MAX:
            raise _invalid(_join(path, "factor"), f"must be between {SPEED_MIN} and {SPEED_MAX}")
        return Speed(factor=f)
    if name == "fade":
        d = _obj(value, path, ("op", "direction", "duration"), ())
        dur = _number(d["duration"], _join(path, "duration"))
        if dur <= 0:
            raise _invalid(_join(path, "duration"), "must be > 0")
        return Fade(
            direction=_enum(d["direction"], _join(path, "direction"), FADE_DIRECTIONS), duration=dur
        )
    if name == "box":
        d = _obj(value, path, ("op", "regions", "fill"), ())
        return Box(
            regions=_regions(d["regions"], _join(path, "regions")),
            fill=_enum(d["fill"], _join(path, "fill"), BOX_FILLS),
        )
    d = _obj(value, path, ("op", "regions", "strength"), ())
    s = _pixels(d["strength"], _join(path, "strength"))
    if not BLUR_MIN <= s <= BLUR_MAX:
        raise _invalid(_join(path, "strength"), f"must be between {BLUR_MIN} and {BLUR_MAX}")
    return Blur(regions=_regions(d["regions"], _join(path, "regions")), strength=s)


def _segment(value: Any, path: str) -> Segment:
    d = _obj(value, path, ("start", "end"), ("ops",))
    ops = _list(d.get("ops", []), _join(path, "ops"))
    return Segment(
        start=_number(d["start"], _join(path, "start")),
        end=_number(d["end"], _join(path, "end")),
        ops=tuple(_op(o, _join(_join(path, "ops"), i)) for i, o in enumerate(ops)),
    )


def _transition(value: Any, path: str) -> Transition:
    d = _obj(value, path, ("type", "duration"), ("style",))
    dur = _number(d["duration"], _join(path, "duration"))
    if dur <= 0:
        raise _invalid(_join(path, "duration"), "must be > 0")
    return Transition(
        type=_enum(d["type"], _join(path, "type"), TRANSITION_TYPES),
        style=_enum(d.get("style", "fade"), _join(path, "style"), TRANSITION_STYLES),
        duration=dur,
    )


def parse(source: Any) -> EditList:
    """Parse a JSON text or an already-decoded object into an ``EditList``.

    Structural and type checks only (needs no probe).  Raises
    ``MediaInputError`` (``input.editlist_invalid``) with a JSON-path pointer.
    """
    if isinstance(source, (str, bytes)):
        try:
            source = json.loads(source)
        except ValueError as exc:
            raise _invalid("$", f"not valid JSON ({exc})") from exc
    d = _obj(source, "", ("input", "output", "segments"), ("overwrite", "keep", "transitions"))
    overwrite = d.get("overwrite", False)
    if not isinstance(overwrite, bool):
        raise _invalid("overwrite", "expected a boolean")
    keep = _list(d.get("keep", []), "keep")
    segs = _list(d["segments"], "segments")
    if not segs:
        raise _invalid("segments", "must not be empty")
    trans = _list(d.get("transitions", []), "transitions")
    if len(trans) not in (0, len(segs) - 1):
        raise _invalid(
            "transitions", f"expected 0 or {len(segs) - 1} transitions, got {len(trans)}"
        )
    return EditList(
        input=_path_str(d["input"], "input"),
        output=_path_str(d["output"], "output"),
        overwrite=overwrite,
        keep=tuple(_enum(k, f"keep[{i}]", KEEP_VALUES) for i, k in enumerate(keep)),
        segments=tuple(_segment(s, f"segments[{i}]") for i, s in enumerate(segs)),
        transitions=tuple(_transition(t, f"transitions[{i}]") for i, t in enumerate(trans)),
    )


# ---------------------------------------------------------------- validate


def _check_regions(
    regions: tuple[Region, ...], path: str, frame: tuple[int, int] | None, seg: Segment
) -> None:
    for i, r in enumerate(regions):
        rp = _join(path, i)
        if frame is not None:
            fw, fh = frame
            for axis, pos, size, limit in (("w", r.x, r.w, fw), ("h", r.y, r.h, fh)):
                if pos + size > limit:
                    # blame the origin if it is itself off-frame, else the extent
                    field = ("x" if axis == "w" else "y") if pos >= limit else axis
                    raise _region_err(
                        _join(rp, field),
                        f"region extends past the {limit}px frame ({pos} + {size})",
                    )
        lo = r.start if r.start is not None else seg.start
        hi = r.end if r.end is not None else seg.end
        if r.start is not None and r.start < seg.start - EPSILON:
            raise _time_err(
                _join(rp, "start"), f"{r.start} is before the segment start {seg.start}"
            )
        if r.end is not None and r.end > seg.end + EPSILON:
            raise _time_err(_join(rp, "end"), f"{r.end} is after the segment end {seg.end}")
        if r.start is not None and r.end is not None and hi <= lo:
            raise _time_err(_join(rp, "end"), "end must be greater than start")


def _validate_ops(seg: Segment, path: str, info: MediaInfo) -> None:
    video = info.video
    frame = (
        None
        if video is None or not video.width or not video.height
        else (
            video.width,
            video.height,
        )
    )
    length = seg.end - seg.start
    for j, op in enumerate(seg.ops):
        op_path = _join(_join(path, "ops"), j)
        spatial = isinstance(op, (Crop, Box, Blur))
        if spatial and video is None:
            raise _invalid(op_path, f"'{op.op}' needs a video stream but the input has none")
        if isinstance(op, Crop) and frame is not None:
            if op.x + op.w > frame[0]:
                raise _region_err(_join(op_path, "w"), f"crop extends past the {frame[0]}px frame")
            if op.y + op.h > frame[1]:
                raise _region_err(_join(op_path, "h"), f"crop extends past the {frame[1]}px frame")
            frame = (op.w, op.h)
        elif isinstance(op, (Box, Blur)):
            _check_regions(op.regions, _join(op_path, "regions"), frame, seg)
        elif isinstance(op, Fade) and op.duration > length + EPSILON:
            raise _time_err(
                _join(op_path, "duration"),
                f"fade {op.duration}s is longer than its segment ({length:.3f}s)",
            )


def validate(editlist: EditList, info: MediaInfo) -> EditList:
    """Check ``editlist`` against probe facts.  Pure: no I/O, no subprocess.

    Returns the edit list to use.  A segment end at or up to one nominal frame interval
    (``info.frame_interval``) past ``info.end`` -- the ``editable.end`` of ``media probe
    --json`` -- is clamped to ``info.end``; ends further beyond raise.  Without a known
    frame interval there is no slack beyond ``EPSILON``.
    """
    end_limit = info.end
    slack = info.frame_interval or 0.0
    clamped = []
    for seg in editlist.segments:
        if end_limit + EPSILON < seg.end <= end_limit + slack + EPSILON:
            seg = replace(seg, end=end_limit)
        clamped.append(seg)
    if tuple(clamped) != editlist.segments:
        editlist = replace(editlist, segments=tuple(clamped))
    for i, seg in enumerate(editlist.segments):
        path = f"segments[{i}]"
        if seg.start < -EPSILON or seg.start > end_limit + EPSILON:
            raise _time_err(
                _join(path, "start"),
                f"{seg.start}s is outside the media (0 to {end_limit:.3f}s)",
            )
        if seg.end > end_limit + EPSILON or seg.end < -EPSILON:
            raise _time_err(
                _join(path, "end"),
                f"{seg.end}s is outside the media (0 to {end_limit:.3f}s; the editable end is "
                f"{end_limit:.3f}s, `media probe <file> --json` -> editable.end)",
                "read editable.end from `media probe <file> --json` and use an end "
                "at or below it (within one frame past it is clamped)",
            )
        if seg.end <= seg.start:
            raise _time_err(
                _join(path, "end"), f"end {seg.end} must be greater than start {seg.start}"
            )
        _validate_ops(seg, path, info)
    segs = editlist.segments
    if len(editlist.transitions) not in (0, max(len(segs) - 1, 0)):
        raise _invalid("transitions", f"expected 0 or {len(segs) - 1} transitions")
    for i, t in enumerate(editlist.transitions):
        shortest = min(segs[i].end - segs[i].start, segs[i + 1].end - segs[i + 1].start)
        if t.duration >= shortest - EPSILON:
            raise _time_err(
                f"transitions[{i}].duration",
                f"{t.duration}s must be shorter than both adjacent segments "
                f"(shortest {shortest:.3f}s)",
            )
    return editlist


def load_and_validate(path: str | os.PathLike) -> EditList:
    """Read an edit-list JSON file, probe its input, and validate.  (Does I/O.)"""
    from media_cli.media.probe import probe  # local: keeps validate's module surface pure

    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise MediaInputError(
            INPUT_UNREADABLE,
            f"cannot read edit list {path}: {exc}",
            "check the path exists and is a UTF-8 JSON file",
        ) from exc
    editlist = parse(text)
    return validate(editlist, probe(editlist.input))
