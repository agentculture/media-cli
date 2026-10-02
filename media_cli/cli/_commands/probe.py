"""``media-cli probe <file>`` — read-only facts about a media file.

Never touches the daemon (no ``media_cli.media.daemon`` import) and never
writes anything.
"""

from __future__ import annotations

import argparse

from media_cli.cli._output import emit_result
from media_cli.media import probe as media_probe


def _summary(d: dict) -> str:
    lines = [f"{d.get('path', '')}: {d.get('format_name', '')} {d.get('duration', 0)}s"]
    for s in d.get("streams", []):
        lines.append(f"  #{s.get('index')} {s.get('type')} {s.get('codec')}")
    return "\n".join(lines)


def cmd_probe(args: argparse.Namespace) -> int:
    doc = media_probe.probe(args.file).to_dict()
    json_mode = bool(getattr(args, "json", False))
    emit_result(doc if json_mode else _summary(doc), json_mode=json_mode)
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("probe", help="Show streams, duration and timing of a media file.")
    p.add_argument("file", help="Media file to inspect (never modified).")
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=cmd_probe)
