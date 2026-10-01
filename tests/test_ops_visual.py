"""Tests for media_cli.media.ops.visual: crop, speed, fade (task t11).

Node-list unit tests plus end-to-end runs: compile_editlist -> ffmpeg -> ffprobe /
raw gray pixel stats on the lavfi fixtures.
"""

from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from media_cli.media import _tools
from media_cli.media import compile as C
from media_cli.media import editlist as E
from media_cli.media import output as out
from media_cli.media import probe as P
from media_cli.media.errors import MediaInputError
from media_cli.media.ops import FilterNode, visual

FRAME = 1 / 25


def _ctx(**kw):
    base = dict(start=2.0, end=6.0, speed=1.0, has_video=True, has_audio=True)
    base.update(kw)
    ns = SimpleNamespace(**base)
    ns.duration = (ns.end - ns.start) / ns.speed
    ns.local_time = lambda t, ns=ns: (t - ns.start) / ns.speed
    return ns


def _names(nodes):
    return [(n.name, n.stream) for n in nodes]


# ------------------------------------------------------------------ node lists


def test_crop_nodes():
    nodes = visual.build(E.Crop(x=10, y=20, w=100, h=50), _ctx())
    assert nodes == [FilterNode("crop", {"x": 10, "y": 20, "w": 100, "h": 50}, "v")]


def test_speed_in_range_single_atempo():
    nodes = visual.build(E.Speed(factor=2.0), _ctx())
    assert nodes == [
        FilterNode("setpts", {"speed": 2.0}, "v"),
        FilterNode("atempo", {"tempo": 2.0}, "a"),
    ]


@pytest.mark.parametrize("factor", [0.25, 0.3, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0])
def test_speed_atempo_chain_product_and_bounds(factor):
    nodes = visual.build(E.Speed(factor=factor), _ctx())
    tempos = [n.params["tempo"] for n in nodes if n.name == "atempo"]
    assert tempos and all(0.5 <= t <= 2.0 for t in tempos)
    assert math.prod(tempos) == pytest.approx(factor, rel=1e-6)
    setpts = [n for n in nodes if n.name == "setpts"]
    assert len(setpts) == 1 and setpts[0].params["speed"] == pytest.approx(factor)
    assert "offset" not in setpts[0].params


def test_speed_extremes_chain_two_instances():
    assert len(visual.build(E.Speed(factor=4.0), _ctx())) == 3
    assert len(visual.build(E.Speed(factor=0.25), _ctx())) == 3


def test_fade_in_nodes_use_local_time():
    nodes = visual.build(E.Fade(direction="in", duration=1.0), _ctx())
    assert nodes == [
        FilterNode("fade", {"t": "in", "st": 0.0, "d": 1.0}, "v"),
        FilterNode("afade", {"t": "in", "st": 0.0, "d": 1.0}, "a"),
    ]


def test_fade_out_starts_at_local_end_minus_duration():
    nodes = visual.build(E.Fade(direction="out", duration=1.0), _ctx())
    assert nodes[0].params["st"] == pytest.approx(3.0)  # segment is 4 s long
    assert nodes[1].params["st"] == pytest.approx(3.0)
    assert nodes[0].params["t"] == "out"


def test_fade_out_accounts_for_earlier_speed():
    nodes = visual.build(E.Fade(direction="out", duration=0.5), _ctx(speed=2.0))
    assert nodes[0].params["st"] == pytest.approx(1.5)  # 4 s / 2 - 0.5


def test_fade_longer_than_segment_is_clamped():
    nodes = visual.build(E.Fade(direction="out", duration=10.0), _ctx())
    assert nodes[0].params["st"] == pytest.approx(0.0)
    assert nodes[0].params["d"] == pytest.approx(4.0)


def test_unknown_op_not_implemented():
    with pytest.raises(MediaInputError) as ei:
        visual.build(E.Box(regions=(), fill="black"), _ctx())
    assert ei.value.kind == "input.op_not_implemented"


