"""Model-derived redaction regions (task t17): coverage report + per-frame proof.

The senses model is replaced by a fake client that is told the TRUE box for the
sampled time (read from the extracted frame's file name), so these tests measure
the interpolation / coverage logic, not Gemma's box accuracy (obligation o10).
A real-Gemma accuracy check is deliberately not part of this suite.
"""

from __future__ import annotations

import re

import pytest

from media_cli.media import _tools
from media_cli.media import compile as C
from media_cli.media import editlist as E
from media_cli.media import output as out
from media_cli.media import probe as P
from media_cli.media import regions
from media_cli.media.errors import ENV_SENSE_NOT_LOCAL, MediaEnvError, MediaInputError

pytestmark = pytest.mark.requires_ffmpeg

W, H, FPS, DUR, SIDE = 320, 240, 10, 3, 60


def true_box(t: float) -> dict:
    return {"x": round(20 + 40 * t), "y": 60, "w": SIDE, "h": SIDE}


@pytest.fixture(scope="module")
def moving_square(tmp_path_factory):
    out = tmp_path_factory.mktemp("sq") / "moving.mkv"
    graph = (
        f"color=c=white:s={W}x{H}:r={FPS}:d={DUR}[bg];"
        f"color=c=red:s={SIDE}x{SIDE}:r={FPS}:d={DUR}[sq];"
        "[bg][sq]overlay=x='round(20+40*t)':y=60:eval=frame"
    )
    _tools.run(
        "ffmpeg",
        ["-v", "error", "-nostdin", "-y", "-filter_complex", graph]
        + ["-c:v", "libx264", "-crf", "10", "-pix_fmt", "yuv420p", str(out)],
        timeout=60,
    )
    return out


class FakeClient:
    """Stands in for SensesClient.describe_images; replies keyed by the frame's time."""

    def __init__(self, reply=None):
        self.reply = reply or (lambda t: {"boxes": [true_box(t)]})
        self.calls = []

    def describe_images(self, paths, prompt, *, role="senses", response_format=None):
        assert len(paths) == 1
        assert role == "senses"
        assert response_format is not None
        t = float(re.search(r"frame_t([0-9.]+)_f", str(paths[0])).group(1))
        self.calls.append((t, prompt))
        out = self.reply(t)
        if isinstance(out, Exception):
            raise out
        return out

    def served_model(self, role="senses"):
        return "nvidia/Gemma-4-26B-A4B-NVFP4"


def _redact_and_check(src, result, tmp_path, op="box"):
    """Redact the regions; per frame (t, red px left, mean RGB) of the true box."""
    dst = tmp_path / "out.mkv"
    info = P.probe(src)
    opdoc = {"op": op, "regions": result["regions"]}
    opdoc.update({"fill": "black"} if op == "box" else {"strength": 10})
    el = E.parse(
        {
            "input": str(src),
            "output": str(dst),
            "overwrite": True,
            "segments": [{"start": 0, "end": info.end, "ops": [opdoc]}],
        }
    )
    c = C.compile_editlist(el, info)
    _tools.run("ffmpeg", list(c.args), timeout=120)
    final = out.commit(c.output_plan)
    raw = tmp_path / "o.rgb"
    _tools.run(
        "ffmpeg",
        ["-v", "error", "-nostdin", "-y", "-i", final, "-fps_mode", "passthrough"]
        + ["-pix_fmt", "rgb24", "-f", "rawvideo", str(raw)],
        timeout=120,
    )
    data = raw.read_bytes()
    n = W * H * 3
    frames = [data[i : i + n] for i in range(0, len(data), n)]
    times = P.frame_times(src)
    assert len(frames) == len(times)
    rows = []
    for t, f in zip(times, frames):
        b = true_box(t)
        red = tot = 0
        for y in range(b["y"], b["y"] + b["h"]):
            row = f[(y * W + b["x"]) * 3 : (y * W + b["x"] + b["w"]) * 3]
            tot += sum(row)
            red += sum(1 for i in range(0, len(row), 3) if row[i] > 150 and row[i + 1] < 100)
        rows.append((t, red, tot / (b["w"] * b["h"] * 3)))
    return rows


def _uncovered(t, ranges):
    return any(a - 1e-6 <= t <= b + 1e-6 for a, b in ranges)


@pytest.mark.parametrize("method", ["hold", "linear"])
def test_moving_square_every_frame_covered(moving_square, tmp_path, method):
    client = FakeClient()
    res = regions.find_regions(
        moving_square, "the red square", fps=2.0, method=method, client=client
    )
    cov = res["coverage"]
    assert cov["sample_fps"] == 2.0
    assert cov["method"] == method
    assert cov["frames_without_detection"] == []
    assert cov["rejected_boxes"] == []
    assert cov["samples"] == len(client.calls) >= 6
    assert res["description"] == "the red square"
    assert res["model"]
    for r in res["regions"]:
        assert set(r) == {"x", "y", "w", "h", "start", "end"}
        assert 0 <= r["x"]
        assert r["x"] + r["w"] <= W
        assert r["w"] > 0
        assert r["h"] > 0
    rows = _redact_and_check(moving_square, res, tmp_path)
    assert len(rows) == FPS * DUR
    leaked = [
        (t, n, m)
        for t, n, m in rows
        if (n or m >= 5) and not _uncovered(t, cov["frames_without_detection"])
    ]
    assert leaked == [], f"frames leaking red outside the reported gaps: {leaked}"


