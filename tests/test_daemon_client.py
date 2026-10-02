"""Tests for the media daemon client (t20): spawn on demand, socket dir fallback.

Every test runs against a private tmp ``XDG_RUNTIME_DIR`` / ``XDG_STATE_HOME`` /
``XDG_CACHE_HOME``. The ``env`` fixture finds every daemon process whose
environment points at those tmp dirs (via ``/proc/<pid>/environ``), kills it in
teardown and asserts none remain, so a daemon never outlives its test.
"""

from __future__ import annotations

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

from media_cli.media.daemon import client as client_mod
from media_cli.media.daemon import server
from media_cli.media.daemon.client import DaemonClient
from media_cli.media.daemon.jobs import JobStore
from media_cli.media.errors import MediaEnvError, MediaInputError

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")

WAIT = 20.0
SHORT_ARGS = ["-hide_banner", "-f", "lavfi", "-i", "testsrc=d=1", "-f", "null", "-"]
SHORT_JOB = {"kind": "ffmpeg", "args": SHORT_ARGS}
# Never runs to completion within a test: keeps a job "queued"/"running" on purpose.
LONG_ARGS = ["-hide_banner", "-re", "-f", "lavfi", "-i", "testsrc=d=60", "-f", "null", "-"]
LONG_JOB = {"kind": "ffmpeg", "args": LONG_ARGS}


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
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            state = fh.read().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state != "Z"


def daemons_for(marker: str) -> list[int]:
    """Live pids running ``-m media_cli.media.daemon`` whose env mentions ``marker``."""
    pids = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as fh:
                argv = fh.read().split(b"\0")
            if b"media_cli.media.daemon" not in argv:
                continue
            with open(f"/proc/{entry}/environ", "rb") as fh:
                environ = fh.read()
        except OSError:
            continue
        if marker.encode() in environ and pid_alive(int(entry)):
            pids.append(int(entry))
    return pids


class Env:
    def __init__(self, base: Path) -> None:
        self.base = base
        self.runtime = base / "run"
        self.state = base / "state"
        self.cache = base / "cache"
        self.runtime.mkdir(mode=0o700)

    @property
    def sockdir(self) -> Path:
        return self.runtime / "media-cli"

    @property
    def fallback_sockdir(self) -> Path:
        return self.cache / "media-cli" / "run"

    @property
    def log(self) -> Path:
        return self.state / "media-cli" / "daemon.log"

    def daemons(self) -> list[int]:
        return daemons_for(str(self.base))


def _kill_all(env: Env) -> None:
    pids = env.daemons()
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + WAIT
    while time.monotonic() < deadline and any(pid_alive(p) for p in pids):
        time.sleep(0.05)
    for pid in pids:
        if pid_alive(pid):
            os.kill(pid, signal.SIGKILL)
    for pid in pids:  # reap ours so no zombie lingers
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass


@pytest.fixture
def env(monkeypatch):
    # AF_UNIX paths are capped at ~108 bytes; pytest's tmp_path can be longer.
    base = Path(tempfile.mkdtemp(prefix="mdc"))
    e = Env(base)
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


def run_py(code: str, *args: str, timeout: float = WAIT) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", code, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=dict(os.environ),
        check=False,
    )


# -- socket dir ----------------------------------------------------------------


def test_client_uses_server_default_sockdir(env):
    assert DaemonClient().sockdir == env.sockdir == server.default_sockdir()


