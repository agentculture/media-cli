"""The op-module interface: typed filter nodes, the allowlist, and build contexts.

This module is the *only* way anything reaches an ffmpeg filtergraph (the
second half of the filter-injection defence; the first is
:mod:`media_cli.media.editlist`'s closed schema).  ``media_cli.media.compile``
wires nodes together; the op modules ``visual`` (crop/speed/fade), ``redact``
(box/blur) and ``compose`` (transitions) produce them.

Contract for op modules (obligation o8)
=======================================
Each op module exposes exactly::

    def build(op, ctx) -> list[Node]

and nothing else is called by the compiler.  ``compile.REGISTRY`` maps
``crop``/``speed``/``fade`` -> ``visual``, ``box``/``blur`` -> ``redact``, and
``compile.TRANSITION_REGISTRY`` maps transition ``type`` ``xfade``/``acrossfade``
-> ``compose``.  ``build`` must be pure (no I/O, no subprocess); raise
``MediaInputError`` for a request it cannot honour (``not_implemented(op)`` is
the stub error, kind ``input.op_not_implemented``).

Segment ops: ``build(op: editlist.Crop|Speed|Fade|Box|Blur, ctx: SegmentContext)``
    Returns nodes applied **in order** after the compiler's own trim/re-base of
    the segment.  A ``FilterNode`` goes on the chain named by its ``stream``
    (``'v'`` or ``'a'``); a ``Branch`` goes on the video chain.  Nodes for a
    stream the input lacks are ignored (so ``speed`` may always return its
    ``atempo`` nodes).  Allowed in a segment: ``SEGMENT_FILTERS`` (setpts with
    ``speed`` only -- never ``offset``; crop, scale, fps, format, setsar, fade,
    drawbox, boxblur, null; atempo, afade, aresample, anull).  trim/atrim/
    asetpts/concat/split/overlay/xfade/acrossfade are compiler-only.

Transitions: ``build(transition: editlist.Transition, ctx: JoinContext)``
    Returns exactly one ``FilterNode('xfade', {transition, duration, offset}, 'v')``
    when ``ctx.has_video`` and exactly one ``FilterNode('acrossfade', {d}, 'a')``
    when ``ctx.has_audio`` (xfade ``duration`` must equal acrossfade ``d``).  The
    compiler wires ``[left][right]xfade...`` and ``[left][right]acrossfade...``
    where *left* is everything joined so far.  Use ``ctx.offset`` (=
    ``left_duration - transition.duration``) as the xfade ``offset``.  The
    compiler makes every segment's video CFR at the source fps and the same size
    before a join, as xfade requires.

Nodes
-----
``FilterNode(name, params, stream)``
    Frozen.  ``name`` must be in ``ALLOWLIST``; every param is validated against
    its ``Param`` spec at construction (int / float / enum ``frozenset``, with
    bounds) -- a string is only ever accepted as a member of its enum.
    ``render()`` formats numbers fixed-point (no exponent; nan/inf refused).
    Timeline filters (``TIMELINE_FILTERS``: drawbox, boxblur, overlay) also take
    the float pair ``enable_start``/``enable_end`` (both or neither), rendered as
    ``enable=between(t\\,A\\,B)`` in *chain-local* seconds: always compute them
    with ``ctx.local_time(t)`` from absolute normalized edit-list times.
    ``setpts`` renders ``PTS/speed`` (``speed`` is the playback factor: 2 = twice
    as fast); ``asetpts``/``setpts`` ``offset`` is compiler-only.

``Branch(nodes, x, y, enable_start=None, enable_end=None)``
    A sub-graph for region effects (blur): the compiler renders
    ``split=outputs=2[m][c];[c]<nodes...>[b];[m][b]overlay=x=X:y=Y[:enable=...]``
    and continues the video chain after the overlay.  ``nodes`` are
    ``SEGMENT_FILTERS`` video nodes applied to the *copy* (typically
    ``crop`` to the region then ``boxblur``); ``x``/``y`` place the result back.
    One Branch per region is fine; they chain.

Contexts
--------
``SegmentContext`` (frozen; a fresh one per op call):
    ``index`` segment index; ``op_index`` position of this op in the segment;
    ``start``/``end`` the segment in normalized seconds; ``source_start``/
    ``source_end`` the same in raw container seconds (``probe.to_source_seconds``);
    ``info`` the ``MediaInfo``; ``width``/``height`` the frame size *now* (after
    earlier crops/scales in this segment); ``fps`` (source avg fps, or the last
    ``fps`` node's); ``sample_rate``; ``has_video``/``has_audio``; ``speed`` the
    cumulative playback factor of earlier ops; ``duration`` the segment's current
    output length ``(end - start) / speed``; ``clock_base`` (see ``local_time``);
    ``flags`` a dict **shared by every op of the whole edit** (mutable).
    ``local_time(t)`` maps an absolute normalized edit-list time to the time
    ``t`` seen by filters at this point of the chain (accounts for the trim
    re-base and earlier speed ops).  Never assume the chain starts at 0: when
    the compiler can stream-copy untouched audio it skips the trim, and local
    time is then ffmpeg's input clock.

    Flags the compiler reads: ``flags['redacted'] = True`` -- the output is
    fail-closed: video is always re-encoded, subtitle/attachment (incl. cover
    art)/data streams and container metadata+chapters are dropped unless
    ``keep`` names them.  Any other keys are reported verbatim in the dry-run
    plan (``CompiledEdit.to_dict()['flags']``) and must be JSON-serializable
    (e.g. redaction coverage facts).

``JoinContext`` (frozen): ``index`` (joins segment ``index`` and ``index+1``),
    ``left_duration`` (output length of everything joined so far),
    ``right_duration`` (segment ``index+1`` after its ops), ``offset``
    (``left_duration - transition.duration``), ``info``, ``has_video``,
    ``has_audio``, ``width``, ``height``, ``fps``, ``sample_rate``, ``flags``.
"""

