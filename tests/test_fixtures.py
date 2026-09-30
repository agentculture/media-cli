"""Prove the synthesized media fixtures in conftest.py have the advertised properties."""

from __future__ import annotations

import json
import subprocess

import pytest

from tests.conftest import RED_SQUARE_BOX


def probe(path) -> dict:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            "-show_entries",
            "stream_disposition",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return json.loads(out)


def streams(info: dict, kind: str) -> list[dict]:
    return [s for s in info["streams"] if s["codec_type"] == kind]


def test_mp4_h264_aac_10s(media_mp4):
    info = probe(media_mp4)
    (v,) = streams(info, "video")
    (a,) = streams(info, "audio")
    assert v["codec_name"] == "h264"
    assert a["codec_name"] == "aac"
    assert (v["width"], v["height"]) == (320, 240)
    assert float(info["format"]["duration"]) == pytest.approx(10.0, abs=0.3)


def test_mp4_keyframe_interval_is_sparse(media_mp4):
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-skip_frame",
            "nokey",
            "-show_entries",
            "frame=pts_time",
            "-of",
            "csv=p=0",
            str(media_mp4),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    # g=250 at 25 fps over 10 s: only the first frame is a keyframe.
    assert len(out) == 1


def test_mkv_vp8_opus(media_mkv_vp8_opus):
    info = probe(media_mkv_vp8_opus)
    assert streams(info, "video")[0]["codec_name"] == "vp8"
    assert streams(info, "audio")[0]["codec_name"] == "opus"
    assert float(info["format"]["duration"]) == pytest.approx(10.0, abs=0.3)


def test_vfr_offset_has_positive_start_and_variable_rate(media_vfr_offset):
    info = probe(media_vfr_offset)
    assert float(info["format"]["start_time"]) > 0
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "frame=pts_time",
            "-of",
            "csv=p=0",
            str(media_vfr_offset),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    times = [float(t.rstrip(",")) for t in out if t.strip(",")]
    deltas = {round(b - a, 3) for a, b in zip(times, times[1:])}
    assert len(deltas) > 1, "frame spacing is constant; not VFR"


def test_red_square_present_only_between_4_and_6s(media_red_square):
    x, y, w, h = RED_SQUARE_BOX
    cx, cy = x + w // 2, y + h // 2

    def pixel(t: float) -> tuple[int, int, int]:
        raw = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-ss",
                str(t),
                "-i",
                str(media_red_square),
                "-frames:v",
                "1",
                "-vf",
                f"format=rgb24,crop=1:1:{cx}:{cy}",
                "-f",
                "rawvideo",
                "-",
            ],
            check=True,
            capture_output=True,
        ).stdout
        return raw[0], raw[1], raw[2]

    r, g, b = pixel(5.0)
    assert r > 200 and g < 60 and b < 60
    for t in (2.0, 8.0):
        r, g, b = pixel(t)
        assert not (r > 200 and g < 60 and b < 60)


def test_with_extras_has_subtitle_cover_and_title(media_with_extras):
    info = probe(media_with_extras)
    assert info["format"]["tags"]["title"] == "Fixture Title"
    assert len(streams(info, "subtitle")) == 1
    assert len(streams(info, "audio")) == 1
    pics = [s for s in streams(info, "video") if s["disposition"]["attached_pic"] == 1]
    assert len(pics) == 1


def test_fixtures_skip_without_ffmpeg(pytester):
    """The fixture layer must skip, not fail, when ffmpeg is absent."""
    pytester.makeconftest(
        "import shutil\n"
        "shutil.which = lambda *_a, **_k: None\n"
        "from tests.conftest import _media_dir, media_mp4  # noqa: F401\n"
    )
    pytester.makepyfile("def test_x(media_mp4):\n    raise AssertionError('not skipped')\n")
    result = pytester.runpytest_subprocess()
    result.assert_outcomes(skipped=1)


def test_live_gateway_skipped_without_env(pytester, monkeypatch):
    monkeypatch.delenv("MEDIA_CLI_LIVE_GATEWAY", raising=False)
    pytester.makeconftest(
        "from tests.conftest import pytest_configure, pytest_collection_modifyitems  # noqa: F401\n"
    )
    pytester.makepyfile(
        "import pytest\n@pytest.mark.live_gateway\ndef test_live():\n    raise AssertionError\n"
    )
    result = pytester.runpytest_subprocess()
    result.assert_outcomes(skipped=1)


def test_live_gateway_runs_with_env(pytester, monkeypatch):
    monkeypatch.setenv("MEDIA_CLI_LIVE_GATEWAY", "1")
    pytester.makeconftest(
        "from tests.conftest import pytest_configure, pytest_collection_modifyitems  # noqa: F401\n"
    )
    pytester.makepyfile("import pytest\n@pytest.mark.live_gateway\ndef test_live():\n    pass\n")
    result = pytester.runpytest_subprocess()
    result.assert_outcomes(passed=1)


def test_no_media_checked_in():
    root = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, check=True
    ).stdout.split()
    assert not [f for f in root if f.endswith((".mp4", ".mkv", ".mp3", ".wav", ".webm"))]
