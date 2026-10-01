"""Local media daemon: a single-instance, bounded job runner on a unix socket.

Standard library only. It serves line-delimited JSON on ``<sockdir>/daemon.sock``
to local peers of the same uid, runs at most ``max_concurrent`` jobs at once
(FIFO), and exits after ``idle_timeout`` seconds with nothing queued, running or
connected. It registers nothing on the Culture mesh and opens no network socket.

Security (obligation o13)
-------------------------
``sockdir`` is created mode 0700; an existing one must be a real directory
(not a symlink) owned by us with no group/other bits, or startup refuses. The
socket is bound under umask 0177 and chmod 0600 right after. Every accepted
connection's ``SO_PEERCRED`` uid is checked *before any byte is read*; a
different uid is closed immediately with no response.

Single instance
---------------
An exclusive ``flock`` on ``<sockdir>/daemon.lock`` is held for the process
lifetime. A losing spawner gets :class:`DaemonAlreadyRunning` (``serve`` turns
that into a quiet exit 0). A leftover socket file is unlinked only while
holding the lock, and only after a connect proves nobody listens on it.

Protocol (one JSON object per line in, one per line out)
--------------------------------------------------------
``{"op": "ping"}`` -> ``{"ok": true, "pid": <daemon pid>}``
``{"op": "submit", "job": {...}}`` -> ``{"ok": true, "job_id": "..."}``
``{"op": "status", "job_id": "..."}`` -> ``{"ok": true, "job": <record>}``
``{"op": "result", "job_id": "..."}`` ->
    ``{"ok": true, "job": <record>, "ready": <terminal?>, "output": <path|null>}``
``{"op": "cancel", "job_id": "..."}`` -> ``{"ok": true, "job": <record>}`` once the
    job is terminal (a queued job is cancelled without ever running; a running
    one has its process group killed first).
Errors -> ``{"ok": false, "error": {"kind": "...", "message": "..."}}``. Requests
longer than :data:`MAX_REQUEST_BYTES` are refused (``input.request_too_large``)
and the connection is closed.

Job spec (``submit``'s ``job``)
-------------------------------
Common keys: ``kind`` (a registered handler; unknown kinds are refused at
submit), optional ``argv`` (the originating command, list of str), ``output``
(str) and ``meta`` (object). Every other key is kept as ``record.meta["spec"]``
for the handler. Kind ``"ffmpeg"`` requires ``args`` (ffmpeg arguments without
the binary) and accepts ``tmp_output``, ``duration`` (seconds, for
``progress.fraction``) and ``overwrite`` (bool, default false). The daemon adds
``-nostats -progress pipe:1`` (as leading global options), starts ffmpeg via :func:`media_cli.media._tools.spawn`
in its own session/process group, streams stderr to the job log, parses progress
blocks into ``record.progress`` and records ``meta.pid``/``meta.pgid``. Success
with ``tmp_output`` and ``output`` publishes the result: without ``overwrite``
it hard-links ``tmp_output`` to ``output`` then unlinks the temp (never
clobbering a path that appeared after planning; an existing ``output`` fails the
job with kind ``input.output_exists`` and is left untouched); with
``overwrite`` it does ``os.replace``. Failure or cancel removes ``tmp_output``.

Other kinds plug in through :func:`register_handler`; a handler is
``fn(record, store, cancel_event) -> dict | None`` whose returned dict may set
``output``/``progress``/``meta`` on the finished record. It should return
promptly once ``cancel_event`` is set; raising :class:`JobFailed` (or any
exception) fails the job.

Lifecycle (obligation o14)
--------------------------
``cancel`` on a running ffmpeg job sends SIGTERM to its process group and
SIGKILL after ``kill_grace`` seconds. SIGTERM/SIGINT to the daemon (or idle
exit) stops accepting, cancels every queued job, kills every running group,
waits for the workers, removes the socket and releases the lock.
"""

from __future__ import annotations

import collections
import fcntl
import json
import os
import selectors
import signal
import socket
import stat
import struct
import subprocess  # constants/types only: ffmpeg is started via _tools.spawn (o2)
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from media_cli.media import _tools
from media_cli.media.daemon.jobs import TERMINAL_STATES, JobRecord, JobStore
from media_cli.media.errors import MediaEnvError, MediaInputError

