"""Tests for media_cli.media.compile + the ops interface (task t10).

Security core: the filtergraph is built only from the typed allowlist in
``media_cli.media.ops``; no user string ever reaches it.  Cut accuracy is
checked by actually running ffmpeg on the lavfi fixtures.
"""

from __future__ import annotations

import inspect
import json
import math
import os
import shutil
from types import SimpleNamespace

import pytest

from media_cli.media import _tools
from media_cli.media import compile as C
from media_cli.media import editlist as E
from media_cli.media import ops
from media_cli.media import output as out
from media_cli.media import probe as P
from media_cli.media.errors import MediaInputError
from media_cli.media.ops import (
    ALLOWLIST,
    DENIED_FILTERS,
    Branch,
    FilterNode,
    JoinContext,
    SegmentContext,
    compose,
    redact,
    visual,
)

FORBIDDEN = ("movie", "amovie", "zmq", "azmq", "sendcmd")
PAYLOADS = (",", ";", "[", "=", "'", "movie=", "a,movie=/etc/passwd[x]", "black'", "fade;zmq")


def _doc(inp, outp, segments=None, **extra) -> dict:
    d = {"input": str(inp), "output": str(outp), "segments": segments or [{"start": 2, "end": 5}]}
    d.update(extra)
    return d


def _compile(doc, info=None, **kw) -> C.CompiledEdit:
    el = E.parse(doc)
    return C.compile_editlist(el, info or P.probe(el.input), **kw)


def _run(compiled: C.CompiledEdit) -> str:
    _tools.run("ffmpeg", list(compiled.args), timeout=120)
    return out.commit(compiled.output_plan)


def _video_stream(path):
    info = P.probe(path)
    return info, info.video


def _fake(build):
    return SimpleNamespace(build=build)


# ------------------------------------------------------------------ allowlist / rendering


def test_denied_filters_can_never_be_built():
    assert set(FORBIDDEN) <= DENIED_FILTERS
    assert not DENIED_FILTERS & set(ALLOWLIST)
    for name in FORBIDDEN:
        with pytest.raises(ValueError):
            FilterNode(name, {}, "v")


def test_allowlist_covers_what_ops_tasks_need():
    need = {
        "trim",
        "atrim",
        "setpts",
        "asetpts",
        "concat",
        "crop",
        "scale",
        "fps",
        "format",
        "setsar",
        "fade",
        "afade",
        "atempo",
        "drawbox",
        "boxblur",
        "split",
        "overlay",
        "xfade",
        "acrossfade",
        "aresample",
        "anull",
        "null",
    }
    assert need <= set(ALLOWLIST)


def test_enum_values_are_plain_tokens():
    for spec in ALLOWLIST.values():
        for alts in spec.values():
            for p in alts if isinstance(alts, tuple) else (alts,):
                if isinstance(p.kind, frozenset):
                    assert all(v.isascii() and v.replace("_", "").isalnum() for v in p.kind)


@pytest.mark.parametrize(
    "name,params,stream",
    [
        ("crop", {"w": "10", "h": 10}, "v"),  # string for an int
        ("crop", {"w": True, "h": 10}, "v"),  # bool is not an int
        ("crop", {"w": 10.5, "h": 10}, "v"),  # float for an int
        ("crop", {"w": 10, "h": 10, "bogus": 1}, "v"),  # unknown param
        ("crop", {"w": 0, "h": 10}, "v"),  # out of range
        ("crop", {"h": 10}, "v"),  # missing required
        ("crop", {"w": 10, "h": 10}, "a"),  # wrong stream
        ("trim", {"start": float("nan")}, "v"),
        ("trim", {"start": float("inf")}, "v"),
        ("fade", {"t": "in,movie=x"}, "v"),  # enum outside its set
        ("fade", {"t": "IN"}, "v"),
        ("drawbox", {"x": 0, "y": 0, "w": 1, "h": 1, "color": "red"}, "v"),
        ("drawbox", {"x": 0, "y": 0, "w": 1, "h": 1, "t": "fill'"}, "v"),
        ("drawbox", {"x": 0, "y": 0, "w": 1, "h": 1, "enable_start": 1.0}, "v"),  # half pair
        ("drawbox", {"x": 0, "y": 0, "w": 1, "h": 1, "enable_start": 2, "enable_end": 1}, "v"),
        ("crop", {"w": 1, "h": 1, "enable_start": 0, "enable_end": 1}, "v"),  # no timeline
        ("xfade", {"transition": "circleopen", "duration": 1, "offset": 0}, "v"),
    ],
)
def test_filter_node_rejects_untyped_params(name, params, stream):
    with pytest.raises((ValueError, TypeError)):
        FilterNode(name, params, stream)


