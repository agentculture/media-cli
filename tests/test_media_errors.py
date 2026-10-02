"""Typed media errors ride the CliError contract (exit 1 input / exit 2 env)."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from media_cli.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError
from media_cli.media import errors as E
from media_cli.media.errors import MediaEnvError, MediaInputError, ffmpeg_failure

INPUT_KINDS = [
    E.INPUT_UNREADABLE,
    E.INPUT_TIMESTAMP_OUT_OF_RANGE,
    E.INPUT_REGION_OUTSIDE_FRAME,
]
ENV_KINDS = [
    E.ENV_FFMPEG_MISSING,
    E.ENV_FILTER_UNAVAILABLE,
    E.ENV_SENSE_UNAVAILABLE,
    E.ENV_SENSE_NOT_LOCAL,
]


def test_kind_strings_are_stable():
    assert INPUT_KINDS == [
        "input.unreadable",
        "input.timestamp_out_of_range",
        "input.region_outside_frame",
    ]
    assert ENV_KINDS == [
        "env.ffmpeg_missing",
        "env.filter_unavailable",
        "env.sense_unavailable",
        "env.sense_not_local",
    ]


@pytest.mark.parametrize("kind", INPUT_KINDS)
def test_input_error_exit_1(kind):
    err = MediaInputError(kind, "bad", "fix it")
    assert isinstance(err, CliError)
    assert err.code == EXIT_USER_ERROR == 1
    assert err.kind == kind
    assert err.to_dict() == {
        "code": 1,
        "message": "bad",
        "remediation": "fix it",
        "kind": kind,
    }


@pytest.mark.parametrize("kind", ENV_KINDS)
def test_env_error_exit_2(kind):
    err = MediaEnvError(kind, "missing")
    assert isinstance(err, CliError)
    assert err.code == EXIT_ENV_ERROR == 2
    assert err.to_dict()["kind"] == kind
    assert err.remediation == ""


def test_wrong_namespace_rejected():
    with pytest.raises(ValueError):
        MediaInputError(E.ENV_FFMPEG_MISSING, "x")
    with pytest.raises(ValueError):
        MediaEnvError(E.INPUT_UNREADABLE, "x")


def test_plain_clierror_dict_unchanged():
    assert CliError(1, "m", "r").to_dict() == {"code": 1, "message": "m", "remediation": "r"}


def test_ffmpeg_failure_tails_stderr_no_traceback():
    stderr = "\n".join(f"line {i}" for i in range(100))
    err = ffmpeg_failure(stderr)
    assert isinstance(err, MediaEnvError)
    assert err.kind == E.ENV_FFMPEG_FAILED
    lines = err.remediation.splitlines()
    assert lines[-1] == "line 99"
    assert "line 80" in lines
    assert "line 79" not in lines
    assert "Traceback" not in err.remediation


def test_ffmpeg_failure_empty_stderr():
    err = ffmpeg_failure("")
    assert err.code == 2
    assert err.kind == E.ENV_FFMPEG_FAILED


def test_json_error_output_includes_kind(tmp_path):
    code = (
        "import sys;from media_cli.cli import _dispatch\n"
        "from media_cli.cli._output import emit_error\n"
        "from media_cli.media.errors import MediaInputError\n"
        "emit_error(MediaInputError('input.unreadable','m','r'), json_mode=True)\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stderr)
    assert payload["kind"] == "input.unreadable"
    assert {"code", "message", "remediation"} <= payload.keys()
