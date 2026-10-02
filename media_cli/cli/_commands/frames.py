"""``media-cli frames <file>`` — extract still frames (and a contact sheet) locally.

Writes PNGs into ``--out`` only; the source is never modified. Never touches the
daemon (no ``media_cli.media.daemon`` import). ``--describe`` is optional and
fail-soft: if the senses gateway is unavailable the command still succeeds and
the failure is reported inside the JSON ``describe`` field only.
"""

from __future__ import annotations

import argparse
import os

from media_cli.cli._output import emit_result
from media_cli.media import frames as media_frames
from media_cli.media.errors import (
    ENV_SENSE_NOT_LOCAL,
    ENV_SENSE_UNAVAILABLE,
    MediaEnvError,
)

SHEET_NAME = "contact_sheet.png"
DESCRIBE_PROMPT = "Describe what is visible in this video frame in one or two sentences."
_SOFT_KINDS = (ENV_SENSE_UNAVAILABLE, ENV_SENSE_NOT_LOCAL)


def _describe(paths: list[str]) -> dict:
    # Imported lazily: frames stays fully offline unless --describe is asked for.
    from media_cli.media.senses import SensesClient

    try:
        client = SensesClient()
        descs = [{"path": p, "text": client.describe_images([p], DESCRIBE_PROMPT)} for p in paths]
    except MediaEnvError as err:
        if err.kind not in _SOFT_KINDS:
            raise
        return {"ok": False, "kind": err.kind, "message": err.message}
    return {"ok": True, "descriptions": descs}


def _summary(doc: dict) -> str:
    n = doc["count"]
    lines = [f"{n} frame{'' if n == 1 else 's'} from {doc['source']} -> {doc['out']}"]
    lines += [f"  t={f['t']:.3f}s  {f['path']}" for f in doc["frames"]]
    if doc["sheet"]:
        lines.append(f"contact sheet: {doc['sheet']}")
    d = doc["describe"]
    if d is not None:
        lines.append("describe: ok" if d["ok"] else f"describe: unavailable ({d['kind']})")
    return "\n".join(lines)


def cmd_frames(args: argparse.Namespace) -> int:
    out_dir = os.path.abspath(args.out)
    result = media_frames.extract(
        args.file,
        times=args.at,
        every=args.every,
        scene=args.scene,
        outdir=out_dir,
        overwrite=args.overwrite,
        outdir_create=True,
    )
    sheet = None
    if args.sheet:
        sheet = media_frames.contact_sheet(
            result, os.path.join(out_dir, SHEET_NAME), overwrite=args.overwrite
        )
    doc = {
        "source": os.path.abspath(args.file),
        "out": out_dir,
        "count": len(result),
        "frames": result,
        "sheet": sheet,
        "describe": _describe([f["path"] for f in result]) if args.describe else None,
    }
    json_mode = bool(getattr(args, "json", False))
    emit_result(doc if json_mode else _summary(doc), json_mode=json_mode)
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("frames", help="Extract still frames (and a contact sheet) from a video.")
    p.add_argument("file", help="Video file (never modified).")
    sel = p.add_mutually_exclusive_group(required=True)
    sel.add_argument("--at", type=float, nargs="+", metavar="T", help="Timestamps in seconds.")
    sel.add_argument("--every", type=float, metavar="N", help="One frame every N seconds.")
    sel.add_argument("--scene", type=float, metavar="THR", help="Scene-change threshold 0..1.")
    p.add_argument("--out", required=True, metavar="DIR", help="Directory for PNGs (created).")
    p.add_argument("--sheet", action="store_true", help=f"Also write {SHEET_NAME} in --out.")
    p.add_argument("--describe", action="store_true", help="Describe frames via senses (optional).")
    p.add_argument("--overwrite", action="store_true", help="Replace existing output files.")
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=cmd_frames)
