"""Markdown catalog for ``media explain <path>``.

Each entry is verbatim markdown. Keys are command-path tuples. The empty tuple,
``("media",)`` (the installed console command -- the rubric gate runs
``explain media``) and ``("media-cli",)`` (the distribution / agent nick) all
resolve to the root entry; keep all three.

Keep bodies self-contained: an agent reading one entry should get enough
context without chaining reads. Every example names the installed command
``media`` -- ``media-cli`` is the PyPI distribution and mesh nick, not a binary.
"""

from __future__ import annotations

_EXIT = """\
## Exit codes and errors

- `0` success; `1` your input is at fault; `2` the environment is at fault
  (missing ffmpeg, unreachable gateway or daemon); `3+` reserved.
- Errors go to stderr only, never mixed with results on stdout. Text mode prints
  `error: <message>` + `hint: <remediation>`; with `--json` stderr carries one
  object `{"code", "message", "remediation", "kind"}` where `kind` is a stable
  machine string (`input.*` exits 1, `env.*` exits 2). No traceback ever leaks.
"""

_ROOT = """\
# media

`media` (distribution and mesh nick: `media-cli`) is the AgentCulture agent that
owns the **local media I/O device plane** and the **editing of the media captured
from it**. It moves and transforms bytes; it does not interpret them. Meaning
(what a frame shows, what was said) is asked of the local lobes senses gateway --
local-only and fail-closed: a sense role served by a mesh peer is refused.

Not this tool: capture (webcam-cli), generation (innereye, harmonics-cli),
generic shell/ffmpeg passthrough (shell-cli). The device-plane inventory verbs
(list/describe of speakers, cameras, mics) are not built yet.

## Verbs

Introspection (read-only):

- `media whoami` -- identity from `culture.yaml`.
- `media learn` -- the self-teaching prompt (start here).
- `media explain <path>` -- these docs, for any noun/verb path.
- `media overview` -- descriptive snapshot of the agent.
- `media doctor` -- check the agent-identity invariants.
- `media cli overview` -- describe the CLI surface.

Media files (read-only):

- `media probe <file>` -- streams, duration, time origin.
- `media frames <file> --at|--every|--scene ... --out DIR` -- extract PNG stills.

Editing and search (write verbs: dry run unless `--apply`):

- `media edit plan|apply|regions|overview` -- JSON edit lists compiled to ffmpeg.
- `media search index|query|purge|cache|overview` -- semantic search with evidence.
- `media job status|result|cancel|list|overview` -- poll daemon jobs by id.

## The workflow

    media probe in.mkv --json                          # facts + time origin
    media frames in.mkv --every 5 --out /tmp/peek --sheet --json
    media search index in.mkv --fps 0.5 --json         # dry run: sense-call estimate
    media search index in.mkv --fps 0.5 --apply --json # -> {"job_id": ...}
    media job result <job_id> --json                   # poll until "ready": true
    media search query in.mkv "a dog" --modality all --json   # hits with evidence
    media edit plan edit.json --json                   # dry run: compiled plan
    media edit apply edit.json --apply --json          # -> {"job_id": ...}
    media job result <job_id> --json                   # output path once done

## Conventions

- **Time base**: every time accepted or returned is *normalized seconds* --
  seconds from the first presented video frame (container `start_time`
  removed; VFR handled by PTS). A search hit's `start` can go straight into an
  edit list and selects the same frame.
- **Dry run first**: write verbs (`edit apply`, `search index`, `search purge`)
  print what they would do and change nothing unless `--apply` is given.
- **Daemon**: a private local job runner on a unix socket
  (`$XDG_RUNTIME_DIR/media-cli/daemon.sock`, else `~/.cache/media-cli/run/`).
  It is spawned on demand **only** by a submit (`edit apply --apply`,
  `search index --apply`) and exits when idle. Every other verb -- help,
  overview, explain, learn, doctor, probe, frames, job * -- never starts it.
- **Sources are never modified**; outputs are written atomically and an existing
  output is refused unless the edit list sets `"overwrite": true`.
- Every command takes `--json`; stdout carries exactly one result document.

""" + _EXIT

