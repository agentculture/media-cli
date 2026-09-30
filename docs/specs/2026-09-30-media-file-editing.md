# media file editing

> media-cli lets an agent inspect and edit local video and audio files — probe info, peek at frames (optionally through lobes senses), search inside the media, cut at timestamps, blur regions, and more

## Audience

- Mesh agents (and the operator driving them) that capture video/audio through webcam-cli / microphone-cli and need to inspect, search and cut that media without hand-writing ffmpeg

## Before → After

- Before: Today an agent holding a captured .mkv/.wav can only shell out raw ffmpeg through shell-cli: no typed verbs, no dry-run, no JSON provenance, no way to ask 'where is the dog' — and media-cli itself ships no domain verb at all (4 commits, template verbs only)
- After: An agent runs 'media' verbs to probe a file, extract/peek at frames, semantically search frames and speech for timestamps, and submit a JSON edit list (cut, crop, black-box redact, blur, speed, fades, xfade transitions into a short) as a daemon job it polls by id — every step dry-run first, every result JSON with provenance

## Why it matters

- Capture is already solved by siblings, but what an agent can DO with the captured media is not owned by anyone (no agentculture repo depends on ffmpeg/av/moviepy; 'video editing' search = 0 issues); a typed, dry-run-first surface stops agents from improvising destructive ffmpeg one-liners

## Requirements

- Capability is detected, never assumed: every editing verb probes for ffmpeg/ffprobe and the specific filter/encoder it needs, and fails with a typed environment error (exit 2) plus an install hint when absent — ffmpeg was absent on 2026-07-24 (CLAUDE.md:261) and present by 2026-09-19, so presence has already flipped once on this host
  - honesty: With ffmpeg removed from PATH, every editing verb exits 2 with a hint naming the missing binary — none crash, none silently no-op
- Tests generate their own fixtures at test time with ffmpeg lavfi (testsrc + sine) and skip cleanly when ffmpeg is absent — the only media file in the workspace is /home/spark/git/test.wav (4.2s mono 44.1kHz), there is no video fixture
  - honesty: No test depends on a checked-in media binary or a file outside the repo; all fixtures are produced by lavfi inside the test run
- Peeking through lobes is an optional stdlib-HTTP client to the lobes gateway (OpenAI-compatible /v1/chat/completions, model 'senses' or 'cortex', `image_url` parts), env-configured and fail-soft — media-cli never depends on lobes-cli as a package (no importable client exists; embodiment/pyproject.toml bans it; shabbos-goy decider/gemma.py is the precedent)
  - honesty: Uninstalling lobes-cli and stopping the gateway leaves every non-sense verb fully working; sense calls return a typed error
- Peek works with no lobes at all: frame extraction (timestamp -> PNG, contact sheet) is a local ffmpeg operation media-cli owns; a sense only adds an optional description of frames media-cli already extracted
  - honesty: 'media frames' (peek) produces images with no network access at all
- Blur takes explicit regions as input (rectangle(s) in pixels plus an optional time range) — media-cli does not detect what to blur; lobes has no detector/bbox endpoint and senses/cortex are VLMs whose coordinates are unvalidated
  - honesty: Redaction applies only the regions it was given (explicitly or returned by the vision model and shown in the dry-run plan) — it never invents a region
- Every file-writing verb (cut, blur, ...) follows the repo's dry-run convention: without --apply it prints the plan (resolved ffmpeg argv, output path, expected duration/streams) as --json; with --apply it writes a NEW output file and never overwrites the source (CLAUDE.md:366-371, README.md:77-80)
  - honesty: Running any write verb without --apply leaves the filesystem byte-identical, and with --apply the source file's hash is unchanged afterwards
- The editing surface registers as a new noun through the four-place path (a `_commands` module with register(), `_build_parser`, an explain catalog entry per path, `_VERBS` + learn map) with `parser_class`=type(p) and a noun-level overview, so it passes the teken rubric gate (26 checks) and `test_every_catalog_path_resolves`
  - honesty: teken cli doctor . --strict passes and `test_every_catalog_path_resolves` covers every new path
- ffmpeg/ffprobe run through an absolute path resolved by shutil.which, with an argv list and no shell — bandit's config skips B404/B603 but not B607 (pyproject.toml \[tool.bandit\])
  - honesty: No subprocess call in `media_cli` uses shell=True or a bare executable name; bandit passes with the existing skip list
