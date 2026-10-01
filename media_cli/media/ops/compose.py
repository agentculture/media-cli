"""Op module stub: transitions: xfade/acrossfade joins (task t13).

Keep the signature ``build(op, ctx) -> list[Node]``; the interface (node types,
context fields, flags) is documented in :mod:`media_cli.media.ops`.
"""

from __future__ import annotations

from media_cli.media.ops import JoinContext, Node, not_implemented


def build(op, ctx: JoinContext) -> list[Node]:
    """Return the filter nodes for ``op`` (stub: not implemented yet)."""
    raise not_implemented(op)