from __future__ import annotations

import math
import re
import types
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Union

from media_cli.media.errors import MediaInputError
from media_cli.media.probe import MediaInfo

INPUT_OP_NOT_IMPLEMENTED = "input.op_not_implemented"

Stream = Literal["v", "a"]

#: Filters that can read files, sockets or command streams.  Never allowlisted.
DENIED_FILTERS = frozenset({"movie", "amovie", "zmq", "azmq", "sendcmd", "asendcmd"})


@dataclass(frozen=True)
class Param:
    """A typed parameter: ``kind`` is ``int``, ``float`` or a frozenset of enum tokens."""

    kind: Any
    lo: float | None = None
    hi: float | None = None
    required: bool = False


Spec = Union[Param, tuple]  # a Param, or a tuple of alternative Params


def _i(lo=None, hi=None, required=False) -> Param:
    return Param(int, lo, hi, required)


def _f(lo=None, hi=None, required=False) -> Param:
    return Param(float, lo, hi, required)


def _e(*values: str, required=False) -> Param:
    return Param(frozenset(values), required=required)


_TINY = 1e-6
_CURVES = ("tri", "qsin", "hsin", "esin", "log", "exp")

#: filter name -> {param: spec}.  The ONLY filters a graph may contain.
ALLOWLIST: dict[str, dict[str, Spec]] = {
    "trim": {"start": _f(0), "end": _f(0)},
    "atrim": {"start": _f(0), "end": _f(0)},
    "setpts": {"offset": _f(0), "speed": _f(0.01, 100)},
    "asetpts": {"offset": _f(0)},
    "concat": {"n": _i(1, 4096, True), "v": _i(0, 1), "a": _i(0, 1)},
    "crop": {
        "w": _i(1, 65535, True),
        "h": _i(1, 65535, True),
        "x": _i(0, 65535),
        "y": _i(0, 65535),
        "exact": _i(0, 1),
    },
    "scale": {
        "w": _i(-2, 65535, True),
        "h": _i(-2, 65535, True),
        "flags": _e("bicubic", "bilinear", "lanczos", "neighbor"),
    },
    "fps": {"fps": _f(0.001, 1000, True)},
    "format": {"pix_fmts": _e("yuv420p", "yuvj420p", "yuv444p", "yuva420p", "rgb24", "rgba")},
    "setsar": {"sar": _f(0.001, 1000, True)},
    "fade": {
        "t": _e("in", "out"),
        "st": _f(0),
        "d": _f(_TINY, 86400),
        "alpha": _i(0, 1),
        "c": _e("black", "white"),
    },
    "afade": {"t": _e("in", "out"), "st": _f(0), "d": _f(_TINY, 86400), "curve": _e(*_CURVES)},
    "atempo": {"tempo": _f(0.5, 100, True)},
    "drawbox": {
        "x": _i(0, 65535, True),
        "y": _i(0, 65535, True),
        "w": _i(1, 65535, True),
        "h": _i(1, 65535, True),
        "color": _e("black"),
        "t": (_e("fill"), _i(1, 65535)),
    },
    "boxblur": {
        "luma_radius": _i(0, 65535),
        "luma_power": _i(0, 1000),
        "chroma_radius": _i(-1, 65535),
        "chroma_power": _i(-1, 1000),
        "alpha_radius": _i(-1, 65535),
        "alpha_power": _i(-1, 1000),
    },
    "split": {"outputs": _i(2, 16)},
    "overlay": {
        "x": _i(-65535, 65535),
        "y": _i(-65535, 65535),
        "eof_action": _e("repeat", "endall", "pass"),
        "shortest": _i(0, 1),
    },
    "xfade": {
        "transition": _e("fade", "dissolve", "wipeleft", "slideleft", required=True),
        "duration": _f(_TINY, 60, True),
        "offset": _f(0, None, True),
    },
    "acrossfade": {"d": _f(_TINY, 60, True), "c1": _e(*_CURVES), "c2": _e(*_CURVES)},
    "aresample": {"osr": _i(8000, 768000), "async": _i(0, 1000000)},
    "null": {},
    "anull": {},
}