def test_render_fixed_point_numbers_and_enable():
    assert FilterNode("trim", {"start": 1e-7, "end": 1e20}, "v").render() == (
        "trim=start=0:end=100000000000000000000"
    )
    assert FilterNode("trim", {"start": 2.0230000001, "end": 5}, "v").render() == (
        "trim=start=2.023:end=5"
    )
    assert FilterNode("atempo", {"tempo": 1.5}, "a").render() == "atempo=tempo=1.5"
    node = FilterNode(
        "drawbox",
        {
            "x": 1,
            "y": 2,
            "w": 3,
            "h": 4,
            "color": "black",
            "t": "fill",
            "enable_start": 1,
            "enable_end": 2.5,
        },
        "v",
    )
    assert node.render() == (
        "drawbox=x=1:y=2:w=3:h=4:color=black:t=fill:enable=between(t\\,1\\,2.5)"
    )
    assert (
        FilterNode("drawbox", {"x": 0, "y": 0, "w": 1, "h": 1, "t": 5}, "v")
        .render()
        .endswith(":t=5")
    )
    assert FilterNode("setpts", {}, "v").render() == "setpts=PTS"
    assert FilterNode("setpts", {"speed": 2}, "v").render() == "setpts=PTS/2"
    assert FilterNode("setpts", {"offset": 2.023}, "v").render() == "setpts=PTS-2.023/TB"
    assert FilterNode("asetpts", {"offset": 1}, "a").render() == "asetpts=PTS-1/TB"
    assert FilterNode("null", {}, "v").render() == "null"


def test_filter_node_is_frozen():
    node = FilterNode("crop", {"w": 10, "h": 10}, "v")
    with pytest.raises(Exception):
        node.name = "movie"  # type: ignore[misc]
    with pytest.raises(TypeError):
        node.params["w"] = "x"  # type: ignore[index]
    assert hash(node) == hash(FilterNode("crop", {"w": 10, "h": 10}, "v"))


def test_every_allowlisted_filter_renders_to_valid_ffmpeg(media_mp4):
    """One real ffmpeg run over a graph using every allowlisted filter."""
    v = [
        FilterNode("trim", {"start": 0, "end": 2}, "v"),
        FilterNode("setpts", {"offset": 0.0}, "v"),
        FilterNode("setpts", {"speed": 1.0}, "v"),
        FilterNode("crop", {"w": 300, "h": 200, "x": 2, "y": 4, "exact": 1}, "v"),
        FilterNode("scale", {"w": 320, "h": 240, "flags": "bicubic"}, "v"),
        FilterNode("fps", {"fps": 25}, "v"),
        FilterNode("format", {"pix_fmts": "yuv420p"}, "v"),
        FilterNode("setsar", {"sar": 1}, "v"),
        FilterNode("fade", {"t": "in", "st": 0, "d": 0.5, "alpha": 0, "c": "black"}, "v"),
        FilterNode(
            "drawbox",
            {
                "x": 1,
                "y": 1,
                "w": 9,
                "h": 9,
                "color": "black",
                "t": "fill",
                "enable_start": 0,
                "enable_end": 1,
            },
            "v",
        ),
        FilterNode(
            "boxblur",
            {
                "luma_radius": 2,
                "luma_power": 1,
                "chroma_radius": 1,
                "chroma_power": 1,
                "enable_start": 0,
                "enable_end": 1,
            },
            "v",
        ),
        FilterNode("null", {}, "v"),
    ]
    a = [
        FilterNode("atrim", {"start": 0, "end": 2}, "a"),
        FilterNode("asetpts", {"offset": 0}, "a"),
        FilterNode("atempo", {"tempo": 1.0}, "a"),
        FilterNode("afade", {"t": "out", "st": 1, "d": 0.5, "curve": "tri"}, "a"),
        FilterNode("aresample", {"osr": 44100, "async": 1}, "a"),
        FilterNode("anull", {}, "a"),
    ]
    split = FilterNode("split", {"outputs": 2}, "v").render()
    overlay = FilterNode(
        "overlay",
        {"x": 0, "y": 0, "eof_action": "pass", "shortest": 0, "enable_start": 0, "enable_end": 1},
        "v",
    ).render()
    xf = FilterNode("xfade", {"transition": "wipeleft", "duration": 0.5, "offset": 1}, "v").render()
    ac = FilterNode("acrossfade", {"d": 0.5, "c1": "tri", "c2": "qsin"}, "a").render()
    cc = FilterNode("concat", {"n": 1, "v": 1, "a": 1}, "v").render()
    graph = (
        f"[0:0]{','.join(n.render() for n in v)},{split}[m][c];[m][c]{overlay},split[x][y];"
        f"[0:1]{','.join(n.render() for n in a)},asplit[p][q];"
        f"[x][y]{xf}[vx];[p][q]{ac}[ax];[vx][ax]{cc}[vo][ao]"
    )
    _tools.run(
        "ffmpeg",
        [
            "-hide_banner",
            "-nostdin",
            "-i",
            str(media_mp4),
            "-filter_complex",
            graph,
            "-map",
            "[vo]",
            "-map",
            "[ao]",
            "-f",
            "null",
            "-",
        ],
        timeout=120,
    )


