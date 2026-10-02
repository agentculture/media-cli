"""Tests for media_cli.media.ops.redact (task t12): box + blur, fail-closed streams.

Pixel checks decode single frames to 8-bit gray (luma) with ffmpeg and compare
in Python: the black box must be black inside (mean < 5) and leave everything
outside it as it was (PSNR > 40 dB against the source frame).
"""

from __future__ import annotations

import json
import math
from types import SimpleNamespace

import pytest

from media_cli.media import _tools
from media_cli.media import compile as C
from media_cli.media import editlist as E
from media_cli.media import output as out
from media_cli.media import probe as P
from media_cli.media.errors import MediaInputError
from media_cli.media.ops import Branch, FilterNode, SegmentContext, redact
from tests.conftest import FIXTURE_TITLE, RED_SQUARE_BOX

W, H = 320, 240


# ------------------------------------------------------------------ helpers


def _doc(inp, outp, segments, **extra) -> dict:
    d = {"input": str(inp), "output": str(outp), "segments": segments}
    d.update(extra)
    return d


def _compile(doc) -> C.CompiledEdit:
    el = E.parse(doc)
    return C.compile_editlist(el, P.probe(el.input))


def _run(compiled: C.CompiledEdit) -> str:
    _tools.run("ffmpeg", list(compiled.args), timeout=120)
    return out.commit(compiled.output_plan)


def _region(box=RED_SQUARE_BOX, **times) -> dict:
    x, y, w, h = box
    return {"x": x, "y": y, "w": w, "h": h, **times}


def _gray(path, t: float, tmp_path) -> bytes:
    """The frame with time ``t`` (output seconds; 25 fps grid) as W*H 8-bit luma bytes."""
    raw = tmp_path / f"f{abs(hash((str(path), t)))}.gray"
    _tools.run(
        "ffmpeg",
        [
            *("-v", "error", "-nostdin", "-y", "-ss", f"{max(t - 0.005, 0):.3f}", "-i", str(path)),
            *("-frames:v", "1", "-pix_fmt", "gray", "-f", "rawvideo", str(raw)),
        ],
        timeout=60,
    )
    data = raw.read_bytes()
    assert len(data) == W * H
    return data


def _pixels(data: bytes, box, *, inside: bool) -> list[int]:
    x, y, w, h = box
    vals = []
    for row in range(H):
        for col in range(W):
            hit = x <= col < x + w and y <= row < y + h
            if hit == inside:
                vals.append(data[row * W + col])
    return vals


def _mean(vals) -> float:
    return sum(vals) / len(vals)


def _var(vals) -> float:
    m = _mean(vals)
    return sum((v - m) ** 2 for v in vals) / len(vals)


def _detail(data: bytes, box) -> float:
    """Mean squared horizontal neighbour difference inside ``box`` (edge energy).

    Not the mean *absolute* difference: that is total variation, which a blur
    preserves across a monotone edge; squaring rewards sharp steps only.
    """
    x, y, w, h = box
    diffs = [
        (data[r * W + c + 1] - data[r * W + c]) ** 2
        for r in range(y, y + h)
        for c in range(x, x + w - 1)
    ]
    return _mean(diffs)


def _psnr(a, b) -> float:
    mse = sum((p - q) ** 2 for p, q in zip(a, b)) / len(a)
    return math.inf if mse == 0 else 10 * math.log10(255**2 / mse)


def _ctx(width=W, height=H, start=0.0, end=10.0, flags=None, info=None) -> SegmentContext:
    info = info or SimpleNamespace(start_time=0.0, to_source_seconds=lambda t: t)
    return SegmentContext(
        index=0,
        op_index=0,
        start=start,
        end=end,
        source_start=start,
        source_end=end,
        info=info,
        width=width,
        height=height,
        fps=25.0,
        sample_rate=None,
        has_video=True,
        has_audio=False,
        speed=1.0,
        clock_base=0.0,
        flags={} if flags is None else flags,
    )


def _box(*regions) -> E.Box:
    return E.Box(regions=tuple(regions), fill="black")


def _blur(*regions, strength=10) -> E.Blur:
    return E.Blur(regions=tuple(regions), strength=strength)


# ------------------------------------------------------------------ build(): pure unit tests


