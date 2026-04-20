<h1 align="center">CodeRouter</h1>

<p align="center">
  <strong>Local-first coding AI with ZERO cost by default.</strong><br>
  Local → free cloud → paid cloud, automatic fallback. Claude Code / OpenAI compatible. 5 dependencies.
</p>

<p align="center">
  <a href=""><img src="https://img.shields.io/badge/status-pre--alpha-orange" alt="status"></a>
  <a href=""><img src="https://img.shields.io/badge/python-3.12%2B-blue" alt="python"></a>
  <a href=""><img src="https://img.shields.io/badge/runtime%20deps-5-brightgreen" alt="deps"></a>
  <a href=""><img src="https://img.shields.io/badge/license-MIT-yellow" alt="license"></a>
</p>

## What is this?

A small, dependency-minimal LLM router. One endpoint to point your tools at — internally it tries your local model first, falls back to free cloud (OpenRouter free), and only touches paid APIs if you explicitly opt in (`ALLOW_PAID=true`).

```
Client (Claude Code / OpenAI SDK / curl)
        │
        ▼
  CodeRouter  ──►  ① local model (free, top priority)
                   ② free cloud (OpenRouter qwen3-coder:free, gpt-oss-120b:free, ...)
                   ③ paid cloud (Claude / GPT-4 — only if ALLOW_PAID=true)
```

## Why?

- **LiteLLM is great but heavy.** 100+ transitive dependencies = supply-chain risk. CodeRouter has 5.
- **OpenRouter alone isn't enough.** Free tier is unstable; paid only is unfriendly to new users.
- **`ollama` / `llama.cpp` directly is fast, but Claude Code can't talk to them** without translation. CodeRouter speaks both OpenAI and Anthropic wire formats, including tool-call repair for local models that emit malformed tool JSON.

See [`plan.md`](./plan.md) for the full design and roadmap.

## Quickstart (3 commands)

```bash
# 1. Install (uses uv — fast, lockfile-friendly)
uv sync

# 2. Drop a sample config
mkdir -p ~/.coderouter
cp examples/providers.yaml ~/.coderouter/providers.yaml

# 3. Run
uv run coderouter serve
```

Then point any OpenAI client at `http://127.0.0.1:4000`:

```bash
curl http://127.0.0.1:4000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "ignored",
    "messages": [{"role": "user", "content": "Hello"}]
  }'
```

The `model` field is currently a placeholder — routing is decided by the `profile` field (defaults to `default` from `providers.yaml`).

## Status: v1.0-C — Streaming-path probe (2026-04-20)

Most recent work: **v1.0-C** adds a 6th probe — the streaming-path integrity check — to `coderouter doctor --check-model`. Where v1.0-B caught **input-side** silent truncation (prompt drops from a low `num_ctx`), v1.0-C catches its **output-side** sibling: the upstream closing the SSE stream early with `finish_reason: length` and heavily-truncated content. The usual culprit is Ollama's `options.num_predict` set too low (older builds default to 128; some Ollama-compat forks default to 256). Claude Code users experience this as "the assistant's response cut off mid-word". The probe issues a small deterministic streaming request ("count from 1 to 30"), consumes the SSE stream, and branches on `finish_reason` + observed content length; truncated output emits a copy-paste `extra_body.options.num_predict: 4096` patch. Also catches a secondary symptom: `2xx` response with zero streaming chunks (upstream silently ignored `stream: true` or uses non-standard framing), reported as NEEDS_TUNING with an advisory rather than a patch because the remediation is server-side. Same Ollama-shape gating as v1.0-B (`:11434` port OR declared `options.num_ctx`) so cloud openai_compat providers SKIP without HTTP. Prior: **v1.0-B** added the direct `num_ctx` probe for input-side truncation via canary echo-back; **v1.0-A** added the declarative `output_filters: [strip_thinking, strip_stop_markers]` chain on both adapters across streaming + non-streaming. Tests: 382 → **453** (+71: 49 from v1.0-A filter chain + 10 from v1.0-B num_ctx probe + 12 from v1.0-C streaming probe). Runtime deps: 5 → 5. Prior: v0.7.0 umbrella (Beginner UX, made legible) — retrospective at [`docs/retrospectives/v0.7.md`](./docs/retrospectives/v0.7.md). Per-release detail: [CHANGELOG.md](./CHANGELOG.md) `[v1.0-C]` / `[v1.0-B]` / `[v1.0-A]` / `[v0.7-A]` / `[v0.7-B]` / `[v0.7-C]`.

What works today (see [CHANGELOG.md](./CHANGELOG.md) for the full log):