# ------------------------------------------------------------------ registry / interface


def test_registry_is_fixed_and_complete():
    assert C.REGISTRY == {
        "crop": visual,
        "speed": visual,
        "fade": visual,
        "box": redact,
        "blur": redact,
    }
    assert set(C.REGISTRY) == set(E.OP_NAMES)
    assert C.TRANSITION_REGISTRY == {"xfade": compose, "acrossfade": compose}
    assert set(C.TRANSITION_REGISTRY) == set(E.TRANSITION_TYPES)


@pytest.mark.parametrize("mod", [visual, redact, compose])
def test_op_modules_keep_the_build_signature(mod):
    assert list(inspect.signature(mod.build).parameters) == ["op", "ctx"]


def test_stub_error_helper_kind():
    err = ops.not_implemented(E.Crop(0, 0, 1, 1))
    assert isinstance(err, MediaInputError) and err.kind == "input.op_not_implemented"
    assert "crop" in err.message


def test_dispatch_propagates_op_not_implemented(media_mp4, tmp_path, monkeypatch):
    def stub(op, ctx):
        raise ops.not_implemented(op)

    monkeypatch.setitem(C.REGISTRY, "crop", _fake(stub))
    doc = _doc(
        media_mp4,
        tmp_path / "o.mp4",
        [{"start": 2, "end": 5, "ops": [{"op": "crop", "x": 0, "y": 0, "w": 10, "h": 10}]}],
    )
    with pytest.raises(MediaInputError) as ei:
        _compile(doc)
    assert ei.value.kind == "input.op_not_implemented"


def test_every_op_goes_through_the_registry(media_mp4, tmp_path, monkeypatch):
    calls = []

    def rec(op, ctx):
        calls.append((op.op, ctx.index, ctx.op_index))
        return []

    for name in E.OP_NAMES:
        monkeypatch.setitem(C.REGISTRY, name, _fake(rec))
    region = {"x": 0, "y": 0, "w": 10, "h": 10}
    doc = _doc(
        media_mp4,
        tmp_path / "o.mp4",
        [
            {
                "start": 1,
                "end": 3,
                "ops": [
                    {"op": "crop", "x": 0, "y": 0, "w": 100, "h": 100},
                    {"op": "speed", "factor": 2},
                    {"op": "fade", "direction": "in", "duration": 0.5},
                ],
            },
            {
                "start": 4,
                "end": 6,
                "ops": [
                    {"op": "box", "regions": [region], "fill": "black"},
                    {"op": "blur", "regions": [region], "strength": 3},
                ],
            },
        ],
    )
    _compile(doc)
    assert calls == [
        ("crop", 0, 0),
        ("speed", 0, 1),
        ("fade", 0, 2),
        ("box", 1, 0),
        ("blur", 1, 1),
    ]


def test_ops_returning_forbidden_nodes_are_refused(media_mp4, tmp_path, monkeypatch):
    for bad in (
        [FilterNode("trim", {"start": 0}, "v")],  # compiler-only
        [FilterNode("xfade", {"transition": "fade", "duration": 1, "offset": 0}, "v")],
        [FilterNode("setpts", {"offset": 1}, "v")],  # ops may not re-base time
        ["drawbox=x=0"],  # a string is never a node
    ):
        monkeypatch.setitem(C.REGISTRY, "speed", _fake(lambda op, ctx, bad=bad: bad))
        doc = _doc(
            media_mp4,
            tmp_path / "o.mp4",
            [{"start": 2, "end": 5, "ops": [{"op": "speed", "factor": 2}]}],
        )
        with pytest.raises((ValueError, TypeError)):
            _compile(doc)


# ------------------------------------------------------------------ argv / fuzz


def test_argv_shape_and_job_spec(media_mp4, tmp_path):
    c = _compile(_doc(media_mp4, tmp_path / "o.mp4"))
    args = list(c.args)
    assert args[0] == "-hide_banner" and "ffmpeg" not in args[0]
    i = args.index("-i")
    assert args[i + 1] == "file:" + os.path.abspath(media_mp4)
    assert args[args.index("-filter_complex") + 1] == c.graph
    assert args[-1] == c.output_plan.tmp_path
    assert "-copyts" not in args
    assert c.mode == "filter"
    assert "[0:0]trim=" in c.graph and "[0:1]atrim=" in c.graph
    job = c.job_spec()
    assert job == {
        "kind": "ffmpeg",
        "args": args,
        "output": c.output_plan.dst,
        "tmp_output": c.output_plan.tmp_path,
        "duration": pytest.approx(3.0),
    }
    assert os.path.isabs(job["output"])
    d = c.to_dict()
    json.dumps(d)
    assert d["args"] == args and d["graph"] == c.graph and d["mode"] == "filter"
    assert d["expected_duration"] == pytest.approx(3.0)
    assert d["output"]["streams"][0]["action"] == "encode"
    assert d["segments"][0]["start"] == 2 and d["segments"][0]["end"] == 5