def test_box_emits_one_fill_drawbox_per_region_and_marks_redacted():
    flags: dict = {}
    nodes = redact.build(
        _box(E.Region(10, 20, 30, 40), E.Region(1, 2, 3, 4, start=2.0, end=3.0)), _ctx(flags=flags)
    )
    assert [n.name for n in nodes] == ["drawbox", "drawbox"]
    a, b = (dict(n.params) for n in nodes)
    assert a == {"x": 10, "y": 20, "w": 30, "h": 40, "color": "black", "t": "fill"}
    assert (b["x"], b["y"], b["w"], b["h"], b["t"]) == (1, 2, 3, 4, "fill")
    # covers the frame on screen at start (one 25 fps interval back) .. inclusive end
    assert b["enable_start"] == pytest.approx(1.96 + redact.WINDOW_TOLERANCE)
    assert b["enable_end"] == pytest.approx(3.0 + redact.WINDOW_TOLERANCE)
    assert flags["redacted"] is True
    json.dumps(flags)


def test_box_flags_list_exactly_what_is_covered():
    flags: dict = {}
    redact.build(_box(E.Region(10, 20, 30, 40, start=2.0)), _ctx(start=1.0, end=5.0, flags=flags))
    (entry,) = flags["redaction"]
    assert entry["op"] == "box"
    assert entry["fill"] == "black"
    assert entry["segment"] == 0
    assert entry["op_index"] == 0
    (r,) = entry["regions"]
    assert r["requested"] == {"x": 10, "y": 20, "w": 30, "h": 40, "start": 2.0, "end": None}
    assert r["covered"] == {"x": 10, "y": 20, "w": 30, "h": 40}
    assert r["window"] == {
        "start": 2.0,
        "end": 5.0,
        "covers_from": pytest.approx(1.96),
        "whole_segment": False,
    }


def test_whole_segment_region_has_no_enable():
    nodes = redact.build(_box(E.Region(0, 0, 8, 8)), _ctx())
    assert "enable_start" not in nodes[0].params
    # a window equal to the segment is also unconditional (no edge rounding risk)
    nodes = redact.build(_box(E.Region(0, 0, 8, 8, start=1.0, end=4.0)), _ctx(start=1, end=4))
    assert "enable_start" not in nodes[0].params


def test_region_window_is_clamped_to_the_segment():
    flags: dict = {}
    redact.build(
        _box(E.Region(0, 0, 8, 8, start=0.99995, end=3.5)), _ctx(start=1.0, end=3.0, flags=flags)
    )
    win = flags["redaction"][0]["regions"][0]["window"]
    assert win["start"] == 1.0
    assert win["end"] == 3.0
    assert win["covers_from"] == 1.0


def test_window_uses_ctx_local_time():
    ctx = _ctx(start=2.0, end=8.0)
    ctx_local = SegmentContext(**{**ctx.__dict__, "clock_base": 2.0, "speed": 2.0})
    nodes = redact.build(_box(E.Region(0, 0, 8, 8, start=4.0, end=6.0)), ctx_local)
    p = nodes[0].params
    # local = (t - clock_base) / speed; start pulled back one 25 fps frame (absolute)
    assert p["enable_start"] == pytest.approx((3.96 - 2.0) / 2 + redact.WINDOW_TOLERANCE)
    assert p["enable_end"] == pytest.approx((6.0 - 2.0) / 2 + redact.WINDOW_TOLERANCE)


def test_window_start_near_segment_start_is_not_pushed_past_it():
    (n,) = redact.build(_box(E.Region(0, 0, 8, 8, start=1.02, end=2.0)), _ctx(start=1.0, end=3.0))
    assert n.params["enable_start"] == pytest.approx(1.0 - redact.WINDOW_TOLERANCE)


def test_unknown_fps_falls_back_to_a_wide_interval():
    ctx = SegmentContext(**{**_ctx().__dict__, "fps": None})
    (n,) = redact.build(_box(E.Region(0, 0, 8, 8, start=5.0, end=6.0)), ctx)
    assert n.params["enable_start"] == pytest.approx(
        5.0 - redact.FALLBACK_FRAME_INTERVAL + redact.WINDOW_TOLERANCE
    )


def test_blur_is_one_branch_per_region_crop_then_boxblur():
    flags: dict = {}
    nodes = redact.build(
        _blur(E.Region(100, 60, 80, 80), E.Region(10, 10, 20, 20, start=1, end=2), strength=7),
        _ctx(flags=flags),
    )
    assert all(isinstance(n, Branch) for n in nodes)
    assert len(nodes) == 2
    b = nodes[0]
    crop, blur = b.nodes
    assert crop.name == "crop"
    assert dict(crop.params) == {
        "x": 100,
        "y": 60,
        "w": 80,
        "h": 80,
        "exact": 1,
    }
    assert blur.name == "boxblur"
    assert blur.params["luma_radius"] == 7
    assert blur.params["chroma_radius"] == 7
    assert (b.x, b.y, b.enable_start, b.enable_end) == (100, 60, None, None)
    assert nodes[1].enable_start is not None
    assert nodes[1].enable_start <= 1.0
    assert flags["redacted"] is True
    assert [e["op"] for e in flags["redaction"]] == ["blur"]
    assert flags["redaction"][0]["strength"] == 7
    json.dumps(flags)


