"""Compile a validated edit list into ONE ffmpeg argv (the filter-injection core).

Security (obligation o7)
------------------------
The filtergraph is assembled only from :class:`~media_cli.media.ops.FilterNode`
objects, whose names come from the typed ``ALLOWLIST`` and whose params are
ints, fixed-point floats or members of closed enum sets.  Stream labels are
generated here from integers.  No user string (path, key, value) is ever
interpolated into the graph: ``input``/``output`` travel as their own argv
elements (``file:<abs path>`` and the absolute temp path).  ``movie``,
``amovie``, ``zmq``, ``azmq``, ``sendcmd`` and ``asendcmd`` are not in the
table, and :func:`_assert_safe_graph` re-checks every rendered graph before it
is returned: every filter token must be allowlisted and no quote may appear.

Op dispatch (obligation o8)
---------------------------
Every segment op goes through ``REGISTRY[op.op].build(op, ctx)`` and every
transition through ``TRANSITION_REGISTRY[t.type].build(t, ctx)`` -- nothing
else.  The interface is documented in :mod:`media_cli.media.ops`.

Time base (approved deviation d1)
---------------------------------
Edit-list times are normalized seconds (first presented video frame = 0).  The
one conversion into ffmpeg is :func:`input_clock`.  This module never passes
``-copyts``, so trim/atrim and input ``-ss``/``-to`` all see ffmpeg's *input
clock* ``raw_pts - container start_time`` (verified on ffmpeg 6.1 with the
offset VFR fixture: normalized 2.0 there is raw 3.5 and input clock 2.023).

Modes
-----
``filter``      Every segment is ``trim``-ed (start/end on the input clock,
                biased by ``-probe.EPSILON`` so a frame at the cut point is in),
                re-based with ``setpts=PTS-<trim start>/TB`` (a constant, not
                STARTPTS, so the local clock is exact even when a VFR segment's
                first frame lies after the cut), run through its ops, then
                concatenated (``concat``) or joined by transitions (``xfade`` +
                ``acrossfade``).  Frame-accurate: trim + re-encode.
``passthrough`` A single segment covering the whole file (starts at the first frame, ends
                within half a nominal frame of ``editable.end``) whose ops touch only
                the video frames (no ``a`` nodes, no retiming): no trim, the
                audio is stream-copied, and the video keeps the input clock so
                A/V sync with the copied audio is preserved.
``remux``       Whole file, no ops: every kept stream is copied.
``fast``        ``fast=True``: a single op-free segment stream-copied with
                input ``-ss``/``-to``.  Exact only when both points are
                keyframes; otherwise start snaps back to the previous keyframe,
                end forward to the next one (or the media end), and ``snapped``
                reports the points used.  A stream-copy end is packet (DTS)
                accurate, so B-frame codecs may run a few frames past it.

Streams
-------
Subtitle, attachment (incl. cover art) and data streams are dropped unless
``keep`` names them (a cut would leave them mistimed).  Extra audio/video
streams are dropped when the primary ones are retimed or the edit redacts.
When an op sets ``flags['redacted']`` the edit fails closed: the video is always
re-encoded and container metadata + chapters are dropped (``-map_metadata -1
-map_chapters -1``) unless ``keep`` contains ``metadata``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

from media_cli.media import _tools
from media_cli.media import editlist as E
from media_cli.media import output
from media_cli.media.errors import MediaInputError
from media_cli.media.ops import (
    ALLOWLIST,
    DENIED_FILTERS,
    JOIN_FILTERS,
    SEGMENT_FILTERS,
    Branch,
    FilterNode,
    JoinContext,
    Node,
    SegmentContext,
    compose,
    format_number,
    redact,
    visual,
)
from media_cli.media.probe import EPSILON, MediaInfo

INPUT_FAST_UNSUPPORTED = "input.fast_unsupported"

#: op name -> module exposing ``build(op, ctx)``.  Fixed; the only op dispatch.
REGISTRY: dict[str, Any] = {
    "crop": visual,
    "speed": visual,
    "fade": visual,
    "box": redact,
    "blur": redact,
}
#: transition type -> module exposing ``build(transition, ctx)``.
TRANSITION_REGISTRY: dict[str, Any] = {"xfade": compose, "acrossfade": compose}

if set(REGISTRY) != set(E.OP_NAMES) or set(TRANSITION_REGISTRY) != set(E.TRANSITION_TYPES):
    raise ImportError("compile registries are out of sync with the edit-list enums")

_KEYFRAME_TOL = 1e-3  # seconds; ffprobe prints 6 decimals
_DEFAULT_FPS = 25.0
_PROBE_TIMEOUT = 120
_TIMING_FILTERS = frozenset({"setpts", "fps"})
#: the fixed output pad labels of the compiled graph
_VOUT = "[vout]"
_AOUT = "[aout]"


def input_clock(info: MediaInfo, t: float) -> float:
    """Normalized seconds -> the clock ffmpeg applies to trim/atrim and input -ss/-to.

    The single conversion point.  ``probe.to_source_seconds`` gives the raw
    container time; without ``-copyts`` ffmpeg shifts every input timestamp by
    ``-start_time`` (the container start, e.g. 1.477s when audio starts before
    video at 1.5s) before filtering, and input ``-ss`` is measured from that
    same start.  So the clock is ``raw - info.start_time``.  Never add or
    subtract a start offset anywhere else.
    """
    return info.to_source_seconds(t) - info.start_time


# ------------------------------------------------------------------ graph assembly


_LABEL = re.compile(r"\[[a-z0-9:_]+\]")


def _assert_safe_graph(graph: str) -> None:
    """Final guard: only allowlisted filter tokens, no quotes, no denied names."""
    if "'" in graph or '"' in graph:
        raise RuntimeError("compiled filtergraph contains a quote")
    for denied in DENIED_FILTERS:
        if denied in graph:
            raise RuntimeError(f"compiled filtergraph contains denied filter {denied!r}")
    body = _LABEL.sub("", graph.replace("\\,", ""))
    for part in re.split(r"[;,]", body):
        name = part.split("=", 1)[0]
        if name and name not in ALLOWLIST:
            raise RuntimeError(f"compiled filtergraph contains non-allowlisted {name!r}")


class _Graph:
    def __init__(self) -> None:
        self.stmts: list[str] = []
        self.used: set[str] = set()
        self._n = 0

    def label(self, prefix: str) -> str:
        self._n += 1
        return f"[{prefix}{self._n}]"

    def render(self, node: FilterNode) -> str:
        self.used.add(node.name)
        return node.render()

    def text(self) -> str:
        graph = ";".join(self.stmts)
        _assert_safe_graph(graph)
        return graph


class _Chain:
    """A linear chain from ``src`` label(s); Branch nodes fork and re-merge it."""

    def __init__(self, graph: _Graph, src: str) -> None:
        self.g = graph
        self.src = src
        self.parts: list[str] = []

    def add(self, node: Node) -> None:
        if isinstance(node, Branch):
            self._branch(node)
        else:
            self.parts.append(self.g.render(node))

    def _branch(self, b: Branch) -> None:
        main, copy, done = self.g.label("bm"), self.g.label("bc"), self.g.label("bb")
        self.parts.append(self.g.render(FilterNode("split", {"outputs": 2}, "v")))
        self.g.stmts.append(self.src + ",".join(self.parts) + main + copy)
        body = [self.g.render(n) for n in b.nodes] or [self.g.render(FilterNode("null"))]
        self.g.stmts.append(copy + ",".join(body) + done)
        self.src, self.parts = main + done, [self.g.render(b.overlay())]

    def end(self, label: str, stream: str) -> str:
        if not self.parts:
            null = "null" if stream == "v" else "anull"
            self.parts = [self.g.render(FilterNode(null, {}, stream))]  # type: ignore[arg-type]
        parts = self.parts
        self.g.stmts.append(self.src + ",".join(parts) + label)
        return label


# ------------------------------------------------------------------ op building


@dataclass
class _Seg:
    index: int
    seg: E.Segment
    v_nodes: list[Node] = field(default_factory=list)
    a_nodes: list[FilterNode] = field(default_factory=list)
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    speed_v: float = 1.0
    speed_a: float = 1.0
    trim_start: float = 0.0
    trim_end: float = 0.0

    def duration(self, has_video: bool) -> float:
        speed = self.speed_v if has_video else self.speed_a
        return (self.seg.end - self.seg.start) / speed


def _check_segment_nodes(nodes: Any, where: str) -> list[Node]:
    if not isinstance(nodes, (list, tuple)):
        raise TypeError(f"{where}: build() must return a list of nodes")
    for n in nodes:
        if isinstance(n, Branch):
            continue
        if not isinstance(n, FilterNode):
            raise TypeError(f"{where}: build() returned a non-node {n!r}")
        if n.name not in SEGMENT_FILTERS:
            raise ValueError(f"{where}: filter {n.name!r} is not allowed in a segment")
        if n.name == "setpts" and n.params.get("offset"):
            raise ValueError(f"{where}: setpts offset is compiler-only")
    return list(nodes)


def _scaled(w: int, h: int, cur: tuple[int | None, int | None]) -> tuple[int | None, int | None]:
    cw, ch = cur
    if w > 0 and h > 0:
        return w, h
    if not cw or not ch:
        return None, None
    # At most one side is positive past the first return.
    if h > 0:  # width is auto (w <= 0): derive it from the height
        w2 = round(h * cw / ch)
        return (w2 + (w2 % 2) if w == -2 else w2), h
    if w > 0:  # height is auto (h <= 0): derive it from the width
        h2 = round(w * ch / cw)
        return w, (h2 + (h2 % 2) if h == -2 else h2)
    return cw, ch


def _build_segments(
    el: E.EditList, info: MediaInfo, *, passthrough: bool, flags: dict
) -> list[_Seg]:
    has_v, has_a = info.video is not None, info.audio is not None
    segs: list[_Seg] = []
    for i, seg in enumerate(el.segments):
        s = _Seg(i, seg)
        if info.video is not None:
            s.width, s.height, s.fps = info.video.width, info.video.height, info.video.fps
        s.trim_start = max(input_clock(info, seg.start) - EPSILON, 0.0)
        s.trim_end = max(input_clock(info, seg.end) - EPSILON, 0.0)
        base = 0.0 if passthrough else s.trim_start
        for j, op in enumerate(seg.ops):
            ctx = SegmentContext(
                index=i,
                op_index=j,
                start=seg.start,
                end=seg.end,
                source_start=info.to_source_seconds(seg.start),
                source_end=info.to_source_seconds(seg.end),
                info=info,
                width=s.width,
                height=s.height,
                fps=s.fps,
                sample_rate=info.audio.sample_rate if info.audio else None,
                has_video=has_v,
                has_audio=has_a,
                speed=s.speed_v if has_v else s.speed_a,
                clock_base=base,
                flags=flags,
            )
            where = f"segments[{i}].ops[{j}] ({op.op})"
            for n in _check_segment_nodes(REGISTRY[op.op].build(op, ctx), where):
                if n.stream == "v" and has_v:
                    s.v_nodes.append(n)
                    if isinstance(n, FilterNode):
                        _track_video(s, n)
                elif n.stream == "a" and has_a and isinstance(n, FilterNode):
                    s.a_nodes.append(n)
                    if n.name == "atempo":
                        s.speed_a *= n.params["tempo"]
        if has_v and has_a and abs(s.speed_v - s.speed_a) > 1e-6:
            raise ValueError(
                f"segments[{i}]: video speed {s.speed_v} != audio speed {s.speed_a} (A/V desync)"
            )
        segs.append(s)
    return segs


def _track_video(s: _Seg, n: FilterNode) -> None:
    p = n.params
    if n.name == "crop":
        s.width, s.height = p["w"], p["h"]
    elif n.name == "scale":
        s.width, s.height = _scaled(p["w"], p["h"], (s.width, s.height))
    elif n.name == "fps":
        s.fps = p["fps"]
    elif n.name == "setpts":
        s.speed_v *= p.get("speed", 1.0)


def _full_range(el: E.EditList, info: MediaInfo) -> bool:
    if len(el.segments) != 1 or el.transitions:
        return False
    seg = el.segments[0]
    # Whole-file tolerance (d12): starts at the first frame and ends within half a nominal
    # frame interval of editable.end (``info.end``; EPSILON when the fps is unknown).  Ends
    # past editable.end by up to one frame were already clamped to it by ``E.validate``.
    tol = (info.frame_interval or 0.0) / 2 + EPSILON
    return seg.start <= EPSILON and seg.end >= info.end - tol


def _passthrough_ok(segs: list[_Seg]) -> bool:
    s = segs[0]
    retimed = any(isinstance(n, FilterNode) and n.name in _TIMING_FILTERS for n in s.v_nodes)
    return not s.a_nodes and not retimed


# ------------------------------------------------------------------ joins


def _join_nodes(nodes: Any, ctx: JoinContext, where: str) -> tuple[FilterNode | None, ...]:
    if not isinstance(nodes, (list, tuple)):
        raise TypeError(f"{where}: build() must return a list of nodes")
    xf = [n for n in nodes if isinstance(n, FilterNode) and n.name == "xfade"]
    ac = [n for n in nodes if isinstance(n, FilterNode) and n.name == "acrossfade"]
    if len(xf) + len(ac) != len(nodes) or any(n.name not in JOIN_FILTERS for n in xf + ac):
        raise ValueError(f"{where}: a join may only return xfade and acrossfade nodes")
    if (ctx.has_video and len(xf) != 1) or (ctx.has_audio and len(ac) != 1):
        raise ValueError(f"{where}: need exactly one xfade (video) and one acrossfade (audio)")
    v = xf[0] if ctx.has_video else None
    a = ac[0] if ctx.has_audio else None
    if v is not None and abs(v.params["offset"] - ctx.offset) > 1e-6:
        raise ValueError(f"{where}: xfade offset must be ctx.offset ({ctx.offset})")
    if v is not None and a is not None and abs(v.params["duration"] - a.params["d"]) > 1e-6:
        raise ValueError(f"{where}: xfade duration and acrossfade d differ (A/V desync)")
    return v, a


# ------------------------------------------------------------------ result


@dataclass(frozen=True)
class CompiledEdit:
    """One ffmpeg invocation (``args`` exclude the binary) plus its dry-run facts."""

    args: list[str]
    graph: str | None
    output_plan: output.OutputPlan
    expected_duration: float
    mode: str  # 'filter' | 'passthrough' | 'remux' | 'fast'
    redacted: bool
    metadata: str  # 'kept' | 'dropped'
    segments: tuple[dict, ...]
    flags: dict
    snapped: dict | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "args": list(self.args),
            "graph": self.graph,
            "expected_duration": self.expected_duration,
            "segments": [dict(s) for s in self.segments],
            "snapped": self.snapped,
            "redacted": self.redacted,
            "metadata": self.metadata,
            "flags": dict(self.flags),
            "output": self.output_plan.to_dict(),
        }

    def job_spec(self) -> dict[str, Any]:
        """The daemon's ``ffmpeg`` job dict (see ``daemon.server``)."""
        return {
            "kind": "ffmpeg",
            "args": list(self.args),
            "output": self.output_plan.dst,
            "tmp_output": self.output_plan.tmp_path,
            "duration": self.expected_duration,
        }


