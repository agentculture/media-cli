# Delivery Summary — media file editing

plan: `media-file-editing` · run: `complete` · date: `2026-09-30`
baseline: `devague summary skeleton`

## Intent

> media-cli lets an agent inspect and edit local video and audio files — probe info, peek at frames (optionally through lobes senses), search inside the media, cut at timestamps, blur regions, and more

After-state (confirmed claim `c33`): an agent runs `media` verbs to probe a file,
extract and peek at frames, semantically search frames and speech for
timestamps, and submit a JSON edit list (cut, crop, black-box redact, blur,
speed, fades, xfade transitions into a short) as a daemon job it polls by id.
Every step is dry-run first, and every result is JSON with provenance. The run
executed the 25-task, 10-wave plan
[`docs/plans/2026-09-30-media-file-editing.md`](../plans/2026-09-30-media-file-editing.md)
derived from the spec
[`docs/specs/2026-09-30-media-file-editing.md`](../specs/2026-09-30-media-file-editing.md),
via `/assign-to-workforce` on branch `spec/media-file-editing`.

## Planned Work

Quoted verbatim from the `devague summary` skeleton:

- `t1` — ffmpeg tool layer: resolve, capability-probe, run
- `t2` — Typed media errors inside the CliError contract
- `t3` — Test fixtures synthesized with lavfi
- `t4` — CI: ffmpeg on the runner + pytest markers
- `t5` — Probe + single time base
- `t6` — lobes senses client: local-only, fail-soft, rate-limited
- `t7` — Output writer: atomic, collision-safe, container/codec rule
- `t8` — Edit-list schema + validation
- `t9` — Frame peek: extract frames + contact sheet locally
- `t10` — Filtergraph compiler: allowlist, cut, concat, argv
- `t11` — Visual ops: crop, speed, fade
- `t12` — Redaction ops: black box, blur, fail-closed streams
- `t13` — Compose op: short with smooth transitions
- `t14` — Speech search: chunked stt with offsets
- `t15` — Search index: sampled-frame captions + cache + budget
- `t16` — Semantic query with evidence
- `t17` — Model-derived redaction regions with coverage report
- `t18` — Job store: records, logs, schema version
- `t19` — Daemon server: unix socket, single instance, bounded lifecycle
- `t20` — Daemon client: spawn on demand, socket dir fallback
- `t21` — CLI: probe + frames verbs
- `t22` — CLI: edit, search, job nouns (dry-run first)
- `t23` — Wire the surface + rewrite self-docs (prog=media)
- `t24` — Docs + lane statement
- `t25` — End-to-end acceptance suite

## Actual Delivery

All 25 tasks merged through the TDD gate: the suite ran before and after every
merge, `git merge --no-ff`, one isolated worktree per task under
`../.worktrees.media-cli/`. Five approved fix tasks (`d5`, `d7`, `d11`, `d12`,
`d13`) merged the same way.

