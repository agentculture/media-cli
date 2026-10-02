"""Tests for media_cli.media.output: atomic, collision-safe output writer."""

from __future__ import annotations

import hashlib
import os
import stat

import pytest

from media_cli.media import _tools
from media_cli.media import output as out
from media_cli.media import probe as P
from media_cli.media.errors import MediaEnvError, MediaInputError
from media_cli.media.probe import MediaInfo, StreamInfo


def _sha(p) -> str:
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def _fake(path, fmt, *streams) -> MediaInfo:
    ss = tuple(
        StreamInfo(index=i, type=t, codec=c, disposition={"attached_pic": 1} if ap else {})
        for i, (t, c, ap) in enumerate(streams)
    )
    return MediaInfo(path=str(path), format_name=fmt, duration=1.0, start_time=0.0, streams=ss)


def _by_index(plan):
    return {d.index: d for d in plan.streams}


def test_untouched_streams_copy_touched_reencode_same_codec(media_mp4, tmp_path):
    plan = out.plan_output(media_mp4, tmp_path / "out.mp4", {0})
    d = _by_index(plan)
    assert plan.container == "mp4"
    assert d[0].action == "encode"
    assert d[0].encoder == "libx264"
    assert d[1].action == "copy"
    assert d[1].encoder is None
    assert plan.dst == str(tmp_path / "out.mp4")
    assert plan.container_fallback is None


def test_mkv_vp8_opus_keep_source_codecs(media_mkv_vp8_opus, tmp_path):
    plan = out.plan_output(media_mkv_vp8_opus, tmp_path / "o.mkv", {0, 1})
    d = _by_index(plan)
    assert plan.container == "matroska"
    assert (d[0].encoder, d[1].encoder) == ("libvpx", "libopus")


def test_extras_copied_dropped(media_with_extras, tmp_path):
    info = P.probe(media_with_extras)
    pic = next(s.index for s in info.streams if s.attached_pic)
    sub = next(s.index for s in info.streams if s.type == "subtitle")
    plan = out.plan_output(media_with_extras, tmp_path / "o.mp4", {0, pic}, drop_streams=(sub,))
    d = _by_index(plan)
    assert d[pic].action == "copy"  # cover art is never re-rendered
    assert d[sub].action == "drop"
    args = plan.ffmpeg_output_args()
    assert f"0:{sub}" not in args
    assert "-f" in args
    assert "mp4" in args


def test_missing_encoder_falls_back_to_h264(tmp_path, monkeypatch):
    monkeypatch.setattr(_tools, "has_encoder", lambda n: n != "libvpx")
    info = _fake(tmp_path / "a.mkv", "matroska,webm", ("video", "vp8", False))
    plan = out.plan_output(tmp_path / "a.mkv", tmp_path / "b.mkv", {0}, info=info)
    assert plan.streams[0].encoder == "libx264"
    assert plan.container == "matroska"


def test_unknown_codec_defaults(tmp_path):
    info = _fake(
        tmp_path / "a.mkv",
        "matroska,webm",
        ("video", "weirdvid", False),
        ("audio", "weirdaud", False),
    )
    plan = out.plan_output(tmp_path / "a.mkv", tmp_path / "b.mkv", {0, 1}, info=info)
    assert [d.encoder for d in plan.streams] == ["libx264", "aac"]


def test_container_fallback_to_mkv(tmp_path, monkeypatch):
    monkeypatch.setattr(_tools, "has_encoder", lambda n: n != "libvpx")
    info = _fake(tmp_path / "a.webm", "matroska,webm", ("video", "vp8", False))
    plan = out.plan_output(tmp_path / "a.webm", tmp_path / "b.webm", {0}, info=info)
    assert plan.container == "matroska"
    assert plan.dst.endswith("b.mkv")
    assert plan.container_fallback
    assert "h264" in plan.container_fallback
    assert plan.tmp_path.endswith(".mkv")
    assert plan.to_dict()["container_fallback"] == plan.container_fallback


def test_fallback_disallowed_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(_tools, "has_encoder", lambda n: n != "libvpx")
    info = _fake(tmp_path / "a.webm", "matroska,webm", ("video", "vp8", False))
    with pytest.raises(MediaInputError) as e:
        out.plan_output(
            tmp_path / "a.webm", tmp_path / "b.webm", {0}, info=info, allow_fallback=False
        )
    assert e.value.kind == out.INPUT_CONTAINER_INCOMPATIBLE