def test_xdg_runtime_dir_unset_falls_back_to_private_cache_dir(env, monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    c = DaemonClient()
    assert c.sockdir == env.fallback_sockdir
    job_id = c.submit(SHORT_JOB)
    assert job_id
    st = os.lstat(env.fallback_sockdir)
    assert stat.S_ISDIR(st.st_mode)
    assert stat.S_IMODE(st.st_mode) == 0o700
    assert stat.S_ISSOCK(os.lstat(env.fallback_sockdir / server.SOCKET_NAME).st_mode)
    assert not env.sockdir.exists()
    assert len(env.daemons()) == 1


# -- submit spawns ---------------------------------------------------------------


def test_submit_with_no_daemon_spawns_one_and_returns_id_fast(env):
    assert env.daemons() == []
    c = DaemonClient()
    t0 = time.monotonic()
    job_id = c.submit(SHORT_JOB)
    elapsed = time.monotonic() - t0
    print(f"submit-with-spawn latency: {elapsed * 1000:.0f} ms")
    assert isinstance(job_id, str)
    assert job_id
    assert elapsed < 1.0, f"submit took {elapsed:.3f}s"
    assert len(env.daemons()) == 1
    assert c.ping() is True
    assert env.log.exists()  # detached daemon's stdio goes to the log
    # a second submit reuses the daemon
    t0 = time.monotonic()
    second = c.submit(SHORT_JOB)
    assert time.monotonic() - t0 < 1.0
    assert second != job_id
    assert len(env.daemons()) == 1


@needs_ffmpeg
def test_submitted_job_runs_to_done(env):
    c = DaemonClient()
    job_id = c.submit(SHORT_JOB)
    rec = wait_for(lambda: (r := c.status(job_id))["job"]["state"] in ("done", "failed") and r)
    assert rec["via"] == "daemon"
    assert rec["job"]["state"] == "done", rec
    res = c.result(job_id)
    assert res["ready"] is True
    assert res["via"] == "daemon"


def test_ten_concurrent_threaded_submits_one_daemon(env):
    barrier = threading.Barrier(10)
    ids: list[str] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker():
        try:
            barrier.wait(WAIT)
            job_id = DaemonClient().submit(LONG_JOB)
            with lock:
                ids.append(job_id)
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(WAIT)
    assert errors == []
    assert len(ids) == 10
    assert len(set(ids)) == 10
    assert len(env.daemons()) == 1


SUBMIT_CODE = (
    "import json, sys\n"
    "from media_cli.media.daemon.client import DaemonClient\n"
    "print(DaemonClient().submit(json.loads(sys.argv[1])))\n"
)


def test_ten_concurrent_process_submits_one_daemon(env):
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", SUBMIT_CODE, json.dumps(LONG_JOB)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=dict(os.environ),
        )
        for _ in range(10)
    ]
    ids = []
    for p in procs:
        out, err = p.communicate(timeout=WAIT)
        assert p.returncode == 0, err
        assert err == ""
        ids.append(out.strip())
    assert len(set(ids)) == 10
    assert all(ids)
    assert len(env.daemons()) == 1


def test_stale_socket_file_is_replaced(env):
    server.ensure_sockdir(env.sockdir)
    sock_path = env.sockdir / server.SOCKET_NAME
    dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    dead.bind(str(sock_path))
    dead.close()  # file remains; nobody listens -> connect refused
    c = DaemonClient()
    assert c.ping() is False
    job_id = c.submit(SHORT_JOB)
    assert job_id
    assert stat.S_ISSOCK(os.lstat(sock_path).st_mode)
    assert c.ping() is True
    assert len(env.daemons()) == 1


def test_submit_retries_when_daemon_idle_exits(env, monkeypatch):
    c = DaemonClient()
    c.submit(SHORT_JOB)
    pid = env.daemons()[0]
    # simulate the idle-exit race: the daemon disappears between two submits
    os.kill(pid, signal.SIGTERM)
    wait_for(lambda: not pid_alive(pid))
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass
    job_id = c.submit(SHORT_JOB)
    assert job_id
    assert len(env.daemons()) == 1


def test_submit_reports_typed_error_from_daemon(env):
    c = DaemonClient()
    with pytest.raises(MediaInputError) as ei:
        c.submit({"kind": "no-such-kind"})
    assert ei.value.kind == "input.unknown_job_kind"


def test_submit_spawn_failure_is_typed_env_error(env, monkeypatch):
    # a daemon that cannot start (unsafe sockdir) -> typed env error, no hang
    monkeypatch.setattr(client_mod, "SPAWN_TIMEOUT", 1.0)
    env.sockdir.mkdir(mode=0o700)
    os.chmod(env.sockdir, 0o755)
    client = DaemonClient()
    with pytest.raises(MediaEnvError):
        client.submit(SHORT_JOB)
    assert env.daemons() == []


# -- no daemon: read the store, never spawn (o15) ---------------------------------


def _assert_nothing_created(env: Env) -> None:
    assert not env.sockdir.exists()
    assert not env.fallback_sockdir.exists()
    assert env.daemons() == []


def test_read_ops_with_no_daemon_use_store_and_never_spawn(env):
    store = JobStore()
    rec = store.create("ffmpeg", ["media", "x"])
    c = DaemonClient()
    assert c.ping() is False
    st = c.status(rec.id)
    assert st["via"] == "store"
    assert st["job"]["id"] == rec.id
    assert st["job"]["state"] == "queued"
    res = c.result(rec.id)
    assert res["via"] == "store"
    assert res["ready"] is False
    assert res["output"] is None
    assert [j["id"] for j in c.list_jobs()] == [rec.id]
    can = c.cancel(rec.id)
    assert can["via"] == "store"
    assert can["job"]["state"] == "cancelled"
    assert store.get(rec.id).state == "cancelled"
    res = c.result(rec.id)
    assert res["ready"] is True
    _assert_nothing_created(env)