_WHOAMI = """\
# media whoami

Reports the agent's identity from `culture.yaml`: nick (`suffix`), backend,
served model, and the package version. Read-only.

## Usage

    media whoami
    media whoami --json   # {"nick", "version", "backend", "model"}

Exit 0 (a missing `culture.yaml` reports placeholders).
"""

_LEARN = """\
# media learn

Prints a structured self-teaching prompt: purpose and lane, every command,
the probe -> frames -> search -> edit -> job workflow, the edit-list schema in
brief, dry-run/`--apply` semantics, the time base, and the exit-code policy.

## Usage

    media learn
    media learn --json   # {"tool", "distribution", "version", "purpose", "commands":
                         #  [{"path", "summary"}], "after_state": [{"clause", "commands"}],
                         #  "workflow", "exit_codes", "json_support", "explain_pointer"}

Read-only; exit 0.
"""

_EXPLAIN = """\
# media explain <path>

Prints markdown documentation for any noun/verb path. Unlike `--help` (terse,
positional), `explain` is global and addressable by path.

## Usage

    media explain media
    media explain edit plan
    media explain --json search query   # {"path": [...], "markdown": "..."}

An unknown path exits 1 with a hint.
"""

_OVERVIEW = """\
# media overview

Read-only descriptive snapshot of the agent: identity (from `culture.yaml`), its
lane, the verb surface, and the artifacts it carries. Accepts an ignored
`target` so a stray path never hard-fails.

## Usage

    media overview
    media overview --json   # {"subject": "media-cli", "sections": [{"title", "items"}]}

Exit 0.
"""

_DOCTOR = """\
# media doctor

Checks the agent-identity invariants `steward doctor` verifies:
prompt-file-present and backend-consistency (`colleague` -> `AGENTS.colleague.md`),
plus a skills-present check. Read-only; never starts the media daemon.

## Usage

    media doctor
    media doctor --json   # {"healthy": bool, "checks": [{"id", "passed", "severity",
                          #  "message", "remediation"}]}

Exit 0 when healthy, 1 when not.
"""

_CLI = """\
# media cli

Noun group for CLI-surface introspection. `media cli overview` describes the CLI
itself -- verbs and conventions (distinct from the global `overview`, which
describes the agent).

## Usage

    media cli overview
    media cli overview --json   # {"subject", "sections": [{"title", "items"}]}

Exit 0.
"""

_PROBE = """\
# media probe <file>

Read-only facts about a media file via ffprobe. Never writes anything and never
touches the daemon.

## Usage

    media probe in.mkv
    media probe in.mkv --json

## JSON output

    {"path", "format_name", "duration", "start_time", "tags": {},
     "streams": [{"index", "type", "codec", "duration", "start_time", "fps",
                  "width", "height", "sample_rate", "channels",
                  "disposition", "tags", "attached_pic"}],
     "video": <stream>|null, "audio": <stream>|null, "origin": <raw seconds>}

`origin` is the raw time of the first presented video frame: normalized second
`0.0` everywhere else in `media`. `duration`/`start_time` are raw container
values, reported for transparency. `video` skips attached cover art.

## Errors

- `input.unreadable` (exit 1) -- missing, truncated or not a media file.
- `env.ffmpeg_missing` (exit 2) -- ffprobe is not installed; the hint names it.
- `env.ffmpeg_failed` (exit 2) -- ffprobe failed; its stderr tail is the hint.

""" + _EXIT

