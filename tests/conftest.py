"""Shared test fixtures: media files synthesized on the fly with ffmpeg lavfi.

Nothing binary is checked in.  Every fixture is session-scoped (each file is
generated once per run, into a directory made by ``tmp_path_factory`` because
function-scoped ``tmp_path`` cannot back a session fixture) and skips, rather
than fails, when ``ffmpeg`` is not on PATH.

Fixtures (all 320x240; the video source is ``testsrc`` and audio is a 440 Hz
``sine``, each 10 s long unless noted):

``media_mp4``
    ``media.mp4``: 1 video (h264, 25 fps, ``-g 250`` so only frame 0 is a
    keyframe) + 1 audio (aac).  Duration ~10 s.
``media_mkv_vp8_opus``
    ``media.mkv``: 1 video (vp8) + 1 audio (opus).  Duration ~10 s.
``media_vfr_offset``
    ``vfr_offset.mkv``: h264 + aac, variable frame rate (frames are dropped
    irregularly) and container ``start_time`` of ~1.5 s (``-output_ts_offset``).
``media_red_square``
    ``red_square.mp4``: h264 + aac.  A solid red (``0xFF0000``) square is drawn
    with ``drawbox`` only while 4.0 <= t <= 6.0 s.  Exact pixel box is
    ``RED_SQUARE_BOX`` = (x=100, y=60, w=80, h=80), i.e. x 100..179, y 60..139.
``media_with_extras``
    ``extras.mp4``: video (h264) + audio (aac) + 1 subtitle stream (mov_text,
    from a tiny SRT) + a cover-art PNG as a second video stream flagged
    ``attached_pic``; format tag ``title`` = ``"Fixture Title"`` (``FIXTURE_TITLE``).

Marker ``live_gateway``: tests carrying it are skipped unless
``MEDIA_CLI_LIVE_GATEWAY=1``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

SIZE = "320x240"
DURATION = 10
RED_SQUARE_BOX = (100, 60, 80, 80)  # x, y, w, h
FIXTURE_TITLE = "Fixture Title"
VFR_START_OFFSET = 1.5

_SRT = "1\n00:00:01,000 --> 00:00:03,000\nhello\n\n2\n00:00:04,000 --> 00:00:06,000\nworld\n"


def _ffmpeg(*args: str) -> None:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    cmd = ["ffmpeg", "-v", "error", "-y", *args]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        pytest.skip(f"ffmpeg could not synthesize fixture: {exc.stderr.strip()[:200]}")


def _sources(vf: str = "") -> list[str]:
    video = f"testsrc=size={SIZE}:rate=25:duration={DURATION}"
    if vf:
        video += "," + vf
    return [
        "-f",
        "lavfi",
        "-i",
        video,
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:duration={DURATION}",
    ]


@pytest.fixture(scope="session")
def _media_dir(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("media")


@pytest.fixture(scope="session")
def media_mp4(_media_dir) -> Path:
    out = _media_dir / "media.mp4"
    _ffmpeg(
        *_sources(),
        *("-c:v", "libx264", "-g", "250", "-pix_fmt", "yuv420p", "-c:a", "aac"),
        "-shortest",
        str(out),
    )
    return out


@pytest.fixture(scope="session")
def media_mkv_vp8_opus(_media_dir) -> Path:
    out = _media_dir / "media.mkv"
    _ffmpeg(
        *_sources(),
        *("-c:v", "libvpx", "-b:v", "500k", "-c:a", "libopus"),
        "-shortest",
        str(out),
    )
    return out


@pytest.fixture(scope="session")
def media_vfr_offset(_media_dir) -> Path:
    out = _media_dir / "vfr_offset.mkv"
    # Keep frames where n%5 is 0..2 or n%7 is 0: irregular gaps => VFR.
    select = "select='lt(mod(n,5),3)+eq(mod(n,7),0)'"
    _ffmpeg(
        *_sources(select),
        *("-fps_mode", "vfr", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac"),
        *("-output_ts_offset", str(VFR_START_OFFSET)),
        "-shortest",
        str(out),
    )
    return out


@pytest.fixture(scope="session")
def media_red_square(_media_dir) -> Path:
    x, y, w, h = RED_SQUARE_BOX
    box = f"drawbox=x={x}:y={y}:w={w}:h={h}:color=red:t=fill:enable='between(t,4,6)'"
    out = _media_dir / "red_square.mp4"
    _ffmpeg(
        *_sources(box),
        *("-c:v", "libx264", "-g", "25", "-pix_fmt", "yuv420p", "-c:a", "aac"),
        "-shortest",
        str(out),
    )
    return out


@pytest.fixture(scope="session")
def media_with_extras(_media_dir) -> Path:
    srt = _media_dir / "sub.srt"
    srt.write_text(_SRT, encoding="utf-8")
    cover = _media_dir / "cover.png"
    _ffmpeg("-f", "lavfi", "-i", "color=c=blue:size=64x64", "-frames:v", "1", str(cover))
    out = _media_dir / "extras.mp4"
    _ffmpeg(
        *_sources(),
        *("-i", str(srt), "-i", str(cover)),
        *("-map", "0:v", "-map", "1:a", "-map", "2:s", "-map", "3:v"),
        *("-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-c:s", "mov_text"),
        *("-c:v:1", "png", "-disposition:v:1", "attached_pic"),
        *("-metadata", f"title={FIXTURE_TITLE}"),
        str(out),
    )
    return out


def pytest_configure(config) -> None:
    config.addinivalue_line(
        "markers", "live_gateway: needs a live gateway; skipped unless MEDIA_CLI_LIVE_GATEWAY=1"
    )


def pytest_collection_modifyitems(config, items) -> None:
    if os.environ.get("MEDIA_CLI_LIVE_GATEWAY") == "1":
        return
    skip = pytest.mark.skip(reason="set MEDIA_CLI_LIVE_GATEWAY=1 to run live gateway tests")
    for item in items:
        if "live_gateway" in item.keywords:
            item.add_marker(skip)