def test_hold_region_is_union_of_neighbouring_boxes(moving_square):
    res = regions.find_regions(moving_square, "sq", fps=2.0, method="hold", client=FakeClient())
    first = [r for r in res["regions"] if r["start"] <= 0.0 + 1e-9 and r["end"] >= 0.5 - 1e-9]
    a, b = true_box(0.0), true_box(0.5)
    assert any(
        r["x"] <= a["x"] and r["x"] + r["w"] >= b["x"] + b["w"] and r["y"] <= 60 for r in first
    )


def test_garbage_sample_is_reported_uncovered(moving_square, tmp_path):
    def reply(t):
        return {"nonsense": 1} if abs(t - 1.5) < 1e-6 else {"boxes": [true_box(t)]}

    res = regions.find_regions(moving_square, "sq", fps=2.0, client=FakeClient(reply))
    gaps = res["coverage"]["frames_without_detection"]
    assert gaps
    assert gaps[0][0] <= 1.0 + 1e-6
    assert gaps[-1][1] >= 2.0 - 1e-6
    # nothing is held across the gap
    for r in res["regions"]:
        assert not (r["start"] < 1.4 and r["end"] > 1.6)
    rows = _redact_and_check(moving_square, res, tmp_path)
    for t, n, _m in rows:
        if n:
            assert _uncovered(t, gaps), f"leak at t={t} not listed as uncovered"
    assert any(n for t, n, _m in rows if 1.0 < t < 2.0), "gap frames should actually be uncovered"


def test_clamping_and_rejection_recorded(moving_square):
    def reply(t):
        return {
            "boxes": [
                {"x": -10, "y": 10, "w": 50, "h": 50},  # clamped
                {"x": 10, "y": 10, "w": 0, "h": 5},  # no size
                {"x": 400, "y": 10, "w": 20, "h": 20},  # outside the frame
            ]
        }

    res = regions.find_regions(moving_square, "x", fps=1.0, client=FakeClient(reply))
    rej = res["coverage"]["rejected_boxes"]
    reasons = {r["reason"] for r in rej}
    assert reasons == {"clamped", "non_positive_size", "outside_frame"}
    clamped = next(r for r in rej if r["reason"] == "clamped")
    assert clamped["box"] == {"x": -10, "y": 10, "w": 50, "h": 50}
    assert "t" in clamped
    assert all(r["x"] >= 0 and r["w"] > 0 for r in res["regions"])
    assert res["coverage"]["frames_without_detection"] == []


def test_malformed_box_discards_the_whole_frame(moving_square):
    def reply(t):
        if abs(t - 1.0) < 1e-6:
            return {"boxes": [true_box(t), {"x": "a", "y": 1, "w": 2, "h": 2}]}
        return {"boxes": [true_box(t)]}

    res = regions.find_regions(moving_square, "x", fps=2.0, client=FakeClient(reply))
    assert res["coverage"]["frames_without_detection"]
    assert any(r["reason"] == "malformed" for r in res["coverage"]["rejected_boxes"])


def test_empty_detection_is_not_coverage(moving_square):
    res = regions.find_regions(
        moving_square, "x", fps=2.0, client=FakeClient(lambda t: {"boxes": []})
    )
    assert res["regions"] == []
    assert res["coverage"]["frames_without_detection"] == [[0.0, 2.9]]


def test_multiple_boxes_per_frame(moving_square):
    def reply(t):
        a = true_box(t)
        return {"boxes": [a, {"x": 250, "y": 150, "w": 40, "h": 40}]}

    res = regions.find_regions(moving_square, "x", fps=2.0, client=FakeClient(reply))
    assert any(r["x"] == 250 for r in res["regions"])
    assert any(r["x"] < 100 and r["w"] >= SIDE for r in res["regions"])


def test_start_end_and_margin(moving_square):
    client = FakeClient()
    res = regions.find_regions(
        moving_square, "x", fps=2.0, start=1.0, end=2.0, margin=5, client=client
    )
    assert [t for t, _ in client.calls][0] == 1.0
    assert [t for t, _ in client.calls][-1] == 2.0
    assert all(r["start"] >= 1.0 - 1e-9 and r["end"] <= 2.0 + 1e-9 for r in res["regions"])
    b = true_box(1.0)
    assert any(r["x"] <= b["x"] - 5 and r["y"] <= b["y"] - 5 for r in res["regions"])


def test_bad_arguments(moving_square):
    client = FakeClient()
    with pytest.raises(MediaInputError):
        regions.find_regions(moving_square, "x", fps=0, client=client)
    with pytest.raises(MediaInputError):
        regions.find_regions(moving_square, "x", method="cubic", client=client)
    with pytest.raises(MediaInputError):
        regions.find_regions(moving_square, "  ", client=client)


def test_not_local_error_propagates(moving_square):
    err = MediaEnvError(ENV_SENSE_NOT_LOCAL, "remote", "")
    client = FakeClient(lambda t: err)
    with pytest.raises(MediaEnvError) as ei:
        regions.find_regions(moving_square, "x", client=client)
    assert ei.value.kind == ENV_SENSE_NOT_LOCAL


def test_temp_frames_removed(moving_square, tmp_path):
    wd = tmp_path / "wd"
    wd.mkdir()
    regions.find_regions(moving_square, "x", fps=1.0, client=FakeClient(), workdir=wd)
    assert list(wd.iterdir()) == []
