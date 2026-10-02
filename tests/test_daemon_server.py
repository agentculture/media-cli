"""Tests for the media daemon server (t19).

Every wait is bounded and every process a test starts is killed in teardown,
so a broken server fails the test rather than hanging the suite.
"""

from __future__ import annotations

import ast
import fcntl
import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from media_cli.media.daemon import server
from media_cli.media.daemon.jobs import JobStore

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")

REPO = Path(__file__).resolve().parents[1]
LONG_ARGS = ["-hide_banner", "-re", "-f", "lavfi", "-i", "testsrc=d=60", "-f", "null", "-"]
WAIT = 20.0  # generous upper bound for any single wait


# -- helpers -------------------------------------------------------------------


def wait_for(pred, timeout=WAIT, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = pred()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(f"condition not met within {timeout}s")


def pid_alive(pid: int) -> bool:
    """True if ``pid`` exists and is not a zombie."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            state = fh.read().rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError, IndexError):
        return False
    return state != "Z"


def group_members(pgid: int) -> list[int]:
    members = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", encoding="utf-8") as fh:
                fields = fh.read().rsplit(")", 1)[1].split()
        except OSError:
            continue
        if fields[0] != "Z" and int(fields[2]) == pgid:
            members.append(int(entry))
    return members


def request(sock_path, payload, timeout=WAIT):
    """Send one request (dict, or raw bytes) and return the decoded response line."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(str(sock_path))
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode() + b"\n"
        s.sendall(data)
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf) if buf else None


def wait_state(sock_path, job_id, states, timeout=WAIT):
    def check():
        rec = request(sock_path, {"op": "status", "job_id": job_id})["job"]
        return rec if rec["state"] in states else None

    return wait_for(check, timeout)


def wait_pid(sock_path, job_id):
    def check():
        meta = request(sock_path, {"op": "status", "job_id": job_id})["job"]["meta"]
        return (meta["pid"], meta["pgid"]) if "pid" in meta else None

    return wait_for(check)


@pytest.fixture
def sockdir():
    # AF_UNIX paths are capped at ~108 bytes; pytest's tmp_path can be longer.
    parent = tempfile.mkdtemp(prefix="mdt")
    yield Path(parent) / "d"
    shutil.rmtree(parent, ignore_errors=True)


@pytest.fixture
def store(tmp_path):
    return JobStore(root=tmp_path / "jobs")


class ThreadDaemon:
    def __init__(self, sockdir, store, **kw):
        kw.setdefault("idle_timeout", 60.0)
        self.daemon = server.Daemon(sockdir, store=store, install_signals=False, **kw)
        self.rc = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        self.rc = self.daemon.run()

    @property
    def sock(self):
        return self.daemon.socket_path

    def start(self):
        self.thread.start()
        wait_for(lambda: self.daemon.accepting or not self.thread.is_alive())
        assert self.thread.is_alive(), "daemon thread exited during startup"
        return self

    def stop(self):
        self.daemon.stop()
        self.thread.join(WAIT)
        assert not self.thread.is_alive(), "daemon thread did not stop"


@pytest.fixture
def run_daemon(sockdir, store):
    started = []

    def _start(**kw):
        d = ThreadDaemon(sockdir, store, **kw).start()
        started.append(d)
        return d

    yield _start
    for d in started:
        if d.thread.is_alive():
            d.stop()


SPAWN_CODE = (
    "import sys; from media_cli.media.daemon.server import serve; "
    "sys.exit(serve(sys.argv[1], idle_timeout=float(sys.argv[2])))"
)