def test_dst_equals_src_always_refused(media_mp4, tmp_path):
    import shutil

    src = tmp_path / "s.mp4"
    shutil.copy(media_mp4, src)
    for kw in ({}, {"overwrite": True}):
        with pytest.raises(MediaInputError) as e:
            out.plan_output(src, src, set(), **kw)
        assert e.value.kind == out.INPUT_OUTPUT_IS_SOURCE
    link = tmp_path / "link.mp4"
    os.symlink(src, link)
    with pytest.raises(MediaInputError) as e:
        out.plan_output(src, link, set(), overwrite=True)
    assert e.value.kind == out.INPUT_OUTPUT_IS_SOURCE


def test_existing_dst_refused_unless_overwrite(media_mp4, tmp_path):
    dst = tmp_path / "x.mp4"
    dst.write_bytes(b"precious")
    with pytest.raises(MediaInputError) as e:
        out.plan_output(media_mp4, dst, set())
    assert e.value.kind == out.INPUT_OUTPUT_EXISTS
    assert dst.read_bytes() == b"precious"
    plan = out.plan_output(media_mp4, dst, set(), overwrite=True)
    assert plan.overwrite


def test_missing_dst_dir_refused(media_mp4, tmp_path):
    with pytest.raises(MediaInputError) as e:
        out.plan_output(media_mp4, tmp_path / "nope" / "x.mp4", set())
    assert e.value.kind == out.INPUT_OUTPUT_DIR_MISSING


def test_tmp_in_dst_dir_hidden_same_ext_unique(media_mp4, tmp_path):
    a = out.plan_output(media_mp4, tmp_path / "x.mp4", set())
    b = out.plan_output(media_mp4, tmp_path / "x.mp4", set())
    assert os.path.dirname(a.tmp_path) == str(tmp_path)
    assert os.path.basename(a.tmp_path).startswith(".")
    assert a.tmp_path.endswith(".mp4")
    assert a.tmp_path != b.tmp_path


def test_success_commit_and_src_untouched(media_mp4, tmp_path):
    import shutil

    src = tmp_path / "s.mp4"
    shutil.copy(media_mp4, src)
    os.chmod(src, 0o444)
    before = _sha(src)
    mtime = os.stat(src).st_mtime_ns
    dst = tmp_path / "out.mp4"
    plan = out.plan_output(src, dst, set())
    with out.atomic_output(plan) as tmp:
        assert not dst.exists()
        _tools.run("ffmpeg", ["-v", "error", "-y", "-i", str(src), *plan.ffmpeg_output_args(), tmp])
        assert not dst.exists()
    assert dst.exists()
    assert not os.path.exists(plan.tmp_path)
    assert P.probe(dst).video.codec == "h264"
    assert _sha(src) == before
    assert os.stat(src).st_mtime_ns == mtime
    assert stat.S_IMODE(os.stat(src).st_mode) == 0o444
    assert sorted(os.listdir(tmp_path)) == ["out.mp4", "s.mp4"]


