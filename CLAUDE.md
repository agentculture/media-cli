# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

`media-cli` is an AgentCulture mesh agent that **owns the local media I/O device
plane** — one inventory, one stable identity scheme, and one routing/arbitration
surface across every attached media device: speakers and audio sinks, optional
standalone inputs, and (by composition, not reimplementation) cameras and
microphones. It answers *"what media hardware is on this machine, what can each
piece actually do, which one should I use, and who is holding it right now?"*

It moves bytes to and from devices. It does not interpret them.

The build brief is [issue #1](https://github.com/agentculture/media-cli/issues/1)
— read it before designing anything. It defines the lane, the host evidence it
was derived from, and six open questions that are deliberately unanswered.

The operator's decision is **compose, not absorb**:

| Not ours | Owner |
|----------|-------|
| Camera + microphone *capture* (getting frames and samples off a device) | `webcam-cli` — route to it |
| What a frame contains / what a sound means | a vision model; not this tool |
| *What* to play (non-TTS sonic signatures) | `harmonics-cli` — see open question 2 |
| Browser / page / screen capture | `webglass-cli` |
| General file + shell execution surface | `shell-cli` |
| The Reachy Mini robot that owns this host's only real speaker | `reachy-mini-cli` |

The load-bearing idea: capture and playback are already spoken for, but nothing
in the mesh owns the **device plane underneath them** — and that plane is where
the hard, easily-got-wrong problems live. If this repo ends up a thin
passthrough to `webcam` plus an `aplay` wrapper, the lane was misjudged; say so
on issue #1 rather than build filler.

## Current state: scaffold, not the product

Nothing in the domain is implemented. `media_cli/` contains **no device code at
all** — what exists is the agent-first CLI skeleton scaffolded from
`culture-agent-template`: six template verbs (`whoami`, `learn`, `explain`,
`overview`, `doctor`, `cli overview`), the error/output contract, CI, and the
vendored skill kit. Everything is green: 22 tests pass, the rubric gate passes
26/26, markdownlint is clean.

Three things about the scaffold to know before touching anything:

1. **The self-description strings are still template prose.** `learn`, the
   `explain` catalog (`media_cli/explain/catalog.py`), `overview`'s artifact
   list, and the parser description in `media_cli/cli/__init__.py:74` all
   describe this repo as *"a clonable template for AgentCulture mesh agents"*.
   That is false — it is now the media device-plane agent. These strings are the
   agent-facing docs, so rewrite them **as the domain surface lands**, not after.
2. **The console command is `media`, but the CLI calls itself `media-cli`.**
   `[project.scripts]` installs `media`; argparse is built with
   `prog="media-cli"` (`media_cli/cli/__init__.py:73`). So `--help`, every error
   hint, `learn`, and the whole `explain` catalog tell an agent to type
   `media-cli explain …` — **which is not an installed binary** (`uv run
   media-cli` fails with "Failed to spawn"). The three-way split is deliberate
   per the brief (command `media`, import package `media_cli`, dist `media-cli`;
   the generic `media` module name is deliberately not squatted, and `colleague`
   already carries an internal `media` module). The self-docs pointing at a
   nonexistent command is not deliberate. `harmonics-cli` fixed exactly this in
   its own issue #2 — follow that precedent. Fix `prog` and the doc strings
   together, or the rubric's learnability guarantee is a lie to its only readers.
3. **The catalog carries both `("media",)` and `("media-cli",)` keys**
   (`explain/catalog.py`). This is not redundancy — the rubric gate runs
   `explain <console-command>`, i.e. `explain media`, so the `("media",)` key is
   what keeps `explain_self` green. Whatever you do to `prog`, keep both keys.

## Identity

`culture.yaml` declares `suffix: media-cli`, `backend: colleague`, model
`sakamakismile/Qwen3.6-27B-Text-NVFP4-MTP`.

`backend: colleague` fixes the **resident mesh prompt** to `AGENTS.colleague.md`.
This file (`CLAUDE.md`) is Claude Code guidance only — the mesh runtime does not
read it. The pair satisfies the two invariants `steward doctor` verifies:
prompt-file-present and backend-consistency; `media doctor` checks the same
locally.

**Open genesis follow-up — the backend is not reconciled.** Issue #1's identity
table lists backend `claude` *(seed)* and instructs you to reconcile it against
the scaffolded `culture.yaml`, which says `colleague`. The retired `CLAUDE.md`
seed also asserted `culture.yaml` declares `backend: claude` — it did not. On
disk today `colleague` is coherent (yaml, `AGENTS.colleague.md`, the `doctor`
mapping, and `tests/test_cli_introspection.py::test_doctor_recognizes_declared_backend`
all agree, and `doctor` reports healthy), so nothing is broken — but the brief's
intent is unsettled. **This is the operator's call, not yours.** Changing it
means changing all four places together; both prompt files already exist on
disk, so `doctor` would stay green either way and will not catch a half-done
switch.

## Commands

```bash
uv sync                                    # install (dev group included)

uv run pytest -n auto                      # full suite, parallel (22 tests)
uv run pytest tests/test_cli.py::test_whoami_json -v   # a single test (drop -n)
uv run pytest -n auto --cov=media_cli --cov-report=term   # coverage; fail_under = 60

uv run media whoami                        # NOTE: `media`, not `media-cli`
uv run python -m media_cli learn --json    # equivalent module entry point
```

Lint — CI runs all five, and `markdownlint` is the one most often forgotten:

```bash
uv run black --check media_cli tests       # line-length 100
uv run isort --check-only media_cli tests
uv run flake8 media_cli tests
uv run bandit -c pyproject.toml -r media_cli
markdownlint-cli2 "**/*.md" "#node_modules" "#.local" "#.claude/skills" "#.teken"
uv run teken cli doctor . --strict         # the agent-first rubric gate (26 checks)
```

The rubric gate is a hard CI gate, not advice. It runs the *installed* CLI and
asserts learnability, JSON parseability, the `hint:`/exit-code error contract,
and that every noun with action-verbs exposes `overview`. Any new noun you add
must satisfy it.

**Tooling drift warning:** `teken` has been renamed to `agentfront` upstream —
the globally installed `teken` (0.18.0) now prints a deprecation notice and says
it "will be removed in a future release". CI and `uv.lock` pin `teken 0.8.0`,
which predates the rename, so the gate works today. But the dev-dep specifier is
`teken>=0.8`, so a `uv lock --upgrade` can pull a version whose `teken` console
script is gone and break CI. Rename the dep and the CI step together when that
happens.

## Architecture

The CLI is **cited, not imported**, from teken's `python-cli` reference
(`teken cli cite`). That is why `dependencies = []` in `pyproject.toml` and
`teken` is dev-only. This repo owns its copy outright — edit it freely; do not
try to upgrade it from upstream.

**The zero-runtime-dependency posture is load-bearing and is the first thing the
domain work will pressure.** Nothing on this host's device plane is reachable
from the standard library: enumeration means either shelling out (`pactl`,
`aplay`, `arecord`, `pw-dump`, reading `/dev/v4l/by-id/`) or taking a binding
dependency. `harmonics-cli` faced the same fork and chose a middle path worth
copying — a dependency-free core plus an opt-in `[audio]` extra
(`sounddevice`/`numpy`) that is lazily imported, never at module scope, and
guarded by tests asserting `sounddevice` is absent from `sys.modules` on the
offline paths. Decide deliberately and record why (open question 1).

### Adding a verb or noun

Each command module under `media_cli/cli/_commands/` exposes `register(sub)`;
`_build_parser()` in `media_cli/cli/__init__.py` calls them in order. To add a
surface you touch four places:

1. a module in `_commands/` with `register(sub)`, whose handler returns
   `int | None` and raises `CliError` on failure;
2. the `register()` call in `_build_parser()` (there is a marked spot at
   `media_cli/cli/__init__.py:91`);
3. an entry in `media_cli/explain/catalog.py` — **every registered path needs
   one**, and `tests/test_cli.py::test_every_catalog_path_resolves` enforces it;
4. `_VERBS` in `_commands/overview.py` and the command map in `_commands/learn.py`,
   which are hand-maintained duplicates of the surface.

Noun groups nest via `p.add_subparsers(..., parser_class=type(p))` — passing
`parser_class` is required, or that noun's parse errors bypass the structured
error contract and exit 2 instead of 1 (see `_commands/cli.py:40` and
`test_cli_overview_unknown_flag_structured_error`, which guards it).

### The two stable contracts

**Errors** (`cli/_errors.py`, `cli/__init__.py`): every failure raises
`CliError(code, message, remediation)`. `_dispatch` catches it, and wraps any
other exception, so **no traceback ever reaches stderr**. Exit codes: `0`
success, `1` user error, `2` environment error, `3+` reserved. Text mode renders
`error:` + `hint:`; JSON mode emits `{code, message, remediation}`.

`_CliArgumentParser` routes argparse's own errors through the same path. Because
parse-time failures happen before `args.json` exists, `main()` pre-scans raw argv
for `--json` into a class-level `_json_hint` — that is why the flag is sniffed
twice.

**Output** (`cli/_output.py`): results to stdout, errors and diagnostics to
stderr, **never mixed**. Every command takes `--json`. The rubric asserts stderr
is empty on success.

The domain work adds failure classes that deserve typed treatment inside this
contract rather than a generic `code=2`: *present-but-forbidden* device nodes
(the ACL hazard below), *held-by-another-process* (`EBUSY`), and
*format-not-negotiable* (a sink that cannot accept the requested rate). See open
questions 3 and 4.

### Identity plumbing

`_commands/whoami.py` hand-parses `culture.yaml` (no YAML dependency — the
zero-deps rule) and walks up from `__file__`, deliberately *not* the CWD, so the
identity is the agent's own and not whatever `culture.yaml` a caller happens to
be standing in. In a wheel install no `culture.yaml` ships, and `doctor` degrades
to one info check and exit 0. `doctor` and `overview` both build on
`whoami.report()`.

## Domain constraints

These are design constraints, not trivia. Full derivation is in issue #1; the
essentials below were **independently re-verified on the operator's host on
2026-07-24**, and three of them extend or correct the brief.

### Identity is unstable at every layer

- **`/dev/videoN` and ALSA card numbers are plug-order, not identity, and both
  have been observed moving on this host with no hardware change.** Between
  2026-07-23 and 2026-07-24 the C270 went `video2` → `video0` and the Arducam
  `video0` → `video2`; the ALSA cards swapped the same way. Today's reading
  matches the brief's post-move column (`arecord -l`: card 1 = C270, card 2 =
  Reachy Mini Audio, card 3 = Arducam). **Video and ALSA indices move
  independently** — anything that says "camera 0", "card 1", or correlates a
  camera to a mic *by index* is already broken here.