_FRAMES = """\
# media frames <file> --at T... | --every N | --scene THR --out DIR

Peek at a video: extract still PNG frames (and optionally a contact sheet) with
local ffmpeg. Works with no network at all. Writes only into `--out` (created
if missing); the source is never modified; the daemon is never touched.

## Flags

- `--at T [T ...]` -- timestamps in normalized seconds (first frame = 0).
- `--every N` -- one frame every N seconds.
- `--scene THR` -- frames at scene changes, threshold 0..1.
  (exactly one of `--at`/`--every`/`--scene`; at most 200 frames)
- `--out DIR` -- required; PNGs go here.
- `--sheet` -- also write `DIR/contact_sheet.png`.
- `--describe` -- optional: ask the local `senses` role to describe each frame.
  Fail-soft: if the gateway is down or not local, the command still exits 0
  and the reason is reported inside `describe`.
- `--overwrite` -- replace existing output PNGs (else `input.output_exists`).

## Usage

    media frames in.mkv --at 1 2.5 --out /tmp/peek --json
    media frames in.mkv --every 5 --out /tmp/peek --sheet --describe --json

## JSON output

    {"source", "out", "count",
     "frames": [{"t", "frame_index", "path"}],
     "sheet": <path>|null,
     "describe": null
               | {"ok": true, "descriptions": [{"path", "text"}]}
               | {"ok": false, "kind": "env.sense_unavailable|env.sense_not_local", "message"}}

## Errors

- exit 1: `input.unreadable`, `input.timestamp_out_of_range`,
  `input.bad_frame_request`, `input.too_many_frames`, `input.output_exists`,
  `input.output_dir_missing`.
- exit 2: `env.ffmpeg_missing`, `env.ffmpeg_failed`.

The gateway URL comes from `MEDIA_CLI_LOBES_URL` (default
`http://localhost:8001`; key in `MEDIA_CLI_LOBES_KEY`).

""" + _EXIT

_EDIT = """\
# media edit

Edit media files from a declarative JSON edit list: cut segments, crop,
black-box redact, blur, speed, fades, and xfade/acrossfade transitions that
assemble a long video into a short. The edit list is validated against ffprobe
facts and compiled into ONE ffmpeg command built only from an allowlist of
filters with typed parameters -- no edit-list string ever reaches a filtergraph.

## Verbs

- `media edit plan <editlist.json> [--fast]` -- validate + compile; print the plan.
  Writes nothing. Full edit-list schema: `media explain edit plan`.
- `media edit apply <editlist.json> [--fast] [--apply]` -- dry run (same as plan)
  unless `--apply`, which submits the edit as a daemon job and returns its id.
- `media edit regions <file> <description>` -- ask the local senses role where
  something is, returning `box`/`blur` regions plus a coverage report.
- `media edit overview` -- the verb summary (`media edit` alone prints it too).

## Usage

    media edit overview --json   # {"subject": "media-cli edit", "sections": [...]}
    media edit plan edit.json --json
    media edit apply edit.json --apply --json

""" + _EXIT

