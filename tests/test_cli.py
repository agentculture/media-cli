"""Smoke tests for the ``media`` CLI entry point (dist ``media-cli``) and its verbs."""

from __future__ import annotations

import argparse
import json
import re

import pytest

from media_cli import __version__
from media_cli.cli import _build_parser, main
from media_cli.explain import known_paths
from media_cli.explain.catalog import ENTRIES

#: a user-facing command example that names the dist instead of the installed command
_VERB_WORDS = "whoami|learn|explain|overview|doctor|cli|probe|frames|edit|search|job|--help"
_STALE_CMD = re.compile(r"(?<![\"'])\bmedia-cli\s+(" + _VERB_WORDS + r")\b")

NEW_PATHS = [
    ("probe",),
    ("frames",),
    ("edit",),
    ("edit", "plan"),
    ("edit", "apply"),
    ("edit", "regions"),
    ("edit", "overview"),
    ("search",),
    ("search", "index"),
    ("search", "query"),
    ("search", "purge"),
    ("search", "cache"),
    ("search", "overview"),
    ("job",),
    ("job", "status"),
    ("job", "result"),
    ("job", "cancel"),
    ("job", "list"),
    ("job", "overview"),
]


def _registered_paths() -> list[tuple[str, ...]]:
    """Every command path the real parser registers (nouns and their verbs)."""
    out: list[tuple[str, ...]] = []

    def walk(parser: argparse.ArgumentParser, prefix: tuple[str, ...]) -> None:
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, child in action.choices.items():
                    out.append((*prefix, name))
                    walk(child, (*prefix, name))

    walk(_build_parser(), ())
    return out


def _leaf_paths() -> set[tuple[str, ...]]:
    """Registered paths with no sub-verbs below them (what the learn map lists)."""
    paths = _registered_paths()
    return {p for p in paths if not any(len(q) > len(p) and q[: len(p)] == p for q in paths)}


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert __version__ in out
    assert out.startswith("media ")


def test_no_args_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "usage: media " in out
    assert "media-cli " not in out.split("\n", 1)[0]
    assert "template" not in out


def test_unknown_command_errors(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["bogus"])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err
    assert "'media --help'" in err


@pytest.mark.parametrize(
    "argv",
    [
        ["bogus"],
        ["probe"],
        ["frames", "x.mp4"],
        ["edit", "bogus"],
        ["edit", "plan"],
        ["search", "index"],
        ["search", "query", "x"],
        ["job", "status"],
        ["job", "overview", "--bogus"],
        ["cli", "overview", "--bogus"],
    ],
)
@pytest.mark.parametrize("json_mode", [False, True])
def test_parse_error_hints_name_installed_command(argv, json_mode, capsys) -> None:
    """o17: every argparse error hint names the installed command ``media``."""
    with pytest.raises(SystemExit) as exc:
        main([*argv, "--json"] if json_mode else argv)
    assert exc.value.code == 1
    err = capsys.readouterr().err
    hint = json.loads(err)["remediation"] if json_mode else err.split("hint:", 1)[1]
    assert "'media " in hint, hint
    assert "media-cli" not in hint, hint


def test_top_level_help_describes_the_lane_and_lists_new_verbs(capsys) -> None:
    with pytest.raises(SystemExit):
        main(["--help"])
    out = capsys.readouterr().out
    assert "template" not in out
    assert "device plane" in out and "edit" in out
    for verb in ("probe", "frames", "edit", "search", "job"):
        assert verb in out


def test_help_and_hints_outside_t23_ownership_name_media(capsys) -> None:
    with pytest.raises(SystemExit):
        main(["--help"])
    out = capsys.readouterr().out
    rc = main(["explain", "nonexistent", "--json"])
    err = json.loads(capsys.readouterr().err)
    assert rc == 1
    assert "media-cli" not in out and not _STALE_CMD.search(err["remediation"])


# --- whoami ---------------------------------------------------------------


