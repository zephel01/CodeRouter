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
                   ② free cloud (OpenRouter free, etc.)
                   ③ paid cloud (Claude / GPT-4 — only if ALLOW_PAID=true)
```

## Why?

- **LiteLLM is great but heavy.** 100+ transitive dependencies = supply-chain risk. CodeRouter has 5.
- **OpenRouter alone isn't enough.** Free tier is unstable; paid only is unfriendly to new users.
- **`ollama` / `llama.cpp` directly is fast, but Claude Code can't talk to them** without translation. CodeRouter speaks both OpenAI and (soon) Anthropic wire formats.

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

## Status: v0.2 Anthropic Ingress (2026-04-20)

What works today (see [CHANGELOG.md](./CHANGELOG.md) for the full log):

- [x] OpenAI-compatible `POST /v1/chat/completions` ingress
- [x] **Anthropic-compatible `POST /v1/messages`** ingress — Claude Code works via `ANTHROPIC_BASE_URL`
- [x] SSE streaming on both endpoints (Anthropic event sequence `message_start → content_block_* → message_delta → message_stop`)
- [x] Bidirectional Anthropic ⇄ OpenAI wire-format translation (`text` / `tool_use` / `tool_result` / `image` content blocks)
- [x] OpenAI-compat adapter (covers llama.cpp / Ollama / OpenRouter / LM Studio / Together / Groq)
- [x] Sequential fallback engine with `ALLOW_PAID=false` enforcement
- [x] Profile selection: body `profile` > `X-CodeRouter-Profile` header > config default
- [x] JSON-line structured logging, `/healthz`, tests (54 green)

### Use it with Claude Code

```bash
# Terminal 1: start CodeRouter
uv run coderouter serve --port 8088

# Terminal 2: point Claude Code at it
ANTHROPIC_BASE_URL=http://localhost:8088 \
ANTHROPIC_AUTH_TOKEN=dummy \
claude
```

Note: Claude Code sends a 15-20K-token system prompt every turn. On a 14B local model,
expect ~2 min per turn. Use 7B-or-smaller coding models for interactive speed — see
`examples/providers.yaml` for recommended chains.

Coming next (see [plan.md §18](./plan.md)):

- v0.3 — Tool-call repair (text → `tool_calls` extraction), mid-stream fallback guard, usage aggregation
- v0.3.x — Anthropic native adapter (`kind: "anthropic"`, passthrough), Claude Code profile examples
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