_EDIT_PLAN = """\
# media edit plan <editlist.json> [--fast]

Validate an edit list and compile it to the exact ffmpeg invocation that
`media edit apply --apply` would run. **Writes nothing, starts nothing**: no
ffmpeg encode, no daemon. Every rejection happens here, before any process runs.

## Flags

- `--fast` -- keyframe-snapped stream copy (one op-free segment, no transitions).
  Exact only when both cut points are keyframes; otherwise the plan reports the
  snapped points in `snapped`. Default is frame-accurate (trim + re-encode).
- `--json` -- emit the plan as JSON.

## Edit-list schema (closed: an unknown key anywhere is rejected)

    {
      "input": "in.mkv",                # source path (never modified)
      "output": "out.mkv",              # new file; extension MUST match the source
                                        #   container (.mkv -> .mkv, .mp4 -> .mp4/.m4v)
      "overwrite": false,               # replace an existing output (never the source)
      "keep": ["subtitles"],            # subset of subtitles|attachments|data|metadata
      "segments": [                     # non-empty; concatenated in order
        {"start": 2.0, "end": 5.0,      # normalized seconds
         "ops": [                       # applied in order, default []
           {"op": "crop",  "x": 0, "y": 0, "w": 160, "h": 120},
           {"op": "speed", "factor": 2},                         # 0.25..4.0
           {"op": "fade",  "direction": "in", "duration": 1},    # in|out
           {"op": "box",   "regions": [REGION], "fill": "black"},   # black-box redaction
           {"op": "blur",  "regions": [REGION], "strength": 5}      # 1..50
         ]}
      ],
      "transitions": [                  # none, or exactly len(segments)-1
        {"type": "xfade", "style": "fade", "duration": 0.5}
      ]                                 # type xfade|acrossfade;
    }                                   # style fade|dissolve|wipeleft|slideleft

    REGION = {"x": int, "y": int, "w": int, "h": int, "start"?: sec, "end"?: sec}

- A **cut** is a segment; several segments plus `xfade` transitions make a short.
- Times are **normalized seconds** (first presented video frame = 0), bounded by
  the media; region `start`/`end` are in the same absolute base and must lie
  inside their segment. Pixel fields are integers; a `crop` shrinks the frame
  seen by later ops of that segment. Fade/transition durations must fit the
  segment's source length.
- Types are strict: `"5"`, `true`, `NaN`, `null` are not numbers.
- Redaction (`box`, `blur`) fails closed: the video is always re-encoded, and
  subtitles, attachments/cover art, data streams and container metadata are
  dropped unless named in `keep`.
- Output rule: the container is the source container; untouched streams are
  stream-copied, changed ones re-encoded preferring the source codec. Only when
  the container cannot carry the result does it fall back to `.mkv`
  (`output.container_fallback` says why).

## Usage

    media edit plan edit.json
    media edit plan edit.json --fast --json

## JSON output

    {"dry_run": true, "mode": "filter|passthrough|remux|fast",
     "args": [<ffmpeg argv without the binary>], "graph": <filtergraph>|null,
     "expected_duration": <s>, "segments": [...],
     "snapped": {"start", "end", "exact"}|null,
     "redacted": bool, "metadata": "kept|dropped", "flags": {...},
     "output": {"src", "dst", "requested_dst", "container", "container_fallback",
                "overwrite",
                "streams": [{"index", "type", "action": "copy|encode|drop",
                             "codec", "encoder", "reason"}]}}

## Errors

- exit 1: `input.editlist_invalid` (message starts with the JSON path, e.g.
  `segments[1].ops[0].regions[2].w: ...`), `input.unreadable`,
  `input.timestamp_out_of_range`, `input.region_outside_frame`,
  `input.region_too_small` (blur area too small), `input.output_is_source`,
  `input.output_exists`, `input.output_dir_missing`,
  `input.output_container_mismatch`, `input.container_incompatible`,
  `input.fast_unsupported`.
- exit 2: `env.ffmpeg_missing`, `env.filter_unavailable`, `env.ffmpeg_failed`.

""" + _EXIT

_EDIT_APPLY = """\
# media edit apply <editlist.json> [--fast] [--apply]

Write verb. **Without `--apply` it is a dry run identical to `media edit plan`**
(`{"dry_run": true, ...}`, nothing written, no daemon). With `--apply` it
re-validates, compiles, and submits the ffmpeg command as a job to the local
media daemon -- spawning the daemon on demand if none is running -- then returns
the job id immediately, before any encoding happens.

## Flags

- `--apply` -- actually submit the job.
- `--fast` -- keyframe-snapped stream copy (see `media explain edit plan`).
- `--json` -- JSON output.

## Usage

    media edit apply edit.json --json            # dry run
    media edit apply edit.json --apply --json    # submit
    media job result <job_id> --json             # poll until "ready": true

## JSON output (`--apply`)

    {"job_id": "<id>", "output": "<final path>", "plan": {<the edit plan JSON>}}

The job writes to a hidden temp file in the output directory and renames it into
place on success, so the output path is either complete or absent; a failed or
cancelled job leaves no partial file. The source file is never modified. Poll
with `media job status|result <job_id>`; the job record keeps the ffmpeg argv,
its log path and progress.

## Errors

Every `media edit plan` error (validated before submitting), including
`input.output_container_mismatch` when the output extension does not match the
source container, plus on `--apply`: `env.daemon_unavailable`,
`env.daemon_setup`, `env.daemon_protocol` (exit 2). A job that fails later
reports its error inside `media job result` (`job.error.kind`, e.g.
`env.ffmpeg_failed` with the stderr tail and `log_path`, or
`input.output_exists` if the path appeared after planning).

""" + _EXIT

