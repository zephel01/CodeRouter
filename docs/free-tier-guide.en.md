# Free-tier guide — NVIDIA NIM + OpenRouter for zero-cost operation

> This document is the operational recipe for running CodeRouter as long as possible without a credit card.
> [`README.en.md`](../README.en.md) tells you what CodeRouter *is*; [`docs/usage-guide.en.md`](./usage-guide.en.md) tells you *how to use it in general*. This page covers only **which free tiers to stack, in what order, and where they bite**.

Japanese version: [`docs/free-tier-guide.md`](./free-tier-guide.md)

---

## Contents

1. [Compare the three free tiers](#1-compare-the-three-free-tiers)
2. [Recommended fallback chain — the `claude-code-nim` profile](#2-recommended-fallback-chain--the-claude-code-nim-profile)
3. [Setup (three commands)](#3-setup-three-commands)
4. [Live-verified NIM model roster](#4-live-verified-nim-model-roster)
5. [Current OpenRouter free-tier roster](#5-current-openrouter-free-tier-roster)
6. [Five common footguns](#6-five-common-footguns)
7. [Health-check with `coderouter doctor`](#7-health-check-with-coderouter-doctor)
8. [Related docs](#8-related-docs)

---

## 1. Compare the three free tiers

There are three "no credit card required" lanes you can stack in a CodeRouter fallback chain.

| Tier | Example | Rate limit | Billing model | Tool calling | Notes |
|---|---|---|---|---|---|
| **Local** | Ollama / llama.cpp (qwen2.5-coder:7B etc.) | Hardware-limited | Zero | Model-dependent (≥7B practical) | Fastest, cheapest, offline-capable |
| **NVIDIA NIM dev tier** | `meta/llama-3.3-70b-instruct` etc. | **40 req/min** | Initial credit grant (consumed per request) | ✓ (Llama-3.3 / Qwen3-Coder-480B / Kimi-K2 confirmed) | 70B–480B class models, for free |
| **OpenRouter free** | `qwen/qwen3-coder:free` / `openai/gpt-oss-120b:free` etc. | ~20 req/min / ~200 req/day (per model) | Truly free (no credit burn) | ✓ (varies by SKU) | Roster rotates, 429s common |

**Operational rule of thumb**:

- If the local model can answer, **always local**. Wins on speed, latency, and cost.
- When local times out, **NIM first**. The 40 req/min ceiling and 70B–480B-class models comfortably swallow Claude Code's 15-20K-token system prompt.
- When NIM 429s or your credit balance runs out, **fall through to OpenRouter free**. Model quality is thinner than NIM but the `:free` SKUs are truly free.
- If you still need an answer, flip `ALLOW_PAID=true` and let the paid API catch it (last resort).

Stacking these three free lanes vertically is exactly what the `claude-code-nim` profile in `examples/providers.nvidia-nim.yaml` does.

---

## 2. Recommended fallback chain — the `claude-code-nim` profile

Open `examples/providers.nvidia-nim.yaml` and you'll find this 8-step chain already wired:

```yaml
profiles:
  - name: claude-code-nim
    providers:
      - ollama-qwen-coder-7b       # 1. local (speed)
      - ollama-qwen-coder-14b      # 2. local (quality)
      - nim-llama-3.3-70b          # 3. NIM free (primary; tool calls OK)
      - nim-qwen3-coder-480b       # 4. NIM free (480B MoE, agentic coding)
      - nim-kimi-k2                # 5. NIM free (strong tool calling)
      - openrouter-free            # 6. OpenRouter free (qwen3-coder:free)
      - openrouter-gpt-oss-free    # 7. OpenRouter free (diff vendor, dodges 429)
      - openrouter-claude          # 8. paid Claude (only when ALLOW_PAID=true)
```

Design intent:

- **Two local layers.** 7B for latency, 14B for quality. 95% of small edits finish in 1-2.
- **Three NIM layers from *different model families*.** Meta / Qwen / Moonshot — one SKU deprecation or 429 burst never kills the whole NIM lane.
- **Two OpenRouter free layers from different vendors.** Qwen + OpenAI: quota pools are usually independent.
- **Paid last.** Under the default `ALLOW_PAID=false`, that entry is skipped entirely.

No local GPU? Use `nim-first` instead:

```yaml
  - name: nim-first
    providers:
      - nim-llama-3.3-70b
      - nim-qwen3-coder-480b
      - nim-kimi-k2
      - openrouter-free
      - openrouter-gpt-oss-free
      - openrouter-claude
```

---

## 3. Setup (three commands)

### 3.1 Grab the free API keys

**NVIDIA NIM**: sign up at [build.nvidia.com](https://build.nvidia.com) (no CC required) → API Keys tab → generate an `nvapi-...` key.

**OpenRouter**: [openrouter.ai/keys](https://openrouter.ai/keys) → generate `sk-or-v1-...`. No top-up needed if you only use `:free` SKUs.

### 3.2 Put them in `.env`

```bash
# Repo root, git-ignored
NVIDIA_NIM_API_KEY=nvapi-...
OPENROUTER_API_KEY=sk-or-v1-...
ALLOW_PAID=false            # default; flip to true only when you want paid APIs
```

### 3.3 Copy the sample config and start

```bash
cp examples/providers.nvidia-nim.yaml ~/.coderouter/providers.yaml
coderouter start --profile claude-code-nim
```

Now `http://localhost:8088/v1/messages` (Claude Code ingress) and `http://localhost:8088/v1/chat/completions` (OpenAI ingress) are both up.
For Claude Code:

```bash
ANTHROPIC_BASE_URL=http://localhost:8088 claude
```

---

## 4. Live-verified NIM model roster

Live-probed on 2026-04-23 against `integrate.api.nvidia.com/v1` with both a chat probe and an explicit tool-call probe. The `examples/providers.nvidia-nim.yaml` shipped in this repo reflects this verification.

### 4.1 Shipped with `tools: true`

| Model ID | chat (pong) | tool_calls | YAML name |
|---|---|---|---|
| `meta/llama-3.3-70b-instruct` | 540 ms | ✅ | `nim-llama-3.3-70b` |
| `qwen/qwen3-coder-480b-a35b-instruct` | 634 ms | ✅ | `nim-qwen3-coder-480b` |
| `moonshotai/kimi-k2-instruct` | 2,838 ms | ✅ | `nim-kimi-k2` |

### 4.2 Shipped with `tools: false` (chat-only)

| Model ID | chat | tool_calls | Notes |
|---|---|---|---|
| `qwen/qwen2.5-coder-32b-instruct` | 160 ms | **HTTP 400** | NIM explicitly rejects tool-laden requests (`"Tool use has not been enabled"`). Declaring `tools: false` lets CodeRouter's capability gate route tool-carrying traffic around this entry |

### 4.3 NOT shipped (documented so you don't retry blindly)

| Model ID | Symptom | Workaround |
|---|---|---|
| `nvidia/llama-3.1-nemotron-70b-instruct` | HTTP 404 | Not in the account roster |
| `deepseek-ai/deepseek-r1` | HTTP 410 `"reached its end of life on 2026-01-26"` | EOL on NIM; use `deepseek-v3.*` variants |
| `nvidia/llama-3.3-nemotron-super-49b-v1.5` | 200 OK but `content: null`, no `tool_calls` | Wrong response shape for Claude Code |
| `deepseek-ai/deepseek-v3.2` | TIMEOUT @ 15s | Heavy cold-start |
| `z-ai/glm4.7` | TIMEOUT @ 10s | Same |

> NIM rosters rotate on a months-scale cadence.
> Pull the current roster with `GET https://integrate.api.nvidia.com/v1/models` (auth: `Bearer nvapi-...`). On 2026-04-23 it listed 133 models.

### 4.4 Reasoning models in the `nim-reasoning` profile

`moonshotai/kimi-k2-thinking` returns its answer wrapped in `<think>...</think>` inside `reasoning_content`, not `content`.
The YAML ships with `output_filters: [strip_thinking]` — a no-op today but a safety net if a future NIM build bleeds the tags into the content channel.
First-byte latency is ≈4s, so select `--profile nim-reasoning` only when you actually want reasoning traces.

---

## 5. Current OpenRouter free-tier roster

Both `examples/providers.nvidia-nim.yaml` and `examples/providers.yaml` include:

| Provider name | Model | Role |
|---|---|---|
| `openrouter-free` | `qwen/qwen3-coder:free` | 262K context, agentic-coding tuned, tool calling |
| `openrouter-gpt-oss-free` | `openai/gpt-oss-120b:free` | 117B MoE (5.1B active), 131K context, dodges Qwen-side 429 |

Free-tier SKUs rotate quarterly (e.g. `deepseek/deepseek-r1:free` disappeared in 2026 Q1). Weekly diffs are recorded in `docs/openrouter-roster/CHANGES.md` (produced by `scripts/openrouter_roster_diff.py`).
When introducing a new `:free` SKU, insert it in the **middle** of the chain, not at the end — preserve the existing fallback intent.

---

## 6. Five common footguns

### 6.1 NIM "free" consumes credits

No credit card is required, but every request burns credits. Heavy MoE models (480B) burn quickly — a few days of agentic use drains the initial grant. Check your balance at [build.nvidia.com/account/usage](https://build.nvidia.com/account/usage). When the balance hits zero CodeRouter silently falls through to the OpenRouter free lane, so nothing crashes — but you need monitoring to notice NIM has gone dark (`coderouter stats` / `/dashboard` fallback events).

### 6.2 Some NIM models emit a non-standard `reasoning` field

`nim-llama-3.3-70b` attaches a top-level `reasoning` key to its `message` object. Strict OpenAI clients reject `unknown field: reasoning`, but CodeRouter's v0.5-C passive strip removes it at the adapter boundary so downstream never sees it. Only matters if you're hitting NIM raw (bypassing CodeRouter).

### 6.3 Qwen2.5-Coder-32B on NIM has tools disabled

`qwen/qwen2.5-coder-32b-instruct` returns HTTP 400 for any tool-laden request (`"Tool use has not been enabled"`).
Declaring `tools: true` against this slug means **every Claude Code turn trips a 400** — CodeRouter recovers but wastes one round-trip per turn.
Declare `tools: false` explicitly (already done in `examples/providers.nvidia-nim.yaml`).

### 6.4 OpenRouter free is 200 req/day per model

Driving Claude Code as an agent exhausts 200 req/day faster than you think.
The `claude-code-nim` chain — NIM upstream of OpenRouter — is the easiest way to keep that daily cap unbroken.

### 6.5 NIM model IDs are case-sensitive and drift

`meta/Llama-3.3-70B-Instruct` is a 404 (wrong case).
Slugs get retired on a quarterly cadence (we just lost `deepseek-r1`).
Whenever you add a new NIM model to the YAML, run `coderouter doctor --check-model nim-<name>` to probe auth, tool_calls, and reasoning-leak before shipping.

---

## 7. Health-check with `coderouter doctor`

Run after config load, or whenever you add a new NIM model:

```bash
coderouter doctor --check-model nim-llama-3.3-70b
```

Six probes (auth / num_ctx / tool_calls / thinking / reasoning-leak / streaming) run; on any declaration↔behavior mismatch the command prints a **copy-paste YAML patch**. Exit 0 means clean.

Real output on 2026-04-23:

```text
provider:   nim-llama-3.3-70b
  kind:     openai_compat
  base_url: https://integrate.api.nvidia.com/v1
  model:    meta/llama-3.3-70b-instruct

Probes:
  [1/6] auth+basic-chat …… [OK]
      200 OK (in=44, out=3)
  [3/6] tool_calls …… [OK]
      native `tool_calls` observed; matches declaration.
  [5/6] reasoning-leak …… [OK]
      upstream emits non-standard `reasoning`; v0.5-C adapter strips it
      before it reaches the client (expected — expect `capability-degraded`
      log lines for this provider).
Summary: all probes match declarations.
Exit: 0
```

`[5/6] reasoning-leak` intentionally shows `[OK]` because the YAML declaration (`reasoning_passthrough: false`) and the adapter behavior (strip) agree.
Expect `capability-degraded` log lines for this provider — they're the expected signal that the strip fired, not a failure.

---

## 8. Related docs

- [`docs/usage-guide.en.md`](./usage-guide.en.md) — general usage guide (hardware-tier model picks, `doctor` / `verify` usage, per-OS launch flow)
- [`docs/quickstart.en.md`](./quickstart.en.md) — shortest path to Claude Code / codex + local Ollama
- [`examples/providers.nvidia-nim.yaml`](../examples/providers.nvidia-nim.yaml) — the YAML referenced here, with every observed footgun documented inline
- [`examples/providers.yaml`](../examples/providers.yaml) — standard sample without NIM (local + OpenRouter free + paid)
- [`docs/openrouter-roster/CHANGES.md`](./openrouter-roster/CHANGES.md) — weekly diffs for OpenRouter `:free` SKUs (output of `scripts/openrouter_roster_diff.py`)
