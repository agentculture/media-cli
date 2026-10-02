"""End-to-end acceptance suite (task t25): the installed ``media`` command, as a user runs it.

Everything goes through ``subprocess`` against ``uv run media`` (the installed console
script, not an in-process call) with a private ``XDG_RUNTIME_DIR`` / ``XDG_STATE_HOME`` /
``XDG_CACHE_HOME``.  A session finalizer kills any daemon the suite started, runs
``teken cli doctor . --strict`` and then asserts nothing is left: no daemon process whose
environment points at our tmp dirs and no socket under the tmp runtime dir.

Submit latency (< 1 s, no daemon running beforehand) is asserted both through ``uv run media``
and through the venv console script directly, so ``uv`` overhead is included in the bound.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from tests.test_daemon_client import Env, _kill_all, daemons_for

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "media_cli"
VENV_MEDIA = Path(sys.executable).parent / "media"
RUN_TIMEOUT = 120
SUBMIT_BUDGET_S = 1.0


# ------------------------------------------------------------------ harness


@pytest.fixture(scope="session")
def e2e():
    base = Path(tempfile.mkdtemp(prefix="e2e"))  # short: AF_UNIX path limit
    env = Env(base)
    env.work = base / "work"
    env.work.mkdir()
    try:
        yield env
    finally:
        _kill_all(env)
        doctor = subprocess.run(
            ["uv", "run", "teken", "cli", "doctor", ".", "--strict"],
            cwd=ROOT,
            env=_env(env),
            capture_output=True,
            text=True,
            timeout=RUN_TIMEOUT,
        )
        leftover = daemons_for(str(base))
        sockets = [str(p) for p in base.rglob("*.sock")] if base.exists() else []
        _kill_all(env)  # belt and braces before removing the tree
        shutil.rmtree(base, ignore_errors=True)
        assert doctor.returncode == 0, doctor.stdout[-2000:] + doctor.stderr[-2000:]
        assert leftover == [], f"media daemon processes survived: {leftover}"
        assert sockets == [], f"media sockets survived: {sockets}"


def _env(e: Env) -> dict:
    out = dict(os.environ)
    out["XDG_RUNTIME_DIR"] = str(e.runtime)
    out["XDG_STATE_HOME"] = str(e.state)
    out["XDG_CACHE_HOME"] = str(e.cache)
    return out


def media(e, *args, cmd=("uv", "run", "media"), expect=None):
    p = subprocess.run(
        [*cmd, *args],
        cwd=ROOT,
        env=_env(e),
        capture_output=True,
        text=True,
        timeout=RUN_TIMEOUT,
    )
    if expect is not None:
        assert p.returncode == expect, (p.returncode, p.stdout, p.stderr)
    return p


def mj(e, *args, expect=0, **kw):
    p = media(e, *args, "--json", expect=expect, **kw)
    assert p.stderr == "", p.stderr
    return json.loads(p.stdout)  # one single JSON document


def write_list(e, name, doc) -> str:
    p = e.work / f"{name}.json"
    p.write_text(json.dumps(doc))
    return str(p)


def wait_result(e, job_id, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        doc = mj(e, "job", "result", job_id)
        if doc["ready"]:
            return doc
        time.sleep(0.2)
    raise AssertionError(f"job {job_id} not ready within {timeout}s")


def run_edit(e, name, doc):
    """plan (dry run) then apply; returns (plan, result, wall seconds of the submit)."""
    el = write_list(e, name, doc)
    plan = mj(e, "edit", "plan", el)
    t0 = time.monotonic()
    sub = mj(e, "edit", "apply", el, "--apply")
    wall = time.monotonic() - t0
    res = wait_result(e, sub["job_id"])
    assert res.get("error") in (None, ""), res
    return plan, res, wall


def ffprobe(path) -> dict:
    p = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return json.loads(p.stdout)


def streams(info, kind):
    return [s for s in info["streams"] if s["codec_type"] == kind]


def audio_md5(path) -> str:
    p = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a", "-c", "copy", "-f", "data", "-"],
        capture_output=True,
        timeout=60,
        check=True,
    )
    return hashlib.md5(p.stdout).hexdigest()


def region_mean(path, t, box) -> float:
    x, y, w, h = box
    p = subprocess.run(
        [
            *("ffmpeg", "-v", "error", "-ss", str(t), "-i", str(path)),
            *("-frames:v", "1", "-vf", f"crop={w}:{h}:{x}:{y},format=gray"),
            *("-f", "rawvideo", "-"),
        ],
        capture_output=True,
        timeout=60,
        check=True,
    )
    assert len(p.stdout) == w * h
    return sum(p.stdout) / len(p.stdout)


# ------------------------------------------------------------------ criterion 1


def test_probe_duration_within_100ms(e2e, media_mp4):
    doc = mj(e2e, "probe", str(media_mp4))
    assert abs(doc["duration"] - 10.0) <= 0.1, doc["duration"]


def test_cut_2_to_5_is_3_seconds(e2e, media_mp4):
    out = e2e.work / "cut.mp4"
    plan, res, _ = run_edit(
        e2e,
        "cut",
        {"input": str(media_mp4), "output": str(out), "segments": [{"start": 2.0, "end": 5.0}]},
    )
    assert plan["dry_run"] is True and not out.exists() or res["ready"]
    info = ffprobe(out)
    dur = float(info["format"]["duration"])
    assert abs(dur - 3.0) <= 0.1, dur
    assert abs(plan["expected_duration"] - 3.0) <= 0.1


RED_BOX = (100, 60, 80, 80)


def test_black_box_mean_below_5(e2e, media_red_square):
    x, y, w, h = RED_BOX
    out = e2e.work / "boxed.mp4"
    region = {"x": x, "y": y, "w": w, "h": h}
    run_edit(
        e2e,
        "boxed",
        {
            "input": str(media_red_square),
            "output": str(out),
            "segments": [
                {
                    "start": 3.0,
                    "end": 7.0,
                    "ops": [{"op": "box", "regions": [region], "fill": "black"}],
                }
            ],
        },
    )
    # output t=2 is source t=5, in the middle of the red square window (4-6 s)
    assert region_mean(media_red_square, 5, RED_BOX) > 40  # control: source really is red
    mean = region_mean(out, 2, RED_BOX)
    assert mean < 5, mean


def test_mkv_cut_and_redact_stays_mkv_opus_reencoded(e2e, media_mkv_vp8_opus):
    """d3: a cut re-encodes audio (opus, same container); copy only for untouched audio."""
    out = e2e.work / "cut_redact.mkv"
    region = {"x": 10, "y": 10, "w": 60, "h": 60}
    doc = {
        "input": str(media_mkv_vp8_opus),
        "output": str(out),
        "segments": [
            {"start": 2.0, "end": 5.0, "ops": [{"op": "box", "regions": [region], "fill": "black"}]}
        ],
    }
    plan, _, _ = run_edit(e2e, "mkv_cut", doc)
    actions = {s["type"]: s["action"] for s in plan["output"]["streams"]}
    assert actions["audio"] == "encode"
    assert actions["video"] == "encode"
    info = ffprobe(out)
    assert "matroska" in info["format"]["format_name"]
    assert [s["codec_name"] for s in streams(info, "video")] == ["vp8"]
    assert [s["codec_name"] for s in streams(info, "audio")] == ["opus"]
    assert abs(float(info["format"]["duration"]) - 3.0) <= 0.1
    assert plan["output"]["container_fallback"] in (None, False, "")


@pytest.mark.parametrize("end_of", ["editable_end", "ten_seconds"])
def test_mkv_whole_file_box_copies_audio_byte_identical(e2e, media_mkv_vp8_opus, end_of):
    out = e2e.work / f"whole_{end_of}.mkv"
    # d12: probe's JSON exposes the editable range; an end at (or within one frame of)
    # editable.end is the whole file, so frame-only edits stay passthrough with copied audio.
    editable = mj(e2e, "probe", str(media_mkv_vp8_opus))["editable"]
    assert editable["start"] == 0.0
    end = editable["end"] if end_of == "editable_end" else 10.0
    assert abs(end - editable["end"]) <= editable["frame_interval"]
    region = {"x": 10, "y": 10, "w": 60, "h": 60}
    doc = {
        "input": str(media_mkv_vp8_opus),
        "output": str(out),
        "segments": [
            {
                "start": 0,
                "end": end,
                "ops": [{"op": "box", "regions": [region], "fill": "black"}],
            }
        ],
    }
    plan, _, _ = run_edit(e2e, f"mkv_whole_{end_of}", doc)
    assert plan["mode"] == "passthrough", plan.get("mode")
    actions = {s["type"]: s["action"] for s in plan["output"]["streams"]}
    assert actions["audio"] == "copy", plan["output"]["streams"]
    assert actions["video"] == "encode"
    info = ffprobe(out)
    assert "matroska" in info["format"]["format_name"]
    assert [s["codec_name"] for s in streams(info, "audio")] == ["opus"]
    assert audio_md5(out) == audio_md5(media_mkv_vp8_opus)


# ------------------------------------------------------------------ criterion 2


@pytest.mark.parametrize("launcher", ["uv run media", "venv media"])
def test_submit_with_no_daemon_returns_job_id_fast(media_mp4, launcher):
    """Cold submit (no daemon, no sockdir) returns a job id in < 1 s -- asserted for BOTH
    launchers, so the figure holds including ``uv run`` overhead (measured ~0.2 s)."""
    cmd = ("uv", "run", "media") if launcher == "uv run media" else (str(VENV_MEDIA),)
    base = Path(tempfile.mkdtemp(prefix="e2f"))
    fresh = Env(base)
    try:
        assert fresh.daemons() == []
        assert not fresh.sockdir.exists()
        el = base / "e.json"
        el.write_text(
            json.dumps(
                {
                    "input": str(media_mp4),
                    "output": str(base / "o.mp4"),
                    "segments": [{"start": 0, "end": 1}],
                }
            )
        )
        t0 = time.monotonic()
        p = media(fresh, "edit", "apply", str(el), "--apply", "--json", cmd=cmd)
        wall = time.monotonic() - t0
        print(f"\nSUBMIT WALL ({launcher}, no daemon beforehand): {wall:.3f}s")
        assert p.returncode == 0, (p.stdout, p.stderr)
        assert p.stderr == "", (p.stdout, p.stderr)
        job_id = json.loads(p.stdout)["job_id"]
        assert job_id, "submit should have spawned the daemon"
        assert fresh.daemons(), "submit should have spawned the daemon"
        assert wall < SUBMIT_BUDGET_S, wall
        for verb in ("status", "result"):  # each is a single JSON document
            q = media(fresh, "job", verb, job_id, "--json", cmd=cmd, expect=0)
            assert q.stderr == ""
            assert isinstance(json.loads(q.stdout), dict)
    finally:
        _kill_all(fresh)
        shutil.rmtree(base, ignore_errors=True)


def test_job_status_and_result_are_single_json_documents(e2e, media_mp4):
    out = e2e.work / "single.mp4"
    el = write_list(
        e2e,
        "single",
        {"input": str(media_mp4), "output": str(out), "segments": [{"start": 0, "end": 1}]},
    )
    job_id = mj(e2e, "edit", "apply", el, "--apply")["job_id"]
    status = mj(e2e, "job", "status", job_id)
    assert status["job"]["id"] == job_id or job_id in json.dumps(status)
    res = wait_result(e2e, job_id)
    assert res["ready"] is True
    assert res["output"].endswith("single.mp4")


def _assert_machine_error(p, code):
    assert p.returncode == 1, (p.returncode, p.stdout, p.stderr)
    assert p.stdout == ""
    assert "Traceback" not in p.stderr
    err = json.loads(p.stderr)  # exactly one JSON object on stderr
    assert err["code"] == 1 or err.get("kind") == code
    assert err["kind"] == code, err
    return err["kind"]


def test_distinct_machine_codes_for_corrupt_range_and_offframe(e2e, media_mp4):
    corrupt = e2e.work / "corrupt.mp4"
    # Seeded, not os.urandom: ~1% of random 4 KiB blobs are sniffed by ffprobe as a
    # stream-less format (e.g. "lrc") and probe successfully, making this flaky.
    rng = random.Random(0)
    corrupt.write_bytes(bytes(rng.randrange(256) for _ in range(4096)))
    kinds = []

    p = media(e2e, "probe", str(corrupt), "--json")
    kinds.append(_assert_machine_error(p, "input.unreadable"))
    el = write_list(
        e2e,
        "bad_corrupt",
        {
            "input": str(corrupt),
            "output": str(e2e.work / "x1.mp4"),
            "segments": [{"start": 0, "end": 1}],
        },
    )
    _assert_machine_error(media(e2e, "edit", "plan", el, "--json"), "input.unreadable")

    el = write_list(
        e2e,
        "bad_time",
        {
            "input": str(media_mp4),
            "output": str(e2e.work / "x2.mp4"),
            "segments": [{"start": 0, "end": 999}],
        },
    )
    kinds.append(
        _assert_machine_error(
            media(e2e, "edit", "plan", el, "--json"), "input.timestamp_out_of_range"
        )
    )

    box = {"op": "box", "regions": [{"x": 5000, "y": 5000, "w": 10, "h": 10}], "fill": "black"}
    el = write_list(
        e2e,
        "bad_region",
        {
            "input": str(media_mp4),
            "output": str(e2e.work / "x3.mp4"),
            "segments": [{"start": 0, "end": 1, "ops": [box]}],
        },
    )
    kinds.append(
        _assert_machine_error(
            media(e2e, "edit", "plan", el, "--json"), "input.region_outside_frame"
        )
    )
    assert len(set(kinds)) == 3, kinds
    assert not (e2e.work / "x1.mp4").exists()
    assert not (e2e.work / "x3.mp4").exists()


# ------------------------------------------------------------------ criterion 3


def _source_files():
    return sorted(PKG.rglob("*.py"))


def _code_strings(path):
    """String constants in code, excluding docstrings (comments never reach the AST)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docs.add(id(body[0].value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            yield node.value


DEVICE_PATTERNS = re.compile(
    r"/dev/(video|snd|media|vbi)|arecord|v4l2|alsa|pw-record|\bx11grab\b", re.I
)


def test_static_no_device_capture_in_executable_code():
    files = _source_files()
    assert len(files) > 20
    hits = [
        (str(f.relative_to(ROOT)), s)
        for f in files
        for s in _code_strings(f)
        if DEVICE_PATTERNS.search(s)
    ]
    assert hits == [], hits


def test_static_no_synthetic_input_sources_in_code():
    """No verb synthesizes content: no lavfi/anullsrc/color-source strings in executable code."""
    pat = re.compile(r"lavfi|anullsrc|testsrc|\bcolor=|\bnullsrc\b|\bsine=", re.I)
    hits = [
        (str(f.relative_to(ROOT)), s)
        for f in _source_files()
        for s in _code_strings(f)
        if pat.search(s)
    ]
    assert hits == [], hits


@pytest.mark.parametrize("pseudo", ["lavfi:testsrc", "-f", "color=c=red:size=64x64", "/dev/video0"])
def test_pseudo_inputs_are_refused(e2e, pseudo):
    el = write_list(
        e2e,
        "pseudo",
        {"input": pseudo, "output": str(e2e.work / "p.mp4"), "segments": [{"start": 0, "end": 1}]},
    )
    p = media(e2e, "edit", "plan", el, "--json")
    assert p.returncode == 1
    assert p.stdout == ""
    assert "Traceback" not in p.stderr
    assert json.loads(p.stderr)["kind"].startswith("input.")
    assert not (e2e.work / "p.mp4").exists()


def test_compiled_args_have_exactly_one_file_input(e2e, media_mp4, media_with_extras):
    for n, src in enumerate((media_mp4, media_with_extras)):
        el = write_list(
            e2e,
            f"args{n}",
            {
                "input": str(src),
                "output": str(e2e.work / f"a{n}.mp4"),
                "segments": [{"start": 1, "end": 3}, {"start": 5, "end": 7}],
                "transitions": [{"type": "xfade", "style": "fade", "duration": 0.5}],
            },
        )
        args = mj(e2e, "edit", "plan", el)["args"]
        idx = [i for i, a in enumerate(args) if a == "-i"]
        assert len(idx) == 1, args
        value = args[idx[0] + 1]
        assert value == "file:" + str(src.resolve())
        assert Path(value[5:]).is_file()
        assert "lavfi" not in args


def test_no_daemon_or_socket_after_read_only_verbs(e2e, media_mp4):
    """Read-only verbs never start a daemon (the final leftover check covers the session)."""
    before = daemons_for(str(e2e.base))
    mj(e2e, "probe", str(media_mp4))
    media(e2e, "job", "list", "--json", expect=0)
    assert daemons_for(str(e2e.base)) == before