_EDIT_REGIONS = """\
# media edit regions <file> <description>

Find redaction regions for a description ("the license plate") by sampling
frames and asking the local `senses` vision role for boxes. Creates no file
(sampled frames go to a private temp dir that is removed) and never touches the
daemon. Paste the returned `regions` into a `box` or `blur` op.

## Flags

- `--fps F` -- sampling rate (default 2). Between samples regions are held or
  interpolated, so a higher fps narrows uncovered gaps.
- `--method hold|linear` -- `hold` (default): one region per interval covering
  the union of both samples' boxes; `linear`: interpolated sub-intervals.
- `--start S` / `--end E` -- limit to a window (normalized seconds).
- `--margin PX` -- pad every box by PX pixels (clamped to the frame).

## Usage

    media edit regions in.mkv "the license plate" --fps 4 --margin 8 --json

## JSON output

    {"regions": [{"x", "y", "w", "h", "start", "end"}],
     "coverage": {"sample_fps", "method", "samples",
                  "frames_without_detection": [[a, b], ...],
                  "rejected_boxes": [{"t", "box", "reason"}]},
     "model": "<served senses model>", "description": "..."}

Redaction **coverage** is reported, never implied: `frames_without_detection`
lists every time range where no detection bracketed the frames, and
`rejected_boxes` every malformed/clamped/out-of-frame reply. An empty result is
not proof of absence. The model's box accuracy is not measured.

## Errors

- exit 1: `input.unreadable`, `input.bad_region_request`,
  `input.timestamp_out_of_range`.
- exit 2: `env.sense_unavailable` (gateway unreachable),
  `env.sense_not_local` (the role is served by a mesh peer -- refused: media
  never leaves this machine), `env.ffmpeg_missing`.

""" + _EXIT

_EDIT_OVERVIEW = """\
# media edit overview

Summarise the `edit` verbs. `media edit` with no verb prints the same thing.

## Usage

    media edit overview
    media edit overview --json   # {"subject": "media-cli edit", "sections": [...]}

Read-only; exit 0.
"""

_SEARCH = """\
# media search

Semantic search inside media: "where is the dog", "when does someone say
budget". Two phases: **index** (sample frames, caption them through the local
`senses` role, cache the captions keyed by file fingerprint + sense identity)
and **query** (match the text against cached captions and/or a chunked speech
transcript). Every hit carries its time range and the evidence it came from.

## Verbs

- `media search index <file> [--fps F|--scene T] [--apply]` -- dry run prints
  the sense-call estimate; `--apply` submits an index job to the daemon.
- `media search query <file> <text> [--modality frames|speech|all]` -- find hits.
- `media search purge <file> [--apply]` -- dry run lists, `--apply` removes the
  file's cached index and transcripts.
- `media search cache [<file>]` -- inspect the cache.
- `media search overview` -- the verb summary.

Cache: `$XDG_CACHE_HOME/media-cli/index` (size-capped, LRU). Re-querying an
unchanged file never re-runs the caption model; changing the served model or
prompt version re-indexes.

## Usage

    media search index in.mkv --fps 0.5 --apply --json
    media search query in.mkv "a red car" --json

""" + _EXIT

_SEARCH_INDEX = """\
# media search index <file> [--fps F | --scene T] [--apply]

Write verb, budgeted. **Without `--apply` it is a dry run**: it plans the
frame sampling and prints the exact number of sense calls it would make; it
makes no caption call and writes nothing. A plan above `--max-calls` is
refused (`input.budget_exceeded`) before any gateway request -- with or
without `--apply`. With `--apply` it submits an `index` job to the local
daemon (spawned on demand) and returns the job id at once.

## Flags

- `--fps F` -- frames per second to sample (default 0.5), or
- `--scene T` -- sample at scene changes (threshold 0..1).
- `--batch-size N` -- frames per caption call (default 8).
- `--max-calls N` -- sense-call cap (default 600); raise it deliberately.
- `--apply` -- submit the job.

Use the same `--fps`/`--scene` for `media search query` -- the index is keyed by
the sampling parameters.

## Usage

    media search index in.mkv --fps 0.5 --json          # estimate
    media search index in.mkv --fps 0.5 --apply --json  # submit
    media job result <job_id> --json                    # poll

## JSON output

    dry run:  {"dry_run": true, "frames", "batches", "sense_calls", "cap",
               "cached": bool, "identity": {...}}
    --apply:  {"job_id": "<id>", "plan": {<the dry-run fields>}}

`sense_calls` is 0 when an index with the same identity is already cached.
Frames and captions stay on this host: the `senses` role must be served
locally (verified via the gateway's `/capabilities`), otherwise the job fails
with `env.sense_not_local`.

## Errors

- exit 1: `input.budget_exceeded`, `input.bad_index_request`, `input.unreadable`.
- exit 2: `env.ffmpeg_missing`, `env.daemon_unavailable`, `env.daemon_setup`.
- inside the job record: `env.sense_unavailable`, `env.sense_not_local`.

""" + _EXIT

