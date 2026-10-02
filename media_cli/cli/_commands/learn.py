"""``media learn`` — the learnability affordance.

Prints a structured self-teaching prompt. Must satisfy the agent-first rubric:
>=200 chars and mention purpose, command map, exit codes, --json, and explain.
It is also the spec's usability promise: an agent can do every clause of the
after-state (probe -> frames -> search -> edit -> job) from ``learn`` + ``explain``
alone, so the text names every verb with the installed command ``media``.
"""

from __future__ import annotations

import argparse
import textwrap

from media_cli import __version__
from media_cli.cli._output import emit_result

_PURPOSE = (
    "Owns the local media I/O device plane and the editing of the media captured from it: "
    "probe files, peek at frames, search frames and speech semantically, and run JSON edit "
    "lists (cut, crop, black-box redact, blur, speed, fades, xfade transitions) as daemon "
    "jobs -- dry-run first. Moves and transforms bytes; interpretation is delegated to the "
    "local lobes senses gateway (local-only, fail-closed)."
)

#: (path, summary) for every registered command -- the hand-maintained learn map.
_COMMANDS: list[tuple[tuple[str, ...], str]] = [
    (("whoami",), "Identity probe from culture.yaml."),
    (("learn",), "This self-teaching prompt."),
    (("explain",), "Markdown docs for any noun/verb path."),
    (("overview",), "Descriptive snapshot of the agent."),
    (("doctor",), "Check the agent-identity invariants."),
    (("cli", "overview"), "Describe the CLI surface."),
    (("probe",), "Streams, duration and time origin of a media file (read-only)."),
    (("frames",), "Extract PNG frames (+ contact sheet, optional local describe)."),
    (("edit", "plan"), "Validate + compile a JSON edit list; print the plan, write nothing."),
    (("edit", "apply"), "Dry run unless --apply, which submits the edit as a daemon job."),
    (("edit", "regions"), "Find redaction regions via the local senses role, with coverage."),
    (("edit", "overview"), "Summarise the edit verbs."),
    (("search", "index"), "Dry run: sense-call estimate; --apply submits an index job."),
    (("search", "query"), "Find where text occurs in frames and/or speech, with evidence."),
    (("search", "purge"), "Dry run lists, --apply removes a file's cached index/transcripts."),
    (("search", "cache"), "Inspect the search cache."),
    (("search", "overview"), "Summarise the search verbs."),
    (("job", "status"), "A job record: state, progress, error. Never starts the daemon."),
    (("job", "result"), "Poll: ready flag and output path once done."),
    (("job", "cancel"), "Cancel a queued or running job."),
    (("job", "list"), "Every job record, oldest first."),
    (("job", "overview"), "Summarise the job verbs."),
]

#: h26 -- each clause of the spec's after-state and the commands that do it.
_AFTER_STATE: list[tuple[str, list[tuple[str, ...]]]] = [
    ("probe a file", [("probe",)]),
    ("extract/peek at frames", [("frames",)]),
    (
        "semantically search frames and speech for timestamps",
        [("search", "index"), ("search", "query")],
    ),
    (
        "submit a JSON edit list (cut, crop, black-box redact, blur, speed, fades, xfade "
        "transitions into a short)",
        [("edit", "plan"), ("edit", "apply"), ("edit", "regions")],
    ),
    ("as a daemon job it polls by id", [("job", "status"), ("job", "result"), ("job", "cancel")]),
    ("every step dry-run first", [("edit", "apply"), ("search", "index"), ("search", "purge")]),
    (
        "every result JSON with provenance",
        [("probe",), ("frames",), ("search", "query"), ("edit", "plan"), ("job", "result")],
    ),
]

_WORKFLOW = [
    "media probe in.mkv --json",
    "media frames in.mkv --every 5 --out /tmp/peek --sheet --json",
    "media search index in.mkv --fps 0.5 --json",
    "media search index in.mkv --fps 0.5 --apply --json",
    "media job result <job_id> --json",
    'media search query in.mkv "a dog" --modality all --json',
    "media edit plan edit.json --json",
    "media edit apply edit.json --apply --json",
    "media job result <job_id> --json",
]

