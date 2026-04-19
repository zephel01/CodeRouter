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

## Status: v0.1 Walking Skeleton

What works today:

- [x] OpenAI-compatible `POST /v1/chat/completions` ingress
- [x] SSE streaming
- [x] OpenAI-compat adapter (covers llama.cpp / Ollama / OpenRouter / LM Studio / Together / Groq)
- [x] Sequential fallback engine
- [x] `ALLOW_PAID=false` enforcement
- [x] JSON-line structured logging
- [x] Healthcheck at `/healthz`

Coming next (see [plan.md](./plan.md)):

- v0.2 — Anthropic-compatible `POST /v1/messages` (so Claude Code can use it directly)
- v0.5 — Profiles / capability flags / per-mode routing
- v1.0 — Tool-call format recovery, Code Mode (slim Claude Code harness), output cleaning
- v1.1 — `coderouter doctor --network` (audit zero outbound), launchers
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