@pytest.fixture
def spawn_daemon(tmp_path):
    procs: list[subprocess.Popen] = []

    def _spawn(sockdir, idle_timeout=60.0):
        env = dict(os.environ, XDG_STATE_HOME=str(tmp_path / "state"))
        p = subprocess.Popen(
            [sys.executable, "-c", SPAWN_CODE, str(sockdir), str(idle_timeout)],
            cwd=str(REPO),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        procs.append(p)
        return p

    yield _spawn
    for p in procs:
        if p.poll() is None:
            p.kill()
        try:
            p.communicate(timeout=WAIT)
        except subprocess.TimeoutExpired:
            pass


# -- progress parsing ------------------------------------------------------------


def test_progress_parser_emits_blocks():
    parser = server.ProgressParser(duration=4.0)
    lines = [
        "frame=25",
        "fps=25.0",
        "out_time_us=1000000",
        "out_time_ms=1000000",
        "out_time=00:00:01.000000",
        "speed=1.0x",
        "progress=continue",
    ]
    results = [parser.feed(line) for line in lines]
    assert results[:-1] == [None] * 6
    block = results[-1]
    assert block["progress"] == "continue"
    assert block["out_time_us"] == 1_000_000
    assert block["frame"] == 25
    assert block["speed"] == "1.0x"
    assert block["fraction"] == pytest.approx(0.25)
    end = [parser.feed(x) for x in ("out_time_us=N/A", "out_time_ms=3000000", "progress=end")]
    assert end[-1]["progress"] == "end"
    assert end[-1]["out_time_us"] == 3_000_000  # falls back to out_time_ms (really µs)
    assert end[-1]["fraction"] == 1.0  # end is always complete


def test_progress_parser_without_duration():
    parser = server.ProgressParser()
    for line in ("out_time_us=N/A", "garbage line", "progress=continue"):
        block = parser.feed(line)
    assert block["progress"] == "continue"
    assert block["out_time_us"] is None
    assert block["fraction"] is None


# -- socket security (o13) --------------------------------------------------------


def test_socket_0600_in_0700_dir_and_ping(run_daemon, sockdir):
    d = run_daemon()
    assert stat.S_IMODE(os.stat(sockdir).st_mode) == 0o700
    st = os.lstat(d.sock)
    assert stat.S_ISSOCK(st.st_mode)
    assert stat.S_IMODE(st.st_mode) == 0o600
    resp = request(d.sock, {"op": "ping"})
    assert resp == {"ok": True, "pid": os.getpid()}


def test_default_sockdir_prefers_xdg_runtime_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "rt"))
    assert server.default_sockdir() == tmp_path / "rt" / "media-cli"


def test_default_sockdir_falls_back_to_cache_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    got = server.default_sockdir()
    assert got == tmp_path / "cache" / "media-cli" / "run"
    assert not got.exists()
    server.ensure_sockdir(got)  # creates missing parents, leaf 0700
    assert got.is_dir()
    assert stat.S_IMODE(os.stat(got).st_mode) == 0o700
    assert os.stat(got).st_uid == os.getuid()