SOCKET_NAME = "daemon.sock"
LOCK_NAME = "daemon.lock"
DEFAULT_MAX_CONCURRENT = 1
DEFAULT_IDLE_TIMEOUT = 300.0
DEFAULT_KILL_GRACE = 3.0
MAX_REQUEST_BYTES = 1 << 20
CONNECTION_TIMEOUT = 60.0
_TICK = 0.2
_SUN_PATH_MAX = 107  # sizeof(sun_path) - 1 on Linux
_UCRED = struct.Struct("3i")  # pid, uid, gid

# Protocol error kinds
INPUT_BAD_REQUEST = "input.bad_request"
INPUT_REQUEST_TOO_LARGE = "input.request_too_large"
INPUT_UNKNOWN_OP = "input.unknown_op"
INPUT_UNKNOWN_JOB_KIND = "input.unknown_job_kind"
INPUT_JOB_ILLEGAL_TRANSITION = "input.job_illegal_transition"
ENV_DAEMON_SETUP = "env.daemon_setup"
ENV_DAEMON_STOPPING = "env.daemon_stopping"
ENV_DAEMON_INTERNAL = "env.daemon_internal"
ENV_JOB_FAILED = "env.job_failed"
INPUT_OUTPUT_EXISTS = "input.output_exists"  # same string as media.output

Handler = Callable[[JobRecord, JobStore, threading.Event], "dict[str, Any] | None"]
_RESULT_FIELDS = frozenset({"output", "progress", "meta"})


class DaemonAlreadyRunning(Exception):
    """Another daemon holds the instance lock for this sockdir."""


class DaemonSetupError(MediaEnvError):
    """The socket directory or socket cannot be set up safely (exit 2)."""

    def __init__(self, message: str, remediation: str = "") -> None:
        super().__init__(ENV_DAEMON_SETUP, message, remediation)


