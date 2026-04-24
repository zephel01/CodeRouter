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
6. [Six common footguns](#6-six-common-footguns)
7. [Health-check with `coderouter doctor`](#7-health-check-with-coderouter-doctor)
8. [Related docs](#8-related-docs)

---

## 1. Compare the three free tiers

There are three "no credit card required" lanes you can stack in a CodeRouter fallback chain.

| Tier | Example | Rate limit | Billing model | Tool calling | Notes |
|---|---|---|---|---|---|
| **Local** | Ollama / llama.cpp (qwen3.5:9B / gemma4:e4b etc.) | Hardware-limited | Zero | Model-dependent (≥7B practical) | Fastest, cheapest, offline-capable |
| **NVIDIA NIM dev tier** | `qwen/qwen3-coder-480b-a35b-instruct` / `moonshotai/kimi-k2-instruct` etc. | **40 req/min** | Initial credit grant (consumed per request) | ✓ (Qwen3-Coder-480B / Kimi-K2 / Llama-3.3-70B confirmed) | 70B–480B class models, for free |
| **OpenRouter free** | `qwen/qwen3-coder:free` / `openai/gpt-oss-120b:free` etc. | ~20 req/min / ~200 req/day (per model) | Truly free (no credit burn) | ✓ (varies by SKU) | Roster rotates, 429s common |

**Operational rule of thumb**:

- If the local model can answer, **always local**. Wins on speed, latency, and cost.
- When local times out, **NIM first**. The 40 req/min ceiling and 70B–480B-class models comfortably swallow Claude Code's 15-20K-token system prompt.
- When NIM 429s or your credit balance runs out, **fall through to OpenRouter free**. Model quality is thinner than NIM but the `:free` SKUs are truly free.
- If you still need an answer, flip `ALLOW_PAID=true` and let the paid API catch it (last resort).

Stacking these three free lanes vertically is exactly what the `claude-code-nim` profile in `examples/providers.nvidia-nim.yaml` does.

---

## 2. Recommended fallback chain — the `claude-code-nim` profile

Open `examples/providers.nvidia-nim.yaml` and you'll find this 8-step chain already wired (**v1.6.2 reordered the NIM lane based on real-machine verification**):

```yaml
profiles:
  - name: claude-code-nim
    providers:
      - ollama-qwen-coder-7b       # 1. local (speed)
      - ollama-qwen-coder-14b      # 2. local (quality)
      - nim-qwen3-coder-480b       # 3. NIM free (primary; agentic-coding tuned)
      - nim-kimi-k2                # 4. NIM free (second; strong tool calling)
      - nim-llama-3.3-70b          # 5. NIM free (tail fallback; see §6.6)
      - openrouter-free            # 6. OpenRouter free (qwen3-coder:free)
      - openrouter-gpt-oss-free    # 7. OpenRouter free (diff vendor, dodges 429)
      - openrouter-claude          # 8. paid Claude (only when ALLOW_PAID=true)
```

Design intent:

- **Two local layers.** 7B for latency, 14B for quality. 95% of small edits finish in 1-2.
- **Three NIM layers from *different model families*.** Qwen / Moonshot / Meta — one SKU deprecation or 429 burst never kills the whole NIM lane.
- **Qwen3-Coder-480B leads.** It's purpose-trained for agentic coding and selects tools conservatively. Llama-3.3-70B is faster but over-eagerly invokes tools when fronted by Claude Code's system prompt (see [§6.6](#66-llama-3-3-70b-over-eager-tool-calling-in-claude-code)), so we demoted it to a tail fallback (v1.6.2 reordering).
- **Two OpenRouter free layers from different vendors.** Qwen + OpenAI: quota pools are usually independent.
- **Paid last.** Under the default `ALLOW_PAID=false`, that entry is skipped entirely.

No local GPU? Use `nim-first` instead:

```yaml
  - name: nim-first
    providers:
      - nim-qwen3-coder-480b
      - nim-kimi-k2
      - nim-llama-3.3-70b
      - openrouter-free
      - openrouter-gpt-oss-free
      - openrouter-claude
```

---

## 3. Setup (three commands)

### 3.1 Grab the free API keys

**NVIDIA NIM**: sign up at [build.nvidia.com](https://build.nvidia.com) (no CC required) → API Keys tab → generate an `nvapi-...` key.

**OpenRouter**: [openrouter.ai/keys](https://openrouter.ai/keys) → generate `sk-or-v1-...`. No top-up needed if you only use `:free` SKUs.

### 3.2 Put them in `.env` (**`export` keyword is required** — v1.6.2 convention)

```bash
# Repo root, git-ignored
# `source .env` only propagates to child processes when each line is `export`-ed.
export NVIDIA_NIM_API_KEY=nvapi-...
export OPENROUTER_API_KEY=sk-or-v1-...
export ALLOW_PAID=false            # default; flip to true only when you want paid APIs
```

Without `export`, upstream returns `Header of type 'authorization' was missing` 401 and the chain falls through silently. **v1.6.3 added `coderouter doctor --check-env .env` so you can audit this in one command** — see [`docs/troubleshooting.en.md` §1-2 / §5](./troubleshooting.en.md#1-2-env-requires-export).

> **Using a secret manager (e.g. 1Password)?** You can keep `.env` off disk entirely with `op run --env-file=.env.tpl -- coderouter serve ...`. Recipes in [`docs/troubleshooting.en.md` §5-3](./troubleshooting.en.md#5-3-1password-cli-integration-recommended).

### 3.3 Copy the sample config and start

```bash
cp examples/providers.nvidia-nim.yaml ~/.coderouter/providers.yaml
coderouter serve --mode claude-code-nim --port 8088
```

> Subcommand is `serve` (the older `start` was a typo); the profile flag is `--mode` (not `--profile`); pass `--port` so it matches your `ANTHROPIC_BASE_URL`. These were tidied up in the v1.6.2 hygiene pass.

Now `http://localhost:8088/v1/messages` (Claude Code ingress) and `http://localhost:8088/v1/chat/completions` (OpenAI ingress) are both up.
For Claude Code:

```bash
ANTHROPIC_BASE_URL=http://localhost:8088 ANTHROPIC_AUTH_TOKEN=dummy claude
```

Or via 1Password:

```bash
op run --env-file=.env.tpl -- \
  coderouter serve --mode claude-code-nim --port 8088
```

---

## 4. Live-verified NIM model roster

Live-probed on 2026-04-23 against `integrate.api.nvidia.com/v1` with both a chat probe and an explicit tool-call probe. The `examples/providers.nvidia-nim.yaml` shipped in this repo reflects this verification.

### 4.1 Shipped with `tools: true`

Recommended ordering for the `claude-code-nim` profile:

| Priority | Model ID | chat (pong) | tool_calls | YAML name | Claude Code fitness |
|---|---|---|---|---|---|
| ★ Primary | `qwen/qwen3-coder-480b-a35b-instruct` | 634 ms | ✅ | `nim-qwen3-coder-480b` | **◎** purpose-built for agentic coding |
| Secondary | `moonshotai/kimi-k2-instruct` | 2,838 ms | ✅ | `nim-kimi-k2` | ○ stable tool selection, slower first byte |
| Tail fallback | `meta/llama-3.3-70b-instruct` | 540 ms | ✅ | `nim-llama-3.3-70b` | △ see [§6.6](#66-llama-3-3-70b-over-eager-tool-calling-in-claude-code) |

**Llama-3.3-70B passes raw chat / tool_calls probes cleanly, but in live Claude Code traffic (with the full system prompt) it over-eagerly invokes Skill / AskUserQuestion tools** — a `こんにちは` greeting got rewritten as `Skill(hello)`. The profile order was changed to Qwen-first in v1.6.2 to dodge this. Direct API clients without Claude Code's prompt are fine on Llama.

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
First-byte latency is ≈4s, so select `--mode nim-reasoning` only when you actually want reasoning traces.

### 4.5 Candidate models worth probing (NIM roster, not yet adopted)

The NIM roster carries several SKUs that look promising for tool calling but **haven't been live-probed by us** yet, so we haven't shipped them in the default chain. If you want vendor diversity beyond Qwen / Moonshot / Meta, add the entry to `providers.yaml` and run `coderouter doctor --check-model <name>` to confirm auth / tool_calls / reasoning-leak before promoting it.

| Model ID | Why investigate | Caveats |
|---|---|---|
| `qwen/qwen3-235b-a22b-instruct` | Smaller / faster Qwen3 alternative (235B MoE / 22B active) | Confirm presence in your account roster + tool_calls behavior |
| `mistralai/mixtral-8x22b-instruct-v0.1` | Adds Mistral — diversifies away from Qwen / Llama / Moonshot | Older SKU, deprecation candidate; doctor first |
| `mistralai/codestral-latest` | Mistral coding-specialized | tool_calls support unverified |
| `nv-mistralai/mistral-nemo-12b-instruct` | NVIDIA + Mistral collab, low-latency small model | tool_calls stability unknown |
| `nvidia/usdcode-llama3-70b-instruct` | NVIDIA coding-specialized Llama | Test tool_calls behavior under Claude Code |
| `deepseek-ai/deepseek-v3.1` | If v3.2 times out but v3.1 responds, this is a candidate | Long cold-start; expect first request to time out |

> Once adopted, place new entries in `providers.nvidia-nim.yaml` between Qwen and Kimi in the `claude-code-nim` profile so existing fallback ordering survives. **Hitting four+ verified vendors** is where NIM-lane availability really shines (single-vendor stacks share failure modes).

Adoption workflow:

```bash
# 1. Add a nim-mixtral-8x22b entry to providers.nvidia-nim.yaml
# 2. Probe three things at once
coderouter doctor --check-model nim-mixtral-8x22b
# 3. If tool_calls is [OK] and reasoning-leak is [OK], promote it
# 4. Insert into the profile and restart serve
```

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

## 6. Six common footguns

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

### 6.6 Llama-3.3-70B over-eager tool calling in Claude Code

`doctor` reports all probes green and a raw API call returns `tool_calls` cleanly — yet **fronted by Claude Code (with its 15-20K-token system prompt declaring Skill / AskUserQuestion tools), Llama-3.3-70B rewrites even harmless utterances as tool calls**. Live samples:

```
❯ こんにちは
⏺ Skill(hello)
  ⎿  Initializing…
  ⎿  Error: Unknown skill: hello. Did you mean help?
```

or:

```
❯ こんにちは
[ AskUserQuestion: What is your name? ]
  1. John
  2. Jane
  3. Type something
  4. Chat about this
```

Likely cause: aggressive agentic tuning that overfits "user message → must invoke a tool" (v1.6.2 retrospective).

**Mitigation**: the `claude-code-nim` profile demoted Llama-3.3-70B to the tail and put Qwen3-Coder-480B (purpose-built for agentic coding) and Kimi-K2 ahead of it. **Llama-3.3-70B is fine for raw API use, simple chat, translation, summarization** — define a separate profile (e.g. `chat-nim`) with Llama at the head if you have those workloads.

Full diagnostic write-up in [`docs/troubleshooting.en.md` §4-1](./troubleshooting.en.md#4-1-greetings-get-rewritten-as-tool-calls-llama-3-3-70b-class).

---

## 7. Health-check with `coderouter doctor`

Run after config load, or whenever you add a new NIM model:

```bash
# Verify the primary
coderouter doctor --check-model nim-qwen3-coder-480b

# Run the whole NIM lane in one go
for p in nim-qwen3-coder-480b nim-kimi-k2 nim-llama-3.3-70b; do
  coderouter doctor --check-model "$p" || true
done
```

Six probes (auth / num_ctx / tool_calls / thinking / reasoning-leak / streaming) run; on any declaration↔behavior mismatch the command prints a **copy-paste YAML patch**. Exit 0 means clean.

Real output on 2026-04-24 for `nim-qwen3-coder-480b` (with env injected via 1Password):

```text
$ op run --env-file=.env.tpl -- coderouter doctor --check-model nim-qwen3-coder-480b
provider:   nim-qwen3-coder-480b
  kind:     openai_compat
  base_url: https://integrate.api.nvidia.com/v1
  model:    qwen/qwen3-coder-480b-a35b-instruct

Probes:
  [1/6] auth+basic-chat …… [OK]
      200 OK (in=17, out=3)
  [3/6] tool_calls …… [OK]
      native `tool_calls` observed; matches declaration.
  [5/6] reasoning-leak …… [OK]
      no `reasoning` field observed and no content-embedded markers — nothing to strip.
Summary: all probes match declarations.
Exit: 0
```

`nim-llama-3.3-70b` clears the same checks too, but its `reasoning-leak` row notes "upstream emits non-standard `reasoning`; v0.5-C adapter strips it" — that's the expected signal that the strip fired, not a failure (`capability-degraded` log lines for this provider are likewise expected).

### 7.1 `--check-env` (added in v1.6.3)

To audit the **`.env` file's own hygiene** rather than the API keys, use the v1.6.3 `--check-env` flag:

```bash
coderouter doctor --check-env .env
```

Three checks: POSIX 0600 permissions, `.gitignore` coverage, git-tracking state. WARN / ERROR each come with a copy-paste fix. Details in [`docs/troubleshooting.en.md` §5](./troubleshooting.en.md#5-env-security-in-practice-added-in-v163).

---

## 8. Related docs

- [`docs/usage-guide.en.md`](./usage-guide.en.md) — general usage guide (hardware-tier model picks, `doctor` / `verify` usage, per-OS launch flow)
- [`docs/quickstart.en.md`](./quickstart.en.md) — shortest path to Claude Code / codex + local Ollama
- [`docs/troubleshooting.en.md`](./troubleshooting.en.md) — when things don't work (doctor, `.env` traps, Llama over-eager mitigation, 1Password recipe)
- [`examples/providers.nvidia-nim.yaml`](../examples/providers.nvidia-nim.yaml) — the YAML referenced here, with every observed footgun documented inline
- [`examples/providers.yaml`](../examples/providers.yaml) — standard sample without NIM (local + OpenRouter free + paid)
- [`docs/openrouter-roster/CHANGES.md`](./openrouter-roster/CHANGES.md) — weekly diffs for OpenRouter `:free` SKUs (output of `scripts/openrouter_roster_diff.py`)
