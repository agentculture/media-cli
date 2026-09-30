# Build Plan — media file editing

slug: `media-file-editing` · status: `exported` · from frame: `media-file-editing`

> media-cli lets an agent inspect and edit local video and audio files — probe info, peek at frames (optionally through lobes senses), search inside the media, cut at timestamps, blur regions, and more

## Tasks

### t2 — Typed media errors inside the CliError contract

- instruction: Create the new package: own `media_cli`/media/`__init__.py`, `media_cli`/media/errors.py, `media_cli`/cli/`_errors.py` and tests/`test_media_errors.py`. Extend CliError minimally (an optional 'kind' string field emitted in `to_dict` when set); keep the exit-code constants and existing behaviour byte-identical for current callers.
- covers: c16
- acceptance:
  - `media_cli`/media/errors.py defines MediaInputError (exit 1) and MediaEnvError (exit 2), both subclasses of CliError, each carrying a stable machine code (e.g. 'input.unreadable', 'input.`timestamp_out_of_range`', 'input.`region_outside_frame`', 'env.`ffmpeg_missing`', 'env.`filter_unavailable`', 'env.`sense_unavailable`', 'env.`sense_not_local`')
  - --json error output includes the machine code alongside code/message/remediation; existing tests/`test_cli`\*.py still pass unchanged
  - an `ffmpeg_failure`(stderr) helper puts the last ~20 lines of ffmpeg stderr in remediation, never a traceback
- obligation: `o1` (criterion 1) [`media_cli`.media.errors -> every engine module and CLI verb] Machine error codes (input.\*, env.\*) are stable strings, and every media failure surfaces as exit 1 (input) or 2 (environment) with one of them. No bare Exception or traceback reaches stderr

### t3 — Test fixtures synthesized with lavfi

- instruction: Own tests/conftest.py and tests/`test_fixtures.py` only. Session-scope the fixtures so each file is generated once per run. Generate files with ffmpeg -f lavfi (testsrc, sine, drawbox with enable='between(t,4,6)'). Add a tiny SRT, write it in via -i and -map; attach cover art as an `attached_pic` stream.
- covers: c5, h3
- acceptance:
  - tests/conftest.py provides fixtures that generate, into `tmp_path`: a 10s testsrc+sine mp4 (h264 g=250 + aac); an mkv (vp8 + opus); a variant with non-zero `start_time` and VFR; one with a red square drawn at 4.0-6.0s; one carrying a subtitle stream, a cover-art attachment and a title tag
  - every fixture is skipped (pytest.skip, not failed) when ffmpeg is absent; no media file is checked into the repo
  - a '`live_gateway`' pytest marker is honoured: such tests are skipped unless `MEDIA_CLI_LIVE_GATEWAY`=1

### t4 — CI: ffmpeg on the runner + pytest markers

- instruction: Own .github/workflows/tests.yml and the \[tool.pytest.`ini_options`\] table in pyproject.toml only. Do NOT bump the version here; the PR bump happens once at the end.
- covers: h29
- acceptance:
  - .github/workflows/tests.yml installs ffmpeg (apt-get install -y ffmpeg) in the test job before pytest, so ffmpeg-backed tests run in CI rather than skipping
  - pyproject.toml \[tool.pytest.`ini_options`\] registers the '`live_gateway`' marker; 'pytest -m `live_gateway`' collects only the opt-in tests and CI never selects them

### t6 — lobes senses client: local-only, fail-soft, rate-limited