def _drop_streams(
    info: MediaInfo, keep: tuple[str, ...], primary: set[int], *, strip_extra_av: bool
) -> list[int]:
    drop = []
    for s in info.streams:
        if s.index in primary:
            continue
        if s.type == "subtitle":
            category = "subtitles"
        elif s.type == "attachment" or s.attached_pic:
            category = "attachments"
        elif s.type in ("video", "audio"):
            if strip_extra_av:
                drop.append(s.index)
            continue
        else:
            category = "data"
        if category not in keep:
            drop.append(s.index)
    return drop


def _seg_facts(el: E.EditList, info: MediaInfo, durations: list[float]) -> tuple[dict, ...]:
    return tuple(
        {
            "index": i,
            "start": seg.start,
            "end": seg.end,
            "source_start": info.to_source_seconds(seg.start),
            "source_end": info.to_source_seconds(seg.end),
            "duration": durations[i],
        }
        for i, seg in enumerate(el.segments)
    )


def _head(src: str) -> tuple[list[str], list[str]]:
    return ["-hide_banner", "-nostdin", "-y"], ["-protocol_whitelist", "file", "-i", "file:" + src]


# ------------------------------------------------------------------ compile


def compile_editlist(el: E.EditList, info: MediaInfo, *, fast: bool = False) -> CompiledEdit:
    """Compile a (re-validated) edit list against its probe facts into one ffmpeg argv."""
    el = E.validate(el, info)  # may clamp an end within one frame past editable.end
    src, dst = os.path.abspath(el.input), os.path.abspath(el.output)
    if fast:
        return _compile_fast(el, info, src, dst)
    flags: dict = {}
    segs = _build_segments(el, info, passthrough=False, flags=flags)
    mode = "filter"
    if _full_range(el, info) and _passthrough_ok(segs):
        flags = {}
        segs = _build_segments(el, info, passthrough=True, flags=flags)
        mode = "passthrough" if _passthrough_ok(segs) else "filter"
        if mode == "filter":  # an op changed its mind with the new clock; stay safe
            flags = {}
            segs = _build_segments(el, info, passthrough=False, flags=flags)
    redacted = bool(flags.get("redacted"))
    has_v, has_a = info.video is not None, info.audio is not None
    if mode == "passthrough" and not segs[0].v_nodes and not redacted:
        mode = "remux"

    labels: dict[int, str] = {}
    graph_text: str | None = None
    durations = [s.duration(has_v) for s in segs]
    expected = sum(durations) - sum(t.duration for t in el.transitions)
    touched: set[int] = set()
    if mode == "filter":
        graph_text, vout, aout = _filter_graph(el, info, segs, flags)
        if has_v:
            labels[info.video.index] = vout  # type: ignore[union-attr]
        if has_a:
            labels[info.audio.index] = aout  # type: ignore[union-attr]
        touched = set(labels)
    elif mode == "passthrough" and has_v:
        g = _Graph()
        chain = _Chain(g, f"[0:{info.video.index}]")  # type: ignore[union-attr]
        for n in segs[0].v_nodes:
            chain.add(n)
        labels[info.video.index] = chain.end(_VOUT, "v")  # type: ignore[union-attr]
        touched = set(labels)
        graph_text = g.text()
        _require(g.used)

    primary = {s.index for s in (info.video, info.audio) if s is not None}
    plan = output.plan_output(
        src,
        dst,
        touched,
        info=info,
        drop_streams=_drop_streams(
            info, el.keep, primary, strip_extra_av=mode == "filter" or redacted
        ),
        overwrite=el.overwrite,
    )
    strip_meta = redacted and "metadata" not in el.keep
    meta = ["-map_metadata", "-1", "-map_chapters", "-1"] if strip_meta else []
    head, inp = _head(src)
    fg = ["-filter_complex", graph_text] if graph_text is not None else []
    args = [*head, *inp, *fg, *meta, *plan.ffmpeg_output_args(labels), *_timing(plan)]
    args.append(plan.tmp_path)
    return CompiledEdit(
        args=args,
        graph=graph_text,
        output_plan=plan,
        expected_duration=expected,
        mode=mode,
        redacted=redacted,
        metadata="dropped" if strip_meta else "kept",
        segments=_seg_facts(el, info, durations),
        flags=flags,
    )