def test_read_ops_with_no_daemon_and_no_runtime_dir_never_create_fallback(env, monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    rec = JobStore().create("ffmpeg", [])
    c = DaemonClient()
    assert c.ping() is False
    assert c.status(rec.id)["via"] == "store"
    c.result(rec.id)
    c.cancel(rec.id)
    _assert_nothing_created(env)


def test_no_daemon_errors_are_typed(env):
    store = JobStore()
    c = DaemonClient()
    with pytest.raises(MediaInputError) as ei:
        c.status("nope")
    assert ei.value.kind == "input.job_not_found"
    done = store.create("ffmpeg", [])
    store.update(done.id, state="running")
    store.update(done.id, state="done")
    with pytest.raises(MediaInputError) as ei:
        c.cancel(done.id)
    assert ei.value.kind == "input.job_illegal_transition"
    running = store.create("ffmpeg", [])
    store.update(running.id, state="running")
    with pytest.raises(MediaEnvError) as ee:
        c.cancel(running.id)
    assert ee.value.kind == client_mod.ENV_DAEMON_NOT_RUNNING
    assert store.get(running.id).state == "running"
    _assert_nothing_created(env)


def test_read_ops_with_stale_socket_do_not_spawn(env):
    server.ensure_sockdir(env.sockdir)
    dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    dead.bind(str(env.sockdir / server.SOCKET_NAME))
    dead.close()
    rec = JobStore().create("ffmpeg", [])
    c = DaemonClient()
    assert c.ping() is False
    assert c.status(rec.id)["via"] == "store"
    assert c.cancel(rec.id)["via"] == "store"
    assert env.daemons() == []


@pytest.mark.requires_ffmpeg
def test_ops_go_through_running_daemon(env):
    c = DaemonClient()
    first = c.submit(LONG_JOB)
    queued = c.submit(LONG_JOB)  # max_concurrent=1 -> stays queued
    assert c.status(queued)["via"] == "daemon"
    can = c.cancel(queued)
    assert can["via"] == "daemon"
    assert can["job"]["state"] == "cancelled"
    can = c.cancel(first)
    assert can["job"]["state"] == "cancelled"


READ_OPS_CODE = (
    "import sys\n"
    "from media_cli.media.daemon.client import DaemonClient\n"
    "c = DaemonClient()\n"
    "assert c.ping() is False\n"
    "c.status(sys.argv[1]); c.result(sys.argv[1]); c.list_jobs(); c.cancel(sys.argv[1])\n"
)


def test_read_ops_in_fresh_process_never_spawn(env):
    rec = JobStore().create("ffmpeg", [])
    p = run_py(READ_OPS_CODE, rec.id)
    assert p.returncode == 0, p.stderr
    _assert_nothing_created(env)


@pytest.mark.parametrize(
    "argv",
    [
        ["--help"],
        ["whoami"],
        ["learn"],
        ["explain", "media"],
        ["overview"],
        ["doctor"],
        ["cli", "overview"],
    ],
)
def test_cli_verbs_never_create_a_daemon(env, argv):
    p = subprocess.run(
        [sys.executable, "-m", "media_cli", *argv],
        capture_output=True,
        text=True,
        timeout=WAIT,
        env=dict(os.environ),
        check=False,
    )
    assert p.returncode in (0, 1), p.stderr
    _assert_nothing_created(env)


# -- durability across processes -------------------------------------------------

POLL_CODE = (
    "import json, sys\n"
    "from media_cli.media.daemon.client import DaemonClient\n"
    "print(json.dumps(DaemonClient().status(sys.argv[1])))\n"
)


@needs_ffmpeg
def test_job_survives_submitter_exit_and_is_polled_from_fresh_process(env):
    sub = run_py(SUBMIT_CODE, json.dumps(SHORT_JOB))
    assert sub.returncode == 0, sub.stderr
    assert sub.stderr == ""
    job_id = sub.stdout.strip()
    assert job_id

    def done():
        p = run_py(POLL_CODE, job_id)
        assert p.returncode == 0, p.stderr
        st = json.loads(p.stdout)
        return st if st["job"]["state"] in ("done", "failed", "cancelled") else None

    st = wait_for(done, interval=0.2)
    assert st["job"]["state"] == "done", st
    assert st["via"] == "daemon"
    # with the daemon gone, a fresh process still reads the job from the store
    _kill_all(env)
    p = run_py(POLL_CODE, job_id)
    assert p.returncode == 0, p.stderr
    st = json.loads(p.stdout)
    assert st["via"] == "store"
    assert st["job"]["state"] == "done"


# -- python -m media_cli.media.daemon ----------------------------------------------


def test_module_entry_point_is_quiet_and_idle_exits(env):
    sockdir = env.sockdir
    p = subprocess.run(
        [
            sys.executable,
            "-m",
            "media_cli.media.daemon",
            "--sockdir",
            str(sockdir),
            "--idle-timeout",
            "0.3",
        ],
        capture_output=True,
        text=True,
        timeout=WAIT,
        env=dict(os.environ),
        check=False,
    )
    assert p.returncode == 0
    assert p.stdout == ""
    assert p.stderr == ""
    assert not (sockdir / server.SOCKET_NAME).exists()


def test_module_entry_point_losing_instance_exits_0_quietly(env):
    DaemonClient().submit(SHORT_JOB)
    p = subprocess.run(
        [sys.executable, "-m", "media_cli.media.daemon"],
        capture_output=True,
        text=True,
        timeout=WAIT,
        env=dict(os.environ),
        check=False,
    )
    assert (p.returncode, p.stdout, p.stderr) == (0, "", "")
    assert len(env.daemons()) == 1


def test_module_entry_point_bad_flag_exits_nonzero(env):
    p = subprocess.run(
        [sys.executable, "-m", "media_cli.media.daemon", "--max-concurrent", "zero"],
        capture_output=True,
        text=True,
        timeout=WAIT,
        env=dict(os.environ),
        check=False,
    )
    assert p.returncode != 0
    _assert_nothing_created(env)


# -- d13: socket path length and non-empty remediations --------------------------


def test_long_runtime_dir_fails_fast_without_spawning(env, monkeypatch):
    deep = env.base / ("d" * 60) / ("e" * 60)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(deep))
    client = DaemonClient()
    with pytest.raises(MediaEnvError) as ei:
        client.submit(SHORT_JOB)
    assert ei.value.kind == "env.socket_path_too_long"
    assert "XDG_RUNTIME_DIR" in ei.value.remediation
    assert "bytes" in ei.value.message
    assert env.daemons() == []
    assert not deep.exists()  # nothing was created either


