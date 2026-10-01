"""``media overview`` — read-only descriptive snapshot of the agent.

Describes the agent to an agent reader: identity (from culture.yaml), its lane,
the verb surface, and the artifacts it carries. The shared
section/render helpers here are reused by the ``cli`` noun's ``overview`` (see
:mod:`media_cli.cli._commands.cli`).

Descriptive verbs never hard-fail on a missing target path — an optional
positional ``target`` is accepted and ignored (overview describes this agent,
not an external target), so ``overview <bogus-path>`` still exits 0.
"""

from __future__ import annotations

import argparse

from media_cli.cli._commands.whoami import report
from media_cli.cli._output import emit_result

_LANE = [
    "owns the local media I/O device plane and the editing of the media captured from it",
    "moves and transforms bytes; interpretation goes to the local lobes senses gateway "
    "(local-only, fail-closed)",
    "not here: capture (webcam-cli), generation (innereye, harmonics-cli), "
    "shell/ffmpeg passthrough (shell-cli)",
    "device-plane inventory verbs (list/describe) are not built yet",
]

_ARTIFACTS = [
    "culture.yaml + AGENTS.colleague.md — mesh identity (suffix + backend)",
    "docs/specs/2026-09-30-media-file-editing.md — the media-file editing spec",
    ".claude/skills/ — the canonical guildmaster skill kit (cite-don't-import)",
    "docs/skill-sources.md — skill provenance ledger",
    "pyproject.toml + .github/workflows/ — buildable, deployable package baseline",
]

_VERBS = [
    "whoami — identity probe (nick, version, backend, model)",
    "learn — structured self-teaching prompt",
    "explain <path> — markdown docs for a topic",
    "overview — this descriptive snapshot",
    "doctor — check the agent-identity invariants",
    "probe <file> — streams, duration and time origin of a media file (read-only)",
    "frames <file> — extract PNG frames / contact sheet locally (read-only toward the source)",
    "edit plan|apply|regions|overview — JSON edit lists compiled to ffmpeg; "
    "apply is a dry run unless --apply",
    "search index|query|purge|cache|overview — semantic search of frames and speech, "
    "with evidence",
    "job status|result|cancel|list|overview — poll daemon jobs by id (never starts the daemon)",
]


def agent_sections() -> list[dict[str, object]]:
    """Sections describing the agent (used by the global verb)."""
    ident = report()
    return [
        {
            "title": "Identity",
            "items": [
                f"nick: {ident['nick']}",
                f"version: {ident['version']}",
                f"backend: {ident['backend']}",
                f"model: {ident['model']}",
            ],
        },
        {"title": "Lane", "items": list(_LANE)},
        {"title": "Verbs", "items": list(_VERBS)},
        {"title": "Artifacts", "items": list(_ARTIFACTS)},
    ]


def cli_sections() -> list[dict[str, object]]:
    """Sections describing the CLI surface itself (used by `cli overview`)."""
    return [
        {
            "title": "Verbs",
            "items": list(_VERBS) + ["cli overview — describe the CLI surface (this command)"],
        },
        {
            "title": "Conventions",
            "items": [
                "every command supports --json",
                "results to stdout, errors/diagnostics to stderr (never mixed)",
                "exit codes: 0 success, 1 user error, 2 environment error, 3+ reserved",
                "write verbs (edit apply, search index, search purge) are a dry run "
                "unless --apply",
                "the media daemon is spawned on demand only by a submit (edit apply --apply, "
                "search index --apply); no read-only verb starts it",
                "times are normalized seconds from the first presented video frame",
                "the installed command is 'media'; 'media-cli' is the distribution and nick",
            ],
        },
    ]


def render_text(subject: str, sections: list[dict[str, object]]) -> str:
    lines = [f"# {subject}", ""]
    for section in sections:
        lines.append(f"## {section['title']}")
        for item in section["items"]:
            lines.append(f"- {item}")
        lines.append("")
    return "\n".join(lines).rstrip()


def emit_overview(subject: str, sections: list[dict[str, object]], *, json_mode: bool) -> None:
    if json_mode:
        emit_result({"subject": subject, "sections": sections}, json_mode=True)
    else:
        emit_result(render_text(subject, sections), json_mode=False)


def cmd_overview(args: argparse.Namespace) -> int:
    # `target` is accepted for rubric compatibility (descriptive verbs must not
    # hard-fail on a missing path) but overview describes this agent itself.
    emit_overview(
        "media-cli",
        agent_sections(),
        json_mode=bool(getattr(args, "json", False)),
    )
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "overview",
        help="Read-only descriptive snapshot of the agent (identity, verbs, artifacts).",
    )
    p.add_argument(
        "target",
        nargs="?",
        help="Ignored — overview always describes this agent itself. Accepted so a "
        "stray path argument never hard-fails.",
    )
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=cmd_overview)