def _timing(plan: output.OutputPlan) -> list[str]:
    """Keep the filtergraph's exact timestamps on every re-encoded video stream.

    By default ffmpeg gives a video encoder the time base ``1/frame_rate`` and,
    for mp4, forces CFR: that quantizes a VFR/offset stream to the frame grid
    (breaking A/V sync with copied audio) and drops or duplicates frames after a
    ``setpts`` speed change.  ``-enc_time_base filter`` + ``-fps_mode vfr``
    keep the timestamps the graph produced.
    """
    kept = [d for d in plan.streams if d.action != "drop"]
    args: list[str] = []
    for pos, d in enumerate(kept):
        if d.action == "encode" and d.type == "video":
            args += [f"-fps_mode:{pos}", "vfr", f"-enc_time_base:{pos}", "filter"]
    return args


def _require(names: set[str]) -> None:
    for name in sorted(names):
        _tools.require_filter(name)


def _filter_graph(
    el: E.EditList, info: MediaInfo, segs: list[_Seg], flags: dict
) -> tuple[str, str, str]:
    has_v, has_a = info.video is not None, info.audio is not None
    g = _Graph()
    joined = len(segs) > 1
    target = (segs[0].width, segs[0].height)
    fps = (info.video.fps if info.video else None) or _DEFAULT_FPS
    outs: list[tuple[str, str]] = []
    for s in segs:
        v_label = a_label = ""
        if has_v:
            c = _Chain(g, f"[0:{info.video.index}]")  # type: ignore[union-attr]
            c.add(FilterNode("trim", {"start": s.trim_start, "end": s.trim_end}, "v"))
            c.add(FilterNode("setpts", {"offset": s.trim_start}, "v"))
            for n in s.v_nodes:
                c.add(n)
            if joined and (s.width, s.height) != target and target[0] and target[1]:
                c.add(FilterNode("scale", {"w": target[0], "h": target[1]}, "v"))
                c.add(FilterNode("setsar", {"sar": 1}, "v"))
            if el.transitions:
                c.add(FilterNode("fps", {"fps": fps}, "v"))
            v_label = c.end(_VOUT if not joined else g.label("v"), "v")
        if has_a:
            c = _Chain(g, f"[0:{info.audio.index}]")  # type: ignore[union-attr]
            c.add(FilterNode("atrim", {"start": s.trim_start, "end": s.trim_end}, "a"))
            c.add(FilterNode("asetpts", {"offset": s.trim_start}, "a"))
            for n in s.a_nodes:
                c.add(n)
            a_label = c.end(_AOUT if not joined else g.label("a"), "a")
        outs.append((v_label, a_label))

    if joined and el.transitions:
        v_run, a_run = outs[0]
        left = segs[0].duration(has_v)
        for k, t in enumerate(el.transitions):
            right = segs[k + 1].duration(has_v)
            ctx = JoinContext(
                index=k,
                left_duration=left,
                right_duration=right,
                offset=left - t.duration,
                info=info,
                has_video=has_v,
                has_audio=has_a,
                width=target[0],
                height=target[1],
                fps=fps if has_v else None,
                sample_rate=info.audio.sample_rate if info.audio else None,
                flags=flags,
            )
            where = f"transitions[{k}] ({t.type})"
            xf, ac = _join_nodes(TRANSITION_REGISTRY[t.type].build(t, ctx), ctx, where)
            last = k == len(el.transitions) - 1
            v_next, a_next = outs[k + 1]
            if xf is not None:
                lbl = _VOUT if last else g.label("vx")
                g.stmts.append(v_run + v_next + g.render(xf) + lbl)
                v_run = lbl
            if ac is not None:
                lbl = _AOUT if last else g.label("ax")
                g.stmts.append(a_run + a_next + g.render(ac) + lbl)
                a_run = lbl
            overlap = xf.params["duration"] if xf is not None else ac.params["d"]  # type: ignore
            left = left + right - overlap
    elif joined:
        ins = "".join(v + a for v, a in outs)
        concat = FilterNode("concat", {"n": len(segs), "v": int(has_v), "a": int(has_a)}, "v")
        out_labels = (_VOUT if has_v else "") + (_AOUT if has_a else "")
        g.stmts.append(ins + g.render(concat) + out_labels)
    text = g.text()
    _require(g.used)
    return text, _VOUT, _AOUT


