"""``media-cli edit`` -- plan and apply edit lists; ``edit regions`` finds redaction boxes.

``plan`` validates + compiles an edit list and prints the full plan; it writes nothing.
``apply`` without ``--apply`` is identical to ``plan`` (dry run). ``apply --apply`` submits an
ffmpeg job to the local daemon (the only code path that may start it) and prints the job id.
``regions`` samples frames and asks the local senses role for boxes; it creates no file.
"""

from __future__ import annotations

import argparse
from typing import Any

from media_cli.cli._commands.overview import emit_overview
from media_cli.cli._output import emit_result
from media_cli.media import compile as media_compile
from media_cli.media import editlist as media_editlist
from media_cli.media import probe as media_probe

_SUBVERBS = [
    "plan <editlist.json> [--fast] — validate + compile; print the plan, write nothing",
    "apply <editlist.json> [--fast] [--apply] — dry run unless --apply, which submits a job",
    "regions <file> <description> — find redaction regions via the local senses role",
    "overview — this summary",
]


def _json(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "json", False))


def _compile(path: str, fast: bool):
    el = media_editlist.load_and_validate(path)
    compiled = media_compile.compile_editlist(el, media_probe.probe(el.input), fast=fast)
    return el, compiled


def _plan_text(plan: dict[str, Any], note: str) -> str:
    out = plan["output"]
    lines = [
        f"{note}: {out['src']} -> {out['dst']}",
        f"mode: {plan['mode']}  duration: {plan['expected_duration']:.3f}s  "
        f"redacted: {plan['redacted']}  metadata: {plan['metadata']}",
        "ffmpeg " + " ".join(plan["args"]),
    ]
    for s in out["streams"]:
        lines.append(f"  stream: {s}")
    if plan["flags"]:
        lines.append(f"flags: {plan['flags']}")
    return "\n".join(lines)


def _emit_plan(args: argparse.Namespace, *, dry: bool) -> None:
    _, compiled = _compile(args.editlist, args.fast)
    plan = compiled.to_dict()
    doc = {"dry_run": dry, **plan}
    emit_result(
        doc if _json(args) else _plan_text(plan, "dry run (nothing written)"),
        json_mode=_json(args),
    )


def cmd_plan(args: argparse.Namespace) -> int:
    _emit_plan(args, dry=True)
    return 0


def cmd_apply(args: argparse.Namespace) -> None:
    if not args.apply:
        _emit_plan(args, dry=True)
        return
    from media_cli.media.daemon.client import DaemonClient  # lazy: dry runs never touch it

    el, compiled = _compile(args.editlist, args.fast)
    spec = compiled.job_spec()
    spec["overwrite"] = el.overwrite
    job_id = DaemonClient().submit(spec)
    plan = compiled.to_dict()
    doc = {"job_id": job_id, "output": spec["output"], "plan": plan}
    emit_result(
        doc if _json(args) else f"submitted job {job_id} -> {spec['output']}",
        json_mode=_json(args),
    )


def _regions_text(doc: dict[str, Any]) -> str:
    cov = doc["coverage"]
    lines = [
        f"{len(doc['regions'])} region(s) for {doc['description']!r} "
        f"(samples={cov['samples']}, without detection={len(cov['frames_without_detection'])}, "
        f"rejected={len(cov['rejected_boxes'])})"
    ]
    lines += [f"  {r}" for r in doc["regions"]]
    return "\n".join(lines)


def cmd_regions(args: argparse.Namespace) -> int:
    from media_cli.media import regions as media_regions

    doc = media_regions.find_regions(
        args.file,
        args.description,
        fps=args.fps,
        method=args.method,
        start=args.start,
        end=args.end,
        margin=args.margin,
    )
    emit_result(doc if _json(args) else _regions_text(doc), json_mode=_json(args))
    return 0


def cmd_overview(args: argparse.Namespace) -> int:
    emit_overview(
        "media-cli edit", [{"title": "Subverbs", "items": list(_SUBVERBS)}], json_mode=_json(args)
    )
    return 0


def _add_json(p: argparse.ArgumentParser) -> None:
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("edit", help="Plan/apply edit lists (see 'media edit overview').")
    _add_json(p)
    p.set_defaults(func=cmd_overview, json=False)
    # propagate the structured parser class so noun parse errors exit 1, not 2
    ns = p.add_subparsers(dest="edit_command", parser_class=type(p))

    pl = ns.add_parser("plan", help="Validate + compile an edit list; print the plan (no writes).")
    pl.add_argument("editlist", help="Edit-list JSON file.")
    pl.add_argument("--fast", action="store_true", help="Keyframe-snapped stream copy.")
    _add_json(pl)
    pl.set_defaults(func=cmd_plan)

    ap = ns.add_parser("apply", help="Submit the edit as a job (dry run unless --apply).")
    ap.add_argument("editlist", help="Edit-list JSON file.")
    ap.add_argument("--fast", action="store_true", help="Keyframe-snapped stream copy.")
    ap.add_argument("--apply", action="store_true", help="Actually submit the job.")
    _add_json(ap)
    ap.set_defaults(func=cmd_apply)

    rg = ns.add_parser("regions", help="Find redaction regions for a description (local senses).")
    rg.add_argument("file", help="Video file (never modified).")
    rg.add_argument("description", help="What to find, e.g. 'the license plate'.")
    rg.add_argument("--fps", type=float, default=2.0, help="Sampling rate (default 2).")
    rg.add_argument("--method", choices=("hold", "linear"), default="hold")
    rg.add_argument("--start", type=float, default=None, help="Window start (s).")
    rg.add_argument("--end", type=float, default=None, help="Window end (s).")
    rg.add_argument("--margin", type=int, default=0, help="Pixels added around each box.")
    _add_json(rg)
    rg.set_defaults(func=cmd_regions)

    ov = ns.add_parser("overview", help="Summarise the edit verbs.")
    _add_json(ov)
    ov.set_defaults(func=cmd_overview)