def _string_fields():
    region = {"x": 0, "y": 0, "w": 10, "h": 10}
    base = {
        "input": "IN",
        "output": "OUT",
        "keep": ["metadata"],
        "segments": [
            {
                "start": 1,
                "end": 3,
                "ops": [
                    {"op": "fade", "direction": "in", "duration": 0.5},
                    {"op": "box", "regions": [region], "fill": "black"},
                ],
            },
            {"start": 4, "end": 6},
        ],
        "transitions": [{"type": "xfade", "style": "fade", "duration": 0.5}],
    }
    setters = {
        "input": lambda d, v: d.__setitem__("input", v),
        "output": lambda d, v: d.__setitem__("output", v),
        "keep[0]": lambda d, v: d["keep"].__setitem__(0, v),
        "op": lambda d, v: d["segments"][0]["ops"][0].__setitem__("op", v),
        "direction": lambda d, v: d["segments"][0]["ops"][0].__setitem__("direction", v),
        "fill": lambda d, v: d["segments"][0]["ops"][1].__setitem__("fill", v),
        "type": lambda d, v: d["transitions"][0].__setitem__("type", v),
        "style": lambda d, v: d["transitions"][0].__setitem__("style", v),
        "number-as-string": lambda d, v: d["segments"][0].__setitem__("start", "1" + v),
        "region-as-string": lambda d, v: d["segments"][0]["ops"][1]["regions"][0].__setitem__(
            "x", v
        ),
    }
    return base, setters


@pytest.mark.parametrize("payload", PAYLOADS)
@pytest.mark.parametrize("field", list(_string_fields()[1]))
def test_fuzz_injection_into_every_string_field(field, payload, media_mp4, tmp_path, monkeypatch):
    base, setters = _string_fields()
    doc = json.loads(json.dumps(base))
    src = tmp_path / "src.mp4"
    shutil.copy(media_mp4, src)
    doc["input"], doc["output"] = str(src), str(tmp_path / "o.mp4")
    value = payload
    name = payload.replace("/", "_")  # a file name cannot hold '/'
    if field == "input":
        value = str(tmp_path / f"in{name}x.mp4")
        shutil.copy(media_mp4, value)
    elif field == "output":
        value = str(tmp_path / f"out{name}x.mp4")
    setters[field](doc, value)
    try:
        el = E.parse(doc)
        info = P.probe(el.input)
        E.validate(el, info)
    except MediaInputError:
        assert field not in ("input", "output"), "paths must be accepted, not mangled"
        return
    assert field in ("input", "output"), f"{field}={payload!r} was accepted"
    # Accepted paths: compile with typed fake ops and inspect the graph.
    monkeypatch.setitem(C.REGISTRY, "fade", _fake(lambda op, ctx: []))
    monkeypatch.setitem(C.REGISTRY, "box", _fake(lambda op, ctx: []))
    monkeypatch.setitem(
        C.TRANSITION_REGISTRY,
        "xfade",
        _fake(
            lambda t, ctx: [
                FilterNode(
                    "xfade",
                    {"transition": t.style, "duration": t.duration, "offset": ctx.offset},
                    "v",
                ),
                FilterNode("acrossfade", {"d": t.duration}, "a"),
            ]
        ),
    )
    c = C.compile_editlist(el, info)
    graph = c.graph
    assert value not in graph and os.path.basename(value) not in graph
    if payload not in ",;[=":  # single syntax chars are the graph's own syntax
        assert name not in graph
    assert "'" not in graph
    for name in FORBIDDEN:
        assert name not in graph
    # the path shows up only as whole argv elements (input, tmp output), never in the graph
    holders = [a for a in c.args if os.path.basename(value) in a]
    assert holders and all(os.path.isabs(a.removeprefix("file:")) for a in holders)


def test_graph_final_guard_rejects_denied_names(monkeypatch):
    with pytest.raises(RuntimeError):
        C._assert_safe_graph("[0:0]null,movie=x[v]")
    with pytest.raises(RuntimeError):
        C._assert_safe_graph("[0:0]drawbox=x=1:enable='1'[v]")
    C._assert_safe_graph("[0:0]null[v0]")


# ------------------------------------------------------------------ cut accuracy (real ffmpeg)