# ------------------------------------------------------------------ fast mode


def _keyframes(info: MediaInfo) -> list[float]:
    v = info.video
    cp = _tools.run(
        "ffprobe",
        [
            "-v",
            "error",
            "-select_streams",
            str(v.index),  # type: ignore[union-attr]
            "-skip_frame",
            "nokey",
            "-show_entries",
            "frame=pts_time",
            "-of",
            "csv=p=0",
            info.path,
        ],
        timeout=_PROBE_TIMEOUT,
    )
    times = []
    for line in cp.stdout.splitlines():
        cell = line.split(",")[0].strip()
        try:
            times.append(info.from_source_seconds(float(cell)))
        except ValueError:
            continue
    return sorted(times)


def _fast_refused(why: str) -> MediaInputError:
    return MediaInputError(
        INPUT_FAST_UNSUPPORTED,
        f"fast (stream-copy) cut is not possible: {why}",
        "drop --fast for a frame-accurate re-encoded edit",
    )


def _compile_fast(el: E.EditList, info: MediaInfo, src: str, dst: str) -> CompiledEdit:
    if len(el.segments) != 1 or el.transitions:
        raise _fast_refused("it needs exactly one segment and no transitions")
    seg = el.segments[0]
    if seg.ops:
        raise _fast_refused("ops need re-encoding")
    if info.video is None:
        raise _fast_refused("the input has no video stream to find keyframes in")
    keys = _keyframes(info)
    before = [k for k in keys if k <= seg.start + _KEYFRAME_TOL]
    start = round(before[-1], 6) if before else 0.0
    after = [k for k in keys if k >= seg.end - _KEYFRAME_TOL]
    at_end = not after or seg.end >= info.end - _KEYFRAME_TOL
    end = round(info.end if at_end else after[0], 6)
    exact = abs(start - seg.start) <= _KEYFRAME_TOL and abs(end - seg.end) <= _KEYFRAME_TOL
    snapped = {"start": start, "end": end, "exact": exact}

    primary = {s.index for s in (info.video, info.audio) if s is not None}
    plan = output.plan_output(
        src,
        dst,
        set(),
        info=info,
        drop_streams=_drop_streams(info, el.keep, primary, strip_extra_av=False),
        overwrite=el.overwrite,
    )
    head, inp = _head(src)
    # +EPSILON so float rounding never seeks back to the previous keyframe
    seek = ["-ss", format_number(input_clock(info, start) + EPSILON)]
    if not at_end:
        seek += ["-to", format_number(input_clock(info, end))]
    args = [*head, *seek, *inp, *plan.ffmpeg_output_args(), plan.tmp_path]
    return CompiledEdit(
        args=args,
        graph=None,
        output_plan=plan,
        expected_duration=end - start,
        mode="fast",
        redacted=False,
        metadata="kept",
        segments=_seg_facts(el, info, [end - start]),
        flags={},
        snapped=snapped,
    )


__all__ = [
    "INPUT_FAST_UNSUPPORTED",
    "REGISTRY",
    "TRANSITION_REGISTRY",
    "CompiledEdit",
    "compile_editlist",
    "input_clock",
]