class JobFailed(Exception):
    """Raised by a handler: the job failed; ``stderr`` is kept on the record."""

    def __init__(self, message: str, stderr: str = "", kind: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.stderr = stderr
        self.kind = kind  # typed error kind; None keeps the default for the job kind


class JobCancelled(Exception):
    """Raised by a handler that stopped because its cancel event was set."""


# -- handler registry ------------------------------------------------------------

_HANDLERS: dict[str, Handler] = {}


def register_handler(kind: str, fn: Handler) -> None:
    """Register a non-ffmpeg job kind (e.g. the search indexer)."""
    if not isinstance(kind, str) or not kind:
        raise ValueError("job kind must be a non-empty string")
    if kind == "ffmpeg":
        raise ValueError("the 'ffmpeg' job kind is built in and cannot be replaced")
    _HANDLERS[kind] = fn


# -- paths -----------------------------------------------------------------------


def default_sockdir() -> Path:
    """``$XDG_RUNTIME_DIR/media-cli``; else ``$XDG_CACHE_HOME/media-cli/run``
    (``~/.cache/media-cli/run`` when ``XDG_CACHE_HOME`` is unset)."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / "media-cli"
    cache = os.environ.get("XDG_CACHE_HOME")
    base = Path(cache) if cache else Path.home() / ".cache"
    return base / "media-cli" / "run"


def ensure_sockdir(sockdir: Path) -> Path:
    """Create ``sockdir`` 0700 (and any missing parents), or verify an existing
    one is a private directory we own."""
    sockdir = Path(sockdir)
    sockdir.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.mkdir(sockdir, 0o700)
        os.chmod(sockdir, 0o700)  # a restrictive umask may have removed owner bits
    except FileExistsError:
        pass
    st = os.lstat(sockdir)
    if not stat.S_ISDIR(st.st_mode):
        raise DaemonSetupError(
            f"daemon socket directory {sockdir} is not a directory (symlink?)",
            "remove it or choose another socket directory",
        )
    if st.st_uid != os.getuid():
        raise DaemonSetupError(
            f"daemon socket directory {sockdir} is owned by uid {st.st_uid}, not {os.getuid()}",
            "remove it or choose another socket directory",
        )
    if stat.S_IMODE(st.st_mode) & 0o077:
        raise DaemonSetupError(
            f"daemon socket directory {sockdir} has mode "
            f"{stat.S_IMODE(st.st_mode):04o}; it must be 0700",
            f"chmod 700 {sockdir}",
        )
    return sockdir


def peer_uid(conn: socket.socket) -> int:
    """The uid of the process on the other end of a unix stream socket."""
    raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _UCRED.size)
    _pid, uid, _gid = _UCRED.unpack(raw)
    return uid


# -- ffmpeg progress -------------------------------------------------------------


class ProgressParser:
    """Turn ``ffmpeg -progress`` key=value lines into one dict per block."""

    def __init__(self, duration: float | None = None) -> None:
        self.duration = duration if duration and duration > 0 else None
        self._cur: dict[str, str] = {}

    @staticmethod
    def _int(value: str | None) -> int | None:
        try:
            return int(value) if value is not None else None
        except ValueError:
            return None

    @staticmethod
    def _float(value: str | None) -> float | None:
        try:
            return float(value) if value is not None else None
        except ValueError:
            return None

    def feed(self, line: str) -> dict[str, Any] | None:
        """Feed one line; return the block summary when ``progress=`` closes it."""
        key, sep, value = line.strip().partition("=")
        if not sep:
            return None
        key, value = key.strip(), value.strip()
        if key != "progress":
            self._cur[key] = value
            return None
        cur, self._cur = self._cur, {}
        # out_time_ms is (historically) microseconds too; use it as a fallback.
        us = self._int(cur.get("out_time_us"))
        if us is None:
            us = self._int(cur.get("out_time_ms"))
        fraction = None
        if value == "end":
            fraction = 1.0
        elif self.duration and us is not None:
            fraction = max(0.0, min(1.0, us / 1e6 / self.duration))
        return {
            "progress": value,
            "out_time_us": us,
            "out_time": cur.get("out_time"),
            "frame": self._int(cur.get("frame")),
            "fps": self._float(cur.get("fps")),
            "speed": cur.get("speed"),
            "total_size": self._int(cur.get("total_size")),
            "fraction": fraction,
        }


def _remove(path: str | None) -> None:
    if path:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _kill_group(proc: subprocess.Popen, pgid: int, grace: float) -> None:
    """SIGTERM the process group, SIGKILL it after ``grace`` seconds."""
    if proc.poll() is not None:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(grace)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            return
        proc.wait()


def _publish(tmp: str, output: str, overwrite: bool) -> None:
    """Move ``tmp`` to ``output`` with output.commit's no-clobber semantics.

    Without ``overwrite`` an existing ``output`` is never touched (link() refuses
    it atomically); ``tmp`` is always removed on failure.
    """
    try:
        if overwrite:
            os.replace(tmp, output)
            return
        try:
            os.link(tmp, output)
        except FileExistsError:
            raise JobFailed(
                f"output already exists: {output}",
                "",
                INPUT_OUTPUT_EXISTS,
            ) from None
        except OSError:
            if os.path.lexists(output):  # e.g. dangling symlink or other race
                raise JobFailed(
                    f"output already exists: {output}", "", INPUT_OUTPUT_EXISTS
                ) from None
            os.rename(tmp, output)  # fs without hard links; tmp is gone after this
        _remove(tmp)
    except BaseException:
        _remove(tmp)
        raise


def _make_ffmpeg_handler(kill_grace: float) -> Handler:
    def run_ffmpeg(rec: JobRecord, store: JobStore, cancel_event: threading.Event):
        spec = rec.meta.get("spec", {})
        tmp_output = spec.get("tmp_output")
        argv = ["-nostats", "-progress", "pipe:1", *spec["args"]]
        if cancel_event.is_set():
            _remove(tmp_output)
            raise JobCancelled()
        proc = _tools.spawn(
            "ffmpeg",
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        pgid = proc.pid  # start_new_session => it leads its own group
        done = threading.Event()
        tail: collections.deque[str] = collections.deque(maxlen=200)

        def pump_stderr() -> None:
            for raw in proc.stderr:  # type: ignore[union-attr]
                text = raw.decode("utf-8", errors="replace")
                tail.append(text)
                try:
                    store.append_log(rec.id, text)
                except OSError:
                    pass

        def watch_cancel() -> None:
            while not done.is_set():
                if cancel_event.wait(_TICK):
                    _kill_group(proc, pgid, kill_grace)
                    return

        threads = [
            threading.Thread(target=pump_stderr, daemon=True, name=f"stderr-{rec.id}"),
            threading.Thread(target=watch_cancel, daemon=True, name=f"cancel-{rec.id}"),
        ]
        try:
            store.update(rec.id, meta={**rec.meta, "pid": proc.pid, "pgid": pgid})
            for t in threads:
                t.start()
            parser = ProgressParser(spec.get("duration"))
            last = None
            for raw in proc.stdout:  # type: ignore[union-attr]
                block = parser.feed(raw.decode("utf-8", errors="replace"))
                if block is not None:
                    last = block
                    store.update(rec.id, progress=block)
            rc = proc.wait()
        except BaseException:
            _kill_group(proc, pgid, kill_grace)
            raise
        finally:
            done.set()
            for t in threads:
                if t.ident is not None:
                    t.join(kill_grace + 5)
        if cancel_event.is_set():
            _remove(tmp_output)
            raise JobCancelled()
        if rc != 0:
            _remove(tmp_output)
            raise JobFailed(f"ffmpeg failed (exit {rc})", "".join(tail))
        result: dict[str, Any] = {}
        if tmp_output and rec.output:
            _publish(tmp_output, rec.output, bool(spec.get("overwrite", False)))
        if last is not None:
            result["progress"] = {**last, "fraction": 1.0}
        return result

    return run_ffmpeg


# -- the daemon ------------------------------------------------------------------


class Daemon:
    """One daemon instance. :meth:`run` blocks until stop, SIGTERM or idle exit."""

    def __init__(
        self,
        sockdir: Path | str | None = None,
        *,
        store: JobStore | None = None,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT,
        idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
        kill_grace: float = DEFAULT_KILL_GRACE,
        install_signals: bool = True,
    ) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be >= 1")
        self.sockdir = Path(sockdir) if sockdir is not None else default_sockdir()
        self.socket_path = self.sockdir / SOCKET_NAME
        self.lock_path = self.sockdir / LOCK_NAME
        self.store = store
        self.max_concurrent = max_concurrent
        self.idle_timeout = idle_timeout
        self.kill_grace = kill_grace
        self.install_signals = install_signals
        self.expected_uid = os.getuid()
        self.rejected_peers = 0
        self._stop = threading.Event()
        self._cv = threading.Condition(threading.Lock())
        self._queue: collections.deque[str] = collections.deque()
        self._active: dict[str, threading.Event] = {}
        self._connections = 0
        self._last_activity = time.monotonic()
        self._accepting = False
        self._ffmpeg = _make_ffmpeg_handler(kill_grace)

    # -- public -------------------------------------------------------------------
    @property
    def accepting(self) -> bool:
        """True once the socket is bound and workers are running."""
        return self._accepting

    def stop(self) -> None:
        """Ask :meth:`run` to shut down (thread-safe, idempotent)."""
        self._stop.set()

    def run(self) -> int:
        """Acquire the lock, serve until stopped, clean up. Returns 0."""
        ensure_sockdir(self.sockdir)
        if len(os.fsencode(str(self.socket_path))) > _SUN_PATH_MAX:
            raise DaemonSetupError(
                f"socket path {self.socket_path} is too long for a unix socket",
                "use a shorter socket directory",
            )
        lock_fd = self._acquire_lock()
        listener = None
        workers: list[threading.Thread] = []
        restore: dict[int, Any] = {}
        try:
            self._clear_stale_socket()
            listener = self._bind()
            if self.store is None:
                self.store = JobStore()
            if self.install_signals and threading.current_thread() is threading.main_thread():
                for sig in (signal.SIGTERM, signal.SIGINT):
                    restore[sig] = signal.signal(sig, lambda *_: self._stop.set())
            for n in range(self.max_concurrent):
                t = threading.Thread(target=self._worker, name=f"media-worker-{n}", daemon=True)
                t.start()
                workers.append(t)
            self._accepting = True
            self._serve(listener)
            return 0
        finally:
            self._accepting = False
            self._stop.set()
            if listener is not None:
                self._close_listener(listener)
            self._shutdown_jobs(workers)
            for sig, old in restore.items():
                signal.signal(sig, old)
            os.close(lock_fd)  # releases the flock

    # -- setup --------------------------------------------------------------------
    def _acquire_lock(self) -> int:
        flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
        fd = os.open(self.lock_path, flags, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise DaemonAlreadyRunning(str(self.lock_path)) from None
        except BaseException:
            os.close(fd)
            raise
        try:
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode())
        except OSError:
            pass
        return fd

    def _clear_stale_socket(self) -> None:
        """Only called with the lock held."""
        try:
            st = os.lstat(self.socket_path)
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(st.st_mode):
            raise DaemonSetupError(
                f"{self.socket_path} exists and is not a socket", f"remove {self.socket_path}"
            )
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(1.0)
        try:
            probe.connect(str(self.socket_path))
        except OSError:
            os.unlink(self.socket_path)  # stale: nobody listens
            return
        finally:
            probe.close()
        raise DaemonAlreadyRunning(f"a listener already serves {self.socket_path}")

    def _bind(self) -> socket.socket:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            old = os.umask(0o177)
            try:
                listener.bind(str(self.socket_path))
            finally:
                os.umask(old)
            os.chmod(self.socket_path, 0o600)
            self._socket_ino = os.lstat(self.socket_path).st_ino
            listener.listen(64)
            listener.setblocking(False)
        except BaseException:
            listener.close()
            raise
        return listener

    def _close_listener(self, listener: socket.socket) -> None:
        listener.close()
        try:
            if os.lstat(self.socket_path).st_ino == self._socket_ino:
                os.unlink(self.socket_path)
        except (FileNotFoundError, AttributeError):
            pass

    # -- accept loop --------------------------------------------------------------
    def _touch(self) -> None:
        self._last_activity = time.monotonic()

    def _idle(self) -> bool:
        with self._cv:
            busy = bool(self._queue or self._active or self._connections)
            if busy:
                self._touch()
                return False
            return time.monotonic() - self._last_activity >= self.idle_timeout

    def _serve(self, listener: socket.socket) -> None:
        self._touch()
        with selectors.DefaultSelector() as sel:
            sel.register(listener, selectors.EVENT_READ)
            while not self._stop.is_set():
                if sel.select(timeout=_TICK):
                    self._accept(listener)
                if self._idle():
                    return

    def _accept(self, listener: socket.socket) -> None:
        try:
            conn, _ = listener.accept()
        except (BlockingIOError, InterruptedError):
            return
        try:
            uid = peer_uid(conn)
        except OSError:
            uid = -1
        if uid != self.expected_uid:
            self.rejected_peers += 1
            conn.close()  # refused before a single byte is read
            return
        with self._cv:
            self._connections += 1
            self._touch()
        threading.Thread(target=self._serve_conn, args=(conn,), daemon=True).start()

    def _serve_conn(self, conn: socket.socket) -> None:
        try:
            conn.setblocking(True)
            conn.settimeout(CONNECTION_TIMEOUT)
            rfile = conn.makefile("rb")
            while not self._stop.is_set():
                line = rfile.readline(MAX_REQUEST_BYTES + 1)
                if not line:
                    return
                if len(line) > MAX_REQUEST_BYTES and not line.endswith(b"\n"):
                    err = _error(
                        INPUT_REQUEST_TOO_LARGE,
                        f"request exceeds {MAX_REQUEST_BYTES} bytes",
                    )
                    conn.sendall(_encode(err))
                    self._drain(conn, rfile)
                    return
                conn.sendall(_encode(self._handle_line(line)))
                self._touch()
        except OSError:
            return
        finally:
            try:
                conn.close()
            finally:
                with self._cv:
                    self._connections -= 1
                    self._touch()

    @staticmethod
    def _drain(conn: socket.socket, rfile: Any) -> None:
        """Discard what the peer is still sending so close() doesn't reset it."""
        conn.shutdown(socket.SHUT_WR)
        conn.settimeout(2.0)
        budget = 16 * MAX_REQUEST_BYTES
        while budget > 0:
            chunk = rfile.read1(65536)
            if not chunk:
                return
            budget -= len(chunk)

    # -- request handling ---------------------------------------------------------
    def _handle_line(self, line: bytes) -> dict[str, Any]:
        try:
            req = json.loads(line)
        except (ValueError, UnicodeDecodeError) as exc:
            return _error(INPUT_BAD_REQUEST, f"malformed JSON: {exc}")
        if not isinstance(req, dict) or not isinstance(req.get("op"), str):
            return _error(INPUT_BAD_REQUEST, "request must be an object with a string 'op'")
        op = req["op"]
        ops = {
            "ping": self._op_ping,
            "submit": self._op_submit,
            "status": self._op_status,
            "result": self._op_result,
            "cancel": self._op_cancel,
        }
        fn = ops.get(op)
        if fn is None:
            return _error(INPUT_UNKNOWN_OP, f"unknown op {op!r}; known: {sorted(ops)}")
        try:
            return fn(req)
        except (MediaInputError, MediaEnvError) as exc:
            return _error(exc.kind, exc.message)
        except Exception as exc:  # never kill the connection thread silently
            return _error(ENV_DAEMON_INTERNAL, f"{type(exc).__name__}: {exc}")

    def _op_ping(self, req: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "pid": os.getpid()}

    @staticmethod
    def _job_id(req: dict[str, Any]) -> str:
        job_id = req.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            raise MediaInputError(INPUT_BAD_REQUEST, "'job_id' must be a non-empty string")
        return job_id

    def _op_submit(self, req: dict[str, Any]) -> dict[str, Any]:
        kind, argv, output, meta, spec = _validate_job(req.get("job"))
        with self._cv:
            if self._stop.is_set() or not self._accepting:
                raise MediaEnvError(ENV_DAEMON_STOPPING, "the daemon is shutting down")
            rec = self.store.create(kind, argv, output=output, meta={**meta, "spec": spec})
            self._queue.append(rec.id)
            self._touch()
            self._cv.notify()
        return {"ok": True, "job_id": rec.id}

    def _op_status(self, req: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "job": self.store.get(self._job_id(req)).to_dict()}

    def _op_result(self, req: dict[str, Any]) -> dict[str, Any]:
        rec = self.store.get(self._job_id(req))
        return {
            "ok": True,
            "job": rec.to_dict(),
            "ready": rec.state in TERMINAL_STATES,
            "output": rec.output if rec.state == "done" else None,
        }

    def _op_cancel(self, req: dict[str, Any]) -> dict[str, Any]:
        job_id = self._job_id(req)
        with self._cv:
            rec = self.store.get(job_id)
            ev = self._active.get(job_id)
            if ev is None:
                if rec.state in TERMINAL_STATES:
                    raise MediaInputError(
                        INPUT_JOB_ILLEGAL_TRANSITION,
                        f"job {job_id} is already {rec.state}",
                        "only queued or running jobs can be cancelled",
                    )
                if job_id in self._queue:
                    self._queue.remove(job_id)
                rec = self.store.update(job_id, state="cancelled")
                return {"ok": True, "job": rec.to_dict()}
            ev.set()
        deadline = time.monotonic() + self.kill_grace + 10.0
        while time.monotonic() < deadline:
            rec = self.store.get(job_id)
            if rec.state in TERMINAL_STATES:
                break
            time.sleep(0.05)
        return {"ok": True, "job": rec.to_dict()}

    # -- workers ------------------------------------------------------------------
    def _worker(self) -> None:
        while True:
            with self._cv:
                while not self._queue and not self._stop.is_set():
                    self._cv.wait(_TICK)
                if self._stop.is_set():
                    return
                job_id = self._queue.popleft()
                try:
                    rec = self.store.update(job_id, state="running")
                except MediaInputError:
                    continue  # cancelled or vanished meanwhile
                ev = threading.Event()
                self._active[job_id] = ev
            try:
                self._execute(rec, ev)
            finally:
                with self._cv:
                    self._active.pop(job_id, None)
                    self._touch()

    def _execute(self, rec: JobRecord, ev: threading.Event) -> None:
        handler = self._ffmpeg if rec.kind == "ffmpeg" else _HANDLERS.get(rec.kind)
        try:
            if handler is None:
                raise JobFailed(f"no handler for job kind {rec.kind!r}")
            result = handler(rec, self.store, ev) or {}
            if ev.is_set():
                raise JobCancelled()
            changes = {k: v for k, v in dict(result).items() if k in _RESULT_FIELDS}
            self.store.update(rec.id, state="done", **changes)
        except JobCancelled:
            self._finish(rec.id, state="cancelled")
        except JobFailed as exc:
            if ev.is_set():
                self._finish(rec.id, state="cancelled")
            elif exc.kind is not None:
                self._fail(rec.id, exc.message, exc.stderr, exc.kind)
            elif rec.kind == "ffmpeg":
                self.store.mark_failed(rec.id, exc.stderr, exc.message)
            else:
                self._fail(rec.id, exc.message, exc.stderr)
        except Exception as exc:  # a handler bug fails its job, not the daemon
            if ev.is_set():
                self._finish(rec.id, state="cancelled")
            else:
                self._fail(rec.id, f"{rec.kind} job failed: {type(exc).__name__}: {exc}", "")

    def _finish(self, job_id: str, state: str) -> None:
        try:
            self.store.update(job_id, state=state)
        except MediaInputError:
            pass

    def _fail(self, job_id: str, message: str, stderr: str, kind: str = ENV_JOB_FAILED) -> None:
        error = {
            "kind": kind,
            "message": message,
            "stderr_tail": stderr,
            "log_path": str(self.store.log_path(job_id)),
        }
        try:
            self.store.update(job_id, state="failed", error=error)
        except MediaInputError:
            pass

    def _shutdown_jobs(self, workers: list[threading.Thread]) -> None:
        with self._cv:
            while self._queue:
                self._finish(self._queue.popleft(), state="cancelled")
            for ev in self._active.values():
                ev.set()
            self._cv.notify_all()
        deadline = time.monotonic() + self.kill_grace + 15.0
        for t in workers:
            t.join(max(0.0, deadline - time.monotonic()))


# -- helpers + entry point -----------------------------------------------------------


def _encode(obj: dict[str, Any]) -> bytes:
    return json.dumps(obj, separators=(",", ":")).encode("utf-8") + b"\n"


def _error(kind: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"kind": kind, "message": message}}