def _frames(path) -> list[float]:
    P.clear_cache()
    return P.frame_times(path)


def _gray_frames(path, tmp_path, name) -> list[bytes]:
    raw = tmp_path / f"{name}.gray"
    _tools.run(
        "ffmpeg",
        [
            "-v",
            "error",
            "-nostdin",
            "-y",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-fps_mode",
            "passthrough",
            "-vf",
            "scale=64:48",
            "-pix_fmt",
            "gray",
            "-f",
            "rawvideo",
            str(raw),
        ],
        timeout=120,
    )
    data = raw.read_bytes()
    n = 64 * 48
    return [data[i : i + n] for i in range(0, len(data), n)]


def _closest(frame: bytes, candidates: list[bytes]) -> int:
    return min(
        range(len(candidates)), key=lambda i: sum(abs(a - b) for a, b in zip(frame, candidates[i]))
    )


def test_cut_mp4_is_frame_accurate(media_mp4, tmp_path):
    c = _compile(_doc(media_mp4, tmp_path / "cut.mp4"))
    dst = _run(c)
    info, v = _video_stream(dst)
    fps = P.probe(media_mp4).video.fps
    assert v.duration == pytest.approx(3.0, abs=1 / fps)
    assert len(_frames(dst)) == 75
    assert info.audio.duration == pytest.approx(3.0, abs=0.05)
    src = _gray_frames(media_mp4, tmp_path, "src")
    got = _gray_frames(dst, tmp_path, "dst")
    assert _closest(got[0], src) == P.to_frame_index(media_mp4, 2.0) == 50
    assert _closest(got[-1], src) == 124


def test_cut_vfr_offset_is_frame_accurate(media_vfr_offset, tmp_path):
    info = P.probe(media_vfr_offset)
    assert info.start_time != info.origin  # audio starts earlier than video
    c = _compile(_doc(media_vfr_offset, tmp_path / "cut.mkv"))
    # the one clock conversion: normalized -> raw -> ffmpeg input clock
    assert C.input_clock(info, 2.0) == pytest.approx(info.origin + 2.0 - info.start_time)
    dst = _run(c)
    src_t = _frames(media_vfr_offset)
    expected = [t for t in src_t if 2.0 - P.EPSILON <= t < 5.0 - P.EPSILON]
    got_t = _frames(dst)
    assert len(got_t) == len(expected)
    gaps = [b - a for a, b in zip(src_t, src_t[1:])]
    measured = got_t[-1] - got_t[0] + (expected[-1] - expected[-2])
    assert measured == pytest.approx(3.0, abs=max(gaps))
    src = _gray_frames(media_vfr_offset, tmp_path, "src")
    got = _gray_frames(dst, tmp_path, "dst")
    first = P.to_frame_index(info, 2.0)
    assert _closest(got[0], src) == first
    assert _closest(got[-1], src) == first + len(expected) - 1
    fmt = P.probe(dst)
    assert fmt.duration == pytest.approx(3.0, abs=max(gaps))


# ------------------------------------------------------------------ hand-built ops end to end


def test_speed_nodes_retime_and_track_duration(media_mp4, tmp_path, monkeypatch):
    seen = []

    def speed(op, ctx):
        seen.append(ctx)
        return [
            FilterNode("setpts", {"speed": op.factor}, "v"),
            FilterNode("atempo", {"tempo": op.factor}, "a"),
        ]

    def fade(op, ctx):
        seen.append(ctx)
        return []

    monkeypatch.setitem(C.REGISTRY, "speed", _fake(speed))
    monkeypatch.setitem(C.REGISTRY, "fade", _fake(fade))
    doc = _doc(
        media_mp4,
        tmp_path / "fast.mp4",
        [
            {
                "start": 2,
                "end": 5,
                "ops": [
                    {"op": "speed", "factor": 2},
                    {"op": "fade", "direction": "out", "duration": 1},
                ],
            }
        ],
    )
    c = _compile(doc)
    assert c.expected_duration == pytest.approx(1.5)
    first, second = seen
    assert (first.speed, first.duration) == (1.0, pytest.approx(3.0))
    assert (second.speed, second.duration) == (2.0, pytest.approx(1.5))
    assert second.local_time(5.0) == pytest.approx(1.5, abs=1e-3)
    assert second.local_time(2.0) == pytest.approx(0.0, abs=1e-3)
    assert (second.width, second.height, second.has_audio, second.has_video) == (
        320,
        240,
        True,
        True,
    )
    assert second.source_start == pytest.approx(2.0) and second.source_end == pytest.approx(5.0)
    dst = _run(c)
    assert P.probe(dst).video.duration == pytest.approx(1.5, abs=0.05)


