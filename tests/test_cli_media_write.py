"""Write verbs (edit / search / job): dry-run by default, --apply commits (obligation o16)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from media_cli.cli import _CliArgumentParser, _dispatch
from media_cli.cli._commands import edit as edit_cmd
from media_cli.cli._commands import job as job_cmd
from media_cli.cli._commands import search as search_cmd
from tests.test_daemon_client import Env, _kill_all
from tests.test_media_index import SENSES_CAPS

NOUNS = (edit_cmd, search_cmd, job_cmd)


def _parser():
    parser = _CliArgumentParser(prog="media-cli")
    sub = parser.add_subparsers(dest="command", parser_class=_CliArgumentParser)
    for mod in NOUNS:
        mod.register(sub)
    return parser


def run(argv, capsys):
    _CliArgumentParser._json_hint = "--json" in argv
    try:
        rc = _dispatch(_parser().parse_args(argv))
    except SystemExit as exc:
        rc = int(exc.code or 0)
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def snapshot(root: Path):
    """Every path under root with its type, size, mtime and content hash."""
    snap = {}
    for dp, dn, fn in os.walk(root):
        for name in dn + fn:
            p = os.path.join(dp, name)
            st = os.lstat(p)
            digest = sha(p) if os.path.isfile(p) and not os.path.islink(p) else None
            snap[p] = (st.st_mode, st.st_size, st.st_mtime_ns, digest)
    return snap


@pytest.fixture
def env(monkeypatch):
    base = Path(tempfile.mkdtemp(prefix="mdw"))
    e = Env(base)
    e.work = base / "work"
    e.work.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(e.runtime))
    monkeypatch.setenv("XDG_STATE_HOME", str(e.state))
    monkeypatch.setenv("XDG_CACHE_HOME", str(e.cache))
    try:
        yield e
    finally:
        _kill_all(e)
        leftover = e.daemons()
        shutil.rmtree(base, ignore_errors=True)
        assert leftover == [], f"daemon processes survived teardown: {leftover}"


def editlist(env, src, name="out.mp4", **extra):
    doc = {"input": str(src), "output": str(env.work / name), "segments": [{"start": 0, "end": 1}]}
    doc.update(extra)
    p = env.work / "edit.json"
    p.write_text(json.dumps(doc))
    return p


def wait_done(capsys, job_id, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rc, out, _ = run(["job", "result", job_id, "--json"], capsys)
        assert rc == 0
        doc = json.loads(out)
        if doc["ready"]:
            return doc
        time.sleep(0.1)
    raise AssertionError("job did not finish")


# ---------------------------------------------------------------- dry runs (o16)


def test_edit_plan_prints_plan_and_writes_nothing(env, media_mp4, capsys):
    el = editlist(env, media_mp4)
    before, src = snapshot(env.base), sha(media_mp4)
    rc, out, err = run(["edit", "plan", str(el), "--json"], capsys)
    assert rc == 0 and err == ""
    doc = json.loads(out)
    assert doc["args"] and doc["output"]["dst"].endswith("out.mp4")
    assert "flags" in doc and "segments" in doc and doc["dry_run"] is True
    assert snapshot(env.base) == before and sha(media_mp4) == src
    assert env.daemons() == [] and not env.sockdir.exists()


def test_edit_apply_without_apply_behaves_like_plan(env, media_mp4, capsys):
    el = editlist(env, media_mp4)
    before = snapshot(env.base)
    rc, out, err = run(["edit", "apply", str(el), "--json"], capsys)
    assert rc == 0 and err == ""
    doc = json.loads(out)
    assert doc["dry_run"] is True and "job_id" not in doc
    assert snapshot(env.base) == before
    assert env.daemons() == [] and not env.sockdir.exists()


def test_edit_plan_text_mode(env, media_mp4, capsys):
    rc, out, err = run(["edit", "plan", str(editlist(env, media_mp4))], capsys)
    assert rc == 0 and err == "" and not out.lstrip().startswith("{")
    assert "out.mp4" in out and "dry run" in out.lower()


def test_edit_output_extension_mismatch_fails_at_plan_time(env, media_mp4, capsys):
    el = editlist(env, media_mp4, name="out.mkv")
    before = snapshot(env.base)
    rc, out, err = run(["edit", "plan", str(el), "--json"], capsys)
    assert rc == 1 and out == ""
    assert json.loads(err)["kind"] == "input.output_container_mismatch"
    assert snapshot(env.base) == before


def test_search_index_dry_run_estimate_writes_nothing(env, media_mp4, capsys, monkeypatch):
    monkeypatch.setenv("MEDIA_CLI_LOBES_URL", "http://127.0.0.1:9")
    before, src = snapshot(env.base), sha(media_mp4)
    rc, out, err = run(["search", "index", str(media_mp4), "--fps", "1", "--json"], capsys)
    assert rc == 0 and err == ""
    doc = json.loads(out)
    for key in ("frames", "batches", "sense_calls", "cap", "cached"):
        assert key in doc
    assert snapshot(env.base) == before and sha(media_mp4) == src
    assert env.daemons() == [] and not env.sockdir.exists()


def test_search_purge_dry_run_lists_and_removes_nothing(env, media_mp4, capsys):
    before = snapshot(env.base)
    rc, out, err = run(["search", "purge", str(media_mp4), "--json"], capsys)
    assert rc == 0 and err == ""
    doc = json.loads(out)
    assert doc["dry_run"] is True and "indexes" in doc["would_remove"]
    assert snapshot(env.base) == before


def test_search_cache_reports(env, media_mp4, capsys):
    rc, out, err = run(["search", "cache", "--json"], capsys)
    assert rc == 0 and err == ""
    assert json.loads(out)["indexes"] == []


# ---------------------------------------------------------------- --apply


def test_edit_apply_runs_a_job_and_keeps_source(env, media_mp4, capsys):
    el = editlist(env, media_mp4)
    src = sha(media_mp4)
    rc, out, err = run(["edit", "apply", str(el), "--apply", "--json"], capsys)
    assert rc == 0 and err == ""
    doc = json.loads(out)
    assert doc["job_id"] and doc["output"].endswith("out.mp4") and doc["plan"]["args"]
    res = wait_done(capsys, doc["job_id"])
    assert res["job"]["state"] == "done", res
    assert (env.work / "out.mp4").exists()
    assert sha(media_mp4) == src
    rc, out, _ = run(["job", "status", doc["job_id"], "--json"], capsys)
    assert rc == 0 and json.loads(out)["job"]["id"] == doc["job_id"]
    rc, out, _ = run(["job", "list", "--json"], capsys)
    assert doc["job_id"] in [j["id"] for j in json.loads(out)["jobs"]]


# ---------------------------------------------------------------- search with a stub gateway


class Stub:
    def __init__(self):
        self.requests = 0
        stub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, body):
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                stub.requests += 1
                self._send(SENSES_CAPS)

            def do_POST(self):
                stub.requests += 1
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                content = body["messages"][0]["content"]
                if isinstance(content, str):
                    n = len(re.findall(r"^\[\d+\]", content, re.M))
                    reply = {
                        "matches": [{"i": i, "match": True, "confidence": 0.9} for i in range(n)]
                    }
                else:
                    n = sum(1 for p in content if p["type"] == "image_url")
                    reply = {"captions": [f"caption {i}" for i in range(n)]}
                self._send({"choices": [{"message": {"content": json.dumps(reply)}}]})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def stub(monkeypatch):
    s = Stub()
    monkeypatch.setenv("MEDIA_CLI_LOBES_URL", s.url)
    yield s
    s.close()


def test_search_index_apply_then_query(env, stub, media_mp4, capsys):
    src = sha(media_mp4)
    rc, out, err = run(["search", "query", str(media_mp4), "a red car", "--json"], capsys)
    assert rc == 1 and json.loads(err)["kind"] == "input.index_missing"
    assert "media search index" in json.loads(err)["remediation"]
    assert stub.requests == 0

    rc, out, err = run(
        ["search", "index", str(media_mp4), "--fps", "0.5", "--apply", "--json"], capsys
    )
    assert rc == 0 and err == ""
    doc = json.loads(out)
    assert doc["job_id"]
    res = wait_done(capsys, doc["job_id"])
    assert res["job"]["state"] == "done", res
    assert res["output"]

    rc, out, err = run(["search", "query", str(media_mp4), "a red car", "--json"], capsys)
    assert rc == 0, err
    q = json.loads(out)
    assert q["hits"] and q["modality"] == "frames"
    assert sha(media_mp4) == src

    rc, out, _ = run(["search", "cache", str(media_mp4), "--json"], capsys)
    assert len(json.loads(out)["indexes"]) == 1
    rc, out, _ = run(["search", "purge", str(media_mp4), "--apply", "--json"], capsys)
    assert rc == 0 and json.loads(out)["removed"]["indexes"] == 1
    rc, out, _ = run(["search", "cache", str(media_mp4), "--json"], capsys)
    assert json.loads(out)["indexes"] == []


def test_search_index_budget_exceeded_makes_no_requests(env, stub, media_mp4, capsys):
    for extra in ([], ["--apply"]):
        rc, out, err = run(
            [
                "search",
                "index",
                str(media_mp4),
                "--fps",
                "25",
                "--max-calls",
                "1",
                *extra,
                "--json",
            ],
            capsys,
        )
        assert rc == 1 and out == ""
        assert json.loads(err)["kind"] == "input.budget_exceeded"
    assert stub.requests == 0
    assert env.daemons() == [] and not env.sockdir.exists()


# ---------------------------------------------------------------- job + overview + parse errors


def test_job_status_unknown_id(env, capsys):
    for verb in ("status", "result", "cancel"):
        rc, out, err = run(["job", verb, "nope", "--json"], capsys)
        assert rc == 1 and out == ""
        assert json.loads(err)["kind"] == "input.job_not_found"
    assert env.daemons() == [] and not env.sockdir.exists()


def test_job_list_empty_never_spawns(env, capsys):
    rc, out, err = run(["job", "list", "--json"], capsys)
    assert rc == 0 and json.loads(out)["jobs"] == []
    assert env.daemons() == [] and not env.sockdir.exists()


@pytest.mark.parametrize("noun", ["edit", "search", "job"])
def test_noun_overview(noun, capsys):
    rc, out, err = run([noun, "overview", "--json"], capsys)
    assert rc == 0 and err == ""
    assert json.loads(out)["subject"].endswith(noun)
    rc, out, err = run([noun, "overview"], capsys)
    assert rc == 0 and err == "" and noun in out


@pytest.mark.parametrize(
    "argv",
    [
        ["edit", "plan"],
        ["edit", "bogus", "--json"],
        ["search", "index"],
        ["search", "query", "x", "--json"],
        ["job", "status", "--json"],
        ["job", "overview", "--bogus", "--json"],
    ],
)
def test_parse_errors_are_structured(argv, capsys):
    rc, out, err = run(argv, capsys)
    assert rc == 1 and out == "" and err.strip()
    assert "Traceback" not in err


def test_edit_regions_fail_closed_and_leaves_no_files(env, media_mp4, capsys, monkeypatch):
    monkeypatch.setenv("MEDIA_CLI_LOBES_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("TMPDIR", str(env.work))
    before = snapshot(env.base)
    rc, out, err = run(["edit", "regions", str(media_mp4), "the logo", "--json"], capsys)
    assert rc == 2 and out == ""
    assert json.loads(err)["kind"].startswith("env.sense")
    assert snapshot(env.base) == before