def test_long_runtime_dir_read_ops_fall_back_to_store(env, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(env.base / ("d" * 120)))
    assert DaemonClient().ping() is False
    assert env.daemons() == []


def test_typed_errors_from_client_and_daemon_carry_remediation(env, monkeypatch):
    errors = []
    c = DaemonClient()
    with pytest.raises(MediaInputError) as ei:
        c.submit({"kind": "no-such-kind"})  # raised in the daemon, relayed over the wire
    errors.append(ei.value)
    with pytest.raises(MediaInputError) as ei:
        c.submit({"kind": "ffmpeg", "args": "nope"})
    errors.append(ei.value)
    with pytest.raises(MediaInputError) as ei:
        c.status("nope")
    errors.append(ei.value)
    store = JobStore()
    running = store.create("ffmpeg", [])
    store.update(running.id, state="running")
    for _ in range(1):
        monkeypatch.setattr(client_mod.DaemonClient, "_request", _always_unreachable)
        with pytest.raises(MediaEnvError) as ee:
            c.cancel(running.id)
        errors.append(ee.value)
    for exc in errors:
        assert exc.remediation, exc.kind


def _always_unreachable(self, payload, timeout=0):
    raise client_mod._Unreachable("x")


def test_raise_remote_never_yields_empty_remediation():
    for err in (
        {"kind": "env.daemon_internal", "message": "m"},
        {"kind": "input.bad_request", "message": "m", "remediation": ""},
        {"kind": "weird", "message": "m"},
        "not a dict",
    ):
        with pytest.raises((MediaEnvError, MediaInputError)) as ei:
            client_mod._raise_remote(err)
        assert ei.value.remediation


def test_unreachable_oserror_has_remediation(env, monkeypatch):
    env.sockdir.mkdir(mode=0o700)
    (env.sockdir / server.SOCKET_NAME).touch()

    class Boom(socket.socket):
        def connect(self, addr):
            raise OSError("boom")

    monkeypatch.setattr(client_mod.socket, "socket", Boom)
    client = DaemonClient()
    with pytest.raises(MediaEnvError) as ei:
        client.status("x")
    assert ei.value.kind == "env.daemon_unavailable"
    assert ei.value.remediation