def test_crop_updates_size_and_segments_are_normalized(media_mp4, tmp_path, monkeypatch):
    sizes = []

    def crop(op, ctx):
        return [FilterNode("crop", {"x": op.x, "y": op.y, "w": op.w, "h": op.h}, "v")]

    def fade(op, ctx):
        sizes.append((ctx.index, ctx.width, ctx.height))
        return []

    monkeypatch.setitem(C.REGISTRY, "crop", _fake(crop))
    monkeypatch.setitem(C.REGISTRY, "fade", _fake(fade))
    doc = _doc(
        media_mp4,
        tmp_path / "c.mp4",
        [
            {
                "start": 1,
                "end": 2,
                "ops": [
                    {"op": "crop", "x": 0, "y": 0, "w": 160, "h": 120},
                    {"op": "fade", "direction": "in", "duration": 0.5},
                ],
            },
            {"start": 3, "end": 4, "ops": [{"op": "fade", "direction": "in", "duration": 0.5}]},
        ],
    )
    c = _compile(doc)
    assert sizes == [(0, 160, 120), (1, 320, 240)]
    assert "concat=n=2:v=1:a=1" in c.graph
    dst = _run(c)
    v = P.probe(dst).video
    assert (v.width, v.height) == (160, 120)
    assert v.duration == pytest.approx(2.0, abs=0.05)


def _mean_region(path, tmp_path, t, box) -> float:
    x, y, w, h = box
    raw = tmp_path / "px.gray"
    _tools.run(
        "ffmpeg",
        [
            "-v",
            "error",
            "-nostdin",
            "-y",
            "-ss",
            str(t),
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-vf",
            f"crop={w}:{h}:{x}:{y}",
            "-pix_fmt",
            "gray",
            "-f",
            "rawvideo",
            str(raw),
        ],
        timeout=60,
    )
    data = raw.read_bytes()
    return sum(data) / len(data)


def test_branch_wires_split_copy_overlay_with_local_enable(media_mp4, tmp_path, monkeypatch):
    box = (100, 60, 40, 40)

    def blur(op, ctx):
        r = op.regions[0]
        return [
            Branch(
                nodes=(
                    FilterNode("crop", {"x": r.x, "y": r.y, "w": r.w, "h": r.h}, "v"),
                    FilterNode(
                        "drawbox",
                        {"x": 0, "y": 0, "w": r.w, "h": r.h, "color": "black", "t": "fill"},
                        "v",
                    ),
                ),
                x=r.x,
                y=r.y,
                enable_start=ctx.local_time(r.start),
                enable_end=ctx.local_time(r.end),
            )
        ]

    monkeypatch.setitem(C.REGISTRY, "blur", _fake(blur))
    region = {"x": box[0], "y": box[1], "w": box[2], "h": box[3], "start": 3.0, "end": 4.0}
    doc = _doc(
        media_mp4,
        tmp_path / "b.mp4",
        [{"start": 2, "end": 5, "ops": [{"op": "blur", "regions": [region], "strength": 3}]}],
    )
    c = _compile(doc)
    assert "split=outputs=2" in c.graph and "overlay=x=100:y=60:enable=between(t\\,1" in c.graph
    dst = _run(c)
    assert _mean_region(dst, tmp_path, 1.5, box) < 20  # inside the window: blacked
    assert _mean_region(dst, tmp_path, 0.5, box) > 40  # before it: untouched
    assert _mean_region(dst, tmp_path, 2.5, box) > 40  # after it: untouched


def test_transitions_wired_through_compose(media_mp4, tmp_path, monkeypatch):
    joins = []

    def xfade(t, ctx):
        joins.append(ctx)
        return [
            FilterNode(
                "xfade", {"transition": t.style, "duration": t.duration, "offset": ctx.offset}, "v"
            ),
            FilterNode("acrossfade", {"d": t.duration}, "a"),
        ]

    monkeypatch.setitem(C.TRANSITION_REGISTRY, "xfade", _fake(xfade))
    doc = _doc(
        media_mp4,
        tmp_path / "x.mp4",
        [{"start": 1, "end": 3}, {"start": 5, "end": 7}, {"start": 8, "end": 9.5}],
        transitions=[
            {"type": "xfade", "style": "wipeleft", "duration": 0.5},
            {"type": "xfade", "duration": 0.25},
        ],
    )
    c = _compile(doc)
    assert [(j.index, j.left_duration, j.right_duration, j.offset) for j in joins] == [
        (0, pytest.approx(2.0), pytest.approx(2.0), pytest.approx(1.5)),
        (1, pytest.approx(3.5), pytest.approx(1.5), pytest.approx(3.25)),
    ]
    assert c.expected_duration == pytest.approx(4.75)
    assert "xfade=transition=wipeleft" in c.graph and "concat" not in c.graph
    dst = _run(c)
    info = P.probe(dst)
    assert info.video.duration == pytest.approx(4.75, abs=0.1)
    assert info.audio.duration == pytest.approx(4.75, abs=0.1)


