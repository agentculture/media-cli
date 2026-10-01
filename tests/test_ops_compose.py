"""Tests for media_cli.media.ops.compose (task t13): xfade + acrossfade joins."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from media_cli.media import _tools
from media_cli.media import compile as C
from media_cli.media import editlist as E
from media_cli.media import output as out
from media_cli.media import probe as P
from media_cli.media.errors import MediaInputError
from media_cli.media.ops import INPUT_OP_NOT_IMPLEMENTED, FilterNode, JoinContext, compose

STYLES = ("fade", "dissolve", "wipeleft", "slideleft")
FRAME = 1 / 25  # fixtures are 25 fps


def _ctx(has_video=True, has_audio=True, offset=1.5) -> JoinContext:
    return JoinContext(
        index=0,
        left_duration=2.0,
        right_duration=2.0,
        offset=offset,
        info=None,  # type: ignore[arg-type]
        has_video=has_video,
        has_audio=has_audio,
        width=320,
        height=240,
        fps=25.0,
        sample_rate=44100,
        flags={},
    )


# ------------------------------------------------------------------ unit


@pytest.mark.parametrize("style", STYLES)
@pytest.mark.parametrize("ttype", E.TRANSITION_TYPES)
def test_build_returns_xfade_and_acrossfade(style, ttype):
    t = E.Transition(type=ttype, duration=0.5, style=style)
    nodes = compose.build(t, _ctx())
    assert nodes == [
        FilterNode("xfade", {"transition": style, "duration": 0.5, "offset": 1.5}, "v"),
        FilterNode("acrossfade", {"d": 0.5}, "a"),
    ]


def test_build_video_only_and_audio_only():
    t = E.Transition(type="xfade", duration=0.5, style="dissolve")
    v = compose.build(t, _ctx(has_audio=False))
    assert [n.name for n in v] == ["xfade"]
    a = compose.build(t, _ctx(has_video=False))
    assert a == [FilterNode("acrossfade", {"d": 0.5}, "a")]


def test_build_uses_ctx_offset():
    t = E.Transition(type="xfade", duration=0.25, style="fade")
    xf, _ac = compose.build(t, _ctx(offset=3.25))
    assert xf.params["offset"] == pytest.approx(3.25)


def test_build_is_pure_no_io(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("compose.build must not do I/O")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    compose.build(E.Transition(type="xfade", duration=0.5), _ctx())


def test_unsupported_style_is_not_implemented():
    bad = SimpleNamespace(type="xfade", duration=0.5, style="circleopen")
    with pytest.raises(MediaInputError) as ei:
        compose.build(bad, _ctx())
    assert ei.value.kind == INPUT_OP_NOT_IMPLEMENTED


def test_no_streams_is_not_implemented():
    with pytest.raises(MediaInputError) as ei:
        compose.build(E.Transition(type="xfade", duration=0.5), _ctx(False, False))
    assert ei.value.kind == INPUT_OP_NOT_IMPLEMENTED


# ------------------------------------------------------------------ end to end


def _doc(inp, outp, segments, transitions, **extra) -> dict:
    d = {
        "input": str(inp),
        "output": str(outp),
        "segments": segments,
        "transitions": transitions,
    }
    d.update(extra)
    return d


def _render(doc) -> tuple[C.CompiledEdit, str]:
    el = E.parse(doc)
    c = C.compile_editlist(el, P.probe(el.input))
    _tools.run("ffmpeg", list(c.args), timeout=120)
    return c, out.commit(c.output_plan)


THREE = [{"start": 0, "end": 2}, {"start": 3, "end": 5}, {"start": 6, "end": 8}]


def test_three_segments_two_half_second_transitions(media_mp4, tmp_path):
    doc = _doc(
        media_mp4,
        tmp_path / "o.mp4",
        THREE,
        [
            {"type": "xfade", "style": "fade", "duration": 0.5},
            {"type": "xfade", "style": "dissolve", "duration": 0.5},
        ],
    )
    c, dst = _render(doc)
    assert c.expected_duration == pytest.approx(5.0)
    assert "xfade=transition=fade" in c.graph and "acrossfade" in c.graph
    info = P.probe(dst)
    assert info.video.duration == pytest.approx(5.0, abs=FRAME)
    assert info.audio.duration == pytest.approx(5.0, abs=FRAME)
    assert abs(info.video.duration - info.audio.duration) <= FRAME


@pytest.mark.parametrize("style", STYLES)
def test_each_style_renders(media_mp4, tmp_path, style):
    doc = _doc(
        media_mp4,
        tmp_path / f"{style}.mp4",
        THREE[:2],
        [{"type": "xfade", "style": style, "duration": 0.5}],
    )
    _c, dst = _render(doc)
    info = P.probe(dst)
    assert info.video.duration == pytest.approx(3.5, abs=FRAME)
    assert info.audio.duration == pytest.approx(3.5, abs=FRAME)


def test_acrossfade_type_also_joins(media_mp4, tmp_path):
    doc = _doc(
        media_mp4,
        tmp_path / "a.mp4",
        THREE[:2],
        [{"type": "acrossfade", "style": "wipeleft", "duration": 0.5}],
    )
    _c, dst = _render(doc)
    assert P.probe(dst).video.duration == pytest.approx(3.5, abs=FRAME)


def _stream_end(path, kind: str) -> float:
    """Last packet end time of the first ``v``/``a`` stream (container-independent)."""
    res = _tools.run(
        "ffprobe",
        ["-v", "error", "-select_streams", f"{kind}:0", "-show_entries"]
        + ["packet=pts_time,duration_time", "-of", "json", str(path)],
        timeout=60,
    )
    pk = json.loads(res.stdout)["packets"]
    return max(float(p["pts_time"]) + float(p.get("duration_time", 0)) for p in pk)


def _crop_first(monkeypatch, w=160, h=120):
    """visual (t11) may still be a stub: fake a crop op so a segment changes size."""

    def build(op, ctx):
        return [FilterNode("crop", {"w": w, "h": h, "x": 0, "y": 0}, "v")]

    monkeypatch.setitem(C.REGISTRY, "crop", SimpleNamespace(build=build))


def test_mixed_size_segments_still_join(media_mp4, tmp_path, monkeypatch):
    _crop_first(monkeypatch)
    segs = [
        {"start": 0, "end": 2, "ops": [{"op": "crop", "x": 0, "y": 0, "w": 160, "h": 120}]},
        {"start": 3, "end": 5},
        {"start": 6, "end": 8},
    ]
    doc = _doc(
        media_mp4,
        tmp_path / "m.mp4",
        segs,
        [
            {"type": "xfade", "style": "slideleft", "duration": 0.5},
            {"type": "xfade", "style": "fade", "duration": 0.5},
        ],
    )
    c, dst = _render(doc)
    assert "scale=" in c.graph  # the compiler re-scaled the uncropped segments to match
    info = P.probe(dst)
    assert (info.video.width, info.video.height) == (160, 120)
    assert info.video.duration == pytest.approx(5.0, abs=FRAME)
    assert abs(info.video.duration - info.audio.duration) <= FRAME


def test_vfr_source_segments_join(media_vfr_offset, tmp_path):
    doc = _doc(
        media_vfr_offset,
        tmp_path / "v.mkv",
        THREE,
        [
            {"type": "xfade", "style": "fade", "duration": 0.5},
            {"type": "xfade", "style": "wipeleft", "duration": 0.5},
        ],
    )
    c, dst = _render(doc)
    assert "fps=" in c.graph  # CFR normalization precedes the joins
    # the output keeps the Matroska container, which carries no per-stream duration
    v_end, a_end = _stream_end(dst, "v"), _stream_end(dst, "a")
    assert v_end == pytest.approx(5.0, abs=2 * FRAME)
    assert abs(v_end - a_end) <= 2 * FRAME


@pytest.mark.requires_ffmpeg
def test_audioless_input_joins_video_only(tmp_path):
    _tools.require_filter("xfade")
    src = tmp_path / "silent.mp4"
    _tools.run(
        "ffmpeg",
        ["-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=6"]
        + ["-pix_fmt", "yuv420p", str(src)],
        timeout=60,
    )
    doc = _doc(
        src,
        tmp_path / "o.mp4",
        THREE[:2],
        [{"type": "xfade", "style": "dissolve", "duration": 0.5}],
    )
    c, dst = _render(doc)
    assert "acrossfade" not in c.graph
    info = P.probe(dst)
    assert info.audio is None
    assert info.video.duration == pytest.approx(3.5, abs=FRAME)