- **`/dev/v4l/by-id/` is the stable video handle** and survived the move intact
  — it carries vendor, product, and serial:

  ```text
  usb-046d_C270_HD_WEBCAM_200901010001-video-index0                 -> ../../video0
  usb-Arducam_Technology_Co.__Ltd._Arducam_12MP_SN0001-video-index0 -> ../../video2
  ```

- **New finding — PipeWire object IDs are unstable too, and faster.** The brief
  recorded the HDMI sink as id `115`; the same sink is `130` today, *within the
  same day*, while the Reachy sink held at `51`. PipeWire ids are session-scoped
  serials handed out on node creation, not identity. So `pactl`'s numeric id is
  exactly as unusable as `/dev/videoN`, and the `alsa_output.usb-…` node **name**
  (which does carry vendor + serial) is the stable handle on that side. Building
  on the id because it "looked stable in one survey" is the trap.

**Stable identity is the first thing to build**, and it is a media-cli-shaped
problem precisely because it spans three subsystems that name things
differently and drift on different clocks.

### The subsystems disagree about what exists

- **Raw ALSA over-counts outputs exactly like `/dev/video*` over-counts
  cameras.** `aplay -l` lists five playback entries; `pactl list short sinks`
  collapses them to two usable sinks — the four NVIDIA HDMI entries are one
  physical sink. This is the output-side mirror of webcam-cli's "4 nodes, 2
  cameras" finding. Solving the collapse once, for both directions, is the
  argument for this repo existing.
