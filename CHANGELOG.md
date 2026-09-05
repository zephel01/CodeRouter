# Changelog

All notable changes to CodeRouter are recorded here.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/),
versioning follows [SemVer](https://semver.org/).
All entries are in English (pre-v2.5.5 entries were originally written in
Japanese and translated in place on 2026-07-06; quoted Japanese examples
are kept verbatim where the Japanese text itself is the subject).

---

## [v2.16.0] — 2026-09-04 (JA<->EN translation layer, bilingual i18n, profile resolution + installer hardening)

### Added

- **JA<->EN translation layer (CPU Argos, masking, buffered streaming).**
  `TranslationConfig` (cpu-only, fail-open, disabled by default) in `config/schemas.py`.
  JA->EN request translation in `ingress/anthropic_routes.py` via `asyncio.to_thread`
  with 5s timeout (system/tool_use/tool_result are excluded; only user text with
  Japanese detection is translated). EN->JA response translation after repair in
  `routing/fallback.py` with buffered-streaming guard and 64KB limit.
  `TranslatorManager` (thread-safe, resident, direct `translate-ja_en` /
  `translate-en_ja` models only) plus `jp_translation/masking.py` that preserves
  code/paths/identifiers. Optional dependency `argostranslate>=1.11,<2`
  (Python 3.12-3.13) and `scripts/setup_argos_models.py` with hash gate.
  Includes B-M1/M-1..M-4/F-1/F-2/J-1/J-4 fixes, BOM removal and slow-startup warnings.
- **Bilingual messages (ja/en) for CLI/doctor/config.**
  New `coderouter/messages.py` catalog with `CODEROUTER_T_LANG` priority (`ja`/`en`,
  `LANG` fallback) and `tr()`. Migrated `cli.py`, `doctor.py`, `config/loader.py`,
  `config/schemas.py`, `config/env_file.py`, `ingress/app.py` to `tr()`
  (`E1xxx`/`W1xxx`/`I1xxx`). Added `ConfigNotFoundError`/`ConfigValidationError`
  with `message_id`/`hint`. Refined `resolve_model_to_profile`: vendor prefix
  stripping, threshold 3->5 chars, case-insensitive alias. Windows parity in
  `conftest.py` and Language section in `README`/`README.en.md`.
- **Automated Argos Translate model setup with official SHA256 verification.**
  `scripts/setup_argos_models.py` now carries canonical SHA256 checksums for both
  `translate-ja_en-1_1.argosmodel` and `translate-en_ja-1_1.argosmodel`
  (`623e3477...`), verifies downloads before installing into the Argos package
  index, and satisfies the CI `--require-hash` gate (resolves TODO(K-1)).
- **Fast-path detection for installed translation models.**
  `scripts/setup_argos_models.py` checks whether direct JA<->EN models are
  already installed and usable before attempting remote package index updates or
  downloads, eliminating redundant 230MB re-downloads on subsequent installer runs.
- **Windows installer scripts.**
  Added `installCodeRouter.bat` / `installCodeR.bat` automated installers and
  integrated the Argos model downloader into the installation flow. Enforced CRLF
  in `.gitattributes` (`*.bat text eol=crlf`) and updated CI to cover the new
  `--require-hash` path.

### Fixed

- **Resolve profile from request `model` field in ingress routes.**
  When `profile` is not explicitly specified (`X-Coderouter-Profile` / body
  `profile` / mode header), `POST /v1/messages`, `POST /v1/chat/completions`
  and `POST /v1/messages/count_tokens` now resolve `config.resolve_model_to_profile(payload.model)`
  and route accordingly. Logged as `model-resolved-to-profile`. Covers both
  `kind: anthropic` passthrough and OpenAI-compat paths (`coderouter/ingress/anthropic_routes.py`,
  `openai_routes.py`, `config/schemas.py`, `tests/unit/test_resolve_model.py`).
- **Windows batch installer parsing and multibyte CRLF stability.**
  Fixed cmd.exe syntax errors caused by LF line endings and unescaped redirect /
  parenthesis characters in `installCodeRouter.bat` and `installCodeR.bat`. Ensured
  enforced CRLF in `.gitattributes`.
- **Argos model download fallback endpoints.**
  Updated `FALLBACK_URLS` in `scripts/setup_argos_models.py` to active official
  `https://argos-net.com/v1/` endpoints for direct download when the Argos
  package index is unavailable.

---

## [v2.15.0] — 2026-08-21 (fallback explainability + stream truncation detection)

### Added

- **Fallback explainability — `X-CodeRouter-Fallback-*` response headers.**
  When a request changes providers, the response now says who it left, who
  took over, and why: `X-CodeRouter-Fallback-From` / `-To` / `-Reason` /
  `-Chain` (multi-hop chains render as `local>ollama>openrouter` with one
  comma-separated reason per departure). Reasons come from a single
  canonical vocabulary in `coderouter/routing/fallback_trace.py` covering
  both chain-resolve filters (`paid-gate`, `budget-exceeded`,
  `memory-pressure`, `backend-unhealthy`, `self-healing-excluded`,
  `unknown-provider`) and runtime attempt failures (`timeout`,
  `rate-limit`, `auth`, `upstream-5xx`, `upstream-4xx`, `connection`,
  `empty-response`, `empty-stream`). The same trail is emitted as
  structured `fallback-occurred` log lines (auditable) and, on the
  Anthropic streaming path, as a trailing `coderouter_fallback` SSE
  metadata event for reasons discovered after the HTTP headers ship.
  Purely additive — a request served by its first provider produces no new
  headers, no SSE trailer and no log lines.
- **Stream truncation detection — `stream_truncation_action`
  (`off` | `warn` | `error`, default `off`).** An upstream can end its HTTP
  body *cleanly* while the LLM protocol carried inside it is still
  mid-message: no `message_stop` on the Anthropic wire, no `data: [DONE]`
  and no `finish_reason` on the OpenAI wire. llama.cpp slot preemption, an
  `--n-predict` cut-off, a proxy closing an EOF-delimited body, or a local
  server that OOM'd all look exactly like that. Transport breakage
  (timeouts, `httpx.RemoteProtocolError`) was already caught; this is the
  layer the transport cannot see, and until now CodeRouter reported it as a
  *successful* completion — the translation layer's terminator-synthesis
  guards fabricated `stop_reason: end_turn` so the client would not hang,
  which also erased every trace of the cut. The synthesis stays (removing
  it hangs Claude Code); what is new is that the engine gets told. Both
  adapters now track whether a terminator arrived, and under `error` raise
  a retryable `StreamTruncatedError` that joins the *existing* branches: no
  bytes forwarded yet → fall back to the next provider with reason
  `stream-truncated`; bytes already out → `MidStreamError`, which
  `partial_stitch_action: surface` renders as a graceful close carrying a
  `coderouter_partial` event labelled `stream_truncated`. If *every*
  provider in the chain truncates before any byte reached the client, the
  request ends in `NoProvidersAvailableError` (an `overloaded_error` SSE
  frame) rather than flushing a half-written message — see **Fixed** below.
  Because the failure is an ordinary `AdapterError`, the L2/L4/L5/L6
  self-healing guards learn it — a backend that keeps going quiet gets
  demoted by adaptive routing instead of being *preferred* for its fast
  first byte. `warn` measures without changing a single byte of output;
  `off` is byte-for-byte v2.14.0 **on the wire** — see the caveat on
  guard-internal state below. Observability: a `stream-truncation-detected` log
  line (with `wire`, `events_forwarded`, `saw_stream_start`,
  `tool_call_in_flight`), `stream_truncated_total` in `/metrics.json` and
  Prometheus (labelled by provider and by action), and the
  `stream-truncated` reason on the `X-CodeRouter-Fallback-*` headers.
  Terminator matching is deliberately lenient — a `message_delta` carrying
  a `stop_reason`, or a `finish_reason` on any choice, both count — so
  providers that legitimately omit the sentinel are not flagged. On the
  Anthropic streaming path, pre-content fallback additionally needs
  `empty_response_action: fallback`, which is the knob that withholds the
  opening events from the client. Known gap: the terminator synthesis this
  entry refers to lives in `translation/convert.py`, so the one route that
  does not pass through it — `/v1/messages` served by a `kind: anthropic`
  backend, i.e. native passthrough — has never had it. Under `off` a
  truncated native stream still reaches the client unterminated, exactly as
  in v2.14.0; that route is the strongest argument for moving to `warn`.
  Docs: `docs/concepts/stream-truncation.md` / `.en.md`.
- **Tool-call integrity regression tests across a fallback hop**
  (`tests/test_fallback_tool_integrity.py`): the conversation handed to the
  second provider keeps every `tool_use` / `tool_result` id paired, and the
  response the client receives is still a structurally valid `tool_use`
  block on both the buffered and the streaming path.
- **Stream truncation tests** (`tests/test_stream_truncation.py`): both
  adapters driven over real SSE bodies (off / warn / error, the
  terminator-leniency false-positive guards, the tool-call-in-flight flag),
  engine-level fallback and `MidStreamError` integration, the ingress
  `coderouter_partial` labelling, and an explicit `off` regression proving
  the default path — including the H6 terminator synthesis — is unchanged.

### Fixed

- **Anthropic streaming: a provider that failed before forwarding any byte
  was invisible to the self-healing guards.** Under
  `empty_response_action: fallback` the engine withholds the opening events
  until real content appears, and an `AdapterError` raised in that window
  is handled by swapping providers instead of raising. That branch recorded
  an adaptive failure but never called the L2 memory-pressure / L4 drift /
  L5 backend-health hooks — while `_observe_provider_success` had *already*
  fired the moment the first event landed. The net effect was a backend
  that opens a stream and then dies keeping a clean bill of health, and
  adaptive routing preferring it for its fast first byte. The identical
  failure under `empty_response_action: off` takes the mid-stream path and
  *was* observed, so provider health effectively depended on an unrelated
  knob. Both hooks now run on that branch (and the error joins the `errors`
  list carried by `NoProvidersAvailableError`), matching the mid-stream
  sibling. A stream that terminates cleanly with no content is still not a
  failure — it is a 200 blank, and reaches neither hook.
  **Operational note:** this is a behavior change for anyone already
  running `empty_response_action: fallback` — pre-content failures now
  reach L2/L4/L5, so if `memory_pressure_action: skip` and/or
  `backend_health_action: demote` are also set, skips and demotions can
  become *more frequent* for a backend that was silently dying before real
  content shipped, even though nothing about those knobs' own
  configuration changed. This is independent of `stream_truncation_action`
  — it fires under the default `off` exactly as it does under `warn` /
  `error`, since the branch it lives in is the *pre-existing*
  `empty_response_action: fallback` path, not the new truncation
  detection. See `docs/concepts/stream-truncation.md` / `.en.md` for
  details.
- **Anthropic streaming: a chain where every provider truncated left the
  client hanging.** On chain exhaustion the engine flushes the last
  buffered preamble so a genuinely empty chain returns a well-formed 200
  blank (`message_start` … `message_stop`). A *truncated* stream's buffer
  has no `message_stop` by definition, so flushing it emitted an SSE
  sequence that never ended — no exception, no error frame, and a client
  waiting forever — and charged `empty_responses_total` for a stream that
  was not empty. The flush is now gated on the buffer actually terminating;
  a truncation-produced buffer falls through to `NoProvidersAvailableError`,
  which the ingress turns into a terminal `overloaded_error` SSE frame. Only
  reachable with `stream_truncation_action: error`, so the default path is
  unchanged, and the empty-chain 200 blank is untouched (including the
  mixed case where a truncated hop is followed by a provider that answered
  with a well-formed empty message — that buffer still gets flushed).
- **`partial-stitch-surfaced` log shape under the default `off`.** The
  `reason` key added for truncation labelling was attached
  unconditionally, so every `partial_stitch_action: surface` log line
  gained a field even where truncation detection is disabled and the value
  could only ever be `mid_stream_failure`. It is now emitted only when it
  says something new, restoring the v2.14.0 key set. (`/metrics.json` was
  never affected — `MetricsCollector` whitelists the fields it copies.)

---

## [v2.14.0] — 2026-08-09 (credential hygiene + undo)

Five changes that came out of reading `duolahypercho/codex-router` and
comparing it with what CodeRouter actually does. Four of them close holes
this project had; one imports a capability it was missing. Everything is
opt-in except the `.envrc` behaviour change called out under **Changed
(BREAKING)**.

### Added

- **`credential.source: cli_session` — borrow the token a vendor CLI
  already wrote.** A subscription-authenticated provider (Kimi Code CLI,
  Grok CLI, …) can now be an ordinary `openai_compat` / `anthropic`
  entry: CodeRouter reads the OAuth token out of the JSON file the
  vendor's CLI maintains and makes the HTTP call itself. Until now the
  only answer to those was `kind: agent_cli`, which spawns the CLI once
  per request and therefore has no streaming, no tool-call repair, no
  context-budget guard, and does not sit in a fallback chain — the
  subscription ended up an island. It is now a chain link:
  `providers: [kimi-sub, free-cloud, local-llama]` just works.
  Refresh delegates to the vendor's own CLI (`refresh.command`, an argv
  list run with `shell=False`) rather than reimplementing OAuth, because
  endpoints, client ids and rotation policies all change and a bespoke
  implementation rots. Refresh is single-flighted in-process and
  serialised across processes with an advisory `flock`. `credential` and
  `api_key_env` are mutually exclusive at load time, `credential.path`
  must live under `$HOME`, and a missing or malformed session file
  resolves to `None` (unauthenticated request → upstream 401 → the chain
  moves on) instead of raising. See `examples/providers.cli-session.yaml`.
- **`coderouter rollback`** — restores `providers.yaml`,
  `~/.coderouter-t/model-capabilities.yaml` and (with `--workspace`)
  `.vscode/settings.json` / `.envrc` from their `.bak` siblings.
  `doctor --apply` and `vscode-init` have always written a backup and
  never offered a way back. Restore is a **swap**: the current contents
  become the new `.bak`, so a second run returns you to where you
  started. Exit 0 restored / 2 nothing to restore / 1 a restore failed.
- **`coderouter doctor --check-secrets`** — proves the log-redaction
  filter scrubs by pushing a canary through it, reports which declared
  `api_key_env` vars are actually set, flags credentials pasted into a
  `base_url`, and scans the already-written `requests.jsonl` /
  `audit.jsonl` (plus rotated `.1`) for live key values. Same 0/2/1 exit
  contract as `--check-env`. The log scan is the part that produces
  evidence rather than reassurance.
- **`CODEROUTER_METRICS_TOKEN`** — opt-in token auth for `/dashboard`,
  `/metrics.json` and `/metrics`, mirroring `CODEROUTER_LAUNCHER_TOKEN`.

### Changed (BREAKING)

- **`vscode-init --with-envrc --force` no longer replaces `.envrc`.** The
  block CodeRouter owns is now fenced between
  `# BEGIN coderouter-managed` / `# END coderouter-managed`, and only
  that block is rewritten. Consequences:
  - A re-run against a fenced file needs **no `--force`** — rewriting our
    own fence is not destructive, so a port change is an ordinary update
    instead of a conflict to override.
  - `--force` now means "adopt a file I did not write": the block is
    appended and every existing line survives. Previously it deleted
    them, which is how the H-11 `.envrc` incident happened — including
    the `source_env_if_exists .envrc.local` line the docs tell users to
    add.
  - A managed variable exported *outside* the fence is refused, because
    direnv applies exports in order and a silent duplicate leaves nobody
    able to say which value is live. Commented-out lines are not
    conflicts.
  - A file byte-identical to a pre-v2.14.0 generation is adopted into the
    fence silently, so upgrading does not demand `--force` for our own
    previous output.
  `settings.json` handling is unchanged (it already refused to overwrite
  a user-owned value without `--force`).
- **A failed `doctor --apply` is now a no-op instead of a partial one.**
  The write loop had no error handling: an apply spanning `providers.yaml`
  and `model-capabilities.yaml` left the first file rewritten when the
  second failed. It now restores what it already wrote (and deletes what
  it created) before raising.

### Fixed (security)

- **Credentials are scrubbed from every log sink.** CodeRouter had no
  notion of "this string is a secret": keys were read by
  `resolve_api_key`, dropped into `x-api-key` / `Authorization`, and
  nothing masked them because nothing knew which strings needed masking.
  No call site logged a header dict, so this was not a live leak — the
  problem was that the safety of every future log line depended on
  whoever wrote it remembering what was in their hand. There is now a
  process-global registry keyed on the exact credential value, armed by
  `resolve_api_key` (the choke point every key passes through, plugins
  included) and pre-armed by `load_config`, plus a
  `SecretRedactingFilter` on the stderr handler, `RequestLogHandler` and
  `AuditLogHandler`. It scrubs the message, printf args, `exc_text` and
  every `extra={...}` field, recursing into nested dicts and lists.
  Exact-match registry first (zero false positives); a small set of
  anchored backstop patterns (`sk-*`, `gh*_`, `AIza*`, `Bearer`,
  `?api_key=`, URL userinfo) covers credentials that never passed through
  the resolver. Values under 8 characters are refused — redacting those
  would corrupt ordinary log text.
- **`/dashboard`, `/metrics.json` and `/metrics` were unauthenticated
  with no way to close them.** They change nothing, so they were never
  part of the H-8 work, but `/metrics.json` returns every provider's
  name, kind, paid flag and `base_url` plus the profile graph — the full
  topology of which models an operator runs and which vendors they pay.
  With `CODEROUTER_METRICS_TOKEN` unset they stay open exactly as before
  (no running scrape breaks on upgrade) and log a one-time
  `metrics-auth-disabled` warning. The check runs **before** the payload
  is assembled, so a 401 leaks nothing, and the page receives only a
  boolean — never the token. `base_url` now also goes through the
  scrubber on its way into the JSON, since it is the one config field an
  operator can paste a credential into and this endpoint hands it to a
  browser.

### Tests

- 2572 passed / 1 skipped (was 2464 on `main`), ruff clean, and 51 mypy
  errors against 53 on `main` (net −2; strict mypy is not CI-enforced).
- New: `tests/test_secret_redaction.py` (36),
  `tests/test_credentials.py` (25), `tests/test_rollback.py` (17),
  `tests/test_ui_auth.py` (17), plus 9 in `tests/test_vscode_init.py` and
  1 in `tests/test_doctor_apply.py`.
- `test_existing_envrc_force_overwrites` asserted the old destructive
  `--force` contract and is rewritten as
  `test_existing_envrc_force_adopts_without_destroying`, pinning the
  opposite.

---

## [v2.13.0] — 2026-08-07 (remaining-high security hardening)

### Changed (BREAKING)

- **Implicit `./providers.yaml` discovery is now opt-in, gated behind
  `CODEROUTER_ALLOW_CWD_CONFIG`.** Previously, starting CodeRouter (or the
  GUI launcher) from a directory containing a `providers.yaml` loaded that
  file automatically. That was a code-execution vector: a hostile config
  dropped into a repo could steer `restart_command`,
  `launcher.backends[*].binary`, and `launcher.bench.command_template` —
  all of which name executables — simply because of the working directory.
  The current-working-directory step now runs only when
  `CODEROUTER_ALLOW_CWD_CONFIG` is truthy (`1`/`true`/`yes`/`on`). When the
  opt-in is off but a `./providers.yaml` exists and was not explicitly
  named, a one-time `cwd-config-skipped` warning explains why it was
  ignored and how to enable it (`--config`, `CODEROUTER_CONFIG`, or the
  opt-in). To keep the old behaviour, set `CODEROUTER_ALLOW_CWD_CONFIG=1`
  in directories you trust. (The v2.12.0 heads-up mis-stated the landing
  release as "v2.12.0"; it actually lands here in v2.13.0.)
- **`restart_command` is now run as `subprocess.run(shlex.split(command),
  shell=False)` — argv dispatch, no shell.** Self-healing previously ran
  it through `shell=True`, so a `providers.yaml` from an untrusted source
  meant arbitrary shell execution (directly, and unconditionally at
  startup when excluded providers were restored from `state_dir`).
  Pipelines, redirects, `~` expansion, and `FOO=bar cmd` env prefixes are
  no longer supported. A value containing shell metacharacters (`&&`,
  `||`, `|`, `;`, `>`, `<`, `$(...)`, `` ` ``) is now **refused (not run)**
  rather than half-executed — under `shell=False`, `touch a; b` would
  otherwise create a file literally named `a;` and report a "successful"
  restart. Wrap shell-dependent commands in a script, or write them as
  `/bin/sh -c '...'` explicitly.
- **`doctor --apply` now writes back to exactly the file the loader read.**
  The apply path's config resolution was a hand-maintained copy of the
  loader's search order; it now delegates to the loader's single
  `resolve_config_path`, so the v2.13.0 CWD change can never make `--apply`
  rewrite a file that `load_config` did not actually load.
- **The GUI launcher (`launcher_gui.py`) applies the same
  `CODEROUTER_ALLOW_CWD_CONFIG` gate** to its own `providers.yaml`
  discovery.
- **The launcher sweep API no longer accepts a `bench_command` (or
  `results_dir`) from the request body.** `POST /api/launcher/sweep/start`
  previously took an arbitrary command string, `shlex.split` it, and ran
  it — so anyone who could reach the (loopback, token-optional) launcher
  port could run any program under the CodeRouter process. The request now
  carries only a `bench_preset` key that must name an entry under
  `launcher.bench.presets` in `providers.yaml`; the executable template
  lives solely in the operator-owned config, matching the trust boundary
  already used for `launcher.backends[*].binary`. `bench_command` /
  `results_dir` in a request are rejected with `400`. To keep a custom
  sweep command, declare it under `launcher.bench.presets`; a single
  `launcher.bench.command_template` still works unchanged as the implicit
  `default` preset. Placeholder substitution in bench templates now splits
  the template into argv *before* substituting, so a config label can no
  longer inject extra arguments.

### Fixed (security)

- **The launcher HTML no longer embeds `CODEROUTER_LAUNCHER_TOKEN`.**
  `GET /launcher` used to substitute the shared secret straight into the
  page, so `curl /launcher | grep` recovered it and defeated
  `_require_launcher_token`. The page now receives only a boolean telling
  the UI whether auth is enabled; the operator enters the token once and it
  is held in `sessionStorage` (per-tab). Token-free deployments are
  unaffected.
- **`POST /api/launcher/start` and `/sweep/start` now validate
  `model_path` / `draft_model_path` against `launcher.model_dirs`** (the
  same check previously applied only to `/suggest`). When `model_dirs` is
  unset the endpoints stay open as before, with a one-time warning;
  configure `model_dirs` when the launcher is reachable beyond loopback.
- **Stored XSS in the launcher process table is fixed.** Process-row
  buttons no longer build `onclick` attributes from the process name
  (which an HTML parser decoded back into executable JS); they use
  `data-*` attributes with a single delegated listener, so a name like
  `x');alert(1);//` renders as inert text.
- **The request body-size limit is now enforced for
  `Transfer-Encoding: chunked` requests.** The middleware previously only
  read `Content-Length`, so a chunked body bypassed the cap entirely (and
  a malformed length header fell through as unlimited). It now also counts
  bytes as they are received and aborts with `413` the moment the running
  total exceeds the cap, without buffering the body.
- **The GUI bench-sweep window no longer orphans `llama-server`
  processes.** Closing the sweep window (the `×` button) did not signal the
  worker, and sweep child processes were never registered for the app's
  bulk shutdown, so backends kept running and leaked VRAM and ports. The
  window now stops the worker and its children on close (non-blocking, via
  an `after()` poll with a grace period then `SIGKILL`), sweep children are
  drained when the app closes, and the app confirms before quitting while a
  sweep is running.

---

## [v2.12.0] — 2026-08-04 (the context-budget guard actually fires now)

**This release changes behaviour you can observe.** The token estimator
counted `tool_result`, `tool_use` and `thinking` blocks as zero
characters, which meant the L1 context-budget guard stayed silent on
exactly the sessions it exists to protect — agent conversations, where
tool output is most of the context. Fixing the estimator makes the guard
fire, the auto-router's `content_token_count_min` rules match earlier,
and `POST /v1/messages/count_tokens` return larger numbers.

Nothing was removed and no field changed meaning, so configs load
unchanged. But if you set `context_budget_action: trim`, or wrote a
`content_token_count_min` threshold, **read the migration note below
before upgrading.** `token_estimation_include_tool_content: false`
restores the v2.11.x estimate exactly.

### Fixed

- **The token estimator ignored tool blocks, so the context-budget guard
  never fired for agent sessions.** `_extract_text_from_content` only
  summed `type: "text"` blocks. A `tool_result` carrying 5,000 characters
  of file content counted as 0, and so did a `tool_use` input. In a
  Claude Code shaped session — short assistant text, large tool results —
  that under-counted by 8x at 20 turns, 14x at 50 and **24x at 200**, so
  a conversation genuinely at 96,311 tokens was reported as 4,011 and the
  0.80 / 0.90 thresholds were never approached. `context_budget_action:
  trim` was, in practice, inert for the workload it was written for. The
  estimator now walks blocks by type: `text`, `tool_result` (recursing
  into a list-shaped `content`), `tool_use` (name plus its serialised
  input) and `thinking` are counted. **`image` blocks and
  `redacted_thinking.data` stay at zero** and this is load-bearing — a
  naive `json.dumps` over the whole block would have inflated a session
  holding one 400 KB base64 PNG by 7.1x. Unknown block types are skipped
  rather than raising, and a self-referential structure terminates
  (`token_estimation.py`).
- **Trimming could empty the conversation entirely.** `_normalize_head`
  drops leading `assistant` messages and `user` messages that carry a
  `tool_result`, because neither is a valid opening turn. When the
  preserved tail was `[assistant(tool_use), user(tool_result), …]` — the
  normal shape of an agent conversation — every message matched and the
  trimmer returned an empty list. `AnthropicRequest.messages` has no
  `min_length`, so pydantic accepted it and the upstream API answered
  400. Measured on the shipped code: 81 messages in, 0 out. This was
  reachable before this release too, but only for text-heavy sessions,
  because tool blocks estimated as zero meant trimming rarely engaged.
  Making the estimator honest would have made it routine. There is now
  no path through the trimmer that returns an empty list for a non-empty
  input (`guards/context_budget.py`).
- **Restoring a head message could undo the entire trim.** When
  normalisation left no valid opening turn, the trimmer restored the most
  recent clean `user` message it had just dropped. If that message was
  the reason the request was over budget — a pasted file, a log dump —
  restoring it put the request straight back over the window while
  reporting `status: "trimmed"`. On a 200 KB paste followed by 25 tool
  turns: 67,066 tokens in, 67,060 out, against a 32,768 window. Across a
  300-session sweep, **89% of the results that still exceeded the window
  were caused by this restore**, none by the preserve floor. Restoration
  now checks the budget: the most recent *affordable* dropped clean user
  wins, and if none fits, a synthetic
  `[earlier conversation trimmed to fit the context budget]` head is
  used instead. Same fixture: 67,066 → 17,062, inside both the window and
  the trim target. The sweep goes to 0% (`guards/context_budget.py`).
- **The trim loop was quadratic.** It rebuilt the surviving message list
  and re-scanned every character for each unit it considered dropping.
  With the corrected estimator that is 217 ms of blocked event loop for
  an 801-message conversation, and it is called synchronously from the
  request path. Per-message character counts are now computed once and
  decremented as units are dropped: 217 ms → 4 ms. Units that would free
  nothing are skipped rather than dropped, so the trimmer no longer
  destroys history without reducing the estimate. The set of dropped
  messages is unchanged — verified against the old algorithm across 200
  random conversations (`guards/context_budget.py`).
- **The readiness probe now targets the `127.0.0.1` literal rather than
  `localhost`.** Both copies of `_backend_ready` fetched
  `http://localhost:<port>/health` while the backends they probe listen
  on IPv4 only (`llama-server --host 127.0.0.1` by default), and the bare
  TCP fallback in the same function already used the literal — so the
  HTTP branch was the odd one out. On a host with no IPv6 stack at all,
  where `localhost` resolves to `::1` and nothing else, this fails every
  attempt with `Address family not supported` and a backend that is up
  and serving never registers. Hardening rather than a reported bug: on a
  host that has `::1`, the connect is refused immediately and the
  resolver falls through to IPv4, so the old form worked. Applied to both
  `ingress/launcher_routes.py` and `launcher_gui.py`, which carry
  independent copies of this function.

### Added

- **`token_estimation_include_tool_content`** (top level, default
  `true`). Set it to `false` to get the v2.11.x estimate back, byte for
  byte — verified identical across 300 fuzzed request bodies on all three
  estimator entry points. It is an escape hatch for operators who tuned
  thresholds against the old numbers and need time to retune; expect it
  to be removed in a future major. It is deliberately not per-profile:
  the auto-router runs *before* a profile is chosen, and `count_tokens`
  answers a client question that must not depend on which chain would
  have served the request (`config/schemas.py`).
- **`coderouter/py.typed`.** The package has advertised
  `Typing :: Typed` in its classifiers without shipping a PEP 561
  marker, so type checkers treated it as untyped. Verified present in the
  built wheel (`pyproject.toml`).
- **Release-time version and changelog checks.** `release.yml` now fails
  the build if the pushed tag does not match `pyproject.toml`'s version,
  or if `CHANGELOG.md` has no section for it — the awk that extracts
  release notes silently falls back to a placeholder otherwise. Skipped
  on `workflow_dispatch`, where `github.ref_name` is a branch.

### Changed

- **`examples/providers.yaml` and `examples/providers-multiagent.yaml`
  now ship `context_budget_action: warn` instead of `trim`.** README
  tells new users to `curl` the first of those, so shipping `trim`
  together with a newly-working guard would have started deleting
  history in conversations for people who never opted into it. Warn
  first, read the logs, then decide. `docs/concepts/context-budget.md`
  was updated to match.
- **`thinking` blocks are now counted.** Extended-thinking content is
  replayed to the model on the following turn — it has to be, or tool-use
  signatures break — so it occupies the window exactly like text.
  `redacted_thinking.data` is still not counted: it is opaque ciphertext,
  the same category as base64 image data.
- **Third-party GitHub Actions are pinned to commit SHAs**, with the
  version in a trailing comment so dependabot still tracks them. The
  publish job holds `id-token: write` for Trusted Publishing and the
  release job holds `contents: write`; a moved tag on any action they
  use is a direct path to the PyPI project.
- **A macOS CI runner was added and then withdrawn again before this
  release shipped.** It failed six launcher tests immediately, and the
  failures turned out to belong to the test harness rather than the
  product: every test that spawns a real stub process and requires its
  `/health` to answer 200 within the harness's 5.0s
  `readiness_timeout_s` failed, and no others. Spawning, argv
  construction, crash detection, SIGTERM handling, TTL and registry
  behaviour all passed on macOS. That 5.0s budget is 1/60th of the
  shipped default of 300.0s, so it does not represent any real
  deployment. Rather than ship six `xfail`s or a red CI, the runner is
  deferred to a follow-up that raises the harness budget first. The
  reasoning and the re-enable steps are recorded in `ci.yml` next to the
  matrix.
- **Corrected the note about tkinter on CI**, which v2.11.2 got wrong in
  both directions. The GUI test modules originally said the uv-managed
  Python on CI has no tkinter; v2.11.2 "fixed" that to say it does,
  based on measuring `uv python install 3.12.11` in a Linux container.
  The actual CI runs settle it: the ubuntu runner skips those four files
  and the macOS runner ran them. Tk availability is a property of the
  runner image, so the docstrings no longer assert it in either
  direction — they just say to keep the `importorskip`.
- **The CVE audit covers every extra.** `uv export` passed only
  `--extra dev`, leaving `accuracy` (tokenizers) and `repair`
  (json-repair) — both installable by users — outside pip-audit's view.
  Now `--all-extras`.
- **The sdist no longer allow-lists `docs` wholesale.** `only-include`
  named the whole directory, so a local `uv build` swept in
  `docs/inside/` and `docs/articles/` — private notes and article drafts
  that `.gitignore` exists to keep out. A CI build from a clean checkout
  was never affected, since those files are untracked, but a maintainer
  building from a working tree would have shipped them. The list now
  names the public subdirectories, and `release.yml` fails the build if
  either directory reappears in the tarball. Every entry has to be
  git-tracked: `docs/evidence` exists only in a maintainer's working tree
  and made the allowlist claim to ship something a fresh checkout does
  not have. `tests/test_packaging.py` now asserts each listed path
  exists, which is how that was caught.

### Migration

Only two settings change meaning. If you set neither, upgrading is a
no-op beyond larger `count_tokens` responses.

**`context_budget_action: trim` or `warn`** — the guard was effectively
inert for tool-heavy sessions and now works. Expect
`context-budget-warning` in the logs, the
`X-CodeRouter-Context-Budget` response header, and — under `trim` —
conversation history actually being dropped, with a
`[earlier conversation trimmed to fit the context budget]` message
appearing at the head when nothing else fits. Measured against a 32 K
window, a tool-driven session now warns around turn 32 and trims around
turn 36; before, it never did either. Start with `warn` and read the
logs before enabling `trim`.

**`content_token_count_min`** — auto-router rules using it will match far
earlier. A tool-driven session that reported 1,083 tokens after 200 turns
under v2.11.x reports 93,183 now, so a `32000` threshold moves from never
firing to firing around turn 69. Check where such a rule routes:
`examples/providers.raspberrypi.yaml` sends matching traffic to a cloud
free tier rather than the local model. Retune the threshold, or set
`token_estimation_include_tool_content: false` while you do.

### Tests

- 3 new files, 48 new cases. Suite: 2393 passed, 1 skipped, ruff clean.
- Each fix has a regression test confirmed to fail against the pre-fix
  tree, including `test_trim_never_returns_empty_messages` (81 in, 0 out
  on the old code) and `test_trim_lands_inside_the_window_across_budgets`,
  which asserts that if a trimmed request is still over the window, the
  preserve floor alone must account for it — structurally forbidding the
  restore branch from being the cause.
- The incremental trim loop is pinned against the old full-recompute
  implementation across 200 random conversations (identical dropped
  sets), and the opt-out is pinned against v2.11.2 across 300 fuzzed
  bodies.

---

## [v2.11.2] — 2026-08-04 (file-write safety and launcher event hygiene)

Two fixes from the same review round as v2.11.1, split out because they
rewrite `_atomic_write` and the launcher GUI's event plumbing and deserved
their own rollback unit. **No breaking changes**: no config-schema field
changed, no CLI flag changed meaning, `--force` still replaces `.envrc`.
Existing `.vscode/settings.json` merge semantics are unchanged.

### Fixed

- **`vscode-init --force` destroyed `.envrc` with no way back.** The
  command replaced the file outright — no merge, no backup — so a
  hand-added `source_env_if_exists .envrc.local`, the very pattern
  `docs/guides/vscode.md` recommends, vanished silently. direnv then
  looked healthy while exporting nothing, which is a hard failure to
  trace back to its cause. The conflict message said only "Re-run with
  `--force` to overwrite" and never mentioned that the whole file goes.
  The previous contents are now copied to `.envrc.bak` before the write,
  the path is reported in the command's output, and the conflict message
  spells out that this is a whole-file replacement rather than a merge.
  `--dry-run` makes no backup. Only one generation is kept, so a second
  `--force` overwrites the first `.bak` (`vscode_init.py`).
- **The launcher GUI could lose track of a running backend because of
  that backend's own log output.** `_log_queue` carried control events
  and raw child stdout in the same channel, with the drain loop
  dispatching on `line.startswith("_ERR_:")` and friends. A child line
  beginning `_ERR_:` ran `del self.processes[proc_id]`, so a live
  llama-server dropped out of the process table — unstoppable from the
  UI, missed by the shutdown handler, holding its port and VRAM until
  killed by hand. A line beginning `_SPAWNED_:` with too few colons
  raised `ValueError` inside the drain loop and discarded that tick's
  log processing entirely. This is reachable in ordinary use: llama.cpp
  echoes GGUF metadata such as the chat template verbatim at startup,
  and `extra_args` wrappers put arbitrary text on the same stream.
  Events are now `(kind, proc_id, payload)` with `kind` in
  `log / spawned / ready / error / devices`; raw child output is
  enqueued unconditionally as `log` and never inspected for markers.
  `_cr_log_queue` got the same treatment, the `"_DEVICES_"` proc_id
  sentinel is gone, and a malformed control payload now skips its own
  event instead of killing the loop. While here, `_SPAWNED_` parsing
  moved from `split(":", 2)` to `rpartition`, so model names containing
  a colon no longer mangle the port (`launcher_gui.py`).

### Security

- **`vscode-init` widened the permissions of the files it rewrote.**
  `_atomic_write` created its temp file under the process umask and
  `os.replace`d it over the target, so an `.envrc` hardened to `0600` —
  a file that holds `ANTHROPIC_AUTH_TOKEN` — came back `0644`. The temp
  path was also fully predictable (`.envrc.tmp`, `settings.json.tmp`),
  so a pre-planted symlink at that path was followed and its target
  overwritten (CWE-377), and two concurrent runs in one workspace raced
  on the same name. Writes now go through `tempfile.mkstemp` with
  `O_EXCL`, are `fsync`ed, restore the original mode before the rename,
  and clean up the temp file on failure. A newly created `.envrc` is
  written `0600`, matching the standard `coderouter doctor --check-env`
  already enforces for credential-bearing files; an existing file keeps
  whatever mode the operator set. Mode handling is POSIX-only —
  `os.chmod` on Windows only toggles the read-only bit and is skipped.
  `.vscode/settings.json` is backed up before any rewrite too, not just
  under `--force`, since the merge path also edits it in place
  (`vscode_init.py`).

### Changed

- `.gitignore` now excludes `*.bak`, so the backups above and
  `providers.yaml.bak` from `doctor --apply` stay out of commits. They
  carry the same secrets as the files they shadow.
- Three GUI test modules documented their `pytest.importorskip("tkinter")`
  as a CI workaround, stating that the uv-managed Python on CI has no
  tkinter and that these tests are therefore skipped there. Measured on
  2026-08-04: `uv python install` ships Tk 8.6 on both 3.12.11 and
  3.13.7, and all 93 cases in those files run and pass on CI. The
  docstrings now say so, and record that the guards stay for local
  system pythons built without Tk — which do exist.

### Tests

- 1 new file, 38 new cases (`test_launcher_gui_events.py`, plus
  additions to `test_vscode_init.py`). Suite: 2345 passed, 1 skipped,
  ruff clean.
- Each fix has a regression test confirmed to fail against the pre-fix
  tree — 10 for the write path, 7 for the event path. The headline one
  asserts a live process survives its own `_ERR_:` stdout line; on the
  old code `app.processes` comes back empty. Another plants a symlink at
  the predictable temp path and asserts its target is untouched.

---

## [v2.11.1] — 2026-08-04 (second-round review: correctness & hardening)

Seven fixes from a full-source review of the 36k-line package, plus two
deprecation warnings that pre-announce the behaviour changes queued for
v2.12.0. **No breaking changes**: no config-schema field was removed or
narrowed, no CLI flag changed meaning, and every default is unchanged.
The two new warnings do not alter behaviour — they only tell you which
of your settings will need editing before v2.12.0.

### Fixed

- **Brace scanning in `tool_repair` was quadratic and blocked the event
  loop for minutes.** `_find_balanced_json_objects` and
  `_find_candidate_object_spans` restarted a full scan to end-of-text at
  every `{`, so an assistant reply carrying unclosed braces — the
  ordinary shape of a deep nested JSON truncated by `max_tokens` — cost
  O(k·n). Measured on the old code: 3.997 s for 8 KiB of `{`, 22.199 s
  for a 48 KiB reply, 139.019 s for 48 KiB of bare `{`. Both scanners
  are called synchronously from `to_anthropic_response`, which
  `FallbackEngine._generate_anthropic_impl` / `._stream_anthropic_impl`
  invoke from inside `async` handlers, so the whole server stalled for
  the duration — every concurrent request, not just the offending one.
  Replaced with a single right-to-left sweep that builds a depth-0
  closing-brace table (`table[j]` = the first depth-0 `}` reached when
  scanning from offset `j` outside any string); the answer for an
  opening brace at `q` is `table[q+1]`, which is by definition the
  fresh-restart semantics the old code had. Same inputs now take
  0.004 s / 0.016 s / 0.024 s. The strict (`"` only) and lenient (`'`
  too) quoting rules live in separate tables and cannot bleed into each
  other. Scan output is unchanged — see Tests. Added
  `_MAX_BARE_SCAN_CHARS` (256 KiB); above it the bare-JSON pass is
  skipped with a `tool-repair-input-too-large` warning while the fenced
  / XML / R4c passes still run. The two `to_anthropic_response` call
  sites moved to `asyncio.to_thread`; the function itself stays
  synchronous (`tool_repair.py`, `routing/fallback.py`).
- **`doctor --apply` dropped `output_filters` a user had configured.**
  The probe emitted only the filters that were *missing*, but
  `deep_merge_dicts` replaces lists wholesale, so applying
  `output_filters: [strip_thinking]` to a provider already carrying
  `[strip_stop_markers, my_filter]` left it with `[strip_thinking]`
  alone. `strip_tool_call_xml` and `repair_byte_fallback` were dropped
  100% of the time, since no probe ever recommends them. The patch now
  carries the union — existing chain first, missing filters appended.
  Order is load-bearing (`OutputFilterChain.feed` applies left to
  right), so the union never sorts; `deep_merge_dicts` semantics are
  untouched (`doctor.py`).
- **`doctor --check-model` could write `capabilities: tools: false` for
  a model that supports tools.** The tool-call probe judged on
  `content[:200]` — a display-oriented truncation — while the probe
  budget is 256 tokens, or 1024 for thinking models (roughly 3–4 k
  chars). A model that emits a preamble before its tool JSON, which is
  exactly what thinking models do, fell through to the "nothing
  tool-shaped at all" branch and got a `tools: false` patch that
  `--apply` then wrote. This re-created, by a different route, the
  false positive the v1.8.3 budget increase was meant to remove.
  Detection now reads the full body up to `_TOOL_PROBE_SCAN_CHARS`
  (16 KiB) and the 200-char slice is display-only; the detail line
  reports how many chars were actually scanned (`doctor.py`).
- **Stray `stop_reason` values from Anthropic were treated as malformed
  responses.** `AnthropicResponse.stop_reason` was a closed `Literal`,
  and `ConfigDict(extra="allow")` does not widen a declared field, so
  `pause_turn` (server-tool turns), `refusal` and
  `model_context_window_exceeded` raised `ValidationError`.
  `AnthropicAdapter` converts that to a retryable `AdapterError`, so a
  perfectly good — and already billed — reply was discarded and the
  chain fell through to the next provider, or to
  `NoProvidersAvailableError` if it was the last. Streaming was
  unaffected (`AnthropicStreamEvent` keeps its payload as a plain
  dict), so only non-streaming requests were hit. The field is now
  `str | None`, deliberately open: Anthropic has added stop reasons
  three times and a closed `Literal` reproduces this bug on the next
  one. `_REVERSE_FINISH_REASON_MAP` gained `pause_turn` → `stop`,
  `refusal` → `content_filter`, `model_context_window_exceeded` →
  `length`. While here, `drift_detection._EXPECTED_STOP` gained
  `stop_sequence` — it was counting a documented, ordinary stop reason
  as an anomaly (`translation/anthropic.py`, `translation/convert.py`,
  `guards/drift_detection.py`).
- **`import coderouter.translation` failed on its own.** The package
  reached `adapters` → `registry` → `anthropic_native`, which imported
  `translation.convert` back at module scope. `pytest tests/` passed
  only because alphabetical collection imports `test_adapter_anthropic`
  first and primes `coderouter.adapters`; running
  `pytest tests/test_tool_repair.py` on its own was a collection error.
  The three `convert` symbols moved into the `generate()` / `stream()`
  bodies that use them (`adapters/anthropic_native.py`).

### Security

- **`doctor --apply` silently rewrote `providers.yaml` while reporting
  "already up to date", then destroyed the original in `.bak`.** The
  write was gated on `diff_text` — "does a ruamel re-dump differ from
  what is on disk" — not on whether a patch changed anything. ruamel
  re-flows quoted scalars at 80 columns and renders an explicit `null`
  as an empty scalar, so a fully idempotent re-run still produced a
  diff and still wrote. The CLI meanwhile took the `is_no_op` early
  return and printed `All N patch(es) already applied`, showing neither
  the diff nor a backup line. The *next* `--apply` then copied that
  already-reformatted text over `providers.yaml.bak`, which is
  deliberately un-timestamped and single-slot — so the only copy of the
  operator's original was gone. The shipped `examples/providers.yaml`
  is affected (five `api_key_env: null` lines), as is
  `examples/providers.llamacpp-vllm.yaml` (two `binary: null`). The
  write is now gated on the merge result per target file; a target
  whose merges were all no-ops reports an empty diff and parks the
  cosmetic delta in `ApplyResult.reformat_only`, surfaced under
  `--dry-run` only. The dumper also sets `width = 4096`, as defence in
  depth. Every exit path of the apply command — including both
  exception returns — now states how many files were written.
  **If you ran `doctor --apply` on v2.11.0 or earlier, compare
  `providers.yaml` against `providers.yaml.bak` once.** The
  reformatting is semantically equivalent YAML, but the first `--apply`
  after upgrading will show the un-wrapping alongside your intended
  change; that is expected and happens once (`doctor_apply.py`,
  `cli.py`).
- **A malformed GGUF file could hang or crash the launcher, including
  from the HTTP route.** `_skip_value` recursed once per array nesting
  level at 12 bytes per level, so a ~12 KB file raised `RecursionError`
  — which is not `GGUFParseError`, so it went straight through
  `try_read_gguf_metadata`'s handler. Separately, array elements were
  skipped with `fh.seek(size, 1)` per element with no end-of-file
  check: a 49-byte file declaring `count = 1<<24` spun 16.7 M times,
  took 7.94 s, and returned a `GGUFInfo` rather than raising. Both are
  reachable from `POST /api/launcher/start`, and
  `launcher_speculative.find_draft_companion` reads *every* `.gguf` in
  the configured model directories, so one bad file poisons the scan.
  `_skip_value` is now iterative over an explicit work stack —
  `RecursionError` is structurally unreachable — with
  `_MAX_ARRAY_DEPTH = 8` retained as an independent bound (real GGUF
  arrays are depth 1; llama.cpp's writer emits nothing deeper). Every
  skip validates against the file size before seeking, and fixed-width
  scalar arrays are skipped in one bulk seek: a 150 k-element `u32`
  array went from 0.0624 s to 0.000102 s, and the 49-byte case from
  7.94 s to 0.000043 s with a `GGUFParseError`. `p.open()` is now
  wrapped for the stat/open TOCTOU, and `try_read_gguf_metadata`
  catches `OSError` / `RecursionError` / `MemoryError` as well
  (`gguf_introspect.py`).

### Changed

- **Deprecation warning: `providers.yaml` discovered in the current
  directory.** When neither `--config` nor `CODEROUTER_CONFIG` resolves
  and the file actually loaded came from `./providers.yaml`, a
  `cwd-config-loaded` warning is logged once per process naming the
  path. In v2.12.0 this search step becomes opt-in behind
  `CODEROUTER_ALLOW_CWD_CONFIG`, because starting the server in an
  untrusted directory currently means loading that directory's
  execution-bearing settings. Behaviour is unchanged in this release
  (`config/loader.py`).
- **Deprecation warning: shell syntax in `restart_command`.** A value
  containing `&&`, `||`, `|`, `;`, `>`, `<`, `$(` or a backtick, or
  starting with `~/` or `FOO=bar`, now logs
  `restart-command-shell-syntax` at config load. In v2.12.0 the command
  will be `shlex.split` and run with `shell=False`, so those forms
  break — and `pkill x && x` breaks *silently*, with `&&` passed to
  `pkill` as an argument. Wrap such commands in a script, or use an
  explicit `/bin/sh -c "..."`. This is a warning only; nothing is
  rejected and execution is unchanged (`config/schemas.py`).

### Tests

- 6 new files, 184 new cases (`test_tool_repair_differential.py`,
  `test_tool_repair_scanners.py`, `test_examples_yaml_roundtrip.py`,
  `test_import_hygiene.py`, `test_stop_reason_forward_compat.py`,
  `test_config_cwd_warning.py`, plus additions to `test_doctor.py`,
  `test_doctor_apply.py` and `test_gguf_introspect.py`). Suite: 2307
  passed, 1 skipped, ruff clean.
- The scanner rewrite is pinned by a differential test that carries the
  old quadratic algorithm as a reference implementation and compares
  both scanners across five random alphabets × 2500 inputs each, plus
  the full `benchmarks/tool-repair` corpus end to end. A naive global
  single-pass rewrite — the obvious way to linearise this, and the way
  that looks correct — diverges on 6870 of 12500 strict cases and 6977
  lenient, because an apostrophe in ordinary prose (`I'll`, `don't`)
  opens a single-quoted string that swallows every following brace. The
  differential test was verified to catch exactly that.
- Each fix above has a regression test confirmed to fail against the
  pre-fix tree, including
  `test_cli_no_op_message_states_nothing_written` (the "up to date"
  message printed while writing),
  `test_no_op_apply_leaves_example_byte_identical` (the shipped
  examples), `test_array_count_past_eof_raises_immediately` (raises,
  and inside 0.5 s) and `test_lenient_survives_prose_apostrophe`.

---

## [v2.11.0] — 2026-07-28 (launcher backend variants)

### Added

- **Backend variants — pick which llama.cpp build to launch.**
  `launcher.backends` now accepts keys of the form `<base>-<variant>`
  (`llama.cpp-cuda`, `llama.cpp-vulkan`, `llama.cpp-rocm`), each naming
  an additional build of the same backend with its own `binary` path.
  Declared variants appear as extra entries in the Launcher's backend
  select (GUI and Web) marked `⚙`, and each one gets its own
  `--list-devices` probe, its own `option_profiles`, and can be pinned
  per model in `launcher.swap`. Motivation: on a machine with mixed
  GPUs the visible devices differ per build — the CUDA build enumerates
  `CUDA0`/`CUDA1` (RTX 5090 + 3090) while the Vulkan build also
  enumerates `Vulkan2` (Radeon 8060S) and the ROCm build only the
  Radeon — so the best build varies by model. Previously `binary` held
  a single path and switching meant editing `providers.yaml` and
  restarting. Docs: `docs/backends/launcher.md` "特化ビルドの切り替え",
  design: `docs/designs/launcher-multi-build.md`.
- **`option_profiles` inheritance for variants.** A variant key
  inherits the base backend's presets and appends its own; a preset
  whose `name` collides replaces the inherited one *in place* (order
  stays stable, no duplicate at the tail). Shared presets no longer
  need duplicating under every variant key. Shared implementation:
  `launcher_devices.resolve_option_profiles` (used by the Web routes,
  the Tk GUI and `launcher_swap`).
- **Cross-build bench sweep.** With two or more llama.cpp builds
  declared, the sweep panel shows a "⚙ ビルド横断" button that probes
  every build and generates configurations labelled
  `cuda / CUDA0 単体`, `vulkan / Vulkan2 単体`, … Running the sweep
  launches the same model on each build in turn, so one sweep answers
  "which build is fastest for this model". `SweepStep.backend` /
  `SweepConfigItem.backend` carry the per-step build; `None` keeps the
  previous plan-wide behaviour. New
  `launcher_devices.build_cross_variant_sweep_configs`. Configurations
  never mix devices across builds — one process runs one executable.
- **Device-id namespace guard.** Device ids are build-specific
  (`CUDA0` and `Vulkan0` are not the same GPU). Switching backend in
  either launcher now clears the device selection, and
  `POST /api/launcher/start` / `sweep/start` reject ids that do not
  exist in the chosen build with a 400 instead of letting
  `--device CUDA0` reach a Vulkan build and fail at startup. Skipped
  when `--list-devices` itself fails (best-effort, per the
  `hardware.py` invariant). New `launcher_devices.foreign_device_ids`,
  which compares the backend *prefix* (`backend_of`) rather than the
  exact id — the mismatch worth catching is the wrong build, not a
  number gap, and prefix matching stays correct if a future
  `--list-devices` format change makes `parse_list_devices` miss a line.

### Changed

- **BREAKING: `launcher.backends` keys are now validated.** A key must
  be `llama.cpp` / `vllm` / `mlx` or one of those with a
  `-<variant>` suffix (variant matching `[a-z0-9][a-z0-9._-]*`).
  Previously any key was accepted and silently ignored, because the
  backend list was a hard-coded set of three; a typo such as
  `llamacpp:` therefore had no effect and no warning. It is now a
  config-load error. Check key spelling if startup fails after
  upgrading.
- **A backend variant must set `binary`.** Allowing it to be omitted
  would fall back to the base default on PATH, meaning an operator who
  selected `llama.cpp-cuda` could silently get the plain build — the
  hardest failure of this feature to notice. Rejected at config load.
  Base keys keep `binary` optional (PATH resolution), unchanged.
- **The Launcher's backend list is now config-derived.** Both launchers
  build the select from the three base backends plus whatever variants
  `launcher.backends` declares, instead of a hard-coded list. Declaring
  no variants yields exactly the previous three entries, so specialized
  builds stay invisible to operators who have not opted in.
- `SwapModelSpec.backend` relaxed from
  `Literal["llama.cpp","vllm","mlx"]` to a validated `str` so it can
  name a variant; the three previous values still validate unchanged.
  When a variant is named, config load verifies it is declared in
  `launcher.backends`.
- `GET /api/launcher/backends` entries gained `base` and `variant`
  keys. All pre-existing keys (`resolved` / `configured` / `default` /
  `is_custom` / `found`) are unchanged.

### Fixed

- **Backend-name branches are normalized through a single helper.**
  Backend name drove behaviour in ten places, several of which would
  have failed *open* for a variant name rather than raising. Two
  mattered: `_MODEL_FLAGS.get(backend, frozenset())` would have
  returned an empty set and **disabled the H8 model-override guard**
  (letting `options` / `extra_args` re-specify `-m` and load an
  arbitrary model), and `_backend_ready`'s
  `backend in ("llama.cpp","vllm")` would have degraded readiness to a
  bare TCP connect — re-introducing the bug readiness gating was added
  to fix (provider registered before the model finished loading). All
  branches now go through `launcher_devices.base_backend()`, in
  `launcher_routes.py`, `launcher_speculative.py`, `launcher_gui.py`
  and the inline SPA JS. `_assert_no_model_override` additionally
  became fail-closed: an unrecognized base uses the union of every
  banned flag set rather than an empty one.

### Tests

- 4 new files, 150 new cases (`test_launcher_backend_variants.py`,
  `test_launcher_variant_config.py`, `test_launcher_variant_routes.py`,
  `test_launcher_variant_sweep.py`, `test_launcher_variant_gui.py`).
  The fail-open branches above each have a 1:1 regression test, plus
  byte-identical-argv and unchanged-API-payload tests for configs
  without variants. Suite: 2098 passed, ruff clean.

---

## [v2.10.0] — 2026-07-20 (VSCode workspace scaffolder)

### Added

- **`coderouter vscode-init` — one-shot VSCode workspace scaffolder.**
  Writes `.vscode/settings.json` with `terminal.integrated.env.osx` /
  `.linux` / `.windows` populated so a Claude Code session launched
  from VSCode's integrated terminal auto-points at CodeRouter with no
  manual `ANTHROPIC_BASE_URL` juggling. Optionally emits a direnv
  `.envrc` (`--with-envrc`). The command is **idempotent** (re-running
  with the same arguments is a no-op) and **conflict-aware**: if an
  existing `settings.json` has different values for one of our managed
  keys, the file is left untouched and a unified diff is printed —
  operators re-run with `--force` to overwrite. Unrelated top-level
  keys (`editor.fontSize`, `python.testing.pytestEnabled`, etc.) and
  unrelated env keys inside the terminal blocks (a user's `PATH`
  tweak) are preserved verbatim on merge. All writes are atomic
  (temp file + `os.replace`) so a partial write cannot corrupt an
  existing `settings.json`. Non-Claude-Code extensions (Cline / Roo
  Code / Kilo Code / Continue.dev) are handled by a cheat sheet
  printed at end of run plus the fuller `docs/guides/vscode.md` —
  `vscode-init` deliberately does NOT reach into those extensions'
  own config schemas (each ships its own release cadence, so any
  automation would need continuous maintenance to follow). The
  Continue.dev cheat-sheet snippet is built via `json.dumps` rather
  than hand-formatted concatenation, so it is guaranteed to be
  syntactically valid JSON (an early hand-formatted version leaked
  an extra `}` from mismatched f-string / plain-string brace
  escaping; a test round-trips the snippet through `json.loads` to
  pin the invariant). Options: `--target PATH` (default cwd),
  `--port PORT` (default **8088**, matching every docs / quickstart
  example — note that `coderouter serve` alone still defaults to
  4000, so if you serve on 4000 pass `--port 4000` here too),
  `--profile NAME` (adds `CODEROUTER_MODE=<profile>` to the terminal
  env for header-less profile routing), `--with-envrc` (also emit
  `.envrc`), `--dry-run` (compute diffs but write nothing —
  byte-identical to the write path minus the final `os.replace`),
  and `--force` (overwrite conflicting values). Exit codes: 0 clean,
  2 conflicts (nothing was written for the conflicting file), 1
  hard error (unparseable JSON, missing target directory, write
  failure). Implementation lives in `coderouter/vscode_init.py`
  (stdlib only — no new runtime deps, keeping the strict-5-package
  rule in `pyproject.toml` intact); the CLI wrapper
  (`_run_vscode_init` in `coderouter/cli.py`) mirrors the existing
  `stats` / `audit` / `replay` "thin argparse plumbing + logic in a
  sibling module" pattern. See `tests/test_vscode_init.py` (33 cases
  across happy paths, merge preservation, conflict detection,
  `--force`, `--dry-run` byte parity, `.envrc`, malformed inputs,
  JSON validity of the Continue.dev snippet, and CLI wrapper
  plumbing) and the new `docs/guides/vscode.md` for the full Cline /
  Roo / Continue.dev cheat sheet.

### Documentation

- **Launcher port ↔ `providers.yaml` mismatch, three ways to prevent
  it.** New section
  ["providers.yaml とのポート整合"](docs/backends/launcher.md#providersyaml-とのポート整合食い違いを構造的に防ぐ)
  in the Launcher guide walks through the three coexisting patterns
  — (A) hardcoded `providers.yaml` with matching Launcher port
  (traditional), (B) v2.7.4 Launcher auto-sync
  (`launcher-<backend>-<port>` provider names, no `providers.yaml`
  edit needed), and (C) the recommended hybrid where
  `option_profiles` pins the port so the Launcher UI and
  `providers.yaml` cannot drift. Includes a copy-paste `cr-check`
  shell function that runs `coderouter doctor --check-model <name>`
  against every hand-declared provider before work begins so
  operators catch a port mismatch in seconds rather than after a
  session of quiet fallback churn. The symptom side (dashboard's
  `unhealthy` badge + half-and-half `provider-failed` events in the
  ring buffer while requests still succeed via fallback) is
  documented as a new
  [troubleshooting §1-7](docs/guides/troubleshooting.md#1-7-transport-error-all-connections-failed--ポート不一致の疑い)
  entry, cross-linked back to the Launcher guide for the fix. Both
  additions are Japanese-side only (English counterpart will follow
  in a later docs-sync commit).

### Fixed

- **`ProviderConfig.timeout_s` upper bound raised from 600s (10 min) to
  86400s (24h)** (#76, contributed by @firelzrd). The previous 10-minute
  ceiling was too restrictive for long-running local model inference —
  configurations that legitimately need multi-hour timeouts (heavy
  reasoning models, large-context sweeps, batch operations) were being
  rejected by the Pydantic `le=600` validator before the request could
  even leave the router. The default (30s) and lower bound (1s) are
  unchanged, so existing configs are unaffected; only users who were
  hitting the ceiling gain the new headroom.

---

## [v2.9.4] — 2026-07-19 (launcher device selection & bench sweep, cache-stable system prompt)

### Added

- **llama.cpp device selection (`--device` / `--tensor-split`) in the
  Launcher.** Both the desktop GUI and the Web edition can now detect
  available `llama-server` devices (via `--list-devices`), display their
  VRAM, and let the operator pick which one(s) to offload to instead of
  always relying on llama.cpp's own default placement. New shared module
  `coderouter/launcher_devices.py` (dataclasses + pure functions only, no
  pydantic, so the standalone GUI can import it without the `coderouter`
  package) owns detection/caching (`detect_llama_devices`,
  `parse_list_devices`, a 60s TTL cache keyed by binary path),
  CLI-fragment building (`DeviceSelection.to_cli_args` — empty selection
  emits nothing, preserving today's launch command byte-for-byte),
  VRAM-ratio tensor-split suggestion (`suggest_tensor_split`), and backend
  grouping (`backend_of` / `group_by_backend` / `selectable_devices`) so a
  GPU listed twice under different backends (e.g. `CUDA0` and `Vulkan1`
  for the same physical card) is never double-counted and cross-backend
  configs are never auto-generated. Devices reporting `0 MiB` (e.g. macOS
  `BLAS: Accelerate`) are still shown for information but excluded from
  selection/suggestion. `StartRequest` gained `device_ids` / `tensor_split`
  (both default empty — existing Web clients are unaffected), and
  `GET /api/launcher/devices` (`?backend=`, `?refresh=1`) returns the
  probe plus a per-backend `suggested_tensor_split` and `auto_configs`.
  `launcher_gui.py`'s LAUNCH form gained a device checklist (🔍 Detect,
  falling back to a manual comma-separated entry on detection failure)
  and a tensor-split field that auto-hides when one or fewer devices are
  detected. See `tests/test_launcher_devices.py`,
  `tests/test_launcher_devices_detect.py`,
  `tests/test_launcher_devices_routes.py`,
  `tests/test_launcher_gui_devices.py`, and the new "Device selection"
  section in `docs/backends/launcher.md` / `launcher.en.md`.
- **Bench sweep: automated device-configuration benchmarking against an
  external `llmbench`.** Both Launcher editions can now drive a list of
  device configurations (e.g. `CUDA0` alone / `CUDA1` alone / a
  multi-GPU split) through start → wait for readiness → run an external
  bench command → stop → advance to the next configuration, then compare
  the results. `coderouter/launcher_devices.py` supplies the shared
  `SweepPlan` / `SweepStep` / `SweepState` state machine,
  `build_auto_sweep_configs` (one config per selectable device plus one
  per multi-device backend group), `render_bench_command` (template
  placeholders `{port}` `{config}` `{base_url}` `{results_dir}` `{runs}`,
  expanded via plain string substitution rather than `str.format` so JSON
  braces in the command aren't misinterpreted, then `shlex`-split with
  `posix=False` on Windows to keep backslash paths intact), and
  `load_latest_results` / `summarize_results` (best-effort, alias-tolerant
  extraction of `tokens_per_sec` / `ttft_ms` / `latency_ms` from the
  newest `llmbench` results JSON). New `LauncherBenchConfig`
  (`command_template` / `runs` / `results_dir` / `readiness_timeout_s`,
  read from an optional `launcher.bench:` block — omitted entirely by
  default, so existing `providers.yaml` files are unaffected) supplies
  the sweep's defaults. The Web edition gained
  `POST /api/launcher/sweep/start` (token-gated; 409 if a sweep is
  already running, 400 for empty `configs` or a busy/unavailable port),
  `GET /api/launcher/sweep/status`, `POST /api/launcher/sweep/abort`
  (token-gated), and `GET /api/launcher/sweep/logs`, backed by a new
  `_SweepRunner` that reuses the existing `spawn_process` /
  `stop_process` / `proc.ready` readiness primitives. `launcher_gui.py`
  gained a "📊 Bench sweep" button opening a separate `SweepWindow` with
  its own `_SweepWorker` thread, reusing `poll_until_ready` for readiness
  and driving the same external bench subprocess model. See
  `tests/test_launcher_config_bench.py` and the new "Bench sweep" section
  in `docs/backends/launcher.md` / `launcher.en.md`.

### Fixed

- **Mid-conversation `role:"system"` messages are no longer hoisted into
  the top-level `system` field.** (#75, contributed by @firelzrd.)
  Claude Code CLI ≥ 2.1.154 emits volatile system reminders as
  `role:"system"` messages *inside* `messages[]`; hoisting them meant the
  serialized prompt's head changed every turn, so llama.cpp / LM Studio
  style prefix-matching KV caches were invalidated wholesale (a captured
  real session measured only 11.8 % reusable prefix between consecutive
  requests). `normalize_message_roles()` now splits by position: leading
  system messages (before any emitted turn) are hoisted as before — the
  legitimate OpenAI-style system prompt — while mid-conversation ones are
  coerced to `user` in place, keeping `messages` append-only and the
  prompt head stable as the conversation grows. See
  `tests/test_role_normalization.py`
  (`test_leading_system_is_hoisted_but_mid_conversation_is_not`,
  `test_system_prompt_is_stable_as_conversation_grows`).

### Security

- **Dependency bumps for two fresh advisories** (lockfile-only; both
  verified with a clean `uv sync --frozen` + full test suite, 1905
  passed): `click` 8.3.2 → 8.4.2 (PYSEC-2026-2132, fix ≥ 8.3.3;
  transitive via uvicorn) and `json-repair` 0.59.10 → 0.61.5
  (GHSA-xf7x-x43h-rpqh, CVSS 7.5, fix ≥ 0.60.1). The `json-repair`
  floor in `pyproject.toml` was raised from `>=0.30` to `>=0.60.1` so
  fresh installs can never resolve a vulnerable version; `uv.lock` also
  records the new lockfile revision 3 format (upload-time metadata)
  emitted by current uv.

---

## [v2.9.3] — 2026-07-12 (GUI/Web parity, swap polish, launcher UI fixes)

### Added

- **Per-model TTL override for `launcher.swap`.** `SwapModelSpec.ttl_seconds`
  (new, optional, `None` by default) lets a catalog entry override the
  global `launcher.swap.ttl_seconds` for just that one model — `None`
  keeps following the global value, `0` unloads the model as soon as
  its last in-flight lease releases (same meaning as the global field's
  `0`, just scoped to one entry). `SwapManager.sweep_once` now resolves
  each model's effective TTL individually, and the sweeper starts
  whenever either the global TTL or at least one catalog entry's
  override is set (previously gated solely on the global value). See
  `tests/test_launcher_swap.py` (`test_ttl_override_spec_wins_over_global`,
  `test_ttl_override_unset_falls_back_to_global`,
  `test_ttl_override_zero_unloads_even_when_global_ttl_disabled`) and the
  updated `launcher.swap` field table in `docs/backends/launcher.md` /
  `launcher.en.md`.
- **Configurable ephemeral-port retry count for `launcher.swap`.**
  `LauncherSwapConfig.port_retry_attempts` (new, default `2`, range
  0–5) replaces the previously hard-coded single retry for catalog
  entries that leave `port` unset — the swap manager now attempts up
  to `1 + port_retry_attempts` spawns (each on a freshly picked
  ephemeral port) before giving up, versus the previous fixed 2 total
  attempts. Fixed-port entries are unaffected (still exactly one
  attempt — a second try on the same port would just collide again).
  This narrows the practical impact of the known pick-then-bind TOCTOU
  window documented on `SwapModelSpec.port` / `_pick_ephemeral_port`
  but does not close it outright; a fixed `port` remains the only way
  to eliminate the race entirely. See
  `tests/test_launcher_swap.py`
  (`test_port_none_retries_default_port_retry_attempts`,
  `test_port_retry_attempts_exhausted_raises`,
  `test_port_retry_attempts_configurable_to_zero`).
- **`/launcher` UI now shows which processes are swap-managed.**
  `GET /api/launcher/processes` includes `swap_managed` (bool) and
  `swap_model` (the swap catalog model name, or `null`) for every
  process; `spawn_process` gained a `swap_model` kwarg that
  `SwapManager._spawn` now passes through. The process table renders a
  small "swap" badge next to the name of any swap-managed process
  (title shows the backing catalog model when known). Manually-started
  processes are unaffected (`swap_managed: false`, `swap_model: null`).
  See `tests/test_launcher_swap.py::test_i1_on_demand_spawn_reaches_200`.

### Fixed

- **Swap-managed "stopped" processes no longer accumulate in the launcher
  registry.** `stop_process` only sets `status="stopped"` and never
  removes the entry — deliberate for manually started processes (the
  stopped row is visible history in the /launcher UI, with logs and an
  explicit ✕ delete button), but swap-managed processes went through the
  same path, so every failed readiness attempt left one permanent
  "stopped" row (`1 + port_retry_attempts` rows per failed load — 3 with
  the defaults) and every TTL unload left another, all growing
  `GET /api/launcher/processes` and the UI without bound. `SwapManager`
  now removes its own processes from the registry after stopping them,
  in both the failed-readiness cleanup (`_spawn_with_retry`) and the TTL
  unload (`_unload_locked`), guarded on `ManagedProcess.swap_managed` so
  a manual process is never swept up. Crash leftovers (a swap process
  that died on its own) are deliberately kept — their log tail is the
  only crash forensics an operator has. See `tests/test_launcher_swap.py`
  (`test_failed_load_leaves_no_registry_litter`,
  `test_ttl_unload_removes_registry_entry`,
  `test_registry_removal_skips_non_swap_processes`; adapted from the
  review repro with its assertions inverted).
- **/launcher UI no longer 404-polls a removed process's logs forever.**
  The log panel's poller (`poll()` → `refreshLogs()`, every 3s) never
  checked the response status: once the polled id left the registry —
  a `coderouter serve` restart under a still-open browser tab, a delete
  issued from another client, or (with the fix above) a swap TTL unload
  removing the entry mid-view — the 404 body has no `.logs`, the
  resulting TypeError landed in the generic catch, and `selectedLogId`
  was never cleared, so the tab kept hitting
  `GET /api/launcher/logs/<stale-id>` and spamming `404 Not Found` into
  the serve log indefinitely. The UI now stops polling as soon as the
  selected id disappears from the periodic `/api/launcher/processes`
  refresh (usually before a single 404 is even issued), treats a logs
  404 as terminal (shows "(process removed)" instead of retrying), and
  the server keeps answering 404 for unknown ids — that behavior is
  correct and now pinned by
  `tests/test_launcher_swap.py::test_logs_unknown_proc_id_is_404`.

---

## [v2.9.2] — 2026-07-12 (config: no more dummy provider for swap-only setups)

### Changed

- **`providers` / `profiles` no longer require a dummy entry for swap-only
  deployments.** Both top-level lists were `min_length=1` (or effectively
  required), forcing an unreachable placeholder provider/profile (e.g.
  `base_url: http://127.0.0.1:9`) into any config that only serves models
  through `launcher.swap`. They are now optional (`providers: []` /
  `profiles: []`, or omitted entirely) whenever `launcher.swap.enabled: true`
  and `launcher.swap.models` has at least one entry — the injected
  `launcher-swap-<name>` profile(s) already cover routing, and their
  providers are registered at runtime on first on-demand spawn. Every other
  deployment shape keeps the original fail-fast-at-load guarantee (now
  enforced by `CodeRouterConfig._check_providers_and_profiles_nonempty`
  rather than the field-level `min_length=1` it replaces), with an error
  message pointing at the swap-only exemption. See
  `tests/test_launcher_swap.py`
  (`test_swap_only_config_loads_with_omitted_providers_and_profiles` and
  neighbors) for the minimal swap-only config shape.

### Docs

- `docs/backends/launcher.md` / `.en.md` — added the `launcher.swap` (v2.9.1+)
  reference: what it does, a minimal swap-only config (no `providers` /
  `profiles`, verified against the real loader), a `launcher.swap.models[]`
  field table, the Phase 2 schema-only fields, and behavioral notes
  (auto-restart exclusion, streaming lease protection, catalog-miss
  fallthrough, no simultaneous-load cap). Also documented the `launcher:`-level
  `readiness_timeout_s` / `readiness_poll_interval_s` / `auto_restart*` fields
  and the new `loading` process status in the configuration reference
  section, and linked `docs/designs/launcher-model-swap.md` from "Related
  docs" — closing the "残課題" noted in that design doc's §10.5.

---

## [v2.9.1] — 2026-07-12 (launcher model swap Phase 1 — llama-swap-style on-demand models)

**New: `launcher.swap`** — an opt-in, dependency-free equivalent of
[llama-swap](https://github.com/mostlygeek/llama-swap)'s core loop, built on
the existing embedded Launcher. Declare a static model catalog and CodeRouter
will **spawn the backing `llama-server` on demand** when a request names the
model, **hold the request until the backend passes its readiness probe**, and
**auto-unload the process after an idle TTL** (`swap-unload`, memory returns
to zero). Only cataloged models can ever be spawned; `model_path` is resolved
against `launcher.model_dirs` both at config load (fail-fast) and again at
spawn (defense in depth). Design + as-built record:
`docs/designs/launcher-model-swap.md`. Sample: `examples/providers.swap.yaml`.
Verified end-to-end on macOS (M3 Max, Metal): cold spawn → warm reuse →
catalog-miss fallthrough → TTL unload → respawn, all green
(`_run/swap-test/` kit).

### Added

- **`launcher.swap` config block** (`LauncherSwapConfig` / `SwapModelSpec`):
  `enabled` (default `false` — zero impact until opted in), global
  `ttl_seconds` (default 1800; `null` = never, `0` = unload at last release),
  `readiness_timeout_s`, `sweep_interval_s`, `inject_auto_router_rules`, and
  the static `models:` catalog (`name` / `backend` / `model_path` / fixed
  `port` recommended / `num_ctx` / `extra_args` / speculative fields). Phase 2
  fields (`group`, `est_weights_gb`, `memory_budget_gb`, `max_loaded`) are
  schema-declared but not yet acted on.
- **`SwapManager`** (`coderouter/launcher_swap.py`): per-model
  `asyncio.Lock` + lease accounting — concurrent requests for the same model
  trigger exactly one spawn; streaming responses hold a lease for their whole
  lifetime (a mid-stream process is never TTL-evicted); a failed spawn resets
  to idle (no poisoning) and the next request retries.
- **Auto-generated routing**: with `default_profile: auto` and no explicit
  `auto_router:` block, one exact-match rule per catalog model
  (`id: swap:<name>`, `model_pattern = re.escape(name)`) is merged **ahead of
  the bundled heuristics** (an explicitly named swap model beats
  code-fence/image heuristics) while keeping the bundled rules and their
  fallthrough intact. A user-declared `auto_router:` block keeps its full-
  replacement semantics — swap rules are appended after user rules.
- **`FallbackEngine.deregister_provider`** — the long-missing inverse of
  `register_provider`; TTL unload now removes the provider from its chain,
  drops the cached adapter (closing its HTTP client), and leaves no dead-port
  entries behind.
- **Launcher readiness gating** — spawned processes now report a new
  `"loading"` status and are only registered as providers **after** the
  backend passes a readiness probe (`GET /health` for llama.cpp/vllm, TCP
  connect fallback otherwise; `launcher.readiness_timeout_s`, default 300 s).
  Requests can no longer race a model that is still loading.
  *Behavior change*: `POST /api/launcher/start` no longer performs the
  provider sync synchronously (`provider_sync` in the response is always
  `null`); watch `/api/launcher/processes` or the `provider sync:` log line.
- **Launcher auto-restart (opt-in)** — `launcher.auto_restart: true` enables
  crash recovery with exponential backoff (`auto_restart_max_attempts`,
  default 3); intentional stops (Stop button, shutdown, TTL unload) are
  marked via `ManagedProcess.stopping` and never trigger a restart. Default
  **off**, matching the opt-in stance of `restart_command`. Swap-managed
  processes are excluded — `SwapManager` is their sole supervisor.
- Tests: `tests/test_launcher_readiness_restart.py` (21),
  `tests/test_launcher_swap.py` (31), `tests/test_launcher_swap_review.py`
  (15 regression tests from the adversarial review). Full suite: 1725 passed.

### Fixed

- **Launcher-spawned providers were registered before the model finished
  loading**, so early requests failed against a half-up backend (readiness
  gating above).
- **Crashed launcher processes stayed `status="error"` forever** with no
  recovery path other than manual restart (auto-restart above).
- Adversarial-review fixes folded into the initial swap release: enabling
  swap no longer silently discards the bundled auto-router rules; TTL unload
  no longer leaks the generic `launcher-<backend>-<port>` provider (generic
  registration is suppressed for swap-managed processes); swap processes are
  exempt from launcher auto-restart (no dueling supervisors on a fixed
  port); on-demand spawn keys off the **resolved profile**, not the raw
  model string (no wasted spawns when routing goes elsewhere, and
  catalog-mismatched model names hitting a swap profile still take a lease
  so they cannot be unloaded mid-flight).

### Docs

- `docs/guides/subagent-routing.md` / `.en.md` — §6.1 measured-verification
  results updated against the 2026-07-11/12 E2E artifacts (3/4 sub-agent
  round trip + rerun evidence, `c6096ed` grok-fix reference, expected-log
  notes), new §5(e) on running the main orchestrator on a 9B-class local
  model (tool-support prerequisite, 9b→30b fallback chain, autocompact
  caveat), §7 UNCONFIRMED items resolved as measured.
- `docs/designs/launcher-model-swap.md` — full design (phases, concurrency
  model, security posture, §10 review decisions, §10.5 as-built record).
- `examples/providers.swap.yaml` + `examples/README.md` index row.

---

## [v2.9.0] — 2026-07-11 (agent_cli extraction Phase 2c — in-core adapter removed)

**PR #73**. **BREAKING.** Phase 2c of the agent_cli plugin extraction
(`docs/designs/agent-cli-plugin-extraction.md` §5/§7): the in-core
`agent_cli` adapter branch is **removed** from `coderouter/adapters/registry.py`.
`kind: agent_cli` now resolves exclusively through the external plugin
**`coderouter-plugin-agents`** — the Phase 2b grace period, during which the
in-core copy still won resolution, has ended.

**Who is affected**: anyone with one or more `kind: agent_cli` providers in
`providers.yaml`. If the plugin isn't installed and enabled, `coderouter
serve` now fails at startup.

**What to do**: install the plugin and enable it —

```bash
uv pip install "coderouter-plugin-agents @ git+https://github.com/zephel01/coderouter-plugin-agents"
# or: pip install "coderouter-plugin-agents @ git+https://github.com/zephel01/coderouter-plugin-agents"
# uv-tool installs of coderouter-cli need the plugin in the same tool env:
#   uv tool install coderouter-cli \
#     --with "coderouter-plugin-agents @ git+https://github.com/zephel01/coderouter-plugin-agents"
```

```yaml
plugins:
  enabled: [agents]
```

**What is unchanged**: existing `kind: agent_cli` provider entries and the
`agent_cli:` sub-config (`AgentCliConfig` — schema, fields, defaults) need no
edits; they remain a stable Core contract per the design's §4.4 case (b).
Per-CLI behavior (claude/codex/antigravity/grok argv, auth, output parsing)
is unaffected — only the adapter's Core-vs-plugin wiring changed.

**Safety nets**: a `kind: agent_cli` provider without the plugin installed
and enabled now fails `serve` at startup with a targeted migration hint
(install command + `plugins.enabled` snippet) rather than a generic
unknown-kind error, and `coderouter doctor` independently emits a
config-level warning with the same fix snippet for configs that would hit
this at startup.

### Removed

- **In-core `agent_cli` adapter** (`coderouter/adapters/agent_cli.py`, ~1181
  lines) and the `build_adapter` in-core branch for `kind == "agent_cli"`
  (`coderouter/adapters/registry.py`) — the adapter body, fully moved to
  `coderouter-plugin-agents` 0.1.0 in Phase 2b, is deleted from Core. `kind:
  agent_cli` now resolves only via the plugin-provided-kind lookup path.
- **`agent-cli-in-core-deprecated` warning** (introduced in Phase 2b,
  `docs/designs/agent-cli-plugin-extraction.md` §5.1) — removed along with
  the in-core branch it guarded; there is nothing left for it to warn about.

### Added

- **`coderouter doctor` migration check** — detects a config with one or
  more `kind: agent_cli` providers where `plugins.enabled` does not include
  `agents` (or the plugin isn't installed/discoverable), and emits a
  config-level warning with a ready-to-paste fix snippet
  (`plugins:\n  enabled: [agents]` plus the install command).
- **Targeted unknown-kind / migration hint at `serve` startup** — when
  `build_adapter` cannot resolve `kind: agent_cli` (plugin missing or not
  enabled), the raised error now names the specific migration steps (install
  `coderouter-plugin-agents`, add `plugins.enabled: [agents]`) instead of
  the generic "unknown kind" listing used for arbitrary typos.

### Changed

- **`ProviderConfig` / `AgentCliConfig` docstrings and error text**
  (`coderouter/config/schemas.py`) — updated to state plainly that
  `agent_cli` is plugin-served as of v2.9.0 and that the schema itself is
  unaffected.
- **`Adapter` plugin Protocol docstrings** (`coderouter/plugins/base.py`) —
  updated to drop transitional Phase 2b language (in-core shadowing,
  deprecation warning) now that `agent_cli` resolves solely through the
  plugin path, same as any other plugin-provided `kind`.

## [v2.8.1] — 2026-07-11 (agent_cli moved to coderouter-plugin-agents — Phase 2b)

**PR #72**. Phase 2b of the agent_cli plugin extraction
(`docs/designs/agent-cli-plugin-extraction.md` §4/§5/§7): the adapter body
and its behavior tests now live in the new external package
**`coderouter-plugin-agents`** (0.1.0, entry point `coderouter.adapter` →
`agents`, pins `coderouter-cli>=2.8,<3.0`). Core still bundles the in-core
`agent_cli` adapter unchanged during the transition — providers.yaml needs
no edits yet — and the in-core copy keeps winning resolution until Phase 2c
removes it.

### Added

- **`agent-cli-in-core-deprecated` warning** (`adapters/registry.py`) —
  fires once per process when the in-core `agent_cli` branch serves a
  request while the plugin is also installed and enabled, signalling that
  the plugin path takes over at Phase 2c.

### Changed

- **`tests/test_agent_cli.py` slimmed 91 → 9** — the 82 adapter-behavior
  tests (argv snapshots, parsers, env isolation, prompt-delivery
  lifecycles, E2E, gemini rejection) moved verbatim to the plugin repo per
  the design's §4.5 split map; the 9 retained tests cover the
  `ProviderConfig`/`AgentCliConfig` pydantic validation that stays in Core
  under §4.4 案(b) (AgentCliConfig remains a stable Core contract).
- `tests/test_plugin_adapter.py` +1 (deprecation-log semantics: in-core
  still serves by identity, warning fires exactly once).

## [v2.8.0] — 2026-07-11 (Plugin SDK: Adapter hook wired — Phase 2a of the agent_cli extraction)

Opens the Plugin SDK's Adapter surface: the previously dead-end `Adapter`
Protocol (name-only) is now a real engine-integrated hook, so an external
plugin package can provide new `kind` values for providers.yaml without
touching Core. This is Phase 2a of the agent_cli plugin-extraction design
(`docs/designs/agent-cli-plugin-extraction.md`): hook wiring only — the
in-core `agent_cli` adapter is unchanged and keeps working exactly as in
v2.7.10; the actual move into `coderouter-plugin-agents` is Phase 2b/2c.

### Added

- **`Adapter` plugin Protocol** (`coderouter/plugins/base.py`) — `kind: str`
  plus synchronous `build(config: ProviderConfig) -> BaseAdapter`. A factory
  shape (kind → adapter), unlike the per-request hooks: construction happens
  once at startup and does no I/O. The `adapter` entry-point group
  (`coderouter.adapter`, singular — matching the loader's group mechanics)
  moved from the future list into the active groups; the existing two-stage
  gate (installed + listed in `plugins.enabled`) applies as-is.
- **`build_adapter` plugin resolution** (`coderouter/adapters/registry.py`)
  — resolution order is in-core kinds first (`openai_compat` / `anthropic` /
  `agent_cli`; a plugin can never shadow them), then plugin-provided kinds,
  then a `ValueError` listing every known kind plus an install/enable hint.
  Both engine call sites (startup adapter cache + runtime
  `register_provider`) pass the plugin registry.
- **Tests** (`tests/test_plugin_adapter.py`, new, 10) — plugin-kind
  resolution, in-core shadowing prevention, unknown-kind error contents,
  two-stage gate via the real entry-point loader path, runtime
  `register_provider` with a plugin kind, and an E2E request through
  `POST /v1/chat/completions` served by a plugin-provided adapter.

### Changed

- **`ProviderConfig.kind`: `Literal[...]` → `str`** (`config/schemas.py`) —
  plugin kinds cannot be known at config-load time (plugins are discovered
  after the config is parsed), so the closed Literal had to open. Fail-fast
  for a typo'd kind moves one step later but stays at startup: the engine
  builds adapters for **all** providers eagerly in its constructor, so an
  unknown kind still aborts `serve` at load with a message that now lists
  in-core and plugin kinds (strictly more helpful than the old pydantic
  error). Recorded as a design deviation in
  `agent-cli-plugin-extraction.md` §8.1, including the residual limitation
  that `doctor` (HTTP-probe based) does not surface typo'd kinds.
- Plugin-group bookkeeping: `adapter` no longer warns as
  "plugin-group-not-yet-active"; `PluginRegistry` gained an `adapters`
  view alongside `input_filters` / `observers`.

## [v2.7.10] — 2026-07-11 (agent_cli Phase 1c: antigravity — Phase 1 complete)

Completes agent_cli Phase 1: `agent: antigravity` (the Google Antigravity
CLI, command `agy`) is now implemented, joining `claude` (1a), `codex`
(1b), and `grok` (1d) — all four Phase 1 backends are now in place. This
target replaces the originally-planned `gemini`: on the author's Mac, the
legacy Gemini CLI (`@google/gemini-cli` 0.50.x) now fails individual-account
OAuth outright with a verified `IneligibleTierError: This client is no
longer supported for Gemini Code Assist for individuals. To continue using
Gemini, please migrate to the Antigravity suite of products:
https://antigravity.google` (`reasonCode: UNSUPPORTED_CLIENT`,
`tierId: free-tier`) — Google discontinued Gemini CLI for individual
accounts on 2026-06-18. The same legacy CLI also still hits the
trusted-directory gate with exit 55 (`--skip-trust` /
`GEMINI_CLI_TRUST_WORKSPACE=true`), a pre-existing constraint unrelated to
the discontinuation. Google's stated successor is the Antigravity CLI
(`agy`) — a separate Go rewrite, not a fork of gemini-cli — which keeps
individual Google-account OAuth alive, including the free tier. Verified
against agy **1.1.1** (author's Mac, 2026-07-11), its flag surface differs
meaningfully from gemini-cli's: no `--output-format json` (plain-text
output only), `--mode plan|accept-edits` instead of an approval-mode flag,
a `--dangerously-skip-permissions` full-bypass flag, a CLI-side
`--print-timeout` (the first agent_cli target with its own timeout), no
`--max-turns`, and no stdin channel for the prompt at all — piping stdin to
`agy` hangs (field-verified). `agent: gemini` remains schema-declared but
is now rejected with a migration-pointer message instead of a generic
not-implemented one.

### Added

- **`agent: antigravity` builder/parser in `AgentCliAdapter`**
  (`coderouter/adapters/agent_cli.py`) — invokes the Antigravity CLI
  one-shot as `agy -p <prompt> --model <model> --mode plan --print-timeout
  <exec_timeout_s>s` (read_only default). The prompt rides on argv as the
  value of `-p` — agy has no `--prompt-file` equivalent and hangs if stdin
  is piped (field-verified), so this is the fourth prompt-delivery pattern
  among the four backends (claude/codex use stdin, grok uses
  `--prompt-file`, antigravity uses argv). Documented limitation: Linux's
  `MAX_ARG_STRLEN` (~128KiB) caps prompt size, and the prompt is visible in
  `ps` output — there is no way around this with agy's current flag
  surface. Output is plain text: stdout is decoded UTF-8, defensively
  stripped of ANSI escapes, and trimmed; an empty result raises a
  retryable `AdapterError`. There is no `--output-format json`, so token
  usage, session id, and structured errors are all unavailable — usage is
  reported as all zeros (same treatment as grok), and
  `coderouter_session_id` is never populated.
- **`sandbox_mode` → antigravity flag mapping** — `read_only` →
  `--mode plan`; `edit` → `--mode accept-edits`; `full_auto` →
  `--mode accept-edits --dangerously-skip-permissions`. As with the other
  three backends, the effective mode is clamped to read-only whenever
  `allow_file_writes=false`.
- **`--print-timeout` as a CLI-side first wall** — generated from
  `AgentCliConfig.exec_timeout_s` (e.g. `exec_timeout_s=600` →
  `--print-timeout 600s`), letting agy self-terminate before the outer
  `asyncio.wait_for` + process-group `SIGKILL` fires. antigravity is the
  only agent_cli target with a double timeout wall.
- **`max_turns` ignored for antigravity** — agy has no
  `--max-turns`-equivalent flag; `AgentCliConfig.max_turns` has no effect
  when `agent: antigravity`, same as codex.
- **`command` defaults to `"agy"`** — the only agent_cli target whose
  default executable name differs from the `agent` field value (the binary
  is named for the CLI, not the product).
- **5th `Literal` value on `AgentCliConfig.agent`** — `"antigravity"` joins
  `claude`/`codex`/`gemini`/`grok`.

### Changed

- **`agent: gemini` now rejected with a migration message** instead of the
  generic "not implemented yet" wording — constructing the adapter with
  `agent: gemini` now raises `AdapterError: Google discontinued Gemini CLI
  for individual accounts (June 2026). Use agent='antigravity' instead.`
  (`retryable=False`).
- **Schema docstrings** (`coderouter/config/schemas.py`) — `AgentCliConfig`
  field descriptions updated for the four-backend reality
  (claude/codex/antigravity/grok implemented, gemini rejected), including
  the `command` and `max_turns` docstrings noting antigravity's `agy`
  default and ignored `max_turns`.
- **Docs & examples** — `docs/backends/external-agents.md`/`.en.md` gain a
  full antigravity section (config example, adapter argv, the
  argv-vs-stdin-vs-`--prompt-file` prompt-delivery comparison across all
  four backends, mode mapping table, plain-text parsing and zero usage,
  the `--print-timeout` double wall, OAuth setup with the `agy models`
  smoke check, the display-string model-name caveat including that Claude
  models are reachable through agy, the stdin-pipe-hang warning, and the
  version-pin caveat) and lead with the gemini-discontinuation story
  (updated implementation-status wording: Phase 1 complete, all four
  backends). The design doc `docs/designs/external-agents-adapter.md` gets
  a new appendix §11.5 recording the gemini-to-antigravity delta (body
  text left as the historical record); its header status line now reads
  Phase 1 (1a/1b/1c/1d) complete. `examples/providers-agent-cli.yaml`
  replaces the Phase 1c gemini preview block with a documented (still
  commented-out) antigravity block, with the now-empty
  "unimplemented preview" framing removed since nothing remains
  unimplemented.

## [v2.7.9] — 2026-07-11 (agent_cli Phase 1b: codex)

Adds the third target to the `agent_cli` adapter: `agent: codex` (the OpenAI
Codex CLI) is now implemented alongside `claude` (Phase 1a) and `grok`
(Phase 1d). The implementation was verified against the real CLI (codex-cli
0.144.1, 2026-07-11), and one design-time assumption from the 2026-07
research did not survive contact with it: `codex exec` has no approval flag
at all (`-a`/`--ask-for-approval` doesn't exist in `exec --help` on
0.144.1, since non-interactive execution has no approval prompt to control
in the first place), so the design's separate `edit`/`full_auto` mappings
(`-a on-request` / `-a never`) collapse into a single `-s workspace-write`
mapping — unlike claude/grok, codex does not distinguish `edit` from
`full_auto`. The verified JSONL `turn.completed.usage` payload also carries
a field the design didn't record, `reasoning_output_tokens`, which is now
normalized defensively into `completion_tokens_details.reasoning_tokens`.
Everything else (stdin prompt delivery, `--skip-git-repo-check`,
`--ephemeral`, the absence of `--max-turns`/`--timeout`, the ~8-day OAuth
staleness) matched the design closely. `gemini` remains schema-declared but
rejected (Phase 1c pending).

### Added

- **`agent: codex` builder/parser in `AgentCliAdapter`**
  (`coderouter/adapters/agent_cli.py`) — invokes the Codex CLI one-shot as
  `codex exec --json --skip-git-repo-check --ephemeral -m <model>
  -C <workdir> -s read-only -` (read_only default). The prompt is delivered
  on stdin with an explicit trailing `-` sentinel — the same channel as
  claude, unlike grok's `--prompt-file` delivery. Output is JSONL on
  stdout (progress goes to stderr); the final answer is the `item.text` of
  the **last** `item.completed` event whose `item.type=="agent_message"`.
  Usage comes from `turn.completed.usage` and is normalized as
  `prompt_tokens=input_tokens` / `completion_tokens=output_tokens`, with
  `cached_input_tokens` kept as `prompt_tokens_details.cached_tokens` (a
  subset of `input_tokens`, not added on top — verified 13810 ⊃ 9984) and
  `reasoning_output_tokens` kept as
  `completion_tokens_details.reasoning_tokens` when nonzero; multiple
  `turn.completed` events, if seen, are summed. `thread_id` (from
  `thread.started`) surfaces as `coderouter_session_id`. Parsing is
  defensive line-by-line — a non-JSON line doesn't abort the rest of the
  stream, but no valid `agent_message` (or empty stdout) raises a
  retryable `AdapterError`, as does any `error` event or `turn.failed`.
  The CLI is pre-1.0 and `--json`'s alias is still `--experimental-json`
  (schema not frozen), so version pinning is recommended alongside the
  defensive parsing.
- **`sandbox_mode` → codex flag mapping** — `read_only` → `-s read-only`;
  `edit` → `-s workspace-write`; `full_auto` → `-s workspace-write` (same
  as `edit` — `exec` has no approval flag in 0.144.1, and
  `--dangerously-bypass-approvals-and-sandbox` is never used). As with
  claude/grok, the effective mode is clamped to read-only whenever
  `allow_file_writes=false`, regardless of `sandbox_mode`.
- **`--skip-git-repo-check` always passed** — CodeRouter's isolated workdir
  is never a git repository, and codex's default "trusted directory" check
  fails immediately outside one (exit 1, stderr `Not inside a trusted
  directory and --skip-git-repo-check was not specified.` — field-verified).
  The adapter always passes this flag, so that error should never surface
  in normal operation.
- **`--ephemeral` always passed** — prevents the session from persisting to
  disk, for the same stateless-transformation rationale as grok's
  `--no-memory`. Not configurable.
- **`max_turns` ignored for codex** — `codex exec` has neither
  `--max-turns` nor `--timeout`; `AgentCliConfig.max_turns` has no effect
  when `agent: codex`, and `exec_timeout_s` + process-group `SIGKILL` is
  the only time bound.
- **`coderouter_session_id` via `thread_id`** — same response-metadata
  treatment as claude's `session_id` and grok's `sessionId`.

### Changed

- **Unsupported-agent error message** — constructing the adapter with a
  not-yet-implemented agent now lists all three implemented agents (e.g.
  `agent 'gemini' is not implemented yet (implemented: claude, codex,
  grok)`) instead of the two-agent wording from v2.7.8.
- **Schema docstrings** (`coderouter/config/schemas.py`) — `AgentCliConfig`
  field descriptions updated for the three-agent reality, including the
  `max_turns` docstring noting codex ignores it.
- **Docs & examples** — `docs/backends/external-agents.md`/`.en.md` gain a
  full codex section (config example, adapter argv, stdin-vs-`--prompt-file`
  contrast with grok, sandbox mapping table with the no-approval-flag-in-exec
  note, JSONL schema and usage normalization including
  `reasoning_output_tokens`, OAuth setup with the ~8-day staleness caveat
  and `CODEX_API_KEY`/`OPENAI_API_KEY`/`CODEX_HOME`, and the pre-1.0
  version-pin caveat) and updated implementation-status wording (claude +
  codex + grok implemented; gemini-only rejected). The design doc
  `docs/designs/external-agents-adapter.md` gets a new appendix §11.4
  recording the verified deltas between the 2026-07 research and
  codex-cli 0.144.1 (body text left as the historical record); its header
  status line now lists Phase 1b alongside 1a/1d.
  `examples/providers-agent-cli.yaml` promotes codex out of the
  not-implemented preview into a documented (still commented-out, so
  copying the file doesn't require codex to be installed) block with
  `model: gpt-5.5`, `paid: false` for subscription OAuth, and
  `passthrough_env: []`; the preview section at the bottom is retitled to
  Phase 1c (gemini only), with codex removed from it.

## [v2.7.8] — 2026-07-10 (agent_cli Phase 1d: grok)

**PR #68**. Adds the second target to the `agent_cli` adapter: `agent: grok` (the xAI
Grok CLI) is now implemented alongside `claude` from Phase 1a. The
implementation was verified against the real CLI (grok v0.2.93, [stable]
channel, 2026-07-10), and several design-time assumptions from the 2026-07
research did not survive contact with it: the CLI now supports OAuth
subscription login (the design assumed grok was API-key-only with no
subscription path), `--sandbox` takes a profile value
(`off|workspace|read-only|strict`) rather than being a bare flag, a
Claude-Code-compatible `--permission-mode` exists, and `-p`/`--single` only
accepts the prompt as an argv value (stdin is not accepted as the prompt) —
so the adapter delivers the prompt via a temp file instead. The grok CLI is
still early beta: version pinning is recommended, and the JSON output is
parsed defensively so schema churn degrades into retryable fallback rather
than hard failure. `codex` / `gemini` remain schema-declared but rejected
(Phase 1b/1c pending).

### Added

- **`agent: grok` builder/parser in `AgentCliAdapter`**
  (`coderouter/adapters/agent_cli.py`) — invokes the Grok CLI one-shot as
  `grok --prompt-file <workdir>/.coderouter-prompt-<uuid>.txt
  --output-format json -m <model> --cwd <workdir> --max-turns <N>
  --no-memory --sandbox read-only --permission-mode plan` (read_only
  default). Auth is subscription-first, same as claude: the CLI's OAuth
  login (`grok login`, SuperGrok / X Premium+) stores credentials at
  `~/.grok/auth.json` (7-day expiry, auto-refresh; `GROK_HOME` overrides),
  which the adapter's `HOME` inheritance reaches with
  `passthrough_env: []`. For CI/API-key use the CLI reads
  `GROK_CODE_XAI_API_KEY` (not `XAI_API_KEY`) — list it in
  `passthrough_env` only if needed; it takes precedence over OAuth.
- **`sandbox_mode` → grok flag mapping** — `read_only` →
  `--sandbox read-only --permission-mode plan`; `edit` →
  `--sandbox workspace --permission-mode acceptEdits`; `full_auto` →
  `--sandbox workspace --always-approve`. As with claude, the effective
  mode is clamped to read-only whenever `allow_file_writes=false`,
  regardless of `sandbox_mode` (defense in depth). Unlike claude,
  `full_auto` maps distinctly from `edit`.
- **Prompt delivery via `--prompt-file`** — grok's `-p`/`--single` requires
  the prompt as an argv VALUE (verified on the real CLI: stdin is not
  accepted as the prompt), and putting huge prompts on argv would hit
  Linux's `MAX_ARG_STRLEN` (~128KiB) and expose the full prompt text to
  `ps`. The adapter instead writes the prompt to a mode-`0600` temp file
  inside the isolated workdir and passes `--prompt-file`; the file is
  always deleted afterwards, including on the timeout and error paths.
- **`--no-memory` always passed** — grok has a cross-session memory
  feature; disabling it unconditionally preserves the
  "one request = one stateless transformation" ethos (a previous call's
  memory can never leak into the next response). Not configurable.
- **grok JSON parsing + zero-usage reporting** — `--output-format json`
  emits a single JSON object `{"text", "stopReason", "sessionId",
  "requestId", "thought"?}` (verified on grok v0.2.93). `text` becomes the
  final answer; `sessionId` surfaces as the `coderouter_session_id`
  response metadata. There are NO token-usage or cost fields, so usage is
  reported as zeros and `coderouter_cost_usd` stays 0 unless the operator
  sets unit prices in `ProviderConfig.cost` (in contrast to claude's
  direct `total_cost_usd`). Parsing is defensive: anything malformed
  raises a retryable `AdapterError` and the fallback chain advances.
  Runtime errors (exit 1 with the error text on stderr) surface a stderr
  tail in the `AdapterError` message.

### Changed

- **Unsupported-agent error message** — constructing the adapter with a
  not-yet-implemented agent now lists what IS implemented (e.g.
  `agent 'codex' is not implemented yet (implemented: claude, grok)`)
  instead of the Phase-1a-specific "claude only" wording. Still
  `retryable=False` — a configuration error, not a fallback candidate.
- **Schema docstrings** (`coderouter/config/schemas.py`) — `AgentCliConfig`
  field descriptions updated for the two-agent reality; the design-time
  "grok requires `XAI_API_KEY` in `passthrough_env`" validation note was
  dropped as obsolete (grok now supports subscription OAuth).
- **Docs & examples** — `docs/backends/external-agents.md`/`.en.md` gain a
  full grok section (config example, adapter argv, sandbox mapping table,
  prompt-file rationale, `--no-memory` rationale, JSON schema and
  zero-usage note, OAuth setup steps, early-beta/version-pin caveat) and
  updated implementation-status wording. The design doc
  `docs/designs/external-agents-adapter.md` gets a new appendix §11.3
  recording the verified deltas between the 2026-07 research and grok
  v0.2.93 (body text left as the historical record).
  `examples/providers-agent-cli.yaml` promotes grok out of the
  not-implemented preview into a documented (still commented-out, so
  copying the file doesn't require grok to be installed) block with the
  corrected model (`grok-4.5` — `grok-code-fast-1` was retired
  2026-05-15), `paid: false` for subscription OAuth, and
  `passthrough_env: []`.

## [v2.7.7] — 2026-07-10 (External coding agents: agent_cli adapter, Phase 1a claude)

**PR #62 / #63**. Adds a new adapter kind, `agent_cli`, that lets CodeRouter front an external
coding-agent CLI (Claude Code / Codex / Gemini / Grok) as a single one-shot
`prompt in → text out` transformation, keeping the "one request = one
stateless transformation" ethos intact even though the underlying CLI is a
stateful, multi-turn, filesystem-editing control loop. Phase 1a implements
only the `claude` target (Claude Code CLI) — the most stable of the four and
the only one that emits `total_cost_usd` directly, making it the reference
implementation for the parser and cost path. `codex` / `gemini` / `grok` are
declared in the config schema for forward compatibility but rejected with a
clear error until their phase lands (Phase 1b-1d).

### Added

- **`kind: "agent_cli"` + `AgentCliAdapter`** (`coderouter/adapters/agent_cli.py`,
  new) — invokes the Claude Code CLI one-shot via
  `claude -p --output-format json` (prompt fed on stdin, never on argv, so
  it is never subject to shell interpretation; `shell=True` is never used).
  Key behaviors:
  - Subscription-first OAuth: the child process does NOT inherit the parent
    environment. A minimal env is built explicitly (fixed safe `PATH`,
    `NO_COLOR=1`, `TERM=dumb`, inherited `HOME` for credential-dir
    discovery); `ANTHROPIC_API_KEY` is never forwarded unless the operator
    explicitly lists it in `passthrough_env` (prevents a stray key from
    silently overriding subscription auth). `--bare` is deliberately never
    added — it would skip OAuth/keychain reads.
  - `CODEROUTER_AGENT_DEPTH` recursion guard: the depth is read from the
    environment, incremented, and propagated into the child; `generate()`
    refuses with a non-retryable error once the current depth reaches
    `agent_depth_limit` (default 2).
  - `exec_timeout_s` is enforced independently of `ProviderConfig.timeout_s`
    via `asyncio.wait_for`; on expiry the whole child process *group* is
    `SIGKILL`ed (`start_new_session=True` + `os.killpg`) so the CLI's real
    LLM-call subprocess is never orphaned.
  - Default `read_only` sandbox: `allow_file_writes=False` /
    `sandbox_mode="read_only"` map to claude's `--permission-mode plan`.
    The permission-mode mapping is clamped to read-only whenever
    `allow_file_writes` is False, regardless of `sandbox_mode` (defense in
    depth). `edit` / `full_auto` both map to `acceptEdits`.
  - Pseudo-streaming: no CLI in Phase 1a exposes a stable token stream, so
    `stream()` runs `generate()` once and chunks the final answer into
    content `StreamChunk`s followed by a terminal `finish_reason="stop"`
    chunk carrying usage.
  - `total_cost_usd` (claude's JSON output) propagates to the response as
    the `coderouter_cost_usd` metadata field, feeding the cost dashboard;
    `session_id` similarly surfaces as `coderouter_session_id`.
- **`AgentCliConfig` schema** (`coderouter/config/schemas.py`) — new
  `ProviderConfig.agent_cli` sub-config (`agent`, `command`, `workdir`,
  `exec_timeout_s`, `allow_file_writes`, `sandbox_mode`, `model`,
  `max_turns`, `passthrough_env`, `agent_depth_limit`), `extra="forbid"`
  like the rest of the module. `ProviderConfig.base_url` is relaxed to
  optional so `agent_cli` providers (which shell out to a local CLI rather
  than calling a URL) can omit it; a `model_validator` restores the
  required-ness of `base_url` for `openai_compat` / `anthropic` and
  requires the `agent_cli` sub-config whenever `kind == "agent_cli"`.
- **30 new tests** (`tests/test_agent_cli.py`) covering argv construction
  (model/max-turns/permission-mode mapping and the write-clamp), claude JSON
  parsing (success / `is_error` / malformed / missing-field paths), a
  stubbed-subprocess `generate()` path, timeout → process-group kill,
  child-env isolation (no `ANTHROPIC_API_KEY` leak, `passthrough_env`
  allowlist, depth increment), the recursion depth limit, and a TestClient
  end-to-end run through `POST /v1/chat/completions`.
- New example config: `examples/providers-agent-cli.yaml` — a runnable
  single-provider `agent_cli` setup for `claude`, plus a commented-out
  preview of the future `codex` / `gemini` / `grok` provider blocks.
- Design doc: `docs/designs/external-agents-adapter.md` — the full Phase 1
  design (architecture, security requirements, per-CLI argv/JSON tables,
  auth policy, test plan). `codex` / `gemini` / `grok` remain Phase
  1b-1d — configuring `agent_cli.agent` to any of them is accepted at
  config-load time but rejected with a clear, non-retryable error
  (`"agent 'codex' is not implemented in Phase 1a (claude only)"`) when the
  adapter is constructed.

---

### Fixed

- **Paid gate blocked the example config** (PR #63): the `ALLOW_PAID` env var
  overrides the yaml `allow_paid`, so the example's `allow_paid: true` was
  silently defeated. Subscription-OAuth claude has zero incremental cost —
  the example now marks the provider `paid: false` (grok stays `paid: true`,
  API-metered).
- **`Not logged in` under the minimal child env** (PR #63): on macOS the
  Claude Code CLI resolves its Keychain credential via `USER`; the child env
  now inherits `USER`/`LOGNAME` alongside `HOME`. Failures reported as an
  `is_error` result JSON on stdout (exit 1, empty stderr) now surface the
  actual `result` text in provider-failed logs instead of an empty tail.

## [v2.7.6] — 2026-07-09 (Launcher MTP / speculative-decoding support)

**PR #59 / #60**. Adds Multi-Token Prediction (MTP) / speculative-decoding
support to the llama.cpp launcher (both the Web UI and the desktop GUI), closing the
gap where operators had to hand-assemble `--spec-type` / `--model-draft`
flags. Fully backward compatible: the new fields default to auto-detection
with a silent no-op fallback, no config-schema changes, no new dependencies.

### Added

- **`resolve_speculative()` / `find_draft_companion()`**
  (`coderouter/launcher_speculative.py`) — shared decision logic for both
  launchers. `mtp_mode="auto"` (default) first checks the selected gguf's
  own metadata for embedded nextn layers (`{arch}.nextn_predict_layers > 0`)
  and, if present, emits `--spec-type draft-mtp` with no separate draft
  model; otherwise it scans the *same folder* as the main gguf for a
  companion draft/MTP file (name hint or shared prefix, size under 50% of
  the main model, architecture-matched when readable) and wires up
  `--spec-type draft-mtp|draft-simple --model-draft <path>`; otherwise it
  starts normally and logs that no companion was found. An explicit
  `draft_model_path` skips detection entirely (400 if the path doesn't
  exist); `mtp_mode="off"` never emits speculative flags. When the operator
  supplies `--spec-type` in extra args, auto-detection defers to it and
  adds nothing.
  Both knobs are llama.cpp-only — vllm/mlx reject them with a 400.
- **`POST /api/launcher/start`**: new optional `draft_model_path` and
  `mtp_mode` (`"auto"` / `"off"`) fields; the response now carries the
  resolved flags as `"speculative"`. `-md` / `--model-draft` /
  `--spec-draft-model` join the existing `-m`/`--model` denylist for
  `options`/`extra_args` (llama.cpp) — the draft model can only be set via
  `draft_model_path`. The remaining spec knobs (`--spec-type`,
  `--spec-draft-n-max/-n-min/-p-min`, `-ngld`, `-devd`) stay free-form.
- **GGUF introspection** (`coderouter/gguf_introspect.py`): `GGUFInfo` now
  reports `n_nextn` and the derived `supports_mtp` property, read from
  `{arch}.nextn_predict_layers`.
- **`--split-mode tensor` crash warning**: known llama.cpp issue #24309 —
  a nextn-embedded model combined with tensor split can crash. The launcher
  detects the combination and logs a warning recommending `--split-mode
  layer` without blocking the launch.
- **UI**: Web LAUNCH form gets "MTP/draft gguf (空欄で自動検出)" text field
  + "MTP" `auto`/`off` select. Desktop GUI (`launcher_gui.py`) gets the
  matching Entry + a "MTP自動検出" checkbox (default on).
- **Automatic MTP startup-crash fallback**: when speculative flags were added
  by AUTO detection (`mtp_mode="auto"`, no explicit `draft_model_path`) and the
  backend dies during startup (non-zero exit within ~3 min), the launcher
  relaunches it ONCE without the speculative flags and logs
  `[launcher] MTP startup failure detected (exit code ...); retrying without
  speculative decoding` plus the flag-free command. This covers architectures
  whose `draft-mtp` support in llama.cpp is still immature (detection is right
  but the MTP context fails to initialize, e.g. `failed to measure MTP context
  memory`). Explicit `draft_model_path` / operator-supplied `--spec-type` are
  never auto-retried, and the retry can never loop (single-shot, guarded).
  Implemented in both the Web launcher (`launcher_routes.py`) and the desktop
  GUI (`launcher_gui.py`).

### Docs

- `docs/backends/launcher.md`/`.en.md`: new "MTP / speculative decoding"
  section (auto-detection order, companion-file convention, `mtp_mode=off`,
  extra-args deferral, `-md` denylist, issue #24309 caveat, API fields), plus
  the LAUNCH form field table and denylist note updated.
- `docs/backends/llamacpp-direct.md`/`.en.md`: manual `--spec-type
  draft-mtp` / `--model-draft` / `--spec-draft-n-max` usage example, with a
  pointer to the launcher's auto-detection.
- `docs/backends/launcher-quickstart.md`/`.en.md`: one-line pointer to the
  new MTP section.

---

## [v2.7.5] — 2026-07-06 (External-bind startup warning + remote-access guide + docs i18n)

Driven by the first field report against the v2.7.0 Host-validation
guard: a user upgrading 2.6 → 2.7 found `serve --host 0.0.0.0` silently
403-ing every LAN client (**PR #54**), plus the day's documentation
overhaul (**PR #51 / #52**). No config-schema changes, no new
dependencies.

### Added

- **Startup warning for the loopback trap.** `serve` now prints a
  stderr warning (before uvicorn takes the console) when binding beyond
  loopback while `CODEROUTER_ALLOWED_HOSTS` is unset/blank — the exact
  combination where every LAN request gets a 403 from the DNS-rebinding
  guard with no hint. The message names the env var, states that the
  value is **this server's address as it appears in the client's URL
  bar (not the client's IP** — the first field tester got this wrong),
  and carries the security caveat (chat endpoints are unauthenticated).
  Loopback binds and configured exposures stay silent. 10 new tests.
  (PR #54)
- **Remote-access guide** (`docs/guides/remote-access.md` + `.en.md`):
  the trust boundary made explicit (ALLOWED_HOSTS is Host validation,
  not authentication), then four concrete exposure patterns with
  configs — SSH tunnel (stays loopback), Tailscale (bind the tailnet
  IP, never `0.0.0.0`), authenticating reverse proxy (Caddy example),
  raw LAN + firewall for fully trusted home networks only. Cross-linked
  from troubleshooting §1-6 (new symptom entry with the exact
  `Host '...' is not allowed.` string) and both READMEs. (PR #54)

### Docs

- **Implementation-accuracy audit** across the living docs (PR #51/#52):
  Memory Pressure guard description corrected (OOM → cooldown exclusion
  → chain fall-through; not "switch to a lighter model"), drift is 6
  signals, doctor is 7 probes (all occurrences incl. sample outputs),
  broken CHANGELOG links in quickstart fixed.
- **Full JA/EN doc pairs**: 13 English versions added (concepts ×5,
  backends ×5, docs index, low-memory integration, remote access) with
  reciprocal interlinks — every living doc now exists in both languages.
- **CHANGELOG unified to English**: 55 historical Japanese entries
  translated in place (versions/dates/PR numbers machine-verified
  unchanged; quoted Japanese examples kept verbatim).
- Volatile hardcoded counters swept: test counts now rounded ("1,500+"),
  stale "964 tests" / "453 tests" removed; design constants (5 deps /
  6 guards / 7 probes / 6 signals) stay literal.

---

## [v2.7.4] — 2026-07-06 (Launcher provider auto-sync + /v1/models passthrough)

Two PRs closing the "launcher started it, but nothing can route to it /
nothing can *name* it" gap, driven by a real field failure (launcher on
port 8085, providers.yaml pointing at 8080; external benchmark reports
indistinguishable across loaded GGUFs). **PR #48** (models passthrough)
and **PR #49** (launcher auto-sync). Fully backward compatible, no new
dependencies. Verified end-to-end on real hardware (NucBox EVO-X2,
llama.cpp + omnicoder-9b): zero-config launch → route via
`X-CodeRouter-Profile: launcher` → response carries the real GGUF name.

### Added

- **`/v1/models` upstream passthrough** for empty-model providers.
  A provider with `model: ""` (the launcher/llama-server pattern — the
  upstream decides what is loaded) used to be listed by its static
  provider name only. Now `/v1/models` queries the upstream's own
  `/models` (2s timeout, 30s TTL cache) and surfaces the real loaded
  model id(s) as `owned_by: coderouter/<provider>`. Any failure falls
  back to the historic provider-name entry; providers with a configured
  model keep the exact previous shape. External benchmarks (e.g.
  SWE-bench harnesses with `model: auto`) can now tell GGUF swaps apart
  without config edits. (PR #48)
- **Launcher provider auto-sync.** A backend started from the embedded
  launcher (`/api/launcher/start`) is auto-registered as a routable
  provider named `launcher-<backend>-<port>` (restart on the same port
  replaces, never duplicates) and placed at the FRONT of an on-demand
  `launcher` profile. `default_profile` is never touched — routing is an
  explicit opt-in via `X-CodeRouter-Profile: launcher` or body
  `profile`. Registration is deliberately **in-memory only** (a YAML
  rewrite would destroy operator comments; provider and process share
  the server's lifetime). Sync failures never fail the start; the
  result rides on the start response as `provider_sync`. New engine
  API: `FallbackEngine.register_provider()`. (PR #49)

### Docs

- README (JA/EN) launcher feature table + `docs/backends/launcher.md`:
  auto-sync semantics, verification curl recipes, and the desktop-GUI
  caveat (launcher_gui.py runs out-of-process and is not synced).

---

## [v2.7.3] — 2026-07-05 (Per-request empty-response fallback + L3 bench evidence)

Two PRs: **PR #45** (`empty_response_action` — the fallback leg of the
three-tier tool-call reliability story) and **PR #46** (L3 benchmark
refresh: model matrix, repairer-version A/B archives, results). Fully
backward compatible: new knob defaults to `off`, no breaking config
changes, no new dependencies.

### Added

- **`FallbackChain.empty_response_action: off | warn | fallback`**
  (default `off`). With `fallback`, a 200 response that is *content-empty*
  (no `tool_use`, no non-whitespace text; thinking-only counts as empty) is
  re-dispatched in-flight to the next provider in the chain. Non-streaming
  responses are judged on the finalized object; streaming buffers events
  until the first real content token and discards an unobserved stream.
  Emptiness is judged on content, not `usage.output_tokens` (unreliable on
  some backends). Motivation: gemma4:26b returns blank 200s on
  `no_tool_temptation`-type prompts (20/20 at temperature 0, direct AND via
  router) — no text exists for the repair layer to fix, and the windowed
  drift guard `empty_response_rate` cannot rescue a single blank turn
  in-flight. 21 new tests. (PR #45)
- **Benchmark refresh**: full L3 model matrix in
  `benchmarks/tool-repair/providers.bench.yaml` (llama3.2/3.1, mistral,
  qwen2.5-coder:1.5b, phi4-mini + originals, `bench-gemma-chain` enabled),
  one-shot matrix runner `bench_l3_p1.sh`, latest live results plus
  repairer-version A/B snapshots under `results/archive-v2.7.{0,1}/`.
  (PR #46)

### Results

- Live chain validation (M3 Max, temperature 0, 100 requests, zero
  errors): gemma4:26b → qwen3-coder:30b chain **80% → 100%** native —
  all 20 blank turns rescued.
- Three-tier story now fully measured: repair rescues broken-but-present
  calls (qwen2.5×2 / mistral 0→100, phi4 +60pt), healthy models pass
  through undegraded, and fallback closes what repair cannot touch.

---

## [v2.7.2] — 2026-07-05 (R4 tool-call repair: nested-XML / JSON-envelope / call-syntax forms)

Single feature PR **#43**, driven by the 2026-07-05 L3 live benchmark
(6 local models × 2 wire paths × repairer-version A/B on an M3 Max). Closes
the three text-tool-call gap classes the bench measured in the wild. Fully
backward compatible: no config-schema changes, no signature changes,
no new dependencies (repairer stays stdlib-only).

### Added

- **R4a — nested-XML name-attribute forms.** The tool name lives in a
  `name` attribute rather than the tag itself:
  `<tools><function name="echo" arguments='{...}'/></tools>`, bare
  `<function .../>`, `<tool ... args='{...}'/>`. Fixed known tag set;
  `name` must be allow-listed; attribute values are delegated to the R1
  lenient-JSON pipe. (PR #43)
- **R4b — JSON envelope forms.** Models sometimes echo the response wrapper
  verbatim into the text body: `{"tool_calls": [...]}` and legacy
  `{"function_call": {"name": ..., "arguments": "<JSON string>"}}` (string
  arguments double-parsed). All-or-nothing allow-list validation: one
  non-allow-listed inner call rejects the whole envelope. (PR #43)
- **R4c — call-syntax family.** One extractor for the "name + parens + args"
  idiom observed independently across three model families (Gemma, Mistral,
  phi4): `print(default_api.echo(message="probe"))`, `echo(message="probe")`,
  `echo(message: 'demo')` (colon kwargs), `write_note({JSON})`. Recognised
  only inside a fence or on a standalone line, never inline in prose. The
  argument list must parse completely — a corrupted inner JSON is left
  alone rather than executed with broken args. (PR #43)
- **50 new unit tests** (`tests/test_tool_repair_r4.py`) and **29 new bench
  corpus cases** (55 → 84), including 10 adversarial-review counterexamples
  locked in as negatives (blank-line cue variants among them). (PR #43)

### Changed

- **Prose-cue false-positive guard hardened** after two adversarial review
  rounds: expanded cue vocabulary (`as follows` / `convention` / `format` /
  `sample` / `payload` / ...), 200-char window with whole-previous-line
  matching (two blank-line window bugs fixed), and a colon-ended lead-in
  rule (full-width `：` U+FF1A included). The colon rule deliberately
  over-suppresses genuine "I'll call it now:" lead-ins — FP-zero-first is
  documented in the module docstring as a design decision. (PR #43)

### Results

- L1 offline bench: recall **78.0% → 100%** (59/59), false positives
  **0/25** maintained, existing 70 cases regression-free.
- L2/L3 live (temperature 0, 100 requests per path, zero errors):
  mistral:7b via router **80% → 100%** (third 0→100 model after
  qwen2.5-coder 1.5b/7b); phi4-mini **40% → 80%**, with the remaining 20%
  being exactly the semantically-corrupted case the repairer refuses by
  design.

---

## [v2.7.1] — 2026-07-04 (Native-endpoint shims + benchmarked tool-repair upgrade)

Two feature PRs driven by the 2026-07-04 competitive re-survey and the new
tool-call repair benchmark: **PR #39** (native `/v1/messages` endpoint gap
shims) and **PR #40** (lenient tool-call repair). All new behavior is
**opt-in with `off` defaults** (except the previously-404 `count_tokens`
endpoint, which is additive), no config-schema breaking changes, and the
Core runtime stays at **5 dependencies**.

### Added

- **`POST /v1/messages/count_tokens`** (was 404). Anthropic-shape
  `{"input_tokens": N}`; accurate when the provider declares
  `tokenizer_path`, char/4 heuristic otherwise. Fills a gap native backends
  leave open (Ollama's Anthropic-compatible API ships without
  `count_tokens` / `tool_choice` / prompt caching). (PR #39)
- **`tool_choice_action: off | warn | emulate`** (profile option) +
  `Capabilities.tool_choice` + registry field + `provider_supports_tool_choice()`.
  `emulate` rewrites forced tool_choice (`any` / `tool`) into a system-prompt
  instruction for non-supporting backends; the original request is preserved
  for fallback to capable providers. New capability-degraded reason:
  `unsupported-backend`. (PR #39)
- **`cache_control_action: off | strip`** (profile option). `strip` removes
  `cache_control` markers before sending to non-supporting providers
  (logged as `cache-control-stripped`; deliberately no tokens-saved
  accounting). (PR #39)
- **Lenient tool-call repair** (`translation/tool_repair.py`): second-pass
  parsing for malformed JSON (double braces, trailing commas, single quotes,
  unquoted keys), key aliases (`tool`/`tool_name`, `parameters`/`input`/`args`),
  and XML-flavoured forms — all gated on `allowed_tool_names`. Residue
  cleanup (empty fences, `[,]`). Benchmark: recall 80.6% → **100%** on a
  55-case corpus; live fail-rate at default temperature 17% → **1%**
  (qwen2.5-coder:7b × 100 requests). (PR #40)

### Fixed

- **Two real false positives in the previous repairer** (python-fence dict
  literals and documentation JSON with allowed tool names were converted
  into executable calls — same class as the v2.7.0 code-eating regression).
  Now fixed and gated by an expanded 12-case negative corpus. (PR #40)

### Docs

- README repositioned for the native `/v1/messages` era: repair + guards
  lead, wire translation demoted to last (passthrough note); new section
  "why a router when Ollama connects directly?".

Tests: **1481 passed, 0 failed** (1401 → 1481; +37 shim, +43 lenient-repair).

---

## [v2.7.0] — 2026-07-02 (Reliability & security: full-source review fixes)

22 reliability and security fixes from a full-source review (26,600 lines),
landed as two sequential PRs: high-priority **PR #34** (H1–H8) and
medium-priority **PR #35** (M1–M14), both merged to `main`. No API or
config-schema breaking changes. Backward compatibility highlights: the new
launcher token auth is **opt-in** (`CODEROUTER_LAUNCHER_TOKEN` unset ⇒
unchanged behavior + a startup warning), and the Host-validation middleware
allows loopback by default with `CODEROUTER_ALLOWED_HOSTS` to extend it.

### Fixed

- **`/metrics` 500 when drift counters are non-zero** — three malformed
  Prometheus label tuples (`(((),), v)` → `((), v)`) (`coderouter/metrics/prometheus.py`, PR #34/H1).
- **`tool_repair` deleted unrelated fenced code blocks** while stripping
  tool-call-shaped ones, losing user content
  (`coderouter/translation/tool_repair.py`, PR #34/H2).
- **Probe URL mismatch (`/v1/v1` duplication)** between the continuous probe
  and the adapters caused healthy backends to be wrongly demoted; URL
  normalization is now shared
  (`coderouter/adapters/anthropic_native.py`, `coderouter/guards/continuous_probe.py`, PR #34/H4).
- **`context_budget` dropped entire history at once** on threshold breach;
  trimming is now sequential per tool-pair with re-estimation after each
  step, and the new head turn is renormalized to a plain user turn
  (`coderouter/guards/context_budget.py`, PR #34/H5).
- **Empty-stream termination violated the SSE protocol** by sending a
  terminator with no preceding `message_start`; one is now synthesized
  first (`coderouter/translation/convert.py`, PR #34/H6).
- **Excluded providers never recovered after a restart** — restored
  self-healing state didn't re-arm recovery probes (only the live
  UNHEALTHY-transition path did); probes are now re-spawned after state
  restore, queued if the event loop isn't running yet
  (`coderouter/routing/fallback.py`, `coderouter/guards/self_healing.py`, PR #34/H7).
- **Malformed upstream responses bypassed retry/fallback** — an uncaught
  pydantic `ValidationError` on missing/misshaped fields now converts to a
  retryable `AdapterError` on non-stream paths, and stream paths skip the
  malformed chunk with a once-per-stream warning
  (`coderouter/adapters/anthropic_native.py`, `coderouter/adapters/openai_compat.py`, PR #35/M6).
- **`"oom"` substring matched inside `"room"` / `"zoom"` / `"bloom"`** in
  upstream error bodies, cooling down healthy providers; the memory-pressure
  guard now matches the token on word boundaries
  (`coderouter/guards/memory_pressure.py`, PR #35/M10).
- **Interleaved text/tool-call deltas sent `input_json_delta` to
  already-closed block indices** — blocks are now reopened at a fresh index
  with their original `id`/`name` preserved
  (`coderouter/translation/convert.py`, PR #35/M7).
- **`tool_result.is_error` was dropped in both translation directions** — an
  `"Error: "` content marker now round-trips the flag without
  double-prefixing (`coderouter/translation/convert.py`, PR #35/M8).
- **Streams ending without `message_stop` sent no terminal chunk**, hanging
  OpenAI-compatible clients; a `finish_reason` + usage chunk is now
  synthesized on abnormal termination (mirrors the H6 fix in the other
  direction) (`coderouter/translation/convert.py`, PR #35/M9).
- **`configure_logging` permanently detached the metrics collector** on a
  second `create_app()` call by removing every root handler;
  `install_collector` now re-attaches when missing, and
  `configure_logging` removes only its own marked handlers
  (`coderouter/logging.py`, PR #35/M4).
- **Metrics persistence save/load asymmetry** zeroed language-tax
  accounting on every restart — `save_state` was missing
  `language_tax_usd` / `language_tax_usd_aggregate`, which `load_state`
  expects (`coderouter/metrics/collector.py`, PR #35/M5).

### Changed

- **Config validation now fails fast at load time.** Previously-silent
  misconfiguration is now rejected: profile chains referencing unknown
  provider names, duplicate provider/profile names, inverted thresholds
  (e.g. `context_budget` warn > trim, `trim_target` ≥ `trim_threshold`,
  `recovery_probe_initial_s` > `recovery_probe_max_s`), and
  `has_tools: false` / `has_image: false` matchers (which could never
  match) (`coderouter/config/schemas.py`, PR #35/M13).
- **Drift-based chain demotion now applies regardless of the `adaptive`
  flag.** A dedicated cooldown-based demotion map is applied in
  `_resolve_anthropic_chain` unconditionally; previously demotion only
  affected ordering when `profile.adaptive` was true (and did nothing
  under five samples) (`coderouter/routing/fallback.py`, PR #35/M3).
- **Adaptive routing now observes streaming paths.** `record_attempt` was
  only called from `generate_anthropic`; all four entry points
  (`stream_anthropic` / `stream` / `generate` and non-stream) now record
  first-event latency and outcomes, including Claude Code's default
  streaming path (`coderouter/routing/fallback.py`, PR #35/M2).
- **Drift verdicts are now request-scoped (`ContextVar`)** instead of
  stored on the shared engine, eliminating a race where concurrent
  requests could read another request's drift header or a stale verdict
  during cooldown; the engine attribute remains as a deprecated mirror
  (`coderouter/routing/fallback.py`, PR #35/M1).
- **Chain resolution and context estimation no longer run twice per
  request** — the resolved dispatch is cached per request and reused when
  the request object is unchanged (`coderouter/routing/fallback.py`, PR #35/M11).
- **Request/audit logs are now buffered** (20 records or 2 seconds,
  flushed on close/`atexit`; `flush_every_n=1` restores write-through)
  instead of synchronous per-request writes
  (`coderouter/state/audit_log.py`, `coderouter/state/request_log.py`, PR #35/M12).
- **The metrics collector hot path exits early** for unrecognized events
  before acquiring the lock, and known events are dispatched via a dict
  table instead of a ~30-branch `if`/`elif` chain
  (`coderouter/metrics/collector.py`, PR #35/M12).
- **Adapters now share a single `httpx.AsyncClient`** (lazily created,
  per-call timeout override, closed on shutdown) instead of creating one
  per request, enabling keep-alive/TLS session reuse
  (`coderouter/adapters/base.py`, `coderouter/adapters/openai_compat.py`,
  `coderouter/adapters/anthropic_native.py`, `coderouter/ingress/app.py`, PR #34/H3).

### Security

- **Host-validation middleware** on all routes rejects non-loopback `Host`
  headers as a DNS-rebinding defense; loopback
  (`localhost` / `127.0.0.1` / `[::1]`) is allowed by default and
  `CODEROUTER_ALLOWED_HOSTS` (comma-separated) extends the allowlist
  (`coderouter/ingress/app.py`, PR #34/H8).
- **Opt-in launcher token authentication.** `launcher start/stop/delete`
  require the `X-CodeRouter-Token` header (constant-time compare) when
  `CODEROUTER_LAUNCHER_TOKEN` is set; unset behaves exactly as before, with
  a one-time startup warning (`coderouter/ingress/launcher_routes.py`, PR #34/H8).
- **`-m`/`--model` re-specification rejected** in launcher `extra_args`/
  `options` to block arbitrary model-path injection
  (`coderouter/ingress/launcher_routes.py`, PR #34/H8).
- **Request body size cap** (64 MB default, `CODEROUTER_MAX_BODY_BYTES`
  override) returns 413; SSE responses are unaffected
  (`coderouter/ingress/app.py`, PR #35/M14).
- **`/api/launcher/suggest` path confinement** — resolved paths must now
  fall inside the configured `model_dirs`, closing an arbitrary-path
  existence/size probe (`coderouter/ingress/launcher_routes.py`, PR #35/M14).

### Tests

- Full suite grew from **1263 → 1401 passed** (1 skipped, environment-only)
  across the two PRs: 139 new regression tests in 12 new test files
  (`tests/test_fix_h*.py` ×6, `tests/test_fix_m*.py` ×6). One existing test,
  `tests/test_auto_router.py::test_has_tools_false_rejected_at_load`, was
  flipped to expect `ValidationError` — its own docstring had anticipated
  this once the new config validator (M13) landed; it is the only existing
  test changed by either PR.

---

## [v2.6.1] — 2026-06-28 (Token-savings accounting)

Patch release: surfaces **token-savings accounting** in the metrics layer
and dashboard. The figure is owned by core, so it appears even when no
plugin is installed (trim savings from the context-budget guard), and the
optional `compress` plugin adds to the same total via a neutral
`tokens-saved` log event. **No new core dependency**, **no behavioral
change** to existing paths, and **fully backward compatible** — existing
counters and events are untouched; the new buckets are additive.

### Added

- **Token-savings buckets in `MetricsCollector`**
  (`coderouter/metrics/collector.py`). `tokens_saved_total` plus a
  per-mechanism breakdown (`tokens_saved_by_mechanism`). Two feeds
  aggregate under one schema: `trim` (derived from the existing
  `context-budget-trimmed` event's before/after token estimate) and
  `compress` (the neutral `tokens-saved` event emitted by the compress
  plugin — no core import). Wired through `snapshot()`, `save_state()`,
  `load_state()`, and `reset()`.
- **Dashboard Token Savings tiles**
  (`coderouter/ingress/dashboard_routes.py`). Three tiles — trim /
  compress / total — in the "Cost & Language Tax" panel, zero-filled so a
  fresh or local-only deployment renders cleanly.

### Docs / examples

- Reorganized provider samples under `examples/` with a category index.
- Added the language-tax guide (JA/EN) and a Claude Code +
  llama.cpp/vllm backend guide.

### Tests

- `tests/test_tokens_saved_metric.py` — trim/compress accounting,
  negative-delta clamping, combined aggregate, persistence round-trip,
  and reset. Full suite: **1263 passed, 1 skipped**.

### Companion

- `coderouter-plugin-compress` emits the `tokens-saved` event (plugin
  branch `feat/tokens-saved-emit`, v0.2.0). CodeRouter core works
  standalone — trim savings show without the plugin.

---

## [v2.6.0] — 2026-06-20 (Language Tax: measure, route, visualize)

Minor release: makes the CJK **"language tax"** — cloud tokenizers bill
Japanese/Chinese/Korean text ~1.2–1.5× more tokens per character than
English, while local models are unaffected — measurable, routable, and
visible. Built entirely on existing infrastructure; **no new core
dependency** (the accurate tokenizer is the existing optional `accuracy`
extra), **no network** (local `tokenizer.json` only), and **fully
backward compatible** — the feature is inert until a provider declares
`tokenizer_path`.

### Added

- **Language-tax measurement (`coderouter/language_tax.py`).** A leaf
  module exposing `cjk_char_ratio`, `estimate_language_tax`,
  `LanguageTaxBreakdown`, and `language_tax_usd`. CJK detection is
  stdlib-only (Unicode range checks); the accurate token count is
  delegated to the optional `accuracy` (`tokenizers`) backend with a
  char/4 fallback. The tax multiplier is `tokens_accurate /
  tokens_heuristic` — ~1.0 for English/code, ~2.0–4.0 for pure CJK.

- **End-to-end cost integration.** `CostBreakdown` gains
  `language_tax_multiplier` / `language_tax_usd`;
  `compute_cost_for_attempt` accepts an optional `language_tax=`. Both
  `cache-observed` emit sites in `routing/fallback.py` (streaming +
  non-streaming) build a `LanguageTaxBreakdown` **only when the provider
  declares `tokenizer_path`**, so the hot path is untouched by default.
  The `cache-observed` log line now carries `language_tax_usd` /
  `language_tax_multiplier`, and `MetricsCollector` aggregates per-provider
  + total language-tax spend (mirroring the cost-savings aggregation).

- **`ProviderConfig.tokenizer_path`** — optional path to a local
  `tokenizer.json` for accurate (language-tax) token counting. Local-file
  only; never contacts the HuggingFace Hub. Inert when unset.

- **`cjk_ratio_min` auto-route matcher.** A new `RuleMatcher` variant that
  routes turns whose latest user message CJK ratio ≥ threshold to a
  (typically local, tax-free) profile, while ASCII/code turns fall through
  to the cloud chain. Per-turn property mirroring `code_fence_ratio_min`.

  ```yaml
  auto_router:
    rules:
      - match: { cjk_ratio_min: 0.3 }   # JA-heavy turns → local
        profile: local
      - match: { has_tools: true }
        profile: cloud
    default_rule_profile: cloud
  ```

- **Dashboard "Cost & Language Tax" panel** on `/dashboard`: total spend,
  cache savings, and CJK language-tax spend (aggregate + per-provider).
  Also surfaces the previously-hidden cost aggregates.

- **`token_estimation.extract_text_from_anthropic_request()`** — pulls the
  concatenated request text for the accurate tokenizer leg.

### Security

- **Bump starlette 1.0.1 → 1.3.1**, clearing four advisories
  (CVE-2026-48817 / CVE-2026-48818 / CVE-2026-54282 / CVE-2026-54283) that
  failed the `cve-audit` CI job (`pip-audit --strict`).

### Notes

- 38 new tests (`test_language_tax`, `test_language_tax_integration`,
  `test_auto_router_cjk`, extended dashboard contract). Full suite:
  **1250 passed, 8 skipped**. ruff clean. The 5-deps invariant is intact.

---

## [v2.5.5] — 2026-06-06 (Claude Code >= 2.1.154 `system` role normalization)

Patch release: ingress-side workaround for a Claude Code CLI regression.

### Fixed

- **Claude Code CLI >= 2.1.154 requests no longer 422 at ingress.**
  Claude Code 2.1.154 introduced a regression where it emits messages with
  `role: "system"` (and reportedly `ctx` / `msg`) inside the Anthropic
  `messages` array, which the Messages API spec restricts to
  `user` / `assistant`. CodeRouter's wire validation correctly rejected
  these with `Input should be 'user' or 'assistant'` — breaking every
  request from affected Claude Code versions (2.1.150 and earlier are fine).

  A new `model_validator(mode="before")` on `AnthropicRequest` now
  normalizes such payloads before validation:

  - `role: "system"` → text content merged into the top-level `system`
    field (newline-joined after any existing system prompt; text block
    appended when `system` is a block list).
  - Any other non-spec role (`ctx`, `msg`, ...) → coerced to `user`,
    preserving conversation position (Anthropic merges consecutive
    same-role turns, so this is safe).
  - Messages with no salvageable text content are dropped (Anthropic
    rejects empty turns).
  - A `normalized-nonspec-message-roles` warning is logged whenever
    normalization fires.

  The strict `AnthropicMessage` role enum is **unchanged** — the wire
  model still matches the Anthropic spec, and the native adapter forwards
  a normalized (valid) payload to `api.anthropic.com`, avoiding the same
  400 upstream.

  Verified with 16 new unit tests (`tests/test_role_normalization.py`);
  full suite 1191 passed / 0 failed on py3.12.

  Refs: `anthropics/claude-code#63469`, `anthropics/claude-code#63473`,
  `vllm-project/vllm#44000`

---

## [v2.5.4] — 2026-06-05 (Gemma `<0xNN>` byte-fallback repair filter)

Patch release: a new opt-in output filter that repairs Japanese (and other
multi-byte) text corrupted by the Ollama 0.30 / llama.cpp detokenizer change.

### Added

- **`repair_byte_fallback` output filter.** Ollama 0.30 unified its GGUF
  runtime onto llama.cpp (`ollama/ollama#16031`). For gemma4 the detokenizer
  changed, and multi-byte characters it cannot assemble now leak as
  llama.cpp's byte-fallback notation `<0xNN>`:
  - full-width space `　` → `<0xE3><0x80><0x80>`
  - rare kanji `躙` → `<0xE8><0xBA><0x99>`

  These corrupt Japanese prose **and** tool-call JSON argument strings routed
  through CodeRouter (a stray `<0xNN>` inside an argument breaks JSON parsing).
  The new filter reassembles consecutive `<0xNN>` runs back into UTF-8.

  - **Opt-in** per provider: `output_filters: [repair_byte_fallback]`
    (disabled by default). Place it **before** `tool_repair` / tool-call XML
    strip so byte-fallback inside tool-call arguments is restored before JSON
    extraction.
  - **Streaming-safe**: handles chunk boundaries inside a single token
    (`<0x` | `E3>`) and inside a multi-byte run (`<0xE3>` | `<0x80><0x80>`).
    A pending byte run is only flushed once it has definitively ended.
  - **Lossless**: bytes that cannot form valid UTF-8 are re-emitted verbatim
    as `<0xNN>`, so output is never made worse than llama.cpp already left it.
  - **No new runtime dependencies** (stdlib `re` only).

  Verified with 22 new unit tests (61 filter tests total pass on py3.12),
  ruff clean, a 20,000-iteration streaming chunk-boundary fuzz (0 mismatches),
  and a Japanese/emoji round-trip.

  Ref: <https://note.com/akb428/n/n737e786f32ce>

### Known mitigations (documented)

- For gemma4, staying on / downgrading to Ollama 0.24 is the most reliable
  fix (the root cause is the 0.30 llama.cpp detokenizer swap).
- A larger `num_ctx` increases the leak rate; consider not auto-raising it for
  gemma4 on Ollama 0.30.

---

## [v2.5.2] — 2026-05-22 (Backend-aware Launcher suggestions + backend install guide)

Patch release: a Launcher bug fix and documentation improvements.

### Fixed

- **Launcher "suggest values" (`⚙ 推奨値`) is now backend-aware.**
  Previously the button emitted llama.cpp flags
  (`-ngl` / `--ctx-size` / `--threads`) for every backend, but vLLM and
  MLX reject those. Now:
  - **llama.cpp** — the flags, as before.
  - **vLLM** — empty; `--max-model-len` etc. depend on the model's real
    context length, so the engine's auto-derivation is left to do its job.
  - **MLX** — empty; it assumes unified memory and takes no launch-time
    tuning flags.

  Fixed in both the desktop GUI (`launcher_gui.py`) and the Web launcher
  (`coderouter/ingress/launcher_routes.py`); the `/api/launcher/suggest`
  endpoint now accepts a `backend` parameter.

### Documentation

- New **`docs/backends/install-backends.md`** (+ `.en.md`) — an
  installation guide for llama.cpp / vLLM / MLX covering macOS / Linux /
  Windows, with per-backend verification steps and common pitfalls.
- **Launcher docs consolidated from 3 files to 2**: `launcher-gui.md` is
  merged into a unified `launcher.md` (Web + Desktop GUI in one guide,
  shared reference documented once); `launcher-quickstart.md` is slimmed
  to delegate installation to the new guide.
- **Backend venv convention documented**: vLLM / MLX virtual
  environments live under `~/.coderouter-t/backends/<backend>/`, one venv
  per backend.

---

## [v2.5.1] — 2026-05-22 (MLX backend + docs reorganization)

Patch release: a third Launcher backend, a reorganized documentation
tree, and a security fix.

### Added

- **MLX backend** for the Launcher (`launcher_gui.py` and
  `coderouter/ingress/launcher_routes.py`): `mlx` joins `llama.cpp` and
  `vllm`, aimed at Apple Silicon users. Launches
  `python -m mlx_lm.server --model <m> --port <p>`. The backend
  selectors (desktop GUI combobox / Web `<select>`) gain an `mlx`
  option, and the binary-not-found error messages are now
  backend-agnostic.

### Changed

- **`docs/` reorganized** into role-based folders — `start/`, `guides/`,
  `backends/`, `concepts/` — with a new bilingual (JA/EN) master index
  at `docs/README.md` including a quick "what to read" table. Internal
  cross-links, `README.md` / `README.en.md`, and code/config path
  references were updated to the new layout.
- **`plan.md` restructured**: deduplicated, version ordering fixed,
  sections compressed (1747 → 721 lines).

### Security

- **starlette `1.0.0` → `1.0.1`** (`uv.lock`): fixes PYSEC-2026-161,
  which failed the `cve-audit` CI job.

---

## [v2.5.0] — 2026-05-22 (Launcher — llama.cpp / vllm GUI)

Browser-based process manager for local inference backends, integrated
into the existing CodeRouter web UI at `/launcher`.

### Added

- **`coderouter/ingress/launcher_routes.py`**: New route module providing
  the Launcher UI and its backing API.

  - `GET /launcher` — Single-page HTML UI (Tailwind CDN + inline JS,
    same dark-theme aesthetic as `/dashboard`).
  - `GET /api/launcher/models` — Recursively scans `launcher.model_dirs`
    and returns discovered model files with name, path, size (GB), and
    extension.
  - `GET /api/launcher/option-profiles` — Returns named option presets
    from `providers.yaml` keyed by backend (`llama.cpp`, `vllm`).
  - `GET /api/launcher/processes` — Lists all managed processes.
  - `POST /api/launcher/start` — Starts a backend process. Accepts name,
    backend, model_path, port, options dict, and extra_args free-text.
  - `POST /api/launcher/stop/{id}` — SIGTERM → SIGKILL (5 s timeout).
  - `DELETE /api/launcher/processes/{id}` — Removes a stopped process.
  - `GET /api/launcher/logs/{id}` — Returns last N lines from the
    process's 200-line stdout/stderr ring buffer.

- **`LauncherOptionProfile` / `LauncherConfig`** (`config/schemas.py`):
  New Pydantic models for the `launcher:` block in `providers.yaml`.
  Adding new CLI flags requires only a YAML edit — no code change.

- **`launcher_profiles.yaml.example`**: Template with 7 llama.cpp
  presets and 7 vllm presets. For GitHub distribution and community
  profile contributions.

- **`/dashboard` header**: Added a "Launcher" navigation link.

### Design notes

- **YAML-driven**: option profiles live entirely in `providers.yaml`.
  No code changes needed to add new backend flags.
- **Multi-process**: each launched process gets a UUID-based ID and is
  tracked independently. llama.cpp and vllm can run side by side.
- **Zero new dependencies**: uses `asyncio.create_subprocess_exec`
  (stdlib only). The 5-dep invariant is maintained.
- **In-memory registry**: does not persist across CodeRouter restarts
  (intentional — avoids zombie GPU allocations on restart).

---

## [v2.4.0] — 2026-05-15 (Goal-session awareness — P1-4/5/6)

Stable release following v2.3.0a4. Promotes the Plugin SDK to stable,
adds three goal-session features, and ships a rule-suggestion CLI.

### Added

- **`coderouter/guards/_fingerprint.py`** (P1-4): Response fingerprinting
  helper.  `fingerprint_response(text)` returns a 12-hex SHA-256 digest
  of the top-N content words (stop-word-filtered, order-independent).
  Used by the new `goal_progress_stall` drift signal to detect when a
  model repeats itself without making progress.

- **Signal 6 — `goal_progress_stall`** (`drift_detection.py`, P1-4):
  Sixth drift signal added to `detect_drift()`.  Fires (mild) when the
  fraction of fingerprinted responses that repeat an already-seen
  fingerprint exceeds `repetition_rate_threshold` (default 0.4).
  Requires `response_fingerprint` to be populated on observations; when
  absent the signal is silently skipped (backward-compatible).

- **`DriftThresholds.repetition_rate_threshold`** (P1-4): New field on
  `DriftThresholds`, present on all three presets.  `THRESHOLDS_GOAL`
  preset added (`min_window_fill=4`, `repetition_rate_threshold=0.2`,
  tighter across the board) and exposed via `SENSITIVITY_PRESETS["goal"]`.

- **`FallbackChain.goal_mode: bool = False`** (`config/schemas.py`, P1-5):
  Profile-level flag.  When `True`, the drift detector ignores
  `drift_detection_sensitivity` and uses `THRESHOLDS_GOAL` instead
  (stricter thresholds + `min_window_fill=4`).  Designed for `/goal`
  agent sessions where forward-progress stalls are more actionable.

- **`coderouter/state/suggest_rules.py`** (P1-6): Statistical rule
  suggestion engine.  `suggest_rules(WindowSummary) → list[RuleSuggestion]`
  analyses the request journal and emits copy-paste YAML snippets.
  Five rules: `provider_reorder` (cost rank), `enable_prompt_cache`
  (high-token / low-hit providers), `enable_drift_detection` (reminder),
  `low_sensitivity_small_window` (sparse-traffic guard), `goal_profile`
  (output-divergence → `goal_mode: true`).  Pure statistics — no LLM.

- **`coderouter replay --suggest-rules`** (`cli.py`, P1-6): New flag on
  the existing `replay` subcommand.  Reads the full request journal,
  runs `suggest_rules`, and prints a formatted terminal report with
  confidence badges and YAML snippets.

### Changed

- **`ResponseObservation.response_fingerprint: str | None = None`**
  (`drift_detection.py`): New optional field (slots-safe, defaults to
  `None`).  Fully backward-compatible — existing callers that don't
  populate it get the same five-signal behaviour as before.

- **`FallbackEngine._observe_drift_signal`** (`fallback.py`): Accepts
  new `response_fingerprint` kwarg.  Non-streaming and streaming success
  paths now compute and pass a fingerprint for the `goal_progress_stall`
  signal.  `goal_mode` check applies `THRESHOLDS_GOAL` when the profile
  flag is set.

### Files touched

```
A  coderouter/guards/_fingerprint.py
M  coderouter/guards/__init__.py          — module registry comment
M  coderouter/guards/drift_detection.py   — Signal 6, THRESHOLDS_GOAL, new fields
M  coderouter/config/schemas.py           — FallbackChain.goal_mode
M  coderouter/routing/fallback.py         — fingerprint wiring, goal_mode dispatch
A  coderouter/state/suggest_rules.py
M  coderouter/state/__init__.py           — module registry comment
M  coderouter/cli.py                      — replay --suggest-rules
A  docs/articles/v1-saga/note-14-v0-4-goal-mode.md
M  docs/articles/v1-saga/INDEX.md
M  docs/inside/future.md
M  CHANGELOG.md, pyproject.toml          — 2.3.0a4 → 2.4.0
```

---

## [v2.3.0a4] — 2026-05-08 (Plugin SDK — ruff cleanup)

Patch over `v2.3.0a3`. CI's `ruff check .` job surfaced six lint
findings in the new Plugin SDK code. None affect runtime behavior.

### Fixed

- **RUF022**: `__all__` in `coderouter/plugins/__init__.py` is now
  isort-sorted alphabetically.
- **RUF006**: `_fanout_observers` was using `asyncio.create_task`
  without holding a strong reference. Asyncio's task tracker only
  keeps a weakref, so a fanout-in-flight task could be GC'd before
  the observer ran. Fixed by storing tasks in a per-engine
  ``_observer_tasks: set[asyncio.Task[None]]`` and removing each
  via ``task.add_done_callback(set.discard)`` on completion. The
  attribute is lazy-initialized in `_fanout_observers` itself so
  engines built via ``__new__`` (which bypass ``__init__``) still
  work.
- **I001 + F841**: `tests/test_plugins_integration.py` had unused
  imports (`AnthropicResponse`, `AnthropicUsage`) and an unused
  local (`captured_chat`) left over from a build-engine helper
  whose code path never ran. Removed the helper entirely; the
  remaining tests exercise the engine's hook surface
  (``_apply_input_filters`` / ``_fanout_observers`` /
  ``_safe_observe``) directly, which is what they always actually
  did.
- **I001**: `tests/test_plugins_loader.py` import block reordered
  alphabetically by module name.

### Files touched

```
M  coderouter/plugins/__init__.py        — __all__ alphabetical
M  coderouter/routing/fallback.py        — task strong-ref set
M  tests/test_plugins_integration.py     — drop dead helper
M  tests/test_plugins_loader.py          — import order
M  tests/test_plugins_registry.py        — formatting nit (blank line)
M  CHANGELOG.md, pyproject.toml          — 2.3.0a3 → 2.3.0a4
```

After this patch, ``ruff check .`` passes against every tracked
Python file in the repo.

---

## [v2.3.0a3] — 2026-05-08 (Plugin SDK — LogRecord.module collision fix)

Patch over `v2.3.0a2`. The wheel-install-and-test job in CI surfaced
one more issue that the source-tree test runs hadn't caught.

### Fixed

- **`KeyError: "Attempt to overwrite 'module' in LogRecord"`** in
  `discover_and_load`. Python's `logging` module reserves several
  attribute names on `LogRecord` (``name`` / ``msg`` / ``args`` /
  ``levelname`` / ``levelno`` / ``pathname`` / ``filename`` /
  **``module``** / ``lineno`` / ``funcName`` / ``exc_info`` /
  ``exc_text`` / ``stack_info`` / ``created`` / ``msecs`` /
  ``relativeCreated`` / ``thread`` / ``threadName`` / ``processName``
  / ``process`` / ``message`` / ``asctime``); passing any of these
  via ``extra=`` raises ``KeyError`` rather than silently overwriting.
  v2.3.0a1's `plugin-loaded` and `plugin-load-failed` log lines used
  ``"module"`` as an extra key (intended to mean "the module:attr
  string from `entry_point.value`"), which collided.

  Renamed the key to ``"entry_point"`` everywhere in
  `coderouter/plugins/loader.py`. Audited every `extra=` payload in
  the new plugins module + the engine's hook helpers — none of them
  use any of the other reserved names.

### Files touched

```
M  coderouter/plugins/loader.py     — "module" → "entry_point" (×2)
M  CHANGELOG.md, pyproject.toml     — 2.3.0a2 → 2.3.0a3
```

No other behavioral change. Downstream consumers that don't rely on
the structured log shape are unaffected; anyone parsing the JSON
log lines should rename `module` → `entry_point` to match.

---

## [v2.3.0a2] — 2026-05-08 (Plugin SDK — CI fixes)

Patch over `v2.3.0a1`. The Plugin SDK addition was sound but two
issues showed up in the test matrix and have been fixed:

### Fixed

- **`'FallbackEngine' object has no attribute 'plugins'`** —
  Many existing tests construct the engine via
  ``FallbackEngine.__new__`` to bypass full initialization (only
  ``config`` + ``_adapters`` are populated). The new direct
  attribute ``self.plugins`` was missing on those instances and
  raised ``AttributeError`` whenever the engine reached the hook
  helpers. Converted to the same lazy-property pattern that
  ``_adaptive`` / ``_budget`` / ``_memory_pressure_guard`` already
  use: store under ``_plugin_registry`` in ``__init__``, surface
  via a ``plugins`` property that lazily builds an empty registry
  when the underlying attribute is missing. Bypass-tests now see
  an empty registry and the hook helpers short-circuit cleanly.

- **`LogRecord` assertions in `test_plugins_loader.py` /
  `test_plugins_integration.py`** — used ``rec.message`` which
  isn't always populated (depends on whether a Formatter has
  processed the record). Switched to ``rec.msg`` with exact
  match, matching the rest of the test suite's convention
  (e.g. ``test_fallback_paid_gate.py``,
  ``test_memory_pressure.py``) where structured-log event names
  are tested via ``rec.msg == "<event-name>"``.

### Files touched

```
M  coderouter/routing/fallback.py    — lazy plugins property
M  tests/test_plugins_loader.py      — rec.msg ==
M  tests/test_plugins_integration.py — rec.msg ==
M  CHANGELOG.md, pyproject.toml      — 2.3.0a1 → 2.3.0a2
```

No behavioral change vs `v2.3.0a1`. If you've already pinned
`v2.3.0a1` in a downstream that doesn't construct engines via
``__new__`` and doesn't run our test suite, the upgrade is a
no-op for runtime.

---

## [v2.3.0a1] — 2026-05-08 (Plugin SDK)

**Theme: in-process plugin SDK. Core 5 deps stays untouched.** v2.3.0a1 adds the plugin discovery + dispatch infrastructure that ``coderouter-plugin-memory`` 0.1.0+ will consume. Two of the six designed extension points (``input_filter`` and ``observer``) are wired into the engine; the other four (``frontend`` / ``guard`` / ``output_filter`` / ``adapter``) ship as Protocol contracts only — plugin authors can target them today, but engine integration is deferred until a real plugin drives the requirement (v2.4+).

### Plugin SDK (new module: ``coderouter.plugins``)

| Component | What it does |
|---|---|
| ``coderouter.plugins.base`` | Six ``Protocol`` definitions (InputFilter, Observer, Frontend, Guard, OutputFilter, Adapter). All ``runtime_checkable`` so ``isinstance(x, InputFilter)`` works for diagnostics. |
| ``coderouter.plugins.loader`` | Reads ``importlib.metadata.entry_points`` under ``coderouter.<group>`` and applies the user's explicit ``plugins.enabled`` allowlist. Failures are logged + degraded — never abort startup. |
| ``coderouter.plugins.registry`` | Group-keyed container. ``input_filters`` / ``observers`` properties return defensive copies. |
| ``PluginsConfig`` (in ``schemas.py``) | New ``plugins:`` block in ``providers.yaml`` — ``enabled`` list + ``config`` dict. Absent → identical behavior to v2.2.0. |
| ``FallbackEngine`` integration | ``__init__`` now takes ``plugins=PluginRegistry``; ``generate_anthropic`` runs the InputFilter chain before chain dispatch; both Anthropic paths fan out ``request_completed`` to observers as fire-and-forget asyncio tasks. The no-plugin code path is bit-identical to v2.2.0. |
| ``ingress/app.py`` | ``create_app`` calls ``discover_and_load`` and hands the registry to the engine. |

### Supply-chain defense

``pip install coderouter-plugin-X`` is **not** sufficient to activate a plugin. The user must also list its entry-point name under ``plugins.enabled`` in ``providers.yaml``. Unlisted-but-installed entry points are logged ``plugin-skipped`` and never instantiated, so a compromised transitive dependency cannot wedge itself into the request flow.

### Failure semantics

| Failure | Engine behavior |
|---|---|
| ``importlib.metadata`` finds no entry point with an enabled name | ``plugin-not-found`` warn (one per missing name); engine boots normally. |
| Plugin module import fails | ``plugin-load-failed`` error; engine boots without that plugin. |
| Plugin ``__init__`` raises | Same — error logged, plugin skipped. |
| ``InputFilter.transform`` raises | ``input-filter-failed`` warn; pre-mutation request flows to the next filter / chain. |
| ``Observer.on_event`` raises | ``observer-failed`` warn; engine response is unaffected (fanout is fire-and-forget). |

### Backward compatibility

100%. ``providers.yaml`` files written for v2.2.0 keep working unchanged because ``plugins`` is optional and defaults to ``None``. The ``FallbackEngine(config)`` legacy constructor keeps working too — the new ``plugins=`` parameter has a sane default.

### Files changed

```
A  coderouter/plugins/__init__.py
A  coderouter/plugins/base.py
A  coderouter/plugins/loader.py
A  coderouter/plugins/registry.py
M  coderouter/config/schemas.py    — PluginsConfig + CodeRouterConfig.plugins field
M  coderouter/routing/fallback.py  — engine __init__, _apply_input_filters,
                                     _fanout_observers, _safe_observe + hook calls
M  coderouter/ingress/app.py       — discover_and_load wired into create_app
A  tests/test_plugins_registry.py
A  tests/test_plugins_loader.py
A  tests/test_plugins_integration.py
```

### Out-of-scope (deferred)

- Engine integration for ``frontend`` / ``guard`` / ``output_filter`` / ``adapter`` — Protocol contracts only.
- ``coderouter-plugin-memory`` itself — separate repo, separate release cadence (0.1.0 lands after this Core release publishes).

---

## [v2.2.0] — 2026-05-06 (Self-healing + Multi-day operation + Replay)

**Theme: complete the "unattended long-run operation" foundation with self-healing + state persistence + statistical replay.** v2.0-J implements automatic exclusion + restart + recovery for UNHEALTHY providers; v2.0-K implements the sqlite3 StateStore + structured audit log + request journal + `coderouter replay` statistical A/B analysis. v2.2 absorbs 3 hardening fixes originating from Unsloth Studio. **Reaches full coverage of all 6 failure classes + self-healing + persistence + replay.**

### v2.0-J: Self-healing Routing (L5 automatic recovery)

**Fully excludes UNHEALTHY providers from the chain, with a restart helper + recovery probe for automatic recovery.**

| Feature | Description |
|---|---|
| `SelfHealingOrchestrator` | Excludes UNHEALTHY providers from the chain when `backend_health_action: exclude` is set, and manages automatic recovery |
| restart helper | Automatically restarts the backend process via the `restart_command` setting (subprocess, with timeout) |
| recovery probe | Exponential backoff (30s → 300s) sends a 1-token probe to the excluded provider → immediate recovery on success |
| `recovery_probe_initial_s` / `recovery_probe_max_s` | Per-profile initial value / upper bound for the probe interval |
| `restart_timeout_s` | Timeout for the restart command |
| original-position recovery | On recovery, the provider is reinserted at its original position in the chain (not appended to the end) |

### v2.0-K: Multi-day Operation Support (persistence + audit + replay)

**Preserves operational state across process restarts + structured logging + statistical A/B analysis.**

| Feature | Description |
|---|---|
| `StateStore` | sqlite3 KV store (namespace-scoped, WAL mode, thread-safe, graceful degradation) |
| `state_dir` config | Enables persistence by pointing to a directory such as `~/.coderouter-t/state/` |
| 4-subsystem persistence | save_state/load_state for BudgetTracker / BackendHealthMonitor / SelfHealingOrchestrator / MetricsCollector |
| `AuditLogHandler` | Records 22 events (guard triggers / chain fallback / self-healing, etc.) to JSONL (single-backup rotation) |
| `coderouter audit` CLI | Browse the audit log with `--tail`, `--filter`, `--since`, `--summary` |
| `RequestLogHandler` | Records `cache-observed` event metadata (provider, tokens, cost) to JSONL (body is not recorded — privacy safe) |
| `request_log: off/active` | Enables the request journal |
| Replay engine | `summarize_window()` (per-provider aggregation) + `compare_providers()` (A/B delta + rate of change) |
| `coderouter replay` CLI | Prints a statistics table via `--compare A B`, `--provider`, `--since`, `--limit` |

### Configuration example

```yaml
# providers.yaml
state_dir: "~/.coderouter-t/state/"    # persistence directory
audit_log: active                     # structured audit log
request_log: active                   # request metadata journal

profiles:
  - name: self-healing
    providers: [ollama-qwen3, openrouter-free]
    backend_health_action: exclude    # UNHEALTHY → exclude + self-heal
    backend_health_threshold: 3

providers:
  - name: ollama-qwen3
    base_url: http://localhost:11434/v1
    model: qwen3:30b-a3b
    restart_command: "ollama serve"   # automatic restart
```

```bash
# CLI
coderouter audit --tail 20 --filter self-healing
coderouter replay --compare anthropic-api openrouter-free --since 2026-05-01
```

### v2.2: 3 hardening fixes originating from Unsloth Studio

| Feature | Description |
|---|---|
| tool_repair dedup | `repair_tools()` removes duplicate blocks sharing the same tool_use_id |
| `StripToolCallXmlFilter` | Strips `<tool_call>` / `<|tool▁call|>` XML tags via output_filters |
| `max_tool_calls` hard cap | Per-profile cap on the number of tool_use calls (default: 50) |

### New files

```
A  coderouter/guards/self_healing.py         — SelfHealingOrchestrator
A  coderouter/state/__init__.py              — package
A  coderouter/state/store.py                 — sqlite3 KV store
A  coderouter/state/audit_log.py             — JSONL audit log handler + reader
A  coderouter/state/request_log.py           — JSONL request journal handler + reader
A  coderouter/state/replay.py                — statistical A/B engine + CLI formatter
A  tests/test_self_healing.py                — 19 tests
A  tests/test_state_store.py                 — 19 tests
A  tests/test_audit_log.py                   — 14 tests
A  tests/test_request_log.py                 — 22 tests
```

### Overall summary

- Tests: ~1005 → **~964** (measured. The old test count included optional deps; 964 is the collectible count)
- Runtime deps: 5 → 5 (**41 consecutive sub-releases unchanged**)
- Backward compat: fully compatible, all features default off — existing behavior is unchanged until opt-in

---

## [v2.1.0] — 2026-05-05 (Long-run Reliability completed — v2.0-G/H/I)

**Theme: complete the Long-run Reliability pillar by simultaneously solving three failure classes — L4 quality degradation, L6 mid-stream failure, and L5 idle-time outages.** Combined with v2.0-F (L1 context overflow), CodeRouter now actively guards against 4 of the 6 failure classes.

### v2.0-G: Drift Detection (L4 quality-degradation guard)

**Automatically detects "drift" — the gradual degradation of model response quality during long agent sessions — and runs a corrective action.** When a local Ollama model runs for several hours, KV cache contamination or VRAM pressure can make responses become empty / shorter / stop returning tool_use (L4); five signals detect this. A three-stage action — warn → promote (chain demotion) → reload (Ollama KV flush) — automatically restores quality.

| Feature | Description |
|---|---|
| 5-signal detector | Monitors empty_response_rate / length_collapse / tool_silence_rate / stop_anomaly_rate / error_rate over a per-provider rolling window |
| `detect_drift()` | Pure function — determines severity none/mild/severe (severe×1 or mild×2 → severe) |
| `drift_detection_action: off/warn/promote/reload` | Enables the guard per profile (default: off) |
| `drift_detection_sensitivity: low/normal/high` | Selects the threshold preset |
| promote action | Diverts traffic to another provider via AdaptiveAdjuster rank demotion |
| reload action | Flushes the KV cache via Ollama `keep_alive=0` → resumes with a fresh context |
| Cooldown & Recovery | Rank is restored + window cleared after the configured number of seconds |
| `X-CodeRouter-Drift` header | Reports mild/severe status via the response header (streaming supported) |
| Prometheus metrics | `coderouter_drift_detected_total`, `coderouter_drift_promoted_total`, `coderouter_drift_reload_total` |

- Tests: ~930 → **~970** (+40, drift_detection 27 + drift_integration 10 + drift_actions 5)
- Runtime deps: 5 → 5 (**36 consecutive sub-releases unchanged**)
- Backward compat: fully compatible, `drift_detection_action` defaults to `"off"` — existing behavior is unchanged until opt-in

### Configuration example

```yaml
profiles:
  - name: long-session
    providers: [ollama-qwen3]
    drift_detection_action: reload      # off | warn | promote | reload
    drift_detection_sensitivity: normal # low | normal | high
    drift_detection_window_size: 20     # rolling window size
    drift_detection_cooldown_s: 300     # seconds to wait before recovery
```

### New files

- `coderouter/guards/drift_detection.py` — detection logic (observation model + detector + window manager)
- `coderouter/guards/drift_actions.py` — reload action (Ollama KV flush)
- `tests/test_drift_detection.py` — pure function tests (27 tests)
- `tests/test_drift_detection_integration.py` — engine integration tests (10 tests)
- `tests/test_drift_actions.py` — reload action tests (5 tests)
- `docs/drift-detection.md` — user documentation

### v2.0-H: Mid-stream Partial Stitching (L6 extension)

**When a streaming response fails partway through, returns the text accumulated so far to the client instead of discarding it.**

| Feature | Description |
|---|---|
| `_StreamUsageAccumulator` text accumulation | Tracks content_block_start/delta/stop and accumulates text blocks in memory |
| `MidStreamError.partial_content` | Carries the accumulated text on the exception (partial tool_use JSON is excluded) |
| `partial_stitch_action: off/surface` | Enables per profile (default: off) |
| `event: coderouter_partial` | Returns accumulated text + provider + reason as SSE metadata |
| Prometheus metric | `coderouter_partial_stitch_surfaced_total` |

### v2.0-I: Continuous Probing (L5 active health check)

**Actively detects provider outages during idle periods and updates the backend health state machine.**

| Feature | Description |
|---|---|
| `probe_one()` | Confirms the health of the full model pipeline via a 1-token completion |
| `probe_loop()` | asyncio background task — sequential probing + graceful shutdown |
| `continuous_probe: off/active` | Enabled via global config (default: off) |
| Model drift detection | Cross-checks the probe response's model name against config → warns on mismatch |
| Prometheus metrics | `probe_total`, `probe_outcomes_total`, `probe_rounds_total`, `probe_latency_ms`, `probe_drift_detected_total` |

### Overall summary

- Tests: ~930 → **~1005** (+75)
- Runtime deps: 5 → 5 (**38 consecutive sub-releases unchanged**)
- Backward compat: fully compatible, all features default off — existing behavior is unchanged until opt-in

---

## [v2.0.0] — 2026-05-05 (Context Budget Management — L1 overflow prevention)

**Theme: implement a guard that proactively prevents context overflow in long-running agent sessions.** When an agentic session such as Claude Code / Cline / OpenClaw runs a loop for over 8 hours, messages asymptotically fill the context window and the backend returns 400 / truncation, killing the session (L1) — this is fixed at the root. A two-stage guard — warn (80%) → auto trim (90%) — brings overflow to zero.

| Feature | Description |
|---|---|
| `estimate_context_usage()` | Estimates a request's context fill ratio via a char/4 heuristic (5-deps invariant preserved) |
| `trim_to_budget()` | Removes old messages from the front; preserves tool_use/tool_result pairs atomically by tool_use_id |
| `context_budget_action: off/warn/trim` | Enables the guard per profile (default: off) |
| `X-CodeRouter-Context-Budget` header | Reports warn/trimmed status via the response header (streaming supported) |
| Prometheus metrics | `coderouter_context_budget_warnings_total`, `coderouter_context_budget_trims_total`, `coderouter_context_budget_usage_ratio` |
| `coderouter stats` TUI | Fallback & Gates panel shows context budget warn/trim count + latest ratio |
| model-capabilities.yaml | Bundles max_context_tokens for major models (Claude 200K, Qwen3/3.5/3.6 32-131K, Gemma4 131K, DeepSeek 131K, etc.) |

- Tests: 878 → **~930** (+50, token_estimation 13 + context_budget 22 + ingress header 5 + metrics 6 + prometheus 3)
- Runtime deps: 5 → 5 (**35 consecutive sub-releases unchanged**)
- Backward compat: fully compatible, `context_budget_action` defaults to `"off"` — existing behavior is unchanged until opt-in

### Configuration example

```yaml
profiles:
  - name: long-session
    providers: [ollama-qwen3]
    context_budget_action: trim          # off | warn | trim
    context_budget_warn_threshold: 0.80  # warn at this fill ratio
    context_budget_trim_threshold: 0.90  # auto-trim at this fill ratio
    context_budget_trim_target: 0.75     # target fill ratio after trim
    context_budget_preserve_last_n: 4    # always keep the last N messages

providers:
  - name: ollama-qwen3
    base_url: http://localhost:11434/v1
    model: qwen3:30b-a3b
    max_context_tokens: 32768            # explicit override (if not in the registry)
```

### Files touched (primary)

```
A  coderouter/token_estimation.py
A  coderouter/guards/context_budget.py
M  coderouter/config/schemas.py
M  coderouter/routing/fallback.py
M  coderouter/routing/auto_router.py
M  coderouter/ingress/anthropic_routes.py
M  coderouter/logging.py
M  coderouter/metrics/collector.py
M  coderouter/metrics/prometheus.py
M  coderouter/cli_stats.py
M  coderouter/data/model-capabilities.yaml
A  tests/test_token_estimation.py
A  tests/test_context_budget.py
M  tests/test_ingress_anthropic.py
M  tests/test_metrics_collector.py
M  tests/test_metrics_prometheus.py
A  docs/inside/v2.0-F-context-budget-plan.md
```

---

## [v1.10.1] — 2026-05-04 (Patch — tool-aware auto routing + Raspberry Pi starter)

**Theme: declaratively solve the "local small models can't do tool calling, so only tool-laden requests should escape to the cloud" use case (OpenClaw + Pi 8GB scenario).** Extends the 6-matcher `auto_router` that v1.10.0 declared feature-complete to 7 matchers by adding `has_tools`, letting profiles branch on whether a request declares `tools[]`. Also ships a Raspberry Pi 8GB starter YAML (`examples/providers.raspberrypi.yaml`) so users running OpenClaw / Claude-Code-compatible agents on an SBC can get up and running by copying a single yaml file.

2 shipments included:

| # | sub-release | Theme | LOC | tests |
|---|---|---|---|---|
| 1 | **has_tools matcher** | Added `RuleMatcher.has_tools` as the 7th matcher, recognizing OpenAI/Anthropic `tools[]` plus OpenAI legacy `functions[]` in one shot (from OpenClaw + Pi) | ~80 | +7 |
| 2 | **Raspberry Pi starter** | New `examples/providers.raspberrypi.yaml`: small Ollama models (≤4B) + OpenRouter free tier + tool-aware profile routing via `has_tools` | YAML only | (+0 direct via loader validation; covered by existing parametric test) |

- Tests: 871 → **878** (+7: 6 has_tools matcher scenarios + a safety-net test for "set but non-matching" `has_tools: false`)
- Runtime deps: 5 → 5 (**34 consecutive sub-releases unchanged**)
- Backward compat: fully compatible; existing yaml / API / log payload schema are all identical; deployments that don't use the new `has_tools` field behave exactly as before
- pyproject version: 1.10.0 → 1.10.1

### Migration

None needed. **A natural upgrade from v1.10.0**:

- The `coderouter` command name / Python import name / providers.yaml format / env vars / ingress URL are all unchanged
- Existing `auto_router.rules[]` are unaffected; adopting `has_tools` only requires adding one line to the yaml
- This lands right after v1.10.0 declared the v1.6-lineage auto_router "feature complete with 6 matchers," but it's an extension of the same declarative framework with no structural changes — read it as "feature complete again, now with 7 matchers"

### Out of scope (v1.11+)

- **Provider capability gate for tools** — the idea of wiring `capabilities.tools=false` up as a skip gate in the fallback chain. This patch instead routes at the profile level (switching chains via the router) using the `has_tools` matcher; a provider-level skip gate is a separate issue. It needs compatibility review against CodeRouter's chain semantics (sequential fallback + downgrade), so it's deferred until the need is confirmed.
- **Stronger tool-call repair for small local models** — `tool_repair.py` currently rescues `<tool_call>{...}</tool_call>`-wrapped forms, but inferring tool calls from freeform text returned by 1-4B models is a separate area (`tool_emulation`). Prompt-template rewrites are another possible lever; design is deferred to v2.0.

### Files touched

```
A  examples/providers.raspberrypi.yaml
M  CHANGELOG.md
M  coderouter/config/schemas.py
M  coderouter/routing/auto_router.py
M  pyproject.toml
M  tests/test_auto_router.py
```

---

### has_tools matcher (from OpenClaw + Raspberry Pi)

**Theme: route only requests that declare tools[] to the cloud, leaving local small models to focus on plain tool-free chat.** When running a tool-aware agent like OpenClaw on an SBC in the Raspberry Pi 8GB / Jetson Nano class, the Ollama models (≤4B) that are practical for CPU inference struggle with tool calling (they don't return `finish_reason: tool_calls`, argument JSON gets malformed, or it gets buried in freeform text), leaving the agent unable to see any tool invocation happen. Adding `auto_router.rules[].if.has_tools` as the 7th matcher lets profile-level routing declaratively switch between "tools present → cloud (Qwen3-Coder/gpt-oss/Gemini-Flash on OpenRouter free tier)" and "no tools → local small model."

Example use case (excerpted from the Raspberry Pi 8GB starter `examples/providers.raspberrypi.yaml`):

```yaml
auto_router:
  rules:
    - id: user:has-tools-go-cloud
      profile: with-tools         # OpenRouter free tier only
      match:
        has_tools: true
    - id: user:image-go-cloud
      profile: vision              # Gemini Flash 1M ctx
      match:
        has_image: true
    - id: user:longcontext-go-cloud
      profile: longcontext
      match:
        content_token_count_min: 32000
  default_rule_profile: local-chat # qwen3.5:2b/4b / gemma3:1b, local
```

Connecting OpenClaw (an agent that declares tools like Bash/Read/Write on every turn) via `OPENAI_BASE_URL=http://<pi-ip>:8088/v1` automatically routes tool-laden traffic to the cloud, while lightweight chat is handled locally on the Pi. Only `OPENROUTER_API_KEY` needs to be set — no paid API key required (`ALLOW_PAID=false` by default).

#### Why profile-level rather than a provider-level capability gate?

The `ProviderConfig.capabilities.tools=false` flag already exists (since v0.x), but today it's only used for `coderouter doctor` diagnostics and `model-capabilities.yaml` resolution — it isn't wired up as a skip gate in the fallback chain. `thinking` / `cache_control` have a `will_degrade` gate (`provider_supports_*` in capability.py), but tools has no equivalent skip mechanism. This relies on the existing v0.3-D "downgrade path" (non-native + `tools[]` present → non-streaming + tool_repair): if the provider can't return tools, the adapter doesn't error, and from upstream's view it looks like a success (empty tool_calls), so the chain doesn't fall through — it just stops (observed symptom: "tool call didn't happen").

Bolting on a provider-level skip gate touches chain semantics and needs compatibility review, so this patch sticks to a **declarative lever at the profile level**. It achieves the same effect via an added auto_router rule without changing chain semantics, and can be introduced under exactly the same contract as the existing 6 matchers (exactly one + first match wins + fast-fail at load).

- Tests: 871 → **878** (+7: OpenAI tools[] / Anthropic tools[] / OpenAI legacy functions[] / no-tools fallthrough / empty-list fallthrough / has_tools rule taking priority over the code-fence rule / safety net for "set but non-matching" `has_tools: false`)
- Runtime deps: 5 → 5 (34 consecutive sub-releases unchanged)
- Backward compat: fully compatible; existing `auto_router` rules are unaffected; deployments that don't use `has_tools` behave exactly as before

#### Changes

- `coderouter/config/schemas.py`:
  - Added `has_tools: bool | None = None` to `RuleMatcher` and to the `_MATCHER_FIELDS` tuple (the existing zero/multiple-fields "exactly one" validator applies automatically).
  - Added it as the 7th entry in the docstring's Variants section, documenting why the boolean shape mirrors `has_image` (only `True` is meaningful; `False` is "set" but doesn't match, per the `is True` check in `_match_rule` — a safety net), and how it differs from the provider-level `capabilities.tools` flag (the former is profile-level routing; the latter is a doctor diagnostic aid, not a chain skip gate).

- `coderouter/routing/auto_router.py`:
  - Added a `_has_tools_in_body(body)` helper — recognizes top-level `tools[]` (shared by OpenAI Chat Completions / Anthropic Messages API) and `functions[]` (OpenAI legacy, deprecated but still seen with pinned SDKs) in one pass; empty list / None both resolve to False (handles lazy init).
  - Added `has_tools: bool` to the `_match_rule(rule, message, text, model, estimated_tokens, has_tools)` signature, implementing the `has_tools is True` branch as the 7th case.
  - `classify(...)` now calls `_has_tools_in_body(body)` once and passes the result through rule iteration. The `has_tools` rule is evaluated even when `user_msg is None` (supports system-only prompts with a `tools[]` declaration).
  - Added `has_tools` to the `signals` payload in `_emit_resolved` / `_emit_fallthrough`, so the `auto-router-resolved` log lets the dashboard / Prometheus exporter see whether routing happened based on a tools-present signal.

- Added Group 8 (tool-aware routing) to `tests/test_auto_router.py`, 7 cases:
  - `test_classify_request_with_openai_tools_routes_to_with_tools` — basic case: OpenAI-style `tools[].function` → `with-tools` profile.
  - `test_classify_request_with_anthropic_tools_routes_to_with_tools` — Anthropic-style `tools[].input_schema` uses the same top-level `tools` key, so a single matcher covers both ingress shapes.
  - `test_classify_request_with_legacy_functions_routes_to_with_tools` — OpenAI legacy `functions[]` (deprecated but still seen with pinned SDKs) also counts as tool-laden.
  - `test_classify_request_without_tools_falls_through` — inverse case: plain chat with no tools declared falls through to `default_rule_profile` (`local-chat` on the Pi).
  - `test_classify_empty_tools_list_treated_as_no_tools` — `tools: []` / `functions: []` (lazy-init shape) resolve to False, pinning the no-spurious-match property.
  - `test_classify_has_tools_first_match_wins_over_later_content_rule` — when the has_tools rule is placed before a code_fence rule, and the body matches both, the first one wins — applying the global "first match wins" rule to the new matcher too.
  - `test_has_tools_false_rejected_at_load` — documents that `has_tools: False` passes `_exactly_one` but never matches due to the `is True` check in `_match_rule`, guaranteeing that a misconfiguration still falls through to the default path.

#### Files touched

```
M  CHANGELOG.md
M  coderouter/config/schemas.py
M  coderouter/routing/auto_router.py
M  pyproject.toml
M  tests/test_auto_router.py
```

---

### Raspberry Pi 8GB starter (`examples/providers.raspberrypi.yaml`)

**Theme: bundle a minimal setup for running OpenClaw on an SBC into a single yaml file.** This starter centers on the `has_tools` matcher added in v1.10.1, letting `coderouter serve` alone route tool-aware traffic between local Ollama on the Pi (qwen3.5:2b/4b, qwen2.5:1.5b, gemma3:1b) and OpenRouter's free tier (qwen3-coder:free / gpt-oss-120b:free / gemini-2.5-flash:free). Only `OPENROUTER_API_KEY` needs to be set — no paid API key required (`ALLOW_PAID=false` by default).

#### Design points

- **All local models set `tools: false`** — the ≤4B models that fit on a Pi 8GB can't reliably return tool_calls, so capability is explicitly declared `false`. This is a doctor-diagnostic declaration; actual routing is handled by the `has_tools` matcher at the profile level, so it's defense in depth.
- **`num_ctx: 8192` + `num_predict: 1024` caps** — CPU inference on the Pi is more practical with a smaller context; Ollama's default 2048 chokes on OpenClaw's system prompt, while raising it to 32K makes prefill take minutes, so 8K is a practical middle ground.
- **Images / long context (32K+) also go to the cloud** — instead of Gemma 4 E4B (vision-capable but at 9.6GB doesn't fit an 8GB Pi), the `has_image` rule routes to OpenRouter's Gemini Flash (1M ctx + native vision).
- **3 OpenRouter free models for vendor diversity** — lining up qwen-coder / gpt-oss / gemini-flash across 3 vendors provides an escape route from rate limits when hitting the daily cap (~200 req/day per model per account).
- **`output_filters: [strip_thinking, strip_stop_markers]` always applied for Qwen models** — the Qwen 3.5 models run on the Pi were observed leaking both `<think>...</think>` and `<|im_end|>`, so both are stripped.

#### Tests

`tests/test_examples_yaml.py::test_example_yaml_loads` parametrically covers `examples/providers*.yaml`, so `providers.raspberrypi.yaml` is automatically covered by this test too. If specific invariants worth pinning emerge (e.g., all-local `tools: false`, presence of the `has_tools` rule, auto_router default being `local-chat`), dedicated tests can be added in a follow-up patch, but this patch only secures the parametric loader-clean property.

#### Files touched

```
A  examples/providers.raspberrypi.yaml
```

---

## [v1.10.0] — 2026-05-01 (Umbrella tag — Cost enforcement + Long-run reliability completion + Auto-router feature complete)

**Theme: complete the "observe → understand → act" triad, rounding out Vision pillars P2/P3.** The 2 features landed in v1.9.1 (patch) (v1.9-B2 streaming usage aggregation + per-model auto-routing) were effectively a warm-up for the v1.10 backlog; this v1.10.0 bundles the remaining 3 features as a minor release. CodeRouter's Vision — **"a reliability layer for running agents on local LLMs over long sessions"** — has its v1.x share completed: of the 6 failure classes (excluding context overflow / L1 and quality drift / L4), L2/L3/L5/L6 are now systematically handled, all 6 declarative auto-router matchers are in place, and the cost pillar's path from observation (v1.9-D) to enforcement (v1.10) is closed.

5 shipments included (per the v1.10 work order in `docs/inside/future.md §6.6`, all complete in this release):

| # | sub-release | Theme | LOC | tests | Shipped in |
|---|---|---|---|---|---|
| 1 | **v1.9-B2** | Usage aggregation for the streaming path (`_StreamUsageAccumulator`, placeholder → observed values) | ~150 | +3 | v1.9.1 |
| 2 | **per-model auto-routing** | `RuleMatcher.model_pattern` (Opus/Sonnet/Haiku branching, from free-claude-code) | ~120 | +5 | v1.9.1 |
| 3 | **provider monthly budget cap** | `BudgetTracker` + `cost.monthly_budget_usd` (from LiteLLM / cumulative version of v1.9-D) | ~250 | +8 | **v1.10.0** |
| 4 | **v1.9-E phase 2 (L2/L5)** | Memory pressure detector + Backend health state machine (Vision pillar complete) | ~370 | +27 | **v1.10.0** |
| 5 | **longContext auto-switch** | `RuleMatcher.content_token_count_min` (from claude-code-router) | ~80 | +5 | **v1.10.0** |

- Tests: 838 (v1.9.1) → **871** (+33: +27 from v1.9-E phase 2 alone within this minor + 8 budget + 5 longContext → net +33 in v1.10.0)
- Runtime deps: 5 → 5 (**34 consecutive sub-releases unchanged**) — still only `fastapi / uvicorn / httpx / pydantic / pyyaml`, as from the start
- pyproject version: 1.9.1 → 1.10.0

### Achievements by pillar

#### P2 Long-run Reliability (v1.9-E lineage) — the core of the Vision is complete

Of the 6 failure classes (`docs/inside/future.md §1`), the ones declared in scope for v1.x are now complete:

| # | Failure | v1.x owner | Status |
|---|---|---|---|
| **L1** | Context overflow | (v2.0-F) | pending |
| **L2** | Memory pressure | v1.9-E phase 2 | done (v1.10.0) |
| **L3** | Tool loop | v1.9-E phase 1 | done (v1.9.0) |
| **L4** | Quality drift | (v2.0-G) | pending |
| **L5** | Backend crash / health | v1.9-E phase 2 | done (v1.10.0) |
| **L6** | Mid-stream interrupt | existing v0.3-A baseline + (v2.0-H enhancement) | done (baseline) |

The L2/L3/L5 trio now live side by side under `coderouter/guards/`, with `MemoryPressureGuard` / `_apply_tool_loop_guard` / `BackendHealthMonitor` each standing as an independent pure module. Engine integration lands cleanly at just 2 chokepoints: `_observe_provider_failure` / `_observe_provider_success`.

#### Cost pillar (v1.9-D lineage) — the observation → constraint path is closed

| Stage | sub-release | Role |
|---|---|---|
| **Observation** | v1.9-A | `cache-observed` log + 4-class cache hit/miss outcome |
| **Observation coverage** | v1.9-B2 (v1.9.1) | Full coverage of the streaming path, placeholders eliminated |
| **Understanding** | v1.9-D | Per-provider USD cost with cache savings computed separately (more precise than what existing LiteLLM offers) |
| **Constraint** | **v1.10.0** | Per-provider monthly cap via `monthly_budget_usd`, in-memory bucketing by UTC calendar month |

By v1.9.0 GA, "4-class observation precision" and "cost calculation more precise than existing LiteLLM" were established as CodeRouter's differentiators; v1.10.0 closes the path to put that toward enforcement.

#### Auto-router (v1.6 lineage) — feature complete with 6 matchers

| # | matcher | Origin | Shipped |
|---|---|---|---|
| 1 | `has_image` | v1.6-A bundled | v1.6.0 |
| 2 | `code_fence_ratio_min` | v1.6-A bundled | v1.6.0 |
| 3 | `content_contains` | v1.6-A user-defined | v1.6.0 |
| 4 | `content_regex` | v1.6-A user-defined | v1.6.0 |
| 5 | `model_pattern` | from free-claude-code | v1.9.1 |
| 6 | `content_token_count_min` | from claude-code-router | **v1.10.0** |

Declarative routing is now complete across "latest message content / image (per-turn signal)" and "request-wide model id / token count (request-shape signal)." Intake of v1.10 candidates extracted from competitive analysis is now closed; further additions will resume on a request-driven basis.

### Migration

None needed. **A natural upgrade from v1.9.1 / v1.9.0 / v1.9.0a\***:

- The `coderouter` command name / Python import name / providers.yaml format / env vars / ingress URL are all unchanged
- New schema fields (`cost.monthly_budget_usd` / `memory_pressure_*` / `backend_health_*` / `content_token_count_min`) are all optional with safe defaults (`monthly_budget_usd: None`, actions default to `warn` or `off`); deployments that don't set them behave exactly like v1.9.x
- New log events (`skip-budget-exceeded` / `chain-budget-exceeded` / `memory-pressure-detected` / `skip-memory-pressure` / `chain-memory-pressure-blocked` / `backend-health-changed` / `demote-unhealthy-provider`) use the same JSON shape as existing ones (`cache-observed` / `provider-failed` / etc.); external consumers can add a dispatch handler on their end to support them

### Out of scope (v2.0+)

- **L1 Context overflow** → v2.0-F (semantic compression, per-mode context budget)
- **L4 Quality drift detection** → v2.0-G (rolling-window observation of response quality)
- **L6 Mid-stream stitching enhancement** → v2.0-H (resumable continuation)
- **Continuous probing** → v2.0-I (hourly/daily model health checks, HF dataset publication)
- **Persistent budget state** (sqlite / Redis) — rejected within v1.x scope per the 5-deps invariant
- **L5 active probing** (active GET /api/version every 60s) — the domain of v2.0-I; deferred since passive observation already covers ~80%
- **Precise token counting via tiktoken / SentencePiece** — rejected per the 5-deps invariant

See `docs/inside/future.md §7` for details.

### Files touched

```
A  coderouter/guards/backend_health.py
A  coderouter/guards/memory_pressure.py
A  coderouter/routing/budget.py
A  tests/test_backend_health.py
A  tests/test_budget.py
A  tests/test_memory_pressure.py
M  CHANGELOG.md
M  coderouter/config/schemas.py
M  coderouter/logging.py
M  coderouter/metrics/collector.py
M  coderouter/metrics/prometheus.py
M  coderouter/routing/auto_router.py
M  coderouter/routing/fallback.py
M  docs/inside/future.md
M  plan.md
M  pyproject.toml
M  tests/test_auto_router.py
```

---

### v1.10 candidate #5: longContext auto-switch (from claude-code-router)

**Theme: automatically escape context-window pressure.** When a long prompt arrives (accumulated conversation history, a pasted codebase, etc.), this mechanism auto-switches away from a model with a smaller context window (200K for Anthropic) toward the 1M-ctx Gemini Flash line. Adds `auto_router.rules[].if.content_token_count_min` as the 6th matcher, inheriting the same "exactly one" contract as the existing 5.

Example use case:

```yaml
auto_router:
  rules:
    - if: { content_token_count_min: 32000 }
      route_to: longcontext
  default_rule_profile: writing

profiles:
  - name: longcontext
    providers:
      - openrouter-gemini-flash-free   # 1M ctx
      - anthropic-haiku-direct          # 200K ctx
  - name: writing
    providers: [anthropic-sonnet-direct]
```

When an agent runs 100 turns of short exchanges and the context balloons, it automatically switches to the 1M-ctx chain. This brings needs originating from `free-claude-code` / `claude-code-router` into CodeRouter's declarative auto_router framework.

#### Design decision: char/4 heuristic vs. tiktoken

Token counting uses the naive `len(text) // 4` heuristic (OpenAI's official rule of thumb). To respect the **5-deps invariant** (`plan.md §5.4`), tiktoken / SentencePiece are not introduced. Trade-offs:

- **English prose / code**: char/4 is somewhat loose (actual is ~3.5/token); since this is a `min` comparison, a larger threshold can be used to stay on the safe side
- **CJK (Japanese/Chinese/Korean)**: char/4 **conservatively undercounts** (actual is ~1.5-2 chars/token) — a 100k-character Japanese prompt is underestimated at ~25k tokens. This doesn't actively cause context overflow, so it's a fail-safe direction of error
- **Trade-off judgment**: tiktoken would be accurate but adds a ~100MB dependency; SentencePiece is ~50MB too. As CodeRouter is a "signal-based router for individual developers," a heuristic that operators tune via real-world feedback on the threshold is sufficient

#### Difference from other matchers

`content_contains` / `content_regex` / `has_image` are evaluated against the **latest user message** (per-turn signal), while `content_token_count_min` walks and sums across the **entire request (system + all messages)** (request-shape signal). Context-window pressure is a property of the whole request, so a latest-only approach would misdetect it.

- Tests: 866 → **871** (+5: long-prompt match / short-prompt fallthrough / walking all messages / rejecting negative values / first-match-wins precedence)
- Runtime deps: 5 → 5 (34 consecutive sub-releases unchanged)
- Backward compat: fully compatible; existing `auto_router` rules are unaffected

#### Changes

- `coderouter/config/schemas.py`:
  - Added `content_token_count_min: int | None = None` (`ge=1`) to `RuleMatcher`, registered in `_MATCHER_FIELDS` (the existing "exactly one" validator applies automatically; `ge=1` rejects 0/negative values at schema load).
  - Documented as the 6th entry in the docstring's Variants section, covering the char/4 heuristic + all-messages scope (distinguishing it from the latest-only matchers) + the 5-deps trade-off.

- `coderouter/routing/auto_router.py`:
  - Added a `_estimate_total_tokens(body)` helper — walks `body["system"]` (supports both str and list-of-blocks) and every message in `body["messages"]`, extracting text via `_extract_text`, then dividing the summed char count by `_CHARS_PER_TOKEN_HEURISTIC=4` to estimate tokens. Image / non-text blocks contribute 0.
  - Added an `estimated_tokens: int` parameter to `_match_rule`, implementing the `content_token_count_min` comparison as the 6th branch.
  - `classify(...)` computes `_estimate_total_tokens(body)` once and threads it through the rule-evaluation loop. Added `estimated_tokens` to the signals payload in `_emit_resolved` / `_emit_fallthrough`, so the dashboard / Prometheus exporter can see the estimated token count that drove routing to a given profile.

- Added Group 7 to `tests/test_auto_router.py`, 5 cases:
  - `test_classify_long_prompt_routes_to_longcontext` — 200,000 chars (~50,000 tokens) exceeds the 32,000 threshold → longcontext profile.
  - `test_classify_short_prompt_below_threshold_falls_through` — 1,000 chars (~250 tokens) → falls through to `default_rule_profile` (writing).
  - `test_classify_long_context_walks_all_messages_not_just_latest` — pins that longContext catches a long conversation history plus a short latest user message, a case a latest-only matcher would miss.
  - `test_content_token_count_min_rejects_non_positive_at_load` — rejects `0` / `-5` when constructing `RuleMatcher` (pydantic `ge=1`).
  - `test_long_context_first_match_wins_over_later_image_rule` — pins first-match-wins ordering: placing the token-count rule first makes longcontext win even on a body that's both long-text and image.

#### Files touched

```
M  CHANGELOG.md
M  coderouter/config/schemas.py
M  coderouter/routing/auto_router.py
M  tests/test_auto_router.py
```

#### Why now

Item **#5 (final)** in the v1.10 work order in `docs/inside/future.md §6.6`. Implementation size ~80 LOC + ~150 LOC of tests, a half-day of effort (well under the original estimate of ~150-200 LOC / 3-5 days — the matcher-addition pattern for auto_router was already established via per-model auto-routing, and only the `_estimate_total_tokens` helper for walking all messages needed to be added).

This completes **all 5 v1.10 candidates** (#1 v1.9-B2 / #2 per-model auto-routing shipped in v1.9.1; #3 monthly budget / #4 v1.9-E phase 2 / #5 longContext auto-switch land in this [Unreleased] umbrella). This is now positioned to be **tagged as the v1.10.0 minor umbrella** at the next PyPI publish (Vision pillar complete + all 6 auto-router matchers in place + cost enforcement complete).

#### Out of scope (v2.0+ / future refinement)

- **Precise token counting via tiktoken / SentencePiece** — rejected per the 5-deps invariant. Revisit if threshold tuning becomes difficult in real-world operation.
- **Automatic per-provider context-window inference** — adding `max_context_tokens` to `model-capabilities.yaml` could enable auto-inference, but this depends on the operator's usage scenario, so explicit declaration is sufficient for now.
- **Dynamic threshold (based on the chain's minimum max_context_tokens)** — same as above; explicit declaration is sufficient for now.

---

### v1.10 candidate #4: v1.9-E phase 2 (L2 memory pressure + L5 backend health) — Vision complete

**Theme: complete the Long-run Reliability pillar (P2) that promises "the agent loop won't stop even after 8 hours."** v1.9.0 shipped L3 (tool-loop guard) first as phase 1, but of the 6 failure classes described in the Vision, **L2 (Memory pressure)** and **L5 (Backend crash / health)** remained as phase 2. This release implements both as opt-in guards, joining the trio (tool_loop / memory_pressure / backend_health) under `coderouter/guards/` that together cover the core 3 failure modes of long-running operation.

#### L2: Memory pressure detection + cooldown

When a local backend (Ollama / LM Studio / llama.cpp) returns a 5xx due to VRAM exhaustion, the error body contains OOM phrases like `out of memory` / `CUDA out of memory` / `insufficient memory` / `model requires more system memory`. L2 observes this and places the affected provider into cooldown, skipping it for `memory_pressure_cooldown_s` seconds starting from the next chain resolution:

```yaml
profiles:
  - name: default
    providers: [ollama-large, ollama-small, openrouter-fallback]
    memory_pressure_action: skip       # off / warn / skip (default: warn)
    memory_pressure_cooldown_s: 120    # default 120s, 10-3600 s
```

With `action=skip`, when ollama-large OOMs it's excluded from the chain for 120 seconds, falling through to ollama-small or openrouter-fallback — then retried once the cooldown expires. `action=warn` (default) only logs; `off` disables the feature entirely (zero overhead).

#### L5: Backend health (consecutive failure state machine)

A de facto demotion mechanism for when a backend crashes suddenly. `BackendHealthMonitor` counts consecutive failures per provider, transitioning `HEALTHY → DEGRADED` at `backend_health_threshold` (default 3) and `DEGRADED → UNHEALTHY` at `2 x threshold`. A single success immediately restores HEALTHY. With `backend_health_action: demote`, an UNHEALTHY provider is demoted to the end of the chain (demoted, not skipped — a single liveness-check request still gets through, per the best-effort principle):

```yaml
profiles:
  - name: default
    providers: [ollama-local, anthropic-fallback]
    backend_health_action: demote       # off / warn / demote (default: warn)
    backend_health_threshold: 3
```

This is orthogonal to v1.9-C's `adaptive` (rolling-window continuous observation + debounce) — adaptive covers the "gradual slowdown" gradient case, while L5 covers the "sudden crash" binary case. Both can be stacked; with both enabled, a chain reorders on either signal — "latency degradation → adaptive demote" and "crash → L5 demote."

#### Numbers

- Tests: 839 → **866** (+27 cumulative: L2 +19 / L5 +8)
- Runtime deps: 5 → 5 (32 consecutive sub-releases unchanged)
- Backward compat: fully compatible; both `*_action` defaults are `warn` (log only, no behavior change). `off` disables entirely. Existing v1.9.x deployments continue naturally with no yaml changes

#### Changes

- New `coderouter/guards/memory_pressure.py` (~170 LOC):
  - `is_memory_pressure_error(exc)` — a pure function doing case-insensitive substring matching against 9 OOM phrases (real-world patterns observed for Ollama / LM Studio / llama.cpp / generic CUDA / Metal).
  - `MemoryPressureGuard` — a per-provider TTL cooldown tracker with `mark_pressured` / `is_pressured` / `pressured_until` API, built on `time.monotonic` for wall-clock-skew resilience; tests inject a deterministic clock via the `now=` argument.

- New `coderouter/guards/backend_health.py` (~200 LOC):
  - `BackendHealthMonitor` — a per-provider state machine (HEALTHY / DEGRADED / UNHEALTHY); `record_attempt(success, threshold)` records an observation and returns a `HealthTransition` only on an actual state change (no log spam on stable state); threshold is per-call to support different profiles.
  - State transition rules: N (= threshold) consecutive failures → DEGRADED, 2N failures → UNHEALTHY, a single success → immediate return to HEALTHY.

- `coderouter/config/schemas.py`:
  - Added `memory_pressure_action` / `memory_pressure_cooldown_s` (L2) and `backend_health_action` / `backend_health_threshold` (L5) to `FallbackChain`, following the same naming and the same "off / warn / action" tri-state pattern as L3 (`tool_loop_*`).
  - Each field has a `Literal` type + range constraints + detailed docstring covering which failure mode it addresses, how L2/L5 differ in use, and the relationship to v1.9-C's adaptive routing.

- `coderouter/logging.py`:
  - L2: `log_memory_pressure_detected` / `log_skip_memory_pressure` / `log_chain_memory_pressure_blocked` helpers + 3 TypedDict payloads. Fully symmetric with the paid-gate / budget-gate helpers.
  - L5: `log_backend_health_changed` (state transition, payload includes old_state/new_state/consecutive_failures) / `log_demote_unhealthy_provider` helpers + 2 TypedDicts.

- `coderouter/routing/fallback.py`:
  - Added `_memory_pressure` / `_backend_health` lazy properties to `FallbackEngine` (same pattern as `_adaptive` / `_budget`, compatible with legacy tests going through `__new__`).
  - New `_observe_provider_failure(provider, exc, profile)` helper — dispatches L2 OOM detection + L5 failure counting at a single chokepoint, called from all 6 failure sites (4 entry points x non-stream/mid-stream).
  - New `_observe_provider_success(provider, profile)` — calls the L5 state machine's success transition from all 4 success sites (on provider success).
  - Extended `_resolve_chain` to 4 passes: paid → budget → **L2 pressure skip** → L5 demote. L2 is a filter (skip), L5 is a reorder (demote), keeping the two roles distinct. L5 demotion only kicks in when the chain has both unhealthy and healthy providers (a uniformly-UNHEALTHY chain is a no-op, suppressing log spam).

- `coderouter/metrics/collector.py`:
  - Added `_provider_skipped_memory_pressure: Counter` + `_chain_memory_pressure_blocked_total: int` (L2).
  - Added `_provider_demoted_unhealthy: Counter` + `_backend_health_transitions: dict[str, Counter]` (L5, keyed by destination state).
  - Wired dispatch for `skip-memory-pressure` / `chain-memory-pressure-blocked` / `backend-health-changed` / `demote-unhealthy-provider` events plus snapshot/reset.

- `coderouter/metrics/prometheus.py`:
  - Added `coderouter_provider_skipped_total{reason="memory_pressure"}` alongside the existing `paid` / `unknown` / `budget` counters.
  - Added `coderouter_provider_demoted_unhealthy_total{provider}` (L5), `coderouter_backend_health_transitions_total{provider, state}` (L5), and `coderouter_chain_memory_pressure_blocked_total` (L2).

- New `tests/test_memory_pressure.py` (~360 LOC, +19 tests):
  - **Group 1 (detector)**: parameterized coverage of 8 OOM phrases, plus 5 non-OOM failures confirmed to return false.
  - **Group 2 (guard)**: TTL cooldown / lazy expiry / re-mark extension.
  - **Group 3 (engine)**: action=warn logs only / action=skip skips the chain during cooldown and falls back / action=off fully disables / all providers pressured triggers `chain-memory-pressure-blocked` warn + `NoProvidersAvailableError`.

- New `tests/test_backend_health.py` (~340 LOC, +8 tests):
  - **Group 1 (monitor)**: initial state HEALTHY, state transitions at threshold/2x threshold, immediate UNHEALTHY → HEALTHY recovery on success, no transition returned on stable state.
  - **Group 2 (engine action)**: warn logs only / demote reorders the chain (verified via try-provider order) / off means zero monitoring / recovery transition logged for UNHEALTHY → HEALTHY.
  - **Group 3 (chain reorder)**: demote is a no-op when all providers are UNHEALTHY (no log spam, best-effort continues).

#### Files touched

```
A  coderouter/guards/backend_health.py
A  coderouter/guards/memory_pressure.py
A  tests/test_backend_health.py
A  tests/test_memory_pressure.py
M  CHANGELOG.md
M  coderouter/config/schemas.py
M  coderouter/logging.py
M  coderouter/metrics/collector.py
M  coderouter/metrics/prometheus.py
M  coderouter/routing/fallback.py
```

#### Why now

Item #4 in the v1.10 work order in `docs/inside/future.md §6.6` — **the core of the Vision**. Among the backlog organized as "v1.10 candidates" at v1.9.0 GA, this is the only Vision-critical pillar at the ~900 LOC scale. With v1.9.1's monthly budget making the cost axis operable, L2/L5 complete the promise that **"of the 6 failure classes, L2/L3/L5 are systematically handled."** `L1 Context overflow` / `L4 Quality drift` / `L6 Mid-stream stitching enhancement` remain the domain of v2.0-F/G/H; this marks the endpoint of long-run reliability coverage intended for v1.x.

#### Out of scope (v2.0+)

- **L5 active probing** (active GET /api/version every 60s) — passive observation already covers most of the relevant range; adding active probing increases httpx lifecycle/mocking complexity, so it's deferred for reconsideration under v2.0-I (the `continuous probing` pillar extension).
- **L2 thresholding (count of OOM events before marking)** — the naive "single OOM = mark" implementation is sufficient. Requiring multiple OOM observations before marking would only be considered after real-world operational feedback.
- **More than the 3-tier HEALTHY/DEGRADED/UNHEALTHY** — 3 tiers is sufficient for now; revisit after operational feedback.

---

### v1.10 candidate #3: provider monthly budget cap (from LiteLLM / cumulative version of v1.9-D)

**Theme: now that v1.9-D made "how much has been spent" visible, add a gate to declare "don't spend beyond this."** v1.9-D's `cost_total_usd` is a process-lifetime cumulative value, so it can't serve as a billing-cycle cap (it disappears on restart and doesn't reset at month boundaries). This feature lets you declare a **per-provider monthly USD cap** via `cost.monthly_budget_usd`, so the chain resolver skips a provider once its running total for the UTC calendar month hits the cap.

Example use case:

```yaml
providers:
  - name: anthropic-direct
    kind: anthropic
    base_url: https://api.anthropic.com
    model: claude-sonnet-4-6
    cost:
      input_tokens_per_million: 3.0
      output_tokens_per_million: 15.0
      monthly_budget_usd: 5.0   # new field in v1.10
  - name: ollama-local
    base_url: http://localhost:11434/v1
    model: qwen3.6:35b-a3b
    # free / cost unset = unlimited (not subject to skipping)
profiles:
  - name: default
    providers: [anthropic-direct, ollama-local]   # paid → free fallback
```

Once `anthropic-direct` has spent 5 USD this month, the chain resolver skips it and falls through to `ollama-local` (free). A `skip-budget-exceeded` info event is emitted, plus a `chain-budget-exceeded` warn only if every provider has hit its cap.

**Deliberate persistence limitation**: in-memory only. The running total resets to 0 on process restart. To respect the **5-deps invariant** (`plan.md §5.4`), sqlite / Redis / disk are not introduced. Operators who need durable monthly enforcement can adequately cover it by feeding v1.9-D's `cost_total_usd` panel into an external monitoring tool (Prometheus alertmanager / Grafana threshold).

- Tests: 831 → **839** (+8: 3 pure BudgetTracker tests / 2 CostConfig schema tests / 3 engine integration tests)
- Runtime deps: 5 → 5 (31 consecutive sub-releases unchanged)
- Backward compat: fully compatible; deployments that don't set `monthly_budget_usd` behave exactly as before (opt-in feature)

#### Changes

- `coderouter/config/schemas.py`:
  - Added `monthly_budget_usd: float | None = None` to `CostConfig` (`ge=0.0`; None = unlimited).
  - Documented UTC calendar-month + in-memory-only persistence in the docstring, and consistency with the 5-deps invariant (no sqlite/Redis).

- New `coderouter/routing/budget.py` (~190 LOC):
  - `BudgetTracker` class — holds a per-provider current-month USD running total in a `dict[str, float]`, guarded by a `threading.RLock`. Month-boundary detection uses the `_utc_month_key` helper (via UTC `datetime.now()`; tests can inject a deterministic value via the `now=` argument).
  - Public API: `record(provider, cost_usd)` / `is_over_budget(provider, budget_usd)` / `current_month()` / `total_for_provider(provider)` / `reset()`.
  - **Lazy month rollover**: each public call calls `_roll_if_needed` on entry, clearing `_totals` before answering the query if the cached month differs from the current UTC month. No background timer needed.
  - `is_over_budget` uses a `>=` comparison — an exact hit of "5.00 USD" is treated as exhausted (conservative: the next call won't be billed).

- `coderouter/logging.py`:
  - Added `SkipBudgetExceededPayload` / `ChainBudgetExceededPayload` TypedDicts + `log_skip_budget_exceeded` / `log_chain_budget_exceeded` helpers, fully mirroring the `log_chain_paid_gate_blocked` pattern, with `month` (YYYY-MM UTC bucket) included in the payload.

- `coderouter/routing/fallback.py`:
  - Added `_budget_tracker: BudgetTracker = BudgetTracker()` to `FallbackEngine.__init__`, exposing `_budget` via the same lazy property pattern as `_adaptive` (returns an empty tracker even via the legacy `__new__` path used by legacy tests).
  - Refactored `_resolve_chain` into 2 passes: pass 1 is the existing paid-gate logic, pass 2 is the new **budget-gate**. The budget-gate only checks providers where `provider_cfg.cost.monthly_budget_usd` is set; if `is_over_budget`, it emits a `skip-budget-exceeded` info event and excludes the provider from candidates. When the chain empties out, the aggregate warn prefers `blocked_by_budget` (since filtering happened after the paid-gate), firing `chain-budget-exceeded`.
  - Added a `budget: BudgetTracker | None = None` argument to `_emit_cache_observed` / `_emit_cache_observed_streaming`, calling `budget.record(provider, cost.total_usd)` whenever `compute_cost_for_attempt` returns a positive result. Wired at both engine call sites (`generate_anthropic` / `stream_anthropic`) by passing `budget=self._budget`.

- `coderouter/metrics/collector.py`:
  - Added `_provider_skipped_budget: Counter[str]` + `_chain_budget_exceeded_total: int`, symmetric with `_provider_skipped_paid` / `_chain_paid_gate_blocked_total`.
  - Added handlers for `skip-budget-exceeded` / `chain-budget-exceeded` events to `_dispatch`. `reset()` / `snapshot()` extended to include both counters.
  - Added 2 v1.10 rows to the module docstring's event inventory.

- `coderouter/metrics/prometheus.py`:
  - Added `coderouter_provider_skipped_total{provider, reason="budget"}` alongside the existing `paid` / `unknown` counters (so dashboards can stack by reason).
  - New `coderouter_chain_budget_exceeded_total` scalar counter, symmetric with `coderouter_chain_paid_gate_blocked_total`.

- New `tests/test_budget.py` (~340 LOC, +8 tests):
  - **Group 1 (pure BudgetTracker)**: record accumulation / `>=` boundary semantics for is_over_budget / month-boundary rollover (deterministically verifying an April→May crossing via the `now=` argument).
  - **Group 2 (CostConfig schema)**: accepts `monthly_budget_usd: 5.0`, rejects negative values (pydantic `ge=0.0`).
  - **Group 3 (engine integration)**: a pre-loaded budget still skips the primary and falls back (no warn) / all providers capped raises `NoProvidersAvailableError` + a single `chain-budget-exceeded` warn / confirms real attempt costs accumulate in `BudgetTracker` and trigger a skip on the 3rd call (an end-to-end test of real wiring).

#### Files touched

```
A  coderouter/routing/budget.py
A  tests/test_budget.py
M  CHANGELOG.md
M  coderouter/config/schemas.py
M  coderouter/logging.py
M  coderouter/metrics/collector.py
M  coderouter/metrics/prometheus.py
M  coderouter/routing/fallback.py
```

#### Why now

Item #3 in the v1.10 work order in `docs/inside/future.md §6.6`. Adding **enforcement** right after v1.9-D built the observation foundation is a natural sequence, and it's the highest-value v1.10 candidate for cost-aware users (operators incorporating paid backends). Where LiteLLM implements equivalent functionality with substantial weight inside `litellm[proxy]` (requiring Redis), CodeRouter avoids that structural debt by staying in-memory and within the 5-deps budget, embracing its role as a "budget guard for individual developers."

#### Out of scope

- **Persistent budget state** (sqlite / Redis / disk-backed) — not supported, per the 5-deps invariant. Cases needing durable enforcement can instead feed the v1.9-D dashboard into external alerting.
- **Rolling 30-day window** — the UTC calendar month is sufficient (matches typical billing cycles, and keeps the month-boundary rollover implementation simple). A rolling window could be added by swapping `_utc_month_key` for a date-windowed key, but only once operators request it.
- **Per-profile budget** (vs. per-provider) — per-provider is sufficient. When multiple profiles share the same provider, the budget should be shared too (since the actual cost attributes to the provider), so provider-level attribution is also semantically correct.

---

## [v1.9.1] — 2026-05-01 (Patch — pre-emptively harvesting 2 quick wins from the v1.10 candidate list)

**Theme: bundle 2 quick wins from the backlog organized as "v1.10 candidates" at v1.9.0 GA — completing streaming cache observation and letting agent-driven model identifiers branch profiles — into a patch, since neither carries structural debt.** Adds, with full compatibility, a path for closing the gap left in the observation loop and for letting agent-side settings (Claude Code / Cursor etc. choosing between Opus / Sonnet / Haiku) feed into CodeRouter's declarative routing. Both features extend v1.9.0's existing framework (`cache-observed` log / `auto_router.rules`) — no new framework, no new dependencies.

2 shipments included (items #1, #2 in the v1.10 work order in `docs/inside/future.md §6.6`):

| # | sub-release | Theme | LOC | tests |
|---|---|---|---|---|
| 1 | **v1.9-B2** | Usage aggregation for the streaming path — `_StreamUsageAccumulator` + `_emit_cache_observed_streaming` replace the `outcome=unknown` placeholder with observed values | ~150 | +3 |
| 2 | **per-model auto-routing** | Added `RuleMatcher.model_pattern` as the 5th matcher, evaluating the body's model id via `re.fullmatch` (from free-claude-code) | ~120 | +5 |

- Tests: 830 → **838** (+8 cumulative: v1.9-B2 +3 / per-model +5)
- Runtime deps: 5 → 5 (30 consecutive sub-releases unchanged)
- Backward compat: fully compatible; existing yaml / API / log payloads all use the same schema as before; deployments that don't use the new `model_pattern` field behave exactly as before
- pyproject version: 1.9.0 → 1.9.1

### Migration

None needed. **A natural upgrade from v1.9.0 / v1.9.0a\***:

- The `coderouter` command name / Python import name / providers.yaml format / env vars / ingress URL are all unchanged
- For external consumers reading the `cache-observed` log on the streaming path (e.g., dashboard / Prometheus / a custom JSONL parser), the fields that were previously fixed at zero through v1.9.0a6 (`cache_read_input_tokens` / `cache_creation_input_tokens` / `input_tokens` / `output_tokens` / `outcome` / `cost_usd` / `cost_savings_usd`) now carry observed values. On the consumer side, this is purely **more accurate numbers** — the schema is unchanged, no logic changes needed
- Adopting `auto_router.rules[].if.model_pattern` only requires adding one line to the yaml; no effect on existing rules

### Out of scope (v1.10 / v1.9.x follow-up)

The remaining 3 v1.10 candidates noted in the v1.9.0 GA notes and `docs/inside/future.md §6.6`:

- **Provider monthly budget cap** (from LiteLLM, cumulative version of v1.9-D) — a per-provider running total via `monthly_budget_usd`, with skip + log on overage. ~400 LOC, 3-5 days.
- **v1.9-E phase 2** — L2 Memory pressure (LM Studio / Ollama backend OOM detection) / L5 Backend health (continuous probing + chain reorder). The pillar that completes **the core of the Vision (the agent loop won't stop even after 8 hours)**. ~900 LOC, 1-2 weeks.
- **longContext auto-switch** — adding a `content_token_count_min` matcher as the 5th `auto_router` rule type (incorporating a claude-code-router task-based idea). ~200 LOC, 3-5 days.

Since these 3 involve structural extensions, they're planned to ship as individual sub-releases in the v1.10.0 minor rather than this v1.9.1 patch.

### Files touched

```
M  CHANGELOG.md
M  coderouter/config/schemas.py
M  coderouter/routing/auto_router.py
M  coderouter/routing/fallback.py
M  docs/inside/future.md
M  plan.md
M  pyproject.toml
M  tests/test_auto_router.py
M  tests/test_fallback_cache_observed.py
```

---

### per-model auto-routing (v1.10 candidate #2, from free-claude-code)

**Theme: add the `model` field the agent sends as another axis auto_router can decide on.** Lets agent-side settings (Claude Code / Cursor etc. choosing between Opus / Sonnet / Haiku) also drive which profile chain CodeRouter selects. Introduces `auto_router.rules[].if.model_pattern` as the 5th matcher, inheriting the same "exactly one" contract and eager regex compile (typos fast-fail at startup) as the existing 4 (`has_image` / `code_fence_ratio_min` / `content_contains` / `content_regex`).

Example use case:

```yaml
auto_router:
  rules:
    - if: { model_pattern: "claude-3-5-haiku.*" }
      route_to: lightweight
    - if: { model_pattern: "claude-3-5-sonnet.*" }
      route_to: coding
  default_rule_profile: writing
```

This lets CodeRouter cleanly ride along in situations where "the choice of model is already decided" on the agent side. It brings a similar feature from the `free-claude-code` repo into CodeRouter's declarative auto_router framework.

- Tests: 833 → **838** (+5: Sonnet→coding / Haiku→lightweight / no-model-field fallthrough / invalid regex fast-fails at schema load / first-match-wins precedence between model_pattern and a content rule)
- Runtime deps: 5 → 5 (30 consecutive sub-releases unchanged)
- Backward compat: fully compatible; existing `auto_router` rules are unaffected; deployments that don't use `model_pattern` behave exactly as before

#### Changes

- `coderouter/config/schemas.py`:
  - Added `model_pattern: str | None = None` to `RuleMatcher`, added to the `_MATCHER_FIELDS` tuple (the existing zero/multiple-fields "exactly one" validator applies automatically).
  - Extended the `_compile_regex_eagerly` validator to also cover `model_pattern`; an invalid regex fires `ValueError("Invalid regex for model_pattern ...")` at schema load (same fast-fail pattern as `content_regex`).
  - Documented `model_pattern` as the 5th entry in the docstring's Variants section, clarifying `re.fullmatch` semantics versus `content_regex`'s `re.search` (model identifiers are "structured tokens," so full-match semantics fit better).

- `coderouter/routing/auto_router.py`:
  - Added a `_extract_model(body)` helper — extracts the body's top-level `model` field in one place for both ingress shapes (Anthropic `/v1/messages` / OpenAI `/v1/chat/completions`); empty string / non-str values resolve to None.
  - Added `model: str | None` to the `_match_rule(rule, message, text, model)` signature, implementing the `model_pattern` matcher as the 5th branch. Evaluated via `re.fullmatch` (since model ids are structured tokens, full-match semantics are more intuitive than partial match). Returns False when `model is None`, falling through (guards against test fixtures with an empty body, etc.).
  - `classify(...)` now calls `_extract_model(body)` once and threads it into `_match_rule`. The `model_pattern` rule is evaluated even when `user_msg is None` (allows routing via the model even with empty messages).
  - Added `model` to the `signals` payload in `_emit_resolved` / `_emit_fallthrough`, so the auto-router-resolved log lets the dashboard / Prometheus exporter see which model id drove a routing decision.

- Added Group 6 (per-model auto-routing) to `tests/test_auto_router.py`, 5 cases:
  - `test_classify_model_pattern_sonnet_routes_to_coding` — basic case: `claude-3-5-sonnet.*` → coding profile, with the model rule winning even when content leans toward writing.
  - `test_classify_model_pattern_haiku_routes_to_lightweight` — a 4-profile fixture (adding lightweight via `_model_pattern_config`), Haiku id → lightweight profile.
  - `test_classify_model_pattern_no_model_field_falls_through` — when the body has no `model` field, even `r".+"` doesn't match, falling through to default_rule_profile (robustness for fixtures / test harnesses).
  - `test_model_pattern_invalid_regex_fast_fails_at_load` — `r"([unclosed"` raises `ValueError(model_pattern)` at `RuleMatcher` construction (same eager compile path as `content_regex`).
  - `test_model_pattern_first_match_wins_over_later_content_rule` — placing the model_pattern rule before a content_contains rule makes it win even on a body that matches both, pinning the global "first match wins" rule.

#### Files touched

```
M  CHANGELOG.md
M  coderouter/config/schemas.py
M  coderouter/routing/auto_router.py
M  tests/test_auto_router.py
```

#### Why now

The second-recommended quick win in the v1.10 work order in `docs/inside/future.md §6.6`. Implementation size ~120 LOC (came in under the ~150-200 LOC estimate), +5 tests, half a day of effort. Adding a single matcher to the existing auto_router framework carries no structural debt, and it incorporates the `free-claude-code`-derived request without compromising CodeRouter's declarative design. Positioned as groundwork ahead of the next v1.10 candidates (provider monthly budget / longContext auto-switch / v1.9-E phase 2).

---

### v1.9-B2: usage aggregation for the streaming path (v1.10 candidate #1)

**Theme: recover the quick win that v1.9.0 deliberately deferred to the v1.10 candidate list.** v1.9.0a6 got as far as "emitting the `cache-observed` log on the streaming path too," but the token counts were still an `outcome=unknown` placeholder fixed at zero. This patch aggregates `message_start.message.usage` plus the terminal `message_delta.usage` via an accumulator using per-field max-merge, matching the same outcome classification + cost calculation + log payload shape as the non-streaming path (`generate_anthropic`). `/dashboard` / Prometheus / MetricsCollector can now get real numbers from the streaming path with no branching needed.

- Tests: 830 → **833** (+3: streaming aggregation for cache_hit / cache_creation / no_cache — in `tests/test_fallback_cache_observed.py`)
- Runtime deps: 5 → 5 (29 consecutive sub-releases unchanged)
- Backward compat: fully compatible; the log payload uses the same schema as v1.9-A; only the meaning of the `streaming=true` flag changes, now reflecting "observed values" rather than a zero placeholder

#### Changes

- `coderouter/routing/fallback.py`:
  - New `_StreamUsageAccumulator` — aggregates `input_tokens` / `output_tokens` / `cache_read_input_tokens` / `cache_creation_input_tokens` from `message_start.message.usage` and `message_delta.usage` via per-field max-merge. `output_tokens` is only final at the terminal `message_delta`, so max is safe; cache fields may appear in either `message_start` or `message_delta` depending on API minor version, so both are observed. `usage_present` tracks whether upstream returned usage at all (including an empty dict), so a streaming response with nothing observed still classifies as `outcome=unknown`.
  - Added `_emit_cache_observed_streaming(...)` — feeds accumulator values through `classify_cache_outcome` / `compute_cost_for_attempt` and calls `log_cache_observed`, using the same outcome classification + cost calculation logic as the non-streaming `_emit_cache_observed`.
  - In the loop inside `stream_anthropic(...)`, initializes `acc = _StreamUsageAccumulator()` and calls `acc.observe(...)` for `first` and every subsequent `event_iter` event. Replaces the completion-time `log_cache_observed(..., outcome="unknown", *=0)` with `_emit_cache_observed_streaming(acc, ..., provider_config=adapter.config)`.
  - Updated the `_emit_cache_observed` docstring — revised to explain that the `streaming=True` arg remains for the openai_compat path (which collapses into a single response via downgrade).

- `tests/test_fallback_cache_observed.py`:
  - Changed `_CacheAnthropicAdapter.stream_anthropic` to be constructor-argument driven (feeding input_tokens + cache fields into `message_start.message.usage`, and input_tokens + output_tokens into `message_delta.usage`; emits an empty dict when zero, so "no usage at all" can be reproduced).
  - Updated the docstring of the existing `test_cache_observed_fires_on_streaming_with_unknown_outcome` test to the v1.9-B2 context (pinning the `unknown` floor for when upstream never sends any usage).
  - 3 new cases:
    - `test_streaming_aggregates_cache_hit_usage` — a stream including `cache_read_input_tokens=2048` → `outcome=cache_hit` + input/output counter aggregation.
    - `test_streaming_aggregates_cache_creation_usage` — a stream with `cache_creation_input_tokens=1500` → `outcome=cache_creation`.
    - `test_streaming_aggregates_no_cache_outcome` — non-zero usage with no cache fields → `outcome=no_cache` (the most common production case, which the v1.9.0a6 placeholder failed to capture).

#### Why now

The shortest-term quick win among those explicitly flagged as "v1.10 candidates" in the v1.9.0 GA notes. At ~150 LOC and half a day of effort, replacing the `outcome=unknown` placeholder with observed values completes streaming-path coverage for the cost dashboard / cache-hit rate panel. Clearing this ahead of higher-priority work like `v1.9-E phase 2` (L2/L5) or per-model auto-routing raises the completeness of subsequent adaptive routing / Vision pillar work.

#### Out of scope

- The `ChatRequest.stream()` path (OpenAI-shaped streaming) is out of scope — it's a sibling of `stream_anthropic`, and cache observation via Anthropic remains unaddressed there. Clients that use Anthropic prompt caching effectively go through `/v1/messages`, so the impact is limited.
- The "`synthesize_anthropic_stream_from_response` after downgrade" path discussed in v1.9.0a6 — since the `message_start` event is reconstructed with usage from the underlying AnthropicResponse, the accumulator covers it automatically (no additional implementation needed).

#### Files touched

```
M  CHANGELOG.md
M  coderouter/routing/fallback.py
M  tests/test_fallback_cache_observed.py
```

---

## [v1.9.0] — 2026-04-29 (Umbrella tag — Cache observability + Adaptive routing + Cost-aware + Long-run reliability)

**Theme: align "observe → understand → act → reliability" in a single minor release, maturing the observability pillar.** Across 6 sub-releases (v1.9-A through E), v1.9.0 lifts CodeRouter from "it's running but we can't tell what's happening" to a state where **"how much was spent on what / where it slowed down / where it got stuck"** is visible from a single operational log line. Specifically:

- **Observation (v1.9-A)** — records Anthropic prompt cache hit/miss for every request in the `cache-observed` log; hit_rate / saved tokens are visible from `/dashboard`
- **Transparency (v1.9-B)** — preserves Anthropic extensions like cache_control / thinking as much as possible even on the openai_compat path, explicitly flagging with `capability-degraded` when not possible
- **Dynamic optimization (v1.9-C)** — setting `adaptive: true` on a profile automatically demotes a normally-fast provider that's temporarily slowed down, protecting user-felt latency
- **Cost visibility (v1.9-D)** — declare USD pricing via `cost:` in providers.yaml; cache savings are computed separately (a granularity that competing products like LiteLLM miss) and shown on the dashboard
- **Reliability guard (v1.9-E phase 1, L3)** — detects "stuck loops" where the same tool is called repeatedly with the same arguments, handled via profile-level policy (`warn` / `inject` / `break`)

For this final v1.9.0 GA, a real-machine verification issue discovered after v1.9.0a6 — **the L3 `break` action failing to catch at ingress** (`ToolLoopBreakError` went uncaught, returning a 500) — was fixed to return 400 with structured detail, aligned across both ingress paths (non-streaming HTTPException / streaming SSE error event).

- Tests: 828 → **830** (+2: break action non-streaming 400 / streaming SSE error event)
- Runtime deps: 5 → 5 (29 consecutive sub-releases unchanged)
- Backward compat: fully compatible; no changes to profile / providers.yaml / API
- This GA rolls up v1.9.0a1 through a6; see the alpha entries further down this file for details of each sub-release

### Changes since v1.9.0a6 — fixing the E-4 break-action ingress gap

#### `coderouter/guards/tool_loop.py`

- Added `threshold: int` / `window: int` as required keyword arguments to `ToolLoopBreakError.__init__`. Carries the detection parameters on the exception itself so the ingress side doesn't need to re-look-up config when building the 400 detail
- Documented in the docstring that "the Anthropic ingress catches this and converts it to 400 + structured detail" (promised in a3 but not actually implemented until now)

#### `coderouter/routing/fallback.py`

- Updated `_apply_tool_loop_guard`'s `raise ToolLoopBreakError(...)` to pass `threshold=profile.tool_loop_threshold, window=profile.tool_loop_window`

#### `coderouter/ingress/anthropic_routes.py`

- Imports `ToolLoopBreakError`
- Adds `except ToolLoopBreakError → HTTPException(status_code=400, detail=_tool_loop_break_detail(exc))` to the non-streaming `messages()` handler. `detail` is a flat dict:

  ```json
  {
    "error": "tool_loop_detected",
    "message": "tool loop detected on profile='test-loop-break': tool 'Read' repeated 3 times consecutively.",
    "profile": "test-loop-break",
    "tool_name": "Read",
    "repeat_count": 3,
    "threshold": 3,
    "window": 5
  }
  ```

  Clients can branch on `detail.error == "tool_loop_detected"`; `message` is identical to `str(exc)`, log-grep friendly
- Adds an `except ToolLoopBreakError` branch to the streaming `_anthropic_sse_iterator`, exposing structured fields via the standard Anthropic envelope (`error.type == "invalid_request_error"`) nested under `error.tool_loop`. HTTP status stays 200 (a StreamingResponse can't switch to 4xx once headers are committed — same constraint as the existing mid-stream-error handling)
- Two helpers: `_tool_loop_break_extension(exc)` (the detection payload shared by both formats) / `_tool_loop_break_detail(exc)` (builds the non-streaming flat dict)
- `args_canonical` is deliberately excluded from both formats (tool input can contain user data, so it must not leak into the 400 detail / SSE error event)

#### Tests

- **`tests/test_ingress_anthropic.py`** +2:
  - Added `_LoopBreakingEngine` class + `client_and_loop_breaking_engine` fixture
  - `test_break_action_non_streaming_returns_400_with_structured_detail` — verifies 400 + `detail.error="tool_loop_detected"` + the 5 detection fields + absence of `args_canonical`
  - `test_break_action_streaming_emits_invalid_request_error_event` — verifies 200 + a single SSE error event + the standard Anthropic envelope + `error.tool_loop` nesting + absence of `args_canonical`

### v1.9 series summary

| sub | release | feature |
|---|---|---|
| a1 | v1.9-A | Cache Observability — `cache-observed` log + dashboard panel |
| a2 | v1.9-B | Cross-backend cache passthrough + capability gate + doctor cache probe |
| a3 | v1.9-E phase 1 | L3 Tool-loop detection guard (warn / inject / break) |
| a4 | v1.9-C | Adaptive Routing — health-based dynamic chain priority |
| a5 | v1.9-D | Cost-aware Dashboard — Anthropic prompt-cache aware |
| a6 | v1.9-A streaming patch | Added `_emit_cache_observed` to `stream_anthropic` (fixing a missed implementation) |
| **GA** | **v1.9-E phase 1 patch** | **Fixed the `break` action's ingress 400 gap** (this entry) |

### Real-machine verification (2026-04-29, LM Studio + Ollama)

```
E-2 (warn):    tool-loop-detected ... action: "warn"   → 200 OK + provider response
E-3 (inject):  tool-loop-detected ... action: "inject" → hint appended to system + 200 OK
                                                         + cache_read_input_tokens: 453 (prefix cache hit)
E-4 (break, non-stream): 400 + {"detail":{"error":"tool_loop_detected","profile":"test-loop-break",
                                           "tool_name":"Read","repeat_count":3,...}}
E-4 (break, stream):     200 + event: error
                               data: {"type":"error","error":{"type":"invalid_request_error",
                                      "tool_loop":{"profile":"test-loop-break","repeat_count":3,...}}}

C  (adaptive, idle):  all providers same speed → static order maintained, no `adaptive-routing-applied` fired
C  (adaptive, triggered): a chain with mixed sizes (lmstudio 27B-dense 474ms / ollama qwen-coder-1.5b 134ms / openrouter-free n/a)
                      → global_median 304ms × 1.5 = 456ms; lmstudio's 474ms ≥ 456ms → demote +1
                      → effective_order: [ollama-qwen-coder-1_5b, openrouter-free, lmstudio-...]
                      → switched to routing through ollama-qwen-coder-1_5b starting from the 4th trial run,
                         and no oscillation was observed with the 30s debounce
```

E-2/E-3 were already observed in a3; E-4 (both formats) and the C trigger path were observed on real hardware for the first time just before GA. verification.md is planned to get a follow-up addendum covering the MoE model trap (Qwen3.6-35B-A3B is fast because only 3.8B is active) and rolling-window timing caveats (not included in this release).

### Migration

None needed. **A natural upgrade from v1.8.x / v1.9.0a\***:

- The `coderouter` command name / Python import name / providers.yaml format / env vars / ingress URL are all unchanged
- Profiles that left `tool_loop_action` unset or set to `warn` / `inject` see no behavior change at all
- Only profiles that were already using `tool_loop_action: break` see a status-code change from 5xx to 4xx (a3 through a6 had an implementation bug that returned a 500 Internal Server Error; 1.9.0 fixes it to the 400 + structured detail the docstring already promised). It's unlikely anyone had `break` in production use for real traffic; for verification purposes, the fixed behavior is the expected one

### Out of scope (v1.10+)

The v1.9 series deliberately closes here:

- **v1.9-B2** — aggregating usage from `message_delta` events to get real token counts / cache_read / cache_creation on the streaming path too (currently fixed at `outcome=unknown`)
- **v1.9-E phase 2** — L2 Memory pressure (LM Studio / Ollama backend OOM detection) / L5 Backend health (continuous probing + chain reorder)
- **v1.10-?** — the plan.md §13 lineage (multi-tenant routing, etc.) — a separate minor release

### Files touched

```
M  CHANGELOG.md
M  coderouter/guards/tool_loop.py
M  coderouter/ingress/anthropic_routes.py
M  coderouter/routing/fallback.py
M  pyproject.toml
M  tests/test_ingress_anthropic.py
```

---

## [v1.9.0a6] — 2026-04-28 (Patch for a missed v1.9-A cache-observed emit on the streaming path)

**Theme: close a small implementation gap in v1.9-A found during real-machine verification.** The v1.9-A CHANGELOG / `CacheOutcome` docstring promised that "streaming responses are recorded with `outcome=unknown`," but the call to `_emit_cache_observed` was never actually added to the `stream_anthropic` path (only the non-streaming `generate_anthropic` had it). This was discovered when a real `curl -N stream:true` request produced no `cache-observed` event in the JSONL. This patch brings the implementation in line with what the docs already promised.

- Tests: 826 → **828** (+2: emit on streaming success / no emit on streaming failure)
- Runtime deps: 5 → 5 (28 consecutive sub-releases unchanged)
- Backward compat: fully compatible; no changes to profile / API
- Pre-release: `1.9.0a6`

### Changes

#### Added cache-observed emit to `stream_anthropic` in `coderouter/routing/fallback.py`

- Right after `_apply_tool_loop_guard`, hoisted `request_had_cache_control = anthropic_request_has_cache_control(request)` into a variable (avoiding double evaluation between the existing v0.5-B inline call and the new emit caller)
- Calls `log_cache_observed(...)` at the end of a successful stream (right before `return`, after `async for ev in event_iter` completes)
  - `outcome="unknown"` (per the promise that streaming won't get real usage until v1.9-B aggregates `message_delta`)
  - `streaming=True`
  - All token counts are 0 (the engine doesn't aggregate usage on the streaming path yet, so cost is also 0)
- No effect on the non-streaming `generate_anthropic` behavior

#### Tests

- **`tests/test_fallback_cache_observed.py`** +2:
  - `test_cache_observed_fires_on_streaming_with_unknown_outcome` — a successful stream records `outcome=unknown` / `streaming=True` / `request_had_cache_control=True`
  - `test_cache_observed_streaming_does_not_fire_on_provider_failure` — no emit on provider failure (same contract as non-streaming)
- To support the above, extended `_CacheAnthropicAdapter.stream_anthropic` from raising `NotImplementedError` to a "minimal stream yielding 3 events (start / delta / stop)"

### Why

Discovered during v1.9-A verification that "sending a stream:true curl produces no `cache-observed` log in the JSONL" (verification path A-3 in `docs/inside/verification.md`). Re-reading the v1.9-A `CacheOutcome` docstring showed it said "streaming responses always pair with `outcome=unknown` until v1.9-B aggregates `message_delta`," but the implementation only covered `generate_anthropic` — the emit call had simply been forgotten in `stream_anthropic`.

This is a **doc-implementation gap**: from a dashboard / metrics dashboard user's perspective, it looked inconsistent — "streaming is supposedly working, but no observation is recorded." v1.9.0a6 is a small patch to align the promise with the implementation.

As a side effect, this patch also made real-machine verification of A-3 (`hit_rate=null when only "unknown" observations`) possible for the first time.

### Migration

`pyproject.toml version 1.9.0a5 → 1.9.0a6`, `coderouter --version` returns 1.9.0a6. **Completely unchanged unless you touch your local `~/.coderouter-t/providers.yaml`.** No change to streaming-path response content either — just one additional log line.

### Files touched

```
M  CHANGELOG.md
M  coderouter/routing/fallback.py
M  pyproject.toml
M  tests/test_fallback_cache_observed.py
```

### Out of scope (deferred to v1.9-B)

- Aggregating `message_delta` events to get real token counts / cache_read / cache_creation on streaming too → surfacing real values for outcome instead of a fixed unknown

---

## [v1.9.0a5] — 2026-04-28 (v1.9-D: Cost-aware Dashboard — Anthropic prompt-cache aware)

**Theme: make "how much is being spent" visible, with cache savings broken out separately.** v1.9-A observed it, v1.9-B guaranteed transparency, and v1.9-D **translates it into money**. Implements Anthropic's prompt-cache pricing model (90% discount on cache_read, 25% premium on cache_creation) accurately from the start, structurally covering a weakness in competing products like LiteLLM, which **don't compute cache savings separately**.

Implements the v1.9-D scope from `docs/inside/future.md` §5.5.

- Tests: 811 → **826** (+15: 8 pure compute_cost / 4 collector dispatch / 3 Prometheus exposition)
- Runtime deps: 5 → 5 (27 consecutive sub-releases unchanged)
- Backward compat: fully compatible; the `cost:` field in `providers.yaml` is optional (unset = 0 contribution)
- Pre-release: `1.9.0a5`

### Changes

#### New `coderouter/cost.py` (~150 LOC)

- `CostBreakdown` dataclass — per-attempt cost components (input/output/cache_read/cache_creation USD + total + savings)
- `compute_cost_for_attempt(cost_config, *, input_tokens, ..., cache_creation)` pure function:
  - Computes each of the 4 token buckets at its respective rate
  - Discounts cache_read tokens by `input_rate × cache_read_discount`
  - Applies a premium to cache_creation tokens via `input_rate × cache_creation_premium`
  - savings = `cache_read tokens × input_rate × (1 - cache_read_discount)` (cache_creation carries a premium, so it isn't counted toward savings)
  - Defensive handling for negative tokens / None config / partial config

#### Schema: new `CostConfig`

- **`coderouter/config/schemas.py`**: `CostConfig` BaseModel declaring `input_tokens_per_million` / `output_tokens_per_million` / `cache_read_discount=0.10` / `cache_creation_premium=1.25`
- Added `ProviderConfig.cost: CostConfig | None = None` — opt-in; providers that leave it unset (e.g., local ones) contribute 0 to the dashboard

#### Engine integration

- **`coderouter/routing/fallback.py`**: extended `_emit_cache_observed` to accept a `provider_config: ProviderConfig | None = None` parameter, computing per-attempt USD cost + savings via `compute_cost_for_attempt()` and folding it into the log payload
- The `generate_anthropic` call site passes `adapter.config`

#### Logging schema extension

- **`coderouter/logging.py`**: added `cost_usd: float` / `cost_savings_usd: float` fields to `CacheObservedPayload` (default 0.0; pre-v1.9-D callers remain compatible with zero contribution)
- Added the corresponding optional kwargs to the `log_cache_observed` helper's signature

#### MetricsCollector: per-provider cost aggregation

- **`coderouter/metrics/collector.py`**: aggregates cost in the `cache-observed` event dispatch
  - `_cost_total_usd: dict[str, float]` (per-provider)
  - `_cost_savings_usd: dict[str, float]` (per-provider)
  - `_cost_total_usd_aggregate: float` / `_cost_savings_usd_aggregate: float` (process-wide)
- Extended `snapshot()`:
  - `counters.cost_total_usd` / `cost_savings_usd` (per-provider dict)
  - `counters.cost_total_usd_aggregate` / `cost_savings_usd_aggregate` (process-wide)
  - A `cost: {total_usd, savings_usd}` panel on each provider row
- `reset()` also clears v1.9-D state
- Defensive: malformed cost values (str/None) default to 0.0; the handler never raises

#### Prometheus exposition

- **`coderouter/metrics/prometheus.py`**: new `_counter_float()` helper (float-valued counter, `.10g` formatter trimming trailing zeros) + 2 new metrics:
  - `coderouter_cost_total_usd_total{provider}` — cumulative USD billed
  - `coderouter_cost_savings_usd_total{provider}` — cumulative cache savings USD

#### Tests (+15)

- New **`tests/test_metrics_cost.py`**:
  - `compute_cost_for_attempt`: None config / no cache / cache read discount / cache creation premium / combined / negative-tokens defensive / partial config (7)
  - Collector dispatch: per-provider aggregation / zero cost produces no entry / per-row cost panel / reset / malformed values (5)
  - Prometheus: HELP+TYPE / per-provider labels / `_total` suffix (3)

### Why

The concrete implementation of the plan established in `docs/inside/future.md` §5.5: "accurately compute cache savings from day one, something even LiteLLM doesn't support." Represents the Anthropic pricing model precisely as 4 token buckets x 4 multipliers, letting operators see on a single screen how much they've saved by mixing in local LLMs, and how much Anthropic prompt caching has saved them.

**Competitive landscape**:
- LiteLLM's cost tracker bills `cache_read_input_tokens` at the full input rate (overstating cost), with no separate savings computation
- claude-code-router has no cost tracking at all
- v1.9-D is **the only Claude-Code-family OSS project with a cache-aware cost dashboard**

### Migration

`pyproject.toml version 1.9.0a4 → 1.9.0a5`, `coderouter --version` returns 1.9.0a5. **Completely unchanged unless you touch your local `~/.coderouter-t/providers.yaml`.**

Operators who want to opt in explicitly add a `cost:` block to a paid provider:

```yaml
providers:
  - name: anthropic-direct
    kind: anthropic
    base_url: https://api.anthropic.com
    model: claude-sonnet-4-8
    api_key_env: ANTHROPIC_API_KEY
    paid: true
    cost:                              # new field in v1.9-D
      input_tokens_per_million: 3.00
      output_tokens_per_million: 15.00
      cache_read_discount: 0.10        # default, can be omitted
      cache_creation_premium: 1.25     # default, can be omitted
```

After starting `coderouter serve`, per-provider cost is available at `/metrics.json` under `counters.cost_total_usd` / `cost_savings_usd`. A Prometheus scrape gets it via `coderouter_cost_total_usd_total{provider="anthropic-direct"}`.

### Files touched

```
M  CHANGELOG.md
M  coderouter/config/schemas.py
M  coderouter/logging.py
M  coderouter/metrics/collector.py
M  coderouter/metrics/prometheus.py
M  coderouter/routing/fallback.py
M  pyproject.toml
A  coderouter/cost.py
A  tests/test_metrics_cost.py
```

### Out of scope (future)

- **`/dashboard` HTML cost panel**: the snapshot schema is ready, but UI rendering is planned for v1.9-D2
- **`coderouter stats --cost` TUI**: a 5-line summary CLI command, planned for v1.9-D2
- **Period-based accumulation (1 day / 1 week / 1 month)**: currently process-lifetime only. Period-based aggregation is a v1.10 candidate to pair with SQLite persistence
- **Cost aggregation for OpenAI-shaped engine paths**: only the Anthropic non-streaming path is covered. OpenAI ingress + streaming support is the same follow-up item as v1.9-C2

---

## [v1.9.0a4] — 2026-04-28 (v1.9-C: Adaptive Routing — health-based dynamic chain priority)

**Theme: bring "steady-state optimization" into the chain.** Automatically re-prioritizes the statically declared `providers` order based on live-observed median latency / error rate. Whereas L5 (planned for v1.9-E phase 3) handles crashes as a binary (HEALTHY/UNHEALTHY), C absorbs **steady-state slowness** as a continuous gradient. Both run off the same observation stream, but their application logic is orthogonal.

Implements the v1.9-C MVP scope from `docs/inside/future.md` §5.4. **Only the Anthropic non-streaming path** is covered (OpenAI-shaped + streaming follow-up planned for v1.9-C2).

- Tests: 795 → **811** (+16: 4 stats / 3 no-demote / 2 latency demote / 2 error-rate demote / 2 debounce / 2 engine integration / 1 constants pin)
- Runtime deps: 5 → 5 (26 consecutive sub-releases unchanged)
- Backward compat: fully compatible; existing profiles keep prior behavior via the `adaptive: false` default
- Pre-release: `1.9.0a4`, available via `pip install --pre coderouter-cli`

### Changes

#### New `coderouter/routing/adaptive.py` (~360 LOC)

- `AdaptiveAdjuster` class — a per-process singleton (the engine holds one)
  - `record_attempt(provider, *, latency_ms, success, now=None)` — records an observation, appended on every engine attempt
  - `stats_for(provider, *, now=None) -> ProviderStats` — computes median latency + error rate from the rolling window
  - `compute_effective_order(adapters, *, now=None) -> list[BaseAdapter]` — turns the static chain into a dynamic order, applying debounce
- `_ProviderObservation` / `_AdjusterState` / `ProviderStats` dataclasses
- `_apply_debounce` internal method — pins rank changes within the debounce window by comparing against `last_committed_rank` (in both directions, demote→promote and promote→demote)
- Constants:
  - `ROLLING_WINDOW_S = 60.0`
  - `LATENCY_DEMOTE_FACTOR = 1.5` (demote 1 rank once median × 1.5 is exceeded)
  - `ERROR_RATE_DEMOTE_THRESHOLD = 0.10` (demote 2 ranks at 10% failure)
  - `DEBOUNCE_S = 30.0`
  - `MIN_SAMPLES_FOR_LATENCY = 3` / `MIN_SAMPLES_FOR_ERROR_RATE = 5`

#### Engine integration (`coderouter/routing/fallback.py`)

- Eagerly constructs `_adaptive_adjuster: AdaptiveAdjuster` in `FallbackEngine.__init__`. Also provides a lazy-fallback `@property` `_adaptive` (resilient against the legacy test `__new__`-bypass pattern)
- `_resolve_anthropic_chain`: when a profile has `adaptive: true`, re-prioritizes the chain via `_adaptive.compute_effective_order(base)` before passing it on to the thinking-capable bucket logic
- `_profile_is_adaptive(profile_name)` helper — shared profile lookup between the chain resolver and the recording side
- Wraps the adapter call in `generate_anthropic` with `time.monotonic()`, calling `record_attempt(...)` on both success and failure. Auth-flavored failures (401/403) are recorded with latency_ms=None (a short-circuit response, meaningless as a latency signal)

#### Logging

- New `adaptive-routing-applied` (info-level) event — fires only when the static chain and effective chain order differ. Payload includes static_order / effective_order / per-provider stats

#### Config schema

- Added `FallbackChain.adaptive: bool = False`. Existing yaml keeps working unchanged (defaults to false)

#### Tests

- New **`tests/test_routing_adaptive.py`** (+16 tests):
  - **Stats**: unseen / median uses only successes / window roll-off / error rate zero on empty (4)
  - **No demote**: empty chain / no obs / all fast (3)
  - **Latency demote**: 1.5x threshold / min samples gate (2)
  - **Error rate demote**: 10% threshold / min samples gate (2)
  - **Debounce**: pin within window / release after window (2)
  - **Engine integration**: static profile doesn't invoke the adjuster / adaptive profile invokes the adjuster (2)
  - **Constants pin**: ROLLING_WINDOW_S / LATENCY_DEMOTE_FACTOR / ERROR_RATE_DEMOTE_THRESHOLD / DEBOUNCE_S / MIN_SAMPLES_* (1)

### Why

Implements the health-based half of the "task-based (auto_router, v1.6-A) + health-based (v1.9-C) dual axis" plan established in `docs/inside/future.md` §5.4. auto_router picks a profile by request shape (intent), but priority within a profile's chain stayed static. v1.9-C lets in-chain priority track live-observed health, so the two axes finally complement each other.

**Competitive landscape**: claude-code-router is task-based only, LiteLLM is session-cost-based; neither has latency-aware adaptive routing. With v1.9-C, CodeRouter is positioned as the only Claude-Code-family OSS with **both task-based and health-based axes**.

### Migration

`pyproject.toml version 1.9.0a3 → 1.9.0a4`, `coderouter --version` returns 1.9.0a4. **Completely unchanged unless you touch your local `~/.coderouter-t/providers.yaml`.** Since the new `adaptive: false` field defaults to false, existing profiles keep prior behavior with zero changes.

Operators who want to opt in explicitly add this to a profile:

```yaml
profiles:
  - name: coding
    providers:
      - lmstudio-qwen3-5-9b
      - ollama-gemma4-26b
      - openrouter-free
    adaptive: true   # dynamic priority based on steady-state latency / error rate
```

### Files touched

```
M  CHANGELOG.md
M  coderouter/config/schemas.py
M  coderouter/routing/fallback.py
M  pyproject.toml
A  coderouter/routing/adaptive.py
A  tests/test_routing_adaptive.py
```

### Out of scope (future v1.9-C2)

- **OpenAI-shaped engine paths**: `record_attempt` calls from `generate` / `stream` (non-Anthropic ingress). The MVP covers only Anthropic non-streaming
- **Anthropic streaming**: latency measurement for `stream_anthropic` (design question of where to draw the mid-stream success boundary)
- **Dashboard panel**: visualizing the effective chain order in `/dashboard` (highlighting the diff between "static order vs. current effective order")
- **Adaptive aggregation in MetricsCollector**: currently only the `adaptive-routing-applied` log exists; future work would aggregate reorder counts / most-recent reorder timestamp for a dashboard panel
- **L5 (v1.9-E phase 3)**: binary HEALTHY/UNHEALTHY backend swap. Designed to coexist with this implementation's continuous gradient, both consuming the same observation stream

---

## [v1.9.0a3] — 2026-04-28 (v1.9-E phase 1: L3 Tool-loop detection guard)

**Theme: the first Long-run reliability guard.** v1.9-E in `docs/inside/future.md` §5.3 is a 1-2 week chunk of work covering 3 failure classes: L2/L3/L5. Doing it all in one commit would be too heavy, so it's split into an alpha pre-release across 3 stages: **L3 (Tool loop detection) → L2 (Memory pressure) → L5 (Backend health)**.

L3 is the most isolated, with no HTTP-related dependencies, ~300 LOC, self-contained. It's the first concrete implementation toward the pitch of "keep using Claude Code against a local LLM for 8 hours straight without it getting stuck."

- Tests: 779 → **795** (+16: 8 pure detect / 3 inject mutation / 5 engine helper)
- Runtime deps: 5 → 5 (25 consecutive sub-releases unchanged)
- Backward compat: fully compatible; no `providers.yaml` edits required (all new fields have defaults)
- Pre-release: `1.9.0a3`, available via `pip install --pre coderouter-cli`

### Changes

#### New `coderouter/guards/` package + L3 detector

- New **`coderouter/guards/__init__.py`** — package home for Long-run guards. Reserves space for L2 / L5 to be added later.
- New **`coderouter/guards/tool_loop.py`** (~250 LOC):
  - `detect_tool_loop(request, *, window, threshold) -> ToolLoopDetection | None` pure function. Detects when the same `(name, args)` occurs `threshold`+ times in the **trailing consecutive run** of assistant `tool_use` blocks within the last `window` entries
  - `ToolUseRecord` / `ToolLoopDetection` dataclasses
  - `inject_loop_break_hint(request, *, hint)` — appends a hint to the system field (handles all 3 shapes: str / None / list-of-blocks)
  - `ToolLoopBreakError` (a `CodeRouterError` subclass) — the exception for the `break` action
  - `DEFAULT_LOOP_INJECT_HINT` constant — "You appear to be calling the same tool with the same arguments repeatedly..."
  - **Canonical-form JSON comparison** (`json.dumps(args, sort_keys=True)`) treats `{"a":1,"b":2}` and `{"b":2,"a":1}` as identical
  - **Trailing-run-only** detection — ignores streaks that were already broken in the past (only the current state is actionable)

#### Engine integration

- **`coderouter/routing/fallback.py`**: added an `_apply_tool_loop_guard(request, config)` helper, called right before chain dispatch in `generate_anthropic` / `stream_anthropic`. Behavior by action:
  - `warn`: log only, request passed through unchanged
  - `inject`: log + returns a new request with the system prompt modified via `inject_loop_break_hint`
  - `break`: log + `raise ToolLoopBreakError`
- Silent no-op on profile lookup failure (chain resolution surfaces the error through a separate path, avoiding double diagnostics)

#### Config schema

- Extended `FallbackChain` in **`coderouter/config/schemas.py`**:
  - `tool_loop_window: int = 5` (range 2-50)
  - `tool_loop_threshold: int = 3` (range 2-50)
  - `tool_loop_action: Literal["warn", "inject", "break"] = "warn"`
- All existing profiles default to warn-only → zero change for existing deployments

#### Logging

- **`coderouter/logging.py`**: new `tool-loop-detected` warn-level log shape
  - `ToolLoopDetectedPayload` TypedDict (profile / tool_name / repeat_count / threshold / window / action)
  - `log_tool_loop_detected()` helper — a single chokepoint
- All 3 actions fire the same log line, so the dashboard can capture every detection (with action as a distinguishing label)

### Why

The first concrete implementation of P3 (Long-run Reliability) from the Vision established in `docs/inside/future.md` §1 — "a reliability layer for running agents on local LLMs over long sessions." L3 is the most isolated, simplest to implement, easiest to test, and valuable on its own, making it the natural first sub-release.

"Claude Code keeps Reading the same file 5 times" or "keeps hitting the same Bash command 3 times without stopping" are typical symptoms in long-running agent loops, and L3 closes the detection loop using request shape alone (Claude Code sends the full conversation history every time, so tail inspection is sufficient).

**Competitive landscape** (referenced in future.md §3): as of 2026-04-27, zero Claude-Code-family OSS projects in the survey list systematically address L3. This implementation stands as a distinct differentiator.

### Migration

`pyproject.toml version 1.9.0a2 → 1.9.0a3`, `coderouter --version` returns 1.9.0a3. **Completely unchanged unless you touch your local `~/.coderouter-t/providers.yaml`.** All new schema fields have defaults, so existing yaml loads as-is, and since the default action is warn level (logging only), there's no side effect on existing processing.

Operators who want to opt in explicitly add this to a profile:

```yaml
profiles:
  - name: long-running-agent
    providers: [...]
    tool_loop_window: 5
    tool_loop_threshold: 3
    tool_loop_action: inject   # or warn / break
```

### Files touched

```
M  CHANGELOG.md
M  coderouter/config/schemas.py
M  coderouter/logging.py
M  coderouter/routing/fallback.py
M  pyproject.toml
A  coderouter/guards/__init__.py
A  coderouter/guards/tool_loop.py
A  tests/test_guards_tool_loop.py
```

### Out of scope (future v1.9-E phases)

- **L2 (Memory pressure awareness)**: backend memory probing by reading Ollama `/api/ps` / LM Studio `/v1/models` / llama.cpp `/proc/meminfo` directly, swapping to a lighter model above 95%
- **L5 (Backend health continuous monitoring)**: a 60s-cycle health probe, demoting UNHEALTHY to the end of the chain / restoring original priority on recovery, with effective chain order shown on the dashboard
- **Loop-event aggregation in MetricsCollector**: currently structured logs only; future dashboard panel would show "N loop detections in the last 24h"
- **Operator override of the inject hint**: currently only `DEFAULT_LOOP_INJECT_HINT`; a future profile-level `tool_loop_inject_hint` would allow localization (e.g., Japanese) etc.

---

## [v1.9.0a2] — 2026-04-28 (v1.9-B: Cross-backend cache passthrough + capability gate + doctor cache probe)

**Theme: upgrade v1.9-A's "observation" into a "guarantee."** Introduces a `cache_control` field in the capability registry, bundling declarations for the Claude 4 family + Qwen3.5/3.6 via LM Studio. Adds a new doctor probe, `_probe_cache`, verifying the cache_control round trip on real hardware (1st call creates, 2nd call reads).

Implements the v1.9-B scope from `docs/inside/future.md` §5.2. The only behavior change is the extended capability gate; existing `provider_supports_cache_control` calls remain backward compatible (an anthropic-kind provider not declared in the registry still returns True).

- Tests: 759 → **779** (+20: 12 registry resolution / 8 doctor cache probe)
- Runtime deps: 5 → 5 (24 consecutive sub-releases unchanged)
- Backward compat: fully compatible; no changes to `providers.yaml` / API
- Pre-release: `1.9.0a2`, available via `pip install --pre coderouter-cli`

### Changes

#### Capability registry: new `cache_control` field

- **`coderouter/config/capability_registry.py`**: added a `cache_control: bool | None` field to `RegistryCapabilities` / `ResolvedCapabilities`. Added the same field to the lookup walker (following the existing first-match-per-flag semantics).
- **`coderouter/data/model-capabilities.yaml`**: 5 bundled rule declarations:
  - `claude-opus-4-*` / `claude-sonnet-4-*` / `claude-haiku-4-*` (kind=anthropic): `cache_control: true` — verified on real hardware against api.anthropic.com (2026-04-20, 1321 tokens written / 1321 tokens read)
  - `qwen3.5-*` / `qwen3.6-*` (kind=anthropic): `cache_control: true` — verified on real hardware via LM Studio 0.4.12 `/v1/messages` in v1.8.4 (`cache_read_input_tokens: 280` observed)
  - openai_compat variants are deliberately left undeclared (= None) → the existing v0.5-B `capability-degraded reason=translation-lossy` log continues to fire as-is

#### Capability gate: consults the registry

- **`coderouter/routing/capability.py`**: added a `registry: CapabilityRegistry | None = None` kwarg to `provider_supports_cache_control`. Resolution order now has 3 tiers:
  1. `provider.capabilities.prompt_cache: true` → True (explicit per-provider)
  2. Registry `cache_control: true|false` → decides immediately
  3. Fallback: `provider.kind == "anthropic"` → True (pre-v1.9-B compatible)
- If the registry returns `False`, it returns False even for kind=anthropic, establishing an escape hatch where operators can temporarily declare `cache_control: false` in user yaml on upstream regression → firing the `capability-degraded` log

#### Doctor: new `_probe_cache` probe

- **`coderouter/doctor.py`**: new `_probe_cache` function, wired in at the end of the orchestrator (after the streaming probe). Also added to the SKIP list on auth failure.
  - Behavior: POSTs the same body (~1900-token system prompt + `cache_control: ephemeral`) twice, expecting `cache_creation_input_tokens > 0` on the 1st call and `cache_read_input_tokens > 0` on the 2nd
  - **4 verdicts**:
    - **OK**: read > 0 on the 2nd call → cache_control plumbing works end-to-end
    - **NEEDS_TUNING**: creation observed on the 1st call / read=0 on the 2nd → TTL too short or a cache key mismatch
    - **NEEDS_TUNING**: neither creation nor read observed on either call → upstream silently ignores cache_control (incomplete Anthropic compat) or the 1024-token minimum wasn't met
    - **SKIP**: not anthropic / undeclared / upstream 5xx / auth failure
  - **The gate is deliberately tight**: since it consumes 2 paid HTTP calls, it only runs when the registry explicitly declares `cache_control: true` OR `providers.yaml capabilities.prompt_cache: true` is set. It doesn't auto-run just because kind=anthropic (avoiding wasted calls against unverified models)

#### Tests

- New **`tests/test_capability_registry_cache_control.py`** (+12): 4 registry resolution / 5 capability gate / 3 bundled YAML verification
  - Confirms bundled returns `cache_control=true` for `claude-opus-4-8` / `claude-sonnet-4-7` / `claude-haiku-4-1`
  - Confirms bundled returns `cache_control=true` for `qwen3.5-9b` / `qwen3.6-35b-a3b`
  - Confirms bundled leaves `openai_compat`'s `qwen2.5-coder:7b` undeclared (None), ensuring the translation-lossy gate still fires
- New **`tests/test_doctor_cache_probe.py`** (+8): probe gate / OK round-trip / NEEDS_TUNING (no hit / no creation) / explicit prompt_cache opt-in / 5xx on 1st call → SKIP / auth failure → SKIP

### Why

Upgrades the cache behavior "observed" in v1.9-A into a v1.9-B contract of **which (kind, model) combinations guarantee cache_control**. The doctor cache probe is a feature **no competitor (LiteLLM / claude-code-router / etc.) has**, letting operators confirm in a single command whether caching is really working on LM Studio, for example — a standalone differentiator.

Including LM Studio 0.4.12 in the bundled YAML formalizes, as a CodeRouter guarantee, the fact verified on real hardware in v1.8.4 that "`cache_read_input_tokens: 280` passes through end-to-end via the Anthropic-compatible `/v1/messages`." For operators declaring Qwen3.5/3.6 as `kind: anthropic`, an OK from `coderouter doctor --check-model lmstudio-qwen3-5-9b-anthropic` is the guarantee that prompt caching is genuinely usable.

### Migration

`pyproject.toml version 1.9.0a1 → 1.9.0a2`, `coderouter --version` returns 1.9.0a2. **Completely unchanged unless you touch your local `~/.coderouter-t/providers.yaml`.**

`provider_supports_cache_control` adds the `registry=None` kwarg, so the signature stays backward-compatible (no changes needed for existing callers). The new capability is being able to hard-disable via a `False` from the registry, but since the bundled YAML only ships positive declarations, default behavior is unchanged.

### Files touched

```
M  CHANGELOG.md
M  coderouter/config/capability_registry.py
M  coderouter/data/model-capabilities.yaml
M  coderouter/doctor.py
M  coderouter/routing/capability.py
M  pyproject.toml
A  tests/test_capability_registry_cache_control.py
A  tests/test_doctor_cache_probe.py
```

### Out of scope (future)

- **v1.9-E (moved up)**: the 3-stage Long-run Guards (L2 memory pressure / L3 tool loop / L5 backend health continuous) — the core Vision implementation
- **v1.9-C**: Adaptive Routing (rolling latency window + health-based dynamic priority)
- **v1.9-D**: Cost-aware Dashboard
- Streaming aggregation: upgrading the streaming-time `outcome` value for cache observation to `cache_hit/creation/no_cache` (from v1.9-A's `unknown`)

---

## [v1.9.0a1] — 2026-04-28 (v1.9-A: Cache Observability — making Anthropic prompt caching observable)

**Theme: the first alpha pre-release of the v1.9 series. Makes Anthropic prompt caching behavior observable from CodeRouter's side, aggregating `cache_read_input_tokens` / `cache_creation_input_tokens` per provider across 4 classes (cache_hit / cache_creation / no_cache / unknown).**

Implements the v1.9-A scope from `docs/inside/future.md` §5.1. A safe addition that changes no behavior — it only adds an observation path. Introduces strict 4-class aggregation that avoids from the start LiteLLM's `cache_creation_input_tokens` undercounting bug (future.md §3). Active cache control (cross-backend cache passthrough + capability gate / doctor cache probe) is planned for the following v1.9-B.

- Tests: 737 → **759** (+22: classify_cache_outcome / collector dispatch / snapshot cache panel / Prometheus exposition / engine emission)
- Runtime deps: 5 → 5 (23 consecutive sub-releases unchanged)
- Backward compat: fully compatible; no changes to `providers.yaml` / `~/.coderouter-t/model-capabilities.yaml` / API
- Pre-release: the `a1` in `1.9.0a1` is a PEP 440 alpha pre-release, available via `pip install --pre coderouter-cli`. The formal `v1.9.0` release will follow once v1.9-B/E/C/D are also complete

### Changes

#### New `cache-observed` structured log event

- **`coderouter/logging.py`**: added a `CacheOutcome` Literal + `CacheObservedPayload` TypedDict + `log_cache_observed()` helper + `classify_cache_outcome()` 4-class classification function.
  - `cache_hit`: `cache_read_input_tokens > 0` (cache reused, ~10% of input rate)
  - `cache_creation`: `cache_creation_input_tokens > 0` and not a hit (cache written, ~125% of input rate)
  - `no_cache`: usage was received but cache fields are 0/missing (no cache_control, or upstream silently dropped it)
  - `unknown`: the response has no usage block at all (streaming / via openai_compat / pre-v1.9-A upstream)
- **Rationale**: mixing cache fields into the `provider-ok` event would require every downstream consumer (collector / JSONL mirror / tests) to validate the new schema. A dedicated event naturally represents `outcome=unknown` for the streaming case too

#### Engine (`fallback.py`): emits cache-observed on every successful response

- **`coderouter/routing/fallback.py`**: added a call to `_emit_cache_observed()` right after `provider-ok` in `generate_anthropic`. Extracts `cache_read_input_tokens` / `cache_creation_input_tokens` from `AnthropicResponse.usage.model_extra` (round-tripped via Pydantic's `extra="allow"`).
  - Native Anthropic + LM Studio `/v1/messages` (`kind: anthropic`) → cache fields present → 4-class classification comes out correctly
  - openai_compat → converted via anthropic conversion → no cache fields → `outcome=no_cache` or `unknown`
- Streaming aggregation is deferred to v1.9-B (requires aggregating `message_delta` events); v1.9-A covers only the non-streaming path

#### MetricsCollector: per-provider cache aggregation

- **`coderouter/metrics/collector.py`**: added the `cache-observed` event to the dispatch table. New counters:
  - `_cache_read_tokens: Counter[str]` (per-provider)
  - `_cache_creation_tokens: Counter[str]` (per-provider)
  - `_cache_outcomes: dict[str, Counter[str]]` (per-provider x 4-class)
  - `_cache_read_tokens_total: int` / `_cache_creation_tokens_total: int` (aggregate, incrementally updated on every event, avoiding a re-fold cost at snapshot time)
- Extended `snapshot()`: added `counters.cache_*` (per-provider + aggregate) plus a `cache: {read_tokens, creation_tokens, outcomes, hit_rate, observations}` panel on each provider row
  - **`hit_rate`** is `cache_hit / (cache_hit + cache_creation + no_cache)`, excluding `unknown` from the denominator (avoids showing 0% when there's simply no signal)
  - `hit_rate=None` when there are no observations, letting the dashboard show "—"
- `reset()` also clears v1.9-A state

#### Prometheus exposition: 3 new counters

- **`coderouter/metrics/prometheus.py`**:
  - `coderouter_cache_read_tokens_total{provider="..."}` — cumulative input tokens served from cache
  - `coderouter_cache_creation_tokens_total{provider="..."}` — cumulative input tokens written to cache
  - `coderouter_cache_observed_total{provider="...", outcome="cache_hit|cache_creation|no_cache|unknown"}` — event counts per class
- `hit_rate` is deliberately not exposed as a gauge, following Prometheus convention (deriving it via `rate()` handles time windows correctly)

#### Tests (+22)

- **`tests/test_metrics_cache.py`** (+11): 4 `classify_cache_outcome` cases / collector dispatch / snapshot cache panel / hit_rate=None when idle / unknown-only keeps None / reset clears state / defensive handling of non-int inputs
- **`tests/test_metrics_prometheus_cache.py`** (+5): empty-snapshot HELP/TYPE / per-provider read/creation labels / outcome label pairing / `_total` suffix
- **`tests/test_fallback_cache_observed.py`** (+6): separate outcomes for cache_hit / cache_creation / no_cache / no_cache or unknown via the openai_compat path / no emit on failure / only the winning provider emits on chain fallthrough

### Why

The first step of the v1.9 series is making Anthropic prompt caching — a core element of **P1 Connection Stability**, one of the 3 pillars of the Vision established in `docs/inside/future.md` §1 ("a reliability layer for running agents on local LLMs over long sessions") — **observable**. The `cache_read_input_tokens: 280` observed via LM Studio 0.4.12's Anthropic-compatible `/v1/messages` in v1.8.4 can now be **aggregated and visualized as a per-provider hit rate** on CodeRouter's side.

The LiteLLM cluster has a known bug (referenced in future.md §3) that rounds `cache_creation_input_tokens` down into `no_cache`, undercounting it; CodeRouter avoids this from the start with strict 4-class aggregation. This positions CodeRouter as **the only Claude-Code-focused OSS with cache observability**.

### Migration

`pyproject.toml version 1.8.5 → 1.9.0a1`, `coderouter --version` returns 1.9.0a1. **Completely unchanged unless you touch your local `~/.coderouter-t/providers.yaml`.**

The `/metrics.json` counters / providers schema changes are **additive only** (new keys `cache_read_tokens` / `cache_creation_tokens` / `cache_outcomes`, plus a `cache` panel on provider rows), so existing dashboards won't break. Prometheus scrapers auto-discover the new metrics.

### Files touched

```
M  CHANGELOG.md
M  coderouter/logging.py
M  coderouter/metrics/collector.py
M  coderouter/metrics/prometheus.py
M  coderouter/routing/fallback.py
M  pyproject.toml
A  tests/test_fallback_cache_observed.py
A  tests/test_metrics_cache.py
A  tests/test_metrics_prometheus_cache.py
```

### Out of scope (future)

- **v1.9-B**: cross-backend cache passthrough + capability gate (`capabilities.cache_control` registry / doctor cache probe / openai_compat strip warn) — from "observation" to "guarantee"
- **v1.9-E (moved up)**: the 3-stage Long-run Guards (L2 memory pressure / L3 tool loop / L5 backend health) — the core Vision implementation
- Streaming aggregation: aggregating `message_delta` events so streaming can also report `outcome=cache_hit/creation/no_cache` (v1.9-B scope)

---

## [v1.8.5] — 2026-04-28 (Align doctor NEEDS_TUNING messages with the v1.8.3 thinking-aware budget facts + new `docs/lmstudio-direct.md`)

**Theme: a wording-consistency patch plus documentation completion.** v1.8.3 introduced a thinking-aware budget (256 / 1024) across the `tool_calls` / `num_ctx` / `streaming` probes. This release reflects that fact in the NEEDS_TUNING detail messages, removing any room for operators to suspect "maybe the probe budget was too small." It also formalizes the LM Studio 0.4.12 path verified on real hardware in v1.8.4 as `docs/lmstudio-direct.md` (+ `.en.md`), pairing it with `docs/llamacpp-direct.md`.

- Tests: 737 → 737 (existing assertions don't check the phrase substring, so no update needed there; added 1 new assertion for the missing case)
- Runtime deps: 5 → 5 (22 consecutive sub-releases unchanged)
- Backward compat: fully compatible; no changes to `providers.yaml` / `~/.coderouter-t/model-capabilities.yaml` / code-side API

### Changes

#### Doctor NEEDS_TUNING wording update (aligning suggestions with the thinking-aware budget)

- **`coderouter/doctor.py` `_probe_tool_calls`**: keeps "Common for quantized small models," but for thinking models it now prepends `Probed with thinking-aware budget (1024 tokens, covers reasoning_content plus the call) — this is a true tools=false case, not budget exhaustion.` For non-thinking models it prepends `Probed with default budget (256 tokens) — the model produced no tool-shaped output at all.`
- **`coderouter/doctor.py` `_probe_streaming`**: to avoid a `finish_reason='length'` false positive, prepends `Probe sent max_tokens=1024 (thinking-aware), so the cap is server-side options.num_predict rather than the probe budget.` for thinking models, and a `Probe sent max_tokens=512;`-style note for non-thinking models
- **`coderouter/doctor.py` `_probe_num_ctx`**: adds a budget note to all 3 "canary missing" cases (declared=None / declared<threshold / declared>=threshold) for thinking models: `Probe sent max_tokens=1024 (thinking-aware), so the miss is prompt-side truncation rather than reply truncation.` This immediately removes any operator doubt about whether the probe's reply budget was insufficient

#### Documentation completion: new `docs/lmstudio-direct.md`

- New **`docs/lmstudio-direct.md` / `.en.md`** — documents the LM Studio 0.4.12 path verified on real hardware in v1.8.4, pairing it with `docs/llamacpp-direct.md` as 7 steps plus Troubleshooting. A canonical recipe assuming M3 Max 64GB / Q4_K_M / Metal and GUI-driven operation
  - Step 1: install LM Studio & download a Q4_K_M model via the Discover tab (Qwen3.5 9B / Qwen3.6 35B-A3B / Jackrong/Qwopus3.5-9B-v3-GGUF)
  - Step 2: Load Model from the Chat tab (Context 32768 / GPU max / Flash Attention ON)
  - Step 3: Port 1234 / Just-in-time Model Loading: ON / Start Server from the Local Server tab
  - Step 4: raw curl calls (both OpenAI-compatible and Anthropic-compatible routes, confirming native tool_calls / native tool_use on both)
  - Step 5: register the provider with CodeRouter (both the `kind: openai_compat` route and the `kind: anthropic` route)
  - Step 6: verify with the 6 doctor probes (all probes OK on both routes)
  - Step 7: end-to-end via CodeRouter (including observing Anthropic prompt caching's `cache_read_input_tokens: 280`)

### Why

v1.8.3 fixed the `tool_calls` probe's active-harmful misdiagnosis (suggesting `tools: false` for thinking models), but the message wording still carried over the pre-v1.8.2 phrasing ("Common for quantized small models" only). This left room for an operator seeing NEEDS_TUNING to suspect "maybe the probe budget was too small" or "maybe the v1.8.2 bug resurfaced." This release aligns the wording with the fact that **the implementation can now state this definitively, since it's already thinking-aware**. A diagnostic tool's output should reflect the implementation's actual confidence.

`docs/lmstudio-direct.md` had already been verified on real hardware and added as a provider example to `examples/providers.yaml` in v1.8.4, but a canonical recipe document on par with `docs/llamacpp-direct.md` was still missing. Since the LM Studio path is currently the most stable way to run the `qwen35` / `qwen35moe` architectures (with Anthropic prompt caching passing through transparently), it's formalized as documentation operators can actually find.

### Migration

`pyproject.toml version 1.8.3 → 1.8.5`, `coderouter --version` returns 1.8.5. **Completely unchanged unless you touch your local `~/.coderouter-t/providers.yaml`.** The doctor output wording changes, but the verdict and suggested_patch semantics remain fully compatible.

### Files touched

```
M  CHANGELOG.md
M  coderouter/doctor.py
M  pyproject.toml
A  docs/lmstudio-direct.md
A  docs/lmstudio-direct.en.md
```

---

## [v1.8.3] — 2026-04-26 (tool_calls probe also made thinking-aware + adapter strips `reasoning_content` — supports llama.cpp direct)

**Theme: the second patch released the same day as v1.8.2. Resolves 2 additional issues discovered during real-hardware verification of Qwen3.6:35b-a3b on llama.cpp — a thinking-model false positive in the `tool_calls` probe, and the adapter's failure to strip the `reasoning_content` field emitted by llama.cpp.**

Right after the v1.8.2 release, while verifying on real hardware — as a follow-up to the note article v1.8.2, "How my own diagnostic tool fooled me" — the claim that **"Qwen3.6, stuck via Ollama, produced perfect native tool_calls once run directly through Unsloth GGUF + llama.cpp"**, a contradiction surfaced: CodeRouter's doctor still reported `tool_calls [NEEDS TUNING]`. Digging deeper revealed that the `tool_calls` probe's `max_tokens=64` gets entirely consumed by `reasoning_content` token usage on thinking models — **exactly the same bug pattern already fixed for num_ctx / streaming in v1.8.2, still present in the tool_calls probe.** It also turned up that llama.cpp's `reasoning_content` field (Ollama / OpenRouter use `reasoning`) wasn't included in the openai_compat adapter's strip list. Both are bundled into a single v1.8.3 patch.

**The real root cause of the Ollama-path failure is now fully confirmed**: Ollama's chat template / tool spec support is immature; the model itself is healthy. Run directly via llama.cpp, the Qwen3.6 family's `tool_calls` works natively.

- Tests: 733 → **737** (+4: thinking-variant tool_calls probe budget / 3 reasoning_content strip cases)
- Runtime deps: 5 → 5 (21 consecutive sub-releases unchanged)
- Backward compat: fully compatible; no edits needed to `providers.yaml` / `~/.coderouter-t/model-capabilities.yaml`

### Changes

#### Doctor `tool_calls` probe: thinking-aware budget

- **`coderouter/doctor.py`**: changed `_probe_tool_calls`'s `max_tokens` from a fixed `64` to a **dynamic choice with thinking detection** (256 default / 1024 thinking). Added `_TOOL_CALLS_PROBE_MAX_TOKENS_DEFAULT/_THINKING` constants, branching via the existing `_is_reasoning_model(provider, resolved)` helper.
  - With the old 64, Qwen3.6:35b-a3b on llama.cpp would consume all 64 tokens on `reasoning_content` → hit the length cap before producing `tool_calls` output → producing **NEEDS_TUNING with a suggested patch to set `tools: false`, exactly the wrong recommendation**
  - The new 1024 gives enough headroom for both thinking and the tool call

#### Adapter: added `reasoning_content` field stripping

- **`coderouter/adapters/openai_compat.py`**: extended `_strip_reasoning_field` to strip both keys in `_NON_STANDARD_REASONING_KEYS = ("reasoning", "reasoning_content")`.
  - `reasoning` (Ollama / OpenRouter naming) and `reasoning_content` (llama.cpp `llama-server` naming) represent the same concept, just with different vendor naming
  - Strict OpenAI clients reject either as an unknown key, so stripping both is correct
  - Updated the `capability-degraded` log's `dropped` field to `["reasoning", "reasoning_content"]` (reflecting that either may be stripped)

#### Doctor `reasoning-leak` probe: detects `reasoning_content`

- **`coderouter/doctor.py`**: extended `_probe_reasoning_leak`'s `has_reasoning` check to `"reasoning" in msg or "reasoning_content" in msg`, so reasoning leaks can now be detected informationally on llama.cpp-based providers too.

#### Tests

- **`tests/test_doctor.py`** +1: `test_tool_calls_max_tokens_bumped_for_thinking_provider` (confirms the tool_calls probe requests 1024 for a thinking provider and gets an OK verdict from a native tool_calls response)
- **`tests/test_reasoning_strip.py`** +3: `test_strip_helper_removes_reasoning_content_field` / `test_strip_helper_removes_both_reasoning_and_reasoning_content` / `test_strip_helper_removes_reasoning_content_from_delta` (confirming `reasoning_content` removal at each layer)
- Updated the existing `tests/test_reasoning_strip.py` assertion `recs[0].dropped == ["reasoning"]` to `["reasoning", "reasoning_content"]` (following the log representation change)

### Why

Right after writing in v1.8.2 the meta-lesson that "the diagnostic tool itself needs to keep being diagnosed," a remaining bug surfaced that proved exactly that point. The `tool_calls` probe carried the same "`max_tokens=64` doesn't account for reasoning token consumption on thinking models" problem already fixed for the num_ctx / streaming probes — and worse, doctor's suggested patch (set `tools: false`) was **recommending exactly the wrong fix** — not just a false positive, but an **active-harmful misdiagnosis** that would suppress a healthy model if a well-meaning user followed it.

This was an oversight that should have been caught while landing the v1.8.2 patch, and the note article v1.8.2's meta-lesson — "the diagnostic tool itself needs ongoing diagnosis" — got tested for real. Fixed promptly in v1.8.3.

Adding the `reasoning_content` strip is an ergonomic improvement letting the llama.cpp-direct path be used cleanly from CodeRouter, an item already recorded as a v1.8.x patch candidate in plan.md, resolved at the same time as the real-hardware discovery.

### Migration

`pyproject.toml version 1.8.2 → 1.8.3`, `coderouter --version` returns 1.8.3. **Completely unchanged unless you touch your local `~/.coderouter-t/providers.yaml`.**

Users who saw `tool_calls [NEEDS TUNING]` for Qwen3.6 / Gemma 4-family thinking providers in v1.8.2 will get an **OK** verdict on re-running in v1.8.3 (a provider that was actually working gets a fair evaluation from doctor too). Users on an llama.cpp-direct provider will have `reasoning_content` cleanly stripped without it reaching the client.

### Files touched

```
M  CHANGELOG.md
M  coderouter/adapters/openai_compat.py
M  coderouter/doctor.py
M  pyproject.toml
M  plan.md
M  docs/troubleshooting.md
M  tests/test_doctor.py
M  tests/test_reasoning_strip.py
```

### Post-release docs followup (a separate commit, not the same one)

Having formally adopted the llama.cpp-direct path as the canonical escape route, related docs were cleaned up after v1.8.3:

- New **`docs/llamacpp-direct.md` / `.en.md`** — covers `llama.cpp` build → Unsloth GGUF → `llama-server` → connecting CodeRouter, in 7 steps plus Troubleshooting. A canonical recipe assuming M3 Max 64GB / Q4_K_M / Metal
- **`setup.sh`**: changed the recommendation for the 48 GB+ tier from the old `qwen3.6:35b` to `gemma4:26b` (since the Ollama path is a dead end). Also removed the Qwen3.6 family from the upgrade hint, pointing instead to `docs/llamacpp-direct.md`
- **`docs/quickstart.md` / `.en.md`**: removed `ollama pull qwen3.6:35b` from the "better models" section, added a pointer to `docs/llamacpp-direct.md`
- **`docs/hf-ollama-models.md`**: replaced `ollama pull qwen3.6:35b` with a "⚠️ the Qwen3.6 family tends to get stuck via Ollama" warning, added guidance toward the llama.cpp-direct path
- **`README.md` / `.en.md`**: added a "llama.cpp direct guide" line to the docs table of contents, and a `llama.cpp direct` link to the English language switcher
- **`examples/providers.yaml`**: added a `llamacpp-qwen3-6-35b-a3b` provider example and wired it into the `coding` profile chain's primary slot (with detailed comments). Also updated the Qwen3.6-via-Ollama comments to reflect the v1.8.3 findings
- **`tests/test_setup_sh.py`**: updated the 48 GB / 64 GB tier's expected_model assertions from `qwen3.6:35b` to `gemma4:26b`

---

## [v1.8.2] — 2026-04-26 (Make doctor probes thinking-model aware — resolving a Gemma 4 false positive)

**Theme: while digging in right after the v1.8.1 release, discovered that the `doctor`'s `num_ctx` / `streaming` probes were producing false-positive NEEDS_TUNING verdicts for thinking models, and redesigned the probes' `max_tokens` budget to account for reasoning token consumption.**

In v1.8.1, Gemma 4 26B, placed as the primary of the `coding` profile, got a doctor verdict of `tool_calls [OK]` + `num_ctx [NEEDS TUNING]` + `streaming [NEEDS TUNING]`, judged as "working only partially." But hitting it directly with curl on real hardware showed that **a 5K-token canary echo-back succeeded even via Ollama's OpenAI-compat interface.** Isolating the cause confirmed it as a false positive: the non-standard `reasoning` field Gemma 4 emits was **entirely consuming the doctor probe's `max_tokens=32` (num_ctx) / `max_tokens=128` (streaming) budget with thinking tokens, returning `content=""` with `finish_reason='length'`.** Real-hardware verification (M3 Max 64GB / Ollama 0.21.2) also confirmed that Gemma 4 26B returns "Hello." in 2 seconds via the Anthropic-compatible `/v1/messages` route, so the final verdict is: **Gemma 4 26B is genuinely usable.**

- Tests: 730 → **733** (+3: thinking provider declaration / registry-based / streaming)
- Runtime deps: 5 → 5 (20 consecutive sub-releases unchanged)
- Backward compat: fully compatible; no edits needed to `providers.yaml` / `~/.coderouter-t/model-capabilities.yaml`

### Changes

#### Doctor probe: thinking-aware budget selection

- **`coderouter/doctor.py`**: changed `_probe_num_ctx` / `_probe_streaming`'s `max_tokens` to a dynamic choice with thinking detection. A new `_is_reasoning_model(provider, resolved)` helper checks both the provider declaration and the registry-resolved capability for `thinking` / `reasoning_passthrough`, choosing the larger budget only for reasoning models.
  - `_NUM_CTX_PROBE_MAX_TOKENS_DEFAULT = 256` (was 32), `_NUM_CTX_PROBE_MAX_TOKENS_THINKING = 1024`
  - `_STREAMING_PROBE_MAX_TOKENS_DEFAULT = 512` (was 128), `_STREAMING_PROBE_MAX_TOKENS_THINKING = 1024`
  - Non-thinking models still stop early at their natural stop point, so there's no wasted consumption; thinking models get enough headroom for both the reasoning trace and the answer

#### Registry: declares `thinking: true` for known thinking models

- **`coderouter/data/model-capabilities.yaml`**: added `thinking: true` for `gemma4:*` / `google/gemma-4*` / `qwen3.6:*` / `qwen/qwen3.6-*`. These are confirmed to emit a substantial number of tokens into the `reasoning` field via Ollama. Since this is delivered via the registry, users don't need to touch `providers.yaml` for doctor's thinking budget to apply
- **Updated the Qwen3.6 section comment**: revised the part that said "Ollama silent cap" as of v1.8.1 to note that **"as of v1.8.2, num_ctx / streaming turned out to be doctor false positives; `tool_calls [NEEDS TUNING]` remains the genuine issue."** The decision to withdraw `claude_code_suitability` stands (Qwen3.6's tool_calls failure is a separate genuine issue, not caused by thinking)

#### Tests

- **`tests/test_doctor.py`**: 3 new cases
  - `test_num_ctx_max_tokens_bumped_for_thinking_provider_declaration`: `provider.capabilities.thinking=True` → 1024
  - `test_num_ctx_max_tokens_bumped_when_registry_says_thinking`: no provider declaration but registry declares it → 1024
  - `test_streaming_max_tokens_bumped_for_thinking_provider`: the streaming probe also gets 1024 via the same path
- Updated the existing `test_num_ctx_request_body_merges_extra_body_options` assertion `max_tokens == 32` to `== 256` (new baseline)
- Added a `max_tokens == 512` assertion to the existing `test_streaming_request_body_carries_stream_true_and_merges_extra_body` (streaming baseline)

### Why

While writing the v1.8.1 article, of the "note's trending model → ollama pull → doesn't work" cases, Gemma 4 was supposed to be the lone **reversal victory** with `tool_calls [OK]`, but `num_ctx [NEEDS TUNING]` was also showing up, leaving the article in an unsatisfying state. Digging further revealed a contradiction: options still take effect via `/v1/chat/completions`, `ollama ps` shows a context length of 262144, **yet doctor still fails.** Confirming that thinking tokens were flowing into the `.choices[0].message.reasoning` field and consuming the `max_tokens=32` budget revealed that **doctor's own probe design hadn't kept up with the thinking-model era.**

This is a meta-lesson one level below the "real-hardware evidence first" principle (plan.md §5.4): **the diagnostic tool itself needs to keep being diagnosed.**

### Migration

`pyproject.toml version 1.8.1 → 1.8.2`, `coderouter --version` returns 1.8.2. **Completely unchanged unless you touch your local `~/.coderouter-t/providers.yaml`.**

Users who had been running Gemma 4 26B conservatively (with reduced `claude_code_suitability`) in v1.8.1 should now see `num_ctx [OK]` + `streaming [OK]` pass on re-running doctor in v1.8.2. Qwen3.6's `tool_calls [NEEDS TUNING]` is genuine (not caused by thinking), so it's still not recommended as the coding chain primary.

### Files touched

```
M  CHANGELOG.md
M  coderouter/data/model-capabilities.yaml
M  coderouter/doctor.py
M  pyproject.toml
M  plan.md
M  docs/troubleshooting.md
M  docs/articles/v1-saga/note-1-v1-8-1-reality-check.md   (or new file v1-8-2)
M  tests/test_doctor.py
```

---

## [v1.8.1] — 2026-04-26 (Patch reflecting real-hardware verification — resolving mode_aliases + promoting Gemma 4 + documenting known Ollama issues)

**Theme: a patch resolving 3 issues hit during real-hardware verification (M3 Max 32GB / Ollama 0.21.2) right after the v1.8.0 release.**

For users running on the NIM example yaml base, v1.8.0's 4 use-case-based profiles were found to have a loader bug where `coderouter serve --mode coding` fails to start with **`default_profile 'coding' is not declared in profiles`**. Real-hardware verification also confirmed that Qwen3.6:27b/35b, placed as the `coding` profile's primary, is impractical via Ollama (num_ctx silent cap / tool_calls returning 0 / streaming returning 0 chars). This documents, as `troubleshooting.md §4-2`, the reality that **"a model being praised in a note article or on HF doesn't mean it'll work right away via Ollama."**

- Tests: 729 → **730** (+1: a loader test for mode_aliases resolution)
- Runtime deps: 5 → 5 (19 consecutive sub-releases unchanged)
- Backward compat: fully compatible; no `providers.yaml` edits required (the loader resolves it via the alias)

### Changes

#### Bug fixes (hit during real-hardware verification)

- **`coderouter/config/loader.py`**: fixed a naive v0.6-A implementation where the `CODEROUTER_MODE` env var (= `--mode` CLI flag) was **assigned directly to `default_profile` without resolving `mode_aliases`.** The runtime `X-CodeRouter-Mode` header (v0.6-D) did resolve aliases, so startup and runtime had asymmetric semantics. v1.8.1 aligns both by resolving env_mode through `mode_aliases` before assigning it to `default_profile`, making the two symmetric. This lets `cr serve --mode coding` start without a validation error even against the NIM example yaml (profiles=`[claude-code-nim, ...]`, mode_aliases=`{coding: claude-code-nim}`)
- **`examples/providers.nvidia-nim.yaml`**: added the `mode_aliases` (default/coding/general/multi/reasoning/fast/cheap/think/vision) that were added to the main `providers.yaml` in v1.8.0, to the NIM example yaml as well, so NIM users can also use `--mode coding|general|reasoning|multi` as canonical short aliases

#### Adjusted `coding` profile primary to reflect real-hardware verification

- **`examples/providers.yaml`**: changed the order at the head of the `coding` profile's providers list from Qwen3.6:35b/27b to **`ollama-qwen-coder-14b` / `ollama-gemma4-26b` / `ollama-qwen-coder-7b` / `ollama-qwen3-coder-30b`**. The Qwen3.6 family is demoted, commented out at the tail (left in place as a candidate to promote back to primary once LM Studio/llama.cpp support improves). This reflects the ordering principle "well-established and reliably working things go first; newer note-recommended things get promoted only after stability is confirmed"
- **`coderouter/data/model-capabilities.yaml`**: **withdrew** `claude_code_suitability: ok` for `qwen3.6:*` / `qwen/qwen3.6-*`. When added in v1.7-B it was a preemptive declaration based on secondhand reports from note articles, but v1.8.1 real-hardware verification confirmed NEEDS_TUNING across num_ctx / tool_calls / streaming, so without confirmation the policy is now to keep the `tools` declaration but not assert suitability. Users who do get it working on real hardware can still override `claude_code_suitability: ok` on their side via `~/.coderouter-t/model-capabilities.yaml` (since the registry's first-match-per-flag walk goes user → bundled)

#### Documentation: added known issues for real-world Ollama operation

- New **`docs/troubleshooting.md` §4-2, "Known issues commonly hit via local Ollama"**:
  - **§4-2-A**: Qwen3.6:27b/35b is impractical via Ollama 0.21.2 (num_ctx silent cap / tool_calls returning 0 / streaming returning 0), and `/no_think` doesn't help. Workaround: promote Gemma 4 / Qwen2.5-Coder above it
  - **§4-2-B**: HF-distilled Qwen3.5-family models (Qwopus3.5 etc.) fail with an `unable to load model` 500 error because llama.cpp doesn't yet support the `qwen35` architecture (hybrid Transformer-SSM). Waiting on upstream framework support
  - **§4-2-C**: confirmed Gemma 4 26B gets tool_calls OK with no tweaks, backing up the note article's "everyday champion" assessment
  - **§4-2-D**: best practice is "well-established models + an observation tool (doctor)"; for a new model found on HF, run `ollama run` and check the server log for `unknown model architecture` — if it shows up, give up on it for now

### Why

v1.8.0 shipped touting "use-case-based 4 profiles with a working `--mode coding`," but the loader bug hit by NIM-example-based users **fails at validation before the first real prompt even reaches the model**, making it the top-priority fix. Also, the fact that the Qwen3.6 family placed as the v1.8.0 example's primary produced 3 NEEDS_TUNING probe results on real hardware, and that Qwen3.5-based HF distillations aren't yet supported by llama.cpp, reaffirms the **"real-hardware evidence over preemptive implementation"** principle (plan.md §5.4).

### Migration

`pyproject.toml version 1.8.0 → 1.8.1`, `coderouter --version` returns 1.8.1. **Completely unchanged unless you touch your local `~/.coderouter-t/providers.yaml`.**

Users on the NIM-example base who couldn't get `cr serve --mode coding` working can:

```bash
# copy the latest example (mode_aliases already added in v1.8.1)
cp examples/providers.nvidia-nim.yaml ~/.coderouter-t/providers.yaml
# or manually add the mode_aliases section to your existing file
```

Or reinstall `cr` from the local dev version:

```bash
uv tool install --reinstall --force --from /path/to/CodeRouter coderouter-cli --with ruamel.yaml
```

### Real-machine verification

```
$ pytest -q
730 passed, 1 skipped in 1.86s

$ ruff check coderouter/ tests/
All checks passed!

$ cr serve --port 8088 --mode coding   # starts successfully even on the NIM example yaml
$ cr doctor --check-model ollama-gemma4-26b --apply   # confirmed tool_calls OK
```

### Out of scope / deferred

- HF-distilled Qwen3.5 family (Qwopus / similar): revisit once llama.cpp implements the `qwen35` architecture
- Qwen3.6:27b/35b via Ollama: revisit if Ollama / llama.cpp improve, consider re-granting `claude_code_suitability`
- v1.7-C candidates (network audit / launcher / startup update check) still awaiting demand

---

## [v1.8.0] — 2026-04-26 (4 use-case-based profiles + officially adding GLM/Gemma 4/Qwen3.6 + apply automation)

**Theme: a minor release delivering operators "substitute models that don't drift in feel from Claude Code."** Clears 6 tasks from plan.md §11.B (the v1.7-B umbrella) in one push:

1. **Automated PyPI Trusted Publishing** — a simple `git tag v* && git push` now has release.yml auto-publish to PyPI and draft a GitHub Release. No API token needed (OIDC)
2. **`claude_code_suitability` hint** — a new `Literal["ok", "degraded"] | None` field in the capability registry; placing a Llama-3.3-70B-family model in a `claude-code-*` profile now emits a structured `chain-claude-code-suitability-degraded` warn at startup. An automated-detection version of the "`こんにちは` → runaway `Skill(hello)`" trap documented in v1.6.2
3. **`coderouter doctor --check-model --apply` / `--dry-run`** — writes doctor's suggested YAML patches back into `providers.yaml` / `model-capabilities.yaml` **non-destructively** (100% preserving comments and key order). `--dry-run` produces a `git apply`-compatible unified diff; `--apply` creates a `.bak` backup and is idempotent (a second run is a no-op). `ruamel.yaml` is an optional dependency (the `[doctor]` extra) lazily imported, preserving the base 5-deps streak
4. **`setup.sh` onboarding wizard** — auto-detects RAM → suggests a recommended local model → runs `ollama pull` → generates `~/.coderouter-t/providers.yaml`. Adds `--ram-gb N` / `--non-interactive` / `--no-pull` / `--dry-run` / `--force` flags, bash 3.2 compatible, zero new dependencies
5. **Extended `examples/providers.yaml` to a 4-profile layout** — `multi` (default) / `coding` / `general` / `reasoning`, each nudged toward Claude-like responses via `append_system_prompt`, with `mode_aliases` shortcuts for `default/fast/vision/think/cheap`
6. **Registered Gemma 4 / Qwen3.6 / Z.AI (GLM-4.7/5.1) in providers.yaml** — registers `gemma4:e4b/26b/31b` (now official Ollama tags, including the note-recommended 26B-A4B) and `qwen3.6:27b/35b` (the note's "local champ" 35b-a3b) as active stanzas, promoting note-recommended models to each profile's primary slot. Z.AI is offered via OpenAI-compat with 2 base_urls (Coding Plan / General API), documented along with the unauthorized-tool caveat. Newly declares the `qwen3.6:*` (claude_code_suitability=ok) / `gemma4:*` / `GLM-5*` / `GLM-4.[5-9]*` families in the bundled `model-capabilities.yaml`

- Tests: 651 → **710** (+59, +9.1%): `tests/test_claude_code_suitability.py` (6, walker + payload + opt-out), `tests/test_capability_registry.py` (+11 schema/lookup/bundled-yaml), `tests/test_doctor_apply.py` (25, parse/merge/apply/idempotent), `tests/test_setup_sh.py` (17 + 1 shellcheck-skip, RAM recommendation / existing-file collision / dry-run / parent dir creation), `tests/test_examples_yaml.py` (+5, 4-profile presence / append_system_prompt required / mode_aliases / coding head verification)
- Runtime deps: 5 → 5 (18 consecutive sub-releases unchanged; `ruamel.yaml` is optional via `[project.optional-dependencies].doctor`)
- Backward compat: since this includes a change from `default_profile: default` to `default_profile: multi`, **re-copying `examples/providers.yaml` over `~/.coderouter-t/providers.yaml` changes behavior.** Your local `providers.yaml` is unaffected unless you touch it. `mode_aliases.default → multi` provides backward compatibility, resolving old default calls to multi

### Theme: achieving "substitute models that don't drift in feel" through 3 layers of measures

The core problem facing users whose primary use case is Claude Code is that, when falling back to a local/open model, **the response's "personality"** diverges from Claude Sonnet/Opus, leaving users confused about why. v1.8.0 addresses this in 3 layers:

1. **Choosing models closer to Claude's feel** — Qwen3.6 35B-A3B (the note article's "local champ") and the Qwen3-Coder family become the coding backbone. Llama-3.3-70B continues to be automatically demoted from claude-code chains via `claude_code_suitability: degraded`. Gemma 4 26B-A4B (the note's "everyday champion") goes into multi/general
2. **Nudging via `append_system_prompt`** — all 4 profiles carry instructions like "Match Claude Sonnet's coding style" / "Match Claude Haiku's style," so response style leans toward Claude even for non-Claude models. Applied per profile (a feature already implemented in v0.6-B)
3. **Cleaning up surface differences with `output_filters`** — Qwen-family `<think>` leaks and stop markers continue to be stripped (v1.0-A). `[strip_thinking, strip_stop_markers]` is applied by default for Qwen3.6 / Qwen3-Coder 30B

### Z.AI (GLM family) — Coding Plan pitfalls and workarounds

Z.AI's GLM-4.7 / 5.1 is a strong option, rated by note articles as having "Claude Opus-level intent understanding." Since it's an OpenAI-compatible endpoint, CodeRouter can connect directly via `kind: openai_compat`, but the Coding Plan terms need attention:

The official docs (docs.z.ai/devpack/overview) explicitly state that **"access via unauthorized third-party tools may have benefits restricted."** Since CodeRouter has an Anthropic-API-compatible ingress, it should look like an authorized tool from Claude Code's perspective, but there remains a risk that Z.AI's detection logic flags it as "routed through a proxy."

`examples/providers.yaml` provides 2 kinds of base_url stanzas:

- `zai-coding-glm-4-7/5-1/4-5-air`: for the Coding Plan (`api/coding/paas/v4`) — for subscribers, should still look like a direct Claude Code connection even via CodeRouter
- `zai-paas-glm-4-7` (commented): for the General API (`api/paas/v4`) — pay-as-you-go, not subject to the restriction. Safe to use via CodeRouter

**Recommended practice**: Coding Plan subscribers who want certainty should either connect Z.AI directly to Claude Code (bypassing CodeRouter) or enable the General API stanza. The General API is billed proportional to usage.

### Changes

#### v1.8-A: automated Trusted Publishing (docs only, one-time registration on the PyPI side)

- Registered a trusted publisher on the PyPI side (Owner: zephel01, Repo: CodeRouter, Workflow: release.yml, Environment: pypi)
- Created a `pypi` environment on the GitHub side (no protection rules, no secrets)
- Added registration steps + the post-automation flow to `docs/inside/release-pypi.md` §0-6, marking the §11 checklist items `[x]` complete

#### v1.8-B: `claude_code_suitability` hint

- Added a `claude_code_suitability: Literal["ok", "degraded"] | None` field to `RegistryCapabilities` / `ResolvedCapabilities` in `coderouter/config/capability_registry.py`, with a new slot in the `lookup` method's first-match-per-flag walk
- Declared the Llama-3.3-70B family (`*llama-3.3-70b*` / `*Llama-3.3-70B*`) with `claude_code_suitability: degraded` in `coderouter/data/model-capabilities.yaml`
- New `ChainClaudeCodeSuitabilityDegradedPayload` TypedDict + `log_chain_claude_code_suitability_degraded` helper in `coderouter/logging.py`, with the message `chain-claude-code-suitability-degraded`
- New `CLAUDE_CODE_PROFILE_PREFIX = "claude-code"` constant + `check_claude_code_chain_suitability(config, *, logger, registry=None)` function in `coderouter/routing/capability.py`. Gates on profile name prefix, walks the chain, and aggregates a per-profile WARN
- The lifespan in `coderouter/ingress/app.py` calls `check_claude_code_chain_suitability` in a single line at startup

#### v1.8-C: `coderouter doctor --check-model --apply` / `--dry-run`

- New `coderouter/doctor_apply.py` — `parse_patch_yaml` (strips comments from doctor's YAML literal and safe_loads it) / `deep_merge_dicts` (recursive merge with idempotency detection) / `merge_provider_patch_into_doc` / `merge_capabilities_rule_into_doc` / `apply_doctor_patches` (top-level entry, returns an ApplyResult dataclass) / `render_unified_diff` (via stdlib `difflib.unified_diff`) / `DoctorApplyError` + `MissingDependencyError`
- Added `[project.optional-dependencies].doctor = ["ruamel.yaml>=0.18.6"]` to `pyproject.toml` (preserving the base 5-deps streak; also added to `[dev]` for testing)
- Added `--apply` / `--dry-run` flags to the doctor subparser in `coderouter/cli.py`, plus `_run_check_model` / `_resolve_config_path` / `_run_apply_or_dry_run` helpers. Doctor's suggested YAML patches can now be written back in a single invocation
- Idempotency: a no-op if the same value is already present (file mtime unchanged, exit 0, "already up to date" message)
- Backup: automatically creates `providers.yaml.bak` when `--apply` is used (overwrite style; git users can track history via git-diff)

#### v1.8-D: `setup.sh` onboarding wizard

- New `setup.sh` at the repo root (bash 3.2 compatible, zero new dependencies)
- Auto-detects RAM on macOS (`sysctl hw.memsize`) / Linux (`/proc/meminfo`)
- RAM → recommended model: ≥24GB→qwen2.5-coder:14b / ≥12GB→qwen2.5-coder:7b / ≥6GB→qwen2.5-coder:1.5b / <6GB→cloud-only bundle + cloud hint
- Missing-`ollama` check: enforced only in the actual pull mode, permitted under `--no-pull` / `--dry-run`
- Protects the existing `providers.yaml`: writes to a `.new` sidecar file by default, only overwrites (leaving a `.bak`) under `--force`
- Pinned via regression test that the generated YAML round-trips through the live `CodeRouterConfig` Pydantic schema

#### v1.8-E: 4-profile layout for examples/providers.yaml + registering Gemma 4/Qwen3.6/Z.AI

- Changed `default_profile: default` → `default_profile: multi` (the new default is a multimodal-capable chain)
- 4 new profiles:
  - `multi` (default): vision-capable, Gemma 4 26B local primary → terminating at Sonnet 4-6 with vision (paid)
  - `coding`: Qwen3.6 35B-A3B (note's "local champ") → Qwen3-Coder 30B → ... → GLM-4.7 → Sonnet 4-6
  - `general`: Gemma 4 E4B (lightweight, runs even on a laptop) → Gemini Flash free → GLM-4.5-Air → Haiku 4-5
  - `reasoning`: Qwen3.6 35B (native thinking) → ... → GLM-5.1 → Opus 4-1 with thinking
- All profiles nudged toward Claude-like responses via `append_system_prompt`
- `mode_aliases`: `default → multi`, `fast → general`, `vision → multi`, `think → reasoning`, `cheap → general`
- Added 11 new providers: Qwen3.6 (27b/35b), Gemma 4 (e4b/26b/31b), Z.AI (GLM-4.7/5.1/4.5-Air), Gemini Flash free, Claude Haiku/Opus direct
- Newly registered `qwen3.6:*` (tools=true, claude_code_suitability=ok), `gemma4:*` (tools=true), `GLM-5*` / `GLM-4.[5-9]*` families in `coderouter/data/model-capabilities.yaml`
- The HF-on-Ollama commented stanza is trimmed now that Gemma 4 / Qwen3.6 have official tags; GLM-4.5-Air documents both the Z.AI cloud route and the HF GGUF route
- New `docs/hf-ollama-models.md` (steps for registering HF GGUF with Ollama, recipes per recommended model, known pitfalls)

### Migration

`pyproject.toml version 1.7.0 → 1.8.0`, `coderouter --version` now returns 1.8.0. **Completely unchanged unless you touch your local `~/.coderouter-t/providers.yaml`** (the new example only lives in `examples/providers.yaml`; copying it over is a manual step).

To try the new example:

```bash
# back up your existing config while copying the new example
cp ~/.coderouter-t/providers.yaml ~/.coderouter-t/providers.yaml.bak
cp examples/providers.yaml ~/.coderouter-t/providers.yaml

# pull the recommended models into Ollama (if you have 24GB+ VRAM)
ollama pull qwen3.6:35b
ollama pull qwen3-coder:30b-a3b
ollama pull gemma4:26b

# if using Z.AI, set the API key as an env var
echo 'export Z_AI_API_KEY="your-key-here"' >> ~/.zshrc
source ~/.zshrc

# verify
coderouter doctor --check-model local --apply  # also try the auto-patch
coderouter serve --port 8088 --mode coding    # be explicit about use case
```

`--mode default` resolves to `multi` (the multimodal chain) in the new example. If you want to keep the old example's meaning (Qwen2.5-Coder + cloud chain), use `--mode coding` or add your own alias to `mode_aliases`.

### Real-machine verification

```
$ pytest -q
710 passed, 1 skipped in 1.81s

$ ruff check coderouter/ tests/
All checks passed!

$ mypy --strict coderouter/doctor_apply.py coderouter/cli.py
Success: no issues found in 4 source files
```

Confirmed the idempotency and backup creation of `coderouter doctor --check-model X --apply` via a smoke test:

```
$ coderouter doctor --check-model local --apply
[probe report ...]
Apply: 1 target file(s).
  1 patch(es) applied.
[diff shown]
  Backup: ~/.coderouter-t/providers.yaml → ~/.coderouter-t/providers.yaml.bak

$ coderouter doctor --check-model local --apply  # a no-op the second time
Apply: 1 target file(s).
  All 1 patch(es) already applied — providers.yaml is up to date.
```

### Out of scope / deferred (v1.9 candidates)

- v1.7-C candidates still awaiting demand: `coderouter doctor --network`, `--check-config`/`--check-adapter` (a no-argument mode that runs everything), extending `recover_garbled_tool_json`, a startup update check
- The macOS `.command` / Linux `.sh` / Windows `.bat` launchers are being reconsidered now that `uvx coderouter-cli` has already lowered onboarding friction enough
- PEP 541 reclamation (the `coderouter` namespace) is still pending review; progress will be recorded in plan.md §11.B
- Obtaining a guarantee from Z.AI that Coding Plan access "is authorized even via a router" (feedback to Z.AI)

---

## [v1.7.0] — 2026-04-25 (PyPI publication: works with a single `uvx coderouter-cli`)

**Theme: a minor release eliminating the onboarding friction of "git clone, then `uv tool install --from git+...`".** Now published on PyPI as **`coderouter-cli`**, so a single line, `uvx coderouter-cli serve --port 8088`, installs and starts it from anywhere. This bundles small code changes needed for distribution infrastructure (package name, following the `importlib.metadata` lookup name) along with a GitHub Actions workflow and a `pyproject.toml` sdist allowlist that make releases repeatable. Runtime / API behavior is completely unchanged from v1.6.3.

- Tests: 651 → **651** (±0, code changes are distribution-only)
- Runtime deps: 5 → 5 (17 consecutive sub-releases unchanged)
- New PyPI package: [`coderouter-cli`](https://pypi.org/project/coderouter-cli/) (Python ≥ 3.12)
- Backward compat: the existing `git clone + uv tool install --from git+...` path remains valid. The `coderouter` command name / Python import name (`from coderouter import ...`) are also completely unchanged

### Why `coderouter-cli` (and not `coderouter`)

The `coderouter` namespace on PyPI was already taken by a different author's (Lawrence Chen) general-purpose HTTP routing library (published 2025-06, only ever reached 0.1.0, a completely unrelated domain). A new publish needed a different name, so following the npm / cargo convention (`*-cli` suffix for CLI tools), `coderouter-cli` was chosen. **Both the Python import name and the console script name stay `coderouter`,** so from a user's perspective only the name at `pip install` time differs:

```bash
pip install coderouter-cli       # ← install (name changes)
import coderouter                # ← import (unchanged)
coderouter serve --port 8088     # ← run (unchanged)
```

The path to reclaiming the `coderouter` name via a PEP 541 reclamation request is tracked in plan.md §11.B (even if approved, it can take one to several months, so `coderouter-cli` is used in the meantime).

### Changes

- **PyPI publish setup** — changed `pyproject.toml`'s `name` from `coderouter` to `coderouter-cli`, bumped `version` to 1.7.0, enriched `classifiers` / `project.urls` / `keywords` with the metadata needed for publishing (`Topic :: Scientific/Engineering :: Artificial Intelligence` plus 4 URLs: Homepage / Issues / Changelog / Documentation)
- **`coderouter/__init__.py`** — updated `importlib.metadata.version("coderouter")` to `version("coderouter-cli")`. Since the Python import name (`coderouter`) is unchanged, this has no impact on any user doing `from coderouter import ...`
- **New `LICENSE`** — explicitly files the MIT License, now bundled into the wheel's `dist-info/licenses/LICENSE` (improves PyPI's license display and sdist completeness)
- **`tool.hatch.build.targets.sdist`** — strictly allowlists the sdist via `only-include`, designed to never pull in local virtualenvs (`.venv*`), `__pycache__`, `dist/`, `.pytest_cache`, etc. This makes `uv build` produce the same size (sdist 668 KB / wheel 161 KB) on any machine
- **New `.github/workflows/release.yml`** — on a `git tag v*` push, auto-publishes to PyPI via Trusted Publishing (OIDC, no API token needed) and drafts a GitHub Release. **The first publish (v1.7.0) was done manually**; from v1.7.x onward, after registering the Trusted Publisher, a tag push alone automates it
- **Doc reorder for the new entry path** — rewrote the install sections of README ja/en, quickstart.md ja/en, and the free-tier-guide ja/en to center on `uvx coderouter-cli`. The `uv tool install --from git+...` path is kept for intermediate users

### Real-machine verification

```
$ uv build
Successfully built dist/coderouter_cli-1.7.0.tar.gz   (668 KB, zero .venv contamination)
Successfully built dist/coderouter_cli-1.7.0-py3-none-any.whl  (161 KB)

$ coderouter-publish-prod   # = op run + uv publish (injects PYPI_TOKEN from 1Password)
Publishing 2 files https://upload.pypi.org/legacy/
Uploading coderouter_cli-1.7.0-py3-none-any.whl (157.7KiB)
Uploading coderouter_cli-1.7.0.tar.gz (652.7KiB)

$ curl -sI "https://pypi.org/pypi/coderouter-cli/json" | head -1
HTTP/2 200
```

After CDN propagation, also confirmed a real PyPI-based install via `uvx --from coderouter-cli coderouter --version` (on uv 0.11+, `--from` is required when the package name ≠ the executable name, per feedback from the reporter of Issue #10).

### Migration

None needed. **For users who had been running `uv tool install --from git+...` through v1.6.x, the natural upgrade path is:**

```bash
# old (still valid)
uv tool install --from git+https://github.com/zephel01/CodeRouter.git coderouter-cli

# new (from PyPI, single command — the canonical uv 0.11+ form)
uvx --from coderouter-cli coderouter serve --port 8088
# or, to install permanently:
uv tool install coderouter-cli
```

The `coderouter` launch command name, the `from coderouter import ...` Python import, the `providers.yaml` format, env vars (`ANTHROPIC_BASE_URL` etc.), and the ingress URL structure are all completely unchanged from v1.6.3.

### Out of scope / deferred (v1.7-B+)

v1.7.0 (= v1.7-A) focused shipping exclusively on the distribution pipeline. The remaining v1.7 candidate features listed in plan.md §11.B are deferred to v1.7-B+:

- `coderouter doctor --check-config` / `--check-adapter` (a no-argument mode that runs everything)
- `coderouter doctor --network` (outbound-connection detection, guaranteeing zero outbound in CI)
- `setup.sh` (RAM detection → model suggestion → providers.yaml generation)
- macOS `.command` / Linux `.sh` / Windows `.bat` launchers
- Startup update check (opt-in)
- The capability registry's `claude_code_suitability` hint (startup WARN for the Llama-3.3-70B family)

---

## [v1.6.3] — 2026-04-24 (`--env-file` + `doctor --check-env` for `.env` hygiene)

**Theme: ergonomic + safe `.env` handling, without rolling our own crypto.** v1.6.2 documented the `.env` `export` gotcha; v1.6.3 makes it disappear by giving operators two new tools that integrate cleanly with the existing secret-management ecosystem (1Password CLI, sops, direnv, OS Keychain) instead of inventing yet another encryption scheme.

- **`coderouter serve --env-file PATH`** — load a `.env`-style file into the worker's env *before* uvicorn boots. Repeatable for layering. Default precedence is "shell wins, file fills in gaps" so it's safe to run as a default; flip with `--env-file-override` when the file is the source of truth (e.g. CI).
- **`coderouter doctor --check-env [PATH]`** — local-fs / git-state probe for a `.env` file: existence + POSIX permissions (0600 expected) + `.gitignore` coverage + git-tracking state. Same exit-code contract as `--check-model` (0 OK / 2 patchable / 1 blocker). `--check-model` and `--check-env` are now mutually optional and can be combined in one invocation.
- **Stdlib-only `.env` parser** (`coderouter.config.env_file`) — supports the subset that 1Password / sops / hand-edited files actually emit (bare values, `"double"` quotes with `\n`/`\t`/`\"` escapes, `'single'` quotes literal, optional `export` prefix, inline `#` comments on bare values, blank lines). No variable expansion, no command substitution, no multi-line. Rejects POSIX-invalid keys and unterminated quotes with `file:lineno`-prefixed errors.
- **`docs/troubleshooting.md` / `.en.md` §5** — new "`.env` security in practice" section with: threat model (what at-rest encryption can and can't defend), 4-point quick checklist, full 1Password CLI recipe (`op run --env-file=.env.tpl --`), direnv + sops recipe (encrypted `.env.enc` in git), OS Keychain recipes (macOS Keychain / Linux libsecret), `--env-file` layering patterns, and the "minimize key scope" hygiene reminder.
- **Why no encryption-in-app**: the design rationale is in §5-1 of the doc — encryption only addresses 2 of 7 realistic threats (cold-disk theft, backups), the decryption key has to live somewhere anyway, and most security-conscious users already run 1Password / sops. `--env-file` makes integration trivial; rolling our own AES would lock those users out of their existing workflow.

- Tests: 601 → **651** (+50, +8.3%): `tests/test_env_file.py` (26 — parsing edge cases, override semantics, multi-file layering), `tests/test_env_security.py` (15 — perms / .gitignore / git-tracking against real subprocess `git`, with `git` skip-marker for non-POSIX), `tests/test_cli.py` (+8 — `--env-file` end-to-end including malformed-file exit, `--check-env` exit codes, multi-`--env-file` precedence; +1 renamed `test_doctor_requires_at_least_one_flag` for the now-optional `--check-model` rule).
- Runtime deps: 5 → 5 (16 sub-release streak preserved). The new modules are pure stdlib (`os`, `stat`, `subprocess`, `shutil`, `pathlib`, `re`).
- Backward compat: `--check-model` is no longer required at the argparse level (now optional), but the CLI emits a friendly "provide --check-model and/or --check-env" + exit 1 when neither is passed. Existing scripts that always passed `--check-model` are unaffected.

### Why

v1.6.2 added 9 docs entries explaining `.env` footguns. The right next step is to give operators commands so they don't have to remember the entire doc — `--env-file` removes the export-in-`.env` confusion entirely (since the file is parsed by us, not sourced by the shell), and `--check-env` collapses the 3-grep manual checklist (`chmod ls -l`, `git check-ignore`, `git ls-files`) into one command with copy-paste fixes. Both ship "additive only" so v1.6.2 setups continue to work verbatim.

### Migration

None required. Existing setups (manual `export` in `.zshrc`, `source .env`, direnv-managed `.envrc`) all keep working unchanged. Adopt `--env-file` and `--check-env` opportunistically when they're the cleaner path for a given workflow.

---

## [v1.6.2] — 2026-04-24 (Troubleshooting split-out + .env / NIM YAML hygiene)

**Theme: a patch-level release consolidating pitfalls hit during real-world operation after the v1.6.1 release, into the documentation.** Discovered while running Llama-3.3-70B via NIM on real hardware from Claude Code, 3 issue clusters (a 401 caused by a missing `export` in `.env` / Llama-3.3-70B overreacting to Claude Code's system prompt and turning "こんにちは" into a `Skill(hello)` call / a structural mismatch where third-party plugins like `claude-mem` fail their internal calls when routed through CodeRouter) are consolidated into a standalone `docs/troubleshooting.md` (JA primary) + `.en.md` (EN sub). The README's Troubleshooting section is trimmed to a 30-second-readable summary plus a symptom-based index. `examples/.env.example` now requires `export` on every key, with loading steps / verification steps / explanations of the 4 API keys (NIM / OpenRouter / Anthropic / CODEROUTER_CONFIG) added to the top-of-file documentation. The 4 profiles in `examples/providers.nvidia-nim.yaml` (`claude-code-nim` / `nim-first` / `free-only-nim` / `nim-reasoning`) are reordered, per real-hardware verification, to demote Llama-3.3-70B to the tail and promote Qwen3-Coder-480B to first choice, with the rationale documented in YAML comments (Llama itself works fine; the issue is a compatibility mismatch with the Claude-Code-specific prompt). All changes are docs / examples only; the Python code's public API / ingress contract is completely unchanged.

- Tests: 601 → **601** (±0, no new logic. Indirectly verified by the NIM YAML invariants in `tests/test_examples_yaml.py` still passing after the profile reorder)
- Runtime deps: 5 → 5 (15 consecutive sub-releases unchanged)
- Non-breaking: only a docs split-out + adding export to the sample YAML / profile reordering — no change to Python code-side behavior

### Changes

- **New `docs/troubleshooting.md` (JA primary)** — splits out the full text of the README's Troubleshooting section, adding 5 topics discovered during v1.6.2 real-hardware verification as §1 (startup/config pitfalls) and §4 (Claude Code integration pitfalls). §1 covers CLI correction (`serve --mode`), the required `export` in `.env`, verifying the export via `env`, isolating the `Header of type authorization was missing` 401, and forgetting to reload `~/.zshrc` — 5 items. §4 covers Llama-3.3-70B-family excessive tool calling / `UserPromptSubmit hook error` (a structural mismatch with plugins like claude-mem) / auto-compact delay / making use of the dashboard — 4 items
- **New `docs/troubleshooting.en.md` (EN sub)** — 1:1 correspondence of chapter numbers / anchors with the JA version
- **Shortened README.md / README.en.md Troubleshooting section** — replaced with a 30-second-readable quick reference plus a symptom-based index (4 entry points); the 5 Ollama symptoms are reduced to a 1-line summary plus a link. The old anchors (`ollama-初心者--サイレント失敗-5-症状-v07-c` / `ollama-beginner--5-silent-fail-symptoms-v07-c`) are kept in both READMEs for backward compatibility
- **README.md / README.en.md documentation table of contents** — added "詰まったとき" / "When stuck" lines pointing to `troubleshooting.md` / `.en.md`, and added `troubleshooting` / `トラブルシューティング` to both READMEs' language switchers
- **`docs/usage-guide.md` / `usage-guide.en.md` §8 quick index** — rewrote the existing README references to point at `docs/troubleshooting.md`, adding 2 lines for `Header of type authorization was missing 401` and "greetings in Claude Code turning into `Skill(hello)` etc."
- **`examples/.env.example`** — unified all keys (`ALLOW_PAID` / `OPENROUTER_API_KEY` / `NVIDIA_NIM_API_KEY` / `ANTHROPIC_API_KEY` / `CODEROUTER_CONFIG`) into `export KEY=value` form. Added top-of-file documentation covering "how to load it (`source .env` works / `set -a && source .env && set +a` also works) / CodeRouter doesn't auto-source it / verification command (`env | grep ...`)"
- **Reordered the 4 profiles in `examples/providers.nvidia-nim.yaml`** — for all of `claude-code-nim` / `nim-first` / `free-only-nim` / `nim-reasoning`, changed the NIM lane order to Qwen3-Coder-480B → Kimi-K2 → Llama-3.3-70B (real-hardware verification found Llama-3.3-70B triggers excessive tool calling when used standalone with Claude Code, demoting it from first choice to the fallback tail). Added the selection rationale (real-hardware verification symptom log + a reference to `docs/articles/note-nvidia-nim.md` §6-2) to the comment block right before each profile
- **Extended the setup comments in `examples/providers.nvidia-nim.yaml`** — expanded the top-of-file "NVIDIA NIM setup" into 5 steps, explicitly noting the required `export` in `.env` / running `coderouter doctor` before startup / matching `--port 8088` to Claude Code
- **Revised `docs/articles/note-nvidia-nim.md`** — added the v1.6.2 verification log as §6 (3 real-hardware pitfalls) and §7 (making use of the dashboard), updating the §4 / §9 / §11 steps to real-hardware-verified commands

### Why

Right after the v1.6.1 release, when the user (= myself) stood up a NIM configuration on real hardware, `source .env` alone didn't get the env vars to `coderouter serve`'s child process, causing a `Header of type authorization was missing` 401 — and even past that, Llama-3.3-70B turned "こんにちは" into a `Skill(hello)` call, making it unusable — a double trap. In both cases, CodeRouter's code itself was fine; the real problem was that the documentation / sample configs didn't guard against the pitfalls one would actually hit on real hardware. v1.6.2 is a small patch release to reliably fold this "actually hit it in the field" knowledge into the docs / examples. Since it involves no code changes, it's contained entirely within CHANGELOG / plan.md / docs.

### Migration

None needed. The existing `~/.coderouter-t/providers.yaml` / existing env vars / existing Python imports / existing ingress contract are all unchanged. Users who copy `examples/providers.nvidia-nim.yaml` over `~/.coderouter-t/providers.yaml` will switch to the Qwen-first order if they overwrite-copy this release's YAML. Users running `.env` in the old form (without export) but without issues were mostly exporting it separately via a parent shell in most cases; simply `cp`-ing v1.6.2's `.env.example` as-is won't change behavior (declaring export twice is harmless).

---

## [v1.6.1] — 2026-04-23 (NIM free-tier + doc hygiene)

**Theme: a patch-level release right after v1.6.0's `auto_router` shipment.** Incorporates NVIDIA NIM's developer tier (40 req/min) as a first-class citizen into the local-first fallback chain, and simultaneously swaps the README / docs language priority to "Japanese main / English sub" (matching the reality of the target audience), rewrites the README hero into the strongest possible pitch — "the problem where tool calling breaks when using a local LLM with Claude Code, fixed at the router level" — and switches `coderouter/__init__.py`'s hardcoded `__version__` to go through `importlib.metadata.version("coderouter")` (making `pyproject.toml`'s `version` the single source of truth). All non-breaking — existing YAML / existing API / existing ingress contract are preserved verbatim; only new files, renames of existing docs, and a swap of the README hero are involved.

- Tests: 596 → **601** (+5, +0.8%), new `tests/test_examples_yaml.py` (loads all example YAMLs + NIM-specific invariants)
- Runtime deps: 5 → 5 (14 consecutive sub-releases unchanged)
- Non-breaking: only a new example YAML + a new reference doc + file renames (via `git mv`, preserving blame) + a README hero swap — the Python code's public API / ingress contract is completely unchanged

### Added

- **`examples/providers.nvidia-nim.yaml`** — a polished sample for the NVIDIA NIM developer tier (40 req/min free, no credit card needed). Defaults to an 8-stage chain across 4 profiles (`claude-code-nim` / `nim-first` / `free-only-nim` / `nim-reasoning`): `local (Ollama 7B/14B) → NIM 3 stages (Meta/Qwen/Moonshot, different vendors) → OpenRouter free 2 stages → paid`. Adoption decisions from live verification (2026-04-23, `integrate.api.nvidia.com/v1`):
  - `meta/llama-3.3-70b-instruct` — chat 540ms, tool_calls OK, streaming 260ms / 12 SSE chunks / usage returned correctly
  - `qwen/qwen3-coder-480b-a35b-instruct` — chat 634ms, tool_calls OK (480B MoE, specialized for agentic coding)
  - `moonshotai/kimi-k2-instruct` — chat 2.8s, tool_calls OK (vendor diversity within the NIM lane)
  - `qwen/qwen2.5-coder-32b-instruct` — chat works fine at 160ms, but NIM returns an HTTP 400, `"Tool use has not been enabled, because it is unsupported by qwen/qwen2.5-coder-32b-instruct"`, for tool-laden requests, so it's registered with a `tools: false` stanza so the capability gate routes tool-laden traffic elsewhere
  - `moonshotai/kimi-k2-thinking` — a variant that returns the answer wrapped in `<think>...</think>` inside `reasoning_content`, dedicated to the `nim-reasoning` profile. `output_filters: [strip_thinking]` is noted alongside it as a safety net
  - Rejected candidates (`nvidia/llama-3.1-nemotron-70b-instruct` → 404, `deepseek-ai/deepseek-r1` → 410 EOL 2026-01-26, `nvidia/llama-3.3-nemotron-super-49b-v1.5` → 200 OK but null content, `deepseek-ai/deepseek-v3.2` / `z-ai/glm4.7` → timeout) are recorded in YAML comments to prevent retrying them
- **`tests/test_examples_yaml.py`** — new, +5 tests loading all `examples/providers*.yaml` files and enforcing NIM-specific invariants in CI:
  - All example YAMLs load, with `default_profile` / profile-reference consistency preserved (parametrized across 4 files)
  - The 3 NIM tool-capable providers (`nim-llama-3.3-70b` / `nim-qwen3-coder-480b` / `nim-kimi-k2`) exist
  - Every `nim-*` stanza satisfies `api_key_env=NVIDIA_NIM_API_KEY` / `base_url=https://integrate.api.nvidia.com/v1` / `paid=False` (pins base_url by prefix-exact match, rejecting typos like `/v2`)
  - `nim-qwen-coder-32b-chat` declares `tools: false` (the capability-gate contract avoiding the HTTP 400)
  - `nim-kimi-k2-thinking` is not included in the primary `claude-code-nim` chain (its high latency and `reasoning_content` output shape don't suit Claude Code, so it's only reachable via the `nim-reasoning` profile)
- **`docs/free-tier-guide.md` / `docs/free-tier-guide.en.md`** — a new reference doc: a 250+ line operational guide focused solely on making the most of NIM + OpenRouter's free tiers:
  - A 3-tier comparison table (local / NIM 40 req/min / OpenRouter free 20 req/min + 200 req/day)
  - The design intent behind the `claude-code-nim` profile's 8-stage chain
  - Setup steps (3 commands) + where to obtain the 2 API keys that go in `.env`
  - A list of live-verified models across 3 tiers (adopted / chat-only / rejected)
  - 5 common footguns (NIM's "free" tier is credit-consuming, some models leak a non-standard `reasoning` field, Qwen2.5-Coder-32B has tools disabled, OpenRouter's 200 req/day cap, case-sensitive drift in NIM model IDs)
  - A real example of `coderouter doctor --check-model` output, with a reading guide
- Added bidirectional links to the free-tier guide right below the "Usage guide" pointer in `README.md` + `README.en.md`
- Added the NIM layer and a reference to the free-tier guide in the §6 OpenRouter pairing section of `docs/usage-guide.md` / `docs/usage-guide.en.md`

### Changed

- **Swapped the documentation language priority** — via `git mv`, swapped 5 pairs to Japanese main / English sub:
  - `README.ja.md` → `README.md` / `README.md` → `README.en.md`
  - `docs/usage-guide.ja.md` → `docs/usage-guide.md` / `docs/usage-guide.md` → `docs/usage-guide.en.md`
  - `docs/security.ja.md` → `docs/security.md` / `docs/security.md` → `docs/security.en.md`
  - `docs/quickstart.ja.md` → `docs/quickstart.md` / `docs/quickstart.md` → `docs/quickstart.en.md`
  - `docs/when-do-i-need-coderouter.ja.md` → `docs/when-do-i-need-coderouter.md` / `docs/when-do-i-need-coderouter.md` → `docs/when-do-i-need-coderouter.en.md`
- Kept `pyproject.toml readme = "README.md"`, so PyPI's readme display also switches to Japanese (matching the target audience)
- Simultaneously updated 20+ cross-references — both READMEs' language switchers, sibling-language cross-references within docs, anchor slug consistency within docs (Japanese README anchors use Japanese slugs, English side uses English slugs), GitHub blob URLs in `docs/articles/note-*.md` / `zenn-*.md` (e.g., `blob/main/docs/quickstart.ja.md` → `blob/main/docs/quickstart.md`), and internal links in `docs/designs/v1.6-auto-router.md`
- **Rewrote the README hero** (both languages):
  - Old: a generic tagline in the style of "Local-first coding AI with ZERO cost by default"
  - New: "The problem where tool calling breaks when using a local LLM with Claude Code — fixed at the router" — leading with the strongest pitch, that CodeRouter's tool-call repair path restores the `{"name":..., "arguments":...}`-as-plain-text symptom seen from quantized models like `qwen2.5-coder:7B` / `phi-4` / `mistral-nemo` into a valid `tool_use` block. Inserted an "and here's everything else CodeRouter does for you" block (doctor / reasoning-leak scrub / local → NIM 40 req/min → OpenRouter free → paid fallback / 5 deps / 601 tests) between the language switcher and the existing "What gets easier" section
  - Reserved an HTML comment placeholder for `docs/assets/before-after-toolcall.gif` (just uncomment once it's captured)
  - Synced the version badge from 1.5.0 → 1.6.1 and the test count from 453 → 601

### Fixed

- **`coderouter/__init__.py`** (`009b2b1`) — switched the `__version__` implementation from a hardcoded `"1.5.0"` to going through `importlib.metadata.version("coderouter")`. From now on, `pyproject.toml`'s single `version` line is the single source of truth, and both `coderouter --version` and `/healthz` correctly report the 1.6.x line. Fixes the issue recorded as a v1.6.0 known quirk in `docs/designs/v1.6-auto-router-verification.md`
- CI fix (`d0de1a9`)

### Non-breaking compatibility

- No change to the YAML schema — existing `providers.yaml` / `providers.auto.yaml` / `providers.auto-custom.yaml` work verbatim
- No change to the Python public API — only `coderouter/__init__.py`'s `__version__` retrieval path changed (the value comes from the same field with the same type)
- No change to the ingress contract — `/v1/messages` / `/v1/chat/completions` / `/metrics` / `/metrics.json` / `/dashboard` all remain verbatim
- File renames were done via `git mv`, preserving blame history. pyproject keeps `readme = "README.md"` (PyPI automatically follows the new Japanese readme)

---

## [v1.6.0] — 2026-04-22 (Umbrella tag — `auto_router`)

**Theme: land plan.md §11 "task-aware auto routing" in a single minor release.** Ships `auto_router` — which classifies the request body via declarative rules and auto-selects a profile — across 3 sub-releases: schema + classifier (v1.6-A) / ingress + metrics wiring (v1.6-B) / examples + docs (v1.6-C). Beginners just write `default_profile: auto` and the built-in rules take over (image → `multi` / code-fence ratio ≥ 0.3 → `coding` / otherwise → `writing`); intermediate users replace them with custom rules via the `auto_router:` block; and advanced users retain top priority for per-request overrides via `body.profile` / `X-CodeRouter-Profile` / `X-CodeRouter-Mode` (the path that's existed since v0.6-D) — all 3 tiers fit into one file. v0.6-D compatibility is fully preserved: unless you write `default_profile: "auto"`, the auto slot never fires at all, and existing configs keep working verbatim.

- Tests: 527 → **596** (+69, +13.1%): v1.6-A adds 26 new auto_router tests (classifier matchers / eager regex precompilation / the reserved `auto` name / bundled-profile requirements / fall-through / disabled) + v1.6-B ingress+metrics wiring tests + v1.6 validator tests
- Runtime deps: 5 → 5 (unchanged for 13 consecutive sub-releases; classification is pure regex + dict traversal, calling no external classifier)
- Non-breaking: only a new config field (`auto_router:`, optional) + a new sentinel (`default_profile: auto`, opt-in) + a new Prometheus counter (`auto_router_fallthrough_total`) — the existing ingress / precedence chain / metrics schema are preserved verbatim

### Added

- **v1.6-A — schema + classifier** (new coderouter/routing/auto_router.py, +245 LOC / coderouter/config/schemas.py +170 LOC)
  - `RuleMatcher` (Pydantic): represents the 4 matcher variants — `has_image` / `code_fence_ratio_min` / `content_contains` / `content_regex` — as fields, with an `_exactly_one` validator enforcing at load time that "each rule has exactly one matcher" (specifying multiple fails with a `pydantic.ValidationError`). `content_regex` is eagerly `re.compile`d at startup via `_compile_regex_eagerly`, so a typo crashes startup rather than silently failing on every request
  - `AutoRouteRule` / `AutoRouterConfig`: each rule carries an `id` (a stable identifier that rides along in the `auto-router-resolved` log payload, following a `builtin:` / `user:` prefix convention) / `profile` / `match`; the top level carries `disabled` (a hard off-switch) / `rules` (ordered, first-match-wins) / `default_rule_profile` (the fall-through target)
  - `BUNDLED_RULES` (declared in code, no YAML needed): `image-attachment → multi`, `code-fence-dense (ratio ≥ 0.3) → coding`, fall-through = `writing`. `BUNDLED_REQUIRED_PROFILES = ("multi", "coding", "writing")` is enforced at startup by `CodeRouterConfig._check_bundled_auto_router_requirements` — when `default_profile: auto` is set and `auto_router` is undefined, load fails if any of multi/coding/writing is missing, with the error message spelling out 3 options: "(a) define all 3 profiles / (b) override with your own `auto_router:` / (c) point `default_profile` at a different profile name"
  - `classify(body, config)`: walks only the single latest `role: user` message (not the full history, by design, to cut token consumption); recognizes `type: image_url` / `type: image` / `type: input_image` for `has_image` across both OpenAI and Anthropic content-list shapes, extracting text from both string and multimodal-list content. Fires an `auto-router-resolved` event on a matcher hit or an `auto-router-fallthrough` event otherwise (the source for the metrics counter described below)
  - `RESERVED_PROFILE_NAME = "auto"`: `CodeRouterConfig._check_auto_is_reserved` rejects `profiles[].name == "auto"` at startup, since it would collide with the `default_profile: auto` sentinel
  - +26 tests (tests/test_auto_router.py: each matcher / reserved name / bundled requirements / eager regex precompilation / disabled / fall-through / rejecting a rule with multiple matchers)
- **v1.6-B — ingress wiring + metrics** (coderouter/ingress/openai_routes.py + coderouter/ingress/anthropic_routes.py + coderouter/metrics/collector.py + coderouter/metrics/prometheus.py)
  - Inserted a single auto-router slot into the precedence chain of both the OpenAI and Anthropic ingresses (between v0.6-D's body.profile > `X-CodeRouter-Profile` > `X-CodeRouter-Mode` > `default_profile`, directly above `default_profile`): `if chat_req.profile is None and config.default_profile == RESERVED_PROFILE_NAME: chat_req.profile = classify(payload, config)`. When `default_profile != "auto"`, the slot is not taken, and the profile passed to the engine is bit-identical to pre-v1.6 (the engine's own default-profile fallback still runs as before)
  - Wired the `auto-router-fallthrough` event into a new counter, `_auto_router_fallthrough_total`, in `MetricsCollector._dispatch`, adding the same key to the snapshot's `counters` dict and to `reset()`. Fall-through is exposed as its own counter since it's a signal for "the rate at which no user-defined rule matches"
  - Added a new export, `coderouter_auto_router_fallthrough_total`, to `format_prometheus()` (with HELP text noting "no user/bundled rule matched, or auto_router.disabled=true"); `promtool check metrics` round-trips clean
  - Added "4. auto_router (v1.6-A, fires only when `default_profile == 'auto'`)" to the precedence-chain documentation (the module docstrings of both ingress files), so readers can trace where both the old and new paths take effect from a single place
- **v1.6-C — examples + quickstart addition** (new examples/providers.auto.yaml / new examples/providers.auto-custom.yaml / docs/quickstart.ja.md +1 section)
  - `examples/providers.auto.yaml`: a zero-config version. Just `allow_paid: false` / `default_profile: auto` / `display_timezone: Asia/Tokyo` plus 3 Ollama providers (qwen2.5-coder:7b / qwen2.5:7b / qwen2.5vl:7b) and 3 profiles (coding / writing / multi) are enough for the built-in rules to fire immediately. The top-of-file comment spells out the 3 `ollama pull` commands, noting the vl model can be skipped if you never send images (only image requests would fast-fail)
  - `examples/providers.auto-custom.yaml`: a copy-edit starting point for intermediate users. Builds on `auto.yaml`, inserting an `auto_router:` block demonstrating each of the 4 matcher variants across 4 rules (image → multi / a translation-intent regex → writing / a "Review this PR" substring → coding / fence ratio ≥ 0.15 → coding) plus `default_rule_profile: writing`. Comments spell out 3 points: "rules fully replace the bundled rules rather than merging with them," "each rule has exactly one matcher," and "rule order is first-match-wins"
  - Added a "Supplement: letting CodeRouter choose the profile for you" section to `docs/quickstart.ja.md` after Patterns A/B. Presents a 3-step merge path (C-1 pull → C-2 `cp auto.yaml` → C-3 customize) without rewriting the existing Patterns A/B

### Changed

- **Updated the official precedence-chain order for v1.6** — unified across plan.md §11 / ingress docstrings / quickstart into 5 stages: `body.profile > X-CodeRouter-Profile > X-CodeRouter-Mode > auto_router (default_profile == "auto") > default_profile`. The only addition versus v0.6-D's 4-stage description is the 4th stage; the existing stages 1-3 and the final default resolution are preserved verbatim

### Non-breaking compatibility

- Unless you write `default_profile: "auto"`, the auto slot is dead code (the branch in ingress is never taken at all). providers.yaml files up through v1.5.x work verbatim under v1.6.0
- The new `auto_router:` field is Optional with a default of None; if you don't write it, it's completely invisible from `CodeRouterConfig.model_validate`'s point of view
- The new Prometheus counter `coderouter_auto_router_fallthrough_total` is a scalar sitting alongside existing counters; the Prometheus scraper's view just gains one more line (nothing removed or renamed)

---

## [v1.5.0] — 2026-04-22 (Umbrella tag — Observability pillar)

**Theme: land plan.md §12 "measurement dashboard" wholesale in a single minor release.** Ships 6 sub-releases side by side: collection (v1.5-A `MetricsCollector` + `/metrics.json`), delivery (v1.5-B Prometheus `/metrics` + `$CODEROUTER_EVENTS_PATH` JSONL mirror), CLI visualization (v1.5-C `coderouter stats` curses TUI), HTML visualization (v1.5-D a single-page `/dashboard`), timezone display (v1.5-E the `display_timezone` config), and a bundled demo (v1.5-F `scripts/demo_traffic.sh`). Added a live-dashboard screenshot (`docs/assets/dashboard-demo.png`) to the README, along with a section spelling out "which questions can you answer at a glance from this dashboard" (that a model is working / being used / has switched over) — a rewrite anchored on operational questions rather than a list of raw numbers. **On the SemVer numbering**: since `v1.0.1 → v1.5.0` skips over the old v1.1 (= distribution / launcher / doctor, plan.md §11), the plan.md §11 header is relabeled to **v1.6**, and `v1.1.0`-`v1.4.x` are treated as skipped. The `v1.5.0` umbrella lands plan.md §12; §11 (v1.6) is the next minor.

- Tests: 457 → **527** (+70, +15.3%): v1.5-A +41 / v1.5-B +16 / v1.5-C ±0 (data/render layer, counted together with D's integration) / v1.5-D +12 / v1.5-E +1 / v1.5-F ±0
- Runtime deps: 5 → 5 — `curses` / `urllib` / `datetime.zoneinfo` are all stdlib, tailwind is a single CDN file, and the Prometheus format is generated via plain string building with zero SDK dependency (dependency count unchanged for 12+ consecutive sub-releases)
- Non-breaking: only new endpoints (`/metrics.json` / `/metrics` / `/dashboard`) + a new CLI (`coderouter stats`) + a new config field (`display_timezone`, optional) — existing endpoints / CLI / config are preserved verbatim

### Added

- **v1.5-A — `MetricsCollector` + `GET /metrics.json`** (coderouter/metrics/collector.py +463 LOC / coderouter/ingress/metrics_routes.py +92 LOC)
  - `MetricsCollector` is a subclass of `logging.Handler`. It attaches to the existing structured log stream (the JSON line shape unchanged since v0.3) simply via `addHandler()`, requiring zero rewrites to code-side log calls. Refreshes an in-memory ring (counters / providers / the most recent 50 events / a startup snapshot) every second in `_process_record()`
  - `GET /metrics.json` (a FastAPI JSON response) returns the snapshot as JSON. Designed as a single source of truth fetched by both the `/dashboard` HTML (v1.5-D) and the `coderouter stats` CLI (v1.5-C)
  - Attaches `MetricsCollector` to the root logger inside app.py's lifespan, firing a `coderouter-startup` event at startup to seed the `startup` snapshot with version / providers / profiles / allow_paid / mode_source
- **v1.5-B — Prometheus text exposition + JSONL mirror** (coderouter/metrics/prometheus.py +211 LOC)
  - `GET /metrics` returns the exposition as Prometheus `text/plain; version=0.0.4`. Uses the `coderouter_*` prefix convention, all scalar (no labels), a mix of gauges and counters (e.g., `coderouter_requests_total`, `coderouter_providers_healthy`)
  - When the `$CODEROUTER_EVENTS_PATH` env var is set, the collector appends the same log record as JSONL to that path. This is a fully independent side effect from the snapshot (the snapshot's in-memory ring is untouched; only the JSONL grows for long-term storage). Since it shares the same line shape as `JsonLineFormatter`, it slots directly into existing log-analysis pipelines
  - +11 tests (test_metrics_prometheus.py), +5 tests (test_metrics_jsonl.py)
- **v1.5-C — `coderouter stats` CLI TUI** (coderouter/cli_stats.py +752 LOC)
  - A 5-panel dashboard running on stdlib `curses` + `urllib` alone: Providers (health state + latency_ms + last_event), Fallback & Gates (fallback chain progression / ALLOW_PAID / capability-degraded count), Requests/min sparkline (a 60-second rolling bucket), Recent Events (the most recent 10, newest first, timezone-converted), Usage Mix (the ratio of local / free / paid)
  - `--once` mode: when there's no TTY (CI / a pipe / the `demo_traffic.sh` banner), renders once and prints a plain-text version to stdout. Separates the driver (the `_Screen` curses wrapper) from a pure data+render layer, so unit tests exercise only the render layer
  - +39 tests (test_cli_stats.py: data layer + render + `--once` snapshot)
- **v1.5-D — a single-page `/dashboard` HTML** (coderouter/ingress/dashboard_routes.py +493 LOC)
  - A single page built from one tailwind CDN file plus vanilla JS (`setInterval` + `fetch("/metrics.json")` every 2 seconds). htmx was avoided due to the 5-dep policy, and fetch polling was confirmed to provide sufficient TTFB (see plan.md §12.3.6)
  - The 5 panels express the same semantics as the CLI TUI (Providers / Fallback & Gates / Requests/min sparkline / Recent Events / Usage Mix) in HTML. Dark theme by default, with JS performing partial updates via `data-bind` attributes
  - +12 tests (test_dashboard_endpoint.py: HTML 200 / snapshot embedding / polling arguments)
- **v1.5-E — the `display_timezone` config field** (coderouter/config/schemas.py + cli_stats.py + dashboard_routes.py)
  - Declares `display_timezone: "Asia/Tokyo"` etc. at the top level of `providers.yaml` (optional, an IANA zone name, defaulting to UTC when unset). Aggregated UTC timestamps are untouched; only the **display layer** converts them: the CLI TUI uses `TzFormatter` (zoneinfo + caching, making repeated conversions to the same zone O(1)), while the HTML uses `Intl.DateTimeFormat` (browser-native, carrying the zone through)
  - Propagated to the JS side via `/metrics.json`'s `config.display_timezone`; a reference stanza was added to `examples/providers.yaml`
  - +1 test (a dedicated display_timezone fixture confirming tz-aware datetime formatting matches)
- **v1.5-F — `scripts/demo_traffic.sh`** (+861 LOC)
  - A weighted scenario picker: normal 4/10 / stream 3/10 / burst+idle 2/10 / fallback 1/10, with a paid-gate every 8th tick. Each scenario is designed with an intent for how it should move the dashboard's panels (e.g., burst+idle → observing a sparkline spike followed by decay during idle)
  - Flags: `--duration <sec>` (default 60, or `∞` to run continuously until SIGINT), `--serve` (starts a mock HTTP server on `127.0.0.1:4444`, runnable standalone locally), `--dry-run` (runs only the scenario picker's probability-distribution sampler, sending no traffic)
  - A banner + expected-count table + elapsed/progress readout (`tick N/M, elapsed=XmYs`), a family of `scenario_*` functions, unified `log_info/ok/warn/err` logging
  - macOS `/bin/bash` 3.2 compatibility fixes: (i) since a heredoc-inside-`$()` can occasionally hang the bash 3.2 parser, `PLAN_PY_SRC` / `BODY_PY_SRC` are hoisted out into single-quoted variables → `python3 -c "$VAR"`; (ii) since a bare `wait` can hang due to missed SIGCHLD when aggregating concurrent background jobs, a new `wait_pids()` helper (individually waits on PIDs collected via `$!`) is applied in `scenario_fallback_burst` / `scenario_burst_then_steady`
- **README dashboard snapshot** (README.md / README.ja.md + docs/assets/dashboard-demo.png)
  - Inserted a "Live dashboard" section right after the architecture diagram. The caption is anchored on operational questions rather than a list of raw numbers — "which questions can you answer at a glance from this dashboard": which provider is alive and currently responding / whether a fallback fired recently / whether the paid gate remains closed / recent request volume / the most recent N events
  - A panel-layout explanation (top-left to bottom-right: Provider / Fallback & Gates / Requests/min sparkline / Recent Events / Usage Mix) so readers can match the image against the caption

### Changed

- **Relabeled plan.md §11's header from "v1.1" to "v1.6"** — since `v1.0.1 → v1.5.0` skipped §11 (distribution / launcher / doctor). Replaced all v1.1 mentions with v1.6 across the TOC / §6.1 milestone table / §6.2 release history detail / the body text (5 places), documenting that the `v1.1` number is treated as skipped
- **Marked README "Coming next" as v1.5 ✅ shipped** — around README.md L149 + L324, and the corresponding spot in README.ja.md. Old: "v1.1 — launcher; v1.5 — metrics dashboard"; new: "v1.5 ✅ — metrics (shipped); v1.6 — launcher (the old v1.1 label, bumped down by v1.5 shipping first)"
- **docs/usage-guide.{md,ja.md}** — replaced "v1.1" Docker image tracking with "v1.6 (formerly v1.1)"
- **pyproject.toml / coderouter/__init__.py** — bumped `version = "1.0.0"` / `__version__ = "1.0.0"` → `1.5.0`

### Non-Added (explicitly out of scope / deferred)

- **Retrospective `docs/retrospectives/v1.5.md`** — the umbrella narrative is planned to be written separately. This release is condensed into CHANGELOG + the plan.md status line + the README snapshot; the retrospective is deferred since it's worth writing about the design through-line spanning the 6 sub-releases (e.g., the 2-consumer-1-producer design sharing a pure data+render layer between the CLI and HTML, the isolation principle that the env-gated JSONL side effect doesn't depend on the snapshot, and the "aggregate in UTC, render in local" principle confining `display_timezone` to the display layer alone)
- **Landing the v0.7 / v1.0 follow-ons** — items pushed to v1.1+ in the CHANGELOG [v1.0.1] entry (output_filters chain-level override / doctor probe-grouping refactor / num_predict-without-max_tokens / the Ollama 0.20.5 silent-override investigation) remain untouched in v1.5. They'll be picked up in `v1.6` (formerly v1.1) or v1.7. v1.5 prioritizes concentrating scope on observability, completing only the "observe" half of "observe → correct"

### Follow-ons

- **A live-verify scenario for v1.5.0** — following the pattern of v0.5-verify / v1.0-verify, write `scripts/verify_v1_5.sh`. Assert, via the delta between bare (collector disabled) and tuned (collector enabled + `$CODEROUTER_EVENTS_PATH` set), that "a JSONL line gets written / `/metrics` returns 200 / `/dashboard` returns HTML"
- **Dashboard retrospective narrative** — as above
- **A runbook section for `scripts/demo_traffic.sh` in the README** — currently only documented in `--help`. The scenario distribution / expected count / what `--serve` means / the why behind the `wait_pids` workaround needed for bash 3.2 compatibility would be valuable as operator-facing docs
- **Long-running demo evidence** — only a screenshot was attached this time; recording "the dashboard stayed stable across 3 minutes x 87 requests" as a time-series log in a separate section would be useful for later regression judgment

---

## [v1.0.1] — 2026-04-21 (Hygiene pass — public error hierarchy + docstring + mypy strict)

**Theme: after the v1.0.0 umbrella, clean up 3 loose ends that hadn't been fully filled in, in a single release.** (1) A new `CodeRouterError` root exception — ties the existing 3 leaves (`AdapterError` / `NoProvidersAvailableError` / `MidStreamError`) together under a common parent, so a downstream integrator can catch every exception the router raises with a single `except CodeRouterError`. Adds a new `coderouter.errors` module, re-exported at the `coderouter` top level; all existing import paths remain non-breaking. (2) Raises docstring coverage from **75.6% to 91.2%** — measured via `interrogate`; every public-API-facing file (the model / logging layers of adapters / routing / ingress / translation) is now at 100%, with the remainder confined to stream-state internal helpers / CLI / doctor / private translation functions. (3) Confirmed mypy `--strict` reports 0 errors (10 errors that had accumulated since v0.6 are resolved via `response_model=None` + `AsyncIterator[str]` in the ingress routes, `isinstance(adapter, AnthropicAdapter)` narrowing in fallback.py, and a `StreamChunk.usage` type declaration — this release documents the portion that wasn't recorded during v1.0-verify). **453 → 457 tests** (+4 comes from the new `tests/test_errors.py`, a guard locking the `CodeRouterError` inheritance invariant for the 3 leaf exceptions). Since the only real public-API addition is `CodeRouterError` itself, and all existing CI gates / real-hardware verification pass, this is **patch-level under semver (no minor bump needed)**.

- Tests: 453 → **457** (+4)
  - New `tests/test_errors.py` +4 (an instance-level smoke test confirming that the 3 classes `AdapterError` / `NoProvidersAvailableError` / `MidStreamError` inherit from `CodeRouterError`, plus actually raising `AdapterError("boom", provider="p", status_code=500, retryable=False)` and catching it via `except CodeRouterError`)
- Runtime deps: 5 → 5 (`interrogate`, used to measure docstring coverage, is dev-only and never enters the runtime)
- Non-breaking: only the base class of the existing 3 exceptions changes from `Exception` to `CodeRouterError`; since `CodeRouterError(Exception)`, callers that wrote `except Exception` keep working as before. All import paths remain at their existing locations (e.g., `from coderouter.adapters.base import AdapterError` is unchanged).

### Added

- **`coderouter/errors.py` — the root `CodeRouterError(Exception)` class** (~30 LOC)
  - The common parent of the existing 3 leaf exceptions. Behaves identically to `Exception` (a `pass`-only definition); it exists to fix the API surface so a downstream integrator can catch router-side failures wholesale via `except CodeRouterError`, without needing to individually import and enumerate each leaf. The docstring notes explicitly that "leaves are free to grow over time," documenting the invariant for when new exceptions get added in the future
  - Placement rationale: putting the root in `coderouter/adapters/base.py` or `coderouter/routing/fallback.py` would be a breeding ground for import cycles (the same failure mode as the `logging.py` approach). `errors.py` is kept independent as a dependency-less leaf module that both adapters and routing import, so `errors.py` settles at the deepest layer of the import graph
- **Re-exported `CodeRouterError` from `coderouter/__init__.py`** — makes `from coderouter import CodeRouterError` possible in a single line. Declares `__all__ = ["CodeRouterError", "__version__"]`, making the top-level public API explicit
- **`tests/test_errors.py` — a regression guard for the inheritance invariant** +4 tests
  - `test_adapter_error_inherits_root` / `test_no_providers_available_inherits_root` / `test_mid_stream_error_inherits_root` — statically assert the inheritance relationship via `issubclass(X, CodeRouterError)`. Lockstep protection: if someone in the future reverts a leaf's base back to `Exception`, the unit test FAILs
  - `test_adapter_error_instance_is_caught_as_root` — confirms at the instance level that actually raising `AdapterError(...)` can be caught via `except CodeRouterError`. Also locks the `__str__` format via `str(exc) == "[p status=500] boom"` (so a future change to `AdapterError.__str__` would be caught as a separate test failure)

### Changed

- **Swapped the base class of `AdapterError` / `NoProvidersAvailableError` / `MidStreamError` from `Exception` to `CodeRouterError`** — a 1-2 line change across 3 files
  - `coderouter/adapters/base.py`: added `from coderouter.errors import CodeRouterError`, changed `class AdapterError(Exception)` to `class AdapterError(CodeRouterError)`
  - `coderouter/routing/fallback.py`: added the same import, changed `class NoProvidersAvailableError(Exception)` to `(CodeRouterError)` and `class MidStreamError(Exception)` to `(CodeRouterError)`
  - Existing signatures / docstrings / behavior remain verbatim. Since it still inherits from `Exception` in the MRO, code catching it with a bare `except:` or `except Exception:` is unaffected
- **Docstring coverage 75.6% → 91.2%** (measured via `interrogate coderouter`, target 90%)
  - Files brought to 100%: `adapters/base.py` (added to Message / ChatRequest / AdapterError.__init__+__str__ / BaseAdapter.__init__+name), `adapters/openai_compat.py` (_headers / _payload / _url / generate / stream), `adapters/anthropic_native.py` (_url / _headers), `routing/fallback.py` (NoProvidersAvailableError.__init__ / MidStreamError.__init__ / FallbackEngine class + __init__ + generate), `ingress/app.py` (create_app / lifespan / healthz / root / __getattr__), `ingress/openai_routes.py` (chat_completions), `ingress/anthropic_routes.py` (messages / _format_anthropic_sse), `output_filters.py` (StripThinkingFilter.__init__+feed / StripStopMarkersFilter.__init__+feed / OutputFilterChain.__init__+is_empty), `translation/anthropic.py` (AnthropicTextBlock / AnthropicUsage), `translation/convert.py` (_convert_anthropic_tools), `logging.py` (JsonLineFormatter.format / get_logger)
  - Remaining gap (21 items, out of scope this time): `cli.py`'s `_build_parser` / `main` (2), private helpers inside `doctor.py` (5), internal readers in `config/capability_registry.py` (3), `config/loader.py`'s `_candidate_paths` (1), the `_StreamState` stream-state helpers in `translation/convert.py` (8), a closure inside `translation/tool_repair.py` (1), 2 helpers in `translation/convert.py` — all genuinely internal / closure / stream-state plumbing, implementation details outside the public surface. The 90% floor is already met on the public API
- **Confirmed mypy `--strict` reports 0 errors** — resolved the 10 errors that had been missed during v1.0-series compaction (some were already fixed as of v1.0-C; this release documents the portion that wasn't recorded)
  - `coderouter/ingress/openai_routes.py` / `anthropic_routes.py`: added type annotations `@router.post(..., response_model=None)` + `payload: dict[str, Any]` + `-> StreamingResponse | dict[str, Any]` + `AsyncIterator[str]` (addressing FastAPI rejecting a union return type as a Pydantic field, plus importing AsyncIterator)
  - `coderouter/routing/fallback.py`: at the call sites for Anthropic-shaped methods in `generate_anthropic` / `stream_anthropic`, rewrote the `if is_native:` boolean guard to `if isinstance(adapter, AnthropicAdapter):` — kept the `is_native` boolean for logging, but switched the method-call branch to a form mypy can narrow (since `BaseAdapter` itself doesn't declare `generate_anthropic` / `stream_anthropic`, a boolean variable can't narrow it)
  - `coderouter/adapters/base.py`: explicitly declared a `usage: dict[str, Any] | None = None` field on `StreamChunk` (Pydantic's `extra="allow"` permits this at runtime but mypy can't see it, so mypy was flagging an unexpected keyword where `convert.py`'s reverse translation passes a `usage=...` kwarg)

### Non-Added (explicitly out of scope)

- **CI enforcement of docstrings** (promoting `interrogate` to a pre-commit / CI gate) — there's a temptation to set 91.2% as a floor, but this release is scoped as a single hygiene pass to avoid turning it into a treadmill. Gating is split out into a separate ticket for the v1.1 line, once the v1.0-series follow-ons settle down
- **Other `Exception`-inheriting classes pulled in transitively via pytest** (candidates like an upstream 4xx abstraction in adapters) — this pass only attributed the existing 3 leaves to `CodeRouterError`. The convention for attaching new leaves to the same root is already documented in the `errors.py` docstring and the header of `tests/test_errors.py`, so the moment the invariant breaks when a new leaf is added (i.e., a test FAILs), it'll be caught

### Follow-ons

- **`docs/retrospectives/v1.0.1.md`** — this release is a hygiene pass, so there isn't enough substance to warrant a narrative. Skipping the retrospective. Instead, the v1.1 retrospective's opening will mention in one line that "the loose ends were tidied up in v1.0.1 before moving into v1.1," preserving the lineage
- **Docstring 90% CI gate** — add a `[tool.interrogate]` section to `pyproject.toml` with `fail-under = 90`, running `interrogate coderouter` as a `pre-commit` or CI step. Currently, coverage could gradually slip without a manual regression check (if new code forgets a docstring)
- **The remaining 21 private docstrings** — mostly stream-state plumbing, so there's a "write it but nobody reads it" cost-benefit concern. That said, `_StreamState._start_event` / `_close_current_block` / `_open_text_block` / `_open_tool_use_block` / `_handle_delta` are unreadable without knowledge of the Anthropic SSE spec, so even a 1-line docstring outlining the state machine's role has standalone value. Separately in v1.1 or v1.2

---

## [v1.0.0] — 2026-04-20 (Umbrella tag — The observation loop, closed)

**Theme: the umbrella tag bundling v1.0-A / v1.0-B / v1.0-C.** Concretizes, in a single minor release, the principle previewed in the v0.7 retrospective that "transformation comes paired with a probe." v1.0-A bundles the declarative `output_filters` filter chain (transformation) together with the doctor reasoning-leak probe extension (probe) in the same release; v1.0-B replaces symptom #1 from v0.7-B (input-side `num_ctx` truncation) from indirect to direct detection — a canary `ZEBRA-MOON-847` plus ~5K tokens of padding plus echo-back drives a 5-verdict branch and an `extra_body.options.num_ctx: 32768` patch; v1.0-C mirrors the same technique to the output side — sending a deterministic `"Count from 1 to 30"` prompt via streaming, directly detecting output truncation from `finish_reason="length"` plus short content, and emitting an `options.num_predict: 4096` patch. Both of Ollama's truncation knobs (input-side `num_ctx` / output-side `num_predict`) are now directly observable. Also assembled as v1.0-verify: a 3-scenario real-hardware runner (`scripts/verify_v1_0.sh`) plus the `verify-ollama-bare` / `verify-ollama-tuned` provider pair — reusing the v0.5-verify bare/tuned delta assertion pattern for its 2nd instance, achieving **3/3 PASS** on a real-hardware run (Ollama 0.20.5 + qwen2.5-coder:7b, 2026-04-20 23:23 JST). As a side finding, Ollama 0.20.5 turned out to be a build that silently overrides request-time `options.num_ctx` / `options.num_predict` on `/v1/chat/completions` — since the symptom couldn't be induced on the bare side, an **ADVISORY branch** was added to scenarios B+C (bare is advisory; the tuned side's `[OK]` flip and the reflected patch-default-value are the hard evidence), and the num_ctx probe's canary-echoed branch in `coderouter/doctor.py` was split into 3 branches (separating `declared is None` from `declared < threshold but still echoed` diagnostically). The narrative layer lives at [`docs/retrospectives/v1.0.md`](./docs/retrospectives/v1.0.md), per-sub-release feature detail is in `[v1.0-A]` / `[v1.0-B]` / `[v1.0-C]` below, and the live-verify evidence doc is at [`docs/retrospectives/v1.0-verify.md`](./docs/retrospectives/v1.0-verify.md).

- Tests: 382 → **453** (+71, +18.6%): v1.0-A +49 / v1.0-B +10 / v1.0-C +12 / v1.0-verify ±0
- Runtime deps: 5 → 5 (output_filters is a pure-Python scanner, the num_ctx probe is padding + string matching, and the streaming probe is `httpx.AsyncClient().stream()` + string-based SSE parsing — zero SDK dependency maintained across 10+ consecutive sub-releases)
- Design through-lines:
  - **Transformation + probe in the same release** (v1.0-A) — the v0.7-B retrospective's declaration became habit in v1.0. v1.0-A bundles the output filter chain and the reasoning-leak probe extension into the same release
  - **A symptom-orthogonality heuristic for probe ordering** (v1.0-B / v1.0-C) — since `num_ctx` interferes with the verdict of a subsequent probe, it's placed **near the front of the chain** (right after auth); `streaming` is an orthogonal axis, so it goes **last**. Documented as "interferes-goes-first, orthogonal-goes-last"
  - **A stateful boundary scrubber with partial-suffix hold-back** (v1.0-A) — `_max_suffix_overlap` holds back a partial tag at a chunk boundary, sharing a single code path across streaming and non-streaming. Establishes the shape for future filter additions
  - **Ollama-shape signals as an abstraction** (shared by v1.0-B / v1.0-C) — the 2-signal check `_is_ollama_like(provider)` (`:11434` port OR `extra_body.options.num_ctx` declared), defined in v1.0-B, is reused verbatim by v1.0-C; a 3rd Ollama-specific probe could plug into the same helper
  - **Bare/tuned delta assertion as a live-verify convention** (v1.0-verify) — confirmed via a 2nd instance that the v0.5-verify pattern generalizes, becoming the standard form for v1.1-verify onward

### v1.0 umbrella-level follow-ons

See the relevant section for each v1.0 sub-release's follow-ons. What cuts across at the umbrella level:

- **A joint `num_ctx` + `num_predict` probe** — a post-processing idea to emit both knobs on the same Ollama upstream as a single verdict + a single merged patch (`extra_body.options: {num_ctx: 32768, num_predict: 4096}`), fusing the patch on the `format_report()` side when both are present. v1.1 scope
- **Generalizing `_has_output_length_knob` / `_has_context_length_knob`** — when a 2nd non-Ollama upstream with a tunable context/output cap appears (vLLM's `--max-model-len` / Together streaming quirks), rename `_is_ollama_like` and extend it to multi-signal. Currently YAGNI
- **`FallbackChain.output_filters: list[str] | None`** — a chain-level override matching the v0.6-B shape (`timeout_s` / `append_system_prompt`). A use case for toggling filters differently between staging/prod. v1.0-D or v1.1-A scope
- **A doctor probe-grouping refactor** — grouping the 6-probe chain (`auth / num_ctx / tool_calls / thinking / reasoning-leak / streaming`) into `[auth] → [truncation: num_ctx, streaming] → [toolcall: tool_calls, thinking, reasoning-leak]` plus `--only truncation` / `--only toolcall` flags. v1.1 scope
- **An Anthropic-native variant of v1.0-verify scenario A** — an `/v1/messages` → `kind: anthropic` provider with `output_filters` declared. First live-verify evidence of per-text-block chain isolation. v1.0-verify-B or v1.1-adjacent
- **Investigating Ollama 0.20.5's `options.*` passthrough** — the real-hardware run for v1.0-verify detected behavior silently overriding request-time `options.num_ctx` / `options.num_predict` via `/v1/chat/completions` (worked around via the ADVISORY branch after failing to induce the symptom on the bare side). In v1.1, investigate (a) which Ollama build introduced the override by checking upstream CHANGELOG, (b) whether the native `/api/generate` endpoint honors it, (c) whether a forcing path like the `OLLAMA_CONTEXT_LENGTH` env var can be used. Depending on the outcome, decide whether to change the doctor probe's inducement method (request-body → env-var injection) or switch the probe target to `/api/generate`. Currently operating with an advisory-bare / hard-tuned asymmetry
- **`recover_garbled_tool_json` / the tool-call translation layer / Code Mode / prompt cache / the 14-case regression / tuning defaults** — of the §10.1 original scope, only output-cleaning was delivered in v1.0.0. Recommend explicitly re-scoping the remaining 5 into v1.1+ (including updating plan.md §10's DoD table)
- **`scripts/release-close.py`** — has appeared as a follow-on across 4 consecutive retrospectives without being implemented. Would automate ~9 doc touchpoints x 3 sub-releases = ~27 manual edits
- **A test-count auto-updater** — across 3 consecutive retrospectives, auto-generating the chart line from `pytest --collect-only -q | wc -l`. Cheapest to implement alongside `release-close.py`

---

## [v1.0-C] — 2026-04-20 (Doctor streaming-path probe — direct Ollama output-side truncation detection)

**Theme: the mirror image of v1.0-B — now that input-side truncation can be directly observed, observe output-side truncation at the same granularity.** v1.0-B directly detected, via canary echo-back, the symptom of a prompt getting truncated from the front due to insufficient `num_ctx`, producing an empty response. But there's another silent failure operators actually run into with Claude Code: **on the output side** — the response gets cut off mid-way with `finish_reason: length`. Typically this happens when Ollama's `options.num_predict` is left at its default of 128 (older builds) or 256 (some forks). With v0.7-B's 4 probes and v1.0-B's `num_ctx` probe, there was no declarative-layer knowledge of how far upstream would generate for a request that doesn't explicitly set `max_tokens`, so this symptom could only be caught as the operator's vague sense that "the response seems to cut off partway." v1.0-C's streaming probe consumes the SSE stream to the end and looks at `finish_reason` plus the measured content length, emitting a NEEDS_TUNING verdict and an `options.num_predict: 4096` patch. This extends the v0.7 retrospective's "silent failures need a direct probe" symptom coverage from 5 to 6. With v1.0-B (input-side) and v1.0-C (output-side), both faces of Ollama's 2-knob truncation are now directly detectable.

- Tests: 441 → **453** (+12)
  - `tests/test_doctor.py` +12 (2 patch-emitter tests: `_patch_providers_yaml_num_predict` shape + YAML round-trip / 10 probe behavior tests: non-11434 port SKIP / non-Ollama kind SKIP chain / successful stream → OK / `finish_reason=length` + short content → NEEDS_TUNING + num_predict patch / zero-chunk JSON-instead-of-SSE → NEEDS_TUNING advisory no patch / no `[DONE]` terminator → OK with note / `extra_body.options.num_ctx` signal on non-11434 port fires the streaming probe / outbound body carries `stream: true` + merged extra_body / HTTP 500 during streaming → SKIP / auth 401 short-circuits the streaming probe)
- Runtime deps: 5 → 5 (`httpx.AsyncClient().stream("POST", ...)` is an existing dependency; SSE parsing is pure string slicing, no dependency added)
- Non-breaking: since v1.0-B already moved the `_oa_provider` fixture to `localhost:8080`, the existing 36 tests pass with the streaming probe also SKIPped as non-Ollama-shape. Added a 5th SSE mock (`_add_sse_ok_mock`) with a single line to the existing 5 Ollama-shape-opt-in num_ctx tests

### Added

- **`coderouter/doctor.py` — the `_probe_streaming(provider)` async function** (~130 LOC)
  - A deterministic prompt: `_STREAMING_PROBE_USER_PROMPT = "Count from 1 to 30, one number per line. Output only the numbers, nothing else."` — a normal response is roughly 60-90 characters (2-digit numbers + newline x 30); when it caps out around `num_predict=128`, content becomes dramatically shorter, an observable pattern. No hallucination resistance is needed as with a canary — the output length **itself** is the observed quantity
  - Threshold constants: `_STREAMING_PROBE_MIN_EXPECTED_CHARS = 40` (just listing 30 numbers with newlines yields 60+ chars, so falling below 40 is a clear-cut truncation case), `_STREAMING_PROBE_NUM_PREDICT_DEFAULT = 4096` (an operational value covering Claude Code's typical response of 200-2000 tokens while avoiding VRAM pressure)
  - Probe body construction: `body = dict(provider.extra_body); body.update({model, messages, max_tokens=128, temperature=0, stream=True})` — like the `num_ctx` probe, this is one of only 2 probes that actually send the operator-declared `options.*` (the other 4 bypass the adapter layer to look at the raw upstream)
  - A 5-way verdict branch: (a) non-Ollama-shape (`_is_ollama_like` False) → SKIP; (b) transport error / 4xx / 5xx → SKIP + a diagnostic note; (c) 2xx + 0 chunks (a JSON response came back / non-standard SSE framing) → NEEDS_TUNING **advisory** (since it's a server-side setting, no patch is emitted; reports "upstream silently ignored `stream: true`"); (d) 2xx + `finish_reason="length"` + content < 40 chars → NEEDS_TUNING + an `num_predict: 4096` patch; (e) 2xx + `finish_reason="stop"` + sufficient content → OK (OK plus an informational note if the `[DONE]` terminator is missing)
- **The `_http_stream_sse(url, *, headers, body, timeout) -> tuple[int|None, list[dict], bool, str]`** helper — consumes SSE via `httpx.AsyncClient().stream("POST", ...)`, JSON-parses `data: <json>` lines from `resp.aiter_lines()`, and observes the `data: [DONE]` sentinel. Returns (status, chunks, saw_done, error_text); transport errors are normalized to (None, [], False, error_msg) (simplifying the caller's branch logic)
- **`_patch_providers_yaml_num_predict(provider_name, desired_predict=4096) -> str`** — a sibling to `_patch_providers_yaml_num_ctx`, emitting `extra_body.options.num_predict: 4096`. The header comment explicitly says "merge into any existing extra_body.options" (anticipating the common case where the operator has already declared `num_ctx`, with instructions to avoid collision). Guaranteed parseable via a YAML round-trip test
- **`_STREAMING_PROBE_USER_PROMPT` / `_STREAMING_PROBE_MIN_EXPECTED_CHARS` / `_STREAMING_PROBE_NUM_PREDICT_DEFAULT`** constants — declared at the module level in the same section as `_NUM_CTX_ADEQUATE_THRESHOLD`, directly importable from tests (the v0.5-onward pattern of locking behavior invariants via tests)
- **`check_model` orchestration update**: extends the 5-probe chain to a 6-probe chain, running in the order `auth → num_ctx → tool_calls → thinking → reasoning-leak → streaming`. Placing streaming **last** is deliberate — num_ctx (input-side) / tool_calls / thinking / reasoning-leak are all declarative-layer probes checking "declared capability vs. measured reality," while streaming is an independent observation axis specific to the output side. Placing it where it won't interfere with a preceding probe's verdict means streaming's NEEDS_TUNING won't paper over another probe's dominant signal (the opposite judgment from placing num_ctx **before** tool_calls in v1.0-B, since these symptom categories are orthogonal)
- **Extended the auth short-circuit SKIP tuple**: `("num_ctx", "tool_calls", "thinking", "reasoning-leak")` → `("num_ctx", "tool_calls", "thinking", "reasoning-leak", "streaming")`. Broadcasts the invariant that all subsequent probes get filled with SKIP when auth fails, from 5-probe to 6-probe

### Changed

- **`coderouter/doctor.py` module docstring**
  - Updated the symptom #1 row in the symptom-mapping table to `"empty response / nonsensical response → num_ctx probe (v1.0-B) + streaming probe (v1.0-C)"`. Notes that v1.0-B enabled catching the input side directly, and v1.0-C now catches the output side too (a section comment clarifies that symptom #1 is actually a beginner-level symptom where 2 kinds of truncation converge, and the probe side needed to be split accordingly)

- **README.md — v1.0-C status section**
  - Pivoted the heading from `## Status: v1.0-B — Direct num_ctx probe` to `## Status: v1.0-C — Streaming-path probe (2026-04-20)`
  - Repositioned the paragraph as the output-side sibling of v1.0-B: `finish_reason=length` plus short content is the typical fingerprint, and the symptom visible to a Claude Code user is "the response cuts off partway." The primary cause is usually an Ollama build left at the `options.num_predict` default of 128/256. Documents the count-1-to-30 deterministic prompt, the `extra_body.options.num_predict: 4096` patch, catching the secondary "2xx with 0 chunks" symptom as advisory, and reusing the Ollama-shape gating from v1.0-B. Updated the test count from 441 → **453** (+12), for a v1.0-series running total of +71 (49 + 10 + 12)

- **`tests/test_doctor.py` — added a 5th SSE mock to the existing Ollama-shape tests**
  - Added a single line, `_add_sse_ok_mock(httpx_mock, url)`, to the 5 existing `extra_body={"options": {"num_ctx": ...}}`-style tests (declared-high canary-echoed OK / declared-low canary-missing bump / declared-adequate canary-missing intrinsic-limit / `extra_body.options.num_ctx` signal on non-11434 / `extra_body` merges into the outbound body), so the 6-probe chain can run all the way to the end
  - Added the `_sse_stream_count_body(*, numbers=30, finish_reason="stop", include_done=True) -> bytes` / `_add_sse_ok_mock(httpx_mock, url, **kwargs)` test helpers — shared across the 10 streaming probe tests, assembling `text/event-stream` content-type + `data: {...}` x N + a closing chunk with `finish_reason` + an optional `data: [DONE]`

### Design notes

- **Why the streaming probe runs last, not before tool_calls like num_ctx.** v1.0-B's num_ctx probe was placed **before** tool_calls because truncation (input-side) has an interference relationship that could cause tool_calls absence to be misdetected. v1.0-C's streaming probe is output-side, independent of other probes' decision space — even if "the response cuts off partway" happens after a preceding probe already ran OK, each addresses a different symptom. Placing it at the **very end** of the probe chain reduces to zero the risk of streaming's NEEDS_TUNING papering over another probe's dominant signal. The principle: "if symptom categories are orthogonal, go last; if they interfere, go first"
- **Why gate on `_is_ollama_like`, not all openai_compat.** Non-Ollama upstreams (OpenRouter / Together / Groq / Anthropic) have no path that honors `options.num_predict` — even if the streaming probe detected truncation, there'd be nowhere to send the patch (writing `extra_body.options.num_predict: 4096` would have no effect). Among Ollama-compatible implementations, rare forks that don't honor `num_predict` would still return sufficiently long content, exiting with OK rather than SKIP; forks that do honor it get the correct patch delivered. The gate is shared with v1.0-B's num_ctx probe (the same `_is_ollama_like` helper) — room to extend the probe to `_has_output_length_knob(provider)` with an additional signal, as more upstreams natively expose something like vLLM's `max_model_len` / `max_tokens`. Currently YAGNI
- **Why "count from 1 to 30," not "echo this canary."** v1.0-B's question is whether the prompt got truncated (input-side), for which canary echo-back is direct. v1.0-C's question is whether the response itself gets truncated (output-side), where the observable is the fact that "the response is too short" **itself**. A canary approach wouldn't catch "the canary came through but the explanation afterward got cut off" (the canary is short enough to fit even at num_predict=128). Counting 1-to-30 gives a clear separation — roughly 60-90 chars normally vs. 15-30 chars when capped at num_predict=128 — allowing a reliable decision at a low threshold (40 chars). A bonus is that numbers are hallucination-free — "0, 1, 2..." plus newlines plus temperature=0 makes it deterministic
- **Why `num_predict: 4096`, not "find the model's max."** The same philosophy as `num_ctx` — if doctor had to carry a model-specific limit database, that would exceed its responsibility. 4096 comfortably covers Claude Code's typical response (200-2000 tokens) while not exhausting the KV cache headroom of 7B-14B models on consumer GPUs (24GB or less). An operator who wants to know a model's true max (Llama 3.1's 32K completion, Qwen2.5's 4K default, etc.) can dial it up after receiving the patch
- **Why the `num_predict` patch and the `num_ctx` patch are separate emitters, rather than one "num_everything" helper.** `num_ctx` is input-side (the buffer size for holding the entire prompt), while `num_predict` is output-side (the token budget allocated to the response). Under OpenAI-compatible API semantics, the former is implicit (allocated automatically based on however much prompt was sent), whereas Ollama defaults to 2048 unless explicitly declared via `options.num_ctx`. `num_predict` is conceptually the counterpart to OpenAI's `max_tokens`, but some Ollama builds ignore the request-body `max_tokens` in favor of `options.num_predict` (observed empirically). Keeping the two as separate emitters lets the probe's verdict distinguish between (a) input truncation / (b) output truncation / (c) both. If an operator receives both as NEEDS_TUNING, they can merge them into a single YAML edit as `extra_body.options: {num_ctx: 32768, num_predict: 4096}` — the header comment already documents the merge direction
- **Why advisory (no patch) for "2xx with 0 chunks."** Cases where upstream silently ignores `stream: true` and returns a non-SSE response (some reverse proxies, some older LM Studio builds, etc.) have no fix on the client-side providers.yaml — it's an upstream server misconfiguration or fork bug. Emitting a patch would leave the operator with "nowhere to paste it," causing confusion. Instead, it surfaces the advisory "server returned 2xx with 0 streaming chunks — upstream may have ignored `stream: true`," pointing toward remediation options like (a) checking the upstream's streaming configuration or (b) a future flag to force `stream: false` on the CodeRouter `providers.yaml` side (not currently available, a future consideration). This preserves the contract, carried forward from v0.7-B, that "if a patch can't be emitted, the verdict is still given but the patch field stays empty"

### Follow-ons

- **An Anthropic-native streaming variant for v1.0-D** — currently the probe only fires for `openai_compat` + Ollama-shape. An idea to add a separate `_probe_streaming_anthropic` for Anthropic native (`kind: "anthropic"`). However, `api.anthropic.com`'s `max_tokens` must be explicitly set on the request side (there's no server-side default), so the symptom's path differs — if Claude Code already includes `max_tokens` in the request, the symptom mostly won't occur. Low priority; decide after measuring the need via v1.0-C's real-hardware verify
- ~~**Real-machine verify for v1.0-C**~~ — **Landed 2026-04-20** via `scripts/verify_v1_0.sh` scenario C (streaming probe). The combined v1.0 verify (A + B + C in one runner) subsumes the originally-scoped per-release verify script. Bare `verify-ollama-bare` triggers the `streaming …… [NEEDS TUNING]` verdict with `num_predict: 4096` patch; tuned `verify-ollama-tuned` flips to `streaming …… [OK]`. Evidence inline in [`docs/retrospectives/v1.0-verify.md`](./docs/retrospectives/v1.0-verify.md) (v0.5-verify pattern — evidence embedded, not a separate file). Nginx reverse-proxy 0-chunk reproducer was deferred — the unit tests already lock that branch via pytest-httpx, and the symptom is environmentally specific (fork-dependent, not Ollama-default) so live verify would be flakier than it's worth
- **vLLM `max_model_len` detection (output-side)** — vLLM sets an output-side cap via `--max-model-len`, restricted per-request via `extra_body.max_tokens`. Semantically corresponds to Ollama's `num_predict`. Room to rename `_is_ollama_like` to `_has_output_length_knob` and add the vLLM signal, v1.2+
- **Streaming probe timeout tuning** — currently uses `timeout=provider.doctor_probe_timeout_s` (default 5.0s). Count-1-to-30 can take 2-4 seconds under CPU inference (a 14B model + CPU-only CI); an idea to offer a dedicated timeout knob for the streaming probe in the future (`CodeRouterConfig.doctor.streaming_probe_timeout_s`). Currently green with the default in CI, deferred
- **Canary collision for the streaming probe** — unlike v1.0-B, the streaming probe uses no canary, so there's no collision risk. However, a model could theoretically "summarize and output only 5 numbers" for the count 1-to-30 task (rare). That could produce a false NEEDS_TUNING with content length ≈ 10 chars. No issue observed in practice so far; deferred

---

## [v1.0-B] — 2026-04-20 (Doctor `num_ctx` probe — direct Ollama truncation detection)

**Theme: the flip side of the v0.7 retrospective's "transformation comes paired with a probe" — replacing a spot where the probe itself relied on indirect symptom detection with direct detection.** When `coderouter doctor --check-model` shipped in v0.7-B, 4 of the 5 symptoms in plan.md §9.4 (symptoms 2-5) already had a direct probe. Only symptom #1 remained — silent prompt truncation caused by Ollama's `num_ctx` default of 2048 — hanging off the **indirect path** of "the tool_calls probe reports `no tool_use emitted`." This risks suggesting the wrong remediation to the operator (`capabilities.tools: false`). v1.0-B's `num_ctx` probe directly observes truncation via a canary echo-back scheme, emitting the **correct** patch, `extra_body.options.num_ctx: 32768`. Going from v0.7-B's 4-probe suite to v1.0-B's 5-probe suite means all 5 symptoms now have a dedicated probe.

- Tests: 431 → **441** (+10)
  - `tests/test_doctor.py` +10 (2 patch-emitter tests: `_patch_providers_yaml_num_ctx` shape + YAML round-trip / 8 probe behavior tests: port-heuristic SKIP / 11434 canary-missing NEEDS_TUNING / declared-high canary-echoed OK / declared-low canary-missing bump / declared-adequate canary-missing intrinsic-limit note / `extra_body.options.num_ctx` signal on a non-11434 port / `extra_body` merges into the outbound body / auth short-circuit includes num_ctx SKIP)
- Runtime deps: 5 → 5 (the canary + padding are pure strings, no dependency added)
- Non-breaking: the existing 36 tests are unaffected, since the fixture `_oa_provider`'s default base_url was moved from `localhost:11434` to `localhost:8080` (the port heuristic classifies it as non-Ollama-shape → probe SKIP); a replace-all of test URLs (31 locations) was done ahead of time

### Added

- **`coderouter/doctor.py` — the `_probe_num_ctx(provider)` async function** (~85 LOC)
  - Canary constants: `_NUM_CTX_PROBE_CANARY = "ZEBRA-MOON-847"` (an all-caps hyphenated token, a shape that never occurs in natural language), `_NUM_CTX_PROBE_PADDING_SENTENCE = "The quick brown fox jumps over the lazy dog near the river bank today. "` (~16 tokens), `_NUM_CTX_PROBE_PADDING_REPEATS = 300` (~4800 tokens — reliably exceeding Ollama's default of 2048), `_NUM_CTX_ADEQUATE_THRESHOLD = 8192` (accounting for the minimum headroom needed to receive Claude Code's 15-20k system + tool prompt)
  - Prompt construction: `"CANARY: ZEBRA-MOON-847\n\n" + padding*300 + "\n\nQuestion: What exact canary token appeared at the very beginning of this message?"`. Placing the canary **at the front** is the key trick — Ollama truncates the beginning on overflow
  - The only probe that shallow-merges `provider.extra_body` into the outbound body (the other 4 probes ignore extra_body). This lets it observe whether truncation occurs while **actually using** the `options.num_ctx` the operator declared. Merge order: `body = dict(provider.extra_body); body.update({model, messages, max_tokens, temperature})` — top-level probe fields win on collision with extra_body (the same semantics as the adapter's merge order)
  - A 5-way verdict branch: (a) canary echoed & declared ≥ 8192 → OK (operator tuned); (b) canary echoed & nothing declared → OK with an informational note (upstream is using a non-default default, unusual but benign); (c) canary missing & nothing declared → `NEEDS_TUNING` + a patch adding 32768; (d) canary missing & declared < 8192 → `NEEDS_TUNING` + a patch bumping to 32768; (e) canary missing & declared ≥ 8192 → `NEEDS_TUNING` + a note that "the model's intrinsic limit may be lower than declared" (rare, occurs with e.g. Llama 3 8B's 8192 cap)
- **`_is_ollama_like(provider) -> bool`** — a 2-signal detector: (a) the base_url contains `:11434` (Ollama's canonical port); (b) `provider.extra_body.options.num_ctx` is declared (only Ollama honors this path, so if the operator wrote it, it's Ollama-shape by construction). `kind != "openai_compat"` short-circuits to False. Doesn't fire for llama.cpp (:8080) / OpenRouter / Together / Groq / Anthropic native — preventing false positives
- **`_declared_num_ctx(provider) -> int | None`** — a helper that safely extracts `extra_body.options.num_ctx` as an int. Returns None if `options` isn't a dict or the value isn't an int
- **`_patch_providers_yaml_num_ctx(provider_name, desired_ctx=32768) -> str`** — a nested YAML patch emitter: `extra_body: \n  options:\n    num_ctx: <n>`. Symmetric with v0.7-B's `_patch_providers_yaml_capability` / v1.0-A's `_patch_providers_yaml_output_filters`, with a comment header explicitly noting "merge into any existing extra_body" (accounting for operators who already have other options set)
- **`check_model` orchestration update**: changed the probe execution order to `auth → num_ctx → tool_calls → thinking → reasoning-leak`. **Placing num_ctx before tool_calls** is deliberate — under the old behavior, truncation was misdetected as `no tool_use emitted`, suggesting a `tools: false` patch. Running num_ctx first makes the truncation verdict dominant in the report, letting the operator apply the correct remediation
- **Extended the auth short-circuit SKIP tuple**: `("tool_calls", "thinking", "reasoning-leak")` → `("num_ctx", "tool_calls", "thinking", "reasoning-leak")`. Broadcasts the existing invariant that all subsequent probes get filled with SKIP when auth fails, from 4-probe to 5-probe

### Changed

- **`coderouter/doctor.py` module docstring**
  - Updated the symptom #1 row in the symptom-mapping table to `"empty response / nonsensical response → num_ctx probe + basic-chat probe"` (in the v0.7-B era it said `num_ctx probe` even though it didn't actually exist yet — v1.0-B makes it literally exist). Also updated the symptom #3 row to `thinking probe + reasoning-leak content-marker detection (v1.0-A)` (reflecting v1.0-A's side effect)

- **`README.md` — Ollama beginner symptom #1**
  - Removed the notice "currently does not probe num_ctx (planned follow-on); symptom shows up indirectly as tool_calls probe..."
  - Rewrote the expected diagnostic in the `coderouter doctor --check-model` output example to `num_ctx: NEEDS_TUNING — canary missing from reply; upstream truncated (no ``extra_body.options.num_ctx`` declared)`, pre-printing the patch doctor now emits directly
  - Added an explanation, "As of v1.0-B the doctor probe detects this directly," spelling out the canary + 5K padding scheme and the Ollama-shape gating (:11434 / declared options) in one paragraph

- **`tests/test_doctor.py` — fixture migration** (`_oa_provider`'s default base_url)
  - Pivoted from `localhost:11434/v1` to `localhost:8080/v1` (the llama.cpp port). Bulk-updated all URL references (31 locations) using the fixture across the existing 36 tests via `replace_all`. This makes `_is_ollama_like` return False, so existing tests pass without adding a single mock (the num_ctx probe SKIPs)
  - Extended the fixture signature to accept `extra_body: dict[str, Any] | None = None`, so the Ollama-opt-in tests can declare `extra_body={"options": {"num_ctx": 32768}}`

### Design notes

- **Why the `:11434` + `options.num_ctx` disjunction, rather than a boolean config flag.** Requiring explicit operator declaration, in the pattern of v0.6-A / v0.6-D onward, was an option, but (a) a fresh Ollama install uses `:11434` 100% of the time, so it can be inferred from the port; (b) the moment an operator writes `options.num_ctx`, it can't be anything but Ollama (no other openai_compat upstream honors this path) — so "implicit signal of intent" is enough, and a new flag shouldn't be added. False positives are limited to self-built servers using `:11434`, but even then, if that server is an Ollama-compatible implementation honoring `num_ctx`, the probe works correctly; if it doesn't honor it, the canary gets echoed and it exits with OK (worst case, just an informational OK, no damage)
- **Why 300 repeats, not 500 or 150.** The minimum padding to **reliably** induce truncation past Ollama's default `num_ctx = 2048` tokens is around 130 repeats (just over 2048 tokens), but accounting for chunking overhead and BPE tokenization variance, 300 repeats (~4800 tokens) gives margin. 500 repeats risks the default `timeout_s=5.0` fixture (especially on CPU-only CI); 150 repeats is too close to the 2048 boundary and would pass for some tokenizers. 300 gives a safe margin empirically
- **Why the canary is "ZEBRA-MOON-847," not a hash or UUID.** A UUID risks the LLM "hallucinating another UUID" even at the start of the prompt (too close to a natural-language prior shape). A hash (e.g., `a7f9e2`) risks the model recognizing it as a "reasonable answer shape" instead. All-caps plus 2 hyphens plus a letter/digit mix is a shape that never occurs in natural text — the model can't produce it unless it actually saw it in the prompt. At 14 characters, it's short enough that regardless of how the tokenizer's BPE splits it, an `in` match will still catch it
- **Why the default patch emits `32768`, not "find the max the model supports."** 32768 is a practical value that comfortably receives Claude Code's prompt (system + tool + user history), while also being a threshold at which most models (7B-14B) run on consumer hardware (M-series 16-64GB / a 24GB VRAM GPU). Trying to research each model's true max (Llama 3.1's 128K, Qwen2.5's 32K, etc.) to compute an optimal value would mean doctor maintaining a separate model-name-to-context registry, exceeding its responsibility. Proposing 32768 uniformly, leaving room for the operator to dial it down under memory constraints, is operationally cheaper
- **Why the num_ctx probe runs before tool_calls, not last.** Under the v0.7-B-era reporting shape, the tool_calls probe was the first to observe the truncation symptom, reporting `NEEDS_TUNING: capabilities.tools`. Placing the num_ctx probe after it would produce a redundant 2-patch suggestion ("tools=false + num_ctx=32768"). Placing it before makes the num_ctx verdict naturally dominant, and once num_ctx becomes OK on a subsequent run, tool_calls' genuine verdict becomes observable. "Surface the most dominant symptom first" is the concrete realization of the v0.7 retrospective's "there's an optimal diagnostic ordering for silent failures"
- **Why the probe shallow-merges `extra_body`.** The other 4 probes (auth / tool_calls / thinking / reasoning-leak) ignore `extra_body` — their purpose is "bypass the adapter layer to see the raw upstream response," and mindlessly merging `extra_body` would end up testing interactions with fields the adapter adds (`think: false` etc.) as well. The num_ctx probe is the sole exception — its very reason for existing is to observe "whether the declared `options.num_ctx` actually takes effect," so a probe that doesn't send `extra_body.options` would be meaningless. The merge is top-level shallow (option fields stay as a nested dict as-is); probe-specific top-level fields (`model` / `messages` / `max_tokens` / `temperature`) overwrite extra_body in a fixed order to preserve determinism

### Follow-ons

- ~~**Real-machine verify for v1.0-B**~~ — **Landed 2026-04-20** via `scripts/verify_v1_0.sh` scenario B (num_ctx probe). Bare `verify-ollama-bare` triggers `num_ctx …… [NEEDS TUNING]` + `num_ctx: 32768` patch; tuned `verify-ollama-tuned` flips to `num_ctx …… [OK]`. Paired with scenario C (streaming) they share a single doctor CLI invocation per side (the 6-probe chain runs all at once). Evidence inline in [`docs/retrospectives/v1.0-verify.md`](./docs/retrospectives/v1.0-verify.md)
- **Probe model detection accuracy** — room to rename `_is_ollama_like` to `_has_context_length_knob` plus add multi-signal support (e.g., vLLM's `extra_body.max_model_len`), for when a future non-Ollama upstream with a `num_ctx`-like knob shows up claiming openai_compat. Currently YAGNI
- **Dynamic threshold** — `_NUM_CTX_ADEQUATE_THRESHOLD = 8192` is currently hard-coded. If Claude Code's system prompt grows to 30k in the future, 8192 won't be enough (even today, declaring all tools reaches 18-20k). An idea to provide `CodeRouterConfig.doctor.min_context: int` so operators can override it, v1.2 scope (at the point when doctor's configuration hierarchy gets organized)
- **Canary collision risk** — extremely low probability, but a model whose training corpus happens to include "ZEBRA-MOON-847" could hallucinate the canary even when it wasn't actually truncated. Counter-measure: switch to generating the canary randomly each time (process-local seed, preserving reproducibility within the same session). No issue observed in practice; deferred

---

## [v1.0-A] — 2026-04-20 (Declarative output cleaning chain)

**Theme: the first application of the principle previewed in the v0.7 retrospective, "transformation comes paired with a probe."** v0.5-C's passive reasoning-field stripping was a passive layer that only worked "if the model happened to emit a reasoning field." But real-world Ollama / HF-distilled models push `<think>...</think>` or `<|turn|>` / `<|channel>thought` etc. **inline into the content channel** (v0.7-C README symptom #3). This adds a filter chain that **declaratively** strips these at the adapter boundary. Just writing `output_filters: [strip_thinking, strip_stop_markers]` in `providers.yaml` makes it work consistently across streaming / non-streaming and both the OpenAI-compat / Anthropic native adapters. Also extends v0.7-B's reasoning-leak probe — detecting content-embedded `<think>` / stop markers and emitting a `providers.yaml` patch enumerating the needed filters. With this triad — **declaration (v0.7-A YAML) → probe (v0.7-B doctor) → transformation (v1.0-A filter chain)** — the observation loop for "beginner symptom 3 (think-leak)" finally closes.

- Tests: 382 → **431** (+49)
  - `tests/test_output_filters.py` +31 (pure unit tests: chunk-boundary correctness / chain composition / validating the registry)
  - `tests/test_output_filters_adapters.py` +12 (adapter integration: generate / stream / tail flush / per-block chain isolation)
  - `tests/test_config.py` +3 (`output_filters: [...]` schema validation at load time)
  - `tests/test_doctor.py` +3 (reasoning-leak probe: content `<think>` / stop marker detection + patch shape + staying silent when already configured)
- Runtime deps: 5 → 5 (a pure stateful scanner, no dependency added)
- `examples/providers.yaml`: enabled `output_filters: [strip_thinking]` on the `ollama-qwen-coder-7b` / `-14b` / `ollama-hf-example` stanzas

### Added

- **`coderouter/output_filters.py`** (a new module, public API ~280 LOC)
  - `DEFAULT_STOP_MARKERS: tuple[str, ...]` — 6 markers observed in practice with Claude Code: `<|turn|>` / `<|end|>` / `<|python_tag|>` / `<|im_end|>` / `<|eot_id|>` / `<|channel>thought`. Includes the unclosed-bracket-abbreviated form (`<|channel>thought`) based on real-hardware observation. Changes are required to include a CHANGELOG note (locked via the `test_default_stop_markers_contents` regression test)
  - `KNOWN_FILTERS: tuple[str, ...] = ("strip_thinking", "strip_stop_markers")` — the registry, 2 filters as of v1.0-A
  - `validate_output_filters(names: list[str]) -> None` — raises `ValueError` enumerating known names for an unknown name. A typo like `strp_thinking` gets an error message fixable by copy-paste
  - `OutputFilter` (Protocol) — `feed(text: str, eof: bool = False) -> str` / `modified: bool`. Stateful, one instance per stream as a principle
  - `StripThinkingFilter` — inclusively removes `<think>...</think>`. Partial tags (`<thi` / `</thi`) are held back at chunk boundaries; on EOF with an unmatched open tag, the tail is dropped (preventing leakage of an unterminated thinking block)
  - `StripStopMarkersFilter` — via `_earliest_match(buffer)`, iteratively strips whichever marker hits first; partial markers (`<|pyth`) are held back. A `<|` that isn't a marker gets flushed at EOF
  - `_max_suffix_overlap(buffer, needle)` — the longest N where `buffer[-N:] == needle[:N]`, the core routine behind chunk-boundary hold-back (shared by both filters)
  - `OutputFilterChain(filter_names)` — applies filters in declaration order. `any_applied` / `applied_filters()` / `names` / `is_empty` / `feed`. An unknown name raises `ValueError` at construction (fast-fail)
  - `apply_output_filters(names, text) -> (scrubbed, applied)` — a non-streaming convenience function. An empty chain is identity, returning only the names of filters that were applied
- **`coderouter/config/schemas.py` — `ProviderConfig.output_filters`** (a new field)
  - Places `output_filters: list[str] = Field(default_factory=list, ...)` right after `append_system_prompt` (a sibling position to v0.6-B)
  - Calls `validate_output_filters` via `@model_validator(mode="after") _check_output_filters_known`. The import is local (preserving the one-way dependency from config → output_filters, avoiding a cycle)
- **`coderouter/adapters/openai_compat.py` — the filter hook** (`generate` + `stream`)
  - `generate()`: inserts iteration over `data["choices"]` right after the existing v0.5-C reasoning strip, applying `OutputFilterChain.feed(text, eof=True)` to each `message.content`, logging once per message via `log_output_filter_applied` if `any_applied`
  - `stream()`: lazily constructs a `filter_chain: OutputFilterChain | None` from the provider's declaration at the entry point, tracking `output_filter_logged: bool` and `last_chunk_template: dict | None`. Applies `chain.feed(text)` to `delta["content"]` per-chunk (eof=False). Changes the previous `return` on receiving `[DONE]` to a `break`, consolidating processing into the post-loop flush code path. Flushes the held-back tail via `chain.feed("", eof=True)`, and if non-empty, yields a single synthetic SSE chunk borrowing `id` / `model` / `created` / `system_fingerprint` from `last_chunk_template` (not breaking OpenAI SDK compatibility), finally resending `[DONE]` to terminate the actual stream
- **`coderouter/adapters/anthropic_native.py` — the filter hook** (`generate_anthropic` + `stream_anthropic`)
  - `generate_anthropic()`: after parsing the response, for each block in `data["content"]` where `block["type"] == "text"`, builds a fresh `OutputFilterChain` and applies it to `block["text"]`. Logs the union of applied filters (one log per response)
  - New helper `_process_stream_event_for_filters(event, *, chains, logged_flag) -> list[event]`
    - `content_block_start` (type=text) → stores a fresh chain in `chains[index]`
    - `content_block_delta` (type=text_delta) → mutates in-place via `chains[index].feed(delta["text"])`
    - `content_block_stop` → obtains the tail via `chains[index].feed("", eof=True)`, and if non-empty, prepends a synthetic `content_block_delta` event for the same index **before** the `content_block_stop` (naturally preserving event order). A mutable cell, `logged_flag: list[bool] = [False]`, guarantees one log per stream
  - `stream_anthropic()`: replaces the 2 `yield AnthropicStreamEvent(...)` call sites with `for out_event in self._process_stream_event_for_filters(...): yield out_event`, initializing `filter_chains: dict[int, OutputFilterChain] = {}` and `logged_flag = [False]` at the entry point. Since it's a **per-text-block chain**, an unfinished `<think>` in block 0 doesn't leak into block 1
- **`coderouter/logging.py` — the `log_output_filter_applied` chokepoint helper**
  - `OutputFilterAppliedPayload` TypedDict: `provider: str` / `filters: list[str]` / `streaming: bool`
  - `log_output_filter_applied(logger, *, provider, filters, streaming)` — info level, the same pattern as `log_capability_degraded` (a single chokepoint, a typed payload, easy cross-provider aggregation)
- **`coderouter/doctor.py` — the reasoning-leak probe extension**
  - Changed the prompt from `"In one word: capital of France?"` to `"Think step by step about the capital of France, then answer in one word."` plus `max_tokens=128` (inducing a thinking block to reliably exercise the leak path)
  - After parsing: computes `has_think = "<think>" in content_text` / `leaked_markers = [m for m in DEFAULT_STOP_MARKERS if m in content_text]`, cross-checking against the current state of `provider.output_filters`
  - `needs_strip_thinking or needs_strip_markers` → verdict `NEEDS_TUNING`, with `_patch_providers_yaml_output_filters(provider_name, filters)` emitting a copy-paste-ready patch of the form `providers:\n  - name: <p>\n    output_filters: [<missing>]`
  - Updated the OK detail for the not-detected case to `"no `reasoning` field observed and no content-embedded markers — nothing to strip."` (kept as-is since the existing `test_reasoning_leak_not_present_reports_clean` test asserts on "nothing to strip")
  - Added an `output_filters` row to `format_report`'s declarations section

### Changed

- **`examples/providers.yaml`**
  - `ollama-qwen-coder-7b`: added `output_filters: [strip_thinking]` plus an explanatory comment (Qwen2.5-Coder intermittently leaks `<think>` under Claude Code's tool-heavy prompting; `strip_stop_markers` isn't needed since Ollama's chat template terminates cleanly with `<|im_end|>`)
  - `ollama-qwen-coder-14b`: likewise `output_filters: [strip_thinking]`; unconditionally enabled since scrub cost is cheap at 14b
  - `ollama-hf-example` (a commented stanza): clarifies in the stanza's comment that remediation for symptom 3 (v0.7-C README) now has 2 paths (source-side `/no_think` / output-side `output_filters`), presenting `output_filters: [strip_thinking]` in commented-in form (uncomment → immediately active). Removed the old `reasoning_passthrough` hint line (the v1.0-A path is more general)

### Design notes

- **Why `output_filters` lives on `ProviderConfig`, not `FallbackChain`.** Whether a filter is needed depends on the model family (Qwen2.5-Coder leaks `<think>`; Claude doesn't), not on the chain. The same philosophy as v0.6-B adding `FallbackChain.timeout_s` / `append_system_prompt` as provider overrides: "the default is a provider declaration," "the chain can partially override if needed (add in v1.0-B if it becomes necessary)"
- **Why a stateful filter, not regex `re.sub`.** When a chunk gets split mid-tag during streaming (`<thi` / `nk>`), regex won't match (the first chunk, "hello <thi", leaks as-is). Writing a scanner that holds back a partial suffix via `_max_suffix_overlap` is shorter than composing regexes, and shares the same code path across streaming and non-streaming. The `re.sub` route was rejected since it would require a separate non-streaming-only implementation
- **Why a per-text-block chain on Anthropic.** Anthropic native emits multiple `content_block`s (text / tool_use / thinking) in series within one response. If `<think>` is left unfinished at the end of block 0 while block 1 (text) begins, a single per-stream chain would end up treating all of block 1 as hidden, losing visible content. Keeping an isolated chain per block index via `dict[int, OutputFilterChain]` means state resets at block boundaries too. If block 0 is left unfinished, only block 0's text gets dropped at EOF (block 1 flows through normally)
- **Why `_process_stream_event_for_filters` returns a list of events.** The synthetic flush event (emitting the held-back tail) needs to be inserted **immediately before** `content_block_stop`. Rather than breaking the original code where the caller simply did `yield event`, the return value is unified as a list plus a `for ... yield` expansion to express the possibility of "1 input event → 0 to 2 output events." This could also be written using Python generator delegation (`yield from`), but a list is easier to test
- **Why fast-fail at config load, not at first request.** Deferring detection of an unknown filter name until request time lengthens the time from config deploy to symptom observation. Calling `validate_output_filters` from both the `ProviderConfig` validator and the `OutputFilterChain` constructor achieves (a) bulk-validating all providers at YAML load time and (b) the same error surfacing on paths that construct a chain directly (e.g., in tests)
- **Why `log_output_filter_applied` fires at most once per stream.** Filter application can happen on every SSE delta, but the granularity useful for observability is "did strip_thinking fire for this request," not "how many chunks got scrubbed." The mutable flag pattern (`output_filter_logged: bool` / `logged_flag: list[bool] = [False]`) guarantees one log per stream. Non-streaming is the same (one log per message)
- **Why the doctor probe prompt became "Think step by step about...".** With the old "capital of France?" prompt, a tuned model would answer in one shot without emitting a thinking block, missing the chance to detect the leak. Changing the prompt to "step by step" and bumping max_tokens to 128 induces thinking while reliably surfacing the `<think>` / stop markers that should be detected. A follow-on from the v0.7-B retrospective's "a probe should actively activate the path it needs to observe"
- **Why the probe emits filter patches, not just diagnostics.** The same philosophy as v0.7-B's tool_calls probe emitting `capabilities.tools: false` in copy-paste form: pair the detected symptom with **remediation the operator can apply immediately**. The patches are listed in detection order (`strip_thinking` first if `<think>` is found, `strip_stop_markers` second if markers are found), matching chain declaration order, so pasting it into YAML works exactly as expected

### Follow-ons

- ~~**Real-machine verify for v1.0-A**~~ — **Landed 2026-04-20** via `scripts/verify_v1_0.sh` scenario A (filter chain). Routes a `/v1/chat/completions` request through CodeRouter against `verify-v1-bare` then `verify-v1-tuned`, asserts the tuned response's `message.content` is `<think>`-free AND the server stderr log contains an `output-filter-applied` record for `filters=["strip_thinking"]`. Bare side is advisory (qwen is stochastic; if it doesn't emit `<think>` on the sample the script reports "symptom could not be induced" rather than failing). Evidence inline in [`docs/retrospectives/v1.0-verify.md`](./docs/retrospectives/v1.0-verify.md). v0.7 retrospective follow-on #5 (real-machine verify for v0.7) remains scheduled for v0.8 scope — that pass will also sanity-check model-capabilities.yaml matcher against live provider metadata
- **Additional filters** — candidates to add to `KNOWN_FILTERS`: `strip_tool_call_text_wrapper` (a scrubber pairing with v0.3-A's text→tool_calls lifting, "in case it leaks anyway"), `collapse_whitespace` (some models leave a double space like `"hello  world"` behind after `<think>` stripping). Currently YAGNI
- **Filter performance under chunk storms** — for models where 1 SSE chunk contains only 1-2 characters (some Ollama configurations), `_max_suffix_overlap` becomes O(N*M) as `len(buffer) * len(markers)`. Currently negligible at DEFAULT_STOP_MARKERS' 6 markers x an average marker length of 10 (worst case 60 ops/chunk), but if the marker count grows in the future, switch to a trie-based approach (v1.5+ scope)
- **Chain-level `output_filters` override** — anticipates cases wanting a chain-level override like v0.6-B's `FallbackChain.timeout_s` / `append_system_prompt` (e.g., filters disabled in a staging environment, enabled in prod). Currently addressable by splitting providers, but under consideration as `FallbackChain.output_filters: list[str] | None` for v1.0-B or v1.1
- **Doctor probe: streaming path** — the current `_probe_reasoning_leak` hits the non-streaming endpoint. It doesn't catch the rare failure mode where `<think>` only leaks when split across a chunk boundary during streaming. Plan to add `_probe_reasoning_leak_streaming` in v1.0-C or later (reusing the streaming verify pattern from v0.5.1 A-2; v1.0-B resolved the direct num_ctx probe first)

---

## [v0.7.0] — 2026-04-20 (Umbrella tag — Beginner UX, made legible)

**Theme: the umbrella tag bundling v0.7-A / v0.7-B / v0.7-C.** A minor release making "I set up Ollama but it doesn't work" diagnosable in a single command. Taking the 5 silent-fail symptoms from plan.md §9.4 (num_ctx truncation / tools incompetence / `<think>` leak / model-tag 404 / missing API key) as a contract, this closes the beginner-UX observation loop in 3 stages: (A) moving declarations out of Python literals into YAML, (B) implementing a live probe (`coderouter doctor --check-model <provider>`) that cross-checks declarations against real hardware, and (C) chaptering, in the README Troubleshooting section, a 3-4-point set of symptom x probe command x YAML patch x fix command for each. The narrative layer lives at [`docs/retrospectives/v0.7.md`](./docs/retrospectives/v0.7.md), with per-sub-release feature detail in `[v0.7-A]` / `[v0.7-B]` / `[v0.7-C]` below.

- Tests: 306 → **382** (+76, +25%): v0.7-A +39 / v0.7-B +37 / v0.7-C ±0
- Runtime deps: 5 → 5 (zero SDK dependency maintained; the probe is pure httpx + pyyaml + pydantic)
- Design through-lines:
  - **Data-as-configuration** (v0.7-A) — a 2-layer (bundled + user) YAML registry replaces an in-Python regex literal
  - **A diagnostic surface that bypasses runtime transformations** (v0.7-B) — the probe goes directly through httpx without the adapter, closing the observation gap left by transformations
  - **Dominant-signal short-circuit with SKIP preserved** (v0.7-B) — remaining probes get SKIPped on auth failure, but transparency is kept / no tokens consumed
  - **A non-code release as a sub-release boundary** (v0.7-C) — docs + examples versioned as an independent sub-release

### v0.7 umbrella-level follow-ons

See the relevant section for each v0.7 sub-release's follow-ons. What cuts across at the umbrella level:

- **A `coderouter doctor` `num_ctx` probe** (direct detection of symptom #1, v0.8 scope)
- **`coderouter doctor --json` output** (for a CI auto-PR bot, v0.7-D or v0.8)
- **A CI smoke workflow**: weekly `doctor --check-model <each-free-provider>`, symmetric with the v0.5-D cron
- **Adding a probe alongside v1.0's output-cleaning** — applying the "transformation comes paired with a probe" principle
- **A real-machine re-verify for v0.7** (something like `scripts/verify_v0_7.sh`)
- **A test-count auto-updater** (named across 3 consecutive retrospectives, still unimplemented)
- **Automating doc-edit touchpoints** (the `scripts/release-close.py` idea, automating ~9 manual edits)

---

## [v0.7-C] — 2026-04-20 (Ollama beginner Troubleshooting + HF-on-Ollama reference profile)

**Theme: bring the declarative layer + probe built in v0.7-A / v0.7-B down to "the operator's point of view."** v0.7-A moved the registry to YAML, and v0.7-B introduced the live probe, but without a path in the README for **which command / which YAML patch to reach for, for a given symptom**, a beginner still falls back into trial-and-error. v0.7-C is a non-code deliverable only: it chapters the 5 symptoms from plan.md §9.4 in the README Troubleshooting section, attaching a 3-point set to each — an example `coderouter doctor --check-model` run, a concrete YAML patch, and the fix. It also adds a reference stanza for an HF-distilled Ollama provider to `examples/providers.yaml` (a commented-out template demonstrating all 5 knobs in a single block). Cross-links to lunacode's [`MODEL_SETTINGS.md`](https://github.com/zephel01/lunacode/blob/main/docs/MODEL_SETTINGS.md) make the correspondence with the editor-harness layer explicit. This completes the v0.7 umbrella at the deliverable level, moving on to the `v0.7.0` tag + writing the retrospective.

- Tests: 382 → **382** (zero code changes, docs + example config only)
- plan.md §9.4 DoD: of the remaining 2 items, "document all 5 symptoms in README Troubleshooting" is now done. The last item is the `v0.7.0` umbrella tag + writing the retrospective

### Added

- **README — `### Ollama beginner — 5 silent-fail symptoms (v0.7-C)`** (a new subsection at the end of the Troubleshooting section)
  - Symptom 1: num_ctx truncation (`extra_body.options.num_ctx: 32768`) — detected **indirectly** by doctor (observed as the tool_calls probe reporting "no tool_call emitted"). The num_ctx probe itself is already noted as a follow-on in v0.7-B's CHANGELOG entry
  - Symptom 2: `tools=false` left undeclared (`capabilities.tools: false`) — detected via doctor's `tool_calls: NEEDS_TUNING`, with the patch being the exact copy-paste YAML at the end of the doctor output
  - Symptom 3: `<think>` tag leak (`append_system_prompt: "/no_think"` + future v1.0 output-cleaning) — detected via doctor's `reasoning-leak: informational`
  - Symptom 4: model tag typo / forgetting `ollama pull` (404) — detected via doctor's `auth+basic-chat: UNSUPPORTED`, with an `ollama pull <tag>` hint. Forgetting the `:Q4_K_M` suffix on HF-on-Ollama falls into the same category
  - Symptom 5: API key not set (401) — detected via doctor's `auth+basic-chat: AUTH_FAIL`, diagnosed with the env var name. This recovers the UX value of the auth short-circuit that SKIPs the remaining 3 probes
  - Ends with a loop example, `for p in <providers>; do coderouter doctor --check-model "$p"; done`, plus an anchor link back to the exit code table (the Doctor subsection)
  - A cross-link to lunacode's [`MODEL_SETTINGS.md`](https://github.com/zephel01/lunacode/blob/main/docs/MODEL_SETTINGS.md) — a one-line explanation of how CodeRouter's provider-granularity and lunacode's per-model-granularity divide responsibilities
- **README — `#### HF-on-Ollama reference profile`** (a subsection right below the 5-symptoms section above)
  - A pointer to the `ollama-hf-example` stanza in `examples/providers.yaml`
  - A one-paragraph explanation of why HF GGUF amplifies all 5 symptoms (missing chat template / `<think>` leakage from distillation / a mandatory quant suffix)
- **`examples/providers.yaml` — the `ollama-hf-example` stanza** (a commented-out reference, placed right after Ollama Tier 1)
  - Uses `base_url: http://localhost:11434/v1` + `model: hf.co/unsloth/Qwen2.5-Coder-7B-Instruct-GGUF:Q4_K_M` as the default example
  - Lists 3 candidate models in a comment (Qwen2.5-Coder / Qwen3-8B / DeepSeek-R1-Distill-Qwen)
  - `extra_body.options.num_ctx: 32768` — addressing symptom 1, with a comment noting the token scale of Claude Code's system prompt
  - `append_system_prompt: "/no_think"` (a commented sub-line) — addressing symptom 3, noting it's only effective for Qwen3 / R1-distill
  - `capabilities.tools: false` by default — addressing symptom 2, documenting the practice of flipping it once `coderouter doctor` reports OK
  - `reasoning_passthrough: false` (commented) — addressing symptom 3's leakage, noting the relationship with v1.0's output-cleaning
  - A warning about the required `:<quant>` suffix (the HF-specific version of symptom 4) in the stanza's header comment
- **README — a cross-link from the Troubleshooting section's `#### Doctor` subsection**
  - A cross-link from the end of the 5-symptoms section back to the Doctor subsection's exit code table

### Changed

- **README's "Coming next" list** — removed the v0.7-C item, moving v1.0 to the front (the next milestone is the 14-case regression suite + Code Mode)
- **README's Troubleshooting intro line** — kept the existing guidance to "first run `coderouter doctor --check-model <provider>`" as-is; the new 5-symptoms subsection organizes "what to read first"

### Design notes

- **v0.7-C as a non-code-only release.** Against v0.7-A (moving to YAML) and v0.7-B (the probe), which were "2 implementation-leaning releases," v0.7-C is deliberately docs + example config only. Since a probe has no value unless the operator can recognize it as a 3-point set of "symptom → command → patch," it's split out as an independent release despite being non-code. This recovers the intent already declared in plan.md §9.4's scope table for this split
- **The 5 symptoms are ordered not by "ease of detection" but by "how easily a beginner hits them."** Symptom 1 (num_ctx) can only be detected **indirectly** by CodeRouter's doctor, but it's placed first since it's the first landmine hit when connecting Ollama with Claude Code for the first time. Symptom 5 (API key not set) is the most deterministically detectable, but it's placed last since it's a symptom hit once setup has progressed somewhat
- **Each symptom is paired with a 1-line mock example of "detection command output."** Pasting just 1 line of an actual `coderouter doctor` output (in the form `# → tool_calls: NEEDS_TUNING — ...`) lets the operator picture what they'll see before running it. Since the full output is already shown in the Doctor subsection, only the relevant probe's verdict line is shown here
- **Why the HF-on-Ollama stanza is placed commented-out.** A design where it only becomes active once uncommented has 2 effects: (a) it doesn't contaminate a fresh install's default chain with the HF provider, and (b) making the operator take the 2 steps of "pull it yourself and uncomment" makes them consciously aware of the `:<quant>` suffix ahead of time, guarding against a typo. Including an active HF provider in the example would make `coderouter serve` keep returning 404s against a model name that hasn't been `ollama pull`ed yet at fresh-install time — a failure-by-example
- **Making the relationship with lunacode's MODEL_SETTINGS.md explicit.** As sibling projects from the same author, there's substantial overlap in the knowledge behind both projects. However, CodeRouter declares at **provider-granularity** ("the capability of the model used via this provider"), while lunacode declares at **per-model-granularity** ("the settings of this model itself"), so the same symptom lands in a different declaration location. The README cross-link serves as a decision aid for "which config file to touch" when running both projects side by side

### Follow-ons

- **Adding a `coderouter doctor` num_ctx probe** — introducing a 5th probe to directly detect symptom 1. Probabilistically sampling whether silent truncation occurs at the 8K / 16K / 32K boundary (a long prompt with a marker phrase at the tail → checking whether the marker appears in the response). v0.8 scope
- **`coderouter doctor --json` output** — machine-readable output for CI. A shape parseable by an auto-patch bot as exit code + a JSON array of symptoms. Already mentioned in the v0.7-B CHANGELOG entry, to be picked up in v0.8
- **The `v0.7.0` umbrella tag + `docs/retrospectives/v0.7.md`** — to be handled in a commit right after this release. The last remaining item in plan.md §9.4's DoD
- **A bundled `model-capabilities.yaml` counterpart for the HF-on-Ollama reference stanza** — currently designed as a per-provider `capabilities.tools: false` opt-out, but whether to add an HF-GGUF-specific glob (e.g., `hf.co/unsloth/*`) to the bundled YAML needs deciding. Since it would conflict with the provider-granularity principle, deferred past v0.7

---

## [v0.7-B] — 2026-04-20 (`coderouter doctor --check-model` — per-provider live probe)

**Theme: make "I set up Ollama but it doesn't work" diagnosable in a single command.** Now that v0.7-A has moved the registry's declarations out to YAML, and providers.yaml's `capabilities.*` explicit opt-in is in place, what's still missing is **a mechanism to detect, ahead of time, the gap between declaration and real-hardware behavior.** v0.7-B implements `coderouter doctor --check-model <provider>`, running 4 probes in sequence against a single provider (auth / tool_calls / thinking / reasoning-leak), cross-checking the registry + providers.yaml declarations against measured reality, and emitting a copy-paste-ready YAML patch whenever there's a mismatch. This is the first step of **pre-emptive diagnosis** for the 5 symptoms in plan.md §9.4 (especially #2 tools / #3 thinking / #4 auth / #5 model-not-found).

- Tests: 345 → **382** (+37: `tests/test_doctor.py` +31, `tests/test_cli.py` +6)
- Exit-code contract: `0` = match / `2` = needs_tuning / `1` = auth_fail | model-not-found | transport-error (with a grep-friendly "Exit: N" terminal line for CI smoke)
- Non-destructive: the probe is read-only, the tool-spec uses a fake `echo` with no side effect, and on auth failure the remaining probes are SKIPped to stop consuming tokens

### Added

- **`coderouter/doctor.py`** (a new module, ~600 lines: the probe body + reporting)
  - The `ProbeVerdict` enum: `OK / SKIP / NEEDS_TUNING / UNSUPPORTED / AUTH_FAIL / TRANSPORT_ERROR`
  - The `ProbeResult` / `DoctorReport` dataclasses — per-probe verdict + `suggested_patch` + `target_file` (`providers.yaml` / `model-capabilities.yaml`)
  - `exit_code_for(report)` — returns 0/1/2 by the precedence blocker (auth/unsupported/transport) > needs_tuning > ok
  - **Probe 1 `auth+basic-chat`** — sends a minimal prompt via `POST /chat/completions` (openai_compat) or `POST /v1/messages` (anthropic). 401/403 → AUTH_FAIL, 404 → UNSUPPORTED (including an `ollama pull` hint), timeout/5xx → TRANSPORT_ERROR, 2xx + parseable → OK. **Remaining 3 probes SKIP on auth failure**
  - **Probe 2 `tool_calls`** — sends "Call echo with message=probe" along with a fake `echo` tool spec. Determines OK / NEEDS_TUNING from the combination of 3 branches (native `tool_calls` / text-JSON that v0.3-A repair can rescue / nothing at all) crossed with the declaration state (providers explicit / registry tools / neither declared). The patch can flip `providers.yaml capabilities.tools` to either `true` or `false`
  - **Probe 3 `thinking`** — `kind: anthropic` only. Sends `thinking: {type: enabled, budget_tokens: 1024}`, observing whether a `{type: thinking}` block appears in the response content. Also treats a 400 rejection (upstream doesn't know the field) as a success signal. SKIPs for openai_compat (since block information is lost in the openai-shape translation), though a misconfigured `capabilities.thinking=True` gets a SKIP plus a warning note
  - **Probe 4 `reasoning-leak`** — `kind: openai_compat` only. Observes whether the response's non-standard `message.reasoning` field is present. If present with `reasoning_passthrough=false` (default) → an informational OK (communicating to the operator why a `capability-degraded` log appears, on the assumption v0.5-C's strip is doing its job). SKIPs for anthropic
  - `check_model(config, provider_name, *, registry=None)` async entry / `run_check_model_sync` sync wrapper (called from the CLI)
  - `format_report(report)` — line-oriented output with `[OK]` / `[NEEDS TUNING]` badges, ending with an `Exit: N` line (for CI grep)
  - `_patch_providers_yaml_capability()` / `_patch_model_capabilities_yaml()` — copy-paste YAML generation helpers, with a header comment specifying which file to paste into
- **`coderouter/cli.py`** — added the `doctor` subcommand (argparse)
  - `--check-model <provider>` (required) / `--config <path>` (shared)
  - `_run_doctor(args)` — loads config, runs the probe, returns the exit code. FileNotFoundError / YAML parse error / an unknown provider name all exit 1 with stderr
- **`tests/test_doctor.py`** (new, +31)
  - Patch emitters: 3 tests (providers.yaml / model-capabilities.yaml each stored, the emitted YAML parses as valid YAML)
  - Auth probe: 5 tests (401 → AUTH_FAIL + remainder SKIPped / 403 likewise / 404 → UNSUPPORTED + a model-name hint / an actual transport error / 2xx + garbage body)
  - Tool-calls probe: 7 tests (native + declared / native + silent → patch true / text-JSON + declared false → OK / text-JSON + declared true → NEEDS_TUNING / nothing + declared → NEEDS_TUNING false / nothing + undeclared / providers.yaml explicit opt-in takes priority)
  - Thinking probe: 5 tests (openai_compat skip / openai_compat opt-in misconfig warn / anthropic match / anthropic no block but declared / anthropic 400 rejection + declared)
  - Reasoning-leak probe: 3 tests (detected → informational OK / absent → OK / anthropic skip)
  - Exit-code: 3 tests (all OK = 0 / NEEDS_TUNING alone = 2 / AUTH_FAIL dominates NEEDS_TUNING = 1)
  - Orchestration: 5 tests (unknown provider → KeyError listing known names / via the registry kwarg default / OpenAI Bearer auth / Anthropic x-api-key auth / format_report ends with "Exit: N")
- **`tests/test_cli.py`** (+6)
  - `doctor` required-arg / `--check-model` propagates to load_config / NEEDS_TUNING propagates to exit 2 / unknown provider → exit 1 + known names in stderr / FileNotFoundError → exit 1 / `--config` reaches load_config

### Design notes

- **Why bypass the adapter layer with direct httpx.** The reasoning-leak probe wants to see the raw body before v0.5-C's passive strip runs, and the thinking probe wants to send the Anthropic wire shape directly for `kind: anthropic`. The tool_calls probe also wants to distinguish raw `tool_calls` from raw text before the repair pass. Going through the adapter would move the observation point inside the adapter, making test mocks adapter-dependent (= brittle). The probe stays confined to "raw POST + raw body interpretation"
- **The rationale for the auth short-circuit.** Running the remaining 3 probes on auth failure doesn't consume tokens, but it adds noise for the operator. The moment a 401 is seen, "fix the env var first" can be stated definitively — the tool_calls / thinking verdicts would be meaningless anyway (the request never gets through in the first place). SKIP lines are kept to preserve transparency about "what wasn't checked"
- **Exit code precedence.** blocker (1) > tuning (2) > ok (0). In a CI context, this splits into "1 needs human intervention (a blocker), 2 is a mechanical fix an automated PR could apply, 0 is green." 2 being a larger number than 1 follows the conventional Unix idiom (in lint tools, `--fix`-able issues get 2, unrecoverable ones get 1)
- **Probe readability vs. mock complexity.** Structuring each probe as a simple single `POST` call means tests can be written by lining up `httpx_mock.add_response` calls in probe order. A batch-endpoint alternative (1 call covering many probes) was considered, but since openai_compat and anthropic have different endpoint shapes, batching offers little benefit — the current structure is the most intuitive
- **Choosing the patch's target_file.** For a single-provider issue, changing `providers.yaml` is the minimal change (touching a glob rule would ripple to other providers in the same family). Conversely, for the case where "an entire model family diverges from the registry," it's up to the operator to write the patch into `model-capabilities.yaml`. Since doctor only looks at 1 provider as a principle, `suggested_patch` always falls back to a `providers.yaml` target. The one exception is the thinking probe's "block emitted but declaration silent" case (since declaring in the registry is the natural expression, it suggests `model-capabilities.yaml`)
- **The safety of the fake `echo` tool.** Named `echo`, with its description explicitly stating "diagnostic-only," parameters limited to `message: string` only, with zero mention of any side effect. Even if a tool_call somehow reaches the caller via repair, `echo` normally won't match any real whitelisted tool, so it gets silently dropped. This secures the probe's non-destructiveness
- **Deferring the `--network` flag.** The `--network` flag mentioned in plan.md §9.4 assumed separation from a static lint mode, but v0.7-B is dedicated to `--check-model`, premised on a live probe; `--network` is semantically self-evident (probe = network call). To be reconsidered when a static-only lint mode is introduced in v0.7-C or v0.8

### Follow-ons

- **Organizing the 5 symptoms into README Troubleshooting in v0.7-C** — attaching a `coderouter doctor --check-model <provider>` pointer to each symptom. Also adding a `providers.yaml` stanza + a bundled `model-capabilities.yaml` entry for the HF-on-Ollama reference profile
- **A num_ctx boundary probe**: under consideration as a 5th probe detecting whether silent truncation occurs with a large system prompt. Currently `max_context_tokens` can be declared in the registry but isn't yet leveraged by a probe
- **A CI smoke script**: a GitHub Actions workflow running `coderouter doctor --check-model <each-free-provider>` weekly. Exit 2 → auto-apply the providers.yaml patch via an auto-PR, exit 1 → an issue. Symmetric with the v0.5-D OpenRouter roster cron
- **Finer-grained `reasoning` field strip**: currently v0.5-C's strip is all-or-nothing (the `capabilities.reasoning_passthrough` flag). A finer-grained design per model — like "strip only the reasoning tag, leave other fields as-is" — to be reconsidered once it converges with the v1.0+ `reasoning_control` abstraction
- **A doctor --json output mode**: machine-readable output for CI / scripts. Currently human-facing text only. To be considered for addition in v0.7-C or v0.8

---

## [v0.7-A] — 2026-04-20 (Declarative `model-capabilities.yaml` registry)

**Theme: move "which family accepts thinking" out to YAML.** The capability-gate heuristic introduced in v0.5-A had a Python literal regex (`^claude-sonnet-4-6` etc.) baked into `coderouter/routing/capability.py`. Every time Anthropic shipped a new family, this required a code change plus a release cycle, and it was a hidden layer invisible to beginners and intermediate users alike. v0.7-A moves the declaration out to `model-capabilities.yaml` (a bundled default plus a user override), so adding a new family becomes a 1-line YAML edit, while also designing it as a hub for future declarations of `tools` / `reasoning_passthrough` / `max_context_tokens`. This is the first sub-release toward the v0.7 scope in plan.md §9.4.

- Tests: 306 → **345** (+39, a new `tests/test_capability_registry.py`: schema validation / glob matching / first-match-per-flag / user override layering / bundled YAML consistency / gate function integration)
- Zero behavior change: `provider_supports_thinking`'s public API and decision results are identical to v0.5-A (the bundled YAML encodes the old regex 1:1)
- The `providers.yaml` `capabilities.*` explicit opt-in remains top priority (`provider.capabilities.thinking=True` skips the registry lookup)

### Added

- **`coderouter/data/model-capabilities.yaml`** (the bundled default, shipped with the package)
  - Schema v1: `rules: [{match: glob, kind: "anthropic"|"openai_compat"|"any", capabilities: {thinking, reasoning_passthrough, tools, max_context_tokens}}]`
  - Current entries: 4 globs — `claude-opus-4-*` / `claude-sonnet-4-6*` / `claude-sonnet-4-7*` (forward-compat) / `claude-haiku-4-*` — all `kind: anthropic` + `thinking: true`
  - Comments state that "adding a new family only requires editing this one file" and that "the user override lives at `~/.coderouter-t/model-capabilities.yaml`"
- **`coderouter/data/__init__.py`** — turns this into a real package so package data can be reliably accessed via `importlib.resources.files()`
- **`coderouter/config/capability_registry.py`** (a new module)
  - The `RegistryCapabilities` / `CapabilityRule` / `CapabilityRegistryFile` Pydantic models (all `extra="forbid"`, so a typo immediately raises ValidationError)
  - The `ResolvedCapabilities` frozen dataclass — 4 flags plus `None` (= not declared)
  - `CapabilityRegistry.lookup(*, kind, model)` — **first-match-per-flag** semantics: walks rules top-down, and for each flag, "the first rule that declared it" wins (a flag left undeclared passes through to a later rule)
  - 3 loaders on `CapabilityRegistry`: `load_default()` / `load_from_paths()` / `from_rule_lists()` (production / test-isolated / fully-in-memory)
  - A missing user file returns `[]`, operating bundled-only (the normal case); a schema error fails fast
- **`coderouter/routing/capability.py`**
  - Removed `_THINKING_CAPABLE_PATTERNS` / `_THINKING_CAPABLE_RE` / the `re` import (retiring the baked-in regex)
  - `get_default_registry()` — a lazy module-level singleton, loaded from disk only once per process
  - `reset_default_registry()` — a test hook, letting tests that stage a user YAML invalidate the cache
  - Added a `registry` kwarg to `provider_supports_thinking(provider, *, registry=None)` — a DI point. Production goes through the default; tests can inject a custom registry
  - Added `CapabilityRegistry` / `ResolvedCapabilities` / `get_default_registry` / `reset_default_registry` to `__all__` (so the adapter/engine layers can import them from routing)
- **`tests/test_capability_registry.py`** (new, +39)
  - Schema: 7 tests (empty YAML OK / top-level typo rejected / rule typo rejected / flag typo rejected / version mismatch rejected / empty match rejected / kind defaults to "any")
  - Glob matching: 10 parameterized tests (`claude-opus-4-*` / `claude-sonnet-4-6*` boundaries / `qwen3-coder:*` / case sensitivity)
  - Lookup semantics: 8 tests (no rules → all None / kind filter / first-match-per-flag / flag independence / user overrides bundled ordering / an unmatched flag = None / `kind: "any"` universal match)
  - Bundled YAML consistency: 3 tests (the 7 models that were thinking-capable under v0.5-A's regex all return thinking=True / a pre-4-6 sonnet → None / openai_compat → None)
  - User override integration: 3 tests (load_from_paths reads both / missing user file is OK / a malformed user file → ValidationError)
  - Gate integration: 8 tests (injecting via the `registry=` kwarg / providers.yaml explicit takes priority over the registry / undeclared in the registry → False / reload via `reset_default_registry` / default == a fresh load / confirming the re-export)

### Design notes

- **Why move it out to YAML.** v0.5-A's retro had already flagged "passive drift against Anthropic's release cadence" as a follow-on (docs/retrospectives/v0.5.md §What was sharp). If a code change is required, a delayed release cycle means drift becomes invisible. With YAML, a user can update it themselves without waiting for the bundled default (`~/.coderouter-t/model-capabilities.yaml`), and updating the bundled default is just a 1-line PR
- **The rationale for first-match-per-flag.** A simple first-match approach leaves it ambiguous whether rule B overwrites or is ignored by rule A, in a case like "rule A declares only thinking, rule B declares only tools for the same glob." Per-flag lets "A sets thinking=true, B sets tools=true, both apply" be expressed naturally. A YAML author can design an independent override order per flag
- **Not adopting layered lookup (per plan.md §9.4 policy).** lunacode has 4 layers (`<cwd>/.kairos → <repo>/.kairos → ~/.kairos → bundled`), but since CodeRouter's providers.yaml is static deployment-time config, a per-cwd layer carries little meaning. Narrowed to 2 layers: bundled + user. If `providers.d/*.yaml` merging is requested in the future, split it out for consideration in v0.7-D or v0.8 (currently YAGNI)
- **Keeping per-provider granularity (not per-model).** The same `qwen3-coder:7b` can have different tool-calling stability between Ollama and LMStudio, so the registry lookup's granularity stays at `(kind, model)`. lunacode is an editor harness so per-model was fine there, but CodeRouter assumes the provider abstraction
- **Distinguishing `kind: "any"` from `"anthropic"`.** The old heuristic had a hard check, `if provider.kind != "anthropic": return False`. v0.7-A re-expresses this as data — "all rules in the bundled YAML are `kind: anthropic`, so an openai_compat query never matches." When a default is added for the openai_compat family in the future (e.g., `qwen3-coder:*` tools=true), a `kind: openai_compat` rule can coexist
- **`provider.capabilities.thinking=True`'s precedence remains unchanged as top priority.** The registry is merely "the default when nothing is explicitly declared" — the unchanged contract is that **whatever the user explicitly overrides stays overridden.** This is the same providers.yaml escape hatch promised as of v0.5-A
- **The test-only `reset_default_registry`.** Since it's a module-level singleton, a hook was added so tests can pick up a staged user YAML. Production code never needs to call it

### Follow-ons

- **A registry ↔ live-probe diff mechanism in v0.7-B**: `coderouter doctor --check-model <provider>` compares registry declarations against real-hardware behavior, emitting a divergence as `⚠️ NEEDS TUNING`. Outputs a copy-paste-ready YAML patch (in a form that can be pasted into either `providers.yaml` or `model-capabilities.yaml`)
- **CI for a registry snapshot**: running `coderouter doctor --check-model` weekly against every entry in providers.yaml, dropping divergences as a PR-ready artifact (symmetric with v0.5-D's OpenRouter roster cron)
- **An HF-on-Ollama reference profile in v0.7-C**: adding a reference `model-capabilities.yaml` entry + `providers.yaml` stanza to examples, for using an HF-distilled model (qwen3.5 / qwen3.6 etc.) via Ollama
- **Adding bundled defaults for tools / max_context_tokens / reasoning_passthrough**: currently the bundled default only covers thinking. Envisions accumulating v0.7-B doctor probe results and progressively promoting them into the bundled default (policy: "only write real-hardware-verified facts into bundled")
- **Merging with the Capabilities class** (v1.0+): `ProviderConfig.capabilities` was flagged in the v0.5 retro as trending toward a "kitchen sink" (nearing 10 flags). To be reorganized on the registry side too, once merged with v1.0's `reasoning_control` / `mcp` Literal abstraction

---

## [v0.6.0] — 2026-04-20 (umbrella tag for v0.6-A / v0.6-B / v0.6-C / v0.6-D)

**Theme: Chain as a first-class object.** Bundles 4 sub-releases into a single tag: v0.6-A (launch-time profile selection + startup validation), v0.6-B (profile-level parameter overrides `timeout_s` / `append_system_prompt` + `ProviderCallOverrides`), v0.6-C (a declarative `ALLOW_PAID` gate + an aggregate `chain-paid-gate-blocked` warn), and v0.6-D (`mode_aliases` + the `X-CodeRouter-Mode` header — separating the intent / implementation namespaces). **Startup fast-fail validators** (4 instances) and **typed log payload + chokepoint helpers** (v0.6-C being the 2nd instance of the v0.5.1 A-1 pattern) are established as the design spine running through the whole minor release. That `_resolve_chain` is the chokepoint tying together the 4 engine entry points was reconfirmed by where v0.6-C's warn was placed (a dividend of v0.4-A's move to a polymorphic chain). Of the v0.5-scope items left unstarted in §9.3, all are now cleared except capability-mismatch→chain-skip (bundled with v1.0+ / the vision).

- Commits: v0.6-A → v0.6-B → v0.6-C → v0.6-D (plus a docs commit for each sub-release)
- Tests: 267 → **306** (+39, +15%)
- Narrative & design through-lines: [`docs/retrospectives/v0.6.md`](./docs/retrospectives/v0.6.md)
- Per-sub-release detail: sections `[v0.6-A]` / `[v0.6-B]` / `[v0.6-C]` / `[v0.6-D]` below.
- The 5-dep bound is maintained (still SDK-independent; the bet confirmed in v0.5 that "the translation layer is thinner than an SDK" continues into the routing / ingress layers)

---

## [v0.6-D] — 2026-04-20 (`mode_aliases` — mapping `X-CodeRouter-Mode: coding` → a profile name)

**Theme: "separate intent from implementation via namespace."** Since v0.1, `profile` (body/header) could already select a chain, but the client side always had to point directly at an **implementation-leaning name** like `default` / `fast` / `long-context`. v0.6-D introduces the `mode_aliases` YAML block and the `X-CodeRouter-Mode` header, letting a client just send its **intent** (`coding` / `long` / `fast` ...). Profile names are demoted to the router's internal implementation detail, so swapping the underlying chain has no effect on the client. Clears the remaining #5 in §9.3.

- Tests: 291 → **306** (+15: schema 3 / OpenAI ingress 6 / Anthropic ingress 6)
- Precedence: body `profile` > the `X-CodeRouter-Profile` header > the `X-CodeRouter-Mode` header > `default_profile` — Mode ranks below Profile (an explicit implementation always wins)
- Startup fast-fail: if `mode_aliases` points to an unknown profile, a `ValidationError` fails before `serve` even starts (the same philosophy as v0.6-A's `default_profile` validation)

### Added

- **`coderouter/config/schemas.py`**
  - `CodeRouterConfig.mode_aliases: dict[str, str]` (`default_factory=dict`) — keys are mode names, values are profile names
  - The `_check_mode_alias_targets_exist` model validator — verifies at startup that every alias target exists among the declared profiles
  - `CodeRouterConfig.resolve_mode(mode) -> str` — resolves an alias (raising `KeyError` if not found, converted to a 400 on the ingress side)
- **`coderouter/ingress/openai_routes.py`**
  - A new header param, `x_coderouter_mode: str | None` (aliasing `X-CodeRouter-Mode`)
  - When the profile is undetermined and a mode header is present, calls `config.resolve_mode()` → logs `mode-alias-resolved` at INFO → assigns the result to `chat_req.profile`. An unknown mode returns 400 listing the known modes
  - Updated the module docstring to the 4-level precedence (body > profile-header > mode-header > default)
- **`coderouter/ingress/anthropic_routes.py`** — applies the same pattern to the Anthropic route, naturally slotting into the existing handling of `anthropic-version` / `anthropic-beta`
- **`tests/test_config.py`** (+3) — `resolve_mode` happy path + KeyError / an unknown target raising `ValidationError` at load / an undeclared `mode_aliases` defaulting to `{}`
- **`tests/test_ingress_profile.py`** (+6) — mode header → aliased profile / a Profile header outranks a Mode header / a body profile outranks a Mode header / an unknown mode → 400 + the known list / an empty `mode_aliases` makes the mode header 400 / the resolved result reaches the engine
- **`tests/test_ingress_anthropic.py`** (+6) — the same patterns for the Anthropic route (including the streaming path)

### Design notes

- **Why Mode ranks below Profile.** If the caller has sent a **concrete profile name**, that caller already knows the router's internal name and intentionally specified it. Letting Mode override that would risk an accident where "a mode header injected through a proxy causes the profile to be ignored." The natural precedence: intent (Mode) loses whenever implementation (Profile) has already been specified
- **Header only — no body field added.** Profile exists as a body field too, but Mode is kept header-only. The rationale is the division of labor "body is the API's contract, header is ops-layer orchestration." Mode is the typical case an operator wants to inject via a proxy (e.g., an API gateway attaching intent), so a header is the natural fit. Putting it in the body would require adding a field to both OpenAI/Anthropic `*Request` shapes, bloating the scope
- **An invalid mode → 400 (no silent fallback).** A design where an empty `mode_aliases` or an unknown mode falls through to the default profile was possible, but the typical failure mode is "a client/proxy typo." A silent fallback creates the situation of "it works, but on a different profile than intended," so it's a 400 instead. The error body lists the known modes, making it self-correctable
- **Startup validation (following v0.6-A).** Failing at startup with a `ValidationError`, rather than at request time with a 400, follows the same fast-fail philosophy as the `default_profile` validation. If a broken alias reached request time, it would show up as an intermittent symptom of "a mode that should work doesn't"
- **The intent behind the `mode-alias-resolved` INFO log.** Since the mode → profile resolution is invisible to the client, a single log line records "what resolved to what." This lets an operator grep-diagnose things like "a request called with coding mode ended up on the fast profile"

### Follow-ons

- **v0.7+**: whether to consider a hierarchy for `mode_aliases` (e.g., dotted names like `coding.fast` / `coding.thorough`). For now, a flat dict is sufficient (usage is expected to converge to 3-5 kinds), so over-engineering is avoided
- **examples/providers.yaml**: no `mode_aliases` block sample was added this time (adding one without breaking the real YAML needs care). To be decided whether to add it at low risk during the v0.6-D docs pass, or to organize it together during an example overhaul around v1.0

---

## [v0.6-C] — 2026-04-20 (A declarative `ALLOW_PAID` gate + an aggregate `chain-paid-gate-blocked` warn)

**Theme: promote "a declared gate" to a single line at chain-granularity.** Since v0.1, `paid: true` providers were already filtered out when `ALLOW_PAID=false`, but this only produced a per-provider INFO (`skip-paid-provider`); the case where "the entire chain went empty due to the paid gate" was buried inside `NoProvidersAvailableError`. v0.6-C adds an **aggregate warn** (`chain-paid-gate-blocked`), firing a single line with a hint the moment the gate empties out a chain. Follows the same "typed payload + chokepoint helper + resides in logging.py" pattern as v0.5's `capability-degraded` capability gate.

- Tests: 283 → **291** (+8, new `tests/test_fallback_paid_gate.py`)
- Zero behavior change: the existing `NoProvidersAvailableError` exception shape is non-breaking, and the `skip-paid-provider` INFO is preserved at the per-provider level
- Fires across all 4 entry points (generate / stream / generate_anthropic / stream_anthropic) — since they're consolidated into `_resolve_chain`, a single change to the shared path was enough

### Added

- **`coderouter/logging.py`**
  - The `ChainPaidGateBlockedPayload` TypedDict — 3 fields: `profile` / `blocked_providers: list[str]` / `hint: str`
  - The `log_chain_paid_gate_blocked(logger, *, profile, blocked_providers, hint=...)` chokepoint helper (warn level)
  - `_DEFAULT_PAID_GATE_HINT` — the default text, `"set ALLOW_PAID=true, mark a provider paid=false, or add a free provider to this profile's chain"` (grep-friendly, overridable per call site if needed)
- **`coderouter/routing/fallback.py`**
  - `_resolve_chain` collects paid-blocked provider names into a `blocked_by_paid` list. Once the chain is resolved, if `adapters == [] and blocked_by_paid`, it fires `log_chain_paid_gate_blocked`
  - `from coderouter.logging import get_logger, log_chain_paid_gate_blocked`
- **`tests/test_fallback_paid_gate.py`** (new, +8) — warn fires on an all-paid chain / `blocked_providers` follows chain order with multiple paid entries / no warn on a mixed chain (since a free one survives) / neither skip-paid nor warn with ALLOW_PAID=true / no warn on an unknown-only chain / confirms the warn fires on streaming / generate_anthropic / stream_anthropic paths

### Design notes

- **Why the aggregate warn was needed.** Since `skip-paid-provider` is per-provider INFO, a chain=[paid-A, paid-B, paid-C] emits 3 lines. For an operator to judge "is this what emptied the whole chain?" required reconstructing the timeline from `skip-paid-provider` → `NoProvidersAvailableError` via grep. v0.6-C surfaces this "declaratively, in one line"
- **Choosing warn over info.** v0.5's `capability-degraded` is info (the gate is always operating on a normal path). v0.6-C is warn (an empty chain strongly suggests a misconfiguration, worth grabbing the operator's attention). The `skip-paid-provider` side stays info (there are moments where the chain still has a chance to survive)
- **Continuing to reside in `logging.py`.** Follows the same policy (from v0.5.1 A-1) of placing the helper in `logging.py` rather than `routing/capability.py`. Same rationale: since `routing/__init__.py` eagerly imports `FallbackEngine`, this avoids a future cycle for when the adapter side might want to fire a paid-gate warn too
- **Why no warn on a mixed chain.** Even with paid-blocked providers present, the chain is still exercised as long as at least one free provider survives. In that case, the "all free providers failed" diagnosis is already narrated by the `provider-failed` lane, so a warn would just be redundant. The same "aggregate only barks when empty/uniform" rule as v0.5.1 A-3 (`chain-uniform-auth-failure`)
- **`retry_max` / startup enumeration are out of scope.** §9.3 #3 implied something like "enumerate paid providers at startup," but since v0.6-A's `coderouter-startup` log already shows full provider info, adding this separately would cost more than it's worth. Prioritizing the chain-time warn for now

### Follow-ons

- **v0.6-D**: a `mode_aliases` YAML block mapping `X-CodeRouter-Mode: coding` → a profile name (remaining item #5 in §9.3)
- **v0.6+**: it would be convenient to override `chain-paid-gate-blocked`'s hint text per profile (e.g., adding context like "set ANTHROPIC_API_KEY and ALLOW_PAID=true" for the `claude-code-direct` profile). Currently overridable per call site via the helper's `hint=`

---

## [v0.6-B] — 2026-04-20 (profile-level `timeout_s` / `append_system_prompt` override)

**Theme: elevate a profile to "an ordered list of providers plus control parameters."** v0.6-A made profile selection itself swappable via CLI/env, but per-profile **control-parameter differences — like "this one assumes local low latency so keep the timeout short" or "that /no_think addition only applies to the fast-profile" — only existed at the provider level.** v0.6-B adds optional `timeout_s` / `append_system_prompt` to `FallbackChain`, with the engine building a single `ProviderCallOverrides` once at profile-resolution time and distributing it across the whole chain.

- Tests: 275 → **283** (+8: 5 fallback engine / 3 openai_compat adapter)
- Precedence: the profile value (if set) → the provider value → the built-in default — **replace** (not append) semantics. Aligned with `timeout_s`'s existing behavior to avoid confusion
- `retry_max` is out of scope since the adapter layer has no existing retry mechanism (a partial handling of §9.3 #4)

### Added

- **`coderouter/config/schemas.py`**
  - `FallbackChain.timeout_s: float | None` (`ge=1.0, le=600.0`) — the same range constraint as `ProviderConfig.timeout_s`
  - `FallbackChain.append_system_prompt: str | None` — a special semantics where explicitly setting `""` on the profile side lets you "disable the provider-side directive, just for this profile"
- **`coderouter/adapters/base.py`**
  - The `ProviderCallOverrides` pydantic model (`extra="forbid"`, all fields optional). Built once per profile by the engine, distributed to every adapter call within the same chain
  - `BaseAdapter.effective_timeout(overrides)` / `effective_append_system_prompt(overrides)` — shared helpers deciding override > provider default
  - Added an `overrides: ProviderCallOverrides | None = None` kwarg to the `generate` / `stream` abstractions (keyword-only, defaulting to None for backward compat)
- **`coderouter/adapters/openai_compat.py`** — `_prepare_messages` / `_payload` / `generate` / `stream` now accept `overrides`, reflected into both `httpx.AsyncClient(timeout=...)` and system-message injection
- **`coderouter/adapters/anthropic_native.py`** — `generate_anthropic` / `stream_anthropic` / (reverse) `generate` / `stream` now accept `overrides`, reflected into the native passthrough path's httpx timeout (`append_system_prompt` isn't natively supported in anthropic_native to begin with, so only timeout applies)
- **`coderouter/routing/fallback.py`**
  - The `_resolve_profile_overrides(profile_name)` helper — builds a `ProviderCallOverrides` once from the profile
  - All 4 entry points — `generate` / `stream` / `generate_anthropic` / `stream_anthropic` — resolve it and pass `overrides=` into the adapter call
- **`tests/test_fallback.py`** (+5) — the timeout override reaches the adapter / falls back to the provider value when unset / replacement of append_system_prompt / clearing it with `""` / FallbackChain schema sanity
- **`tests/test_openai_compat.py`** (+3) — `ProviderCallOverrides(append_system_prompt="/x")` shows up outbound / `""` skips the system injection / `ProviderCallOverrides()` is observationally equivalent to `None` (a regression guard)

### Changed

- **`tests/test_fallback.py` / `tests/test_fallback_anthropic.py`** — added the `overrides` kwarg to the fake adapters' `generate` / `stream` / `generate_anthropic` / `stream_anthropic` signatures. Since the engine now always passes `overrides=`, a fake that can't accept the kwarg fails with `TypeError`

### Design notes

- **Replace vs. append.** There were arguments both ways for `append_system_prompt` — "it's a string, so appending feels natural" vs. "no, having both provider and profile stack would be confusing" — but since `timeout_s` is a scalar constraint that can only be "replaced," keeping `append_system_prompt` (in the same field family) as replace too keeps the semantics simpler. If a use case for stacking both at the profile side emerges, a separate `append_mode: "replace" | "concat"` field could be added in v0.6+
- **The asymmetry of clearing via `""`.** Since pydantic can distinguish `None` from `""` at the field level, a special case was added inside `effective_append_system_prompt` so a profile can properly express "disable the provider directive, just for this profile": `overrides.append_system_prompt == ""` → returns `None`. The helper's comment makes explicit that this shouldn't be confused with `None` meaning "no override"
- **Where override resolution happens.** The engine adopted the approach of "build the override once per chain and distribute it to every adapter call." Doing a per-call lookup on the adapter side would avoid both (a) spreading config dependency into the adapter and (b) having to pass the profile name into the adapter. Since a profile is immutable per request, resolving it once is sufficient
- **Breaking the abstract signature.** Since a kwarg was added to `BaseAdapter.generate`, existing fake adapters (in tests) needed their signatures updated. However, since it satisfies both (i) defaulting to `None` and (ii) being keyword-only, **any third-party implementing a real adapter (none exist yet) would see zero impact.** A break only noticeable in tests
- **`retry_max` out of scope.** §9.3 #4 originally covered `retry_max` too, but the adapter layer currently has no concept of "retry within a single provider" (the fallback chain itself is the retry-equivalent mechanism). Introducing this mechanism first would create a behavior branch of "retry within the provider → fall back if that still fails," with non-obvious interaction with the midstream guard. To be reconsidered with a fuller design in v0.6-D or later

### Follow-ons

- **v0.6-C**: strengthening the declarative `ALLOW_PAID` gate — enumerating paid providers in the startup log + a `chain-paid-gate-blocked` structured log (remaining item #3 in §9.3)
- **v0.6-D**: a `mode_aliases` YAML block mapping `X-CodeRouter-Mode: coding` → a profile name (remaining item #5 in §9.3)
- **Later**: an adapter-level retry mechanism including `retry_max` (at both the profile and provider levels). Consistency with the midstream guard is the crux of the design

---

## [v0.6-A] — 2026-04-20 (`--mode` CLI + CODEROUTER_MODE env + startup validation)

**Theme: promote server-startup-time profile selection to a first-class citizen.** Through v0.5, the only options were "rewrite `default_profile` in the YAML" or "have every client send the header on every request." v0.6-A adds the `--mode <profile>` CLI option plus the `CODEROUTER_MODE` env var, enabling a lightweight server-wide / process-wide override. It also makes an unrecognized `default_profile` (one that doesn't exist in the profiles list) fail fast at startup (previously it only failed with a 500 on the first request).

- Tests: 267 → **275** (+8: 5 CLI + 3 config loader)
- Precedence: per-request > `--mode` (= `CODEROUTER_MODE`) > YAML `default_profile` > the built-in "default"
- Clears 2 of the 5 items in §9.3 (`--mode` CLI / startup fast-fail)

### Added

- **`coderouter/cli.py`**
  - A `serve --mode <profile>` argument. Strips surrounding whitespace from the given value before exporting the `CODEROUTER_MODE` env var (so a shell-quoting mishap producing `" coding "` never reaches the loader)
  - If `CODEROUTER_MODE` is already pre-set in the shell, it's respected when `--mode` isn't given, and overridden when `--mode` is given
- **`coderouter/config/schemas.py`**
  - Added a `@model_validator(mode="after")` on `CodeRouterConfig` checking that `default_profile` exists among `profiles`. Previously a typo went undetected until the `profile_by_name` lookup (i.e., the first request)
- **`coderouter/config/loader.py`**
  - Overlays the `CODEROUTER_MODE` env var (if truthy after stripping whitespace) onto `raw["default_profile"]` before pydantic validation. This makes the model-validator's existence check run against the "effective mode"
- **`coderouter/ingress/app.py`**
  - Added `default_profile` + `mode_source: "env" | "config"` to the `coderouter-startup` log at startup. Lets the operator tell at a glance whether "the shell is driving this" or "it's determined by YAML"
- **`tests/test_cli.py`** (new) — `--mode` → env, `--mode` vs. a pre-set env, whitespace stripping, `--mode` unset doesn't touch the env, plus a regression test for the existing `--config` (+5)
- **`tests/test_config.py`** — `CODEROUTER_MODE` env override, an empty string is ignored, fast-fail when the YAML-side default_profile is invalid (+3)

### Changed

- **`tests/conftest.py`** — added `CODEROUTER_MODE` to the `_clear_env` fixture, matching the existing pattern that prevents env leakage between tests
- **`README.md`** — added a `--mode` example to the Claude Code section, alongside the method of rewriting `default_profile:` in the YAML

### Design notes

- **Why consolidate on env alone?** `--mode` was kept as a thin wrapper that just exports `CODEROUTER_MODE`. Since `uvicorn --reload` spawns workers via fork, passing an argument directly would require threading it through to the factory function; aligning with the existing `--config`'s env-based pattern is more natural. The worker side can pick it up with a single `os.environ.get("CODEROUTER_MODE")` call
- **The order of overlaying env onto raw in the loader.** The env override is applied *before* `CodeRouterConfig.model_validate(raw)`. This makes the model-validator's `default_profile exists` check run against (a) not the YAML's value, but (b) the value actually used. As a result, a case where "the YAML still has an old profile name but env points at the new one" passes correctly, while a typo in env fails immediately at startup
- **Handling an empty string.** `CODEROUTER_MODE=""` or `CODEROUTER_MODE="   "` are treated as equivalent to "unset" (no override if empty after stripping). Matches the shell semantics where `export FOO=` acts as "clear"
- **The boundary of fast-fail.** Detection of an unknown profile only happens at startup + when the loader is invoked. Since a profile can never "disappear" at runtime (during a request), validating on every request would be unnecessary overhead

### Follow-ons

- **v0.6-B**: profile-level `timeout_s` / `append_system_prompt` / `retry_max` overrides (remaining item #4 in §9.3)
- **v0.6-C**: strengthening the declarative `ALLOW_PAID` gate — enumerating paid providers in the startup log + a `chain-paid-gate-blocked` structured log (remaining item #3 in §9.3)
- **v0.6-D**: a `mode_aliases` YAML block mapping `X-CodeRouter-Mode: coding` → a profile name (remaining item #5 in §9.3)

---

## [v0.5-D] — 2026-04-20 (OpenRouter roster weekly cron)

**Theme: automating proactive free-tier inventory.** Motivated by v0.4-B's experience of only noticing `deepseek-r1:free` had disappeared after the fact. `scripts/openrouter_roster_diff.py` polls `/api/v1/models` weekly using nothing but `httpx + stdlib`, appending free-tier (where both `pricing.prompt` and `pricing.completion` parse as numeric 0) diffs to `docs/openrouter-roster/CHANGES.md` newest-first. Zero imports from the `coderouter` package — the cron works safely even while the main body is mid-change.

- Tests: 243 → **267** (+24)
- Runbook + design notes: [`docs/openrouter-roster/README.md`](./docs/openrouter-roster/README.md)

### Added

- **`scripts/openrouter_roster_diff.py`** — a single-file cron script.
  - `parse_models(raw) -> list[RosterEntry]` — extracts id / context_length / pricing from the OpenRouter response; malformed rows are silently skipped
  - `is_free(entry)` — True only when both `pricing.prompt` and `pricing.completion` parse as numeric 0. Doesn't look at the `:free` suffix (pricing is authoritative, the suffix is just a hint)
  - `diff_rosters(old, new) -> RosterDiff` — 4 categories: Added / Removed / pricing_changed / context_changed, output sorted by id
  - `format_markdown(diff, *, fetched_at) -> str` — a markdown section with Removed listed first (marked with `⚠️`)
  - `prepend_changes(path, section)` — prepends to the existing CHANGES.md (newest-first), via an atomic tmp+replace
  - `run(...)` — the 1st invocation writes a snapshot but doesn't write to CHANGES.md (avoiding baseline noise). Tracking starts from the 2nd invocation onward
  - `main(argv)` — `--dry-run` / `--url` / `--snapshot` / `--changes`. Exit 0 on success / exit 2 on an HTTP error
- **`tests/test_openrouter_roster_diff.py`** — 24 tests across 3 tiers.
  - Tier 1 (8): pure logic for `parse_models` / `is_free` / `filter_free`
  - Tier 2 (8): pure diffing for `diff_rosters` / `format_markdown`
  - Tier 3 (8): `run()` orchestration — swapping `/api/v1/models` via `httpx_mock`, end-to-end coverage of first-run baseline / 2nd-run Removal detection / dry-run no-write / paid exclusion / no-change no-op / newest-first prepend / exit codes 0/2
- **`docs/openrouter-roster/README.md`** — a runbook (both manual and scheduled modes), a triage cheatsheet, the definition of "free" (pricing-based), and future extension candidates (a streaming-capability flag / a rate-limit band)

### Design notes

- **Why put this in `scripts/` with an independent import, rather than inside the `coderouter` package?** A lesson from v0.4-B: "roster inventory is better off not depending on the main body's health (you want to inventory it especially when the main body is broken)." With only `stdlib + httpx`, it can run with a single command from anywhere — a pre-merge branch, or even while production is frozen
- **Pricing is authoritative; the `:free` suffix is just a hint.** OpenRouter sometimes shows a nonzero completion price with the `:free` suffix still attached, depending on the period (observed during the v0.4-B inventory period). The invariant is pinned via `test_is_free_does_not_require_free_suffix`
- **The first-run baseline stays silent.** Writing "Added: 100 models" on the first run would degrade the log's signal, so only the snapshot is written and CHANGES.md is left untouched. Tracking starts from the 2nd run
- **Adopted prepend (newest-first).** Since some prefer `git log -p CHANGES.md` and others prefer `head CHANGES.md`, it leans toward the more intuitive newest-first in time order. With append, "the latest" would end up at the tail, invisible via head
- **Assumes a weekly cadence.** OpenRouter's roster doesn't change violently enough to need daily checks, and weekly keeps the PR-review load at a tolerable ~52/year. When registering via the schedule skill, weekday mornings JST are recommended (see the README runbook)

### Follow-ons (starting from v0.5-D)

- Registering the weekly cron with the `schedule` skill — whether to follow the README runbook manually or turn it into a scheduled task via the skill is an operational decision. v0.5-D itself only lays the groundwork of script + docs + tests
- Committing an initial `latest.json` baseline — not done within v0.5-D itself (a real `OPENROUTER_API_KEY` isn't needed for the roster GET, but real data wasn't wanted mixed into v0.5.x). It'll naturally appear on the next manual run
- Tracking the streaming capability flag (README §Future extensions) — a candidate that could explain the SSE behavior change observed in `gpt-oss-120b:free` during the v0.5 period. Implementation cost is adding 1 data column

---

## [v0.5.1] — 2026-04-20 (closeout pack)


**Theme: close out 3 v0.5-retrospective follow-ons in a single bundle.** Bundles 3 small follow-ons that emerged from v0.5-verify's real-machine run (payload typing / streaming verify / a 401-uniform warning) into a closeout pack. Zero change to core behavior (only typing the log shape + an observation tool + an additional diagnostic log); the public surface, including `NoProvidersAvailableError`, remains non-breaking.

- Tests: 225 → **243** (+18)
- Per-item narrative below: `[v0.5.1-A1]` / `[v0.5.1-A2]` / `[v0.5.1-A3]`

### Added

- **`coderouter/logging.py`** (A-1)
  - `CapabilityDegradedReason = Literal["provider-does-not-support", "translation-lossy", "non-standard-field"]` — freezes the v0.5 gate trio's 3 reasons as a type
  - `CapabilityDegradedPayload(TypedDict)` — the structural contract for `provider` / `dropped` / `reason`
  - `log_capability_degraded(logger, *, provider, dropped, reason)` — the single chokepoint every gate goes through. Keyword-only arguments statically enforce the TypedDict contract
- **`coderouter/routing/fallback.py`** (A-3)
  - `_AUTH_STATUS_CODES: Final[frozenset[int]] = frozenset({401, 403})`
  - `_warn_if_uniform_auth_failure(errors, *, profile)` — fires a `chain-uniform-auth-failure` warn only when every attempt in the chain shares the same auth status and all are non-retryable. Carries `profile` / `status` / `count` / `providers` / `hint: "probable-misconfig"` as extras
- **`scripts/verify_v0_5.sh`** (A-2)
  - `run_scenario_streaming()` — runs the streaming scenario via `curl -N` + SSE parsing, automating 3 assertions: HTTP 2xx / exactly one `capability-degraded` fire / absence of `delta.<field>` in every chunk
  - Added the `D-reasoning-stream` scenario — real-machine confirmation of v0.5-C's "log once per stream" dedup contract
- **`tests/test_capability_degraded_payload.py`** — Literal enumeration / TypedDict required_keys / the helper's emit shape / parametrized smoke tests across the 3 reasons / logger-name preservation / dispatch independence (+9 tests)
- **`tests/test_fallback_misconfig_warn.py`** — fires on a single-provider 401 / 403 treated the same / no fire on 400 / no fire when retryable / no fire on mixed status / no fire on an empty chain / fires on the streaming path (+9 tests)

### Changed

- **`coderouter/adapters/openai_compat.py`** (A-1) — imports `log_capability_degraded` directly from `coderouter.logging`. Going through `coderouter.routing.capability` would trigger an import cycle, since `routing/__init__.py` recursively calls `FallbackEngine` → `adapters/registry` → `openai_compat`; placing the helper in the leaf module `logging.py` avoids this. The reasoning-strip logs in `generate()` / `stream()` now go through the unified helper
- **`coderouter/routing/capability.py`** (A-1) — re-exports `CapabilityDegradedReason` / `CapabilityDegradedPayload` / `log_capability_degraded` from `coderouter.logging`. This module retains semantic ownership (logging for the capability gate) while delegating the actual location to a cycle-safe leaf
- **`coderouter/routing/fallback.py`** (A-3) — calls `_warn_if_uniform_auth_failure(errors, profile=profile)` right before all 4 raise sites (generate / stream / generate_anthropic / stream_anthropic). The exception shape remains non-breaking

### Design notes

- **Why `logging.py` was chosen (A-1).** The semantic home for `CapabilityDegraded*` is `routing/capability.py`, but placing the actual implementation there would trigger `routing/__init__.py`'s eager execution the moment `adapters/openai_compat.py` imports it, hitting the cycle `FallbackEngine` → `adapters/registry` → `openai_compat` (a quirk of Python package init). `logging.py` is a dependency-free leaf, so placing the type + helper there and re-exporting from capability.py achieves both "the source lives in a leaf" and "conceptual ownership stays with routing." Both modules' docstrings spell out the why
- **The 401/403-only scope (A-3).** A non-retryable error like a 400 "model not found" could also wipe out the whole chain, but that's a provider-model mismatch, not an env-var problem. Bundling it under the same `probable-misconfig` hint would mislead the operator, so the scope was narrowed to auth. Extending to non-retryable errors in general is a future decision
- **`chain-uniform-auth-failure` is a warn, not a raise (A-3).** Since preserving `NoProvidersAvailableError`'s exception shape is required to avoid breaking existing ingress / tests, the additional information only **runs alongside in the log lane.** It appears at a position grep-able in one line (right after the existing `provider-failed` trail)

### Follow-ons unchanged

- v0.5-D: the OpenRouter roster weekly cron diff (retro §Follow-ons) — untouched in v0.5.1, the next candidate
- The original v0.5-scope centerpiece (`profiles.yaml` / `--mode` CLI / declarative ALLOW_PAID / timeout-retry) — carried forward into v0.6-A

---

## [v0.5.0] — 2026-04-20 (umbrella tag for v0.5-A / v0.5-B / v0.5-C)

**Theme: the Capability gate trio.** Bundles 3 sub-releases into a single tag: v0.5-A (thinking, request-side strip + chain reorder), v0.5-B (cache_control, observability-only), v0.5-C (the OpenRouter `reasoning` field, response-side strip). A shared gate design (a unified `capability-degraded` log name / a varying `reason` / a YAML escape hatch first / SDK-independent) was established across all 3 pieces.

- Commits: `ff7ca27` (v0.5-A) → `e8803da` (v0.5-B) → `e20fb36` (v0.5-C)
- Tests: 153 → **225** (+72, +47%)
- Narrative & design matrix: [`docs/retrospectives/v0.5.md`](./docs/retrospectives/v0.5.md)
- Per-sub-release detail: sections `[v0.5-A]` / `[v0.5-B]` / `[v0.5-C]` below.

---

## [v0.5-C] — 2026-04-20

### OpenRouter `reasoning` field passive strip

Proper handling of the non-standard field discovered on real hardware during
v0.4-B's inventory. Some OpenRouter free-tier models (confirmed on real
hardware: `openai/gpt-oss-120b:free`, 2026-04-20) bundle a `reasoning` field
into the response choice's `message` / `delta`, non-compliant with the OpenAI
Chat Completions spec.

Since it's a key outside the spec, strict downstream consumers (some typed
classes in the openai SDK, strict validators) could raise a TypeError. This
was flagged and deferred in the v0.4 retro §Follow-ons as "add passive strip +
log in the future." v0.5-C resolves it by inserting a single layer at the
adapter's exit point.

#### Added

- **`coderouter/config/schemas.py`**
  - `Capabilities.reasoning_passthrough: bool = False` — an opt-out flag.
    When `true`, both the strip and the log are skipped (an escape hatch for
    when relaying CodeRouter to a reasoning-aware downstream consumer)
- **`coderouter/adapters/openai_compat.py`**
  - `_strip_reasoning_field(choices, *, delta_key)` — a pure function.
    Removes `choices[*].message.reasoning` (non-stream) / `choices[*].delta.reasoning`
    (stream) in-place. Returns a bool indicating whether anything was removed
    (used to gate the one-shot log). None / an empty list / a non-dict choice
    are defensively skipped

#### Changed

- **`coderouter/adapters/openai_compat.py`**
  - `generate()`: applies `_strip_reasoning_field(..., delta_key=False)` right
    after decoding the response JSON, before constructing `ChatResponse`. If a
    strip occurred, logs the structured `capability-degraded` event
    (`provider` / `dropped: ["reasoning"]` / `reason: "non-standard-field"`)
  - `stream()`: applies the same strip right before yielding each chunk. The
    log fires **only once** per stream (a local `reasoning_logged` flag),
    preventing repeated logging per chunk. Avoids flooding the log on a long
    reasoning track
  - Uses the same `capability-degraded` message name + `reason` discriminator
    as v0.5-A (`provider-does-not-support`) / v0.5-B (`translation-lossy`),
    keeping it grep-friendly

#### Tests

- **+15 cases** (total **225 green**, 210 → 225)
  - `test_reasoning_strip.py` (new):
    - Unit: `_strip_reasoning_field`'s message / delta stripping, no-op
      behavior (field missing / None / empty list / non-dict choice / wrong
      delta_key), multi-choice
    - Non-streaming: strip + `capability-degraded` log fires / no fire when
      reasoning is absent / preserved + no fire with
      `reasoning_passthrough: true` / content stays intact
    - Streaming: stripped from every delta + logged only once / no fire when
      absent / preserved + no fire with passthrough / `delta.content` stays
      intact

#### Notes

- **Zero impact on existing behavior**: providers that never emitted
  `reasoning` in the first place (llama.cpp / Ollama / OpenRouter's older
  models / via Anthropic) end the strip check with a false result, so neither
  the payload nor the log changes
- **The native anthropic adapter is out of scope**. Since the Anthropic wire
  response has no field equivalent to `reasoning`, the gate is entirely
  contained to the OpenAI-shape adapter
- **Real-machine verify**: the raw response returned by
  `openai/gpt-oss-120b:free` was already confirmed during v0.4-B's inventory
  (see retro §3.2). v0.5-C has reproduced that as a test via httpx_mock, so
  it stays continuously guaranteed even if OpenRouter keeps the same behavior
  going forward
- **Operational usage**: grepping for `reason: "non-standard-field"` gives a
  bulk view, from the structured log, of "which provider sent a non-standard
  key." If a new model starts emitting keys other than reasoning, the plan is
  to extend the same function (kept simple for now, since it's reasoning-only)

---

## [v0.5-B] — 2026-04-20

### cache_control observability

The 2nd piece of the capability gate, following v0.5-A (thinking). Whereas
thinking was a hard error — "a 400 if you send it to an unsupported model" —
cache_control has quite different properties: it **silently drops** during
the Anthropic → OpenAI translation step (there's no OpenAI-wire equivalent
for the marker on a content block). It doesn't error; upstream's Anthropic
prompt-cache billing optimization is simply disabled.

Given this asymmetry, v0.5-B lands as **observability-only** (no chain
reorder / no strip):

- When a cache_control-bearing request is routed to an openai_compat
  provider, it logs the structured `capability-degraded` event
  (`reason: "translation-lossy"`)
- Chain order is **not changed** — the user's provider ordering reflects
  their intent around latency / cost, and the policy is not to override
  that for cache-hit savings
- No stripping either — the existing `to_chat_request` translation already
  drops the marker automatically, so no additional processing is needed on
  the router side

#### Added

- **`coderouter/routing/capability.py`** — 2 functions plus 1 helper:
  - `provider_supports_cache_control(provider)` — always True for
    `kind: anthropic` (preserved end-to-end via native passthrough); defaults
    to False for `kind: openai_compat` (no wire equivalent). Explicitly
    declaring `capabilities.prompt_cache: true` in YAML promotes it to True
    even for openai_compat (an escape hatch for when a future upstream
    extends the OpenAI wire)
  - `anthropic_request_has_cache_control(request)` — recursively walks
    `system` (list form), `tools[*]` (via Pydantic extras), and
    `messages[*].content` (list form), returning True if even a single block
    carries a `cache_control` key
  - `_block_has_cache_control(block)` — an internal helper (dict check + key
    existence check)

#### Changed

- **`coderouter/routing/fallback.py`** — in both `generate_anthropic` /
  `stream_anthropic`:
  - For each provider in the loop, if
    `anthropic_request_has_cache_control(request)` and
    `not provider_supports_cache_control(adapter.config)`, fires the
    `capability-degraded` log (`provider` / `dropped: ["cache_control"]` /
    `reason: "translation-lossy"`)
  - The `reason` differs from v0.5-A's thinking gate
    (`provider-does-not-support`), so operators can filter between them
  - `_resolve_anthropic_chain` is **unchanged** — no reorder occurs for
    cache_control. The only change is that 2 kinds of logs can now fire
    within the same method

#### Tests

- **+21 cases** (total **210 green**, 189 → 210)
  - `test_capability.py` +13:
    - `provider_supports_cache_control`: defaults to True for anthropic,
      defaults to False for openai_compat, promoted for openai_compat via
      `prompt_cache: true`, redundant-but-harmless `prompt_cache: true` on
      anthropic
    - `anthropic_request_has_cache_control`: a plain request / a bare-string
      system / a system block with the marker / a system block without the
      marker / a tool-level marker / a message content-block marker /
      string-form content always returns False / detection on a 2nd message
      too / a marker on an image block (type-independent)
  - New `test_fallback_cache_control.py` +8:
    - openai_compat + cache_control → log fires (reason=translation-lossy,
      dropped=["cache_control"])
    - anthropic kind + cache_control → log doesn't fire
    - a plain request + openai_compat → log doesn't fire
    - **chain order is not reordered** (an important differential test
      against v0.5-A)
    - the `prompt_cache: true` escape hatch suppresses the log
    - when the fallback chain touches multiple openai_compat providers, the
      log fires once per provider
    - a mirror for the streaming path (fires for openai_compat / doesn't for
      anthropic)

#### Notes

- **Anthropic prompt cache's 1024-token floor**: a footgun already noted in
  the v0.4 retrospective §What was sharp. If the system prompt is under 1024
  tokens, Anthropic returns `cached_tokens: 0` even for a supported provider.
  v0.5-B's gate has nothing to do with this Anthropic-side constraint (this
  layer only deals with preserving the marker) — so the docstring already
  notes this to avoid the misunderstanding that "a cache-hit of 0 on a small
  prompt is a CodeRouter bug"
- **Real-machine verify**: the v0.4-D retro already confirmed on real
  hardware "1321 tokens written on call 1, 1321 read on call 2" (via native
  anthropic). Since v0.5-B is a routing-side gate, the only diff is the
  translation layer's existing behavior plus the new log
- **Operational usage**: grepping for `reason: "translation-lossy"` captures
  every event where "the user sent cache intent, but this request was routed
  to openai_compat." If this happens frequently, it's a decision point for
  either moving anthropic-direct higher in the YAML or setting
  `prompt_cache: true` on the openai_compat side

---

## [v0.5-A] — 2026-04-20

### thinking capability gate

The first piece of the "capability gate" flagged as a follow-on in the
v0.4-D retrospective. Routes Anthropic's `thinking: {type: "enabled"}` only
to supported models, degrading gracefully with a silent strip plus a
structured log for unsupported ones.

Background: the v0.4-D real-hardware test ran into `claude-sonnet-4-5-20250929`
returning a 400 on an adaptive thinking request, worked around by swapping to
`claude-sonnet-4-6`. v0.5-A demotes the user's model choice from "a decision
affecting correctness" to "purely an economic decision."

#### Added

- **`coderouter/routing/capability.py`** (new) — 3 pure functions:
  - `provider_supports_thinking(provider)` — the YAML flag takes priority; if
    unset, falls back to a model-name heuristic (capable if it matches
    `^claude-(opus|sonnet|haiku)-4-(6|7)`, `claude-opus-4-`, or
    `claude-haiku-4-`). `kind: openai_compat` is always incapable regardless
    of model name (the OpenAI wire has no thinking field)
  - `anthropic_request_requires_thinking(request)` — determines whether
    `model_extra["thinking"]` is `{"type": "enabled"}`. Disabled / missing /
    non-dict all return False
  - `strip_thinking(request)` — returns a copy with `thinking` removed from
    extras (mutation-free). `profile` / `anthropic_beta` (exclude=True
    fields) are preserved
- **`coderouter/config/schemas.py`**
  - Added `Capabilities.thinking: bool = False`. Explicitly setting `true` in
    YAML overrides the heuristic (an escape hatch for when a new model family
    appears). Coexists with `reasoning_control: Literal[...]` (the v1.0+
    abstract interface), since they're separate concerns
- **`coderouter/routing/fallback.py`**
  - `_resolve_anthropic_chain(request)` — when `request` requires thinking,
    returns the chain stable-sorted into 2 buckets: `capable` / `degraded`.
    When not required, preserves the declared order as before

#### Changed

- **`coderouter/routing/fallback.py`** — in both `generate_anthropic` /
  `stream_anthropic`:
  - Replaced `_resolve_chain(...)` with `_resolve_anthropic_chain(...)`. The
    return value is now `list[tuple[BaseAdapter, bool]]`, with a
    `will_degrade` flag attached per provider
  - Before calling a provider with `will_degrade=True`, applies
    `strip_thinking(request)` plus the structured `capability-degraded` log
    (`provider` / `dropped: ["thinking"]` / `reason`)
  - Added `"degraded": will_degrade` to the existing `try-provider` log
- The OpenAI ingress (`/v1/chat/completions`) path is unchanged. Since
  ChatRequest has no thinking field to begin with, there's no need to route
  it through the capability logic

#### Tests

- **+36 cases** (total **189 green**)
  - `test_capability.py` (new) +27: the heuristic's capable/incapable
    families (parametrized), openai_compat always incapable, an explicit
    YAML `true` winning for both kinds, `requires_thinking`'s
    enabled/disabled/missing/non-dict variants, `strip`'s removal /
    preservation / no-op / wire-body cleanliness / other extras staying
    intact
  - `test_fallback_thinking.py` (new) +9: pulling capable providers to the
    front, order preservation for a plain request, degraded fallback +
    firing the `capability-degraded` log, adapter args staying clean at the
    wire-body level after stripping, no-degraded-log on capable success /
    a plain request, openai_compat treated as incapable even for a
    Claude-like slug, promoting a model outside the heuristic to capable
    via YAML `thinking:true`, the same preference on the streaming path

#### Notes

- **Planned for v0.5-B**: normalizing `cache_control`. Unlike thinking's
  binary "400 vs 200," this has an asymmetry — "lossy pass-through via
  openai_compat / preserved via anthropic" — so it's handled in a separate
  release
- **Maintaining the heuristic table**: when a new Claude family appears, add
  a regex to `capability.py`'s `_THINKING_CAPABLE_PATTERNS`. Since it's an
  allow-list, there's no need to remove old patterns (a deprecated family
  matching causes no harm)
- **Real-machine verify is optional**: this release's behavior is already
  confirmed via 36 unit/engine tests. To see the chain reselection on real
  hardware, place a capable and an incapable provider in `providers.yaml`
  and send a thinking-enabled request to `/v1/messages`, checking for the
  presence or absence of the `capability-degraded` log

---

## [v0.4-D] — 2026-04-20

### `anthropic-beta` header passthrough (Claude Code 400 fix)

A fix for a `400 Bad Gateway` returned from Anthropic when hitting Claude
Code → CodeRouter → `anthropic-direct` on real hardware. The root cause is
that the body field `context_management` gets rejected without the
`anthropic-beta: context-management-2025-06-27` header. Claude Code was
sending the header, but CodeRouter wasn't forwarding it on to
`api.anthropic.com`.

#### Added

- **`coderouter/translation/anthropic.py`**
  - `AnthropicRequest.anthropic_beta: str | None = Field(default=None, exclude=True)`
    — a stash for the header hop. Since `exclude=True`, it never appears in
    `model_dump()`, so it doesn't leak into the wire body
- **`coderouter/ingress/anthropic_routes.py`**
  - Added `anthropic_beta: str | None = Header(alias="anthropic-beta")` to
    the `messages()` handler's arguments
  - If a value is present, sets it on the request via
    `anth_req.anthropic_beta = anthropic_beta`
- **`coderouter/adapters/anthropic_native.py`**
  - Changed the `_headers(request: AnthropicRequest | None = None)`
    signature. If `request.anthropic_beta` is set, forwards it verbatim into
    `headers["anthropic-beta"]`. Since the `/v1/chat/completions` reverse
    translation path doesn't pass a request, existing OpenAI-client behavior
    is unaffected (the OpenAI side has no such header to begin with)
  - Replaced the `self._headers()` calls in `generate_anthropic` /
    `stream_anthropic` with `self._headers(request)`. `healthcheck()` is
    called without a request context, so it stays argument-free

#### Changed

- **`coderouter/routing/fallback.py`** — improving diagnosability. Added
  `"error": str(exc)[:500]` to 6 spots in the `provider-failed` /
  `provider-failed-midstream` logs. This surfaced the exact contents of this
  400 (the precise wording of the `context_management` rejection) into the
  structured log. Future bugs of the same kind can now be narrowed down just
  by reading the server log

#### Tests

- **+6 cases** (total **153 green**)
  - `test_adapter_anthropic.py` +4:
    `test_headers_omit_anthropic_beta_when_not_set` /
    `test_headers_forward_anthropic_beta_when_set` /
    `test_generate_anthropic_forwards_anthropic_beta_header` /
    `test_stream_anthropic_forwards_anthropic_beta_header`
  - `test_ingress_anthropic.py` +2:
    `test_anthropic_beta_header_threads_through_to_request` /
    `test_missing_anthropic_beta_header_leaves_field_none`
- Coverage: (a) the field never leaks into the body (verifying
  `Field(exclude=True)`'s actual behavior against outbound JSON) / (b) the
  header reaches the outbound request (both streaming / non-streaming
  paths) / (c) ingress extracts the header and sets it on the request /
  (d) the negative case (header unset → stays None)

#### Notes

- Other beta features could go through the same path in the future.
  `anthropic-beta` is specced to take multiple comma-separated feature
  flags, so forwarding the value verbatim without touching it is correct
- Unrelated to v0.2 §8.4.1's `?beta=true` query-string issue. That one was
  simply ignored by Anthropic; this one is a heavier failure mode returning
  a 400 due to a disallowed body field

---

## [v0.4-A] — 2026-04-20

### ChatRequest → AnthropicRequest reverse translation (OpenAI ingress → kind:anthropic provider)

Fills in the path — deliberately left out of scope under design decision F
in v0.3.x-1 — of "hitting an Anthropic-native provider from an OpenAI
client." Removes `AnthropicAdapter.generate` / `.stream`'s previous
retryable=False rejection, instead calling the upstream Anthropic Messages
API via a reverse translation of `ChatRequest → AnthropicRequest` and
`AnthropicResponse → ChatResponse` / `AnthropicStreamEvent* → StreamChunk*`.
This makes the combination of the `/v1/chat/completions` ingress and a
`kind: anthropic` provider work symmetrically.

#### Added

- **`coderouter/translation/convert.py`** — added reverse-direction
  translation helpers (~300 lines)
  - `to_anthropic_request(ChatRequest) → AnthropicRequest`
    - Consolidates `role: "system"` messages into the top-level `system`
      field (multiple system messages joined with `\n`)
    - Merges consecutive `role: "tool"` messages into a single user turn,
      stored as multiple `tool_result` blocks (the Anthropic canonical
      shape)
    - Converts an assistant's `tool_calls` into `tool_use` content blocks
    - Routes `image_url` content parts into base64 or url sources based on
      whether it's a `data:` URI
    - Converts OpenAI `tools` → Anthropic `tools` (`parameters` →
      `input_schema`)
    - Bidirectional `tool_choice` mapping: `"auto"↔{type:auto}` /
      `"required"↔{type:any}` / `"none"↔{type:none}` /
      `{type:function}↔{type:tool}`
    - Defaults `max_tokens` to 4096 when omitted (Anthropic requires it;
      OpenAI treats it as optional)
    - Malformed JSON in `tool_calls.arguments` is preserved as
      `{"_raw": <string>}`
  - `to_chat_response(AnthropicResponse) → ChatResponse`
    - Concatenates multiple text blocks; promotes `tool_use` blocks to
      top-level `tool_calls`
    - Reverse-maps stop_reason: `end_turn→stop` / `max_tokens→length` /
      `tool_use→tool_calls` / `stop_sequence→stop`
    - Maps `usage.input_tokens`/`output_tokens` to OpenAI's
      `prompt_tokens` / `completion_tokens` / `total_tokens`
  - `stream_anthropic_to_chat_chunks(AnthropicStreamEvent*) → StreamChunk*`
    - Stateful translation: maps Anthropic's per-block index to OpenAI's
      `tool_calls[].index` via `_ReverseStreamState.block_idx_to_tool_idx`
    - Emits `delta.role = "assistant"` on the initial `message_start`
      (OpenAI convention)
    - `text_delta` → `delta.content`
    - A `tool_use` block_start → `delta.tool_calls[].function.name` (args
      empty)
    - `input_json_delta` → fragments of
      `delta.tool_calls[].function.arguments`
    - Emits a finish_reason-bearing chunk plus a `choices: []` usage chunk
      at the end (matching OpenAI's `stream_options.include_usage=true`
      shape)
    - An Anthropic `event: error` raises `AdapterError(retryable=False)`.
      The engine's existing v0.3-B mid-stream guard path, which converts it
      to `MidStreamError`, is unaffected
- **`coderouter/adapters/anthropic_native.py`** — replaced the `generate` /
  `stream` implementations
  - `generate(ChatRequest) → ChatResponse`:
    `to_anthropic_request` → `self.generate_anthropic` → `to_chat_response`
  - `stream(ChatRequest) → AsyncIterator[StreamChunk]`:
    `to_anthropic_request` → `self.stream_anthropic` →
    `stream_anthropic_to_chat_chunks`
  - Retryable semantics carry over as-is from the status-code
    classification of the internally-called `generate_anthropic` /
    `stream_anthropic` (429 retryable, 400 not)
  - The `coderouter_provider` tag is preserved in both directions
- **`coderouter/translation/__init__.py`** — new exports:
  `to_anthropic_request` / `to_chat_response` /
  `stream_anthropic_to_chat_chunks`

#### Changed

- **`FallbackEngine.generate` / `.stream`** — no code changes. Since
  `AnthropicAdapter`'s OpenAI-shape methods now work correctly, the
  engine's polymorphic loop naturally handles a profile that includes a
  kind:anthropic provider (including a mixed chain)
- **`coderouter/ingress/openai_routes.py`** — no changes. A path opened up
  for `/v1/chat/completions` to reach a `kind: anthropic` provider
  (previously an immediate 500)

#### Tests

Grew from 110 (at v0.3.x-1 completion) to **147 (+37)**:

- `tests/test_adapter_anthropic.py` — replaced 2 OpenAI-shape entry-point
  tests, previously "reject with retryable=False," with "works correctly
  via reverse translation" (+2 net)
  - `test_openai_shaped_generate_reverse_translates`: verifies the outbound
    body (system consolidation / tool_result batching / tools shape /
    tool_choice mapping / max_tokens default) for 5 messages — system /
    user / assistant+tool_calls / tool / user — and confirms a text+tool_use
    response comes back as a `ChatResponse`
  - `test_openai_shaped_generate_429_is_retryable`: confirms retryable=True
    is preserved for a 429 even through the reverse path
  - `test_openai_shaped_stream_reverse_translates`: consumes SSE via
    `adapter.stream`, verifying the order of the initial role chunk /
    content delta / finish / trailing usage
  - `test_openai_shaped_stream_anthropic_error_event_is_non_retryable`:
    confirms an upstream `event: error` surfaces as
    `AdapterError(retryable=False)`
- `tests/test_translation_reverse.py` **31 cases (new)**
  - `to_anthropic_request`: simple text / system consolidation / joining
    multiple system messages / a system list / assistant tool_calls /
    consecutive tool batching / tool-then-user flush / an image data URI /
    an image URL / tools conversion / 4 tool_choice cases / max_tokens
    passthrough / malformed JSON args / omitting an empty user / an empty
    assistant placeholder / stream+profile+stop
  - `to_chat_response`: text only / tool_use only / mixed / concatenating
    multiple text blocks / 4 stop_reason cases
  - `stream_anthropic_to_chat_chunks`: a text stream / a tool_use stream
    (joining arg fragments) / index separation for parallel tool_use blocks
    / `event: error` → retryable=False
- `tests/test_fallback_anthropic.py` **+4 cases**
  - `test_openai_generate_routes_to_kind_anthropic_via_reverse_translation`
  - `test_openai_stream_routes_to_kind_anthropic_via_reverse_translation`
  - `test_openai_generate_mixed_chain_falls_over_openai_to_anthropic`
  - `test_openai_stream_midstream_kind_anthropic_raises_midstream_error`

Test total: **147 passed**. Lint: 0 issues introduced by v0.4-A.

#### Design Decisions

- **A**: convert transparently at the adapter layer (leave the engine
  unchanged). Since `FallbackEngine.generate` / `.stream` loop without
  caring about the provider kind, the reverse translation stays entirely
  self-contained within `AnthropicAdapter.generate` / `.stream`'s internal
  implementation
- **B**: treat the `model` sent by the client as a placeholder; the
  provider config's `model` always takes priority (the same rule as
  v0.3.x-1's openai_compat / anthropic-native rules)
- **C**: when receiving multiple consecutive OpenAI `role: "tool"`
  messages, consolidate them into Anthropic's canonical shape (a single
  user turn with multiple `tool_result` blocks)
- **D**: an Anthropic `event: error` → `AdapterError(retryable=False)`.
  Since the role chunk is already emitted at the initial `message_start`,
  the engine's mid-stream guard converting this to `MidStreamError` has
  also been verified to work correctly

#### Known Limitations

- When sending via the "OpenAI ingress → kind:anthropic provider" path, if
  the client omits `max_tokens`, it defaults to 4096. Users who want
  precise control need to explicitly set `max_tokens` in the
  `/v1/chat/completions` body
- Anthropic-specific `cache_control` / `thinking` blocks have no OpenAI-side
  equivalent, so they can't be set from the OpenAI ingress. Users who want
  to make use of cache_control should use the `/v1/messages` ingress added
  in v0.3.x-1

---

## [v0.3.x-1] — 2026-04-20

### Anthropic Native Adapter (passthrough)

A native adapter that passes through, at zero translation cost, to Claude's
own or OpenRouter's Anthropic-compatible endpoints. Enabled via
`ProviderConfig.kind: "anthropic"`, it lets Anthropic-specific fields —
cache_control / thinking / structured tool_use, etc. — be used as-is along
the path `/v1/messages` → `AnthropicAdapter` → the upstream Anthropic
Messages API. Also supports a fallback chain mixed with openai_compat
providers (native first → openai_compat afterward, or vice versa).

#### Added

- **`coderouter/adapters/anthropic_native.py`** — `AnthropicAdapter(BaseAdapter)`
  - Auth: the `x-api-key` header (not Authorization: Bearer), sourced from `api_key_env`
  - Defaults `anthropic-version: 2023-06-01`; overridable via `extra_body.anthropic_version`
  - Normalizes `base_url` regardless of whether it ends in `/v1`, hitting `{base}/v1/messages`
  - `generate_anthropic(AnthropicRequest) → AnthropicResponse` — a direct httpx passthrough call
  - `stream_anthropic(AnthropicRequest) → AsyncIterator[AnthropicStreamEvent]`
    - Buffers SSE as `event:` / `data:` pairs, finalizing a block at blank-line boundaries
    - Silently skips heartbeat comment lines and malformed blocks
  - The OpenAI-shape `generate` / `stream` raise an `AdapterError` with
    `retryable=False` (the reverse translation `ChatRequest → AnthropicRequest`
    is out of scope per design decision F)
  - Retryable status codes: `{404, 408, 425, 429, 500, 502, 503, 504}`
  - Strips the client-sent `model`; the provider config's `model` always wins
- **`coderouter/routing/fallback.py`** — added Anthropic-specific dispatch (~110 lines)
  - `generate_anthropic(AnthropicRequest) → AnthropicResponse`:
    switches between native / openai_compat per adapter via
    `isinstance(adapter, AnthropicAdapter)`. Native is a straight passthrough;
    openai_compat goes through `to_chat_request` → `adapter.generate` →
    `to_anthropic_response(allowed_tool_names=...)` (triggering the v0.3-A repair)
  - `stream_anthropic(AnthropicRequest) → AsyncIterator[AnthropicStreamEvent]`:
    native does a straight passthrough of `adapter.stream_anthropic`;
    openai_compat + tools uses the v0.3-D downgrade (internal non-stream →
    repair → `synthesize_anthropic_stream_from_response`); openai_compat
    without tools uses real streaming via `stream_chat_to_anthropic_events`
  - The mid-stream guard preserves the same semantics as the existing
    `stream()` (`AdapterError` after the first event is sent →
    `MidStreamError`, no fallback allowed)

#### Changed

- **`coderouter/config/schemas.py`** — added `"anthropic"` to `ProviderConfig.kind`'s
  `Literal` (alongside `openai_compat`). Existing configs continue defaulting to `openai_compat`
- **`coderouter/adapters/registry.py`** — `build_adapter` now branches to
  instantiate `AnthropicAdapter` for `kind="anthropic"`
- **`coderouter/ingress/anthropic_routes.py`** — substantially simplified as a
  side effect of moving the v0.3-D downgrade logic into the engine. The
  `messages()` handler now just calls `engine.generate_anthropic` /
  `engine.stream_anthropic`, with ingress retaining only the responsibility
  for the HTTP boundary + SSE wire format. `_anthropic_sse_iterator` wraps
  events flowing from the engine while converting
  `NoProvidersAvailableError → overloaded_error` /
  `MidStreamError → api_error`
- **`examples/providers.yaml`** — added an `anthropic-direct` sample provider
  (`kind: anthropic`, `paid: true`, referencing `ANTHROPIC_API_KEY`)

#### Tests

Grew from 87 (at v0.3 completion) to **110 (+23)**:

- `tests/test_adapter_anthropic.py` **11 cases (new)**
  - URL normalization (both with and without a trailing `/v1`)
  - `x-api-key` / `anthropic-version` headers (both default and override)
  - OpenAI-shape `generate` / `stream` reject with retryable=False
  - `generate_anthropic`: payload shape (the client's model is ignored, the
    provider config wins), status mapping for 429 / 400 / 500
  - `stream_anthropic`: SSE parsing turns `event:`/`data:` pairs into
    AnthropicStreamEvent, `stream: true` lands in the body, an initial 4xx
    becomes AdapterError, heartbeat / malformed blocks are skipped
- `tests/test_fallback_anthropic.py` **12 cases (new)**
  - A round trip for both native passthrough and via openai_compat
  - Tool-call repair firing even for `generate_anthropic` via openai_compat
  - Bidirectional fallback across mixed chains (native → openai_compat /
    openai_compat → native)
  - All providers failing → `NoProvidersAvailableError`, an immediate abort
    on non-retryable
  - Streaming: real streaming for native, real streaming for openai_compat
    without tools, downgrade for openai_compat + tools (only
    `generate_calls` gets filled, `stream_calls == []`), and **no downgrade
    for native + tools** — native's structured tool_use passes through as-is
  - A mid-stream failure → `MidStreamError`; an initial failure still falls
    back as before
- `tests/test_ingress_anthropic.py` — rewritten so the stub engines directly
  exchange `AnthropicRequest` / `AnthropicResponse` / `AnthropicStreamEvent`,
  matching the transfer of responsibility to the engine. Downgrade-related
  ingress-side tests moved to the engine side (`test_fallback_anthropic.py`)

Test total: **110 passed**. Lint: 0 issues introduced by v0.3.x-1 (the new
`anthropic_native.py`'s SIM117 deliberately follows the same pattern as the
existing `openai_compat.py`).

#### Design Decisions

- **A-1**: add Anthropic-shape entry points to the engine (not self-contained
  within the adapter alone, reusing the existing fallback / mid-stream guard
  / profile resolution as-is)
- **B**: first-class support for a mixed chain (native + openai_compat
  coexisting in a single profile)
- **C**: SSE is received on a parse basis (line-based → block-based), so the
  mid-stream guard can act at the event level
- **D**: auth is fixed to `api_key_env` + `x-api-key`, with the
  `anthropic-version` header added
- **E**: preserves the 5-dependency principle (no `anthropic` SDK, raw httpx)
- **F**: reverse translation (`ChatRequest → AnthropicRequest`) is out of
  scope. The path of hitting an Anthropic-native provider from an OpenAI
  client is future scope (`generate` / `stream` reject immediately with
  retryable=False)

#### Known Limitations

- The `model` a client sends to `/v1/messages` is ignored; the provider
  config's `model` wins (the same behavior as the OpenAI-compat adapter).
  Models can only be switched via a profile, which is a deliberate part of
  CodeRouter's routing design
- The reverse translation of `ChatRequest` → `AnthropicRequest` is not yet
  implemented (design decision F). Hitting an Anthropic-native provider from
  an OpenAI client is a v0.4+ concern

---

## [v0.3.0] — 2026-04-20

### v0.3: quality improvements for real-world operation

A phase closing 3 issues that surfaced during real-world operation of Claude
Code + a local LLM (qwen2.5-coder:14b etc.). All 3 fall into the area of
"working as specced, but breaking once a real model returns malformed
output" left over from v0.2.

#### Added

- **Tool-call repair (non-streaming / v0.3-A)** — `coderouter/translation/tool_repair.py`
  - Handles the failure pattern where an upstream model (especially
    qwen2.5-coder:14b) doesn't use the `tool_calls` field, instead returning
    `{"name": ..., "arguments": ...}` as plain text
  - Extracts JSON from the text body via a balanced-brace scanner (aware of
    strings/escapes)
  - Also detects fenced ` ```json ` blocks
  - Cross-checks against the tool-name allowlist declared by the request;
    unknown tool names are left as plain text
  - Extracted JSON is normalized into the OpenAI `tool_calls` shape, later
    converted as usual into an Anthropic `tool_use` content block
  - Invoked via `to_anthropic_response(..., allowed_tool_names=[...])`
- **Mid-stream fallback guard (v0.3-B)** — `coderouter/routing/fallback.py`
  - Added a new exception, `MidStreamError(provider, original)`
  - `FallbackEngine.stream()` now raises `MidStreamError` instead of falling
    through to the next provider if an AdapterError occurs after the first
    byte has been sent
  - `_anthropic_sse_iterator` catches `MidStreamError`, emitting
    `event: error` / `type: api_error` and closing the SSE (distinguished
    from `overloaded_error`, which is "couldn't even send the first byte")
  - Purpose: prevent Claude Code's screen from receiving a partial response
    plus duplicated content
- **Usage aggregation (v0.3-C)** — `coderouter/translation/convert.py`
  - Correctly fills `message_delta.usage.output_tokens` at the end of a stream
  - Priority: upstream's `completion_tokens` (authoritative) >
    `(emitted_chars + 3) // 4` as an estimate
  - `input_tokens` is only populated if upstream sends `prompt_tokens`
  - The OpenAI-compat adapter now automatically attaches
    `stream_options: {"include_usage": true}` when streaming. If the
    provider overrides it via `extra_body`, that takes priority
  - Even for upstream servers that ignore the flag, like Ollama, the char
    estimate keeps the value from being zero
- **Tool-call repair (streaming / v0.3-D)** — strategy 2: downgrade to non-stream
  - A streaming request that declares `tools` internally switches to
    `stream=false`, runs through v0.3-A's repair, then synthesizes an
    Anthropic SSE event sequence via
    `synthesize_anthropic_stream_from_response`
  - From the client's view, the wire is still streaming
    (`message_start → … → message_stop`)
  - Streaming without tools continues to use the real streaming path as before
  - Trade-off: for a tool turn, first-byte latency stretches out to the full
    response time (acceptable since a tool turn effectively presumes
    "act on the finished result" anyway)

#### Changed

- `coderouter/adapters/openai_compat.py` — defaults `stream_options.include_usage`
  to true when streaming
- `coderouter/translation/__init__.py` — exports
  `synthesize_anthropic_stream_from_response`
- `coderouter/routing/__init__.py` — exports `MidStreamError`
- `_handle_delta` now accumulates `emitted_chars` (text_delta + tool name +
  input_json_delta)

#### Fixed

- **`Message.content = None` triggered a pydantic ValidationError, returning
  a 500** — when Claude Code includes an assistant turn with "only
  tool_use / no text" in a multi-turn history, `_convert_anthropic_message`
  emitted `content: None`, which the `Message` model rejected. Since the
  OpenAI spec allows `content: null` on an assistant message that carries
  `tool_calls`, `Message.content`'s type in `coderouter/adapters/base.py`
  was widened to `str | list[dict[str, Any]] | None = None`, and serialization
  with `exclude_none=True` unifies behavior so the content key is never sent
  upstream at all. A regression test was added at
  `tests/test_translation_anthropic.py::test_assistant_message_with_only_tool_use_has_null_content`.

#### Tests

Grew from 54 (at v0.2 completion) to **86 (+32)** after v0.3:

- `tests/test_tool_repair.py` **13 cases (new)** — every pattern of
  extracting JSON embedded in text
- `tests/test_translation_anthropic.py` **+8 cases**
  - 3 cases for repair integration
  - 5 cases for usage aggregation (upstream priority / estimate fallback /
    including tool args / 0 for an empty response / upstream overriding the
    estimate)
  - 3 cases for the synthesizer (text-only / tool_use / mixed)
- `tests/test_ingress_anthropic.py` **+4 cases**
  - `event: error` / `type: api_error` during a mid-stream failure
  - Downgrade + repair for streaming with tools
  - Streaming without tools stays real streaming
  - Even in the downgrade path, a 502 still surfaces as an error event
- `tests/test_fallback.py` **+2 cases** — MidStreamError on a mid-stream
  failure, an initial error still falls back as before
- `tests/test_openai_compat.py` **+2 cases** — auto-attaching
  stream_options.include_usage / respecting an extra_body override

Lint (ruff): 0 issues introduced by v0.3. The remaining 11 are all pre-existing, from v0.1/v0.2.

#### Verified (2026-04-20, real hardware)

Connectivity confirmed with Ollama + qwen2.5-coder:14b + Claude Code
(`ANTHROPIC_BASE_URL=http://localhost:8088`).

- **(a) text streaming without tools (curl directly hitting `/v1/messages`)**
  — the real streaming path (`engine.stream()` →
  `stream_chat_to_anthropic_events`) matches spec through
  `message_start → content_block_start → content_block_delta × N →
  content_block_stop → message_delta → message_stop`.
  - Run 1: `usage: {output_tokens: 122, input_tokens: 46}` ← Ollama honored
    `stream_options.include_usage: true` → v0.3-C's **upstream-authoritative
    path** fired.
  - Run 2: `usage: {output_tokens: 97}` (input_tokens missing) ← Ollama
    omitted the terminal usage chunk → v0.3-C's **char-based estimate
    fallback** fired. Both paths were exercised on real hardware.
- **(b) Claude Code + streaming with tools** — `_anthropic_downgraded_tool_iterator`
  worked (the server log showed `try-provider ... stream: false` →
  `provider-ok ... stream: false`). The `tool_use` content block rendered in
  the Claude Code UI as a tool invocation (e.g., `⏺ Glob()`). However, the
  correctness of the model's tool choice is a separate-layer concern
  (qwen2.5-coder:14b, for instance, chose Glob instead of Bash for a `pwd`
  request).
- **Profile path**: confirmed on real hardware the fallback from
  `skip-paid-provider` (openrouter-claude, `ALLOW_PAID=false`) →
  `ollama-qwen-coder-14b`.
- **Bug fix (discovered during real-hardware connectivity testing)**: fixed
  the issue where pydantic rejected `Message.content = None`, returning a
  500. Since Claude Code includes an assistant turn with "tool_use only, no
  text" in its multi-turn history, this was a structural bug guaranteed to
  be hit from the 2nd turn onward.
  - Widened `Message.content` in `coderouter/adapters/base.py` to
    `str | list[dict[str, Any]] | None = None`
  - Since `_prepare_messages` dumps with `exclude_none=True`, the content
    key is never sent upstream at all → preserving the shape spec-compliant
    with OpenAI
  - Regression test:
    `test_translation_anthropic.py::test_assistant_message_with_only_tool_use_has_null_content`
- **(c) mid-stream guard**: covered via unit tests (including
  `test_ingress_anthropic.py::test_streaming_midstream_failure_emits_api_error_event`).
  Timing a real `pkill` to qwen's generation speed proved difficult, and
  Ollama's 2-process runner/serve architecture sometimes returns a graceful
  close, so real-hardware smoke testing was made optional. The logic itself
  has been tested algebraically.
- **Claude Code's tool-declaration behavior**: since Claude Code sends all
  tools (Bash/Glob/Read/Write/...) via `tools: [...]` on every turn,
  **going through Claude Code always enters the v0.3-D downgrade path.**
  The real streaming path is only used by OpenAI-shape-compatible clients
  that don't declare tools, or by an Anthropic-direct curl. This structure
  matches the CHANGELOG's Known Limitations note that "streaming with tools
  effectively has the same latency profile as non-streaming."

Total test count: **87 passed** (86 plus 1 regression test for the bug fix
found during real-hardware connectivity testing). Lint clean.

#### Known Limitations

- Even for a model like qwen2.5-coder:14b that returns tool-calls as text,
  repair can currently restore wire compliance, but whether the tool can
  actually be invoked correctly is a separate-layer question of "whether the
  model assembles the arguments correctly." Repair addresses the signal
  path, not a substitute for model capability
- Streaming with tools effectively has the same latency profile as
  non-streaming. "Streaming while showing the user the tool decision" is a
  v0.4+ concern (strategy 1: speculative emit + rollback)
- `input_tokens` is only populated if upstream sends `prompt_tokens`.
  Pre-measuring locally with a bundled tiktoken is deferred to v1.0+ due to
  the 5-dependency-package constraint (plan.md §5.4)
- Unverified via OpenRouter / the official Claude API (planned to be
  covered in v0.3-E)

---

## [v0.2.0] — 2026-04-20

### Anthropic Ingress

Anthropic clients like Claude Code can now hit CodeRouter directly via
`ANTHROPIC_BASE_URL=http://localhost:8088`.

#### Added

- **`POST /v1/messages`** — an Anthropic Messages API-compatible ingress
  - Supports both non-streaming and streaming (SSE)
  - Fires events in the spec-compliant order:
    `message_start → content_block_start → content_block_delta(×N) → content_block_stop → message_delta → message_stop`
  - Bidirectionally converts all 4 content block types: `tool_use` /
    `tool_result` / `image` / `text`
  - Accepts `system` in either string or block-list form, flattening it
    internally into a single system message
  - Passes through `stop_sequences` / `temperature` / `top_p` / `top_k`
  - Accepts the `anthropic-version` header (not enforced, only kept in the
    debug log)
  - Profile selection follows the same order as the existing OpenAI route:
    body > the `X-CodeRouter-Profile` header > default
  - An unknown profile returns 400; every provider failing returns 502
    (non-stream) / an `event: error` (stream)
- **`coderouter/translation/`** — a new module
  - `anthropic.py` — pydantic models for the Anthropic wire format (request /
    response / stream event + all 4 content block types)
  - `convert.py` — bidirectional conversion between Anthropic and the common
    `ChatRequest`/`ChatResponse`
    - `to_chat_request`, `to_anthropic_response`
    - `stream_chat_to_anthropic_events` stateful manages block indices
      (closing a text block first before switching to tool_use, assigning a
      separate index to each of multiple tool_calls)
  - Malformed tool_call JSON is preserved via an `_raw` stash and passed
    through, so it can be repaired downstream
- Added tiny handlers for **`/` and `HEAD /`** — so Claude Code's startup
  preflight doesn't get a 404
- **+28 tests / 54 total**
  - `tests/test_translation_anthropic.py` 17 cases — unit tests for
    request / response / stream conversion
  - `tests/test_ingress_anthropic.py` 11 cases — the HTTP boundary, profile
    path, SSE event ordering, error mapping

### Changed

- `providers.yaml` — changed `ollama-qwen-coder-14b`'s `timeout_s` from 120
  to 300 (since Claude Code sends a huge 15-20K-token system prompt every
  turn, a 14B-class model can easily exceed 120s)
- Updated plan.md §8 to completed status, documenting 7 implementation
  learnings in §8.4 and the items pushed to v0.3+ in §8.5

### Verified

- Full-path connectivity confirmed via
  `ANTHROPIC_BASE_URL=http://localhost:8088 claude`
  - Covered text responses, streaming SSE ordering, and passing through tool
    definitions
- All 54 tests green
- Confirmed working on real hardware against official Ollama /
  qwen2.5-coder:14b

### Known Limitations (→ v0.3+)

- **Instability of structured tool-call output**: passing Claude Code's 10+
  tool definitions to qwen2.5-coder:14b sometimes returns a JSON block in
  the text body instead of using the `tool_calls` field. This isn't a
  translation bug but a model-capability limit; v1.0's "tool-call
  reliability" scope will add a text → tool_calls extraction heuristic.
- **Mid-stream fallback**: falling back to another provider after a provider
  drops post-first-byte is not currently prohibited. Planned to change to a
  `first_byte_sent` guard plus emitting `event: error` in v0.3.
- **`message_delta.usage.output_tokens`** is fixed at 0 (usage isn't
  aggregated at the end of a stream). To be fixed in v0.3.
- **The Anthropic native adapter** (`kind: "anthropic"`, a pass-through that
  skips translation) is not yet implemented. Planned for v0.3+.

---

## [v0.1.0] — 2026-04-20

### Walking Skeleton

The minimal skeleton where "an OpenAI-compatible ingress plus 1 local
provider plus 1 fallback" works.

#### Added

- **`POST /v1/chat/completions`** — an OpenAI Chat Completions-compatible
  ingress (non-streaming / streaming SSE)
- **The adapter layer** — `BaseAdapter` + `OpenAICompatAdapter` (covering
  llama.cpp / Ollama / OpenRouter / LM Studio / Together / Groq with a
  single implementation)
- **`FallbackEngine`** — sequential fallback, aborting on
  `retryable=False`, skipping `paid=true` providers in an
  `ALLOW_PAID=false` environment
- **`providers.yaml` / `profiles`** — provider definitions + named fallback
  chains
- **Profile selection** — the order is body `profile` field >
  `X-CodeRouter-Profile` header > `default_profile`
- **`ProviderConfig.extra_body` / `append_system_prompt`** — model-specific
  options
- **JSON structured logging** — `try-provider` / `provider-ok` /
  `provider-failed` / `skip-paid-provider` from
  `coderouter.routing.fallback`
- **The `/healthz`** endpoint
- **26 tests** (config / fallback / OpenAI-compat / profile selection)

### Verified

- Successfully generated fizzbuzz via curl
- Fallback: removing the 1st provider automatically transitions to the 2nd
- Confirmed the fast profile on real hardware (a successful 2-hop from
  qwen2.5:1.5b → gemma3:1b)
- All 26 tests green

### Notable Decisions / Implementation Learnings

- **qwen3.x's thinking mode can't be suppressed**
  - Ollama drops `think: false` / qwen3.5:4b refuses `/no_think` due to RL
  - Removed qwen3.x from the fast profile, moving it to a dedicated `think`
    profile
- **A lazy module-level `app`** via `__getattr__`
  - Keeps `uvicorn coderouter.ingress.app:app` working while avoiding an
    eager load of providers.yaml on test import
- **Bug fix**: fixed an issue where `request.model` was overwriting the
  provider's model (now sends the provider-specific model as specced)
- **Bug fix**: changed 404 to retryable (allowing fallback for a routing
  mismatch)

---

## Unreleased

See [`plan.md` §8.5](./plan.md) and [`plan.md` §18](./plan.md) for the v0.3+ candidate list.