def test_blur_radius_is_clamped_to_fit_the_region():
    flags: dict = {}
    (b,) = redact.build(_blur(E.Region(0, 0, 10, 40), strength=50), _ctx(flags=flags))
    p = b.nodes[1].params
    # luma: 2*r <= min(10, 40); chroma (4:2:0 worst case 5x20): 2*r <= 5
    assert p["luma_radius"] == 5
    assert p["chroma_radius"] == 2
    r = flags["redaction"][0]["regions"][0]
    assert r["luma_radius"] == 5
    assert r["chroma_radius"] == 2


def test_blur_odd_region_is_expanded_outward_to_the_chroma_grid():
    """yuv420 crop/overlay round x/y down to even: never shift the patch off the region."""
    flags: dict = {}
    (b,) = redact.build(_blur(E.Region(101, 61, 79, 78)), _ctx(flags=flags))
    crop = dict(b.nodes[0].params)
    assert (crop["x"], crop["y"]) == (100, 60)
    assert (b.x, b.y) == (100, 60)
    # covers 101..179 x 61..138 -> 100..179 (w 80) x 60..139 (h 80)
    assert (crop["w"], crop["h"]) == (80, 80)
    cov = flags["redaction"][0]["regions"][0]["covered"]
    assert cov == {"x": 100, "y": 60, "w": 80, "h": 80}


def test_blur_expansion_never_leaves_the_frame():
    (b,) = redact.build(_blur(E.Region(315, 235, 4, 4)), _ctx(width=319, height=239))
    crop = dict(b.nodes[0].params)
    assert crop["x"] + crop["w"] <= 319
    assert crop["y"] + crop["h"] <= 239
    assert crop["x"] <= 315
    assert crop["x"] + crop["w"] >= 319


def test_blur_tiny_region_is_refused():
    op = _blur(E.Region(10, 10, 2, 50))
    ctx = _ctx()
    with pytest.raises(MediaInputError) as ei:
        redact.build(op, ctx)
    assert ei.value.kind == "input.region_too_small"
    assert "box" in ei.value.remediation


def test_region_outside_the_current_frame_is_refused():
    """After a crop, ctx.width/height shrink; a region that no longer fits fails closed."""
    ctx = _ctx(width=60, height=120)
    for op in (_box(E.Region(0, 0, 80, 80)), _blur(E.Region(0, 0, 80, 80))):
        with pytest.raises(MediaInputError) as ei:
            redact.build(op, ctx)
        assert ei.value.kind == "input.region_outside_frame"


def test_no_video_is_refused():
    ctx = SegmentContext(**{**_ctx().__dict__, "has_video": False})
    op = _box(E.Region(0, 0, 8, 8))
    with pytest.raises(MediaInputError):
        redact.build(op, ctx)


def test_unknown_op_is_refused():
    op = E.Crop(0, 0, 8, 8)
    ctx = _ctx()
    with pytest.raises(MediaInputError):
        redact.build(op, ctx)


def test_flags_accumulate_across_ops():
    flags: dict = {}
    redact.build(_box(E.Region(0, 0, 8, 8)), _ctx(flags=flags))
    redact.build(_blur(E.Region(0, 0, 8, 8)), _ctx(flags=flags))
    assert [e["op"] for e in flags["redaction"]] == ["box", "blur"]


def test_build_returns_only_typed_nodes():
    nodes = redact.build(_box(E.Region(0, 0, 8, 8)), _ctx()) + redact.build(
        _blur(E.Region(0, 0, 8, 8)), _ctx()
    )
    for n in nodes:
        assert isinstance(n, (FilterNode, Branch))


# ------------------------------------------------------------------ end to end


def _video_pos(c: C.CompiledEdit, info) -> int:
    kept = [d for d in c.output_plan.streams if d.action != "drop"]
    return [d.index for d in kept].index(info.video.index)


