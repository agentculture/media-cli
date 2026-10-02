"""Tests for the ffmpeg tool layer (media_cli.media._tools)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

from media_cli.media import _tools
from media_cli.media.errors import (
    ENV_FFMPEG_FAILED,
    ENV_FFMPEG_MISSING,
    ENV_FILTER_UNAVAILABLE,
    MediaEnvError,
)

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)


@pytest.fixture(autouse=True)
def _fresh_cache():
    _tools.reset_cache()
    yield
    _tools.reset_cache()


@pytest.fixture
def empty_path(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    _tools.reset_cache()


def test_missing_binary_raises_env_error_naming_binary(empty_path):
    for name in ("ffmpeg", "ffprobe"):
        with pytest.raises(MediaEnvError) as ei:
            _tools.run(name, ["-version"])
        err = ei.value
        assert err.kind == ENV_FFMPEG_MISSING
        assert err.code == 2
        assert name in err.remediation
        assert "apt-get install ffmpeg" in err.remediation


def test_missing_binary_spawn_and_resolvers(empty_path):
    with pytest.raises(MediaEnvError) as ei:
        _tools.spawn("ffmpeg", ["-version"])
    assert ei.value.kind == ENV_FFMPEG_MISSING
    with pytest.raises(MediaEnvError):
        _tools.ffprobe()
    with pytest.raises(MediaEnvError):
        _tools.has_filter("boxblur")


def test_unknown_tool_rejected():
    with pytest.raises(ValueError):
        _tools.run("rm", ["-rf", "/"])


@needs_ffmpeg
def test_resolves_absolute_paths_and_caches(monkeypatch):
    p = _tools.ffmpeg()
    assert os.path.isabs(p)
    assert os.path.isabs(_tools.ffprobe())
    monkeypatch.setenv("PATH", "")  # cache must survive PATH change
    assert _tools.ffmpeg() == p
    _tools.reset_cache()
    with pytest.raises(MediaEnvError):
        _tools.ffmpeg()


@needs_ffmpeg
@pytest.mark.parametrize("name", ["boxblur", "drawbox", "xfade"])
def test_has_filter_true(name):
    assert _tools.has_filter(name) is True
    _tools.require_filter(name)


@needs_ffmpeg
def test_has_filter_false_and_require_raises():
    assert _tools.has_filter("definitely_not_a_filter") is False
    with pytest.raises(MediaEnvError) as ei:
        _tools.require_filter("definitely_not_a_filter")
    assert ei.value.kind == ENV_FILTER_UNAVAILABLE
    assert "definitely_not_a_filter" in ei.value.message


@needs_ffmpeg
def test_has_encoder():
    if not _tools.has_encoder("libx264"):
        pytest.skip("host ffmpeg lacks libx264")
    assert _tools.has_encoder("libx264") is True
    assert _tools.has_encoder("definitely_not_an_encoder") is False
    with pytest.raises(MediaEnvError) as ei:
        _tools.require_encoder("definitely_not_an_encoder")
    assert ei.value.kind == ENV_FILTER_UNAVAILABLE


@needs_ffmpeg
def test_run_success_and_failure():
    cp = _tools.run("ffmpeg", ["-hide_banner", "-version"])
    assert cp.returncode == 0
    assert "ffmpeg version" in cp.stdout
    with pytest.raises(MediaEnvError) as ei:
        _tools.run("ffprobe", ["-v", "error", "/nonexistent/file.mp4"])
    assert ei.value.kind == ENV_FFMPEG_FAILED
    assert "nonexistent" in ei.value.remediation
    cp = _tools.run("ffprobe", ["-v", "error", "/nonexistent/file.mp4"], check=False)
    assert cp.returncode != 0


@needs_ffmpeg
def test_run_timeout_is_typed():
    with pytest.raises(MediaEnvError) as ei:
        _tools.run(
            "ffmpeg",
            ["-f", "lavfi", "-i", "testsrc=d=100000", "-f", "null", "-"],
            timeout=0.3,
        )
    assert ei.value.kind == ENV_FFMPEG_FAILED
    assert "timed out" in ei.value.message


@needs_ffmpeg
def test_spawn_returns_popen():
    proc = _tools.spawn("ffmpeg", ["-hide_banner", "-version"], stdout=subprocess.PIPE, text=True)
    out, _ = proc.communicate()
    assert proc.returncode == 0
    assert "ffmpeg version" in out


def test_run_uses_absolute_list_argv_no_shell(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"], seen["kw"] = argv, kw
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(_tools, "_which", lambda n: f"/opt/bin/{n}")
    monkeypatch.setattr(_tools.subprocess, "run", fake_run)
    _tools.run("ffprobe", ["-i", "x y;rm"])
    assert seen["argv"] == ["/opt/bin/ffprobe", "-i", "x y;rm"]
    assert isinstance(seen["argv"], list)
    assert not seen["kw"].get("shell")


def test_no_shell_true_and_stdlib_only():
    src = open(_tools.__file__, encoding="utf-8").read()
    assert "shell=True" not in src
    import ast

    tops = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            tops |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            tops.add((node.module or "").split(".")[0])
    allowed = set(sys.stdlib_module_names) | {"media_cli"}
    assert tops <= allowed, tops - allowed
