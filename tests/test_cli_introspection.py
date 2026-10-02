"""Tests for the introspection verbs: overview, cli overview, doctor."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from media_cli.cli import main
from media_cli.explain import known_paths
from tests.test_daemon_client import daemons_for

# --- overview -------------------------------------------------------------


def test_overview_text(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["overview"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "# media-cli" in out
    assert "Identity" in out


def test_overview_json_shape(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["overview", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["subject"] == "media-cli"
    assert isinstance(payload["sections"], list)
    assert payload["sections"]


def _section(payload: dict, title: str) -> list[str]:
    return next(s["items"] for s in payload["sections"] if s["title"] == title)


def test_overview_lists_every_new_verb(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["overview", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    verbs = " ".join(_section(payload, "Verbs"))
    for verb in ("probe", "frames", "edit", "search", "job"):
        assert f"{verb} " in verbs, verb
    text = json.dumps(payload)
    assert "template" not in text
    assert "device plane" in json.dumps(_section(payload, "Lane"))


def test_cli_overview_lists_new_verbs_and_dry_run_rule(capsys) -> None:
    rc = main(["cli", "overview", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    verbs = " ".join(_section(payload, "Verbs"))
    for verb in ("probe", "frames", "edit", "search", "job"):
        assert f"{verb} " in verbs, verb
    conventions = " ".join(_section(payload, "Conventions"))
    assert "--apply" in conventions
    assert "daemon" in conventions


def test_overview_graceful_on_bad_path(capsys: pytest.CaptureFixture[str]) -> None:
    # Rubric contract: descriptive verbs never hard-fail on a missing target.
    rc = main(["overview", "/no/such/path/here"])
    assert rc == 0
    assert capsys.readouterr().out.strip()


# --- cli overview ---------------------------------------------------------


def test_cli_overview_text(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["cli", "overview"])
    assert rc == 0
    assert "# media-cli cli" in capsys.readouterr().out


def test_cli_overview_json_shape(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["cli", "overview", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["subject"] == "media-cli cli"
    assert isinstance(payload["sections"], list)


def test_cli_noun_bare_is_non_empty(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["cli"])
    assert rc == 0
    assert capsys.readouterr().out.strip()


def test_cli_overview_unknown_flag_structured_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # `cli overview` parse errors must route through the structured error
    # contract (error:/hint: + exit 1), not argparse's default stderr/exit 2.
    with pytest.raises(SystemExit) as exc:
        main(["cli", "overview", "--bogus"])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err


# --- doctor ---------------------------------------------------------------


def test_doctor_text(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["doctor"])
    assert rc in (0, 1)
    assert "media-cli doctor" in capsys.readouterr().out


def test_doctor_json_shape(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["doctor", "--json"])
    assert rc in (0, 1)
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload["healthy"], bool)
    assert isinstance(payload["checks"], list)
    assert payload["checks"]
    for check in payload["checks"]:
        assert {"id", "passed", "severity", "message", "remediation"} <= set(check)


def test_doctor_recognizes_declared_backend(capsys: pytest.CaptureFixture[str]) -> None:
    """The repo's own declared backend must be a known one — doctor stays healthy.

    Guards the backend-consistency invariant: a promotion that changes
    ``culture.yaml``'s backend without teaching ``doctor`` the matching prompt
    file would otherwise slip through (the shape tests above tolerate rc==1).
    """
    rc = main(["doctor", "--json"])
    payload = json.loads(capsys.readouterr().out)
    messages = " ".join(str(c["message"]) for c in payload["checks"])
    assert "unknown backend" not in messages
    assert rc == 0
    assert payload["healthy"] is True


# --- read-only verbs never spawn the daemon (o15) ------------------------------


@pytest.fixture
def xdg(monkeypatch):
    base = Path(tempfile.mkdtemp(prefix="mdro"))
    dirs = {name: base / name for name in ("run", "state", "cache")}
    dirs["run"].mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(dirs["run"]))
    monkeypatch.setenv("XDG_STATE_HOME", str(dirs["state"]))
    monkeypatch.setenv("XDG_CACHE_HOME", str(dirs["cache"]))
    monkeypatch.setenv("MEDIA_CLI_LOBES_URL", "http://127.0.0.1:9")
    dirs["base"] = base
    try:
        yield dirs
    finally:
        leftover = daemons_for(str(base))
        shutil.rmtree(base, ignore_errors=True)
        assert leftover == [], f"a read-only verb spawned a daemon: {leftover}"


def _run(argv: list[str]) -> int:
    try:
        return main(argv)
    except SystemExit as exc:
        return int(exc.code or 0)


def _assert_no_daemon(dirs) -> None:
    assert not (dirs["run"] / "media-cli").exists(), "socket dir created"
    assert not (dirs["cache"] / "media-cli" / "run").exists(), "fallback socket dir created"
    assert not (dirs["state"] / "media-cli" / "daemon.log").exists(), "daemon log written"
    assert daemons_for(str(dirs["base"])) == []


READ_ONLY = [
    ["--help"],
    ["learn"],
    ["learn", "--json"],
    ["overview", "--json"],
    ["doctor", "--json"],
    ["whoami", "--json"],
    ["cli", "overview"],
    ["edit"],
    ["edit", "overview", "--json"],
    ["edit", "--help"],
    ["edit", "apply", "--help"],
    ["search", "overview", "--json"],
    ["search", "index", "--help"],
    ["job", "overview", "--json"],
    ["job", "status", "no-such-job", "--json"],
    ["job", "result", "no-such-job", "--json"],
    ["job", "list", "--json"],
    ["probe", "--help"],
    ["frames", "--help"],
]


def test_read_only_verbs_never_spawn_the_daemon(xdg, capsys) -> None:
    for argv in READ_ONLY:
        _run(argv)
    for path in known_paths():
        assert _run(["explain", *path, "--json"]) == 0
    capsys.readouterr()
    _assert_no_daemon(xdg)


def test_probe_and_frames_never_spawn_the_daemon(xdg, media_mp4, capsys) -> None:
    out = xdg["base"] / "frames"
    assert _run(["probe", str(media_mp4), "--json"]) == 0
    assert _run(["frames", str(media_mp4), "--at", "1", "--out", str(out), "--json"]) == 0
    capsys.readouterr()
    _assert_no_daemon(xdg)
    assert os.listdir(out)
