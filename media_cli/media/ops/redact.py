"""Redaction ops: black ``box`` and ``blur`` (task t12).

``build(op, ctx) -> list[Node]`` (interface in :mod:`media_cli.media.ops`);
pure -- no I/O, no subprocess.

Fail-closed output (obligation o9)
----------------------------------
Every box/blur sets ``ctx.flags['redacted'] = True``.  The compiler then
re-encodes the video (never ``-c:v copy``), maps every kept A/V stream
explicitly, drops subtitle / attachment (incl. cover art) / data streams and
emits ``-map_metadata -1 -map_chapters -1`` unless ``keep`` names them.

Coverage report
---------------
``ctx.flags['redaction']`` is a list (shared across the whole edit) with one
JSON-serializable entry per op, so the dry-run plan says exactly what will be
covered::

    {"op": "box"|"blur", "segment": i, "op_index": j,
     "fill": "black",                      # box only
     "strength": s,                        # blur only
     "regions": [{
        "requested": {"x","y","w","h","start","end"},   # as written (start/end may be null)
        "covered":   {"x","y","w","h"},                 # pixels actually covered
        "window":    {"start","end","covers_from","whole_segment"},  # absolute seconds
        "luma_radius", "chroma_radius"                  # blur only (after clamping)
     }]}

Decisions
---------
*Coordinates.*  Region pixels are in the coordinates of the frame *at this point
of the op chain*: after an earlier ``crop`` they are relative to the cropped
frame, exactly what :func:`editlist.validate` bounds them against.  No
translation is done; ``build`` re-checks every region against
``ctx.width``/``ctx.height`` and refuses (``input.region_outside_frame``) one
that does not fit -- never silently clipped or shifted.

*Time.*  ``start``/``end`` are absolute normalized seconds; they are clamped to
the segment and mapped with ``ctx.local_time``.  A region without a window, or
whose window spans the whole segment, is applied unconditionally (no ``enable``
expression, so no edge rounding can let a frame through).  A partial window
covers every frame *presented* during ``[start, end]``: the frame already on
screen at ``start`` (time <= start, as ``probe.to_frame_index`` defines it) is
included by pulling the enable start back one nominal frame interval
(``1/ctx.fps``; ``FALLBACK_FRAME_INTERVAL`` when unknown), and the inclusive end
is widened by ``WINDOW_TOLERANCE`` to absorb float/time-base rounding.  It is
never narrowed.  Limitation: build is pure (no frame-time probe), so on a VFR
source a gap before ``start`` longer than the nominal interval can leave the
frame on screen at ``start`` uncovered -- start such a window at or before that
frame's own time.  ``window.covers_from`` reports the effective start.

*Box* is one ``drawbox ... color=black:t=fill`` per region; drawbox fills luma
pixel-exactly at any offset.

*Blur* is one ``Branch`` per region: ``crop`` (``exact=1``) the region from a
copy, ``boxblur`` it, overlay it back.  Two pixel-format hazards are handled
fail-closed:

* ``overlay`` (and non-exact ``crop``) round x/y *down* to the chroma grid on
  4:2:0 video, which would shift the blurred patch left/up by one pixel and
  leave the region's right/bottom edge unblurred.  The covered rectangle is
  therefore expanded *outward* to even x/y (and an even right/bottom edge,
  clamped to the frame): at most one extra pixel per edge, reported in
  ``covered``.  Over-covering one pixel beats leaking one.
* ``boxblur`` refuses a radius ``r`` with ``2r > min(plane w, h)``.  ``strength``
  (1..50) is the luma radius in pixels, clamped to ``min(w, h) // 2``; the
  chroma radius uses the 4:2:0 worst case ``min(ceil(w/2), ceil(h/2)) // 2``
  (valid for every less-subsampled format too).  The effective radii are
  reported.  A covered rectangle under ``MIN_BLUR_SIDE`` px on either side cannot
  be meaningfully blurred and is refused (``input.region_too_small``) -- use a
  black ``box`` for it.
"""

from __future__ import annotations

from typing import Any

from media_cli.media import editlist as E
from media_cli.media.errors import INPUT_REGION_OUTSIDE_FRAME, MediaInputError
from media_cli.media.ops import Branch, FilterNode, Node, SegmentContext

INPUT_REGION_TOO_SMALL = "input.region_too_small"

#: seconds a partial window is widened by on each side (inclusive edges).
WINDOW_TOLERANCE = 1e-3
#: nominal frame interval (s) when the frame rate is unknown (fail closed: wide).
FALLBACK_FRAME_INTERVAL = 0.1
#: smallest side (px) of a blur's covered rectangle.
MIN_BLUR_SIDE = 4
_GRID = 2  # 4:2:0 chroma grid: the worst case among common pixel formats
_EDGE = 1e-6  # seconds; "the window is the whole segment" tolerance


def _outside(path: str, msg: str) -> MediaInputError:
    return MediaInputError(
        INPUT_REGION_OUTSIDE_FRAME,
        f"{path}: {msg}",
        "regions after a crop are relative to the cropped frame; keep x+w and y+h inside it",
    )


def _check_frame(r: E.Region, ctx: SegmentContext, path: str) -> None:
    fw, fh = ctx.width, ctx.height
    if fw is not None and r.x + r.w > fw:
        raise _outside(path, f"region x {r.x} + w {r.w} extends past the {fw}px-wide frame")
    if fh is not None and r.y + r.h > fh:
        raise _outside(path, f"region y {r.y} + h {r.h} extends past the {fh}px-high frame")


