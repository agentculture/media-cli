"""Op module stub: crop, speed, fade (task t11).

Keep the signature ``build(op, ctx) -> list[Node]``; the interface (node types,
context fields, flags) is documented in :mod:`media_cli.media.ops`.
"""

from __future__ import annotations

from media_cli.media.ops import Node, SegmentContext, not_implemented


def build(op, ctx: SegmentContext) -> list[Node]:
    """Return the filter nodes for ``op`` (stub: not implemented yet)."""
    raise not_implemented(op)
