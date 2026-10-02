"""Durable job store for the media daemon and its client.

One JSON record per job lives at ``<root>/<id>.json`` plus an optional
``<root>/<id>.log`` holding ffmpeg stderr. ``root`` defaults to
``$XDG_STATE_HOME/media-cli/jobs`` (``~/.local/state/media-cli/jobs`` when
``XDG_STATE_HOME`` is unset), created mode 0700. The client may read records
directly when no daemon is running, so the on-disk format is the contract.

Record schema (``SCHEMA_VERSION`` = 1)::

    {
      "schema_version": 1,
      "id": "<20-digit ns timestamp>-<8 hex>",   # unique, lexicographically sortable
      "kind": "<job kind, e.g. trim>",
      "state": "queued|running|done|failed|cancelled",
      "argv": ["..."],                            # the command that produced the job
      "sense_calls": [],                          # sense (vision/audio) calls made
      "timings": {"created": f, "started": f|null, "finished": f|null},  # epoch secs
      "progress": null | float | object,          # 0..1 or a richer structure
      "output": null | "<path>",
      "error": null | {"kind", "message", "stderr_tail", "log_path"},
      "meta": {}                                  # free-form extras
    }

Legal state transitions via ``update``: queued -> running | cancelled;
running -> done | failed | cancelled. ``mark_failed`` additionally allows
queued -> failed (a job that could not even start). Done/failed/cancelled are
terminal. Writes are atomic (temp file in the same
directory + ``os.replace``) and guarded by a process-wide lock. A record with a
newer ``schema_version`` than this code understands raises a typed error rather
than being misread.

Error kinds are local ``input.*`` constants (errors.py is owned elsewhere).
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from media_cli.media.errors import ENV_FFMPEG_FAILED, STDERR_TAIL_LINES, MediaInputError

SCHEMA_VERSION = 1

STATES = ("queued", "running", "done", "failed", "cancelled")
TERMINAL_STATES = frozenset({"done", "failed", "cancelled"})
_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"running", "cancelled"}),
    "running": frozenset({"done", "failed", "cancelled"}),
    "done": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}

INPUT_JOB_NOT_FOUND = "input.job_not_found"
INPUT_JOB_ILLEGAL_TRANSITION = "input.job_illegal_transition"
INPUT_JOB_BAD_FIELD = "input.job_bad_field"
INPUT_JOB_SCHEMA_UNSUPPORTED = "input.job_schema_unsupported"
INPUT_JOB_CORRUPT = "input.job_corrupt"

_IMMUTABLE = frozenset({"id", "schema_version", "kind"})


@dataclass
class JobRecord:
    id: str
    kind: str
    state: str = "queued"
    argv: list[str] = field(default_factory=list)
    sense_calls: list[Any] = field(default_factory=list)
    timings: dict[str, float | None] = field(
        default_factory=lambda: {"created": None, "started": None, "finished": None}
    )
    progress: Any = None
    output: str | None = None
    error: dict[str, Any] | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # schema_version first for readability
        return {"schema_version": d.pop("schema_version"), **d}


_FIELD_NAMES = frozenset(f.name for f in fields(JobRecord))


def default_root() -> Path:
    state = os.environ.get("XDG_STATE_HOME")
    base = Path(state) if state else Path.home() / ".local" / "state"
    return base / "media-cli" / "jobs"


INPUT_JOB_STORE_INVALID = "input.job_store_invalid"


def _allowed_bases() -> list[str]:
    bases = [str(Path.home()), tempfile.gettempdir()]
    state = os.environ.get("XDG_STATE_HOME")
    if state:
        bases.append(state)
    return [os.path.realpath(b) for b in bases]


def _safe_root(root: Path | str) -> Path:
    """Resolve a caller-supplied store root and refuse anything unsafe.

    A custom root (``--jobs-root``, a test's ``store=``) must resolve, after
    symlinks, to a directory inside the user's home, ``$XDG_STATE_HOME`` or the
    temp dir, and must not be an existing non-directory or another user's dir.
    """
    resolved = os.path.realpath(os.fspath(root))
    inside = any(
        resolved == base or resolved.startswith(base + os.sep) for base in _allowed_bases()
    )
    if not inside:
        raise MediaInputError(
            INPUT_JOB_STORE_INVALID,
            f"job store root {resolved!r} is outside the home, state and temp directories",
            "omit --jobs-root to use $XDG_STATE_HOME/media-cli/jobs",
        )
    path = Path(resolved)
    if path.exists() and (not path.is_dir() or path.stat().st_uid != os.getuid()):
        raise MediaInputError(
            INPUT_JOB_STORE_INVALID,
            f"job store root {resolved!r} is not a directory owned by this user",
            "point --jobs-root at a private directory you own, or omit it",
        )
    return path


class JobStore:
    """Thread-safe, atomic, restart-surviving job record store."""

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = _safe_root(root) if root is not None else default_root()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._last_ns = 0

    # -- paths ---------------------------------------------------------
    def _check_id(self, job_id: str) -> None:
        if not job_id or "/" in job_id or "\\" in job_id or job_id.startswith("."):
            raise MediaInputError(
                INPUT_JOB_NOT_FOUND, f"no such job: {job_id!r}", "use `media job list` to list ids"
            )

    def _json_path(self, job_id: str) -> Path:
        self._check_id(job_id)
        return self.root / f"{job_id}.json"

    def log_path(self, job_id: str) -> Path:
        self._check_id(job_id)
        return self.root / f"{job_id}.log"

    # -- io ------------------------------------------------------------
    def _new_id(self) -> str:
        ns = time.time_ns()
        if ns <= self._last_ns:  # strictly increasing within a process
            ns = self._last_ns + 1
        self._last_ns = ns
        return f"{ns:020d}-{secrets.token_hex(4)}"

    def _write(self, rec: JobRecord) -> None:
        payload = json.dumps(rec.to_dict(), indent=2, sort_keys=False)  # may raise before any IO
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=f".{rec.id}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._json_path(rec.id))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _read_path(self, path: Path) -> JobRecord:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise MediaInputError(
                INPUT_JOB_NOT_FOUND,
                f"no such job: {path.stem!r}",
                "use `media job list` to list ids",
            ) from None
        except (OSError, ValueError) as exc:
            raise MediaInputError(
                INPUT_JOB_CORRUPT, f"unreadable job record {path.name}: {exc}", ""
            ) from exc
        version = data.get("schema_version") if isinstance(data, dict) else None
        if not isinstance(version, int):
            raise MediaInputError(
                INPUT_JOB_CORRUPT, f"job record {path.name} has no schema_version"
            )
        if version > SCHEMA_VERSION:
            raise MediaInputError(
                INPUT_JOB_SCHEMA_UNSUPPORTED,
                f"job record {path.name} has schema_version {version}; "
                f"this media-cli understands up to {SCHEMA_VERSION}",
                "upgrade media-cli to read this job",
            )
        known = {k: v for k, v in data.items() if k in _FIELD_NAMES}
        try:
            return JobRecord(**known)
        except TypeError as exc:
            raise MediaInputError(
                INPUT_JOB_CORRUPT, f"malformed job record {path.name}: {exc}"
            ) from exc

    # -- public API ----------------------------------------------------
    def create(
        self,
        kind: str,
        argv: list[str],
        output: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> JobRecord:
        with self._lock:
            rec = JobRecord(
                id=self._new_id(),
                kind=kind,
                argv=list(argv),
                output=output,
                meta=dict(meta or {}),
            )
            rec.timings["created"] = time.time()
            self._write(rec)
            return rec

    def get(self, job_id: str) -> JobRecord:
        return self._read_path(self._json_path(job_id))

    def list(self) -> list[JobRecord]:
        """All records, oldest first (ids sort by creation time)."""
        with self._lock:
            paths = sorted(p for p in self.root.glob("*.json") if not p.name.startswith("."))
            return [self._read_path(p) for p in paths]

    def update(self, job_id: str, **changes: Any) -> JobRecord:
        return self._update(job_id, changes)

    def _update(
        self, job_id: str, changes: dict[str, Any], extra_ok: frozenset[str] = frozenset()
    ) -> JobRecord:
        with self._lock:
            rec = self.get(job_id)
            _check_updatable(changes)
            _apply_transition(rec, changes.get("state"), extra_ok)
            for key, value in changes.items():
                setattr(rec, key, value)
            self._write(rec)
            return rec

    def append_log(self, job_id: str, text: str) -> None:
        path = self.log_path(job_id)
        with self._lock, open(path, "a", encoding="utf-8") as fh:
            fh.write(text)

    def mark_failed(self, job_id: str, stderr: str, message: str = "ffmpeg failed") -> JobRecord:
        """Fail a job, keeping the stderr tail and the full log path on the record."""
        tail = "\n".join((stderr or "").strip().splitlines()[-STDERR_TAIL_LINES:])
        with self._lock:
            log = self.log_path(job_id)
            if stderr and not log.exists():
                self.append_log(job_id, stderr)
            error = {
                "kind": ENV_FFMPEG_FAILED,
                "message": message,
                "stderr_tail": tail,
                "log_path": str(log),
            }
            return self._update(
                job_id, {"state": "failed", "error": error}, extra_ok=frozenset({"failed"})
            )


def _check_updatable(changes: dict[str, Any]) -> None:
    """Refuse unknown or immutable field names before anything is changed."""
    for key in changes:
        if key not in _FIELD_NAMES or key in _IMMUTABLE:
            raise MediaInputError(INPUT_JOB_BAD_FIELD, f"cannot update job field {key!r}", "")


def _apply_transition(rec: JobRecord, new_state: Any, extra_ok: frozenset[str]) -> None:
    """Validate ``rec.state -> new_state`` and stamp the started/finished timings."""
    if new_state is not None and new_state != rec.state:
        allowed = _TRANSITIONS.get(rec.state, frozenset())
        if new_state not in allowed and not (rec.state == "queued" and new_state in extra_ok):
            raise MediaInputError(
                INPUT_JOB_ILLEGAL_TRANSITION,
                f"illegal job transition {rec.state} -> {new_state}",
                f"legal from {rec.state}: {sorted(allowed) or 'none'}",
            )
        now = time.time()
        if new_state == "running":
            rec.timings["started"] = now
        if new_state in TERMINAL_STATES:
            rec.timings["finished"] = now
    elif new_state is not None and rec.state in TERMINAL_STATES:
        raise MediaInputError(
            INPUT_JOB_ILLEGAL_TRANSITION,
            f"job already {rec.state}",
            "terminal states are final",
        )