# ------------------------------------------------------------------ end to end


def _render(src, dst, ops, seg=(1, 5)):
    doc = {
        "input": str(src),
        "output": str(dst),
        "segments": [{"start": seg[0], "end": seg[1], "ops": ops}],
    }
    el = E.parse(doc)
    compiled = C.compile_editlist(el, P.probe(el.input))
    _tools.run("ffmpeg", list(compiled.args), timeout=120)
    out.commit(compiled.output_plan)
    return P.probe(dst)


def _gray_frames(path, w, h):
    raw = path.with_suffix(".gray")
    _tools.run(
        "ffmpeg",
        [
            "-v",
            "error",
            "-y",
            "-i",
            str(path),
            "-an",
            "-pix_fmt",
            "gray",
            "-f",
            "rawvideo",
            str(raw),
        ],
        timeout=60,
    )
    data = raw.read_bytes()
    size = w * h
    return [data[i : i + size] for i in range(0, len(data), size)]


def _mean(frame):
    return sum(frame) / len(frame)


def test_crop_yields_requested_size(media_mp4, tmp_path):
    dst = tmp_path / "crop.mp4"
    info = _render(media_mp4, dst, [{"op": "crop", "x": 20, "y": 10, "w": 160, "h": 100}])
    assert (info.video.width, info.video.height) == (160, 100)


# 0.25x slow-motion loses up to ~3 frames of tail (the last source frame is not stretched
# by the CFR output), so its video tolerance is wider than the 1-frame bound at 2x.
@pytest.mark.parametrize(
    "factor,expected,frames", [(2.0, 2.0, 1.5), (0.25, 16.0, 4), (4.0, 1.0, 1.5)]
)
def test_speed_scales_duration_av_in_sync(media_mp4, tmp_path, factor, expected, frames):
    dst = tmp_path / f"speed{factor}.mp4"
    info = _render(media_mp4, dst, [{"op": "speed", "factor": factor}], seg=(0, 4))
    print(f"speed {factor}: v={info.video.duration} a={info.audio.duration}")
    assert info.video.duration == pytest.approx(expected, abs=FRAME * frames)
    assert info.audio.duration == pytest.approx(expected, abs=0.1)
    assert abs(info.video.duration - info.audio.duration) <= 0.1


def test_fade_in_first_frame_is_black(media_mp4, tmp_path):
    dst = tmp_path / "fin.mp4"
    info = _render(media_mp4, dst, [{"op": "fade", "direction": "in", "duration": 1}])
    frames = _gray_frames(dst, info.video.width, info.video.height)
    first, mid = _mean(frames[0]), _mean(frames[len(frames) // 2])
    print(f"fade-in first luma={first:.2f} mid luma={mid:.2f}")
    assert first < 5
    assert mid > 20


def test_fade_out_last_frame_is_dark(media_mp4, tmp_path):
    dst = tmp_path / "fout.mp4"
    info = _render(media_mp4, dst, [{"op": "fade", "direction": "out", "duration": 1}])
    frames = _gray_frames(dst, info.video.width, info.video.height)
    last, mid = _mean(frames[-1]), _mean(frames[len(frames) // 2])
    print(f"fade-out last luma={last:.2f} mid luma={mid:.2f}")
    assert last < 12
    assert mid > 20


def test_fade_out_after_speed_lands_at_the_end(media_mp4, tmp_path):
    dst = tmp_path / "sfade.mp4"
    info = _render(
        media_mp4,
        dst,
        [{"op": "speed", "factor": 2}, {"op": "fade", "direction": "out", "duration": 0.5}],
        seg=(0, 4),
    )
    frames = _gray_frames(dst, info.video.width, info.video.height)
    assert _mean(frames[-1]) < 12
    assert _mean(frames[-20]) > 20  # 0.8 s before the end: fade (0.5 s) not yet started