- Media failures get typed treatment inside the CliError contract (exit 1 bad input: unreadable/unsupported file, timestamp out of range, region outside frame; exit 2 environment: ffmpeg missing, filter or encoder unavailable), and the ffmpeg stderr tail goes into the remediation, never a raw traceback (cli/`_errors.py`:21-42, `_dispatch` `__init__.py`:98-119)
  - honesty: Feeding a corrupt file, an out-of-range timestamp, and an off-frame region each yields a distinct error code/message in --json with no traceback on stderr
- The docs that this work contradicts are corrected with it: CLAUDE.md:261 ('Absent: ffmpeg') and the Q1 claim that webcam-cli is 'a bare scaffold' are both stale as of 2026-09-30
  - honesty: After this work, CLAUDE.md no longer claims ffmpeg is absent or that webcam-cli is a bare scaffold
- Search is semantic as well as mechanical: an agent asks a model about image, audio or video content along the frames ('which frames have a dog') and gets back timestamps/frame ranges (user decision, resolves q2)
  - honesty: Every search hit carries its timestamp range plus the evidence it came from (frame image path or transcript chunk, caption/text, model name)
- Editing ops include black-box redaction (not only blur), crop, speed up / slow down, fade-in / fade-out, and assembling a long video into a short with smooth transitions (user decision, resolves q3; open to more relevant ops)
  - honesty: Each listed op (black box, crop, speed, fade in/out, short-with-transitions) has at least one CI test asserting its effect via ffprobe or pixel stats
- Long-running edits and searches run as jobs that the agent submits and then polls, ideally served by a media-cli daemon (user decision, resolves q4)
  - honesty: A job survives the submitting CLI process exiting — the agent can poll it from a fresh shell
- Edits are expressed as a declarative edit list (JSON timeline: segments, per-segment ops like speed/crop/redact, transitions between segments) that media-cli validates against ffprobe facts and compiles to one ffmpeg filtergraph. The dry-run prints the compiled plan; --apply submits it as a job. 'Make a short with smooth transitions' is then an edit list of N segments joined with xfade/acrossfade
  - honesty: An edit list that references a time beyond the file's duration or an unknown op is rejected at dry-run, before any ffmpeg process starts
- Semantic search runs in two phases: (1) index — sample frames (fps/scene-change) and chunk audio, send each through lobes (senses/cortex for frames, stt for speech), and cache the results keyed by file content hash; (2) query — answer 'which frames have a dog' against the index, returning timestamps/ranges plus the evidence (frame, caption, model, confidence). Re-querying never re-runs the models
  - honesty: A second query against an already-indexed, unchanged file makes zero calls to the gateway
- Speech search gets timestamps by chunking the audio into windows of <= 30s with overlap and offsetting each chunk's transcript — lobes /v1/audio/transcriptions returns text only, and whisper refuses clips > 30s (s6)
  - honesty: Transcript timestamps for a fixture with speech at a known offset > 30s land within +/- 1s of the true offset
- The daemon starts on demand: the first request that needs it spawns it in the background (no explicit 'media daemon start'), and the CLI reaches it over a unix socket (user, resolves q6)
  - honesty: With no daemon running, the first submit spawns it and succeeds; a stale socket file from a dead daemon is detected and replaced, not treated as live
- submit returns a job id immediately; the agent polls status/result by that id (user, resolves q7)
  - honesty: submit returns before the job's ffmpeg work completes (job id first, work after)
- Cuts are frame-accurate by default (re-encode at the cut boundaries); stream-copy is only used when both cut points land on keyframes, or when the agent explicitly asks for a fast inexact cut and the dry-run reports the actual snapped start/end
  - honesty: On a g=250 fixture, a default 2.0-5.0s cut measures 3.0s +/- 1 frame by ffprobe
- The ffmpeg filtergraph is compiled only from an allowlist of filters with typed numeric/enum parameters; no edit-list string is ever interpolated into a filtergraph. Source/IO filters (movie, amovie, zmq, azmq, sendcmd) are never emitted, and input/output paths are passed as argv entries, never inside filter strings
  - honesty: A fuzz test feeding edit-list values containing ',', ';', '\[', '=' and 'movie=' either rejects them at validation or produces a filtergraph with none of those filters, and no file outside the declared inputs is opened
- Redaction fails closed: a redacted output is always re-encoded (never stream-copied); streams media-cli cannot redact (subtitles, attachments/cover art, data streams) and container metadata are dropped, unless explicitly kept; the result reports which streams were kept or dropped
  - honesty: Redacting a fixture that carries a subtitle stream, a cover-art attachment and a title metadata tag yields an output with none of them, unless they were explicitly kept