def test_transition_nodes_must_match(media_mp4, tmp_path, monkeypatch):
    monkeypatch.setitem(
        C.TRANSITION_REGISTRY,
        "xfade",
        _fake(lambda t, ctx: [FilterNode("acrossfade", {"d": t.duration}, "a")]),
    )
    doc = _doc(
        media_mp4,
        tmp_path / "x.mp4",
        [{"start": 1, "end": 3}, {"start": 5, "end": 7}],
        transitions=[{"type": "xfade", "duration": 0.5}],
    )
    with pytest.raises(ValueError):
        _compile(doc)


# ------------------------------------------------------------------ streams / redaction plumbing


def _by_type(plan):
    return {(d.type, d.index): d for d in plan.streams}


def test_full_range_visual_op_copies_untouched_audio(media_vfr_offset, tmp_path, monkeypatch):
    info = P.probe(media_vfr_offset)
    monkeypatch.setitem(
        C.REGISTRY,
        "box",
        _fake(
            lambda op, ctx: [
                FilterNode(
                    "drawbox",
                    {
                        "x": 0,
                        "y": 0,
                        "w": 10,
                        "h": 10,
                        "color": "black",
                        "t": "fill",
                        "enable_start": ctx.local_time(1.0),
                        "enable_end": ctx.local_time(2.0),
                    },
                    "v",
                )
            ]
        ),
    )
    region = {"x": 0, "y": 0, "w": 10, "h": 10, "start": 1.0, "end": 2.0}
    doc = _doc(
        media_vfr_offset,
        tmp_path / "p.mkv",
        [
            {
                "start": 0,
                "end": info.end,
                "ops": [{"op": "box", "regions": [region], "fill": "black"}],
            }
        ],
    )
    c = _compile(doc)
    assert c.mode == "passthrough"
    assert "trim" not in c.graph
    acts = {d.type: d.action for d in c.output_plan.streams}
    assert acts == {"video": "encode", "audio": "copy"}
    # passthrough keeps ffmpeg's input clock, so local time == input clock
    assert f"between(t\\,{C.input_clock(info, 1.0):.3f}".rstrip("0") in c.graph
    dst = _run(c)
    o = P.probe(dst)
    # A/V offset preserved (video starts ~23ms after audio, as in the source)
    src_off = info.video.start_time - info.audio.start_time
    assert (o.video.start_time - o.audio.start_time) == pytest.approx(src_off, abs=0.005)


def test_full_range_without_ops_is_a_remux(media_mp4, tmp_path):
    info = P.probe(media_mp4)
    c = _compile(_doc(media_mp4, tmp_path / "r.mp4", [{"start": 0, "end": info.end}]))
    assert c.mode == "remux" and c.graph is None and "-filter_complex" not in c.args
    assert all(d.action == "copy" for d in c.output_plan.streams)


def test_extras_dropped_by_default_and_metadata_kept_when_not_redacted(media_with_extras, tmp_path):
    c = _compile(_doc(media_with_extras, tmp_path / "e.mp4"))
    acts = {d.index: d.action for d in c.output_plan.streams}
    info = P.probe(media_with_extras)
    for s in info.streams:
        if s.type == "subtitle" or s.attached_pic:
            assert acts[s.index] == "drop"
    assert "-map_metadata" not in c.args
    assert c.to_dict()["metadata"] == "kept" and c.to_dict()["redacted"] is False


def _redacting_box(op, ctx):
    ctx.flags["redacted"] = True
    r = op.regions[0]
    return [
        FilterNode(
            "drawbox", {"x": r.x, "y": r.y, "w": r.w, "h": r.h, "color": "black", "t": "fill"}, "v"
        )
    ]


@pytest.mark.parametrize("keep", [[], ["subtitles", "attachments", "metadata"]])
def test_redacted_fails_closed_unless_kept(media_with_extras, tmp_path, monkeypatch, keep):
    monkeypatch.setitem(C.REGISTRY, "box", _fake(_redacting_box))
    info = P.probe(media_with_extras)
    region = {"x": 0, "y": 0, "w": 50, "h": 50}
    doc = _doc(
        media_with_extras,
        tmp_path / "red.mp4",
        [
            {
                "start": 0,
                "end": info.end,
                "ops": [{"op": "box", "regions": [region], "fill": "black"}],
            }
        ],
        keep=keep,
    )
    c = _compile(doc)
    d = c.to_dict()
    assert d["redacted"] is True and d["flags"]["redacted"] is True
    acts = {s.index: s.action for s in c.output_plan.streams}
    assert acts[info.video.index] == "encode"
    dst = _run(c)
    o = P.probe(dst)
    has_sub = any(s.type == "subtitle" for s in o.streams)
    has_pic = any(s.attached_pic for s in o.streams)
    title = o.tags.get("title")
    if keep:
        assert has_sub and has_pic and title == "Fixture Title"
        assert "-map_metadata" not in c.args and d["metadata"] == "kept"
    else:
        assert not has_sub and not has_pic and title is None
        assert c.args[c.args.index("-map_metadata") + 1] == "-1"
        assert d["metadata"] == "dropped"


