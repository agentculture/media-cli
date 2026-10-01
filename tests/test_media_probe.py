"""Tests for media_cli.media.probe: probe facts and the single time base."""

from __future__ import annotations

import json

import pytest

from media_cli.media import probe as P
from media_cli.media.errors import (
    INPUT_TIMESTAMP_OUT_OF_RANGE,
    INPUT_UNREADABLE,
    MediaInputError,
)
from tests.conftest import DURATION, FIXTURE_TITLE, VFR_START_OFFSET


def test_probe_mp4_facts(media_mp4):
    info = P.probe(media_mp4)
    assert info.video is not None and info.audio is not None
    v = info.video
    assert (v.type, v.codec, v.width, v.height) == ("video", "h264", 320, 240)
    assert v.fps == pytest.approx(25.0)
    assert info.audio.type == "audio"
    assert info.audio.codec == "aac"
    assert info.audio.sample_rate == 44100
    assert info.audio.fps is None
    assert info.duration == pytest.approx(DURATION, abs=0.2)
    assert "mp4" in info.format_name
    assert info.start_time == pytest.approx(0.0, abs=0.1)


def test_probe_mkv_codecs(media_mkv_vp8_opus):
    info = P.probe(media_mkv_vp8_opus)
    assert info.video.codec == "vp8"
    assert info.audio.codec == "opus"


def test_probe_extras_skips_attached_pic(media_with_extras):
    info = P.probe(media_with_extras)
    types = [s.type for s in info.streams]
    assert "subtitle" in types
    pics = [s for s in info.streams if s.attached_pic]
    assert len(pics) == 1
    assert info.video is not None and not info.video.attached_pic
    assert info.video.index == 0
    assert info.tags.get("title") == FIXTURE_TITLE


def test_probe_to_dict_is_json_serializable(media_mp4):
    d = P.probe(media_mp4).to_dict()
    json.dumps(d)
    assert d["video"]["codec"] == "h264"
    assert d["streams"][0]["index"] == 0


def test_time_base_subtracts_start(media_vfr_offset):
    info = P.probe(media_vfr_offset)
    assert info.start_time == pytest.approx(1.477, abs=0.01)  # raw, transparent
    times = P.frame_times(media_vfr_offset)
    assert times[0] == pytest.approx(0.0, abs=1e-6)
    assert times == sorted(times)
    # VFR: irregular spacing
    gaps = {round(b - a, 3) for a, b in zip(times, times[1:])}
    assert len(gaps) > 1
    # raw first frame pts is ~1.5
    assert P.to_source_seconds(info, 0.0) == pytest.approx(VFR_START_OFFSET, abs=1e-3)
    assert P.from_source_seconds(info, VFR_START_OFFSET) == pytest.approx(0.0, abs=1e-3)


def test_round_trip_vfr(media_vfr_offset):
    times = P.frame_times(media_vfr_offset)
    for i in (0, 1, 2, 3, 10, len(times) - 1):
        t = P.to_seconds(media_vfr_offset, i)
        assert t == times[i]
        assert P.to_frame_index(media_vfr_offset, t) == i
    # mid-gap time selects the earlier frame
    i = next(k for k in range(len(times) - 1) if times[k + 1] - times[k] > 0.1)
    mid = (times[i] + times[i + 1]) / 2
    assert P.to_frame_index(media_vfr_offset, mid) == i
    # just before the next frame still selects the earlier one; at it, the next
    assert P.to_frame_index(media_vfr_offset, times[i + 1] - 0.01) == i
    assert P.to_frame_index(media_vfr_offset, times[i + 1]) == i + 1


def test_frame_index_accepts_info_and_path(media_vfr_offset):
    info = P.probe(media_vfr_offset)
    assert P.to_frame_index(info, 0.5) == P.to_frame_index(media_vfr_offset, 0.5)
    assert P.to_seconds(info, 4) == P.to_seconds(str(media_vfr_offset), 4)