#: media type each filter works on ('concat' is compiler-only and nominally 'v').
FILTER_STREAM: dict[str, Stream] = {
    name: (
        "a"
        if name in {"atrim", "asetpts", "afade", "atempo", "acrossfade", "aresample", "anull"}
        else "v"
    )
    for name in ALLOWLIST
}

#: filters accepting the typed ``enable_start``/``enable_end`` float pair.
TIMELINE_FILTERS = frozenset({"drawbox", "boxblur", "overlay"})

#: single-input/single-output filters op modules may return in a segment.
SEGMENT_FILTERS = frozenset(
    {
        "setpts",
        "crop",
        "scale",
        "fps",
        "format",
        "setsar",
        "fade",
        "drawbox",
        "boxblur",
        "null",
        "afade",
        "atempo",
        "aresample",
        "anull",
    }
)

#: two-input filters compose.build returns for a join.
JOIN_FILTERS = frozenset({"xfade", "acrossfade"})

_TOKEN = re.compile(r"^[a-z0-9_]+$")


def _check_table() -> None:
    assert not DENIED_FILTERS & set(ALLOWLIST), "a denied filter is allowlisted"  # nosec B101
    for name, spec in ALLOWLIST.items():
        if not _TOKEN.match(name):
            raise AssertionError(f"filter name {name!r} is not a plain token")
        for pname, alts in spec.items():
            _check_table_param(name, pname, alts)


def _check_table_param(name: str, pname: str, alts: Spec) -> None:
    """Every param name and enum value is a plain token; every kind is int/float/enum."""
    if not _TOKEN.match(pname):
        raise AssertionError(f"param {name}.{pname} is not a plain token")
    for p in alts if isinstance(alts, tuple) else (alts,):
        if isinstance(p.kind, frozenset):
            if not all(isinstance(v, str) and _TOKEN.match(v) for v in p.kind):
                raise AssertionError(f"enum {name}.{pname} has a non-token value")
        elif p.kind not in (int, float):
            raise AssertionError(f"param {name}.{pname} has an untyped kind")


_check_table()


# ------------------------------------------------------------------ rendering


