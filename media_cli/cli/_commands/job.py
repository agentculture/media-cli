"""``media-cli job`` -- inspect and cancel daemon jobs. Never starts the daemon."""

from __future__ import annotations

import argparse
from typing import Any

from media_cli.cli._commands.overview import emit_overview
from media_cli.cli._output import emit_result

_SUBVERBS = [
    "status <id> — the job record (state, progress, error)",
    "result <id> — ready flag and output path once the job is done",
    "cancel <id> — cancel a queued or running job",
    "list — every job record, oldest first",
    "overview — this summary",
]


def _json(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "json", False))


def _client():
    from media_cli.media.daemon.client import DaemonClient

    return DaemonClient()


def _line(job: dict[str, Any]) -> str:
    out = f" -> {job['output']}" if job.get("output") else ""
    return f"{job['id']}  {job['kind']}  {job['state']}{out}"


def cmd_status(args: argparse.Namespace) -> int:
    doc = _client().status(args.job_id)
    emit_result(doc if _json(args) else _line(doc["job"]), json_mode=_json(args))
    return 0


def cmd_result(args: argparse.Namespace) -> int:
    doc = _client().result(args.job_id)
    text = _line(doc["job"]) + ("" if doc["ready"] else "  (not finished)")
    emit_result(doc if _json(args) else text, json_mode=_json(args))
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    doc = _client().cancel(args.job_id)
    emit_result(doc if _json(args) else _line(doc["job"]), json_mode=_json(args))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    jobs = _client().list_jobs()
    text = "\n".join(_line(j) for j in jobs) if jobs else "no jobs"
    emit_result({"jobs": jobs} if _json(args) else text, json_mode=_json(args))
    return 0


def cmd_overview(args: argparse.Namespace) -> int:
    emit_overview(
        "media-cli job", [{"title": "Subverbs", "items": list(_SUBVERBS)}], json_mode=_json(args)
    )
    return 0


def _add_json(p: argparse.ArgumentParser) -> None:
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("job", help="Inspect/cancel daemon jobs (see 'media job overview').")
    _add_json(p)
    p.set_defaults(func=cmd_overview, json=False)
    ns = p.add_subparsers(dest="job_command", parser_class=type(p))
    for name, fn, help_ in (
        ("status", cmd_status, "Show a job record."),
        ("result", cmd_result, "Show a job's result (output path once done)."),
        ("cancel", cmd_cancel, "Cancel a queued or running job."),
    ):
        sp = ns.add_parser(name, help=help_)
        sp.add_argument("job_id", help="Job id.")
        _add_json(sp)
        sp.set_defaults(func=fn)
    ls = ns.add_parser("list", help="List every job.")
    _add_json(ls)
    ls.set_defaults(func=cmd_list)
    ov = ns.add_parser("overview", help="Summarise the job verbs.")
    _add_json(ov)
    ov.set_defaults(func=cmd_overview)