# ------------------------------------------------------------------ fast mode


def test_fast_on_keyframes_is_exact_stream_copy(media_red_square, tmp_path):
    c = _compile(_doc(media_red_square, tmp_path / "f.mp4"), fast=True)
    assert c.mode == "fast" and c.graph is None
    assert c.snapped == {"start": 2.0, "end": 5.0, "exact": True}
    assert all(d.action == "copy" for d in c.output_plan.streams)
    args = list(c.args)
    assert args.index("-ss") < args.index("-i") and "-filter_complex" not in args
    dst = _run(c)
    # stream-copy end is packet (DTS) accurate: may run a few frames past a keyframe end
    assert P.probe(dst).video.duration == pytest.approx(3.0, abs=0.15)


def test_fast_off_keyframes_reports_snapped_points(media_red_square, tmp_path):
    doc = _doc(media_red_square, tmp_path / "f.mp4", [{"start": 2.5, "end": 4.5}])
    c = _compile(doc, fast=True)
    assert c.snapped == {"start": 2.0, "end": 5.0, "exact": False}
    assert c.to_dict()["snapped"] == c.snapped
    assert c.expected_duration == pytest.approx(3.0)


def test_fast_snaps_to_media_end_when_no_later_keyframe(media_mp4, tmp_path):
    info = P.probe(media_mp4)
    c = _compile(_doc(media_mp4, tmp_path / "f.mp4"), fast=True)
    assert c.snapped["start"] == 0.0 and c.snapped["end"] == pytest.approx(info.end)
    assert c.snapped["exact"] is False
    assert "-to" not in c.args
    dst = _run(c)
    assert P.probe(dst).video.duration == pytest.approx(info.video.duration, abs=0.1)


@pytest.mark.parametrize(
    "segments,extra",
    [
        ([{"start": 1, "end": 2}, {"start": 3, "end": 4}], {}),
        ([{"start": 1, "end": 2, "ops": [{"op": "speed", "factor": 2}]}], {}),
    ],
)
def test_fast_refused_for_non_plain_cuts(media_mp4, tmp_path, segments, extra):
    with pytest.raises(MediaInputError) as ei:
        _compile(_doc(media_mp4, tmp_path / "f.mp4", segments, **extra), fast=True)
    assert ei.value.kind == C.INPUT_FAST_UNSUPPORTED


def test_segment_context_is_frozen_with_shared_flags(media_mp4, tmp_path, monkeypatch):
    ctxs = []

    def rec(op, ctx):
        ctxs.append(ctx)
        ctx.flags.setdefault("n", 0)
        ctx.flags["n"] += 1
        return []

    monkeypatch.setitem(C.REGISTRY, "fade", _fake(rec))
    doc = _doc(
        media_mp4,
        tmp_path / "o.mp4",
        [
            {"start": 1, "end": 2, "ops": [{"op": "fade", "direction": "in", "duration": 0.5}]},
            {"start": 3, "end": 4, "ops": [{"op": "fade", "direction": "in", "duration": 0.5}]},
        ],
    )
    c = _compile(doc)
    assert isinstance(ctxs[0], SegmentContext)
    with pytest.raises(Exception):
        ctxs[0].width = 1  # type: ignore[misc]
    assert ctxs[0].flags is ctxs[1].flags and c.flags["n"] == 2
    assert ctxs[0].fps == pytest.approx(25.0) and ctxs[0].sample_rate == 44100
    assert JoinContext.__dataclass_params__.frozen  # type: ignore[attr-defined]
    assert math.isclose(ctxs[1].start, 3.0)


def test_audio_only_cut_concat(tmp_path):
    src = tmp_path / "tone.m4a"
    _tools.run(
        "ffmpeg",
        [
            "-v",
            "error",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=6",
            "-c:a",
            "aac",
            str(src),
        ],
        timeout=60,
    )
    c = _compile(_doc(src, tmp_path / "o.m4a", [{"start": 1, "end": 2}, {"start": 3, "end": 4.5}]))
    assert "concat=n=2:v=0:a=1" in c.graph and "[0:0]trim" not in c.graph
    assert c.expected_duration == pytest.approx(2.5)
    dst = _run(c)
    assert P.probe(dst).audio.duration == pytest.approx(2.5, abs=0.05)
