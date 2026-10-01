"""Visual ops: crop, speed, fade (task t11).

``build(op, ctx) -> list[Node]`` is pure: it maps an edit-list op onto allowlisted
:class:`~media_cli.media.ops.FilterNode` objects (interface documented in
:mod:`media_cli.media.ops`).

* ``crop``  -> ``crop`` (video).
* ``speed`` -> ``setpts`` (``speed`` factor) on video plus a chain of ``atempo``
  on audio.  ``atempo`` only accepts 0.5-2.0 per instance, so factors outside that
  range are split into equal instances whose product is the factor; video and
  audio therefore always agree on the cumulative speed (the compiler rejects a
  mismatch).
* ``fade``  -> ``fade`` (video) + ``afade`` (audio).  Times are filter-local
  seconds from ``ctx.local_time`` so earlier speed ops and the trim re-base are
  accounted for; the duration is clamped to the segment's current length.
"""

from __future__ import annotations

from media_cli.media import editlist as E
from media_cli.media.ops import FilterNode, Node, SegmentContext, not_implemented

ATEMPO_MIN = 0.5
ATEMPO_MAX = 2.0


def _tempos(factor: float) -> list[float]:
    """Split ``factor`` into equal atempo instances, each within 0.5-2.0."""
    if factor == 1.0:
        return [1.0]
    n = 1
    while not ATEMPO_MIN <= factor ** (1.0 / n) <= ATEMPO_MAX:
        n += 1
    return [factor ** (1.0 / n)] * n


def _crop(op: E.Crop) -> list[Node]:
    return [FilterNode("crop", {"x": op.x, "y": op.y, "w": op.w, "h": op.h}, "v")]


def _speed(op: E.Speed) -> list[Node]:
    nodes: list[Node] = [FilterNode("setpts", {"speed": float(op.factor)}, "v")]
    nodes += [FilterNode("atempo", {"tempo": t}, "a") for t in _tempos(float(op.factor))]
    return nodes


def _fade(op: E.Fade, ctx: SegmentContext) -> list[Node]:
    d = min(float(op.duration), ctx.duration)
    first, last = ctx.local_time(ctx.start), ctx.local_time(ctx.end)
    st = first if op.direction == "in" else max(last - d, first)
    st = max(st, 0.0)
    params = {"t": op.direction, "st": st, "d": d}
    return [FilterNode("fade", params, "v"), FilterNode("afade", params, "a")]


def build(op, ctx: SegmentContext) -> list[Node]:
    """Return the filter nodes for a crop, speed or fade op."""
    if isinstance(op, E.Crop):
        return _crop(op)
    if isinstance(op, E.Speed):
        return _speed(op)
    if isinstance(op, E.Fade):
        return _fade(op, ctx)
    raise not_implemented(op)