def test_whoami_text(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["whoami"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "nick: media-cli" in out
    assert "backend: colleague" in out
    assert "model:" in out


def test_whoami_json(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["whoami", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["nick"] == "media-cli"
    assert payload["version"] == __version__
    assert payload["backend"] == "colleague"


# --- learn ----------------------------------------------------------------


def test_learn_text(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["learn"])
    assert rc == 0
    out = capsys.readouterr().out
    assert len(out) >= 200
    assert "Exit-code policy" in out
    assert "--json" in out
    assert "explain" in out
    assert "template" not in out
    assert "media probe" in out and "media edit apply" in out
    assert not _STALE_CMD.search(out)


def test_learn_json(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["learn", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["tool"] == "media"
    assert payload["distribution"] == "media-cli"
    assert payload["version"] == __version__
    assert payload["json_support"] is True
    assert payload["explain_pointer"].startswith("media explain")
    assert "template" not in payload["purpose"]
    assert not _STALE_CMD.search(json.dumps(payload))


def test_learn_json_lists_every_registered_command(capsys) -> None:
    rc = main(["learn", "--json"])
    assert rc == 0
    listed = {tuple(c["path"]) for c in json.loads(capsys.readouterr().out)["commands"]}
    leaves = _leaf_paths()
    for path in NEW_PATHS:
        if path in leaves:
            assert path in listed, f"learn --json is missing {' '.join(path)}"
    assert leaves <= listed, sorted(leaves - listed)
    assert listed <= set(_registered_paths()), sorted(listed - set(_registered_paths()))


#: h26 -- every clause of the spec's after-state (c33) and the learn commands that do it
AFTER_STATE = {
    "probe a file": [("probe",)],
    "extract/peek at frames": [("frames",)],
    "semantically search frames and speech for timestamps": [
        ("search", "index"),
        ("search", "query"),
    ],
    "submit a JSON edit list (cut, crop, black-box redact, blur, speed, fades, xfade "
    "transitions into a short)": [("edit", "plan"), ("edit", "apply")],
    "as a daemon job it polls by id": [("job", "status"), ("job", "result")],
    "every step dry-run first": [("edit", "apply"), ("search", "index"), ("search", "purge")],
    "every result JSON with provenance": [("probe",), ("search", "query"), ("job", "result")],
}


def test_after_state_clauses_map_to_learn_entries(capsys) -> None:
    rc = main(["learn", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    listed = {tuple(c["path"]) for c in payload["commands"]}
    declared = {a["clause"]: [tuple(p) for p in a["commands"]] for a in payload["after_state"]}
    registered = set(_registered_paths())
    for clause, paths in AFTER_STATE.items():
        assert clause in declared, f"learn --json after_state lacks clause: {clause}"
        for path in paths:
            assert path in declared[clause], (clause, path)
            assert path in listed and path in registered, path
    # the edit-list ops of the clause are named in the learn text an agent reads
    rc = main(["learn"])
    text = capsys.readouterr().out
    for word in ("cut", "crop", "box", "blur", "speed", "fade", "xfade", "--apply", "dry run"):
        assert word in text, word
    assert "media job result" in text and "media search query" in text


# --- explain --------------------------------------------------------------


def test_explain_root(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["explain"])
    assert rc == 0
    assert capsys.readouterr().out.startswith("# media\n")


@pytest.mark.parametrize("key", ["media", "media-cli"])
def test_explain_self(key: str, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["explain", key])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.startswith("# media")
    assert "template" not in out and "device plane" in out


def test_explain_json(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["explain", "whoami", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["path"] == ["whoami"]
    assert "media whoami" in payload["markdown"]


def test_explain_unknown_path_errors(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["explain", "nonexistent"])
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("error:")
    assert "hint:" in captured.err


def test_every_catalog_path_resolves(capsys: pytest.CaptureFixture[str]) -> None:
    for path in known_paths():
        rc = main(["explain", *path])
        assert rc == 0, f"explain {' '.join(path)} failed"
        capsys.readouterr()


def test_every_registered_path_has_a_catalog_entry() -> None:
    missing = [p for p in _registered_paths() if p not in ENTRIES]
    assert not missing, f"no explain entry for: {missing}"
    for path in NEW_PATHS:
        assert path in ENTRIES
    assert ("media",) in ENTRIES and ("media-cli",) in ENTRIES


@pytest.mark.parametrize("path", NEW_PATHS, ids=lambda p: " ".join(p))
def test_new_catalog_entries_are_agent_docs(path, capsys) -> None:
    rc = main(["explain", *path, "--json"])
    assert rc == 0
    md = json.loads(capsys.readouterr().out)["markdown"]
    assert md.startswith("# media " + path[0])
    assert "--json" in md
    assert "Exit" in md or "exit" in md
    assert "media " + " ".join(path) in md  # a usage example with the installed command


def test_catalog_never_names_media_cli_as_a_command() -> None:
    for path, md in ENTRIES.items():
        assert not _STALE_CMD.search(md), (path, _STALE_CMD.search(md).group(0))
        assert "template" not in md.lower(), path


def test_catalog_documents_write_verb_semantics() -> None:
    plan = ENTRIES[("edit", "plan")]
    for word in ("crop", "speed", "fade", "box", "blur", "xfade", "acrossfade", "keep"):
        assert word in plan, word
    assert "normalized seconds" in plan.lower() or "first presented" in plan
    apply = ENTRIES[("edit", "apply")]
    assert "--apply" in apply and "job_id" in apply and "daemon" in apply
    assert "input.output_container_mismatch" in apply
    assert "coverage" in ENTRIES[("edit", "regions")]
    assert "sense_calls" in ENTRIES[("search", "index")]
    assert "evidence" in ENTRIES[("search", "query")]
    assert "never" in ENTRIES[("job", "status")].lower()  # never starts the daemon
