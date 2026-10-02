"""Daemon job handlers beyond ffmpeg: the ``index`` kind (search index build).

Importing this module registers the handler; ``media_cli.media.daemon.__main__`` imports it
so the daemon process knows the kind. Spec keys (the submitted job minus ``kind``):
``path`` (str, required), ``fps`` or ``scene`` (number), ``batch_size`` (int), ``max_calls``
(int), ``role`` (str). The finished record carries ``output`` (the index directory) and
``meta`` (``frames``, ``entries``, ``model``, ``index_dir``).

Cancellation: the senses client is wrapped so that every gateway request first checks the
cancel event, so a cancel lands between caption batches; the half-built stage is removed by
``build_index`` itself.
"""

from __future__ import annotations

import threading
from typing import Any

from media_cli.cli._errors import CliError
from media_cli.media import index
from media_cli.media.daemon import server
from media_cli.media.daemon.jobs import JobRecord, JobStore

KIND = "index"


class _CancellableClient:
    """Delegates to a SensesClient but raises JobCancelled once the event is set."""

    def __init__(self, inner: Any, cancel_event: threading.Event) -> None:
        self._inner = inner
        self._ev = cancel_event

    def describe_images(self, *args: Any, **kwargs: Any) -> Any:
        if self._ev.is_set():
            raise server.JobCancelled()
        return self._inner.describe_images(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _spec(rec: JobRecord) -> dict[str, Any]:
    spec = rec.meta.get("spec")
    if not isinstance(spec, dict) or not isinstance(spec.get("path"), str):
        raise server.JobFailed("index job needs a 'path' string", kind="input.bad_index_request")
    return spec


def run_index(rec: JobRecord, store: JobStore, cancel_event: threading.Event) -> dict[str, Any]:
    from media_cli.media.senses import SensesClient  # lazy: only when a job actually runs

    spec = _spec(rec)
    if cancel_event.is_set():
        raise server.JobCancelled()
    kwargs: dict[str, Any] = {}
    for key in ("fps", "scene", "batch_size", "max_calls"):
        if spec.get(key) is not None:
            kwargs[key] = spec[key]
    role = spec.get("role") or "senses"
    try:
        doc = index.build_index(
            spec["path"],
            client=_CancellableClient(SensesClient(), cancel_event),
            role=role,
            **kwargs,
        )
    except CliError as exc:
        raise server.JobFailed(exc.message, kind=exc.kind or None) from exc
    sampling = doc["identity"]["sampling"]
    directory = index.index_dir(
        index.default_cache_dir(),
        doc["fingerprint"],
        {k: v for k, v in doc["identity"].items() if k != "served_model"},
    )
    return {
        "output": directory,
        "meta": {
            "index_dir": directory,
            "entries": len(doc["entries"]),
            "model": doc["identity"].get("served_model"),
            "sampling": sampling,
        },
    }


server.register_handler(KIND, run_index)