- Model-derived redaction reports its coverage: regions from senses are sampled at N fps, so the dry-run and result state the sampling rate, how regions are held or interpolated between samples, and any frames where no detection ran — the agent never gets an implied 'fully redacted' it can't verify
  - honesty: A redaction test with a moving target shows no frame where the target is uncovered at the configured sampling rate, or the result explicitly lists the uncovered frame ranges
- Outputs are written to a temp file in the destination directory and atomically renamed on success; an existing output path is refused unless --overwrite is given (and the source path is refused outright); a killed or cancelled job leaves no partial file at the output path
  - honesty: Killing the daemon mid-encode leaves no file at the output path, and submitting to an existing output path without --overwrite is refused at dry-run
- Daemon spawn is race-free and private: concurrent first requests elect a single daemon via an exclusive lock; the socket lives under $`XDG_RUNTIME_DIR` (0700 on this host) with mode 0600, and the daemon rejects peers whose `SO_PEERCRED` uid differs from its own
  - honesty: Ten concurrent submits against no running daemon produce exactly one daemon process and ten job ids
- The daemon has a bounded lifecycle: it exits after an idle timeout with no queued/running jobs, it caps concurrent jobs (default 1 encode), and cancelling or shutting down kills the job's ffmpeg process group — no orphaned ffmpeg survives the daemon
  - honesty: After cancel, and again after daemon SIGTERM, pgrep finds no ffmpeg child of the daemon
- Indexing and encoding are budgeted: the dry-run of an index/search job estimates sense calls (sampled frames + audio chunks) and refuses above a configurable cap unless raised; sense calls are rate-limited and back off on 429/502/503; encodes default to CPU with a single concurrent job so media work does not starve the co-resident lobes vLLM
  - honesty: Dry-running an index of a 1-hour fixture reports the exact number of sense calls it would make, and exceeding the cap is refused before any call is sent
- The search index cache key includes the file content hash AND the sense identity (role, served model name, prompt/template version, sampling params), so a model or prompt change never serves stale captions; the cache lives under $`XDG_CACHE_HOME`/media-cli with a size cap, and is inspectable and purgeable per file via a verb
  - honesty: Changing the served model name (or the caption prompt version) on an already-indexed file triggers re-indexing, and a purge verb removes every cached artifact for that file hash
- All timestamps media-cli accepts and returns are seconds from the first presented frame (normalized for container `start_time`), with the frame index alongside; VFR input is handled by PTS, not frame-count x fps
  - honesty: On a fixture with a non-zero container `start_time` and variable frame rate, a search hit's timestamp fed straight back into a cut selects the same frame
- Every job keeps a retained record: the compiled ffmpeg argv, ffmpeg's stderr log, sense calls made, and timings; 'status' reports progress (from ffmpeg -progress) and 'result' includes the log path, so a failed or wrong edit can be diagnosed after the fact
  - honesty: A deliberately failing job (bad codec) yields a result whose remediation quotes the ffmpeg error and whose log path exists and is readable
- Introspection and read-only verbs (help, overview, explain, learn, doctor, probe, job status of a non-existent daemon) never spawn the daemon; only submit does — so the teken rubric gate and CI never leave a daemon running
  - honesty: After a full 'teken cli doctor . --strict' run and the full pytest suite, no media daemon process or socket remains
- Concrete output rule: the output container = the source container; streams an edit leaves untouched are stream-copied; streams an edit changes are re-encoded to a codec that container accepts, preferring the source codec when it has a usable encoder (e.g. mkv: vp8 -> vp8/libvpx, else H.264; opus stays opus), and falling back to mkv (which holds anything) only when the source container cannot carry the result. The dry-run states every per-stream copy/re-encode choice
  - honesty: An mkv (vp8 + opus) input cut and redacted comes out as mkv with an opus track that was stream-copied where untouched, and the dry-run lists each stream's copy/re-encode decision

## Honesty conditions

- Every capability named in the announcement (probe, peek, search, cut, blur/redact) is reachable through an installed 'media' verb, not only a Python function
- No media-cli code path opens /dev/video\* or an ALSA capture device for the editing features
- No editing verb produces content that is not derived from an input file (no synthesis, no generation)
- media-cli imports no ML framework and loads no model weights; every model call is an HTTP request to the lobes gateway
- culture.yaml and AGENTS.colleague.md are unchanged by the daemon work; the daemon registers nothing on the mesh
- The verbs are usable by an agent from 'media learn' + 'media explain' alone, without reading source
- The before-state claims are checkable: git log shows no domain verb, and gh search shows no editing lane elsewhere (s5, s8)
- Each clause of the after-state maps to at least one verb in the exported plan
- No sibling repo's lane statement is contradicted by the new lane text in issue #1 / CLAUDE.md / README
- The thresholds are enforced by assertions in the test suite, not just observed manually
- The live-gateway check is a documented, opt-in test (marker) so CI without a gateway still passes
- The rubric gate check count is recorded before and after, and the after count shows zero failures