def _window(r: E.Region, ctx: SegmentContext) -> tuple[dict[str, Any], dict[str, float]]:
    """(report window, enable params) -- params empty when the whole segment is covered.

    The frame *presented* at ``start`` (the last frame with time <= start, as in
    ``probe.to_frame_index``) is covered too: the enable start is pulled back by
    one nominal frame interval (then nudged forward by ``WINDOW_TOLERANCE`` so
    a CFR frame exactly one interval earlier -- not on screen at ``start`` --
    stays out).  The end is inclusive plus ``WINDOW_TOLERANCE``.
    """
    lo = ctx.start if r.start is None else max(r.start, ctx.start)
    hi = ctx.end if r.end is None else min(r.end, ctx.end)
    hi = max(hi, lo)
    interval = 1.0 / ctx.fps if ctx.fps else FALLBACK_FRAME_INTERVAL
    from_abs = lo - interval
    if from_abs <= ctx.start + _EDGE:
        from_abs = ctx.start
        enable_start = ctx.local_time(ctx.start) - WINDOW_TOLERANCE
    else:
        enable_start = ctx.local_time(from_abs) + WINDOW_TOLERANCE
    whole = from_abs <= ctx.start + _EDGE and hi >= ctx.end - _EDGE
    report = {"start": lo, "end": hi, "covers_from": from_abs, "whole_segment": whole}
    if whole:
        return report, {}
    enable_end = ctx.local_time(hi) + WINDOW_TOLERANCE
    return report, {"enable_start": enable_start, "enable_end": enable_end}


def _requested(r: E.Region) -> dict[str, Any]:
    return {"x": r.x, "y": r.y, "w": r.w, "h": r.h, "start": r.start, "end": r.end}


def _aligned(r: E.Region, ctx: SegmentContext) -> tuple[int, int, int, int]:
    """Expand (x, y, w, h) outward to the chroma grid, never past the frame."""
    x0, y0 = r.x - r.x % _GRID, r.y - r.y % _GRID
    x1, y1 = r.x + r.w, r.y + r.h
    x1 += -x1 % _GRID
    y1 += -y1 % _GRID
    if ctx.width is not None:
        x1 = min(x1, ctx.width)
    if ctx.height is not None:
        y1 = min(y1, ctx.height)
    return x0, y0, x1 - x0, y1 - y0


def _box(op: E.Box, ctx: SegmentContext, path: str) -> tuple[list[Node], dict[str, Any]]:
    nodes: list[Node] = []
    regions = []
    for i, r in enumerate(op.regions):
        _check_frame(r, ctx, f"{path}.regions[{i}]")
        window, enable = _window(r, ctx)
        params = {"x": r.x, "y": r.y, "w": r.w, "h": r.h, "color": "black", "t": "fill"}
        nodes.append(FilterNode("drawbox", {**params, **enable}, "v"))
        regions.append(
            {
                "requested": _requested(r),
                "covered": {"x": r.x, "y": r.y, "w": r.w, "h": r.h},
                "window": window,
            }
        )
    return nodes, {"op": "box", "fill": op.fill, "regions": regions}


def _blur(op: E.Blur, ctx: SegmentContext, path: str) -> tuple[list[Node], dict[str, Any]]:
    nodes: list[Node] = []
    regions = []
    for i, r in enumerate(op.regions):
        rp = f"{path}.regions[{i}]"
        _check_frame(r, ctx, rp)
        x, y, w, h = _aligned(r, ctx)
        if min(w, h) < MIN_BLUR_SIDE:
            raise MediaInputError(
                INPUT_REGION_TOO_SMALL,
                f"{rp}: a {w}x{h}px area is too small to blur (minimum "
                f"{MIN_BLUR_SIDE}x{MIN_BLUR_SIDE}px)",
                "enlarge the region, or redact it with a black box op instead",
            )
        luma = min(op.strength, min(w, h) // 2)
        chroma = min(op.strength, min(-(-w // _GRID), -(-h // _GRID)) // 2)
        window, enable = _window(r, ctx)
        blur = {"luma_radius": luma, "luma_power": 2, "chroma_radius": chroma, "chroma_power": 2}
        nodes.append(
            Branch(
                nodes=(
                    FilterNode("crop", {"x": x, "y": y, "w": w, "h": h, "exact": 1}, "v"),
                    FilterNode("boxblur", blur, "v"),
                ),
                x=x,
                y=y,
                enable_start=enable.get("enable_start"),
                enable_end=enable.get("enable_end"),
            )
        )
        regions.append(
            {
                "requested": _requested(r),
                "covered": {"x": x, "y": y, "w": w, "h": h},
                "window": window,
                "luma_radius": luma,
                "chroma_radius": chroma,
            }
        )
    return nodes, {"op": "blur", "strength": op.strength, "regions": regions}


def build(op, ctx: SegmentContext) -> list[Node]:
    """Filter nodes covering exactly ``op.regions``; marks the edit as redacted."""
    path = f"segments[{ctx.index}].ops[{ctx.op_index}]"
    if not isinstance(op, (E.Box, E.Blur)):
        raise MediaInputError(
            E.INPUT_EDITLIST_INVALID,
            f"{path}: redact cannot build op {getattr(op, 'op', type(op).__name__)!r}",
            "only box and blur are redaction ops",
        )
    if not ctx.has_video:
        raise MediaInputError(
            E.INPUT_EDITLIST_INVALID,
            f"{path}: '{op.op}' needs a video stream but the input has none",
            "redaction only applies to video",
        )
    if isinstance(op, E.Box):
        nodes, entry = _box(op, ctx, path)
    else:
        nodes, entry = _blur(op, ctx, path)
    ctx.flags["redacted"] = True
    ctx.flags.setdefault("redaction", []).append(
        {"segment": ctx.index, "op_index": ctx.op_index, **entry}
    )
    return nodes