_SEARCH_QUERY = """\
# media search query <file> <text> [--modality frames|speech|all]

Find where `<text>` occurs. Read-only toward the file; never starts the daemon.

- `frames` (default) matches cached frame captions -- **build the index first**
  with `media search index <file> --apply`; otherwise `input.index_missing`
  with a hint naming the exact index command. Zero caption calls on this path.
- `speech` transcribes the audio through the local `stt` role in <= 30 s
  overlapping chunks (cached per file), with timestamps offset per chunk.
- `all` returns both, sorted by `start`.

Matching is a yes/no + confidence judgement by the `senses` model, not
substring matching.

## Flags

- `--modality frames|speech|all`
- `--threshold X` -- minimum confidence 0..1 (default 0.5).
- `--fps F` / `--scene T` -- which index to read (must match the build).

## Usage

    media search query in.mkv "a dog" --json
    media search query in.mkv "budget" --modality speech --json

## JSON output

    {"file", "query", "modality",
     "hits": [{"start", "end", "score", "frame_index"?,
               "evidence": {"kind": "frame", "frame_path", "caption", "model"}
                         | {"kind": "speech", "transcript", "text", "model"},
               "samples": [...]}]}

Hit times are normalized seconds: feed `start`/`end` straight into an edit-list
segment or a `frames --at`.

## Errors

- exit 1: `input.index_missing`, `input.bad_query`, `input.unreadable`.
- exit 2: `env.sense_unavailable` (gateway down -- an error, never an empty
  result), `env.sense_not_local`, `env.ffmpeg_missing`.

""" + _EXIT

_SEARCH_PURGE = """\
# media search purge <file> [--apply]

Write verb. **Without `--apply` it is a dry run** listing the cached index
directories and transcripts of `<file>` that would be removed. With `--apply`
it removes them. The media file itself is never touched.

## Usage

    media search purge in.mkv --json
    media search purge in.mkv --apply --json

## JSON output

    dry run:  {"dry_run": true, "file", "would_remove": {"indexes": [...],
               "transcripts": [...]}, "total_bytes"}
    --apply:  {"dry_run": false, "file", "removed": {"indexes": n, "transcripts": m}}

""" + _EXIT

_SEARCH_CACHE = """\
# media search cache [<file>]

Inspect the search cache -- everything, or only `<file>`'s entries. Read-only.

## Usage

    media search cache --json
    media search cache in.mkv --json

## JSON output

    {"indexes": [{"dir", "bytes", "source", "fingerprint", "identity", "entries"}],
     "transcripts": [{"file", "bytes", "source", "fingerprint", "segments"}],
     "total_bytes"}

""" + _EXIT

_SEARCH_OVERVIEW = """\
# media search overview

Summarise the `search` verbs. `media search` with no verb prints the same.

## Usage

    media search overview
    media search overview --json   # {"subject": "media-cli search", "sections": [...]}

Read-only; exit 0.
"""

_JOB = """\
# media job

Inspect and cancel jobs run by the local media daemon. Jobs are created only by
`media edit apply --apply` and `media search index --apply`, which return a
`job_id` immediately; you then poll. **No `job` verb ever starts the daemon**:
with no daemon running they read the durable job store
(`$XDG_STATE_HOME/media-cli/jobs`) directly, so a job survives the submitting
process and can be polled from a fresh shell.

## The polling loop

    media job result <job_id> --json    # repeat until "ready": true
    # then: job.state == "done" -> use "output"; "failed" -> read job.error

## Verbs

- `media job status <id>` -- the job record (state, progress, error).
- `media job result <id>` -- `ready` flag and output path once done.
- `media job cancel <id>` -- cancel a queued or running job.
- `media job list` -- every job, oldest first.
- `media job overview` -- the verb summary.

## Job record

    {"schema_version": 1, "id", "kind": "ffmpeg|index",
     "state": "queued|running|done|failed|cancelled",
     "argv", "sense_calls", "timings": {"created", "started", "finished"},
     "progress": null | {"fraction": 0..1, ...},
     "output": <path>|null,
     "error": null | {"kind", "message", "stderr_tail", "log_path"},
     "meta": {}}

""" + _EXIT