def format_number(value: Any) -> str:
    """Fixed-point rendering: ints as-is, floats to 6 decimals, no exponent.

    ``bool``, non-numbers, NaN and infinities are refused (``TypeError``/``ValueError``).
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"expected a number, got {type(value).__name__}")
    if isinstance(value, int):
        return str(value)
    if not math.isfinite(value):
        raise ValueError(f"refusing non-finite number {value!r}")
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def _check_one(p: Param, value: Any, where: str) -> Any:
    if isinstance(p.kind, frozenset):
        if not isinstance(value, str) or value not in p.kind:
            raise ValueError(f"{where}: must be one of {sorted(p.kind)}")
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{where}: expected {p.kind.__name__}, got {type(value).__name__}")
    if p.kind is int and not isinstance(value, int):
        raise TypeError(f"{where}: expected int, got {type(value).__name__}")
    if not math.isfinite(value):
        raise ValueError(f"{where}: must be finite")
    if p.lo is not None and value < p.lo:
        raise ValueError(f"{where}: {value} < {p.lo}")
    if p.hi is not None and value > p.hi:
        raise ValueError(f"{where}: {value} > {p.hi}")
    return float(value) if p.kind is float else value


def _check_param(spec: Spec, value: Any, where: str) -> Any:
    alts = spec if isinstance(spec, tuple) else (spec,)
    errors: list[Exception] = []
    for p in alts:
        try:
            return _check_one(p, value, where)
        except (TypeError, ValueError) as exc:
            errors.append(exc)
    raise errors[0] if len(errors) == 1 else ValueError(f"{where}: {value!r} matches no spec")


def _render_value(value: Any) -> str:
    return value if isinstance(value, str) else format_number(value)


def _clean_params(name: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Validate ``raw`` against ``ALLOWLIST[name]``: no unknown keys, every required one set."""
    spec = ALLOWLIST[name]
    clean: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in spec:
            raise ValueError(f"{name}: unknown param {key!r}")
        clean[key] = _check_param(spec[key], value, f"{name}.{key}")
    for key, p in spec.items():
        if isinstance(p, Param) and p.required and key not in clean:
            raise ValueError(f"{name}: missing required param {key!r}")
    return clean


def _clean_enable(name: str, start: Any, end: Any) -> tuple[float, float]:
    """Validate the timeline ``enable_start``/``enable_end`` pair (at least one is set)."""
    if name not in TIMELINE_FILTERS:
        raise ValueError(f"{name}: does not support enable_start/enable_end")
    if start is None or end is None:
        raise ValueError(f"{name}: enable_start and enable_end go together")
    start = _check_one(_f(), start, f"{name}.enable_start")
    end = _check_one(_f(), end, f"{name}.enable_end")
    if end < start:
        raise ValueError(f"{name}: enable_end < enable_start")
    return start, end


@dataclass(frozen=True, eq=True)
class FilterNode:
    """One allowlisted filter with typed, validated params (see module docstring)."""

    name: str
    params: Mapping[str, Any] = field(default_factory=dict)
    stream: Stream = "v"

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or self.name not in ALLOWLIST:
            raise ValueError(f"filter {self.name!r} is not allowlisted")
        if self.stream != FILTER_STREAM[self.name]:
            raise ValueError(f"filter {self.name!r} works on stream {FILTER_STREAM[self.name]!r}")
        if not isinstance(self.params, Mapping):
            raise TypeError("params must be a mapping")
        raw = dict(self.params)
        start, end = raw.pop("enable_start", None), raw.pop("enable_end", None)
        clean = _clean_params(self.name, raw)
        if start is not None or end is not None:
            clean["enable_start"], clean["enable_end"] = _clean_enable(self.name, start, end)
        object.__setattr__(self, "params", types.MappingProxyType(clean))

    def __hash__(self) -> int:
        return hash((self.name, self.stream, tuple(sorted(self.params.items()))))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, FilterNode):
            return NotImplemented
        return (self.name, self.stream, dict(self.params)) == (
            other.name,
            other.stream,
            dict(other.params),
        )

    def render(self) -> str:
        """``name[=k=v:...]`` built only from allowlisted names and typed values."""
        p = self.params
        if self.name in ("setpts", "asetpts"):
            expr = "PTS"
            if p.get("offset"):
                expr = f"PTS-{format_number(p['offset'])}/TB"
            speed = p.get("speed", 1.0)
            if not math.isclose(speed, 1.0):
                expr = f"({expr})" if "-" in expr else expr
                expr = f"{expr}/{format_number(speed)}"
            return f"{self.name}={expr}"
        parts = [f"{key}={_render_value(p[key])}" for key in ALLOWLIST[self.name] if key in p]
        if "enable_start" in p:
            a, b = format_number(p["enable_start"]), format_number(p["enable_end"])
            parts.append(f"enable=between(t\\,{a}\\,{b})")
        return self.name + ("=" + ":".join(parts) if parts else "")


