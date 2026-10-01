"""``media-cli search`` -- build, query, inspect and purge the per-file search cache.

``index`` is a write verb: dry run by default (prints the sense-call estimate, makes no
gateway call for a fresh plan and writes nothing); ``--apply`` runs the same budget check and
then submits an ``index`` job to the local daemon. ``purge`` is dry-run by default too.
"""

from __future__ import annotations

import argparse
import os
from typing import Any

from media_cli.cli._commands.overview import emit_overview
from media_cli.cli._output import emit_result
from media_cli.media import index as media_index
from media_cli.media import search as media_search
from media_cli.media.errors import MediaInputError

DEFAULT_FPS = 0.5  # same default as media.search.query's index_params

_SUBVERBS = [
    "index <file> [--fps F | --scene T] [--apply] — dry run shows the sense-call estimate; "
    "--apply submits an index job",
    "query <file> <text> [--modality frames|speech|all] — find where text occurs",
    "purge <file> [--apply] — list (or with --apply remove) the cached artifacts of a file",
    "cache [<file>] — inspect the cache",
    "overview — this summary",
]


def _json(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "json", False))


def _sampling(args: argparse.Namespace) -> dict[str, Any]:
    return {"scene": args.scene} if args.scene is not None else {"fps": args.fps or DEFAULT_FPS}


def _plan(args: argparse.Namespace) -> dict[str, Any]:
    return media_index.build_index(
        args.file,
        dry_run=True,
        batch_size=args.batch_size,
        max_calls=args.max_calls,
        **_sampling(args),
    )


def cmd_index(args: argparse.Namespace) -> int:
    plan = _plan(args)  # raises input.budget_exceeded before anything is submitted
    if not args.apply:
        doc = {"dry_run": True, **plan}
        text = (
            f"dry run (nothing written): {plan['frames']} frames, {plan['batches']} batches, "
            f"{plan['sense_calls']} sense calls (cap {plan['cap']}), cached: {plan['cached']}"
        )
        emit_result(doc if _json(args) else text, json_mode=_json(args))
        return 0
    from media_cli.media.daemon.client import DaemonClient  # lazy: dry runs never touch it

    job = {
        "kind": "index",
        "path": os.path.abspath(args.file),
        "batch_size": args.batch_size,
        "max_calls": args.max_calls,
        **_sampling(args),
    }
    job_id = DaemonClient().submit(job)
    doc = {"job_id": job_id, "plan": plan}
    emit_result(
        doc if _json(args) else f"submitted index job {job_id} ({plan['sense_calls']} sense calls)",
        json_mode=_json(args),
    )
    return 0


def _hits_text(doc: dict[str, Any]) -> str:
    lines = [f"{len(doc['hits'])} hit(s) for {doc['query']!r} in {doc['file']}"]
    for h in doc["hits"]:
        lines.append(f"  {h['start']:.2f}-{h['end']:.2f}s  score={h.get('score', 0):.2f}")
    return "\n".join(lines)


def cmd_query(args: argparse.Namespace) -> int:
    try:
        hits = media_search.query(
            args.file,
            args.text,
            modality=args.modality,
            index_params=_sampling(args),
            threshold=args.threshold,
        )
    except MediaInputError as err:
        if err.kind != media_search.INPUT_INDEX_MISSING:
            raise
        flag = (
            f"--scene {args.scene}" if args.scene is not None else f"--fps {_sampling(args)['fps']}"
        )
        raise MediaInputError(
            err.kind,
            err.message,
            f"build it first: media search index {args.file} {flag} --apply "
            "(then poll 'media job result <id>')",
        ) from err
    doc = {"file": os.path.abspath(args.file), "query": args.text, "modality": args.modality}
    doc["hits"] = hits
    emit_result(doc if _json(args) else _hits_text(doc), json_mode=_json(args))
    return 0


def _purge_text(prefix: str, rep: dict[str, Any]) -> str:
    return f"{prefix}: {len(rep['indexes'])} index dir(s), {len(rep['transcripts'])} transcript(s)"


def cmd_purge(args: argparse.Namespace) -> int:
    rep = media_search.cache_report(args.file)
    if not args.apply:
        doc = {
            "dry_run": True,
            "file": os.path.abspath(args.file),
            "would_remove": {"indexes": rep["indexes"], "transcripts": rep["transcripts"]},
            "total_bytes": rep["total_bytes"],
        }
        emit_result(
            doc if _json(args) else _purge_text("dry run, would remove", rep),
            json_mode=_json(args),
        )
        return 0
    removed = media_search.purge(args.file)
    doc = {"dry_run": False, "file": os.path.abspath(args.file), "removed": removed}
    text = f"removed {removed['indexes']} index dir(s), {removed['transcripts']} transcript(s)"
    emit_result(doc if _json(args) else text, json_mode=_json(args))
    return 0


def cmd_cache(args: argparse.Namespace) -> int:
    rep = media_search.cache_report(args.file)
    emit_result(rep if _json(args) else _purge_text("cached", rep), json_mode=_json(args))
    return 0


def cmd_overview(args: argparse.Namespace) -> int:
    emit_overview(
        "media-cli search",
        [{"title": "Subverbs", "items": list(_SUBVERBS)}],
        json_mode=_json(args),
    )
    return 0


def _add_json(p: argparse.ArgumentParser) -> None:
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")


def _add_sampling(p: argparse.ArgumentParser) -> None:
    g = p.add_mutually_exclusive_group()
    g.add_argument("--fps", type=float, default=None, help=f"Frames/s (default {DEFAULT_FPS}).")
    g.add_argument("--scene", type=float, default=None, help="Scene-change threshold 0..1.")


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("search", help="Index and query media (see 'media-cli search overview').")
    _add_json(p)
    p.set_defaults(func=cmd_overview, json=False)
    ns = p.add_subparsers(dest="search_command", parser_class=type(p))

    ix = ns.add_parser("index", help="Plan (dry run) or submit a frame-index job.")
    ix.add_argument("file", help="Media file (never modified).")
    _add_sampling(ix)
    ix.add_argument("--batch-size", type=int, default=media_index.DEFAULT_BATCH_SIZE)
    ix.add_argument("--max-calls", type=int, default=media_index.DEFAULT_MAX_CALLS)
    ix.add_argument("--apply", action="store_true", help="Actually submit the index job.")
    _add_json(ix)
    ix.set_defaults(func=cmd_index)

    q = ns.add_parser("query", help="Find where text occurs (needs a built index for frames).")
    q.add_argument("file", help="Media file.")
    q.add_argument("text", help="What to look for.")
    q.add_argument("--modality", choices=media_search.MODALITIES, default="frames")
    q.add_argument("--threshold", type=float, default=media_search.DEFAULT_THRESHOLD)
    _add_sampling(q)
    _add_json(q)
    q.set_defaults(func=cmd_query)

    pg = ns.add_parser("purge", help="List (dry run) or remove a file's cached artifacts.")
    pg.add_argument("file", help="Media file.")
    pg.add_argument("--apply", action="store_true", help="Actually remove them.")
    _add_json(pg)
    pg.set_defaults(func=cmd_purge)

    ca = ns.add_parser("cache", help="Inspect cached indexes and transcripts.")
    ca.add_argument("file", nargs="?", default=None, help="Limit to this file.")
    _add_json(ca)
    ca.set_defaults(func=cmd_cache)

    ov = ns.add_parser("overview", help="Summarise the search verbs.")
    _add_json(ov)
    ov.set_defaults(func=cmd_overview)