## Success signals

- On a lavfi-generated 10s fixture: 'media probe' returns duration within 0.1s and every stream as JSON; a cut from 2.0s-5.0s yields an output whose ffprobe duration is 3.0s +/- 0.1s; a black-box redact leaves the region's mean pixel value < 5 while outside the box is unchanged — all in CI with ffmpeg present, and skipped cleanly (not failed) without it
- A semantic query ('frames with a red square') against a fixture with a red square inserted at 4.0s-6.0s returns a range overlapping 4.0-6.0s via senses on a live gateway, and returns a typed exit-2 'sense unavailable' error (not a traceback, not an empty result) when no gateway is reachable
- submit returns a job id in < 1s with no daemon running beforehand (on-demand spawn over the unix socket); status/result for that id return a single JSON document each; the teken rubric gate stays 100% (26+/26+) with the new nouns

## Scope / boundaries

- Capture stays with webcam-cli: it already ships list/stream/record writing Matroska via GStreamer (webcam-cli/CLAUDE.md:35-37) — media-cli edits files that exist, it never opens a camera or mic to make one
- Generation stays with innereye (text/image -> image & video, innereye#1) and harmonics-cli (non-TTS audio synthesis) — media-cli transforms existing media, it never synthesizes new content
- Interpretation is delegated, never owned: what a frame shows or a sound means goes to a sense/vision model (media-cli CLAUDE.md:10 'It moves bytes to and from devices. It does not interpret them'; face-recognition-cli owns face bboxes, image-only, no redaction verb) — media-cli accepts regions and timestamps as input or asks a sense for them, it runs no model itself
- The daemon is a job runner for media-cli's own edits and indexing, not a second mesh agent — the resident mesh agent (culture.yaml backend: colleague, AGENTS.colleague.md) stays as-is; the daemon is invoked through the same 'media' CLI (e.g. submit / status / result / cancel)

## Non-goals

- Not a general ffmpeg passthrough: shell-cli already owns gated shell exec and can run ffmpeg (shell-cli lane per webcam-cli/CLAUDE.md:18); media-cli exposes typed, validated editing verbs with --json provenance, not an `ffmpeg <args>` escape hatch
- Configurable / pluggable object detectors (a dedicated detector model or CLI) are out of v1 — deferred future work (user, q5)

## Assumptions

- Editing shells out to the host ffmpeg/ffprobe (6.1.1-3ubuntu5+esm13, /usr/bin) via subprocess — no Python media binding; av/cv2/numpy/moviepy are absent from both the media-cli venv and system python3, so the zero-runtime-dependency posture (pyproject dependencies = \[\]) survives
- Encoding defaults to CPU libx264/aac; NVENC (h264/hevc/`av1_nvenc` compiled in, GB10 driver 580.126.09 libs present) is an opt-in accelerator only after a runtime probe, since a real NVENC encode on GB10 is unverified
- Frames, audio chunks and their captions sent for sensing stay on this host: 'lobes endpoint senses' resolves to <http://localhost:8001>, but that spark gateway proxies suffixed lanes to mesh peers (thor/orin/spark2, lobes#284), so 'local endpoint' does not by itself guarantee local inference
- Hashing a multi-GB capture on every query is too slow; the cache is keyed by a cheap fingerprint (size + mtime + inode + sampled-chunk hash) with a full hash only at index time

## Scope exploration

- `s1` — `host: /usr/bin/ffmpeg + ffprobe`: ffmpeg/ffprobe 6.1.1-3ubuntu5+esm13 (arm64, --enable-gpl) are installed, dpkg info mtime 2026-09-19 — contradicts CLAUDE.md:261 'Absent: ffmpeg' (verified 2026-07-24). Every filter the idea needs is present: trim/atrim/concat, boxblur/gblur/avgblur/pixelize/delogo, crop/overlay/drawbox, select/scdet/thumbnail, silencedetect/ebur128, blackdetect/freezedetect. ffprobe -`print_format` json -`show_streams` works (verified on test.wav)
  - seeds: `c2`, `c3`
- `s2` — `host: python bindings + other tools`: av, cv2, numpy, moviepy, ffmpeg-python, whisper, `faster_whisper`, scenedetect, torch all absent from the media-cli venv; system python3 has only PIL. sox, v4l2-ctl, mediainfo, mkvmerge, exiftool still absent; gst-launch-1.0 present. The subprocess-to-ffmpeg path is the only zero-install route
  - seeds: `c2`
- `s3` — `host: GPU (NVIDIA GB10, driver 580.126.09, aarch64)`: h264/hevc/`av1_nvenc` and CUVID decoders compiled in and libnvidia-encode.so.1 present, but no NVENC encode was run on GB10; blur has no GPU path (no libnpp, no `gblur_cuda`) — only `scale_cuda`/`overlay_cuda`
  - seeds: `c4`
- `s4` — `workspace fixtures`: find -maxdepth 2 over /home/spark/git turns up only test.wav (`pcm_s16le` 44.1kHz mono 4.2s); no video file exists anywhere, so fixtures must be synthesized with lavfi
  - seeds: `c5`
- `s5` — `sibling repos: webcam-cli, innereye, harmonics-cli, face-recognition-cli, shell-cli (+ mysight/seer/face/webglass/storybook/reduce/data-refinery)`: No sibling owns or claims media FILE editing; gh search 'video editing' across agentculture = 0 hits, no AgentCulture pyproject depends on ffmpeg-python/av/moviepy/whisper. Adjacent only: webcam-cli records Matroska (CLAUDE.md:35-37, 'we produce an artifact ... and stop there' :22), innereye generates video (innereye#1), face-recognition-cli detects face bboxes on still images with no redaction verb, shell-cli can run ffmpeg as gated exec. Note media-cli CLAUDE.md Q1 calls webcam-cli 'a bare scaffold' — stale, it ships list/stream/record
  - seeds: `c6`, `c7`, `c8`, `c9`
- `s6` — `lobes-cli (0.81.4, github agentculture/lobes-cli) + gateway docs`: lobes is an inference-serving fleet, not a perception library: a 'lobe' is a gateway-routed model role (cortex=Qwen3.8-27B multimodal incl. video intake, senses=Gemma 4 12B vision, stt=parakeet-tdt-0.6b-v2 English/whisper Hebrew overlay, opt-in via --audio). The only CLI verb that runs a model, 'lobes run minor', is text-only (run.py:57-150). No bbox/detector, no OCR role, and the image/video wire contract is still unwritten (lobes#278). /v1/audio/transcriptions returns only {text} with no segments/timestamps (docs/openai-api.md:420-438), and whisper refuses clips > 30s (`listen_server_whisper.py`:56)
  - seeds: `c10`, `c11`, `c12`
- `s7` — `media-cli lane docs: CLAUDE.md, README.md, pyproject description, issue #1`: The lane is the device plane: CLAUDE.md:7-9 'owns the local media I/O device plane', :14 'It moves bytes to and from devices. It does not interpret them', README.md:9-14 'device selection, routing, playback and recording'. Files, editing and transcoding appear nowhere, in or out of scope. The only nod to conversion is issue #1's 'Play this WAV silently means resample-or-fail' (playback-side). issue #1 is the repo's only issue (open, no comments). Editing is a lane expansion that needs an operator decision
  - seeds: `q1` (question, resolved)
- `s8` — `media_cli/cli/ (_commands, explain/catalog.py, overview _VERBS, learn map)`: 4 commits, no domain verb has landed, and there is no subprocess/shutil use anywhere in `media_cli`/. The six template verbs live in `_commands`/{cli,doctor,explain,learn,overview,whoami}.py. A new noun touches the four places in CLAUDE.md:148-161 and follows the noun-group pattern with `parser_class`=type(p) (`_commands`/cli.py:30-43). learn purpose, parser description (`__init__.py`:74) and prog='media-cli' are still template prose/mismatched — that pre-existing debt will surface in every new catalog entry
  - seeds: `c14`
- `s9` — `pyproject.toml deps + [tool.bandit]`: dependencies = \[\], no optional-dependencies; dev group only. bandit skips B101/B404/B603 but not B607, so subprocess is pre-provisioned but bare-name executables would trip
  - seeds: `c2`, `c15`
- `s10` — `cli/_errors.py + cli/_output.py + rubric gate`: Exit codes 0/1/2 with 3+ reserved; CliError(code,message,remediation); `_dispatch` wraps all exceptions. Output is a single stdout document; `emit_diagnostic` goes to stderr, but the rubric asserts stderr is empty on success. There is no streaming, progress, timeout or partial-result precedent, so long ffmpeg runs have no home in the contract yet
  - seeds: `c16`, `q4` (question, resolved)
- `s11` — `tests/ + .github/workflows/tests.yml`: 22 tests (`test_cli.py` 12, `test_cli_introspection.py` 10), coverage `fail_under`=60. CI runs pytest+Sonar, lint (black/isort/flake8/bandit/markdownlint/teken cli doctor --strict) and version-check. tests.yml has no ffmpeg install step. Whether the ubuntu-latest runner image ships ffmpeg was NOT verified, so ffmpeg-backed tests need either an explicit install step or a skip-when-absent guard
  - seeds: `c5`
- `s12` — `ffmpeg filter set vs 'search' and 'and more'`: Mechanical search primitives exist locally with no model: scdet, silencedetect, blackdetect, freezedetect, ebur128, thumbnail (host survey). Semantic search needs a timestamped transcript or a VLM over sampled frames, and neither is served by lobes today (s6)
  - seeds: `q2` (question, resolved), `q3` (question, resolved)
- `s13` — `media-cli CLAUDE.md host facts (Q1, :261)`: Two facts in CLAUDE.md have drifted: ffmpeg/ffprobe are now installed (dpkg 2026-09-19), and webcam-cli ships list/stream/record (webcam-cli/CLAUDE.md:35). webcam-cli/CLAUDE.md:121 carries the same stale ffmpeg claim — that is the sibling's file, so flag it, don't edit it
  - seeds: `c17`
- `s14` — `host ffmpeg filters for the q3 ops (verified 2026-09-30)`: ffmpeg -filters lists every filter the user's q3 ops need: drawbox (black box, fill with t=fill), crop/cropdetect, setpts + atempo (speed; rubberband and minterpolate for higher quality), fade/afade, xfade (+`xfade_opencl`) and acrossfade for smooth transitions, trim/atrim/concat/segment for assembling a short, thumbnail/tile/select/fps for frame sampling, showwavespic/showspectrumpic for audio visuals. No new install is needed
  - seeds: `c20`
- `s15` — `lobes-cli issues (embed search, 2026-09-30)`: No existing lobes issue asks for a multimodal embedder; the only embedder is Qwen3-Embedding-0.6B (text-only, roles.py:50), and it is unreachable through the spark gateway anyway (lobes#284). Filed lobes-cli#289 with acceptance criteria (shared image+text space at /v1/embeddings, dog/no-dog cosine smoke check)
  - seeds: `c30`
- `s16` — `challenge pass / cheap-probe lens: ffmpeg -ss 2 -to 5 -c copy on a g=250 h264 fixture`: Stream-copy cut came out 3.16s (video) / 3.015s (audio) instead of 3.0s; the re-encoded cut was exactly 3.000s. c35's +/-0.1s threshold fails under naive stream copy, and webcam-cli's vp8 recordings have sparse keyframes too
  - seeds: `c39`
- `s17` — `challenge pass / security lens: ffmpeg filter set on this host`: The filters movie, amovie, sendcmd, zmq and azmq are all present. An edit list that reaches the filtergraph as text could read arbitrary files (movie=/etc/...) or open sockets (zmq). Edit lists come from agents, so they are untrusted input
  - seeds: `c40`
- `s18` — `challenge pass / failure-mode lens: redaction (c12, c26, s6)`: The spec has redaction regions coming from a VLM that has no detector (s6) and a boundary saying redaction never invents regions (h9). It never addressed MISSED content: frames between samples, subtitle/metadata/cover-art streams, or audio saying the redacted thing. A privacy feature that silently misses frames is worse than none
  - seeds: `c41`, `c42`
- `s19` — `challenge pass / reversibility lens: c13 'never overwrites the source'`: c13 protects the source, but not an unrelated existing file at the output path, and not a half-written output if the daemon dies mid-encode (jobs now outlive the CLI, c21)
  - seeds: `c43`
- `s20` — `challenge pass / concurrency + operations lens: on-demand daemon (c21, c27)`: The spec settles spawn-on-demand and the unix socket, but not two first-requests racing to spawn, who may connect to the socket, when the daemon exits, how many encodes run at once, or what happens to ffmpeg children on cancel/crash. `XDG_RUNTIME_DIR`=/run/user/1000 exists with mode 0700 here, but a headless/systemd context may not set it — see open question
  - seeds: `c44`, `c45`
- `s21` — `challenge pass / data-flow lens: lobes gateway routing`: 'lobes endpoint senses --json' returned {"endpoint": "<http://localhost:8001"}> on this host; lobes#284 documents the same gateway proxying embedder-thor/-orin lanes to peers. The spec never states whether media leaving the machine is acceptable
  - seeds: `c46`, `q9` (question, resolved)
- `s22` — `challenge pass / adjacent-systems lens: GB10 shared by lobes`: nvidia-smi shows VLLM::EngineCore holding 32452 MiB, and free -g shows 121 GiB total with 56 GiB available on the unified-memory GB10. The lobes fleet runs on the same machine media-cli would index against, so a 1-hour video at 1 fps is 3600 senses calls competing with every other mesh consumer. The spec had no budget, rate or cost ceiling
  - seeds: `c47`
- `s23` — `challenge pass / lifecycle lens: search index cache (c23, h19)`: h19 promises zero re-calls on an unchanged file, but the spec never defines what 'unchanged' covers (a different senses model, a new prompt), where the cache lives, how big it gets, or how to purge it. The cached captions describe sensitive content too (see q9)
  - seeds: `c48`, `c49`
- `s24` — `challenge pass / unstated-assumption lens: timestamps across search -> cut`: The spec chains search output into edit-list input (c19 -> c22) but never defines the time base. Matroska from webcam-cli can carry a non-zero `start_time` and VFR, so 'second 4.0' can mean different frames across probe, search and cut
  - seeds: `c50`
- `s25` — `challenge pass / observability lens: jobs (c21, c28, c16)`: Jobs run detached from the caller, so the ffmpeg stderr tail in the CliError (c16) never reaches an agent that polls later. The spec had no job log, no progress field and no audit trail of which argv actually ran
  - seeds: `c51`
- `s26` — `challenge pass / adjacent-systems lens: teken rubric gate + CI (s10, s11)`: The rubric runs the installed CLI across every noun (overview, --help, --json). If any of those spawned the on-demand daemon (c27), CI would leak background processes and the 'stderr empty on success' check could catch daemon chatter
  - seeds: `c52`
- `s27` — `challenge pass / migration lens: persisted state`: Clean pass. media-cli has no persisted state today (s8), so no migration is needed now. The spec introduces two new stores (job records, search cache) with no `schema_version`. Residual risk parked as v6
- `s28` — `challenge pass / overlooked-actors lens: Reachy default sink, webcam-cli outputs`: The Reachy default-sink hazard (CLAUDE.md) does not apply: no editing verb plays audio. webcam-cli writes Matroska (MJPG passthrough / vp8 + opus, webcam-cli/CLAUDE.md:35-37). The spec never states the default OUTPUT container/codec for edits, and ffmpeg can't mux MJPG+opus into mp4 cleanly, so the choice matters

## Decisions

- Lane expands: media-cli owns the device plane AND editing of the media captured from it (user decision, resolves q1)
- Redaction regions come from a vision model (lobes senses / Gemma 4) in v1; a configurable detector (pluggable model or sibling CLI) is deferred (user, resolves q5)
- v1 frame-level semantic search is caption-then-match through senses/cortex; an embedding index waits for a multimodal (image+text) embedder, tracked as an upstream issue (user, resolves q8)
- v1 audio search is speech-only (chunked stt transcripts, c24); non-speech sound-event search ('when does the dog bark') waits for a lobes sound-event model, tracked as an upstream issue (user, resolves v3 with option a)
- Captured media never leaves this machine for sensing: media-cli verifies via the gateway's GET /capabilities that the role is served locally (not a proxied mesh-peer lane) and refuses with a typed error otherwise (user, resolves q9)
- When `XDG_RUNTIME_DIR` is unset, the daemon socket falls back to a private 0700 directory under the user's cache dir (~/.cache/media-cli) rather than refusing (user, resolves question q1)
- Edit outputs prefer the source container and codecs; when a conversion is unavoidable the output stays in the converted form and is never converted back (user, resolves question q2)

## Hard questions

- Lane: CLAUDE.md:7-14, README.md:9-14 and issue #1 define media-cli as the DEVICE plane ('moves bytes to and from devices, does not interpret them'), and none of them mention files, editing or transcoding. Does file editing extend media-cli's lane (update the lane statement, issue #1 and the pyproject description together) or belong in a new sibling (e.g. a dedicated editing CLI) that media-cli only routes to? Q6 of issue #1 asks exactly this kind of question (resolved: USER: expand the lane — media-cli edits the media we capture (device plane + the files it produces))
- What does 'search inside the media' mean for v1: (a) mechanical signal search media-cli can own with ffmpeg alone (scdet scene cuts, silencedetect, blackdetect/freezedetect, ebur128 loudness), (b) speech search over a timestamped transcript, or (c) visual query ('find when the red car appears') via senses? (b) and (c) need capabilities lobes does not expose today — stt returns text only, no timestamps (docs/openai-api.md:420-438) (resolved: USER: search is semantic, not just mechanical — a model queries image, audio or video along the frames, e.g. 'which frames have a dog')
- What is in 'and more' for v1? Candidates the host already supports with no new install: probe/info, frames/contact-sheet, cut/trim (stream-copy vs re-encode), concat, blur/pixelize region, crop/scale, extract/replace/strip audio, transcode, loudness normalize. Which are v1, which later, which never? (resolved: USER: open to more relevant features — black box (redaction) instead of blur, cropping, slow down / speed up, fade-in / fade-out, turning a video into a short with smooth transitions, etc.)
- Semantic search (c19) finds WHEN a dog appears, but redacting it needs WHERE. lobes has no detector and no bbox endpoint (s6), and VLM-reported coordinates are unvalidated. Where do regions come from for 'black-box the dog': (a) ask senses/cortex for boxes and validate them, (b) add or borrow a real open-vocabulary detector (face-recognition-cli covers faces only), or (c) v1 redaction stays explicit-rectangle only? (resolved: USER: v1 gets regions from a vision model (lobes senses / Gemma 4) — enough for now. Not dog-specific; a configurable detection capability (model or CLI) is a deferred future extension)
- Long encodes vs the output contract: stdout is one JSON document at the end, `emit_diagnostic` writes progress to stderr (`_output.py`:92-95), but the rubric asserts stderr is empty on success (CLAUDE.md:181-183), and there is no timeout, cancellation or job model. Quiet by default with --progress opt-in, a job+poll model, or a hard timeout? (resolved: USER: job and poll; a daemon would be great)
- Daemon shape: how does the CLI reach it (unix socket vs localhost HTTP), where do job state and outputs live (XDG state dir vs repo), how does it start (systemd --user unit vs 'media daemon start'), and what happens when it is not running — does submit fail with a hint or run the job in the foreground? (resolved: USER: no explicit 'media daemon start' — a request starts the daemon on demand (or it runs as a background job), and the CLI talks to it over a unix socket)
- Job output contract: the rubric asserts stdout is a single JSON document and stderr is empty on success (s10). Is it 'submit' returns {`job_id`} immediately and `status/result <id>` return JSON snapshots, with no streaming? And are jobs cancellable and resumable after a daemon restart? (resolved: USER: yes — submit returns a job id; status/result are polled)
- Is frame-level semantic search a caption-then-match over senses outputs, or an embedding index (lobes has an 'embedder' role, but lobes#284 says it's unreachable through the gateway, and it's unknown whether it embeds images)? Captioning is slow per frame; embeddings are fast to query but need a multimodal embedder (resolved: USER: embeddings must be multimodal and we don't have one yet — v1 frame search is caption-then-match through senses; open an issue for a multimodal embedder)
- Captured media is often exactly the sensitive material being redacted. May media-cli send frames/audio to a sense served by a mesh PEER via the gateway, or must it require (and verify via GET /capabilities) that the role is served on this machine, refusing otherwise? (resolved: USER: yes — frames/audio stay on this machine (consistent with the user confirming assumption c46): media-cli requires the sense role to be served locally, verified via GET /capabilities, and refuses otherwise)

## Open parks

- [unknown_nonblocking] lobes image/video wire contract is unwritten (lobes-cli#278) and no client sends `image_url`/`video_url` today — the peek-via-senses request shape (frame size limits, multi-image, `video_url` to cortex) must be confirmed against a live gateway before it is built
- [unknown_nonblocking] NVENC on GB10 is unverified — a single probe encode decides whether hardware encoding is offered at all
- [unknown_nonblocking] Job-record and search-cache formats carry no schema version yet; a later format change could strand or misread old jobs/indexes — give both a `schema_version` field from day one
- [unknown_nonblocking] Residual after this challenge pass: not examined — ffmpeg behaviour on corrupt or truncated captures, very long (> 1h) inputs, HDR/10-bit sources, multi-audio-track files, and the lobes gateway's auth-key handling. These surfaced as lenses but no fixture or probe covered them
- [follow_up] Frame-embedding search waits on a multimodal image+text embedder in lobes — filed as agentculture/lobes-cli#289 (current embedder Qwen3-Embedding-0.6B is text-only, lobes/roles.py:50)
- [follow_up] Non-speech sound-event search waits on a lobes sound-event role — filed as agentculture/lobes-cli#290

## Resolved vagueness

- [unknown_blocking] Non-speech audio semantics ('when does a dog bark') has no served model — lobes serves stt (speech) only, senses audio is not served (lobes#101). Speech search is covered by c24; sound-event search needs a model nobody serves yet — resolved: USER: option (a) — v1 audio search is speech-only; file a lobes issue for a sound-event model