def _assert_fail_closed_args(c: C.CompiledEdit, info) -> None:
    args = list(c.args)
    pos = _video_pos(c, info)
    assert args[args.index(f"-c:{pos}") + 1] != "copy", "redacted video must be re-encoded"
    assert "-map_metadata" in args
    assert args[args.index("-map_metadata") + 1] == "-1"
    assert args[args.index("-map_chapters") + 1] == "-1"
    maps = [args[i + 1] for i, a in enumerate(args) if a == "-map"]
    assert maps[0] in ("[vout]",)  # the redacted video comes from the graph, explicitly
    assert len(maps) == len([d for d in c.output_plan.streams if d.action != "drop"])


def test_box_blacks_the_red_square_and_leaves_the_rest(media_red_square, tmp_path):
    info = P.probe(media_red_square)
    doc = _doc(
        media_red_square,
        tmp_path / "box.mp4",
        [
            {
                "start": 0,
                "end": info.end,
                "ops": [{"op": "box", "regions": [_region(start=4.0, end=6.0)], "fill": "black"}],
            }
        ],
    )
    c = _compile(doc)
    assert c.redacted
    assert c.to_dict()["flags"]["redaction"][0]["op"] == "box"
    _assert_fail_closed_args(c, info)
    dst = _run(c)
    for t in (4.0, 5.0, 6.0):
        o, s = _gray(dst, t, tmp_path), _gray(media_red_square, t, tmp_path)
        inside = _mean(_pixels(o, RED_SQUARE_BOX, inside=True))
        assert inside < 5, f"t={t}: box mean {inside}"
        assert _mean(_pixels(s, RED_SQUARE_BOX, inside=True)) > 40  # the source is red there
        psnr = _psnr(
            _pixels(o, RED_SQUARE_BOX, inside=False), _pixels(s, RED_SQUARE_BOX, inside=False)
        )
        assert psnr > 40, f"t={t}: outside PSNR {psnr}"
    # frames not presented during 4..6 are untouched (whole-frame PSNR vs source)
    for t in (3.0, 3.96, 6.04):
        o, s = _gray(dst, t, tmp_path), _gray(media_red_square, t, tmp_path)
        assert _psnr(o, s) > 40, f"t={t}: frame changed outside the window"


def test_box_window_in_a_cut_segment_maps_to_local_time(media_red_square, tmp_path):
    # segment 3..8 (output t = abs - 3); box only 4.5..5.5 over the square shown 4..6.
    # 25 fps: frames ..4.44, 4.48 (on screen at 4.5), 4.52 .. 5.48 (on screen at 5.5), 5.52
    region = _region(start=4.5, end=5.5)
    doc = _doc(
        media_red_square,
        tmp_path / "win.mp4",
        [{"start": 3, "end": 8, "ops": [{"op": "box", "regions": [region], "fill": "black"}]}],
    )
    c = _compile(doc)
    assert c.mode == "filter"
    dst = _run(c)
    at = {
        t: _mean(_pixels(_gray(dst, t - 3, tmp_path), RED_SQUARE_BOX, inside=True))
        for t in (4.2, 4.44, 4.48, 4.52, 5.0, 5.48, 5.52, 5.8)
    }
    for t in (4.48, 4.52, 5.0, 5.48):
        assert at[t] < 5, (t, at)
    for t in (4.2, 4.44, 5.52, 5.8):
        assert at[t] > 40, (t, at)  # not presented during the window: still red
    # before the square appears at all: identical to the source
    o, s = _gray(dst, 0.0, tmp_path), _gray(media_red_square, 3.0, tmp_path)
    assert _psnr(o, s) > 40


def test_blur_lowers_variance_inside_only(media_red_square, tmp_path):
    info = P.probe(media_red_square)
    box = (40, 40, 120, 100)  # textured testsrc area
    doc = _doc(
        media_red_square,
        tmp_path / "blur.mp4",
        [
            {
                "start": 0,
                "end": info.end,
                "ops": [{"op": "blur", "regions": [_region(box)], "strength": 20}],
            }
        ],
    )
    c = _compile(doc)
    assert "boxblur=luma_radius=20" in c.graph
    assert "split=outputs=2" in c.graph
    _assert_fail_closed_args(c, info)
    dst = _run(c)
    o, s = _gray(dst, 2.0, tmp_path), _gray(media_red_square, 2.0, tmp_path)
    vin_o, vin_s = _var(_pixels(o, box, inside=True)), _var(_pixels(s, box, inside=True))
    assert vin_o < vin_s, (vin_o, vin_s)
    d_o, d_s = _detail(o, box), _detail(s, box)
    assert d_o < 0.5 * d_s, (d_o, d_s)
    assert _psnr(_pixels(o, box, inside=False), _pixels(s, box, inside=False)) > 40