def _str_list(value: Any, name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise MediaInputError(INPUT_BAD_REQUEST, f"'{name}' must be a list of strings")
    return list(value)


def _opt_str(value: Any, name: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise MediaInputError(INPUT_BAD_REQUEST, f"'{name}' must be a string")
    return value


def _validate_job(job: Any) -> tuple[str, list[str], str | None, dict, dict]:
    if not isinstance(job, dict):
        raise MediaInputError(INPUT_BAD_REQUEST, "'job' must be an object")
    kind = job.get("kind")
    if not isinstance(kind, str) or (kind != "ffmpeg" and kind not in _HANDLERS):
        known = sorted({"ffmpeg", *_HANDLERS})
        raise MediaInputError(INPUT_UNKNOWN_JOB_KIND, f"unknown job kind {kind!r}; known: {known}")
    argv = _str_list(job.get("argv", []), "argv")
    output = _opt_str(job.get("output"), "output")
    meta = job.get("meta", {})
    if not isinstance(meta, dict):
        raise MediaInputError(INPUT_BAD_REQUEST, "'meta' must be an object")
    spec = {k: v for k, v in job.items() if k not in ("kind", "argv", "output", "meta")}
    if kind == "ffmpeg":
        _str_list(spec.get("args"), "args")
        _opt_str(spec.get("tmp_output"), "tmp_output")
        if not isinstance(spec.get("overwrite", False), bool):
            raise MediaInputError(INPUT_BAD_REQUEST, "'overwrite' must be a boolean")
        duration = spec.get("duration")
        if duration is not None and (
            isinstance(duration, bool) or not isinstance(duration, (int, float))
        ):
            raise MediaInputError(INPUT_BAD_REQUEST, "'duration' must be a number")
    return kind, argv, output, meta, spec


def serve(
    sockdir: Path | str | None = None,
    *,
    store: JobStore | None = None,
    max_concurrent: int = DEFAULT_MAX_CONCURRENT,
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
    kill_grace: float = DEFAULT_KILL_GRACE,
    install_signals: bool = True,
) -> int:
    """Run the daemon in the foreground; the process exit code.

    0 after a clean stop, SIGTERM or idle exit -- and also, quietly, when
    another daemon already holds the lock (a losing concurrent spawner).
    2 when the socket directory cannot be set up safely.
    """
    daemon = Daemon(
        sockdir,
        store=store,
        max_concurrent=max_concurrent,
        idle_timeout=idle_timeout,
        kill_grace=kill_grace,
        install_signals=install_signals,
    )
    try:
        return daemon.run()
    except DaemonAlreadyRunning:
        return 0
    except DaemonSetupError as exc:
        sys.stderr.write(f"error: {exc.message}\n")
        if exc.remediation:
            sys.stderr.write(f"hint: {exc.remediation}\n")
        return exc.code
