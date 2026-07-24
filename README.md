# media-cli

Agent and CLI for the **local media I/O device plane** — one inventory, one
stable identity scheme, and one routing surface across the media hardware
attached to a machine: speakers and audio sinks, optional standalone inputs,
and (by composing [`webcam-cli`](https://github.com/agentculture/webcam-cli),
not reimplementing it) cameras and microphones.

It answers *"what media hardware is on this machine, what can each piece
actually do, which one should I use, and who is holding it right now?"* — device
selection, routing, playback and recording. It does **not** interpret what is
captured or played: what a frame contains is a vision model's job, and *what* to
play belongs to [`harmonics-cli`](https://github.com/agentculture/harmonics-cli)
and TTS engines.

## Status: scaffold

**The domain is not implemented yet.** What ships today is the agent-first CLI
skeleton — six introspection verbs, the error/output contract, CI, and PyPI
publishing. `media_cli/` contains no device code.

The build brief, including the host survey it is derived from and the open
questions that block design, is
[issue #1](https://github.com/agentculture/media-cli/issues/1).
[`CLAUDE.md`](CLAUDE.md) carries the working notes: verified device-plane
constraints, the architecture of the CLI skeleton, and the conventions.

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

## CLI

| Verb | What it does |
|------|--------------|
| `whoami` | Report this agent's nick, version, backend, and model from `culture.yaml`. |
| `learn` | Print a structured self-teaching prompt. |
| `explain <path>` | Markdown docs for any noun/verb path. |
| `overview` | Read-only descriptive snapshot of the agent. |
| `doctor` | Check the agent-identity invariants (prompt-file-present, backend-consistency). |
| `cli overview` | Describe the CLI surface itself. |

Every command supports `--json`. Results go to stdout, errors and diagnostics to
stderr (never mixed). Exit codes: `0` success, `1` user error, `2` environment
error, `3+` reserved.

Write verbs — none yet — are dry-run by default and require `--apply`. For this
agent that is a safety property, not a convention: playback and device
reconfiguration are physically observable side effects, and a speculative call
from an agent loop must not make noise in a room.

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
