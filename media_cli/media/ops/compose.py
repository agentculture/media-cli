"""Op module: transitions: xfade (video) + acrossfade (audio) joins (task t13).

``build(op, ctx)`` (``op`` is the edit-list Transition) is called once per join by the compiler.  The
edit-list ``type`` (``xfade`` | ``acrossfade``) only selects this module in
``compile.TRANSITION_REGISTRY``; it does not change the result -- a join always
yields the video xfade *and* the audio acrossfade so the two streams stay the
same length.  The ``style`` enum (fade, dissolve, wipeleft, slideleft) picks the
xfade ``transition``.

xfade needs matching resolution, fps, pixel format and SAR on both inputs; the
compiler normalizes every segment (scale + setsar to the first segment's size,
fps to the source rate) before wiring the join, so this module stays pure.
"""

from __future__ import annotations

from media_cli.media import editlist
from media_cli.media.ops import FilterNode, JoinContext, Node, not_implemented


def build(op, ctx: JoinContext) -> list[Node]:
    """One xfade when ``ctx.has_video`` and one acrossfade when ``ctx.has_audio``."""
    style = getattr(op, "style", None)
    if style not in editlist.TRANSITION_STYLES or not (ctx.has_video or ctx.has_audio):
        raise not_implemented(op)
    duration = op.duration
    nodes: list[Node] = []
    if ctx.has_video:
        nodes.append(
            FilterNode(
                "xfade",
                {"transition": style, "duration": duration, "offset": ctx.offset},
                "v",
            )
        )
    if ctx.has_audio:
        nodes.append(FilterNode("acrossfade", {"d": duration}, "a"))
    return nodes