| Plan task | Status | What actually landed |
|-----------|--------|----------------------|
| `t1` | delivered | `media_cli/media/_tools.py`, the only subprocess seam (absolute path, argv, no shell, capability probes) — merge `6018cd2` |
| `t2` | delivered | `media_cli/media/errors.py` (`MediaInputError`/`MediaEnvError` with a machine `kind`), `CliError.kind` — merge `3c600b6` |
| `t3` | delivered | `tests/conftest.py` lavfi fixtures (mp4 g=250, mkv vp8+opus, VFR+offset, red square, extras), `live_gateway` skip logic — merge `9bd54cc` |
| `t4` | delivered | CI installs ffmpeg; `live_gateway` marker registered — merge `671c940` |
| `t5` | delivered | `probe.py` plus the single normalized time base (with `d1`); `editable` range added later by `d12` — merges `316f819`, `8dc0e07` |
| `t6` | delivered | `senses.py`: stdlib client, fail-closed locality from a real recorded `/capabilities` payload, backoff — merge `4950300` |
| `t7` | delivered | `output.py`: atomic temp+link/replace, container/codec plan; output-extension check added by `d7` — merges `22ad75e`, `0242903` |
| `t8` | delivered | `editlist.py`: closed, typed schema with JSON-path errors, pure `validate` — merge `addb5d2` |
| `t9` | delivered | `frames.py`: extract and contact sheet, offline, with its own atomic writes (`d2`) — merge `493b035` |
| `t10` | delivered | `compile.py` and `ops/__init__.py`: allowlisted typed filtergraph, frame-accurate cut, op registry, passthrough/remux modes (`d3`, `d4`) — merge `e1c1003` |
| `t11` | delivered | `ops/visual.py`: crop, speed (chained atempo), fade — merge `bd1ec16` |
| `t12` | delivered | `ops/redact.py`: box and blur, fail-closed stream drop via the compiler (`d8`, `d9`) — merge `e6d2f2c` |
| `t13` | delivered | `ops/compose.py`: xfade + acrossfade joins — merge `5fab41d` |
| `t14` | delivered | `speech.py`: chunks of 30 s or less with 2 s overlap, offset transcripts. The real-speech ±1 s check was never run (see Remaining Work) — merge `77c4cee` |
| `t15` | delivered | `index.py`: fingerprint + identity cache, budget, LRU, purge (`d6`); caption retry and per-frame fallback added by `d13` — merges `2ea25ef`, `49d5021` |
| `t16` | delivered | `search.py`: yes/no semantic matching with evidence, a transcript cache that is purgeable and size-capped — merge `dcf9324` |
| `t17` | delivered | `regions.py`: Gemma boxes with a hold/linear coverage report, fail-closed — merge `e1362d1` |
| `t18` | delivered | `daemon/jobs.py`: atomic JSON job records plus logs, `schema_version` — merge `97b5355` |
| `t19` | delivered | `daemon/server.py`: 0600 socket, `SO_PEERCRED`, flock single instance, process-group kill, idle exit; no-clobber publish added by `d5`; socket-path check added by `d13` — merges `a20c3ad`, `f6d6f2f`, `49d5021` |
| `t20` | delivered | `daemon/client.py` and `__main__.py`: spawn on demand, `spawn.lock`, store fallback for read ops — merge `5dcbf76` |
| `t21` | delivered | `media probe`, `media frames` — merge `21106ca` |
| `t22` | delivered | `media edit`/`search`/`job` nouns, plus the `index` job handler (`d10`) and the extra subverbs `edit regions`, `search purge` and `search cache` — merge `bb98f24` |
| `t23` | delivered | five nouns wired, `prog=media`, 29-path agent-facing catalog, `learn`/`overview` rewritten; residual hints fixed by `d11` — merges `a8d5ddb`, `d87d7a0` |
| `t24` | delivered | `CLAUDE.md`, `README.md` and the pyproject description state the two lanes. The issue #1 comment is drafted but **not posted** (it needs approval) — merge `083d996` |
| `t25` | delivered | `tests/test_e2e_media.py` (18 tests) drives the installed `media` command; `d3`/`d12` assertions — merge `3b52c7d` |

## Mid-work Decisions