def test_frame_times_lazy_and_cached(media_vfr_offset, monkeypatch):
    P.clear_cache()
    calls = []
    real = P._tools.run

    def spy(tool, args, **kw):
        calls.append(list(args))
        return real(tool, args, **kw)

    monkeypatch.setattr(P._tools, "run", spy)
    P.probe(media_vfr_offset)
    assert not any("frame=pts_time" in " ".join(a) for a in calls)
    P.frame_times(media_vfr_offset)
    P.frame_times(media_vfr_offset)
    P.to_frame_index(media_vfr_offset, 1.0)
    frame_calls = [a for a in calls if "frame=pts_time" in " ".join(a)]
    assert len(frame_calls) == 1


def test_out_of_range_timestamps(media_vfr_offset):
    with pytest.raises(MediaInputError) as ei:
        P.to_frame_index(media_vfr_offset, -1.0)
    assert ei.value.kind == INPUT_TIMESTAMP_OUT_OF_RANGE
    with pytest.raises(MediaInputError) as ei:
        P.to_frame_index(media_vfr_offset, 9999.0)
    assert ei.value.kind == INPUT_TIMESTAMP_OUT_OF_RANGE
    with pytest.raises(MediaInputError) as ei:
        P.to_seconds(media_vfr_offset, 10**6)
    assert ei.value.kind == INPUT_TIMESTAMP_OUT_OF_RANGE


def test_mp4_time_base_is_identity_ish(media_mp4):
    info = P.probe(media_mp4)
    assert P.to_source_seconds(info, 2.0) == pytest.approx(2.0 + P.origin(info), abs=1e-9)
    assert P.to_frame_index(media_mp4, 1.0) == 25


def test_missing_file_unreadable(tmp_path):
    with pytest.raises(MediaInputError) as ei:
        P.probe(tmp_path / "nope.mp4")
    assert ei.value.kind == INPUT_UNREADABLE
    assert ei.value.remediation


def test_text_file_unreadable(tmp_path):
    f = tmp_path / "x.mp4"
    f.write_text("this is not media\n" * 50)
    with pytest.raises(MediaInputError) as ei:
        P.probe(f)
    assert ei.value.kind == INPUT_UNREADABLE


def test_truncated_file_unreadable(media_mp4, tmp_path):
    f = tmp_path / "trunc.mp4"
    f.write_bytes(media_mp4.read_bytes()[:2000])
    with pytest.raises(MediaInputError) as ei:
        P.probe(f)
    assert ei.value.kind == INPUT_UNREADABLE


def test_directory_unreadable(tmp_path):
    with pytest.raises(MediaInputError) as ei:
        P.probe(tmp_path)
    assert ei.value.kind == INPUT_UNREADABLE


def test_probe_json_exposes_editable_range(media_mp4):
    info = P.probe(media_mp4)
    d = info.to_dict()
    json.dumps(d)
    assert d["editable"]["start"] == 0.0
    assert d["editable"]["end"] == pytest.approx(info.end)
    assert d["editable"]["frame_interval"] == pytest.approx(1 / 25.0)


def test_editable_end_is_normalized_not_container_duration(media_mkv_vp8_opus):
    d = P.probe(media_mkv_vp8_opus).to_dict()
    info = P.probe(media_mkv_vp8_opus)
    assert d["editable"]["end"] == pytest.approx(info.end)
    assert d["editable"]["end"] == pytest.approx(info.start_time + info.duration - info.origin)


def test_editable_frame_interval_null_without_video():
    info = P.MediaInfo(
        path="a.wav",
        format_name="wav",
        duration=3.0,
        start_time=0.0,
        streams=(P.StreamInfo(index=0, type="audio", codec="pcm_s16le"),),
    )
    assert info.to_dict()["editable"] == {"start": 0.0, "end": 3.0, "frame_interval": None}
