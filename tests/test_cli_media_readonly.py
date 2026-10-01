"""CLI probe + frames verbs: read-only toward the source, no daemon, clean streams."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys

import pytest

from media_cli.cli import _CliArgumentParser, _dispatch
from media_cli.cli._commands import frames as frames_cmd
from media_cli.cli._commands import probe as probe_cmd
from tests.test_media_senses import RECORDED_CAPS, Stub

PNG = b"\x89PNG\r\n\x1a\n"


def _parser():
    parser = _CliArgumentParser(prog="media-cli")
    sub = parser.add_subparsers(dest="command", parser_class=_CliArgumentParser)
    probe_cmd.register(sub)
    frames_cmd.register(sub)
    return parser


def run(argv, capsys):
    _CliArgumentParser._json_hint = "--json" in argv
    try:
        args = _parser().parse_args(argv)
        rc = _dispatch(args)
    except SystemExit as exc:
        rc = int(exc.code or 0)
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


@pytest.fixture
def runtime_dir(tmp_path, monkeypatch):
    d = tmp_path / "xdg"
    d.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(d))
    monkeypatch.setenv("TMPDIR", str(d))
    return d


def test_probe_json(media_mp4, runtime_dir, capsys):
    before = sha(media_mp4)
    rc, out, err = run(["probe", str(media_mp4), "--json"], capsys)
    assert rc == 0 and err == ""
    doc = json.loads(out)
    assert doc["video"]["codec"] == "h264"
    assert doc["audio"] is not None
    assert sha(media_mp4) == before
    assert list(runtime_dir.iterdir()) == []


def test_probe_text_summary(media_mp4, capsys):
    rc, out, err = run(["probe", str(media_mp4)], capsys)
    assert rc == 0 and err == ""
    assert "video" in out and not out.lstrip().startswith("{")


def test_probe_missing_file(tmp_path, capsys):
    rc, out, err = run(["probe", str(tmp_path / "nope.mp4"), "--json"], capsys)
    assert rc == 1 and out == ""
    assert json.loads(err)["kind"] == "input.unreadable"
    assert "Traceback" not in err


def test_probe_text_error_has_hint(tmp_path, capsys):
    rc, out, err = run(["probe", str(tmp_path / "nope.mp4")], capsys)
    assert rc == 1 and err.startswith("error:") and "hint:" in err


def test_frames_at(media_mp4, tmp_path, runtime_dir, capsys):
    before = sha(media_mp4)
    out_dir = tmp_path / "o"
    rc, out, err = run(
        ["frames", str(media_mp4), "--at", "1", "2.5", "--out", str(out_dir), "--json"], capsys
    )
    assert rc == 0 and err == ""
    doc = json.loads(out)
    assert doc["count"] == 2 and len(doc["frames"]) == 2
    assert doc["sheet"] is None and doc["describe"] is None
    for f in doc["frames"]:
        assert open(f["path"], "rb").read(8) == PNG
        assert os.path.dirname(f["path"]) == str(out_dir)
    assert sha(media_mp4) == before
    assert list(runtime_dir.iterdir()) == []


def test_frames_every_with_sheet(media_mp4, tmp_path, capsys):
    rc, out, err = run(
        ["frames", str(media_mp4), "--every", "5", "--out", str(tmp_path), "--sheet", "--json"],
        capsys,
    )
    assert rc == 0 and err == ""
    doc = json.loads(out)
    assert doc["count"] == 2
    assert open(doc["sheet"], "rb").read(8) == PNG


def test_frames_scene(media_red_square, tmp_path, capsys):
    rc, out, err = run(
        ["frames", str(media_red_square), "--scene", "0.01", "--out", str(tmp_path), "--json"],
        capsys,
    )
    assert rc == 0 and err == ""
    assert json.loads(out)["count"] >= 1


def test_frames_text_summary(media_mp4, tmp_path, capsys):
    rc, out, err = run(["frames", str(media_mp4), "--at", "1", "--out", str(tmp_path)], capsys)
    assert rc == 0 and err == ""
    assert "1 frame" in out and not out.lstrip().startswith("{")


def test_frames_overwrite(media_mp4, tmp_path, capsys):
    argv = ["frames", str(media_mp4), "--at", "1", "--out", str(tmp_path), "--json"]
    assert run(argv, capsys)[0] == 0
    rc, out, err = run(argv, capsys)
    assert rc == 1 and json.loads(err)["kind"] == "input.output_exists"
    assert run([*argv, "--overwrite"], capsys)[0] == 0


def test_frames_out_of_range(media_mp4, tmp_path, capsys):
    rc, out, err = run(
        ["frames", str(media_mp4), "--at", "999", "--out", str(tmp_path), "--json"], capsys
    )
    assert rc == 1 and out == ""
    assert json.loads(err)["kind"] == "input.timestamp_out_of_range"
    assert "Traceback" not in err


def test_frames_bad_file(tmp_path, capsys):
    rc, out, err = run(
        ["frames", str(tmp_path / "x.mp4"), "--at", "1", "--out", str(tmp_path), "--json"], capsys
    )
    assert rc == 1 and json.loads(err)["kind"] == "input.unreadable"


def test_frames_needs_exactly_one_selector(media_mp4, tmp_path, capsys):
    base = ["frames", str(media_mp4), "--out", str(tmp_path), "--json"]
    rc, out, err = run(base, capsys)
    assert rc == 1 and out == "" and "Traceback" not in err
    rc, out, err = run([*base, "--at", "1", "--every", "2"], capsys)
    assert rc == 1 and out == ""


def test_frames_describe_ok(media_mp4, tmp_path, monkeypatch, capsys):
    stub = Stub(RECORDED_CAPS)
    try:
        monkeypatch.setenv("MEDIA_CLI_LOBES_URL", stub.url)
        rc, out, err = run(
            [
                "frames",
                str(media_mp4),
                "--at",
                "1",
                "3",
                "--out",
                str(tmp_path),
                "--describe",
                "--json",
            ],
            capsys,
        )
    finally:
        stub.close()
    assert rc == 0 and err == ""
    doc = json.loads(out)
    assert doc["describe"]["ok"] is True
    descs = doc["describe"]["descriptions"]
    assert [d["path"] for d in descs] == [f["path"] for f in doc["frames"]]
    assert all(d["text"] == "a red square" for d in descs)


def test_frames_describe_fail_soft(media_mp4, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MEDIA_CLI_LOBES_URL", "http://127.0.0.1:9")
    rc, out, err = run(
        ["frames", str(media_mp4), "--at", "1", "--out", str(tmp_path), "--describe", "--json"],
        capsys,
    )
    assert rc == 0 and err == ""
    doc = json.loads(out)
    assert doc["count"] == 1
    assert doc["describe"]["ok"] is False
    assert doc["describe"]["kind"].startswith("env.sense_")
    assert doc["describe"]["message"]


def test_frames_describe_not_local(media_mp4, tmp_path, monkeypatch, capsys):
    from tests.test_media_senses import PROXIED_SENSES

    stub = Stub(PROXIED_SENSES)
    try:
        monkeypatch.setenv("MEDIA_CLI_LOBES_URL", stub.url)
        rc, out, err = run(
            ["frames", str(media_mp4), "--at", "1", "--out", str(tmp_path), "--describe", "--json"],
            capsys,
        )
    finally:
        stub.close()
    assert rc == 0 and err == ""
    assert json.loads(out)["describe"]["kind"] == "env.sense_not_local"


def test_modules_never_import_daemon():
    code = (
        "import sys, media_cli.cli._commands.probe, media_cli.cli._commands.frames;"
        "bad=[m for m in sys.modules if m.startswith('media_cli.media.daemon')];"
        "sys.exit(1 if bad else 0)"
    )
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0