def test_blur_window_only_within_start_end(media_red_square, tmp_path):
    box = (40, 40, 120, 100)
    region = _region(box, start=2.0, end=3.0)
    doc = _doc(
        media_red_square,
        tmp_path / "bw.mp4",
        [{"start": 1, "end": 4, "ops": [{"op": "blur", "regions": [region], "strength": 20}]}],
    )
    dst = _run(_compile(doc))
    for t_abs, blurred in ((1.5, False), (2.5, True), (3.5, False)):
        o, s = _gray(dst, t_abs - 1, tmp_path), _gray(media_red_square, t_abs, tmp_path)
        do, ds = _detail(o, box), _detail(s, box)
        assert (do < 0.5 * ds) is blurred, (t_abs, do, ds)


def _extras_doc(src, dst, keep):
    info = P.probe(src)
    return _doc(
        src,
        dst,
        [
            {
                "start": 0,
                "end": info.end,
                "ops": [{"op": "box", "regions": [_region((0, 0, 40, 40))], "fill": "black"}],
            }
        ],
        keep=keep,
    )


def test_redacted_extras_come_out_without_subtitle_cover_or_title(media_with_extras, tmp_path):
    info = P.probe(media_with_extras)
    assert any(s.type == "subtitle" for s in info.streams)
    assert any(s.attached_pic for s in info.streams)
    assert info.tags.get("title") == FIXTURE_TITLE
    c = _compile(_extras_doc(media_with_extras, tmp_path / "x.mp4", []))
    _assert_fail_closed_args(c, info)
    d = c.to_dict()
    assert d["redacted"] is True
    assert d["metadata"] == "dropped"
    # the plan lists kept and dropped streams
    acts = {s["index"]: s["action"] for s in d["output"]["streams"]}
    for s in info.streams:
        if s.type == "subtitle" or s.attached_pic:
            assert acts[s.index] == "drop"
    assert acts[info.video.index] == "encode"
    dst = _run(c)
    o = P.probe(dst)
    assert not any(s.type in ("subtitle", "attachment", "data") for s in o.streams)
    assert not any(s.attached_pic for s in o.streams)
    assert [s.type for s in o.streams if s.type == "video"] == ["video"]
    assert "title" not in o.tags


def test_redacted_extras_kept_when_named(media_with_extras, tmp_path):
    info = P.probe(media_with_extras)
    c = _compile(_extras_doc(media_with_extras, tmp_path / "k.mp4", ["subtitles", "metadata"]))
    d = c.to_dict()
    assert d["redacted"] is True
    assert d["metadata"] == "kept"
    assert "-map_metadata" not in c.args
    pos = _video_pos(c, info)
    assert c.args[c.args.index(f"-c:{pos}") + 1] != "copy"
    dst = _run(c)
    o = P.probe(dst)
    assert any(s.type == "subtitle" for s in o.streams)
    assert not any(s.attached_pic for s in o.streams)  # attachments not named: dropped
    assert o.tags.get("title") == FIXTURE_TITLE


def test_crop_then_box_uses_cropped_coordinates(media_red_square, tmp_path, monkeypatch):
    """Regions after a crop are in the cropped frame's coordinates (as validate checks)."""

    def crop(op, ctx):
        return [FilterNode("crop", {"x": op.x, "y": op.y, "w": op.w, "h": op.h, "exact": 1}, "v")]

    monkeypatch.setitem(C.REGISTRY, "crop", SimpleNamespace(build=crop))
    x, y, w, h = RED_SQUARE_BOX
    doc = _doc(
        media_red_square,
        tmp_path / "cb.mp4",
        [
            {
                "start": 4,
                "end": 6,
                "ops": [
                    {"op": "crop", "x": x - 20, "y": y - 20, "w": w + 40, "h": h + 40},
                    {"op": "box", "regions": [_region((20, 20, w, h))], "fill": "black"},
                ],
            }
        ],
    )
    c = _compile(doc)
    assert "drawbox=x=20:y=20:w=80:h=80" in c.graph
    dst = _run(c)
    raw = tmp_path / "cb.gray"
    _tools.run(
        "ffmpeg",
        [
            *("-v", "error", "-nostdin", "-y", "-ss", "1.0", "-i", dst, "-frames:v", "1"),
            *("-pix_fmt", "gray", "-f", "rawvideo", str(raw)),
        ],
        timeout=60,
    )
    data, cw = raw.read_bytes(), w + 40
    inside = [data[r * cw + col] for r in range(20, 20 + h) for col in range(20, 20 + w)]
    assert _mean(inside) < 5