- `d1` — t5 criterion 2: the time-base origin is the first presented video frame's `start_time`, not the container `start_time`; later tasks convert with probe.`to_source_seconds`()/`from_source_seconds`() and never add the container `start_time` themselves — On the VFR fixture the container `start_time` (1.477s, from the audio stream) precedes the first video frame (1.500s), so subtracting it literally would put the first presented frame at 0.023s, contradicting spec c50/h40 ('seconds from the first presented frame'). User approved 2026-09-30.
- `d2` — t9 writes its PNGs with its own atomic temp-file + os.link/os.replace instead of output.py's helper, and seeks with `to_source_seconds`(t) - container `start_time` to match ffmpeg 6.1's input-relative -ss clock — output.`plan_output` plans media containers from a source and would rewrite a .png dst to .mkv; the same no-partial-file / no-overwrite guarantee (obligation o5) is kept and tested. ffmpeg's input -ss is relative to the container `start_time` even with -copyts (verified on this host); probe's normalized base is unchanged. User approved 2026-10-01.
- `d3` — h43/t25: an mkv (vp8+opus) cut re-encodes its audio to opus in the same container; audio is stream-copied only when the edit spans the whole file with frame-only video ops. t25 asserts same container+codec, copy only for untouched audio — Trimming audio frame-accurately requires re-encoding it, so 'opus stream-copied where untouched' cannot apply to a cut (found while building t10's compiler). User approved 2026-10-01.
- `d4` — Filter-mode edits drop subtitle, attachment and data streams unless listed in 'keep' (not only when redacting); every drop is listed in the dry-run plan — A cut would leave copied subtitle/attachment/data streams mistimed; c56 implied they ride along. Re-timing subtitles is out of scope. User approved 2026-10-01.
- `d5` — Reopen t19's daemon/server.py for a small fix: job completion must not clobber an output path that appeared after planning; use output.commit's no-clobber semantics (link then unlink; replace only with overwrite) plus a regression test — Found by t10: server.py finishes ffmpeg jobs with os.replace(`tmp_output`, output), which overwrites a dst created after the dry-run even without --overwrite, breaking c43 and obligation o5. User approved the fix 2026-10-01.
- `d6` — `build_index` defaults verify=True: one GET /capabilities per build re-indexes when the served model changed; query-time reads (`load_index`/search) make zero gateway calls. o11's 'zero requests' is read as the query path — o11/c48 promise both zero requests on an unchanged file and re-indexing on a model change; detecting a model change needs one capabilities call, so a verify-off default would serve stale captions after a model swap. User chose verify-on-build 2026-10-01.
- `d7` — An output path whose extension does not match the source container is refused at dry-run (input.`output_container_mismatch`, naming the expected extension); fix lands in output.py (t7's file) via a small fix task — Found by t13: following c56 an mkv source written to out.mp4 silently produced Matroska content under a .mp4 name. User chose refuse-at-dry-run 2026-10-01.
- `d8` — Blur rectangles are widened outward to even (4:2:0) edges - at most 1px per edge, clamped to the frame - and the actual covered area is reported; black boxes stay pixel-exact — ffmpeg overlay rounds x/y down to even on 4:2:0, so an odd-edged blur would shift and leave an unblurred column (a leak). Fail closed: never cover less than asked. User approved 2026-10-01.
- `d9` — Redaction time windows start one nominal frame interval (+1ms) early and end 1ms late so every frame on screen during \[start,end\] is covered; VFR gaps longer than the nominal interval remain a documented caveat — Exact between(t,start,end) leaves the frame presented at 'start' (shown slightly earlier) uncovered; matches probe's frame-presented-at-t rule. build() is pure so it cannot probe actual VFR frame times. User approved 2026-10-01.
- `d10` — t22 additionally owns new `media_cli`/media/daemon/handlers.py (registers an 'index' job kind running index.`build_index`) plus a one-line import of it in daemon/`__main__.py` (t20's file) — c21 requires long searches to run as daemon jobs, but the daemon process only knows the built-in 'ffmpeg' kind, so 'search index --apply' would be rejected; handlers must be registered inside the daemon process. User approved 2026-10-01.
- `d11` — Fix task edits five user-facing strings outside t23's ownership (explain/`__init__.py` unknown-path hint; 'see media-cli ... overview' help in `_commands`/cli.py, edit.py, search.py, job.py) to name the installed command 'media', and turns t23's strict-xfail guard into a normal test — Obligation o17 requires every help/error hint to name the installed 'media' command; t23 could not edit files it did not own and pinned the gap with a strict xfail. User approved 2026-10-01.
- `d12` — Fix: probe's JSON exposes the editable range (normalized start 0 and end = probe.end); an edit-list end at or beyond that end (within one frame) is clamped and treated as whole-file, so frame-only edits on the full span stay passthrough with stream-copied audio — Found by t25: on an mkv probe reported 10.008s (audio tail) but edit plan accepted ends only up to 10.001s (video end), probe never exposed that bound, and end=10.0 fell to filter mode and re-encoded audio, so agents could not express 'whole file'. User approved fixing now 2026-10-01.
- `d13` — Validation fixes: (1) index captioning retries a batch once on a caption-count mismatch, then captions that batch frame by frame; it still fails closed (env.`sense_unavailable`, nothing cached) if any single frame stays malformed. (2) The daemon socket path is checked against the `AF_UNIX` length limit up front, with a typed env error whose remediation says to set a shorter `XDG_RUNTIME_DIR`; no daemon error may carry an empty remediation — Validation at 8dc0e07: the c36 live test failed because Gemma-4 returned 3 captions for a 4-frame batch of near-identical frames, aborting the whole index; and an over-long socket path produced env.`daemon_unavailable` with an empty remediation. User approved fixing both before the PR 2026-10-01.

Decisions that no `dN` record covers, captured directly:

- The main agent added three subverbs to `t22`'s brief, outside its criteria:
  `edit regions`, `search purge` and `search cache`. Confirmed claims
  `c26`/`h1` (model-derived redaction reachable from a verb) and `c48` (the
  cache is inspectable and purgeable "via a verb") needed a CLI surface that no
  task's criteria named. They live in `t22`'s owned files.
- `t16` was sent back once. Its transcript cache had escaped `purge` and the
  size cap, short of `c48`. It now exposes `purge`, `purge_transcripts` and
  `cache_report`, plus an LRU cap.
- `t20` meets "takes the lock" with a separate short-lived `spawn.lock`. The
  daemon holds `daemon.lock` for its whole life, so it remains the
  single-instance guarantee. The main agent specified this in the brief.
- `t19` was sent back once. Its fallback socket directory became
  `~/.cache/media-cli/run` (decision `c54`) instead of `/tmp`, and it gained a
  SIGKILL-escalation test.
- The main agent narrowed `t23`'s strict guard test, under `d11`. It now bans
  stale `media-cli <verb>` command strings, not the literal `media-cli`,
  because the parser description rightly names the PyPI distribution.
- The main agent added explain-catalog lines for `d12` (`probe` `editable`) and
  `d13` (caption retry, `env.socket_path_too_long`) on those fix branches.
- Live validation used `grant run --inject
  MEDIA_CLI_LOBES_KEY=LOBES_GATEWAY_API_KEY`, at the user's direction. The key
  value was never printed or stored.

## Drift From Plan

| Plan item | Reason for divergence | Classification |
|-----------|------------------------|-----------------|
| `t5` (`d1`) | On the VFR fixture the container `start_time` (1.477s, from the audio stream) precedes the first video frame (1.500s), so subtracting it literally would put the first presented frame at 0.023s, contradicting spec c50/h40 ('seconds from the first presented frame'). User approved 2026-09-30. | `acceptable` |
| `t9` (`d2`) | output.`plan_output` plans media containers from a source and would rewrite a .png dst to .mkv; the same no-partial-file / no-overwrite guarantee (obligation o5) is kept and tested. ffmpeg's input -ss is relative to the container `start_time` even with -copyts (verified on this host); probe's normalized base is unchanged. User approved 2026-10-01. | `acceptable` |
| `t25` (`d3`) | Trimming audio frame-accurately requires re-encoding it, so 'opus stream-copied where untouched' cannot apply to a cut (found while building t10's compiler). User approved 2026-10-01. | `acceptable` |
| `t10` (`d4`) | A cut would leave copied subtitle/attachment/data streams mistimed; c56 implied they ride along. Re-timing subtitles is out of scope. User approved 2026-10-01. | `acceptable` |
| `t19` (`d5`) | Found by t10: server.py finishes ffmpeg jobs with os.replace(`tmp_output`, output), which overwrites a dst created after the dry-run even without --overwrite, breaking c43 and obligation o5. User approved the fix 2026-10-01. | `needs-follow-up` |
| `t15` (`d6`) | o11/c48 promise both zero requests on an unchanged file and re-indexing on a model change; detecting a model change needs one capabilities call, so a verify-off default would serve stale captions after a model swap. User chose verify-on-build 2026-10-01. | `acceptable` |
| `t7` (`d7`) | Found by t13: following c56 an mkv source written to out.mp4 silently produced Matroska content under a .mp4 name. User chose refuse-at-dry-run 2026-10-01. | `needs-follow-up` |
| `t12` (`d8`) | ffmpeg overlay rounds x/y down to even on 4:2:0, so an odd-edged blur would shift and leave an unblurred column (a leak). Fail closed: never cover less than asked. User approved 2026-10-01. | `acceptable` |
| `t12` (`d9`) | Exact between(t,start,end) leaves the frame presented at 'start' (shown slightly earlier) uncovered; matches probe's frame-presented-at-t rule. build() is pure so it cannot probe actual VFR frame times. User approved 2026-10-01. | `acceptable` |
| `t22` (`d10`) | c21 requires long searches to run as daemon jobs, but the daemon process only knows the built-in 'ffmpeg' kind, so 'search index --apply' would be rejected; handlers must be registered inside the daemon process. User approved 2026-10-01. | `acceptable` |
| `t23` (`d11`) | Obligation o17 requires every help/error hint to name the installed 'media' command; t23 could not edit files it did not own and pinned the gap with a strict xfail. User approved 2026-10-01. | `acceptable` |
| `t5` (`d12`) | Found by t25: on an mkv probe reported 10.008s (audio tail) but edit plan accepted ends only up to 10.001s (video end), probe never exposed that bound, and end=10.0 fell to filter mode and re-encoded audio, so agents could not express 'whole file'. User approved fixing now 2026-10-01. | `needs-follow-up` |
| `t15` (`d13`) | Validation at 8dc0e07: the c36 live test failed because Gemma-4 returned 3 captions for a 4-frame batch of near-identical frames, aborting the whole index; and an over-long socket path produced env.`daemon_unavailable` with an empty remediation. User approved fixing both before the PR 2026-10-01. | `needs-follow-up` |

Drift no record covers:

| Plan item | Reason for divergence | Classification |
|-----------|-----------------------|----------------|
| `t22` | It gained the `edit regions`, `search purge` and `search cache` subverbs beyond its criteria, to make confirmed `c26`/`h1`/`c48` reachable from the CLI | acceptable |
| `t11` | The criteria are met as written (2.0x speed gives 2.02 s; the first fade-in frame has mean luma 0.00). Its extra tests use wider bounds: 0.25x slow motion comes out 15.88 s against 16.0 (about 3 frames short), and the last fade-out frame has luma < 12 | acceptable |
| `t14` | Delivered, but the plan's `h20` real-speech ±1 s check never ran: no speech fixture exists, and none of the user's screencasts has audio (plan risk `r3`) | needs-follow-up |
| `t17` | Box geometry and coverage are proven against a stub that returns the true boxes. Gemma-4 box *accuracy* was never measured: one live run returned plausible near-full-frame boxes for "a terminal window" | needs-follow-up |

## Evidence

All checks below are read-only re-runs on branch `spec/media-file-editing`.

- **Full suite at `49d5021` (2026-10-01 10:13):**
  - `uv run pytest -n auto -q --cov=media_cli`: exit 0, **824 passed, 2 skipped** (the skips are the opt-in `live_gateway` tests). Coverage **91 %** (`fail_under = 60`).
  - The earlier run at `8dc0e07` gave 812 passed.
- **Lint at `49d5021`:**
  - `black --check`, `isort --check-only`, `flake8`, and `bandit -c pyproject.toml -r media_cli` all clean.
  - `markdownlint-cli2` on every tracked `.md` (excluding `.claude/skills`): 0 errors.
- **Rubric gate:** `uv run teken cli doctor . --strict` reported **26/26 passed** before the run (base `931f254`) and after (`49d5021`).
- **End to end:** `tests/test_e2e_media.py`, 18 tests, drives the installed `media` command. The t25 agent mutation-checked each assertion: it made each one fail deliberately, then reverted.
- **Live gateway:** `MEDIA_CLI_LIVE_GATEWAY=1`, local senses `nvidia/Gemma-4-26B-A4B-NVFP4`, key injected via `grant`.
  - `tests/test_media_search.py::test_live_finds_red_square_range` **failed at `8dc0e07`**: Gemma returned 3 captions for a 4-frame batch.
  - After `d13` it **passed 3/3 at `49d5021`**.
  - `tests/test_media_speech.py::test_live_real_speech_within_one_second` **skipped**: there is no speech fixture.
- **Live, on the user's screencasts** (`/home/spark/Videos/Screencasts`, read-only, outputs in the scratchpad):
  - `probe` on a VFR 2090x1384 file gave `editable.end` 72.94 s.
  - `frames --at 10 30 60` returned the frame actually presented at 9.916 s (frame 132).
  - A daemon job for a 10–20 s cut plus a 400x200 black box:
    - The job finished in about 1 s and submit took 0.254 s.
    - Box luma was 0.0002 against 20 outside it.
    - The source sha was unchanged.
    - The output was 9.871 s long, because the last VFR frame before 20 s falls early.
  - Search on the 37-minute file: a dry run at fps 0.5 plans 139 calls; at fps 4 it is refused (`input.budget_exceeded`, 1108 > 600) before any request.
  - An over-long `XDG_RUNTIME_DIR` gives `env.socket_path_too_long` with a hint, no spawn and no files.
  - While the senses backend refused connections, the gateway returned 503 and media-cli failed with `env.sense_unavailable`, no traceback.
- **Ledger** (all `--origin llm`, so all **proposed**, pending adjudication by the gate owner):
  - Evidence `e1`–`e24`; `e20` (c36 live fail) is superseded by `e23`.
  - Deltas `b1`–`b15`; `b12`/`b13` are superseded by `b14`/`b15`.
  - Read them back with `devague evidence --list` and `devague delta --list`.
- **Commits:** `931f254..ed1c50a` on `spec/media-file-editing`, with 31 merges: 25 tasks, 5 fixes, and the d7 sync.
- **Issues filed during the run:** agentculture/lobes-cli#289 (multimodal embedder) and agentculture/lobes-cli#290 (sound-event role).

## Delivery Claims

Evidence pointers are test node ids that ran green at `49d5021` unless stated
otherwise. Confidence is capped by approved lapses `l1`/`l2`; proposed lapses
`l3`–`l13` are pending and are not cited as evidence.

| Claim | Confidence | Evidence |
|-------|------------|----------|
| Every announced capability (probe, peek, search, cut, blur/redact) is an installed `media` verb (`c1`/`h1`) | high | `tests/test_cli_introspection.py`, `tests/test_cli.py::test_every_catalog_path_resolves`, `media learn --json` command list; rubric 26/26 |
| ffmpeg is used only through an absolute-path argv seam with no shell (`c15`, `o2`) | high | `tests/test_media_tools.py::test_run_uses_absolute_list_argv_no_shell`, `::test_no_shell_true_and_stdlib_only` |
| A missing ffmpeg gives typed exit 2 with an install hint (`c3`/`h2`) | high | `tests/test_media_tools.py` missing-binary tests (PATH emptied) |
| Failures are typed: exit 1/2, a stable `kind`, a non-empty hint, no traceback (`c16`, `o1`) | high | `tests/test_e2e_media.py::test_distinct_machine_codes_for_corrupt_range_and_offframe`, `tests/test_daemon_client.py::test_typed_errors_from_client_and_daemon_carry_remediation`; live `env.socket_path_too_long` |
| One normalized time base round-trips search to edit (`c50`, `o3`) | high | `tests/test_media_probe.py::test_round_trip_vfr`, `tests/test_media_frames.py::test_vfr_offset_pixels_match_the_indexed_frame`; live VFR frames |
| Cuts are frame-accurate by default (`c39`/`h32`) | high | `tests/test_media_compile.py` cut tests (75/75 frames, VFR 49/49), `tests/test_e2e_media.py::test_cut_2_to_5_is_3_seconds` |
| The filtergraph is allowlisted and typed; `movie`/`amovie`/`zmq`/`azmq`/`sendcmd` are never emitted (`c40`, `o7`) | high | `tests/test_media_compile.py::test_fuzz_injection_into_every_string_field`, `::test_ops_returning_forbidden_nodes_are_refused` |
| Edit lists are validated before any ffmpeg process starts (`c22`/`h18`, `o6`) | high | `tests/test_media_editlist.py::test_validate_is_pure_no_subprocess` |
| Black box redacts and leaves the rest untouched (`c20`, `c35`) | high | `tests/test_e2e_media.py::test_black_box_mean_below_5`, `tests/test_ops_redact.py`; live screencast YAVG 0.0002 |
| Redaction fails closed on extra streams and metadata (`c41`/`h34`, `o9`) | high | `tests/test_ops_redact.py::test_redacted_extras_come_out_without_subtitle_cover_or_title` |
| Crop, speed, fade and xfade shorts work (`c20`/`h16`) | high | `tests/test_ops_visual.py`, `tests/test_ops_compose.py` (3x2 s with 0.5 s transitions gives 5.0 s) |
| Outputs are atomic and never clobber, at plan time and in the daemon (`c43`/`h35`, `o5`) | high | `tests/test_media_output.py::test_kill_mid_write_leaves_no_dst_and_no_tmp`, `::test_dst_appearing_before_commit_is_not_clobbered`, `tests/test_daemon_server.py` (d5 tests) |
| The output keeps the source container; a mismatched extension is refused (`c56`, `d7`) | high | `tests/test_media_output.py::test_mkv_source_mismatched_extension_refused`, `tests/test_e2e_media.py::test_mkv_whole_file_box_copies_audio_byte_identical` |
| Dry run changes nothing; `--apply` never touches the source (`c13`/`h10`, `o16`) | high | `tests/test_cli_media_write.py::test_edit_plan_prints_plan_and_writes_nothing`, `::test_edit_apply_runs_a_job_and_keeps_source`; live sha unchanged |
| Jobs are submitted and polled; submit with no daemon returns in under 1 s (`c21`/`c27`/`c28`/`c37`) | high | `tests/test_e2e_media.py::test_submit_with_no_daemon_returns_job_id_fast` (0.285 s / 0.201 s); live 0.254 s |
| Only submit spawns the daemon (`c52`, `o15`) | high | `tests/test_cli_introspection.py::test_read_only_verbs_never_spawn_the_daemon`, `tests/test_e2e_media.py::test_no_daemon_or_socket_after_read_only_verbs` |
| The daemon is private: socket 0600 in a 0700 dir, other uid refused (`c44`, `o13`) | medium | `tests/test_daemon_server.py::test_socket_0600_in_0700_dir_and_ping`, `::test_peer_with_other_uid_refused_before_read`. The uid check was exercised by injecting `expected_uid`; no real second user (`l4` pending) |
| No orphan ffmpeg after cancel, SIGTERM or idle exit (`c45`, `o14`) | medium | `tests/test_daemon_server.py::test_cancel_running_ffmpeg_kills_group`, `::test_sigterm_kills_running_groups_and_cleans_up`. SIGKILL escalation is tested only at helper level, and cancelling a *running index* job is untested (`l9` pending) |
| Sensing is local-only and fails closed (`c8`/`c10`, `o4`, decision `c53`) | high | `tests/test_media_senses.py::test_not_local_fails_closed_and_sends_no_media` (recorded real `/capabilities` payload) |
| Semantic frame search finds content via live senses (`c19`, `c36`) | medium | `tests/test_media_search.py::test_live_finds_red_square_range` passed 3/3 at `49d5021` after `d13`, and failed at `8dc0e07`. Model output is nondeterministic |
| The cache is keyed by fingerprint and identity, budgeted, purgeable and inspectable (`c23`/`c47`/`c48`, `o11`/`o12`) | medium | `tests/test_media_index.py::test_load_index_zero_requests_of_any_path`, `::test_budget_exceeded_before_any_request`; live budget refusal. Capped: fingerprint speed versus full hash was never measured (approved `l2`) |
| Speech search timestamps land within ±1 s past 30 s (`c24`/`h20`) | unverified | Only stub tests ran; the live test is skipped with no speech fixture. Not claimed done |
| Gemma-derived redaction boxes are accurate (`c26`, `c42`) | unverified | Coverage reporting is proven (`tests/test_media_regions.py::test_garbage_sample_is_reported_uncovered`), but box accuracy against ground truth was never measured. Not claimed done |
| NVENC acceleration (`c4`) | unverified | Never probed; encoding stays on the CPU (plan risk `r1`). Not claimed |

Lapse ledger evidence:

| Lapse | Code | What |
|-------|------|------|
| `l1` | `assumption-for-measurement` | s16 concludes stream-copy cuts are inaccurate from container durations alone; the per-frame probe of the copy cut's first PTS/keyframe returned no rows and was not re-run, so the exact start snap was not measured |
| `l2` | `assumption-for-measurement` | c49 claims full-file hashing is too slow for multi-GB captures without timing a hash of a large file on this host |

pending approval (not yet evidence): `l3`, `l4`, `l5`, `l6`, `l7`, `l8`, `l9`, `l10`, `l11`, `l12`, `l13`

## Remaining Work / Follow-up

No plan task is partial, dropped or blocked. Remaining items:

- **Speech ±1 s check (`h20`, plan risk `r3`).** It never ran. Provide a
  recorded speech clip and run with
  `MEDIA_CLI_SPEECH_FIXTURE=<clip> MEDIA_CLI_SPEECH_EXPECT="<phrase>@<sec>"
  MEDIA_CLI_LIVE_GATEWAY=1`. The user's screencasts have no audio. Note that
  local `stt` is the Hebrew Whisper overlay (`ivrit-ai/whisper-large-v3-turbo`).
- **Gemma box accuracy (`c26`/`c42`).** Measure the boxes against ground truth
  on real footage before relying on model-derived redaction for privacy.
  Coverage reporting is honest, but accuracy is unmeasured.
- **Gaps in the tests:**
  - cancelling a *running* index job (`l9`);
  - spawn-failure and spawn-timeout paths (`l6`);
  - a real cross-uid socket test (`l4`);
  - SIGKILL escalation end to end through the daemon.
- **Private coupling.** `index.py` uses `frames._requested_times`, and the
  `index` handler uses `index._location`. Promote both to public helpers.
- **Upstream (lobes):**
  - `/capabilities` advertised `senses` as `loaded: true, ready: true` while
    the `multimodal` backend refused connections (gateway 503). Worth an issue
    on agentculture/lobes-cli; media-cli failed correctly, with
    `env.sense_unavailable`.
  - Follow-ups lobes-cli#289 (multimodal embedder) and #290 (sound-event role).
- **Ledger ambiguity (`l12`).** The frame-level obligations `o1`–`o3`
  (`c35`/`c36`/`c37`) share bare ids with plan obligations `o1`–`o3`. This is
  a devague limitation; the evidence contract text names the claim.
- **Adjudication (the gate owner):**
  - evidence `e1`–`e24`, deltas `b1`–`b15`, lapses `l3`–`l13`;
  - frame obligations `o1`–`o3`;
  - the `d5`/`d7`/`d12`/`d13` `needs-follow-up` deviations, all of which
    landed as fix tasks in this run.
- **Before or at the PR:**
  - the version bump and CHANGELOG (plan risk `r6`);
  - posting the drafted issue #1 lane-expansion comment once the user
    approves its text;
  - `webcam-cli/CLAUDE.md:121` still says ffmpeg is absent. That is the
    sibling's file, so it is flagged here, not edited.
- **Not in this plan's scope:**
  - the device-plane half (`list`/`describe`, routing, playback, arbitration),
    still unbuilt, with open questions Q1–Q6 on issue #1;
  - the culture backend reconciliation, which is the operator's call.
- **VFR edge.** Cut duration on sparse-frame VFR screencasts ends at the last
  frame presented before `end`; a 10 s cut measured 9.871 s. This is correct
  under the time base, but agents should read `editable` and the output
  duration from the job result.
