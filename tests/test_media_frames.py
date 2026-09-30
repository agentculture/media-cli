"""Tests for media_cli.media.frames: local frame peek + contact sheet."""

from __future__ import annotations

import ast
import hashlib
import os
import re
import shutil
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest

from media_cli.media import frames as F
from media_cli.media import probe as P
from media_cli.media.errors import INPUT_TIMESTAMP_OUT_OF_RANGE, MediaEnvError, MediaInputError

RED_PT = (140, 100)  # inside the red box (x100..179, y60..139)


def _pixel(png, x, y) -> tuple[int, int, int]:
    cp = subprocess.run(
        [
            shutil.which("ffmpeg"),
            *("-v", "error", "-i", str(png)),
            *("-vf", f"crop=1:1:{x}:{y},format=rgb24", "-f", "rawvideo", "-"),
        ],
        capture_output=True,
        check=True,
    )
    return tuple(cp.stdout[:3])


def _is_red(rgb) -> bool:
    r, g, b = rgb
    return r > 200 and g < 60 and b < 60


def _size(png) -> tuple[int, int]:
    data = Path(png).read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def _sha(p) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def test_extract_times_names_and_shape(media_mp4, tmp_path):
    res = F.extract(media_mp4, times=[1.0, 2.5], outdir=tmp_path)
    assert [set(r) for r in res] == [{"t", "frame_index", "path"}] * 2
    assert [r["frame_index"] for r in res] == [25, 62]
    for r in res:
        assert Path(r["path"]).is_file() and _size(r["path"]) == (320, 240)
        assert re.fullmatch(r"frame_t\d{6}\.\d{3}_f\d{5}\.png", Path(r["path"]).name)
    assert Path(res[0]["path"]).name == "frame_t000001.000_f00025.png"
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(Path(r["path"]).name for r in res)


def test_extract_is_frame_accurate_red_square(media_red_square, tmp_path):
    before, after_in, after_out = F.extract(
        media_red_square, times=[3.5, 4.5, 6.5], outdir=tmp_path
    )
    assert not _is_red(_pixel(before["path"], *RED_PT))
    assert _is_red(_pixel(after_in["path"], *RED_PT))
    assert not _is_red(_pixel(after_out["path"], *RED_PT))


def test_roundtrip_on_vfr_offset(media_vfr_offset, tmp_path):
    info = P.probe(media_vfr_offset)
    assert info.origin > 1.0  # the offset file really is offset
    req = [0.0, 0.777, 2.44, 5.0, 8.123]
    res = F.extract(media_vfr_offset, times=req, outdir=tmp_path)
    assert len(res) == len(req)
    for r in res:
        assert P.to_frame_index(info, r["t"]) == r["frame_index"]
        assert P.to_seconds(info, r["frame_index"]) == pytest.approx(r["t"], abs=1e-6)


def test_vfr_offset_pixels_match_the_indexed_frame(media_vfr_offset, tmp_path):
    """The PNG really is the frame the returned index names (not a neighbour)."""
    res = F.extract(media_vfr_offset, times=[0.0, 2.44, 5.0], outdir=tmp_path)
    for r in res:
        ref = tmp_path / f"ref_{r['frame_index']}.png"
        subprocess.run(
            [
                shutil.which("ffmpeg"),
                *("-v", "error", "-i", str(media_vfr_offset)),
                *("-vf", f"select=eq(n\\,{r['frame_index']})", "-vsync", "0"),
                *("-frames:v", "1", str(ref)),
            ],
            check=True,
        )
        assert _sha(ref) == _sha(r["path"]), r


def test_every_and_count(media_mp4, tmp_path):
    res = F.extract(media_mp4, every=2.5, outdir=tmp_path)
    assert [r["t"] for r in res] == pytest.approx(
        [0.0, 2.48, 5.0, 7.48], abs=1e-6
    )  # snapped to the frame at t
    assert len(list(tmp_path.glob("*.png"))) == 4


def test_scene_mode_finds_red_square_edges(media_red_square, tmp_path):
    res = F.extract(media_red_square, scene=0.03, outdir=tmp_path)
    ts = [r["t"] for r in res]
    assert any(abs(t - 4.0) < 0.1 for t in ts) and any(abs(t - 6.04) < 0.1 for t in ts), ts
    info = P.probe(media_red_square)
    for r in res:
        assert P.to_frame_index(info, r["t"]) == r["frame_index"]
        assert Path(r["path"]).is_file()


def test_scene_mode_offset_file_is_normalized(media_vfr_offset, tmp_path):
    info = P.probe(media_vfr_offset)
    res = F.extract(media_vfr_offset, scene=0.0, outdir=tmp_path, max_frames=1000)
    assert res and res[0]["t"] < 1.0  # normalized, not raw (~1.5+)
    for r in res[:5]:
        assert P.to_frame_index(info, r["t"]) == r["frame_index"]


@pytest.mark.parametrize(
    "t", [-1.0, 10.5, 1e9, float("nan")], ids=["neg", "past_end", "huge", "nan"]
)
def test_times_out_of_range(media_mp4, tmp_path, t):
    with pytest.raises(MediaInputError) as ei:
        F.extract(media_mp4, times=[1.0, t], outdir=tmp_path)
    assert ei.value.kind == INPUT_TIMESTAMP_OUT_OF_RANGE
    assert list(tmp_path.iterdir()) == []


