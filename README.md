# CodeRouter

> **Local-first coding AI with ZERO cost by default.**
> Local → free cloud → paid cloud, automatic fallback. Claude Code / OpenAI compatible. 5 dependencies.

[![status](https://img.shields.io/badge/status-pre--alpha-orange)]()
[![python](https://img.shields.io/badge/python-3.12%2B-blue)]()
[![deps](https://img.shields.io/badge/runtime%20deps-5-brightgreen)]()
[![license](https://img.shields.io/badge/license-MIT-yellow)]()

## What is this?

A small, dependency-minimal LLM router. One endpoint to point your tools at — internally it tries your local model first, falls back to free cloud (OpenRouter free), and only touches paid APIs if you explicitly opt in (`ALLOW_PAID=true`).

```
Client (Claude Code / OpenAI SDK / curl)
        │
        ▼
  CodeRouter  ──►  ① local model (free, top priority)
                   ② free cloud (OpenRouter qwen3-coder:free, deepseek-r1:free, ...)
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

## Status: v0.4-A — Symmetric OpenAI ⇄ Anthropic routing (2026-04-20)

What works today (see [CHANGELOG.md](./CHANGELOG.md) for the full log):

- [x] OpenAI-compatible `POST /v1/chat/completions` ingress
- [x] **Anthropic-compatible `POST /v1/messages`** ingress — Claude Code works via `ANTHROPIC_BASE_URL`
- [x] SSE streaming on both endpoints (Anthropic event sequence `message_start → content_block_* → message_delta → message_stop`)
- [x] Bidirectional Anthropic ⇄ OpenAI wire-format translation (`text` / `tool_use` / `tool_result` / `image` content blocks)
- [x] OpenAI-compat adapter (covers llama.cpp / Ollama / OpenRouter / LM Studio / Together / Groq)
- [x] **Native Anthropic adapter** (`kind: "anthropic"`) — from the Anthropic ingress the request passes straight through to `api.anthropic.com` with no OpenAI-shape round-trip, preserving `cache_control` / `thinking` blocks
- [x] **Symmetric routing (v0.4-A)** — `/v1/chat/completions` can also reach `kind: "anthropic"` providers; the adapter reverse-translates `ChatRequest → AnthropicRequest` (system lifted, `tool_result` blocks batched into one user turn, `tool_calls ↔ tool_use`, stream `event: error → retryable=False`)
- [x] Sequential fallback engine with `ALLOW_PAID=false` enforcement; mixed chains (`kind: anthropic` → `kind: openai_compat`) supported via polymorphic dispatch
- [x] Profile selection: body `profile` > `X-CodeRouter-Profile` header > config default
- [x] **Tool-call repair** — models that emit `{"name":..., "arguments":...}` as plain text (qwen2.5-coder:14b often does this) are lifted back to valid `tool_use` blocks via a balanced-brace scanner + allowlist matching (non-streaming and streaming-via-downgrade)
- [x] **Mid-stream fallback guard** — `MidStreamError` prevents silent fall-through after first byte; clients see an explicit `event: error` / `type: api_error` instead of spliced partial responses from two different providers
- [x] **Usage aggregation** — `message_delta.usage.output_tokens` uses upstream `completion_tokens` when available, falls back to `(emitted_chars + 3) // 4`. Adapter auto-adds `stream_options.include_usage: true`, overridable per provider.
- [x] JSON-line structured logging, `/healthz`, tests (**147 green**)

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

Or select it per-request via the `X-CodeRouter-Profile` header when your client lets you set headers.

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
      - openrouter-deepseek-free     # deepseek/deepseek-r1:free (rate-limit escape)
      - openrouter-claude            # paid, requires ALLOW_PAID=true
```

If you'd rather have the paid tier go through Anthropic's native API (so `cache_control` / `thinking` blocks survive when reached via the Anthropic ingress), swap `openrouter-claude` for `anthropic-direct` — the `claude-code-direct` profile in `examples/providers.yaml` does exactly that.

#### What to expect

- **First byte latency**: Claude Code declares all its tools (Bash/Glob/Read/Write/…) every turn, so CodeRouter always uses the v0.3-D tool-downgrade path (internal non-streaming + SSE replay). User-felt latency ≈ upstream total response time.
- **On M-series macOS**, qwen2.5-coder:7b returns in ~30–60s per turn, 14b in ~2 min. That's dominated by prompt prefill of the 15–20K-token system prompt Claude Code sends every turn — **not** a CodeRouter overhead.
- **Tool selection quality** is a model limitation, not a wire issue. CodeRouter repairs the wire (text JSON → `tool_use` block); whether the model chose the *right* tool is on the model. qwen2.5-coder:14b sometimes picks `Glob` where `Bash` would be correct — the remedy is a stronger local model or falling through to Claude via `ALLOW_PAID=true`.
- **Mid-stream failure** (Ollama dies after first chunk) surfaces as a single `event: error` to the client, no retry — the partial response is preserved and the stream closes cleanly.

Coming next (see [plan.md §18](./plan.md)):

- v0.4 — OpenRouter free-tier roster refresh (2026-04 snapshot); opportunistic cache_control / thinking passthrough audit
- v0.5 — Profiles / capability flags / per-mode routing (full scope)
- v1.0 — 14-case regression suite, Code Mode (slim Claude Code harness), output cleaning
- v1.1 — `coderouter doctor --network`, launchers
- v1.5 — Metrics dashboard

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