- instruction: Own `media_cli`/media/senses.py and tests/`test_media_senses.py`. Follow the shape of shabbos-goy's decider/gemma.py (stdlib urllib, JSON response, typed failure). The /capabilities payload shape is unconfirmed (lobes#278): parse it defensively and write the local-vs-proxied check against a recorded sample kept as a test constant. The gateway on this host is <http://localhost:8001>.
- depends on: t2
- covers: c10, h7, c8, h6
- acceptance:
  - `media_cli`/media/senses.py talks to the gateway with urllib only (URL from `MEDIA_CLI_LOBES_URL`, else 'lobes endpoint &lt;role&gt; --json' if lobes is on PATH, else <http://localhost:8001>; key from `MEDIA_CLI_LOBES_KEY`)
  - before the first call it checks GET /capabilities and refuses with MediaEnvError('env.`sense_not_local`') when the role is not served on this machine (proxied/suffixed peer lane), so captured media never leaves the host
  - `describe_images`(paths, prompt) and transcribe(wav) exist; connect errors and timeouts raise MediaEnvError('env.`sense_unavailable`'); 429/502/503 back off with bounded retries
  - a test asserts no ML framework (torch, transformers, cv2, numpy) is imported by `media_cli`; all sense tests use a local stub HTTP server, not a live gateway
- obligation: `o4` (criterion 2) [senses client -> lobes gateway (network egress)] No frame, audio chunk or caption is sent unless GET /capabilities shows the role served on this machine; otherwise env.`sense_not_local` is raised before any media byte leaves the process

### t18 — Job store: records, logs, schema version

- instruction: Own `media_cli`/media/daemon/`__init__.py`, `media_cli`/media/daemon/jobs.py and tests/`test_daemon_jobs.py`. Write records atomically (temp + rename).
- depends on: t2
- covers: c51, h41, c28
- acceptance:
  - `media_cli`/media/daemon/jobs.py persists one JSON record per job under $`XDG_STATE_HOME`/media-cli/jobs/&lt;id&gt;.json (`schema_version`, kind, state queued|running|done|failed|cancelled, argv, `sense_calls`, timings, progress, output, error) plus &lt;id&gt;.log for ffmpeg stderr
  - job ids are unique and sortable; records survive a process restart; a failed job's error carries the ffmpeg stderr tail and the log path

### t19 — Daemon server: unix socket, single instance, bounded lifecycle

- instruction: Own `media_cli`/media/daemon/server.py and tests/`test_daemon_server.py`. Use stdlib socketserver/selectors + threading. Parse ffmpeg -progress pipe:1 output into the job record's progress field.
- depends on: t18
- covers: c44, h36, c45, h37, c25, h21
- acceptance:
  - `media_cli`/media/daemon/server.py serves line-delimited JSON requests (submit, status, result, cancel, ping) on a 0600 unix socket and rejects peers whose `SO_PEERCRED` uid differs from its own
  - single instance: an exclusive flock on &lt;sockdir&gt;/daemon.lock; ten concurrent spawners yield exactly one daemon process
  - runs at most N (default 1) encodes at once; each ffmpeg starts in its own process group; cancel and SIGTERM kill the group (pgrep finds no orphan); it exits after an idle timeout (default 300s) with an empty queue
  - it registers nothing on the Culture mesh; culture.yaml and AGENTS.colleague.md are untouched
- obligation: `o13` (criterion 1) [daemon unix socket -> local peers] The socket is mode 0600 inside a 0700 directory, and a connection from a different uid is refused before any request is read
- obligation: `o14` (criterion 3) [daemon -> ffmpeg child processes] After cancel, SIGTERM or idle exit, no ffmpeg process spawned by the daemon remains alive

### t20 — Daemon client: spawn on demand, socket dir fallback

- instruction: Own `media_cli`/media/daemon/client.py, `media_cli`/media/daemon/`__main__.py` and tests/`test_daemon_client.py`. Tests must use a tmp `XDG_RUNTIME_DIR` and kill any daemon they start (fixture teardown).
- depends on: t19
- covers: c27, h22, c37, h23, h17, c21, c52
- acceptance:
  - `media_cli`/media/daemon/client.py submit() connects to the socket; if absent or stale (connect refused), it takes the lock and spawns 'python -m `media_cli`.media.daemon' detached (setsid, stdio to the log), then retries; submit returns a job id in < 1s
  - the socket dir is $`XDG_RUNTIME_DIR`/media-cli, falling back to a 0700 ~/.cache/media-cli/run when `XDG_RUNTIME_DIR` is unset (decision c54)
  - only submit may spawn; status/result/cancel against no daemon read the job store directly or return a typed 'no daemon' result; a job survives the submitting process exiting and can be polled from a fresh process
- obligation: `o15` (criterion 3) [CLI verbs -> daemon lifecycle] Only submit may spawn the daemon; help, overview, explain, learn, doctor, probe, frames, status, result and cancel never create a daemon process or socket

### t1 — ffmpeg tool layer: resolve, capability-probe, run

- instruction: Own only `media_cli`/media/`_tools.py` and tests/`test_media_tools.py` (the package `__init__.py` belongs to t2). Raise MediaEnvError/MediaInputError from `media_cli`/media/errors.py. Never import anything outside the stdlib.
- depends on: t2
- covers: c3, h2, c15, h12
- acceptance:
  - `media_cli`/media/`_tools.py` resolves ffmpeg/ffprobe via shutil.which to absolute paths and caches the result
  - `has_filter`(name)/`has_encoder`(name) parse 'ffmpeg -`hide_banner` -filters/-encoders' and answer correctly for boxblur, drawbox, xfade, libx264 on a host that has them
  - run(argv) uses a list argv, never shell=True; a missing binary raises the typed env error (exit 2) whose remediation names the binary (test: monkeypatch PATH to empty)
  - tests/`test_media_tools.py` passes; bandit passes with the unchanged skip list
- obligation: `o2` (criterion 3) [`media_cli`.media.`_tools`.run -> subprocess] Every ffmpeg/ffprobe invocation is an absolute-path argv list with shell=False. No other module calls subprocess directly for media binaries

### t5 — Probe + single time base

- instruction: Own `media_cli`/media/probe.py and tests/`test_media_probe.py`. Get VFR frame times from ffprobe -`show_entries` frame=`pts_time` (read lazily, only when a frame-index mapping is requested).
- depends on: t1, t2, t3
- covers: c50, h40
- acceptance:
  - `media_cli`/media/probe.py probe(path) runs ffprobe -`print_format` json and returns format + per-stream facts (codec, type, duration, fps, width/height, `sample_rate`, `start_time`)
  - all returned times are seconds from the first presented frame (container `start_time` subtracted); `to_frame_index`(t) and `to_seconds`(frame) round-trip on the VFR + non-zero `start_time` fixture
  - an unreadable or corrupt file raises MediaInputError('input.unreadable')
- obligation: `o3` (criterion 2) [probe time base -> search hits -> edit-list times] A timestamp returned by search, fed unchanged into an edit list, selects the same frame (seconds from the first presented frame, with `start_time` normalized)

### t7 — Output writer: atomic, collision-safe, container/codec rule

- instruction: Own `media_cli`/media/output.py and tests/`test_media_output.py`. Simulate a kill by raising mid-write and asserting dst is absent and the temp file is cleaned up.
- depends on: t5
- covers: c43, h35, c56
- acceptance:
  - `media_cli`/media/output.py `plan_output`(src, dst, `touched_streams`) returns per-stream copy/re-encode decisions: output container = source container; untouched streams are stream-copied; changed streams use the source codec when an encoder exists (vp8->libvpx, opus->libopus, h264->libx264), else H.264; fall back to mkv only when the container can't carry the result
  - the output goes to a temp file in the destination dir and is renamed on success; an existing dst is refused unless overwrite=True; dst == src is always refused; a failure or kill leaves no file at dst
- obligation: `o5` (criterion 2) [output writer -> filesystem] The source file is never opened for writing; dst is either the complete result or absent, never partial; an existing dst is untouched unless overwrite was requested

### t8 — Edit-list schema + validation

- instruction: Own `media_cli`/media/editlist.py and tests/`test_media_editlist.py`. Every op parameter is typed (float seconds, int pixels, enum names); reject strings anywhere a number is expected. That is the first half of the filter-injection defence (c40).
- depends on: t5
- covers: c22, h18
- acceptance:
  - `media_cli`/media/editlist.py parses a JSON edit list: {input, output, segments:\[{start,end,ops:\[...\]}\], transitions:\[{type:'xfade'|'acrossfade', duration}\]}, with ops drawn from a fixed enum
  - validation against probe facts rejects out-of-range times, end <= start, unknown ops, non-numeric params and regions outside the frame, raising MediaInputError with the offending JSON path, before any ffmpeg process starts
  - validate() is pure: a test asserts no subprocess is spawned
- obligation: `o6` (criterion 2) [editlist.validate -> compile -> ffmpeg] No ffmpeg process is spawned for an edit list that has not passed validation against probe facts

### t9 — Frame peek: extract frames + contact sheet locally

- instruction: Own `media_cli`/media/frames.py and tests/`test_media_frames.py`. Use -ss before -i per timestamp for speed, select='gt(scene,thr)' for scene sampling, and write via output.py's atomic helper.
- depends on: t5, t7
- covers: c11, h8
- acceptance:
  - `media_cli`/media/frames.py extract(path, times|every=N|scene=thr, outdir) writes PNGs named by timestamp and returns \[{t, `frame_index`, path}\]; `contact_sheet`() tiles them with the tile filter
  - works with networking disabled (test monkeypatches urllib/socket to raise) and never imports senses

### t10 — Filtergraph compiler: allowlist, cut, concat, argv

- instruction: Own `media_cli`/media/compile.py, `media_cli`/media/ops/`__init__.py`, stub files `media_cli`/media/ops/{visual,redact,compose}.py (each exposing def build(op, ctx) -> list\[FilterNode\]), and tests/`test_media_compile.py`. Later tasks replace only the stub bodies, so keep the stub signature stable. Define FilterNode(name, params: dict\[str, int|float|enum\]) and render params with strict numeric formatting.
- depends on: t1, t7, t8
- covers: c40, h33, c39, h32
- acceptance:
  - `media_cli`/media/compile.py compiles a validated edit list to one ffmpeg argv: inputs/outputs as argv entries, filtergraph built only from an allowlisted filter table with typed params; movie, amovie, zmq, azmq and sendcmd can never be emitted
  - a fuzz test feeding ',', ';', '\[', '=', "'" and 'movie=' into every string-accepting field either fails validation or yields a graph containing none of them
  - cuts are frame-accurate by default (trim/atrim + re-encode): a 2.0-5.0s cut of the g=250 fixture measures 3.0s +/- 1 frame; fast=True stream-copies only when both points are keyframes, otherwise it reports the snapped start/end
  - op modules are dispatched via a fixed registry in compile.py that maps op names to `media_cli`/media/ops/{visual,redact,compose}.py; each module ships as a stub raising MediaInputError('input.`op_not_implemented`') until its own task fills it
- obligation: `o7` (criterion 1) [compile -> ffmpeg filtergraph] The emitted filtergraph never contains movie, amovie, zmq, azmq or sendcmd, and no user-supplied string appears verbatim inside it
- obligation: `o8` (criterion 4) [ops registry build(op, ctx) -> list\[FilterNode\]] t11/t12/t13 op modules keep the stub's build signature; the compiler dispatches every enum op through this registry and nothing else

### t11 — Visual ops: crop, speed, fade

- instruction: Own `media_cli`/media/ops/visual.py and tests/`test_ops_visual.py` only. atempo accepts 0.5-2.0 per instance, so chain instances for factors outside that range.
- depends on: t10
- covers: c20, h16
- acceptance:
  - `media_cli`/media/ops/visual.py implements crop{x,y,w,h}, speed{factor 0.25-4.0} (setpts + chained atempo keeps A/V in sync) and fade{in|out, duration} (fade + afade)
  - tests/`test_ops_visual.py` proves each op via ffprobe or pixel stats: crop yields the requested w x h; speed 2.0 halves duration +/- 1 frame; fade-in's first frame has mean luma < 5

### t12 — Redaction ops: black box, blur, fail-closed streams

- instruction: Own `media_cli`/media/ops/redact.py and tests/`test_ops_redact.py` only. Emit -`map_metadata` -1 and explicit -map for the kept A/V streams.
- depends on: t10
- covers: c12, h9, c41, h34, c20, h16
- acceptance:
  - `media_cli`/media/ops/redact.py implements box{regions:\[{x,y,w,h,start?,end?}\], fill:black} via drawbox t=fill and blur{regions, strength} via crop+boxblur+overlay, applying ONLY the given regions
  - a redacted output is always re-encoded; subtitle, attachment and data streams and container metadata are dropped unless keep=\[...\] names them; the result lists kept and dropped streams
  - tests: the black box region's mean pixel value < 5 and outside the box is unchanged (PSNR > 40); the subtitle + cover-art + title fixture comes out with none of them
- obligation: `o9` (criterion 2) [redacted output file] A redacted output carries no subtitle, attachment or data stream and no container metadata unless explicitly kept, and is never stream-copied for the video it redacts

### t13 — Compose op: short with smooth transitions

- instruction: Own `media_cli`/media/ops/compose.py and tests/`test_ops_compose.py` only. xfade requires matching resolution, fps and pixel format, so normalize every segment (scale, fps, format) before chaining.
- depends on: t10
- covers: c20, h16
- acceptance:
  - `media_cli`/media/ops/compose.py joins N segments with xfade (video) + acrossfade (audio), computing offsets from segment durations; supported transitions are an enum (fade, dissolve, wipeleft, slideleft)
  - a test assembles 3 segments of 2s with 0.5s transitions into an output whose duration is 5.0s +/- 1 frame, with audio and video durations equal within 1 frame

### t14 — Speech search: chunked stt with offsets

- instruction: Own `media_cli`/media/speech.py and tests/`test_media_speech.py`. The gateway returns text only, so treat each chunk's time span as the result's time range. There is no speech fixture generator, so the live test needs a recorded speech clip passed via `MEDIA_CLI_SPEECH_FIXTURE`.
- depends on: t1, t6
- covers: c24, h20
- acceptance:
  - `media_cli`/media/speech.py extracts audio to 16 kHz mono WAV chunks of <= 30s with 2s overlap, transcribes each via senses.transcribe, and returns \[{start, end, text}\] with times offset by the chunk start; overlap duplicates are merged
  - unit tests use a stub transcriber and assert chunk boundaries and offsets for a 75s input; the +/- 1s real-speech check lives in a `live_gateway`-marked test

### t15 — Search index: sampled-frame captions + cache + budget

- instruction: Own `media_cli`/media/index.py and tests/`test_media_index.py`. Use a stub HTTP server for senses. Tie-in with c49: the fingerprint avoids a full-file hash per query (hash timing was never measured, lapse l2), so keep the full hash out of the hot path.
- depends on: t6, t9
- covers: c23, h19, c48, h39, c47, h38
- acceptance:
  - `media_cli`/media/index.py `build_index`(path, fps|scene, `dry_run`) samples frames via frames.py, captions them in batches via senses, and stores {t, `frame_path`, caption, model, `prompt_version`} under $`XDG_CACHE_HOME`/media-cli/index/&lt;key&gt;/, with a `schema_version`
  - the cache key = file fingerprint (size + mtime + inode + head/tail sample hash) + sense identity (role, served model, prompt version, sampling params); a model or prompt change re-indexes, and an unchanged file makes zero gateway calls (stub server counts requests)
  - `dry_run` reports the exact planned sense-call count; above the cap (default 600, configurable) it raises MediaInputError('input.`budget_exceeded`') before any call; purge(path) deletes every cached artifact for that file; a total size cap evicts least-recently-used
- obligation: `o11` (criterion 2) [search index cache -> lobes gateway] An unchanged file with an unchanged sense identity triggers zero gateway requests; a changed model or prompt version always re-indexes
- obligation: `o12` (criterion 3) [index budget -> lobes gateway] When the planned sense-call count exceeds the cap, input.`budget_exceeded` is raised before the first request

### t16 — Semantic query with evidence

- instruction: Own `media_cli`/media/search.py and tests/`test_media_search.py`. Without a live gateway, test against the stub server returning canned yes/no answers.
- depends on: t14, t15
- covers: c19, h15, c36
- acceptance:
  - `media_cli`/media/search.py query(path, text, modality=frames|speech|all) returns hits \[{start, end, `frame_index`?, evidence:{kind, `frame_path`|transcript, caption|text, model}, score}\], merging adjacent matching samples into ranges
  - frame matching sends each cached caption with the query to senses for a yes/no + confidence (batched), not substring matching; speech matching runs over the chunked transcript
  - a `live_gateway` test finds the red-square fixture's 4.0-6.0s range (overlap required); with no gateway reachable, the call raises MediaEnvError('env.`sense_unavailable`')

### t17 — Model-derived redaction regions with coverage report

- instruction: Own `media_cli`/media/regions.py and tests/`test_media_regions.py`. The model is Gemma 4 via the senses role (decision c26). Ask for JSON boxes in pixel coordinates via `response_format` and discard malformed output rather than guessing.
- depends on: t6, t9, t12
- covers: c42, h31
- acceptance:
  - `media_cli`/media/regions.py `find_regions`(path, description, fps) asks senses for pixel boxes on sampled frames, validates them (inside the frame, positive size, clamped), and holds or interpolates between samples
  - the result carries coverage: {`sample_fps`, method:'hold'|'linear', `frames_without_detection`:\[ranges\], `rejected_boxes`:\[...\]}; the dry-run shows every region before any render
  - a test with a moving-square fixture and a stub sense returning the true boxes shows no uncovered frame at the configured fps, or lists the uncovered ranges explicitly
- obligation: `o10` (criterion 2) [regions -> redaction dry-run/result] Every model-derived redaction reports `sample_fps`, the interpolation method and `frames_without_detection`; it never implies full coverage it did not measure

### t21 — CLI: probe + frames verbs

- instruction: Own only those two command modules and their test file. Expose register(sub) exactly as the existing `_commands` modules do. Do NOT edit cli/`__init__.py`, the catalog, overview or learn; t23 wires them in. Tests build a throwaway parser that calls register() directly.
- depends on: t5, t9
- acceptance:
  - `media_cli`/cli/`_commands`/probe.py registers 'probe &lt;file&gt; \[--json\]'; `media_cli`/cli/`_commands`/frames.py registers 'frames &lt;file&gt; --at T... | --every N | --scene THR --out DIR \[--sheet\] \[--describe\] \[--json\]' (--describe calls senses, is optional and fail-soft)
  - both are read-only toward the source, never touch the daemon, print one JSON document on stdout and nothing on stderr on success; tests/`test_cli_media_readonly.py` covers the success and error paths

### t22 — CLI: edit, search, job nouns (dry-run first)

- instruction: Own `media_cli`/cli/`_commands`/{edit,search,job}.py and tests/`test_cli_media_write.py`. Do NOT edit cli/`__init__.py`, the catalog, overview or learn. Follow `_commands`/cli.py:30-43 for the noun-group pattern.
- depends on: t10, t11, t12, t13, t16, t17, t20
- covers: c13, h10
- acceptance:
  - `media_cli`/cli/`_commands`/edit.py: 'edit plan <editlist.json>' validates + compiles and prints the full plan (argv, per-stream decisions, output path, redaction coverage) and writes nothing; 'edit apply <editlist.json> --apply' submits a job and prints {`job_id`}; without --apply it behaves like plan
  - `media_cli`/cli/`_commands`/search.py: 'search index &lt;file&gt; \[--fps|--scene\] \[--apply\]' (dry-run shows the sense-call estimate) and 'search query &lt;file&gt; &lt;text&gt; \[--modality\]'; `media_cli`/cli/`_commands`/job.py: 'job status|result|cancel|list'
  - every noun uses `add_subparsers`(`parser_class`=type(p)) and exposes an 'overview' subverb; a test proves a dry run leaves the filesystem byte-identical and --apply never changes the source file's hash
- obligation: `o16` (criterion 3) [write verbs dry-run/--apply boundary] Without --apply, no file is created, modified or deleted and no job is submitted; with --apply the source file's hash is unchanged

### t23 — Wire the surface + rewrite self-docs (prog=media)

- instruction: Own `media_cli`/cli/`__init__.py`, `media_cli`/explain/catalog.py, `media_cli`/cli/`_commands`/overview.py, `media_cli`/cli/`_commands`/learn.py and tests/`test_cli.py` / tests/`test_cli_introspection.py` (update expectations for the new prog). Write the catalog entries as agent-facing docs: flags, JSON shapes, dry-run/--apply, exit codes.
- depends on: t21, t22
- covers: c1, h1, c14, h11, c31, h24, c33, h26, h30
- acceptance:
  - `media_cli`/cli/`__init__.py` registers probe, frames, edit, search and job; prog is 'media' (the installed command), and the parser description, learn purpose and explain catalog describe the device-plane + media-editing agent, not the template
  - `media_cli`/explain/catalog.py has an entry for every registered path (both ('media',) and ('media-cli',) keys kept); overview `_VERBS` and the learn command map list every new verb; `test_every_catalog_path_resolves` passes
  - 'uv run teken cli doctor . --strict' passes with zero failures, with the check count recorded in the PR body before and after; every clause of the spec's after-state maps to a verb shown by 'media learn'
- obligation: `o17` (criterion 3) [installed 'media' CLI -> teken rubric gate] 'teken cli doctor . --strict' reports zero failures, and every help/error hint names the installed command 'media'

### t24 — Docs + lane statement

- instruction: Own CLAUDE.md, README.md and the \[project\] description line of pyproject.toml. Draft the issue #1 comment into the PR body for approval; do not post it unilaterally.
- depends on: t23
- covers: c17, h14, c34, h27, c32, h25
- acceptance:
  - CLAUDE.md: the lane section says media-cli owns the device plane AND editing of captured media; the stale 'Absent: ffmpeg' (was :261) and 'webcam-cli is a bare scaffold' claims are corrected; the architecture section describes `media_cli`/media/ and the daemon
  - README.md and the pyproject description state the expanded lane; the compose-not-absorb table still names webcam-cli (capture), innereye/harmonics-cli (generation), shell-cli (generic exec) as owners, contradicting no sibling
  - a comment on media-cli issue #1 records the lane expansion and links the spec (posted via the communicate/cicd scripts only after the user approves the text); markdownlint is clean

### t25 — End-to-end acceptance suite

- instruction: Own tests/`test_e2e_media.py` only. Use subprocess against 'uv run media', a tmp `XDG_RUNTIME_DIR`/`XDG_STATE_HOME`/`XDG_CACHE_HOME`, and a teardown that kills any daemon started.
- depends on: t23
- covers: c35, h28, c37, h13, h42, h43, c6, h4, c7, h5
- acceptance:
  - tests/`test_e2e_media.py` drives the installed 'media' command on lavfi fixtures: probe duration within 0.1s; a 2-5s cut = 3.0s +/- 0.1; black-box mean < 5; an mkv (vp8+opus) cut + redact stays mkv with opus stream-copied where untouched
  - submit with no daemon returns a job id in < 1s, status/result are single JSON documents, and a corrupt file, an out-of-range timestamp and an off-frame region each give a distinct machine code in --json with empty stderr tracebacks
  - after the suite and a 'teken cli doctor . --strict' run, no media daemon process or socket remains; a static test asserts `media_cli` never opens /dev/video\* or ALSA capture and no verb synthesizes content without an input file

## Risks

- [unknown_nonblocking] NVENC on GB10 is unverified (frame v2); plan encodes default to CPU libx264. A probe encode gates any later hardware path, and no task depends on it
- [unknown_nonblocking] The lobes /capabilities payload shape and the image wire contract are unwritten (lobes#278, frame v1). t6's local-vs-proxied check is written against a recorded sample and may need adjusting against the live gateway (task t6)
- [unknown_nonblocking] The h20 +/- 1s speech-offset check needs a recorded speech clip (no lavfi generator), so it only runs as an opt-in `live_gateway` test with `MEDIA_CLI_SPEECH_FIXTURE` (task t14)
- [follow_up] Multimodal embeddings (lobes-cli#289) and sound-event search (lobes-cli#290) are follow-ups outside this plan; v1 is caption-then-match and speech-only by decision
- [unknown_nonblocking] Residual from /challenge (frame v7): corrupt/truncated captures, > 1h inputs, HDR/10-bit, multi-audio-track files and the gateway auth key have no fixture. t25 covers corrupt input only (task t25)
- [follow_up] The media-cli version bump + CHANGELOG happen once, in the final PR (version-check CI), not per task