def test_default_sockdir_falls_back_to_home_cache(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    got = server.default_sockdir()
    assert got == tmp_path / ".cache" / "media-cli" / "run"
    server.ensure_sockdir(got)
    assert stat.S_IMODE(os.stat(got).st_mode) == 0o700


def test_kill_group_escalates_to_sigkill_when_sigterm_ignored():
    # Exercises the escalation helper used by the ffmpeg handler with a child
    # that ignores SIGTERM (ffmpeg itself always honours SIGTERM).
    code = (
        "import signal, subprocess, sys, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "c = subprocess.Popen([sys.executable, '-c', "
        "'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)']); "
        "print(c.pid, flush=True); time.sleep(60)"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code], stdout=subprocess.PIPE, start_new_session=True
    )
    try:
        grandchild = int(proc.stdout.readline())
        pgid = proc.pid
        assert sorted(group_members(pgid)) == sorted([proc.pid, grandchild])
        t0 = time.monotonic()
        server._kill_group(proc, pgid, 0.5)
        elapsed = time.monotonic() - t0
        assert proc.returncode == -signal.SIGKILL
        assert 0.4 <= elapsed < WAIT
        wait_for(lambda: not pid_alive(grandchild))
        assert group_members(pgid) == []
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(WAIT)
        proc.stdout.close()


def test_refuses_loose_sockdir(sockdir, store):
    sockdir.mkdir(mode=0o755)
    os.chmod(sockdir, 0o755)
    daemon = server.Daemon(sockdir, store=store, install_signals=False)
    with pytest.raises(server.DaemonSetupError):
        daemon.run()
    assert not (sockdir / server.SOCKET_NAME).exists()


def test_refuses_symlinked_sockdir(sockdir, store, tmp_path):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    sockdir.parent.mkdir(exist_ok=True)
    os.symlink(real, sockdir)
    daemon = server.Daemon(sockdir, store=store, install_signals=False)
    with pytest.raises(server.DaemonSetupError):
        daemon.run()


def test_serve_returns_2_on_bad_sockdir(sockdir, store):
    sockdir.mkdir(mode=0o700)
    os.chmod(sockdir, 0o777)
    assert server.serve(sockdir, store=store, install_signals=False) == 2


def test_peer_with_other_uid_refused_before_read(run_daemon, store):
    d = run_daemon()
    d.daemon.expected_uid = os.getuid() + 1  # pretend we are someone else
    seen = []
    orig = d.daemon._handle_line
    d.daemon._handle_line = lambda line: seen.append(line) or orig(line)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(WAIT)
        s.connect(str(d.sock))
        try:
            s.sendall(json.dumps({"op": "submit", "job": {"kind": "ffmpeg", "args": []}}).encode())
            s.sendall(b"\n")
        except (BrokenPipeError, ConnectionResetError):
            pass
        try:
            data = s.recv(1024)
        except ConnectionResetError:
            data = b""
    assert data == b""  # closed without a single byte of response
    assert seen == []  # no request line was ever handled
    assert store.list() == []  # and nothing was submitted
    assert d.daemon.rejected_peers >= 1
    d.daemon.expected_uid = os.getuid()
    assert request(d.sock, {"op": "ping"})["ok"] is True


# -- protocol --------------------------------------------------------------------


def test_malformed_json_rejected(run_daemon):
    d = run_daemon()
    resp = request(d.sock, b"{not json\n")
    assert resp["ok"] is False
    assert resp["error"]["kind"] == "input.bad_request"
    resp = request(d.sock, b"[1, 2]\n")
    assert resp["error"]["kind"] == "input.bad_request"


def test_oversized_request_rejected(run_daemon):
    d = run_daemon()
    big = b'{"op": "ping", "pad": "' + b"x" * (server.MAX_REQUEST_BYTES + 10) + b'"}\n'
    resp = request(d.sock, big)
    assert resp["ok"] is False
    assert resp["error"]["kind"] == "input.request_too_large"


def test_unknown_op_and_kind_and_job(run_daemon, store):
    d = run_daemon()
    assert request(d.sock, {"op": "explode"})["error"]["kind"] == "input.unknown_op"
    resp = request(d.sock, {"op": "submit", "job": {"kind": "nope"}})
    assert resp["error"]["kind"] == "input.unknown_job_kind"
    resp = request(d.sock, {"op": "submit", "job": {"kind": "ffmpeg", "args": "not-a-list"}})
    assert resp["error"]["kind"] == "input.bad_request"
    resp = request(d.sock, {"op": "status", "job_id": "0000-missing"})
    assert resp["error"]["kind"] == "input.job_not_found"
    assert store.list() == []


def test_multiple_requests_per_connection(run_daemon):
    d = run_daemon()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(WAIT)
        s.connect(str(d.sock))
        s.sendall(b'{"op": "ping"}\n{"op": "ping"}\n')
        f = s.makefile("rb")
        assert json.loads(f.readline())["ok"] is True
        assert json.loads(f.readline())["ok"] is True


# -- pluggable handlers + concurrency bound ---------------------------------------


def test_custom_handler_runs_and_bound_is_respected(run_daemon, monkeypatch):
    lock = threading.Lock()
    state = {"now": 0, "max": 0, "order": []}
    release = threading.Event()

    def handler(rec, store, cancel_event):
        with lock:
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
            state["order"].append(rec.meta["spec"]["n"])
        release.wait(WAIT)
        with lock:
            state["now"] -= 1
        return {"output": f"out-{rec.meta['spec']['n']}"}

    monkeypatch.setitem(server._HANDLERS, "test.block", handler)
    d = run_daemon(max_concurrent=2)
    ids = [
        request(d.sock, {"op": "submit", "job": {"kind": "test.block", "n": n}})["job_id"]
        for n in range(5)
    ]
    wait_for(lambda: state["now"] == 2)
    time.sleep(0.3)
    assert state["max"] == 2
    queued = [request(d.sock, {"op": "status", "job_id": i})["job"]["state"] for i in ids]
    assert queued.count("running") == 2
    assert queued.count("queued") == 3
    release.set()
    for i in ids:
        rec = wait_state(d.sock, i, {"done"})
        assert rec["output"].startswith("out-")
    assert state["max"] == 2
    assert state["order"][:2] == [0, 1]
    assert sorted(state["order"]) == [0, 1, 2, 3, 4]
    res = request(d.sock, {"op": "result", "job_id": ids[0]})
    assert res["ok"]
    assert res["ready"] is True
    assert res["output"] == "out-0"


def test_custom_handler_failure_marks_failed(run_daemon, monkeypatch):
    def boom(rec, store, cancel_event):
        raise RuntimeError("kaboom")

    monkeypatch.setitem(server._HANDLERS, "test.boom", boom)
    d = run_daemon()
    jid = request(d.sock, {"op": "submit", "job": {"kind": "test.boom"}})["job_id"]
    rec = wait_state(d.sock, jid, {"failed"})
    assert "kaboom" in rec["error"]["message"]
    res = request(d.sock, {"op": "result", "job_id": jid})
    assert res["ready"] is True
    assert res["output"] is None


def test_register_handler_rejects_ffmpeg_override():
    with pytest.raises(ValueError):
        server.register_handler("ffmpeg", lambda *a: None)


def test_cancel_queued_job_never_runs(run_daemon, monkeypatch):
    release = threading.Event()
    ran = []

    def handler(rec, store, cancel_event):
        ran.append(rec.id)
        release.wait(WAIT)

    monkeypatch.setitem(server._HANDLERS, "test.block", handler)
    d = run_daemon(max_concurrent=1)
    first = request(d.sock, {"op": "submit", "job": {"kind": "test.block"}})["job_id"]
    second = request(d.sock, {"op": "submit", "job": {"kind": "test.block"}})["job_id"]
    wait_for(lambda: ran == [first])
    resp = request(d.sock, {"op": "cancel", "job_id": second})
    assert resp["ok"]
    assert resp["job"]["state"] == "cancelled"
    release.set()
    wait_state(d.sock, first, {"done"})
    time.sleep(0.3)
    assert ran == [first]
    rec = request(d.sock, {"op": "status", "job_id": second})["job"]
    assert rec["state"] == "cancelled"
    assert rec["timings"]["started"] is None


def test_cancel_terminal_job_is_error(run_daemon, monkeypatch):
    monkeypatch.setitem(server._HANDLERS, "test.quick", lambda rec, store, ev: None)
    d = run_daemon()
    jid = request(d.sock, {"op": "submit", "job": {"kind": "test.quick"}})["job_id"]
    wait_state(d.sock, jid, {"done"})
    resp = request(d.sock, {"op": "cancel", "job_id": jid})
    assert resp["ok"] is False
    assert resp["error"]["kind"] == "input.job_illegal_transition"


def test_idle_exit_waits_for_running_job(run_daemon, monkeypatch):
    release = threading.Event()
    monkeypatch.setitem(
        server._HANDLERS, "test.block", lambda rec, store, ev: release.wait(WAIT) and None
    )
    d = run_daemon(idle_timeout=0.3)
    jid = request(d.sock, {"op": "submit", "job": {"kind": "test.block"}})["job_id"]
    time.sleep(1.0)
    assert d.thread.is_alive()  # busy, so not idle
    release.set()
    wait_for(lambda: not d.thread.is_alive())
    assert d.rc == 0
    assert not d.sock.exists()
    assert JobStore(root=d.daemon.store.root).get(jid).state == "done"


# -- ffmpeg jobs ----------------------------------------------------------------


@needs_ffmpeg
def test_ffmpeg_job_success_progress_and_atomic_replace(run_daemon, tmp_path):
    d = run_daemon()
    out = tmp_path / "out.mkv"
    tmp_out = tmp_path / ".out.partial.mkv"
    args = ["-hide_banner", "-y", "-f", "lavfi", "-i", "testsrc=d=1:size=160x120:rate=10"]
    args += ["-c:v", "mpeg4", "-f", "matroska", str(tmp_out)]
    job = {"kind": "ffmpeg", "args": args, "output": str(out), "tmp_output": str(tmp_out)}
    job["duration"] = 1.0
    jid = request(d.sock, {"op": "submit", "job": job})["job_id"]
    rec = wait_state(d.sock, jid, {"done", "failed"})
    assert rec["state"] == "done", rec
    assert out.exists()
    assert out.stat().st_size > 0
    assert not tmp_out.exists()
    assert rec["progress"]["progress"] == "end"
    assert rec["progress"]["fraction"] == 1.0
    res = request(d.sock, {"op": "result", "job_id": jid})
    assert res["ready"]
    assert res["output"] == str(out)
    assert not pid_alive(rec["meta"]["pid"])


def _lavfi_job(tmp_path, **extra):
    out = tmp_path / "out.mkv"
    tmp_out = tmp_path / ".out.partial.mkv"
    args = ["-hide_banner", "-y", "-f", "lavfi", "-i", "testsrc=d=1:size=160x120:rate=10"]
    args += ["-c:v", "mpeg4", "-f", "matroska", str(tmp_out)]
    job = {"kind": "ffmpeg", "args": args, "output": str(out), "tmp_output": str(tmp_out)}
    job.update(extra)
    return job, out, tmp_out


@needs_ffmpeg
def test_ffmpeg_job_does_not_clobber_output_created_after_planning(run_daemon, tmp_path):
    d = run_daemon()
    job, out, tmp_out = _lavfi_job(tmp_path)
    out.write_bytes(b"appeared after planning")  # exists before completion, no overwrite
    jid = request(d.sock, {"op": "submit", "job": job})["job_id"]
    rec = wait_state(d.sock, jid, {"done", "failed"})
    assert rec["state"] == "failed", rec
    assert rec["error"]["kind"] == "input.output_exists"
    assert str(out) in rec["error"]["message"]
    assert out.read_bytes() == b"appeared after planning"
    assert not tmp_out.exists()


@needs_ffmpeg
def test_ffmpeg_job_overwrite_true_replaces_existing_output(run_daemon, tmp_path):
    d = run_daemon()
    job, out, tmp_out = _lavfi_job(tmp_path, overwrite=True)
    out.write_bytes(b"old")
    jid = request(d.sock, {"op": "submit", "job": job})["job_id"]
    rec = wait_state(d.sock, jid, {"done", "failed"})
    assert rec["state"] == "done", rec
    assert out.stat().st_size > len(b"old")
    assert not tmp_out.exists()


@needs_ffmpeg
def test_ffmpeg_job_normal_publish_leaves_no_tmp(run_daemon, tmp_path):
    d = run_daemon()
    job, out, tmp_out = _lavfi_job(tmp_path, overwrite=False)
    jid = request(d.sock, {"op": "submit", "job": job})["job_id"]
    rec = wait_state(d.sock, jid, {"done", "failed"})
    assert rec["state"] == "done", rec
    assert out.stat().st_size > 0
    assert not tmp_out.exists()


def test_ffmpeg_job_overwrite_must_be_bool(run_daemon):
    d = run_daemon()
    job = {"kind": "ffmpeg", "args": ["-version"], "overwrite": "yes"}
    resp = request(d.sock, {"op": "submit", "job": job})
    assert not resp["ok"]


@needs_ffmpeg
def test_ffmpeg_job_failure_marks_failed_and_removes_tmp(run_daemon, tmp_path):
    d = run_daemon()
    out = tmp_path / "out.mkv"
    tmp_out = tmp_path / ".out.partial.mkv"
    tmp_out.write_bytes(b"partial")
    args = ["-hide_banner", "-y", "-i", str(tmp_path / "does-not-exist.mp4"), str(tmp_out)]
    job = {"kind": "ffmpeg", "args": args, "output": str(out), "tmp_output": str(tmp_out)}
    jid = request(d.sock, {"op": "submit", "job": job})["job_id"]
    rec = wait_state(d.sock, jid, {"done", "failed"})
    assert rec["state"] == "failed"
    assert "does-not-exist" in rec["error"]["stderr_tail"]
    assert Path(rec["error"]["log_path"]).exists()
    assert not tmp_out.exists()
    assert not out.exists()


@needs_ffmpeg
def test_cancel_running_ffmpeg_kills_group(run_daemon, tmp_path):
    d = run_daemon()
    tmp_out = tmp_path / "partial.nut"
    tmp_out.write_bytes(b"x")
    job = {"kind": "ffmpeg", "args": LONG_ARGS, "tmp_output": str(tmp_out)}
    jid = request(d.sock, {"op": "submit", "job": job})["job_id"]
    wait_state(d.sock, jid, {"running"})
    pid, pgid = wait_pid(d.sock, jid)
    assert pid == pgid != os.getpgid(0)  # its own process group
    wait_for(lambda: request(d.sock, {"op": "status", "job_id": jid})["job"]["progress"])
    resp = request(d.sock, {"op": "cancel", "job_id": jid})
    assert resp["ok"]
    assert resp["job"]["state"] == "cancelled"
    assert not pid_alive(pid)
    assert group_members(pgid) == []
    assert not tmp_out.exists()


@needs_ffmpeg
def test_ffmpeg_jobs_run_one_at_a_time_by_default(run_daemon):
    d = run_daemon()
    a = request(d.sock, {"op": "submit", "job": {"kind": "ffmpeg", "args": LONG_ARGS}})
    b = request(d.sock, {"op": "submit", "job": {"kind": "ffmpeg", "args": LONG_ARGS}})
    rec_a = wait_state(d.sock, a["job_id"], {"running"})
    time.sleep(0.5)
    assert request(d.sock, {"op": "status", "job_id": b["job_id"]})["job"]["state"] == "queued"
    request(d.sock, {"op": "cancel", "job_id": a["job_id"]})
    rec_b = wait_state(d.sock, b["job_id"], {"running"})
    pid_b, _ = wait_pid(d.sock, b["job_id"])
    request(d.sock, {"op": "cancel", "job_id": b["job_id"]})
    assert rec_a["state"] == "running"
    assert rec_b["state"] == "running"
    assert not pid_alive(pid_b)


# -- process-level lifecycle ------------------------------------------------------


def test_ten_concurrent_spawners_yield_one_daemon(sockdir, spawn_daemon):
    procs = [spawn_daemon(sockdir) for _ in range(10)]
    wait_for(lambda: sum(p.poll() is None for p in procs) <= 1, timeout=60)
    time.sleep(0.5)
    alive = [p for p in procs if p.poll() is None]
    assert len(alive) == 1
    losers = [p for p in procs if p is not alive[0]]
    for p in losers:
        out, err = p.communicate(timeout=WAIT)
        assert p.returncode == 0, err
        assert out == b""
        assert err == b""
    sock = sockdir / server.SOCKET_NAME
    wait_for(sock.exists)
    assert request(sock, {"op": "ping"})["pid"] == alive[0].pid
    alive[0].send_signal(signal.SIGTERM)
    assert alive[0].wait(WAIT) == 0
    assert not sock.exists()


def test_losing_spawner_returns_0(sockdir, store):
    sockdir.mkdir(mode=0o700)
    fd = os.open(sockdir / server.LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        daemon = server.Daemon(sockdir, store=store, install_signals=False)
        with pytest.raises(server.DaemonAlreadyRunning):
            daemon.run()
        assert server.serve(sockdir, store=store, install_signals=False) == 0
    finally:
        os.close(fd)


def test_stale_socket_is_replaced(sockdir, run_daemon):
    sockdir.mkdir(mode=0o700)
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(sockdir / server.SOCKET_NAME))
    stale.close()  # file remains, nobody listens
    d = run_daemon()
    assert request(d.sock, {"op": "ping"})["ok"] is True


@needs_ffmpeg
def test_sigterm_kills_running_groups_and_cleans_up(sockdir, spawn_daemon, tmp_path):
    p = spawn_daemon(sockdir)
    sock = sockdir / server.SOCKET_NAME
    wait_for(sock.exists)
    jid = request(sock, {"op": "submit", "job": {"kind": "ffmpeg", "args": LONG_ARGS}})["job_id"]
    queued = request(sock, {"op": "submit", "job": {"kind": "ffmpeg", "args": LONG_ARGS}})
    wait_state(sock, jid, {"running"})
    pid, pgid = wait_pid(sock, jid)
    assert pid_alive(pid)
    p.send_signal(signal.SIGTERM)
    assert p.wait(WAIT) == 0
    assert not pid_alive(pid)
    assert group_members(pgid) == []
    assert not sock.exists()
    store = JobStore(root=tmp_path / "state" / "media-cli" / "jobs")
    assert store.get(jid).state == "cancelled"
    assert store.get(queued["job_id"]).state == "cancelled"
    # lock released: a fresh daemon can take over
    p2 = spawn_daemon(sockdir, idle_timeout=0.3)
    assert p2.wait(WAIT) == 0


def test_idle_exit_subprocess(sockdir, spawn_daemon):
    p = spawn_daemon(sockdir, idle_timeout=0.5)
    sock = sockdir / server.SOCKET_NAME
    wait_for(lambda: sock.exists() or p.poll() is not None)
    assert p.wait(WAIT) == 0
    assert not sock.exists()


# -- boundaries (criterion 4) ---------------------------------------------------


def test_server_has_no_mesh_imports_and_no_network_sockets():
    src_path = Path(server.__file__)
    src = src_path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    banned = ("culture", "agentirc", "irc", "mesh", "http", "urllib", "requests", "asyncio")
    for mod in imported:
        assert not any(mod == b or mod.startswith(b + ".") for b in banned), mod
    assert "AF_INET" not in src
    assert "culture.yaml" not in src
    assert "AGENTS" not in src
    # ffmpeg only via the _tools seam (o2): no direct subprocess/os.exec*/spawn calls.
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            owner = node.func.value
            if isinstance(owner, ast.Name) and owner.id in ("subprocess", "os"):
                name = node.func.attr
                assert name not in ("run", "Popen", "call", "check_call", "check_output"), name
                assert not name.startswith(("exec", "spawn", "posix_spawn", "system", "popen"))


def test_identity_files_untouched():
    if not (REPO / ".git").exists():
        pytest.skip("not a git checkout")
    out = subprocess.run(
        ["git", "status", "--porcelain", "--", "culture.yaml", "AGENTS.colleague.md"],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=WAIT,
        check=False,
    )
    assert out.stdout.strip() == ""


def test_check_socket_path_limit_and_remediation(tmp_path):
    server.check_socket_path("/tmp/" + "a" * (server._SUN_PATH_MAX - 5 - 1))  # exactly at limit
    with pytest.raises(server.MediaEnvError) as ei:
        server.check_socket_path("/tmp/" + "a" * server._SUN_PATH_MAX)
    assert ei.value.kind == "env.socket_path_too_long"
    assert "XDG_RUNTIME_DIR" in ei.value.remediation


def test_daemon_run_rejects_long_socket_path_before_creating_anything(tmp_path, store):
    sd = tmp_path / ("x" * 120)
    daemon = server.Daemon(sd, store=store, install_signals=False)
    with pytest.raises(server.MediaEnvError) as ei:
        daemon.run()
    assert ei.value.kind == "env.socket_path_too_long"
    assert not sd.exists()


def test_wire_errors_carry_remediation(run_daemon):
    d = run_daemon()
    for payload in ({"op": "nope"}, {"op": "status"}, {"op": "submit", "job": 3}):
        err = request(d.sock, payload)["error"]
        assert err["remediation"], err["kind"]
