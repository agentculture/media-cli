# media-cli

Agent and CLI for **local media**: it owns the **device plane** (one inventory,
one stable identity scheme and one routing surface across the media hardware
attached to a machine) and the **editing of the media captured from it** —
probe a file, peek at frames, search frames and speech by meaning, and cut,
crop, redact, blur, speed up, fade and join segments with declarative JSON edit
lists run as daemon jobs.

It moves and transforms bytes; it does not interpret them. What a frame shows or
what was said is asked of the local `lobes` senses gateway (local-only,
fail-closed). It composes with its siblings rather than absorbing them:

| Not ours | Owner |
|----------|-------|
| Camera and microphone capture | [`webcam-cli`](https://github.com/agentculture/webcam-cli) |
| Generation (text/image to image and video) | `innereye` |
| *What* to play; non-TTS audio synthesis | [`harmonics-cli`](https://github.com/agentculture/harmonics-cli) |
| General shell / raw `ffmpeg <args>` execution | `shell-cli` |

## Status

- **Editing: built.** `probe`, `frames`, `edit`, `search` and `job` ship, backed
  by `ffmpeg`/`ffprobe` and a spawn-on-demand job daemon. Design:
  [`docs/specs/2026-09-30-media-file-editing.md`](docs/specs/2026-09-30-media-file-editing.md).
- **Device plane: not built yet.** There is no `list`/`describe`, routing or
  playback verb, and `media_cli/` contains no device code. The build brief,
  including the host survey and open questions, is
  [issue #1](https://github.com/agentculture/media-cli/issues/1).

[`CLAUDE.md`](CLAUDE.md) carries the working notes: architecture, verified
device-plane constraints, and the conventions.

## Requirements

- Python 3.12+; no Python runtime dependencies.
- **`ffmpeg` and `ffprobe` on `PATH`** for every editing verb. They are detected,
  never assumed: when missing (or lacking a needed filter/encoder) a verb exits 2
  with an install hint.
- A running **lobes senses gateway** is optional — only `frames --describe`,
  `search index`, speech search and `edit regions` need it. Without it those
  fail with a typed `env.sense_unavailable` error and everything else works.

## Why a device plane

Surveyed on a real Linux workstation, the problem is not "call `aplay`":

- **Indices are not identity, at any layer.** `/dev/videoN` and ALSA card
  numbers are plug-order and have been observed swapping between two cameras
  with no hardware change; PipeWire object IDs churn faster still. `"capture
  from device 0"` is not a reproducible instruction.
- **The subsystems disagree about what exists.** Raw ALSA lists five playback
  entries where PipeWire has two usable sinks (four HDMI entries are one
  physical output), and lists a microphone that PipeWire does not expose at all.
- **Formats are device-native and incompatible.** One sink is 16 kHz, another
  48 kHz. "Play this WAV" silently means resample-or-fail.
- **Access can be granted by a login seat rather than a group**, so a device
  that works from a desktop session is unopenable from a headless agent or
  container — and the diagnosis differs between video and audio.

Capture and playback are already spoken for in the mesh. The plane underneath
them is not, and that is where these problems live.

## Quickstart

```bash
uv sync
uv run pytest -n auto                 # run the test suite
uv run media whoami                   # identity from culture.yaml
uv run media learn                    # self-teaching prompt (add --json)
uv run teken cli doctor . --strict    # the agent-first rubric gate CI runs
```

The console command is **`media`**. The distribution and import package are
`media-cli` and `media_cli` — deliberately distinct, so installing this does not
squat a generic `media` module in a consumer's environment.

Working with a media file (every step is dry-run first, every result `--json`):

```bash
media probe in.mkv --json                                  # streams, duration, time origin
media frames in.mkv --every 5 --out /tmp/peek --sheet      # local PNG stills + contact sheet

media search index in.mkv --fps 0.5 --json                 # dry run: sense-call estimate
media search index in.mkv --fps 0.5 --apply --json         # -> {"job_id": ...}
media job result <job_id> --json                           # poll until "ready": true
media search query in.mkv "a dog" --modality all --json    # hits: start/end + evidence

media edit plan edit.json --json                           # validate + compiled ffmpeg plan, writes nothing
media edit apply edit.json --apply --json                  # -> {"job_id": ...}
media job result <job_id> --json                           # output path once done
```

An edit list is JSON; a cut is a segment, and several segments joined by
transitions make a short:

```json
{"input": "in.mkv", "output": "out.mkv",
 "segments": [
   {"start": 2.0, "end": 5.0, "ops": [
     {"op": "crop", "x": 0, "y": 0, "w": 160, "h": 120},
     {"op": "box", "regions": [{"x": 10, "y": 10, "w": 40, "h": 40}], "fill": "black"},
     {"op": "fade", "direction": "in", "duration": 1}]},
   {"start": 20.0, "end": 24.0}],
 "transitions": [{"type": "xfade", "style": "fade", "duration": 0.5}]}
```

Times everywhere are *normalized seconds* from the first presented video frame,
so a search hit's `start`/`end` can go straight into a segment. The output
extension must match the source container. Full schema: `media explain edit plan`.

## CLI

| Verb | What it does |
|------|--------------|
| `whoami` | Report this agent's nick, version, backend, and model from `culture.yaml`. |
| `learn` | Print a structured self-teaching prompt. |
| `explain <path>` | Markdown docs for any noun/verb path. |
| `overview` | Read-only descriptive snapshot of the agent. |
| `doctor` | Check the agent-identity invariants (prompt-file-present, backend-consistency). |
| `cli overview` | Describe the CLI surface itself. |
| `probe <file>` | Streams, duration and time origin (read-only). |
| `frames <file>` | Extract PNG frames at times, intervals or scene changes; optional sense captions. |
| `edit plan` / `apply` / `regions` | Validate and compile an edit list; submit it as a job; get redaction boxes from the senses role. |
| `search index` / `query` / `purge` / `cache` | Semantic search over frames and speech, with evidence. |
| `job status` / `result` / `cancel` / `list` | Poll and manage daemon jobs by id. |

Every command supports `--json`. Results go to stdout, errors and diagnostics to
stderr (never mixed); in JSON mode errors carry a stable `kind` (`input.*` exits
1, `env.*` exits 2). Exit codes: `0` success, `1` user error, `2` environment
error, `3+` reserved.

## Safety properties

- **Dry run by default.** Write verbs (`edit apply`, `search index`, `search
  purge`) print what they would do and change nothing unless `--apply` is given.
  Physical or irreversible side effects must never fire from a speculative call
  in an agent loop.
- **Sources are never modified.** Outputs are written atomically to a new file; an
  existing output is refused unless the edit list sets `"overwrite": true`.
- **Local-only sensing.** Frames and audio go only to a senses/stt role served on
  this machine; a role proxied to a mesh peer is refused.
- **Redaction fails closed.** Redacted output is always re-encoded, streams that
  cannot be redacted (subtitles, attachments, data) and container metadata are
  dropped unless kept, and `edit regions` reports a coverage summary — frames
  where nothing was detected are listed, so an empty result is never read as
  proof of absence.
- **The daemon is private and on-demand.** A unix socket (mode 0600, peer-uid
  checked) spawned only by a submit (`edit apply --apply`, `search index
  --apply`) and gone when idle; read-only verbs never start it.

## Install

```bash
uv tool install media-cli
```

## Development

See [`CLAUDE.md`](CLAUDE.md) for the full conventions — the version-bump-every-PR
rule, the `cicd` PR lane, lint commands, and the rubric gate. Skill provenance
and the re-sync procedure are in [`docs/skill-sources.md`](docs/skill-sources.md).

## License

Apache 2.0 — see [`LICENSE`](LICENSE).