_TEXT = """\
media — local media device plane + media-file editing (dist/nick: media-cli).

Purpose
-------
{purpose}
Not here: capture (webcam-cli), generation (innereye, harmonics-cli), raw shell or
ffmpeg passthrough (shell-cli). Device-plane inventory verbs are not built yet.

Commands
--------
  media whoami                 Identity from culture.yaml.
  media learn                  This self-teaching prompt.
  media explain <path>...      Markdown docs for any noun/verb path.
  media overview               Descriptive snapshot of the agent.
  media doctor                 Check the agent-identity invariants.
  media cli overview           Describe the CLI surface itself.

  media probe <file>           Streams, duration, time origin (read-only).
  media frames <file> (--at T... | --every N | --scene THR) --out DIR
                               [--sheet] [--describe] [--overwrite]
                               Peek: extract PNG frames locally, no network needed.
  media search index <file> [--fps F | --scene T] [--max-calls N] [--apply]
                               Dry run prints the sense-call estimate; --apply
                               submits an index job (captions via local senses).
  media search query <file> <text> [--modality frames|speech|all]
                               Hits {{start, end, score, evidence}} in seconds.
  media search purge <file> [--apply]   Dry run lists / --apply removes the cache.
  media search cache [<file>]  Inspect cached indexes and transcripts.
  media edit plan <editlist.json> [--fast]
                               Validate + compile; prints the ffmpeg plan, writes nothing.
  media edit apply <editlist.json> [--fast] [--apply]
                               Dry run unless --apply -> {{"job_id"}} (daemon job).
  media edit regions <file> <description> [--fps F] [--method hold|linear]
                               Redaction boxes from the local senses role + coverage.
  media job status <id>        Job record: state, progress, error.
  media job result <id>        Poll until "ready": true; then "output" is the file.
  media job cancel <id>        Cancel a queued or running job.
  media job list               Every job, oldest first.
  media edit|search|job overview   Summarise a noun's verbs.

Workflow (every step dry-run first)
-----------------------------------
  {workflow}

Edit list (JSON; full schema: media explain edit plan)
------------------------------------------------------
  {{"input": "in.mkv", "output": "out.mkv",
   "segments": [{{"start": 2.0, "end": 5.0, "ops": [
       {{"op": "crop", "x": 0, "y": 0, "w": 160, "h": 120}},
       {{"op": "box", "regions": [{{"x": 10, "y": 10, "w": 40, "h": 40}}], "fill": "black"}},
       {{"op": "blur", "regions": [...], "strength": 5}},
       {{"op": "speed", "factor": 2}}, {{"op": "fade", "direction": "in", "duration": 1}}]}},
     {{"start": 20.0, "end": 24.0}}],
   "transitions": [{{"type": "xfade", "style": "fade", "duration": 0.5}}]}}
  A cut is a segment; N segments joined by xfade/acrossfade transitions make a short.
  The output extension must match the source container (.mkv -> .mkv); the source is
  never modified and an existing output is refused unless "overwrite": true.

Rules
-----
  * Write verbs (edit apply, search index, search purge) are a dry run unless --apply.
  * Time base: normalized seconds from the first presented video frame. A search hit's
    start/end goes straight into an edit-list segment and selects the same frame.
  * The daemon (a private unix-socket job runner) is spawned on demand only by a
    submit: edit apply --apply or search index --apply. Help, overview, explain,
    learn, doctor, probe, frames and job verbs never start it.
  * Sensing is local-only: a senses/stt role served by a mesh peer is refused
    (env.sense_not_local); an unreachable gateway is env.sense_unavailable.
  * Redaction fails closed and edit regions reports coverage (frames without
    detection) -- an empty result is not proof of absence.

Machine-readable output
-----------------------
Every command supports --json: one document on stdout. Errors in JSON mode emit
{{"code", "message", "remediation", "kind"}} to stderr; kind is a stable machine
string (input.* or env.*). Stdout and stderr never mix; no traceback ever leaks.

Exit-code policy
----------------
  0 success
  1 user-input error (bad flag, bad path, invalid edit list: input.*)
  2 environment / setup error (ffmpeg missing, gateway or daemon down: env.*)
  3+ reserved

More detail
-----------
  media explain media
  media explain edit plan
"""


def _render_text() -> str:
    return _TEXT.format(purpose=textwrap.fill(_PURPOSE, 84), workflow="\n  ".join(_WORKFLOW))


def _as_json_payload() -> dict[str, object]:
    return {
        "tool": "media",
        "distribution": "media-cli",
        "version": __version__,
        "purpose": _PURPOSE,
        "commands": [{"path": list(path), "summary": summary} for path, summary in _COMMANDS],
        "after_state": [
            {"clause": clause, "commands": [list(p) for p in paths]}
            for clause, paths in _AFTER_STATE
        ],
        "workflow": list(_WORKFLOW),
        "conventions": {
            "dry_run": "write verbs change nothing unless --apply",
            "time_base": "normalized seconds from the first presented video frame",
            "daemon": "spawned on demand only by edit apply --apply / search index --apply",
            "sensing": "local-only lobes senses gateway; fail-closed",
            "edit_list_schema": "media explain edit plan",
        },
        "exit_codes": {
            "0": "success",
            "1": "user-input error (input.*)",
            "2": "environment/setup error (env.*)",
        },
        "json_support": True,
        "explain_pointer": "media explain <path>",
    }


def cmd_learn(args: argparse.Namespace) -> int:
    if getattr(args, "json", False):
        emit_result(_as_json_payload(), json_mode=True)
    else:
        emit_result(_render_text(), json_mode=False)
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "learn",
        help="Print a structured self-teaching prompt for agent consumers.",
    )
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=cmd_learn)