_JOB_STATUS = """\
# media job status <job_id>

Show one job record. Never starts the daemon: if none is running the record
is read from the store (`"via": "store"`).

## Usage

    media job status <job_id> --json

## JSON output

    {"job": {<job record, see `media explain job`>}, "via": "daemon|store"}

`job.progress.fraction` (0..1) comes from ffmpeg's progress stream.

## Errors

- exit 1: `input.job_not_found`, `input.job_corrupt`,
  `input.job_schema_unsupported`.
- exit 2: `env.daemon_protocol`.

""" + _EXIT

_JOB_RESULT = """\
# media job result <job_id>

The polling verb. Returns whether the job is finished and, once it is `done`,
its output path. Never starts the daemon.

## Usage

    media job result <job_id> --json   # repeat until "ready": true

## JSON output

    {"job": {<job record>}, "ready": bool, "output": <path>|null,
     "via": "daemon|store"}

`ready` is true for any terminal state (`done`, `failed`, `cancelled`); check
`job.state`. A failed job carries `job.error` with the ffmpeg stderr tail and a
readable `log_path`.

## Errors

- exit 1: `input.job_not_found`, `input.job_corrupt`.
- exit 2: `env.daemon_protocol`.

""" + _EXIT

_JOB_CANCEL = """\
# media job cancel <job_id>

Cancel a queued or running job. A running job's ffmpeg process group is killed
(SIGTERM, then SIGKILL) and its temp output removed -- no partial file is left.
Never starts the daemon: with none running, a queued job is marked cancelled in
the store, and a running one is refused with `env.daemon_not_running`.

## Usage

    media job cancel <job_id> --json

## JSON output

    {"job": {<job record, state "cancelled">}, "via": "daemon|store"}

## Errors

- exit 1: `input.job_not_found`, `input.job_illegal_transition` (already
  done/failed/cancelled).
- exit 2: `env.daemon_not_running`, `env.daemon_protocol`.

""" + _EXIT

_JOB_LIST = """\
# media job list

Every job record, oldest first, read from the durable store. Never starts the
daemon.

## Usage

    media job list --json   # {"jobs": [{<job record>}, ...]}

""" + _EXIT

_JOB_OVERVIEW = """\
# media job overview

Summarise the `job` verbs. `media job` with no verb prints the same.

## Usage

    media job overview
    media job overview --json   # {"subject": "media-cli job", "sections": [...]}

Read-only; exit 0.
"""


ENTRIES: dict[tuple[str, ...], str] = {
    (): _ROOT,
    ("media",): _ROOT,
    ("media-cli",): _ROOT,
    ("whoami",): _WHOAMI,
    ("learn",): _LEARN,
    ("explain",): _EXPLAIN,
    ("overview",): _OVERVIEW,
    ("doctor",): _DOCTOR,
    ("cli",): _CLI,
    ("cli", "overview"): _CLI,
    ("probe",): _PROBE,
    ("frames",): _FRAMES,
    ("edit",): _EDIT,
    ("edit", "plan"): _EDIT_PLAN,
    ("edit", "apply"): _EDIT_APPLY,
    ("edit", "regions"): _EDIT_REGIONS,
    ("edit", "overview"): _EDIT_OVERVIEW,
    ("search",): _SEARCH,
    ("search", "index"): _SEARCH_INDEX,
    ("search", "query"): _SEARCH_QUERY,
    ("search", "purge"): _SEARCH_PURGE,
    ("search", "cache"): _SEARCH_CACHE,
    ("search", "overview"): _SEARCH_OVERVIEW,
    ("job",): _JOB,
    ("job", "status"): _JOB_STATUS,
    ("job", "result"): _JOB_RESULT,
    ("job", "cancel"): _JOB_CANCEL,
    ("job", "list"): _JOB_LIST,
    ("job", "overview"): _JOB_OVERVIEW,
}