- **`arecord -l` shows three capture cards; PipeWire exposes two sources.** The
  C270's microphone is present in ALSA and **absent** from `pactl list short
  sources` — re-confirmed today. Neither view is the truth. Decide which is
  authoritative for which question, and document it.
- **Device-native formats differ sharply.** The Reachy sink is `s16le 2ch
  16000Hz`; HDMI is `s32le 2ch 48000Hz`. "Play this WAV" silently means
  resample-or-fail. Be explicit about what you convert and what you refuse.

### The environment lies about itself

- **The stack is PipeWire 1.0.5, and `pactl` misreports it.** `pulseaudio` is
  not installed; `pactl` is PipeWire's compatibility shim and reports
  `Server Name: PulseAudio (on PipeWire 1.0.5)` with `Server Version: 15.0.0` —
  a PulseAudio version for a server that is not PulseAudio. **Do not
  version-sniff `pactl`.**
- **Assume no media backend is installed.** Present: `pactl`, `aplay`,
  `arecord`, `speaker-test`, `pw-cli`, `pw-dump`, `pw-play`, `paplay`, `wpctl`.
  **Absent: `ffmpeg`, `sox`, `v4l2-ctl`, `fswebcam`** — the same lesson
  webcam-cli hit. Detect capability and fail with a clear install hint; never
  assume. (`paplay`/`pw-play` being present is a small addition to the brief's
  list — there is more than `aplay` to work with.)

### Access: the video/audio asymmetry (corrects the brief)

The brief says the seat-ACL hazard "applies to audio too". It is more precise
than that, and the difference decides how `doctor` should diagnose each side:

| Node | Group | Operator in group? | What actually grants access |
|------|-------|--------------------|------------------------------|
| `/dev/video0` | `video` | **no** | seat ACL only (logind grants `user:<operator>:rw-`) |
| `/dev/snd/controlC0` | `audio` | **yes** | group membership **and** the seat ACL |

So on this host a headless agent, container, or systemd unit loses **video**
access but keeps **audio** access, because `audio` group membership is
independent of the seat. Both nodes carry the ACL; only one is load-bearing.
Group membership is per-host configuration, so neither the group fallback nor
its absence can be assumed — which is the actual requirement: **`list` must
distinguish *absent* from *present-but-forbidden*, name which mechanism failed,
and give the matching fix.** Silence becomes "the agent says there is no
camera" when there plainly is one.

### Your only real speaker belongs to another agent

The sole non-HDMI output is **`Reachy Mini Audio`** (Pollen Robotics) — a robot
driven by `reachy-mini-cli`, simultaneously a capture source, and **currently
this host's default sink and default source**. So a `play` with no explicit
device selection makes noise on someone else's robot. Two consequences: the
boundary is a conversation to have with that agent rather than resolve
unilaterally, and the "speakers" story cannot be tested here without touching a
robot. It also sharpens the dry-run rule below from a convention into a safety
property.

## Open design questions

Six questions in issue #1 are parked deliberately rather than guessed at. Settle
them with the operator — do not quietly assume an answer while implementing.
What was verified above sharpens three of them:

- **Q1 — How do you depend on `webcam-cli`?** PyPI dependency + Python import,
  or subprocess the `webcam` command? **Sequencing risk, and it is real:
  `webcam-cli` is still a bare scaffold with no capture implementation at all**
  (its own `CLAUDE.md` says so; its open issues are #1 the brief and #2 a docs
  re-init). You cannot depend on an API that does not exist. Options: agree the
  interface up front and build against it; start with the output/inventory half
  that depends on nobody; or subprocess a CLI contract that is easier to keep
  stable than a Python API. Cite-don't-import governs *skills*, not runtime deps
  — sibling CLIs do take real PyPI dependencies. Pick deliberately, record why.
- **Q2 — Where is the `harmonics-cli` seam?** *This is no longer hypothetical.*
  `harmonics-cli` **0.8.0 has already shipped live playback with device
  selection**: `harmonics play --play --device <name-substring|index>`, an
  `$HARMONICS_AUDIO_DEVICE` override, and `sounddevice`/PortAudio enumeration
  (`harmonics/audio/_playback.py::select_output_device`). Read that file before
  designing anything on the output side. Two things follow:
  1. Its default is a **name-substring heuristic** — prefer a device whose name
     contains `pipewire` or `pulse`, else fall back to the backend default. Its
     own docstring explains why: a host whose default sink is "a FIXED-rate
     device (e.g. a 16 kHz USB audio adapter)" rejects the synth's 44.1 kHz.
     **That 16 kHz adapter is this host's Reachy sink, which is also the default
     sink.** A sibling already hit media-cli's format-negotiation problem in
     production and worked around it with a hardcoded name guess.
  2. Its explicit `--device` accepts a **PortAudio index** — a fourth unstable
     numbering scheme on top of the three above.

  That is the strongest available argument for the lane: harmonics needed device
  identity and capability, did not have it, and hardcoded a heuristic. The clean
  split to propose is *harmonics decides **what** to play; media-cli resolves
  **which device** and what it can accept*. But webcam-cli's brief also assigns
  harmonics "non-TTS sound **out**", so this genuinely overlaps. **Settle it with
  that sibling on an issue.** The answer determines whether media-cli owns a
  `play` verb at all or only sink selection, capability reporting, and routing.
- **Q3 — What is the device model?** How do you correlate a camera to its mic
  through USB topology when indices are unstable and the sets are not 1:1
  (`Reachy Mini Audio` is capture-capable with no camera; the C270's mic is
  invisible to PipeWire)? What stable identifier do you hand out, and what
  happens to it across a replug?
- **Q4 — Arbitration.** V4L2 streaming is generally single-open (`EBUSY` when a
  browser or meeting app holds the camera); audio devices are contended too.
  Does media-cli merely *report* who holds what, or mediate? Fail fast rather
  than hang, either way.
- **Q5 — What are the "other" optional devices?** Left open by the operator.
  Make it an extension point, not a hardcoded list — capture cards, HDMI inputs,
  Bluetooth audio, virtual/loopback devices are all plausible. Do not build
  speculative support for devices nobody has.
- **Q6 — Does the lane hold?** If, once Q1 and Q2 are answered, media-cli is a
  thin passthrough, say so on issue #1. A correct *"this should be folded into
  `webcam-cli`"* is a better outcome than a repo built to justify itself.

Whatever is decided, `--json` must be complete enough that a follow-on tool never
has to re-derive what was enumerated, from where, or why a device was rejected.

**Suggested starting surface**, on top of the template verbs — and per the
brief's own "first moves", build the inventory first: it depends on nobody, is
provably non-trivial on real hardware, and is what every other verb sits on.
`media list`, `media describe <device>`, then the routing/playback verbs once Q2
is settled.

**Not yet done from the brief's first moves:** no issue has been opened against
`agentculture/webcam-cli` (Q1) or `agentculture/harmonics-cli` (Q2). Both block
real design and are cheap to ask now. Use the `communicate` skill.

## Conventions and workflow

- **Any write verb is dry-run by default; `--apply` commits.** Agents call CLIs
  in loops. For this repo that matters more than most: playback and device
  reconfiguration are **physically observable side effects**. Something that
  makes noise in a room — on a robot that belongs to another agent, per the
  default-sink finding above — or changes the system's default sink must not
  fire from a speculative call.
- **Every PR bumps the version** — even docs, config, or CI. Use the
  `version-bump` skill; the `version-check` CI job blocks merge otherwise.
- **PRs go through the `cicd` skill** (`devex pr` + SonarCloud gating). Online
  posts sign as `- media-cli (Claude)`; the `cicd` / `communicate` scripts
  resolve the nick from `culture.yaml` automatically, so don't sign bodies by
  hand.
- **Deploy**: pushing to `main` publishes to PyPI via Trusted Publishing; PRs do
  a TestPyPI dry-run. Both need the `pypi` / `testpypi` GitHub environments
  configured. `media-cli 0.6.1` is already published.
- **The vendored `.claude/skills/` are cited verbatim** — never reformat or edit
  their scripts. Re-sync from guildmaster per `docs/skill-sources.md`, which also
  records the tracked local divergences (`devex` rename, `ask-colleague` from
  colleague, four skills vendored straight from devague). Prerequisites: `devex`
  (>=0.21) and `agtag` (>=0.1) on PATH; `colleague` optional. All three are
  present in this workspace.
- **Reach for `ask-colleague` reflexively**, not as a last resort — its value is
  a *second, independent mind* (a different backend/model), not a stronger one.
  Run `review` on a non-trivial committed diff before opening a PR, and `explore`
  for a fresh read of an unfamiliar area. Both are read-only in a throwaway
  worktree, so the reflex is always safe; side-effecting `write --apply` /
  `write --pr` needs the user's go-ahead. Its output is a second opinion to
  verify and own, never authority.
- **Memory discipline — recall before, remember after.** This repo's eidetic
  memory is **in-repo and public**: `.claude/skills/remember/scripts/remember.sh`
  injects `--scope media-cli --visibility public`, which resolves to
  `<repo-root>/.eidetic/memory` — committed, and shared with the team and mesh
  peers (the `claude` and `colleague` backends share the `media-cli` scope). Pass
  `--visibility private` to route to `$HOME/.eidetic/memory` instead. Note the
  vendored `SKILL.md` frontmatter still describes the old private-by-default
  behaviour; the script is what runs. `/recall` before non-trivial work so you
  build on prior decisions instead of re-deriving them; `/remember` when a
  non-obvious decision, constraint, fix-and-why, or costly gotcha surfaces — as
  it happens, not at the end. Don't store what the repo already records. No
  `.eidetic/` exists yet — this repo has no stored memory.
- **Worktrees you create by hand live in `../.worktrees.media-cli/<name>/`** —
  one repo-named directory beside the checkout:

  ```bash
  git worktree add ../.worktrees.media-cli/<name> -b <branch>
  ```

  Never a shared `../worktrees/`: this workspace holds many sibling projects, and
  a generic folder accumulates orphaned trees from several repos with nothing
  indicating ownership, so a stale-tree sweep cannot tell a live lane from junk.
  Scope the branch prefix to the work (`inventory/t2`, not `agent/t2`) — plain
  `agent/*` collides with leftovers from earlier fan-outs and `git worktree add
  -b` fails on an existing branch. Remove with `git worktree remove <path>`;
  `git worktree prune` only clears metadata for directories already gone. Never
  `rm -rf` a worktree you did not create.

  The vendored `assign-to-workforce` skill's fan-out example uses both the shared
  path *and* `agent/<task-id>` branches. It is cited verbatim and must not be
  edited — override both when following it. `ask-colleague`'s read-only verbs
  create their own detached worktree under `${TMPDIR:-/tmp}` and reap it on an
  EXIT trap; those are outside this rule, not a violation of it.

## Layout

```text
media_cli/
  cli/__init__.py         parser assembly, _dispatch, exception→exit-code translation
  cli/_errors.py          CliError + exit-code policy (stable contract)
  cli/_output.py          stdout/stderr split (stable contract)
  cli/_commands/          one module per verb, each exposing register(sub)
  explain/catalog.py      markdown keyed by command-path tuple — every path needs an entry
tests/                    smoke + introspection tests
.claude/skills/           vendored guildmaster skill kit (cite-don't-import; never edit)
docs/skill-sources.md     skill provenance ledger + re-sync procedure
culture.yaml              mesh identity (suffix + backend)
AGENTS.colleague.md       resident mesh prompt (backend: colleague)
```

This file describes the repository **as it exists on disk today**. Keep claims
grounded in checked-in reality; if a section drifts ahead of reality, mark it
`(planned)` or move it under a `## Roadmap` heading. The domain sections above
are the one place that intentionally describes work not yet built — they are
marked as such, and they cite issue #1 and verifiable host state rather than
asserting a design.