@pytest.mark.parametrize("exc", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_kill_mid_write_leaves_no_dst_and_no_tmp(media_mp4, tmp_path, exc):
    dst = tmp_path / "out.mp4"
    plan = out.plan_output(media_mp4, dst, set())

    def killed_mid_write():
        with out.atomic_output(plan) as tmp:
            with open(tmp, "wb") as f:
                f.write(b"partial")
            assert os.path.exists(tmp)
            raise exc("killed")

    with pytest.raises(exc):
        killed_mid_write()
    assert not dst.exists()
    assert not os.path.exists(plan.tmp_path)
    assert os.listdir(tmp_path) == []


def test_failure_with_overwrite_leaves_existing_dst_intact(media_mp4, tmp_path):
    dst = tmp_path / "out.mp4"
    dst.write_bytes(b"old")
    plan = out.plan_output(media_mp4, dst, set(), overwrite=True)

    def failing_write():
        with out.atomic_output(plan) as tmp:
            open(tmp, "wb").write(b"new-partial")
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        failing_write()
    assert dst.read_bytes() == b"old"
    assert not os.path.exists(plan.tmp_path)


def test_overwrite_commit_replaces(media_mp4, tmp_path):
    dst = tmp_path / "out.mp4"
    dst.write_bytes(b"old")
    plan = out.plan_output(media_mp4, dst, set(), overwrite=True)
    with out.atomic_output(plan) as tmp:
        open(tmp, "wb").write(b"new")
    assert dst.read_bytes() == b"new"


def test_dst_appearing_before_commit_is_not_clobbered(media_mp4, tmp_path):
    dst = tmp_path / "out.mp4"
    plan = out.plan_output(media_mp4, dst, set())

    def racing_write():
        with out.atomic_output(plan) as tmp:
            open(tmp, "wb").write(b"mine")
            dst.write_bytes(b"someone else")

    with pytest.raises(MediaInputError) as e:
        racing_write()
    assert e.value.kind == out.INPUT_OUTPUT_EXISTS
    assert dst.read_bytes() == b"someone else"
    assert not os.path.exists(plan.tmp_path)


def test_commit_without_output_errors_and_cleans(media_mp4, tmp_path):
    plan = out.plan_output(media_mp4, tmp_path / "out.mp4", set())
    with pytest.raises(MediaEnvError):
        out.commit(plan)
    assert not (tmp_path / "out.mp4").exists()


def test_ffmpeg_output_args_and_label_override(media_mp4, tmp_path):
    plan = out.plan_output(media_mp4, tmp_path / "o.mp4", {0})
    args = plan.ffmpeg_output_args()
    assert args[:4] == ["-map", "0:0", "-map", "0:1"]
    assert args[args.index("-c:0") : args.index("-c:0") + 2] == ["-c:0", "libx264"]
    assert args[args.index("-c:1") : args.index("-c:1") + 2] == ["-c:1", "copy"]
    assert args[-2:] == ["-f", "mp4"]
    lab = plan.ffmpeg_output_args(map_labels={0: "[v0]"})
    assert "[v0]" in lab
    assert "0:0" not in lab


def test_to_dict_json_serializable(media_mp4, tmp_path):
    import json

    d = out.plan_output(media_mp4, tmp_path / "o.mp4", {0}).to_dict()
    json.dumps(d)
    assert d["streams"][0]["action"] == "encode"
    assert d["container"] == "mp4"


@pytest.mark.parametrize(
    "name,expect",
    [("out.mp4", ".mkv"), ("out.MP4", ".mkv"), ("out", ".mkv"), ("out.webm", ".mkv")],
)
def test_mkv_source_mismatched_extension_refused(media_mkv_vp8_opus, tmp_path, name, expect):
    with pytest.raises(MediaInputError) as e:
        out.plan_output(media_mkv_vp8_opus, tmp_path / name, set())
    assert e.value.kind == out.INPUT_OUTPUT_CONTAINER_MISMATCH == "input.output_container_mismatch"
    assert expect in e.value.message


def test_mp4_source_expects_mp4_m4v_and_case_insensitive(media_mp4, tmp_path):
    with pytest.raises(MediaInputError) as e:
        out.plan_output(media_mp4, tmp_path / "o.mkv", set())
    assert ".mp4" in e.value.message
    assert ".m4v" in e.value.message
    assert out.plan_output(media_mp4, tmp_path / "o.MP4", set()).container == "mp4"
    assert out.plan_output(media_mp4, tmp_path / "o.m4v", set()).container == "mp4"


def test_mov_and_webm_expected_extensions(tmp_path):
    for fmt, src, bad, want in (
        ("mov,mp4,m4a,3gp,3g2,mj2", "a.mov", "b.mp4", ".mov"),
        ("matroska,webm", "a.webm", "b.mkv", ".webm"),
    ):
        info = _fake(tmp_path / src, fmt, ("video", "vp9", False))
        with pytest.raises(MediaInputError) as e:
            out.plan_output(tmp_path / src, tmp_path / bad, set(), info=info)
        assert e.value.kind == out.INPUT_OUTPUT_CONTAINER_MISMATCH
        assert want in e.value.message


def test_mismatch_checked_before_fallback_and_exists(media_mp4, tmp_path):
    (tmp_path / "x.mkv").write_bytes(b"old")
    with pytest.raises(MediaInputError) as e:
        out.plan_output(media_mp4, tmp_path / "x.mkv", set())
    assert e.value.kind == out.INPUT_OUTPUT_CONTAINER_MISMATCH


@pytest.mark.requires_ffmpeg
def test_m4a_accepted_for_mp4_family_source(tmp_path):
    info = _fake(tmp_path / "t.m4a", "mov,mp4,m4a,3gp,3g2,mj2", ("audio", "aac", False))
    plan = out.plan_output(tmp_path / "t.m4a", tmp_path / "o.m4a", {0}, info=info)
    assert plan.container == "mp4"
    assert plan.dst.endswith("o.m4a")