- [x] OpenAI-compatible `POST /v1/chat/completions` ingress
- [x] **Anthropic-compatible `POST /v1/messages`** ingress — Claude Code works via `ANTHROPIC_BASE_URL`
- [x] SSE streaming on both endpoints (Anthropic event sequence `message_start → content_block_* → message_delta → message_stop`)
- [x] Bidirectional Anthropic ⇄ OpenAI wire-format translation (`text` / `tool_use` / `tool_result` / `image` content blocks)
- [x] OpenAI-compat adapter (covers llama.cpp / Ollama / OpenRouter / LM Studio / Together / Groq)
- [x] **Native Anthropic adapter** (`kind: "anthropic"`) — from the Anthropic ingress the request passes straight through to `api.anthropic.com` with no OpenAI-shape round-trip, preserving `cache_control` / `thinking` blocks
- [x] **Symmetric routing (v0.4-A)** — `/v1/chat/completions` can also reach `kind: "anthropic"` providers; the adapter reverse-translates `ChatRequest → AnthropicRequest` (system lifted, `tool_result` blocks batched into one user turn, `tool_calls ↔ tool_use`, stream `event: error → retryable=False`)
- [x] **`anthropic-beta` header passthrough (v0.4-D)** — beta-gated body fields Claude Code relies on (`context_management`, newer `cache_control` / `thinking` variants) reach `api.anthropic.com` without the validator 400 because the client's `anthropic-beta` header is now forwarded verbatim
- [x] Sequential fallback engine with `ALLOW_PAID=false` enforcement; mixed chains (`kind: anthropic` → `kind: openai_compat`) supported via polymorphic dispatch
- [x] Profile selection: body `profile` > `X-CodeRouter-Profile` header > `X-CodeRouter-Mode` header (via `mode_aliases`) > config default
- [x] **Tool-call repair** — models that emit `{"name":..., "arguments":...}` as plain text (qwen2.5-coder:14b often does this) are lifted back to valid `tool_use` blocks via a balanced-brace scanner + allowlist matching (non-streaming and streaming-via-downgrade)
- [x] **Mid-stream fallback guard** — `MidStreamError` prevents silent fall-through after first byte; clients see an explicit `event: error` / `type: api_error` instead of spliced partial responses from two different providers
- [x] **Usage aggregation** — `message_delta.usage.output_tokens` uses upstream `completion_tokens` when available, falls back to `(emitted_chars + 3) // 4`. Adapter auto-adds `stream_options.include_usage: true`, overridable per provider.
- [x] **Structured upstream-error logging (v0.4-D)** — `provider-failed` log lines now include the upstream response body (truncated to 500 chars). 4xx diagnosis is no longer guesswork.
- [x] **Thinking capability gate (v0.5-A)** — requests carrying Anthropic's `thinking: {type: "enabled"}` block are routed to providers whose model supports it (stable-sorted to the front of the fallback chain); if none do, the block is silently stripped and `capability-degraded` is emitted to the structured log. This means pinning `provider.model = claude-sonnet-4-5-20250929` no longer 400s on adaptive-thinking side requests — the block is dropped on the way out rather than rejected by the upstream.
- [x] **cache_control observability (v0.5-B)** — requests carrying `cache_control` markers that are about to hit an `openai_compat` provider (where the marker is lost during Anthropic → OpenAI translation) now emit `capability-degraded` with `reason: "translation-lossy"`. Unlike thinking, this is observability-only: chain order is preserved (user's latency/cost intent outweighs cache-hit savings), and the marker drop itself happens inside the translator, not in the gate. YAML escape hatch: `capabilities.prompt_cache: true` suppresses the log if a future `openai_compat` upstream extends the wire to preserve cache markers.
- [x] **`reasoning` field passive strip (v0.5-C)** — some OpenRouter free models (confirmed on `openai/gpt-oss-120b:free`) return a non-standard `reasoning` field on each choice's `message` / `delta`. Strict OpenAI-shape clients can reject the unknown key, so the `openai_compat` adapter now removes it before the response leaves the adapter and emits `capability-degraded` (`reason: "non-standard-field"`). Streaming: one log per stream, not per chunk. YAML escape hatch: `capabilities.reasoning_passthrough: true` keeps the field intact when CodeRouter is intentionally fronting a reasoning-aware downstream.
- [x] **`--mode` CLI / `CODEROUTER_MODE` env (v0.6-A)** — pick the active profile at server-launch time without editing the config (`coderouter serve --mode claude-code-direct`). Startup validates the name against the loaded config and fails fast with the list of valid profiles instead of deferring to the first request. Per-request overrides (header / body) still win.
- [x] **Profile-level parameter override (v0.6-B)** — a profile can override `timeout_s` and `append_system_prompt` for every attempt in its chain (replace semantics: profile value wins entirely when set). `append_system_prompt: ""` explicitly clears the provider's directive for this profile. Threaded through a `ProviderCallOverrides` dataclass so adapters never need to know the profile concept.
- [x] **Declarative `ALLOW_PAID` gate (v0.6-C)** — when the paid gate filters the entire chain to empty, a single aggregate `chain-paid-gate-blocked` warn fires (with `profile` / `blocked_providers` / `hint` fields) so the root cause is grep-visible in one line, instead of getting buried under `NoProvidersAvailableError`. Per-provider `skip-paid-provider` INFO is preserved for traceability; mixed chains where a free provider survives stay silent (the normal `provider-failed` trail narrates them).
- [x] **`mode_aliases` + `X-CodeRouter-Mode` header (v0.6-D)** — clients send an intent name (`coding` / `long` / `fast`) via header; a YAML `mode_aliases:` block resolves it to the concrete profile. Lets the chain be rewired without touching client code. Precedence: body `profile` > `X-CodeRouter-Profile` > `X-CodeRouter-Mode` > default (explicit implementation always wins over intent). Broken alias targets fast-fail at startup; unknown modes → 400 with the list of declared aliases; a `mode-alias-resolved` INFO log records every resolution.
- [x] **Declarative `model-capabilities.yaml` registry (v0.7-A)** — the "which Anthropic model families accept the `thinking` body field" heuristic (and future `tools` / `reasoning_passthrough` / `max_context_tokens` defaults) lives in `coderouter/data/model-capabilities.yaml` instead of being baked into Python. Users can extend or override at `~/.coderouter/model-capabilities.yaml`. Schema: `rules: [{match: <fnmatch glob>, kind: anthropic|openai_compat|any, capabilities: {...}}]` with first-match-per-flag semantics. Precedence: `providers.yaml` `capabilities.*` (explicit per-provider) > user YAML > bundled YAML > unset. Adding a new Anthropic family when it ships is a one-line YAML edit; no code change required.
- [x] **`coderouter doctor --check-model <provider>` (v0.7-B)** — live probes one provider from `providers.yaml` (auth + basic chat / tool_calls / thinking / reasoning-leak), compares with the v0.7-A registry, and prints a per-capability verdict table plus copy-paste YAML patches on mismatch. Uses direct `httpx` (not the adapter) so the reasoning-leak probe sees the raw upstream body before the v0.5-C strip, and the thinking probe sends Anthropic's native shape. Auth-probe failure short-circuits the remaining probes (`SKIP`) to save tokens. Exit codes: `0` match / `2` needs-tuning / `1` unrecoverable (auth / unreachable / unknown provider).
- [x] **Declarative output-cleaning filter chain (v1.0-A)** — `output_filters: [strip_thinking, strip_stop_markers]` on any provider scrubs `<think>...</think>` blocks and the six default stop markers (`<|turn|>` / `<|end|>` / `<|python_tag|>` / `<|im_end|>` / `<|eot_id|>` / `<|channel>thought`) from model output. Stateful streaming: partial tags split across SSE chunks (`<thi` / `nk>`) are held back and re-examined with the next chunk; EOF flushes any safe tail via a synthetic chunk on OpenAI-compat or a synthetic `content_block_delta` on Anthropic. Per-text-block chain on Anthropic so `<think>` leakage in one content block cannot bleed into the next. One log per stream (`output-filter-applied` with `provider` / `filters` / `streaming`). Unknown filter names fail fast at config load, not on the first request. The v0.7-B doctor reasoning-leak probe now observes content-embedded markers and emits the exact `output_filters: [...]` patch needed — the first application of "transformation always rides with a probe".
- [x] **Doctor `num_ctx` probe (v1.0-B)** — direct detection of Ollama silent prompt truncation (plan.md §9.4 symptom #1). The probe embeds a canary token at the front of a ~5K-token prompt and asks the model to echo it back; a missing canary implies the front of the prompt was dropped. Fires only for Ollama-shape providers (port 11434, or an `extra_body.options.num_ctx` declaration) — other `kind: openai_compat` upstreams SKIP. Four verdict branches: canary echoed & declared adequate → OK; canary echoed with nothing declared → informational OK; canary missing with nothing declared → NEEDS_TUNING with patch adding `extra_body.options.num_ctx: 32768`; canary missing with a declaration present → NEEDS_TUNING bumping to 32768 and, when the declaration already exceeds the threshold, a note that the model's intrinsic context limit may be the real ceiling. Runs between auth and tool_calls in the pipeline so truncation verdicts dominate the report (the previous heuristic misattributed the symptom to `capabilities.tools`).
- [x] JSON-line structured logging, `/healthz`, tests (**441 green**)

### Use it with Claude Code

```bash
# Terminal 1: start CodeRouter with a Claude Code-tuned profile
uv run coderouter serve --port 8088

# Terminal 2: point Claude Code at it, selecting the tuned profile via header
ANTHROPIC_BASE_URL=http://localhost:8088 \
ANTHROPIC_AUTH_TOKEN=dummy \
claude
```

To use the `claude-code` profile from `examples/providers.yaml` (7b first, 14b as quality fallback, 14b timeout bumped to 300s), set it as the default in your config:

```yaml
# ~/.coderouter/providers.yaml
default_profile: claude-code
```

Or pick the profile at server-launch time with the `--mode` flag (v0.6-A):

```bash
uv run coderouter serve --port 8088 --mode claude-code
# equivalent to: CODEROUTER_MODE=claude-code uv run coderouter serve --port 8088
```

`--mode` overrides the YAML `default_profile` for this process only. Per-request overrides (`X-CodeRouter-Profile` header, or `profile` field in the body) still win, so `--mode` is the right knob when you want to try a different chain without editing the config file. Unknown profile names fail fast at startup rather than on the first request.

The profile itself looks like this in `examples/providers.yaml` — copy it verbatim, then edit the `base_url` / `model` of each `providers:` entry to match your local stack:

```yaml
# Tuned for ANTHROPIC_BASE_URL=http://localhost:8088 claude.
# Claude Code declares all its tools (Bash/Glob/Read/Write/...) every turn,
# so the router always uses the v0.3-D tool-downgrade path; user-felt latency
# ≈ upstream total response time. Put the fastest tool-capable model first,
# 14b second as a quality fallback, 2 free clouds for rate-limit escape,
# and Claude as paid last resort.
profiles:
  - name: claude-code
    providers:
      - ollama-qwen-coder-7b         # ~30–60s/turn on M-series, tool-capable
      - ollama-qwen-coder-14b        # quality fallback (timeout_s: 300)
      - openrouter-free              # qwen/qwen3-coder:free (262K context)
      - openrouter-gpt-oss-free      # openai/gpt-oss-120b:free (different vendor = rate-limit escape)
      - openrouter-claude            # paid, requires ALLOW_PAID=true
```

If you'd rather have the paid tier go through Anthropic's native API (so `cache_control` / `thinking` blocks survive when reached via the Anthropic ingress), swap `openrouter-claude` for `anthropic-direct` — the `claude-code-direct` profile in `examples/providers.yaml` does exactly that.

#### Profile-level parameter overrides (v0.6-B)

A profile can override two per-call provider parameters for every attempt in its chain — handy when the same provider list should behave differently under different profiles (e.g. a long-context `/no_think` mode vs. a short chat mode):

```yaml
profiles:
  - name: claude-code-long
    timeout_s: 600             # replaces ProviderConfig.timeout_s for this profile
    append_system_prompt: ""   # empty string = explicitly clear the provider's directive
    providers:
      - ollama-qwen-coder-14b
      - openrouter-free
```

Semantics: the profile value **replaces** the provider's value when set (not appended), keeping parity with how scalar fields like `timeout_s` naturally behave. `append_system_prompt: ""` explicitly clears the provider directive for this profile (distinguished from "unset", which means "fall back to the provider's default"). Unset fields leave every provider's defaults intact. `retry_max` is deferred to a later minor since adapter-level retry is still unpiloted — the fallback chain itself is currently the retry mechanism.

#### Mode aliases — `X-CodeRouter-Mode` (v0.6-D)

Clients that want to express **intent** rather than a concrete profile name can send an `X-CodeRouter-Mode` header, and CodeRouter resolves it against a YAML `mode_aliases:` block:

```yaml
# providers.yaml
mode_aliases:
  coding: claude-code          # client sends Mode: coding → profile claude-code
  long:   claude-code-long
  fast:   ollama-only
```

```bash
curl http://localhost:8088/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'X-CodeRouter-Mode: coding' \
  -d '{ "messages": [{"role":"user","content":"hi"}] }'
```

Precedence (first hit wins): body `profile` > `X-CodeRouter-Profile` header > `X-CodeRouter-Mode` header > `default_profile`. Mode sits below Profile because **Profile is the implementation, Mode is the intent** — when a caller specifies the concrete profile, respect it verbatim. This matters when a proxy in front of CodeRouter auto-injects a Mode header: an explicit body/header `profile` from the caller still wins.

Guardrails: broken alias targets fast-fail at startup (same philosophy as `default_profile` validation), unknown Mode values return 400 with the list of declared aliases, and every resolution logs a `mode-alias-resolved` INFO line so operators can grep the mapping after the fact.

#### Model capabilities registry — `model-capabilities.yaml` (v0.7-A)

The "which Anthropic families accept `thinking: {type: enabled}`" knowledge used to live as a regex literal inside `coderouter/routing/capability.py`. From v0.7-A it lives in `coderouter/data/model-capabilities.yaml` (shipped with the package) with an optional override at `~/.coderouter/model-capabilities.yaml`. Adding a new family when Anthropic releases one is a one-line YAML edit — no code change, no release cycle.

```yaml
# ~/.coderouter/model-capabilities.yaml — optional user override
version: 1
rules:
  # A hypothetical new family Anthropic just shipped; you want to use
  # it before the CodeRouter bundled defaults are updated.
  - match: "claude-sonnet-5-*"
    kind: anthropic
    capabilities:
      thinking: true

  # Your local Ollama reliably calls tools on this tag — declare it so
  # the doctor probe (v0.7-B) agrees and future glob consumers can
  # see it as an opinionated default.
  - match: "qwen3-coder:*"
    kind: openai_compat
    capabilities:
      tools: true
```

Schema: each rule has `match` (fnmatch glob against `provider.model`, case-sensitive), optional `kind` filter (`"anthropic"` / `"openai_compat"` / `"any"`, default `"any"`), and a `capabilities:` map that can declare `thinking` / `reasoning_passthrough` / `tools` / `max_context_tokens`. Rules are walked top-to-bottom and the **first rule that declares each flag** wins for that flag — a rule can override one capability while leaving others to fall through.

Precedence across the layers is `providers.yaml` `capabilities.*` per-provider (explicit opt-in) > user `model-capabilities.yaml` > bundled `model-capabilities.yaml` > unset (treated as False). This means a user never loses the ability to override — an explicit `capabilities.thinking: true` on a provider in `providers.yaml` still wins over the registry, same as v0.5-A.

Typos are caught at load time: unknown top-level fields, unknown flag names, and invalid `kind` values raise `ValidationError` before the server accepts traffic. The same fast-fail stance as `default_profile` / `mode_aliases` validation.

#### Doctor — `coderouter doctor --check-model <provider>` (v0.7-B)

"I set up Ollama and pointed the router at it, but something's off" is the most common onboarding failure. The v0.7-B `doctor` subcommand makes that a one-liner to diagnose:

```bash
coderouter doctor --check-model ollama-qwen-coder-14b
```

It runs four small probes (≤100 tokens each) against that one provider — **not** the whole fallback chain — and prints a verdict table plus copy-paste YAML patches if the observed behavior doesn't match what `providers.yaml` + `model-capabilities.yaml` currently declare:

```
provider: ollama-qwen-coder-14b  (kind=openai_compat, model=qwen2.5-coder:14b)

probe                     verdict        detail
auth+basic-chat           OK             200 in 1.4s, 18 tokens in / 6 tokens out
tool_calls                NEEDS_TUNING   model emitted a tool_use block but registry says tools=false
thinking                  N/A            kind=openai_compat; thinking probe is anthropic-only
reasoning-leak            OK             no stray `reasoning` field on choice.message

suggested patch for ~/.coderouter/providers.yaml:
  providers:
    - name: ollama-qwen-coder-14b
      capabilities:
        tools: true        # observed: model returned a well-formed tool_use block
```

The four probes and why they exist:

- **auth+basic-chat** — one trivial turn. Catches the "API key not set" / "wrong base_url" / "provider unreachable" class of failures up front. If this probe fails, the remaining three are marked `SKIP` so you don't waste time (or tokens) chasing symptoms.
- **tool_calls** — sends a fake `echo(text: string)` tool spec and a prompt that should trigger it. Non-destructive on purpose — no real side effects upstream. Verdict is `NEEDS_TUNING` when the model emits a valid `tool_use` but the registry says `tools: false` (or vice versa).
- **thinking** — Anthropic-only; sends `thinking: {type: enabled, budget_tokens: 16}` natively (bypassing the OpenAI-shape adapter) and checks whether the upstream accepts the field. Emits a registry patch (not a `providers.yaml` patch) when the family isn't yet in `model-capabilities.yaml`.
- **reasoning-leak** — issues a chat turn and inspects the raw upstream body **before** the v0.5-C strip runs, so it can distinguish "model returns `reasoning`" from "adapter already scrubbed it". Relevant to OpenRouter free models like `openai/gpt-oss-120b:free`.

Exit codes (designed to drop into CI):

| Code | Meaning |
|------|---------|
| `0`  | All probes match declared capabilities; nothing to patch |
| `2`  | At least one `NEEDS_TUNING` verdict; YAML patches are in the output |
| `1`  | A probe couldn't run — auth failed, provider unreachable, or unknown provider name. Fix the precondition and re-run |

Precedence when multiple signals fire: `1` (blocker) > `2` (tuning) > `0` (clean). That matches the Unix lint convention where `2` means "auto-fixable" and `1` means "give up". One probe failure doesn't suppress the others — each verdict line is emitted for transparency, even under auth short-circuit where the skipped probes are shown with `SKIP: upstream auth failed`.

The subcommand targets **one** provider per invocation by design: a doctor probe shouldn't suggest registry-glob changes that affect other providers sharing the same family. Re-run with a different `--check-model` name for each provider in your chain.

#### What to expect

- **First byte latency**: Claude Code declares all its tools (Bash/Glob/Read/Write/…) every turn, so CodeRouter always uses the v0.3-D tool-downgrade path (internal non-streaming + SSE replay). User-felt latency ≈ upstream total response time.
- **On M-series macOS**, qwen2.5-coder:7b returns in ~30–60s per turn, 14b in ~2 min. That's dominated by prompt prefill of the 15–20K-token system prompt Claude Code sends every turn — **not** a CodeRouter overhead.
- **Tool selection quality** is a model limitation, not a wire issue. CodeRouter repairs the wire (text JSON → `tool_use` block); whether the model chose the *right* tool is on the model. qwen2.5-coder:14b sometimes picks `Glob` where `Bash` would be correct — the remedy is a stronger local model or falling through to Claude via `ALLOW_PAID=true`.
- **Mid-stream failure** (Ollama dies after first chunk) surfaces as a single `event: error` to the client, no retry — the partial response is preserved and the stream closes cleanly.

Coming next (see [plan.md §10](./plan.md) for v1.0, §18 for v1.0+):

- v1.0 — 14-case regression suite, Code Mode (slim Claude Code harness); output cleaning shipped in **v1.0-A** (`output_filters` chain, done)
- v1.1 — `coderouter doctor --network` (explicit network-allowed runs for CI), launchers
- v1.5 — Metrics dashboard

## Choosing `kind: openai_compat` vs `kind: anthropic`

Every provider in `providers.yaml` has a `kind`. You have two options. The choice affects which wire-level features survive the hop and which clients can reach it.

| Dimension | `kind: openai_compat` | `kind: anthropic` |
|---|---|---|
| Reachable from `/v1/chat/completions` | ✅ native — no translation | ✅ via v0.4-A reverse translation |
| Reachable from `/v1/messages` | ✅ via translation + tool-call repair | ✅ native passthrough |
| Targets | llama.cpp, Ollama, OpenRouter, LM Studio, Together, Groq, ... | `api.anthropic.com`, Bedrock's Anthropic shim, any server speaking the Messages wire |
| `cache_control` blocks | ❌ lost (no OpenAI equivalent) | ✅ preserved end-to-end when reached via `/v1/messages` |
| `thinking` blocks | ❌ lost | ✅ preserved when reached via `/v1/messages` |
| Structured `tool_use` SSE events | synthesized from repair (v0.3-D downgrade) | passthrough from upstream |
| Tool-call repair (plain-text JSON → `tool_use`) | ✅ needed for local models that emit broken JSON | n/a (Anthropic never emits broken JSON) |
| `anthropic-beta` header forwarding (v0.4-D) | n/a | ✅ verbatim |

**Rules of thumb:**

- **Local model or OpenRouter free tier** → `kind: openai_compat`. The reverse path exists, but there's no reason to pay translation cost for providers that speak OpenAI wire natively.
- **Claude via the official API, and you want `cache_control` / `thinking` to work** → `kind: anthropic`, reached via `/v1/messages` (i.e. `ANTHROPIC_BASE_URL=http://localhost:8088` from Claude Code). The `claude-code-direct` profile in `examples/providers.yaml` is pre-wired for this case.
- **Claude reached from an OpenAI client** (`openai` SDK / curl against `/v1/chat/completions`) → `kind: anthropic` still works — basic chat / tools / vision survive the v0.4-A reverse path. But `cache_control` / `thinking` cannot be sent because OpenAI has no equivalent shape.
- **Mixed chain** (local first, Claude as paid last resort) → list both kinds in the same profile. The engine's polymorphic dispatch handles the hop at each provider boundary.

## Troubleshooting

First pass: **run [`coderouter doctor --check-model <provider>`](#doctor--coderouter-doctor---check-model-provider-v07-b)** against the failing provider. It runs four small probes and prints copy-paste YAML patches for any declaration mismatch. If `doctor` comes back clean and the issue is still reproducing, fall through to the log-reading workflow below.

Thanks to v0.4-D, failed upstream requests now appear in the server log with the **exact upstream response body** attached. When a request fails, look for:

```
{"level": "WARNING", "msg": "provider-failed", "provider": "...",
 "status": 4xx, "retryable": true|false, "error": "[provider status=4xx] 4xx from upstream: {...}"}
```

Common patterns and what they mean:

- **`"Extra inputs are not permitted"` on a body field** — the upstream model (usually Anthropic) rejected a field it doesn't know. If the field is gated behind an `anthropic-beta` header (`context_management`, newer `cache_control` / `thinking` variants), check that the client actually set the header. CodeRouter forwards it verbatim as of v0.4-D, but if the client never sent one, no header will reach upstream.
- **`"adaptive thinking is not supported on this model"`** — as of v0.5-A this should no longer reach the user. The capability gate routes `thinking: {type: enabled}` requests to providers whose model accepts the field (heuristic: `claude-opus-4-*` / `claude-sonnet-4-6` / `claude-sonnet-4-7` / `claude-haiku-4-*`), and strips the block when the chain only has incapable providers. If you still see this error, either (a) your chain has a newer Anthropic family that isn't in the heuristic yet — set `capabilities.thinking: true` on that provider to opt in explicitly, or (b) file an issue with the model slug so the heuristic can be updated. Check the server log for `capability-degraded` lines to confirm the gate is firing.
- **`capability-degraded` log with `reason: "non-standard-field"` and `dropped: ["reasoning"]`** (v0.5-C) — the upstream model returned an OpenAI-spec-non-compliant `reasoning` field on a choice's `message` / `delta`. Some OpenRouter free models (notably `openai/gpt-oss-120b:free`) do this. The adapter strips the field before handing the response downstream, so this log is purely observational — nothing is broken. If you actually want the reasoning text passed through (e.g. you're fronting a reasoning-aware client), set `capabilities.reasoning_passthrough: true` on that provider and the strip turns off. Streaming: the log fires at most once per stream regardless of how many chunks carried the field.
- **`capability-degraded` log with `reason: "translation-lossy"` and `dropped: ["cache_control"]`** (v0.5-B) — your request carried a `cache_control` marker but the chosen provider is `kind: openai_compat`, so the marker was dropped during Anthropic → OpenAI translation. This is not an error (the request still succeeds), but Anthropic prompt caching will not kick in on that provider. Fix by either (a) putting a `kind: anthropic` provider earlier in the chain, or (b) if a future `openai_compat` upstream preserves cache markers, set `capabilities.prompt_cache: true` on that provider to opt out of the log. Note also the Anthropic-side 1024-token minimum: system prompts shorter than that report `cached_tokens: 0` even on supported providers — that's an upstream constraint, not a CodeRouter bug.
- **`rate_limit_error` / 429** — Anthropic org-level TPM cap. This is retryable (the engine will try the next provider); adjust profile order or lower Claude Code's context with `/compact`.
- **`unknown profile 'xxx'` (400)** — the `profile` field in the request body or `X-CodeRouter-Profile` header doesn't match any `profiles[].name` in your config. The response body shows the valid names.
- **`502 Bad Gateway: all providers failed`** — every provider in the chain returned a retryable error. Inspect the `provider-failed` log lines in order; the last `error` field shows why the chain bottomed out.

Mid-stream failures surface as a single `event: error` with `type: api_error` inside the SSE stream (no 5xx HTTP status — headers have already shipped). This is distinct from "no provider could start" which emits `type: overloaded_error`.

### Ollama beginner — 5 silent-fail symptoms (v0.7-C)

"I pointed the router at a fresh Ollama install and something is off" is by far the most common onboarding failure. The symptoms almost never look like errors — they look like the model shrugged. Here are the five we've collected in practice, with the one-liner that diagnoses each and the YAML patch that fixes it. `<provider>` below is the provider name from `providers.yaml`, e.g. `ollama-qwen-coder-7b`.

**1. Blank / gibberish reply even though the provider returned 200.** Ollama's default `num_ctx` is 2048 tokens. Claude Code's system prompt alone is 15–20K tokens per turn, so everything after the first 2048 gets silently dropped from the **front** of the prompt — tool definitions, task description, everything. The model replies from the leftover tail.

```bash
coderouter doctor --check-model <provider>
# → num_ctx: NEEDS_TUNING — canary missing from reply; upstream truncated
#   (no `extra_body.options.num_ctx` declared, Ollama default is 2048)
```

```yaml
# providers.yaml — patch suggested by doctor:
- name: <provider>
  extra_body:
    options:
      num_ctx: 32768    # or 16384 if you need to save VRAM
```

As of **v1.0-B** the doctor probe detects this directly — it sends a canary token at the front of a ~5K-token prompt and asks the model to echo it back. If the canary is missing, Ollama truncated the front of the prompt. The probe fires only for Ollama-shape providers (port 11434 in the base URL, or a declared `extra_body.options.num_ctx`), so other `kind: openai_compat` upstreams SKIP it silently.

**2. Claude Code keeps saying "I can't read files".** The model received the `tools` parameter, got confused, and returned an empty assistant message. Small quantized models (≤ 7B, Q4) frequently can't handle tool specs at all. CodeRouter's v0.3-A tool-call repair can recover *malformed* tool JSON, but this case is "model never attempted a tool call" — nothing to repair.

```bash
coderouter doctor --check-model <provider>
# → tool_calls: NEEDS_TUNING — model returned no tool_use and registry says tools=true
```

```yaml
# providers.yaml — patch suggested by doctor:
- name: <provider>
  capabilities:
    tools: false    # observed: model returned no tool_use block
```

With `tools: false` the chain moves on to the next provider when a tool-heavy request arrives. Pair this with a stronger model later in the chain (e.g. qwen2.5-coder:14b or a cloud fallback).

**3. `<think>...</think>` tags leak into the UI.** Qwen3-distilled models, DeepSeek-R1 distills, and some HF GGUF variants emit chain-of-thought inside the regular content channel (not an Anthropic `thinking` block). The tags land in Claude Code's terminal verbatim.

```bash
coderouter doctor --check-model <provider>
# → reasoning-leak: NEEDS_TUNING — observed `<think>` in content,
#   provider has no `output_filters` declared
```

As of **v1.0-A** the doctor probe emits a ready-to-apply filter patch. Two independent remediations — pick either or both:

```yaml
# providers.yaml — output-side scrub (v1.0-A, always works, recommended):
- name: <provider>
  output_filters: [strip_thinking]
  # Add `strip_stop_markers` too if you also see <|turn|> / <|channel>thought / ...
```

```yaml
# providers.yaml — source-side opt-out (cheap when the model honors it;
# Qwen3 / R1-distill families respect `/no_think`):
- name: <provider>
  append_system_prompt: "/no_think"
```

`output_filters` operates on the byte stream at the adapter boundary, so it works on every model — at the cost of one pass over the content. The two can be layered; the sample `ollama-qwen-coder-*` profiles in `examples/providers.yaml` ship with `output_filters: [strip_thinking]` enabled.

**4. First request to the chain always fails, then recovers.** The `model` field in `providers.yaml` has a typo or you forgot `ollama pull <tag>`. Ollama returns `404 model not found`, which is classified as retryable (bug fix from v0.2-x), so the chain falls through — but you lose the local-tier latency advantage on every turn.

```bash
coderouter doctor --check-model <provider>
# → auth+basic-chat: UNSUPPORTED — 404 from upstream (run `ollama pull <tag>`)
# → (remaining probes SKIP — no point running them until the model exists)
```

Fix: either `ollama pull <the-tag-in-your-YAML>` or correct the typo. The 404 is Ollama's way of saying "I have no GGUF with that tag loaded". Note that HF-on-Ollama model names are required to include the `:Q4_K_M`-style quant suffix — omitting it yields the same 404.

**5. Every provider in the chain fails uniformly.** Your `OPENROUTER_API_KEY` / `ANTHROPIC_API_KEY` isn't set (or is expired), and every cloud provider in the chain 401s in turn. As of v0.5.1 A-3 there's a `chain-uniform-auth-failure` WARN that identifies this pattern after the fact, but it's easier to catch before traffic starts.

```bash
coderouter doctor --check-model <the-cloud-provider>
# → auth+basic-chat: AUTH_FAIL — 401 from upstream (check env var <KEY_NAME>)
# → (remaining probes SKIP — auth dominates)
```

Fix: set the env var, or add it to the `.env` loaded at server start (`cp examples/.env.example .env`). `coderouter doctor` reads the same env the running server would, so a successful probe from your shell is a reliable signal that the server will work too.

**Running the full set** is one `doctor` per provider:

```bash
for p in ollama-qwen-coder-7b ollama-qwen-coder-14b openrouter-free openrouter-gpt-oss-free; do
  coderouter doctor --check-model "$p" || true
done
```

Exit codes collapse into three buckets (0 clean / 2 patchable / 1 blocker) so the loop above can be wired into CI — see the [Doctor subsection](#doctor--coderouter-doctor---check-model-provider-v07-b) for the full table.

If you run both CodeRouter (router layer) and [lunacode](https://github.com/zephel01/lunacode) (editor harness) against the same local Ollama, lunacode's [`docs/MODEL_SETTINGS.md`](https://github.com/zephel01/lunacode/blob/main/docs/MODEL_SETTINGS.md) is the sister reference — it covers the same 5 symptoms at the editor/harness layer (per-model settings, chat template overrides, `/no_think` variants) where CodeRouter's provider-granularity declarations stop.

#### HF-on-Ollama reference profile

Running any HF-hosted GGUF through Ollama's `hf.co/<user>/<repo>:<quant>` loader amplifies all 5 symptoms — HF GGUFs often ship without chat templates, inherit the leaky `<think>` tag from distillation, and require the `:<quant>` suffix that catches symptom 4. `examples/providers.yaml` includes a commented-out `ollama-hf-example` stanza that exercises every knob (`extra_body.options.num_ctx`, `append_system_prompt: "/no_think"`, `capabilities.tools: false`, `reasoning_passthrough`) with inline comments mapping each to its symptom. Copy it, set `model:` to the HF tag you pulled, and run `coderouter doctor --check-model ollama-hf-example` to verify.

## Dependency policy

Strict — see [`plan.md` §5.4](./plan.md). Runtime deps:

| Package | Why |
|---------|-----|
| `fastapi` | HTTP ingress |
| `uvicorn` | ASGI server |
| `httpx` | Outbound HTTP (no Anthropic/OpenAI SDK on purpose) |
| `pydantic` | Schema validation |
| `pyyaml` | Config parsing |

That's it. No `litellm`, no `langchain`, no `openai`/`anthropic` SDKs.

## License

MIT
