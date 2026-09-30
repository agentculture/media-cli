"""Client for the local media daemon: spawn on demand, never elsewhere.

Standard library only. It speaks the line-delimited JSON protocol documented in
:mod:`media_cli.media.daemon.server` over ``<sockdir>/daemon.sock``, one
connection per request.

Socket directory (decision c54)
-------------------------------
``sockdir`` defaults to :func:`server.default_sockdir` -- ``$XDG_RUNTIME_DIR/media-cli``,
else ``$XDG_CACHE_HOME/media-cli/run`` (``~/.cache/media-cli/run``). Only
:meth:`DaemonClient.submit` creates it (0700, via :func:`server.ensure_sockdir`).
Every op refuses an existing sockdir that is not a private directory we own.

Only submit may spawn (obligation o15)
--------------------------------------
:meth:`DaemonClient.submit` is the single code path that can start a daemon.
``ping``/``status``/``result``/``cancel``/``list_jobs`` never create the socket
directory, the socket or a process: with no daemon reachable they fall back to
the :class:`JobStore` on disk (the store *is* the contract; see ``jobs.py``).

* ``status`` / ``result`` read the record from the store (``"via": "store"``).
* ``cancel`` of a **queued** job marks it ``cancelled`` in the store. That is
  safe: the daemon's queue is in memory only, so a queued record with no daemon
  is an orphan nobody will ever run. ``cancel`` of a **running** job raises a
  typed :data:`ENV_DAEMON_NOT_RUNNING` error and leaves the record untouched --
  its process may still be alive and only a daemon can kill its group. A
  terminal job raises ``input.job_illegal_transition``, as the daemon does.
* ``list_jobs`` always reads the store (the protocol has no list op).

Spawn and locking (refines t20 criterion 1's "takes the lock")
-------------------------------------------------------------
The daemon holds ``<sockdir>/daemon.lock`` for its whole life -- that flock is
the single-instance guarantee and the client never touches it. Concurrent
*spawners* are serialized by a separate, short-lived ``<sockdir>/spawn.lock``
(``flock LOCK_EX``, polled with a bounded wait). Holding it, the client
re-checks with ``ping``; only if the daemon is still unreachable does it run
``[sys.executable, "-m", "media_cli.media.daemon"]`` detached
(``start_new_session=True`` i.e. setsid, cwd ``/``, stdin ``/dev/null``,
stdout+stderr appended to ``<state>/media-cli/daemon.log``, environment
inherited), then polls ``ping`` until it answers and releases the lock. A daemon
that lost the ``daemon.lock`` race (e.g. an old one still shutting down) exits 0
quietly; the client notices the child exited and spawns again, within the same
bounded budget (:data:`SPAWN_TIMEOUT`). A stale socket file (connect refused) is
removed by the new daemon while it holds ``daemon.lock``.

Idle-exit race: a daemon may idle-exit between our connect and its accept.
A connection refused/reset, broken pipe or EOF without a response is treated as
"no daemon" and submit goes back through the spawn path (bounded attempts).
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import socket
import stat
import subprocess  # nosec B404
import sys
import time
from pathlib import Path
from typing import Any

from media_cli.media.daemon import server
from media_cli.media.daemon.jobs import TERMINAL_STATES, JobStore, default_root
from media_cli.media.errors import MediaEnvError, MediaInputError

SPAWN_LOCK_NAME = "spawn.lock"
LOG_NAME = "daemon.log"
SPAWN_TIMEOUT = 10.0  # hard cap for start-up; the expected path is well under 1 s
REQUEST_TIMEOUT = 10.0
CANCEL_TIMEOUT = server.DEFAULT_KILL_GRACE + 20.0
SUBMIT_ATTEMPTS = 3
_POLL = 0.01

ENV_DAEMON_NOT_RUNNING = "env.daemon_not_running"
ENV_DAEMON_UNAVAILABLE = "env.daemon_unavailable"
ENV_DAEMON_PROTOCOL = "env.daemon_protocol"
INPUT_JOB_ILLEGAL_TRANSITION = "input.job_illegal_transition"

_UNREACHABLE_ERRNOS = frozenset(
    {errno.ENOENT, errno.ECONNREFUSED, errno.ECONNRESET, errno.EPIPE, errno.ENOTCONN}
)


# Spawned daemons we are the parent of. Kept referenced so ``Popen.__del__`` never
# warns about a still-running child, and pruned (reaped) on each spawn.
_SPAWNED: list[subprocess.Popen] = []


class _Unreachable(Exception):
    """No daemon answered on the socket (absent, stale, or exited mid-request)."""


def _raise_remote(error: Any) -> None:
    kind = error.get("kind", "") if isinstance(error, dict) else ""
    message = error.get("message", "daemon error") if isinstance(error, dict) else str(error)
    if isinstance(kind, str) and kind.startswith("input."):
        raise MediaInputError(kind, message)
    if isinstance(kind, str) and kind.startswith("env."):
        raise MediaEnvError(kind, message)
    raise MediaEnvError(ENV_DAEMON_PROTOCOL, f"daemon error without a typed kind: {message}")


class DaemonClient:
    """Talk to (and, from :meth:`submit` only, start) the local media daemon."""

    def __init__(self, sockdir: Path | str | None = None, store: JobStore | None = None) -> None:
        self.sockdir = Path(sockdir) if sockdir is not None else server.default_sockdir()
        self.socket_path = self.sockdir / server.SOCKET_NAME
        self._store = store

    # -- store ---------------------------------------------------------------------
    @property
    def store(self) -> JobStore:
        if self._store is None:
            self._store = JobStore()
        return self._store

    @property
    def jobs_root(self) -> Path:
        return self._store.root if self._store is not None else default_root()

    @property
    def log_path(self) -> Path:
        return self.jobs_root.parent / LOG_NAME

    # -- transport -----------------------------------------------------------------
    def _check_sockdir(self) -> bool:
        """True if the sockdir exists and is safe; False if absent. Never creates it."""
        try:
            st = os.lstat(self.sockdir)
        except FileNotFoundError:
            return False
        if (
            not stat.S_ISDIR(st.st_mode)
            or st.st_uid != os.getuid()
            or stat.S_IMODE(st.st_mode) & 0o077
        ):
            server.ensure_sockdir(self.sockdir)  # exists -> only verifies; raises typed
        return True

    def _request(self, payload: dict[str, Any], timeout: float = REQUEST_TIMEOUT) -> dict:
        """One request on a fresh connection. Raises :class:`_Unreachable`."""
        if not self._check_sockdir():
            raise _Unreachable(str(self.sockdir))
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"
        buf = b""
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(timeout)
                s.connect(str(self.socket_path))
                s.sendall(data)
                while not buf.endswith(b"\n"):
                    if len(buf) > server.MAX_REQUEST_BYTES * 64:
                        raise MediaEnvError(ENV_DAEMON_PROTOCOL, "daemon response too large")
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
        except socket.timeout:
            raise MediaEnvError(
                ENV_DAEMON_UNAVAILABLE,
                f"the media daemon did not answer within {timeout:g}s",
                f"see {self.log_path}",
            ) from None
        except OSError as exc:
            if exc.errno in _UNREACHABLE_ERRNOS:
                raise _Unreachable(str(exc)) from None
            raise MediaEnvError(
                ENV_DAEMON_UNAVAILABLE, f"cannot reach the media daemon: {exc}", ""
            ) from None
        if not buf:
            raise _Unreachable("connection closed without a response")
        try:
            resp = json.loads(buf)
        except ValueError as exc:
            raise MediaEnvError(ENV_DAEMON_PROTOCOL, f"malformed daemon response: {exc}") from None
        if not isinstance(resp, dict):
            raise MediaEnvError(ENV_DAEMON_PROTOCOL, "daemon response is not an object")
        if not resp.get("ok"):
            _raise_remote(resp.get("error"))
        return resp

    def _ping_pid(self) -> int | None:
        try:
            resp = self._request({"op": "ping"}, timeout=2.0)
        except _Unreachable:
            return None
        pid = resp.get("pid")
        return pid if isinstance(pid, int) else 0

    # -- spawn (submit only) -------------------------------------------------------
    def _spawn(self) -> subprocess.Popen:
        log = self.log_path
        log.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        argv = [sys.executable, "-m", "media_cli.media.daemon", "--sockdir", str(self.sockdir)]
        if self._store is not None:
            argv += ["--jobs-root", str(self._store.root)]
        _SPAWNED[:] = [p for p in _SPAWNED if p.poll() is None]
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC, 0o600)
        try:
            # our own python module: fixed argv list, no shell
            child = subprocess.Popen(  # nosec B603
                argv,
                stdin=subprocess.DEVNULL,
                stdout=fd,
                stderr=fd,
                cwd="/",
                start_new_session=True,
                close_fds=True,
            )
        finally:
            os.close(fd)
        _SPAWNED.append(child)
        return child

    def _lock_spawn(self, deadline: float) -> int:
        path = self.sockdir / SPAWN_LOCK_NAME
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise MediaEnvError(
                        ENV_DAEMON_UNAVAILABLE,
                        f"timed out waiting for {path}",
                        f"another client is starting the daemon; see {self.log_path}",
                    ) from None
                time.sleep(_POLL)
            except BaseException:
                os.close(fd)
                raise

    def _ensure_daemon(self, deadline: float) -> None:
        """Start the daemon unless one answers. Serialized by ``spawn.lock``."""
        server.ensure_sockdir(self.sockdir)
        fd = self._lock_spawn(deadline)
        try:
            if self._ping_pid() is not None:
                return
            child = self._spawn()
            while time.monotonic() < deadline:
                if self._ping_pid() is not None:
                    return
                if child.poll() is not None:
                    # lost the daemon.lock race (old daemon shutting down) or failed
                    if child.returncode != 0:
                        break
                    child = self._spawn()
                time.sleep(_POLL)
            raise MediaEnvError(
                ENV_DAEMON_UNAVAILABLE,
                "the media daemon did not start",
                f"see {self.log_path}",
            )
        finally:
            os.close(fd)  # releases spawn.lock

    # -- public API ----------------------------------------------------------------
    def ping(self) -> bool:
        """True if a daemon answers. Never spawns or creates anything."""
        return self._ping_pid() is not None

    def submit(self, job: dict[str, Any]) -> str:
        """Queue ``job`` (see the server's job spec); the job id.

        The only method that may start a daemon.
        """
        deadline = time.monotonic() + SPAWN_TIMEOUT
        payload = {"op": "submit", "job": job}
        for _ in range(SUBMIT_ATTEMPTS):
            try:
                resp = self._request(payload)
            except _Unreachable:
                self._ensure_daemon(deadline)
                continue
            job_id = resp.get("job_id")
            if not isinstance(job_id, str) or not job_id:
                raise MediaEnvError(ENV_DAEMON_PROTOCOL, "daemon submit response has no job_id")
            return job_id
        raise MediaEnvError(
            ENV_DAEMON_UNAVAILABLE,
            "the media daemon kept going away while submitting",
            f"see {self.log_path}",
        )

    def status(self, job_id: str) -> dict[str, Any]:
        """``{"job": <record>, "via": "daemon"|"store"}``."""
        try:
            resp = self._request({"op": "status", "job_id": job_id})
            return {"job": resp.get("job"), "via": "daemon"}
        except _Unreachable:
            return {"job": self.store.get(job_id).to_dict(), "via": "store"}

    def result(self, job_id: str) -> dict[str, Any]:
        """``{"job", "ready", "output", "via"}`` -- ``output`` only once done."""
        try:
            resp = self._request({"op": "result", "job_id": job_id})
            return {
                "job": resp.get("job"),
                "ready": bool(resp.get("ready")),
                "output": resp.get("output"),
                "via": "daemon",
            }
        except _Unreachable:
            rec = self.store.get(job_id)
            return {
                "job": rec.to_dict(),
                "ready": rec.state in TERMINAL_STATES,
                "output": rec.output if rec.state == "done" else None,
                "via": "store",
            }

    def cancel(self, job_id: str) -> dict[str, Any]:
        """``{"job": <record>, "via": ...}``; see the module docstring for no-daemon rules."""
        try:
            resp = self._request({"op": "cancel", "job_id": job_id}, timeout=CANCEL_TIMEOUT)
            return {"job": resp.get("job"), "via": "daemon"}
        except _Unreachable:
            pass
        rec = self.store.get(job_id)
        if rec.state in TERMINAL_STATES:
            raise MediaInputError(
                INPUT_JOB_ILLEGAL_TRANSITION,
                f"job {job_id} is already {rec.state}",
                "only queued or running jobs can be cancelled",
            )
        if rec.state == "running":
            raise MediaEnvError(
                ENV_DAEMON_NOT_RUNNING,
                f"job {job_id} is recorded as running but no media daemon is reachable",
                f"its process may still be running (meta.pid={rec.meta.get('pid')}); "
                "check it and kill it by hand",
            )
        rec = self.store.update(job_id, state="cancelled")
        return {"job": rec.to_dict(), "via": "store"}

    def list_jobs(self) -> list[dict[str, Any]]:
        """Every job record, oldest first, read from the store."""
        return [rec.to_dict() for rec in self.store.list()]