def test_exactly_one_mode(media_mp4, tmp_path):
    for kw in ({}, {"times": [1.0], "every": 1.0}, {"every": 1.0, "scene": 0.3}):
        with pytest.raises(MediaInputError) as ei:
            F.extract(media_mp4, outdir=tmp_path, **kw)
        assert ei.value.kind == F.INPUT_BAD_FRAME_REQUEST
    for kw in ({"every": 0}, {"every": -1}, {"scene": 1.5}, {"scene": -0.1}, {"times": []}):
        with pytest.raises(MediaInputError) as ei:
            F.extract(media_mp4, outdir=tmp_path, **kw)
        assert ei.value.kind == F.INPUT_BAD_FRAME_REQUEST


def test_max_frames_cap(media_mp4, tmp_path):
    with pytest.raises(MediaInputError) as ei:
        F.extract(media_mp4, every=1.0, outdir=tmp_path, max_frames=3)
    assert ei.value.kind == F.INPUT_TOO_MANY_FRAMES
    assert list(tmp_path.iterdir()) == []
    with pytest.raises(MediaInputError) as ei:
        F.extract(media_mp4, times=[1, 2, 3, 4], outdir=tmp_path, max_frames=3)
    assert ei.value.kind == F.INPUT_TOO_MANY_FRAMES


def test_outdir_missing(media_mp4, tmp_path):
    with pytest.raises(MediaInputError) as ei:
        F.extract(media_mp4, times=[1.0], outdir=tmp_path / "nope")
    assert ei.value.kind == F.INPUT_OUTPUT_DIR_MISSING


def test_refuses_overwrite_unless_asked(media_mp4, tmp_path):
    (r,) = F.extract(media_mp4, times=[1.0], outdir=tmp_path)
    p = Path(r["path"])
    p.write_bytes(b"precious")
    with pytest.raises(MediaInputError) as ei:
        F.extract(media_mp4, times=[1.0, 2.0], outdir=tmp_path)
    assert ei.value.kind == F.INPUT_OUTPUT_EXISTS
    assert p.read_bytes() == b"precious"
    assert len(list(tmp_path.iterdir())) == 1  # nothing else was written
    F.extract(media_mp4, times=[1.0], outdir=tmp_path, overwrite=True)
    assert p.read_bytes()[:4] == b"\x89PNG"


def test_failure_leaves_no_partial_or_temp_files(media_mp4, tmp_path, monkeypatch):
    calls = {"n": 0}
    real = F._tools.run

    def flaky(tool, args, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise MediaEnvError("env.ffmpeg_failed", "boom")
        return real(tool, args, **kw)

    monkeypatch.setattr(F._tools, "run", flaky)
    with pytest.raises(MediaEnvError):
        F.extract(media_mp4, times=[1.0, 2.0, 3.0], outdir=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_contact_sheet(media_mp4, tmp_path):
    res = F.extract(media_mp4, every=1.0, outdir=tmp_path / "f", outdir_create=True)
    assert len(res) == 10
    dst = tmp_path / "sheet.png"
    out = F.contact_sheet(res, dst, cols=4, width=100)
    assert out == str(dst)
    assert _size(dst) == (
        400,
        76 * 3,
    )  # 4 cols x ceil(10/4)=3 rows; 100x75 tiles, height rounded even by scale=-2
    assert [p.name for p in tmp_path.iterdir() if p.is_file()] == ["sheet.png"]


def test_contact_sheet_accepts_paths_and_few_frames(media_mp4, tmp_path):
    res = F.extract(media_mp4, times=[1.0, 2.0], outdir=tmp_path / "f", outdir_create=True)
    dst = tmp_path / "s.png"
    F.contact_sheet([r["path"] for r in res], dst, cols=4, width=80)
    assert _size(dst) == (160, 60)  # cols clamped to the frame count


def test_contact_sheet_guards(media_mp4, tmp_path):
    res = F.extract(media_mp4, times=[1.0], outdir=tmp_path / "f", outdir_create=True)
    dst = tmp_path / "s.png"
    dst.write_bytes(b"x")
    with pytest.raises(MediaInputError) as ei:
        F.contact_sheet(res, dst)
    assert ei.value.kind == F.INPUT_OUTPUT_EXISTS
    F.contact_sheet(res, dst, overwrite=True)
    assert dst.read_bytes()[:4] == b"\x89PNG"
    with pytest.raises(MediaInputError) as ei:
        F.contact_sheet([], tmp_path / "e.png")
    assert ei.value.kind == F.INPUT_BAD_FRAME_REQUEST


def test_works_with_networking_disabled(media_mp4, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network used")

    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    res = F.extract(media_mp4, times=[1.0, 2.0], outdir=tmp_path)
    F.contact_sheet(res, tmp_path / "sheet.png")
    assert (tmp_path / "sheet.png").is_file()


def test_frames_never_imports_senses():
    src = Path(F.__file__).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""] + [f"{node.module}.{a.name}" for a in node.names]
        assert not any("senses" in n for n in names), names
    code = (
        "import sys, media_cli.media.frames;"
        "sys.exit(1 if 'media_cli.media.senses' in sys.modules else 0)"
    )
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0


def test_all_ffmpeg_goes_through_tools_seam():
    src = Path(F.__file__).read_text(encoding="utf-8")
    assert "subprocess" not in src
    assert os.path.basename(F.__file__) == "frames.py"