@dataclass(frozen=True)
class Branch:
    """Region sub-graph: split, run ``nodes`` on the copy, overlay it back at (x, y)."""

    nodes: tuple[FilterNode, ...]
    x: int
    y: int
    enable_start: float | None = None
    enable_end: float | None = None
    stream: Stream = "v"

    def __post_init__(self) -> None:
        object.__setattr__(self, "nodes", tuple(self.nodes))
        for n in self.nodes:
            if not isinstance(n, FilterNode) or n.stream != "v" or n.name not in SEGMENT_FILTERS:
                raise ValueError(f"Branch nodes must be video SEGMENT_FILTERS nodes, got {n!r}")
        if self.stream != "v":
            raise ValueError("a Branch is always on the video stream")

    def overlay(self) -> FilterNode:
        params: dict[str, Any] = {"x": self.x, "y": self.y}
        if self.enable_start is not None or self.enable_end is not None:
            params["enable_start"], params["enable_end"] = self.enable_start, self.enable_end
        return FilterNode("overlay", params, "v")


Node = Union[FilterNode, Branch]


# ------------------------------------------------------------------ contexts


@dataclass(frozen=True)
class SegmentContext:
    """What ``build(op, ctx)`` may know about the segment (see module docstring)."""

    index: int
    op_index: int
    start: float
    end: float
    source_start: float
    source_end: float
    info: MediaInfo
    width: int | None
    height: int | None
    fps: float | None
    sample_rate: int | None
    has_video: bool
    has_audio: bool
    speed: float
    clock_base: float
    flags: dict = field(compare=False)

    @property
    def duration(self) -> float:
        """Current output length of this segment: ``(end - start) / speed``."""
        return (self.end - self.start) / self.speed

    def local_time(self, t: float) -> float:
        """Absolute normalized seconds -> filter-local ``t`` at this point of the chain."""
        from media_cli.media.compile import input_clock  # local: avoids an import cycle

        return (input_clock(self.info, t) - self.clock_base) / self.speed


@dataclass(frozen=True)
class JoinContext:
    """What ``compose.build(transition, ctx)`` may know about a join."""

    index: int
    left_duration: float
    right_duration: float
    offset: float
    info: MediaInfo
    has_video: bool
    has_audio: bool
    width: int | None
    height: int | None
    fps: float | None
    sample_rate: int | None
    flags: dict = field(compare=False)


def not_implemented(op: Any) -> MediaInputError:
    """The stub error an op module raises until its task fills it in."""
    name = getattr(op, "op", None) or getattr(op, "type", None) or type(op).__name__
    return MediaInputError(
        INPUT_OP_NOT_IMPLEMENTED,
        f"op '{name}' is not implemented yet",
        "remove this op from the edit list, or upgrade media-cli",
    )


__all__ = [
    "ALLOWLIST",
    "DENIED_FILTERS",
    "FILTER_STREAM",
    "INPUT_OP_NOT_IMPLEMENTED",
    "JOIN_FILTERS",
    "SEGMENT_FILTERS",
    "TIMELINE_FILTERS",
    "Branch",
    "FilterNode",
    "JoinContext",
    "Node",
    "Param",
    "SegmentContext",
    "format_number",
    "not_implemented",
]
