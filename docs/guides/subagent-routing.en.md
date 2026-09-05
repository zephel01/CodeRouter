# Routing Sub-Agents to Different Models

日本語版: [`docs/guides/subagent-routing.md`](./subagent-routing.md)

A practical guide to routing Claude Code sub-agents (`.claude/agents/*.md`) to different backends — local LLM, cloud, or an external agent CLI (`agent_cli`) — through CodeRouter, one per sub-agent role.

Table of contents:

1. [Overview](#1-overview)
2. [How it works — the three channels and their precedence](#2-how-it-works--the-three-channels-and-their-precedence)
3. [Client-side setup (Claude Code)](#3-client-side-setup-claude-code)
4. [CodeRouter-side setup](#4-coderouter-side-setup)
5. [Real-world patterns](#5-real-world-patterns)
6. [Verifying it works](#6-verifying-it-works)
7. [Limitations and known gaps](#7-limitations-and-known-gaps)
8. [Related documents](#8-related-documents)

---

## 1. Overview

CodeRouter can assign a different model/backend to each Claude Code sub-agent. A reviewer role can go to a cheap local model, an architect role to a high-capability cloud model, an audit role to an external `claude` CLI — and so on.

Start with the honest core of the mechanism. **There is no dedicated identifier for "this is a sub-agent" on the wire (the HTTP request).** Claude Code simply resolves a model name per sub-agent and sends it in the same `model` field as any other request. From CodeRouter's side, that `model` name is effectively the only practical signal for distinguishing sub-agents. So the routing described in this guide is fundamentally a two-step combination: **(1) set a distinct `model` in each sub-agent's frontmatter, then (2) let CodeRouter route on that `model` name** — it is not a dedicated feature of CodeRouter by itself.

## 2. How it works — the three channels and their precedence

CodeRouter has three channels for assigning a request to a profile (a bundle of backends), evaluated in this order (same for both ingress paths — Anthropic Messages API and OpenAI Chat Completions):

```
body.profile  >  X-CodeRouter-Profile header  >  X-CodeRouter-Mode header  >  auto_router  >  default_profile
```

Source: `coderouter/routing/auto_router.py`, `coderouter/ingress/anthropic_routes.py`, `coderouter/ingress/openai_routes.py`.

| # | Channel | Mechanism | Where it fits sub-agent routing |
|---|---|---|---|
| ① **Model-name match** | auto_router's `model_pattern` matcher (`re.fullmatch` against body's `model`) | **The primary lever.** Give each sub-agent a distinct `model` in frontmatter and route on the resolved name. Works even with a stock Claude Code sub-agent launch, which cannot inject custom headers. |
| ② **Explicit header** | `X-CodeRouter-Profile` (names a profile directly) / `X-CodeRouter-Mode` (resolved via `mode_alias`) | The right tool when your own orchestrator drives each sub-call deterministically, e.g. attaching `X-CodeRouter-Profile: planner\|coder\|reviewer-audit`. It's an override, not a condition — you cannot write a rule that branches on "if this header equals X". |
| ③ **Content-based** | auto_router's remaining 6 matchers (CJK ratio, code-fence ratio, token count, etc.) | A fallback for when the client declares no role at all. Heuristics such as: long content → planner, CJK-heavy → local, code-dense → coder. |

All three channels ultimately resolve to a profile name, and a profile is bound to a bundle of backends (a fallback chain). Sub-agent routing is built as a three-stage pipeline: (model name or explicit header) → profile → backend.

## 3. Client-side setup (Claude Code)

### The `model` frontmatter field

A sub-agent definition lives at `.claude/agents/*.md` (project) or `~/.claude/agents/*.md` (user), as YAML frontmatter plus a Markdown body; identity comes only from the `name` frontmatter field. The `model` field accepts (as of the official docs, 2026-07):

- A model alias: `sonnet` / `opus` / `haiku` / `fable`
- A full model ID: e.g. `claude-opus-4-8` / `claude-sonnet-5` (the same values accepted by `--model`)
- `inherit`: use the same model as the main conversation
- Default when unset: `inherit`

Source: <https://code.claude.com/docs/en/sub-agents> (the frontmatter table and the "Choose a model" section).

**Measured (2026-07-11, Claude Code 2.1.206–207 with a custom `ANTHROPIC_BASE_URL`; 2.1.206 is this run's measured value, `report.md:5`, and 2.1.207 is from the late-night rerun transcript `codex-debug.jsonl`'s `init` event)**: in addition to the official spec above, **arbitrary custom strings such as `model: e2e-codex` are accepted and arrive verbatim in the wire-level `model` field, with no alias expansion**. This means you can assign each sub-agent type its own non-colliding custom name and route on it directly with `model_pattern` on the CodeRouter side (see [§6.1](#61-measured-verification-results-coderouter-v290--claude-code-21206207-2026-07-11) for details).

Example:

```markdown
---
name: reviewer
description: Code review specialist. Use proactively after code changes.
model: haiku        # ← pin to a cheap model → CodeRouter routes it to local
---
(system prompt body)
```

```markdown
---
name: architect
description: Architecture design and planning specialist.
model: opus         # ← pin to a high-capability model → CodeRouter routes it to the planner profile
---
```

If every sub-agent stays on `inherit` (the same model as the main conversation), the model name can no longer distinguish them. This pattern only works if you give each sub-agent role a distinct `model` in its frontmatter.

### Sub-agent model resolution order

Inside Claude Code, the resolution order is (highest priority first):

1. Environment variable `CLAUDE_CODE_SUBAGENT_MODEL` (when set to an alias or model ID)
2. The per-invocation `model` parameter passed by the Agent/Task tool at launch time
3. The sub-agent definition's `model` frontmatter
4. The main conversation's model (`inherit`)

Source: <https://code.claude.com/docs/en/sub-agents> ("Choose a model" section, numbered list). `CLAUDE_CODE_SUBAGENT_MODEL=inherit` is treated as equivalent to unset, so resolution continues to per-invocation → frontmatter (since v2.1.196).

Frontmatter has no field for injecting an HTTP header (`tools` / `disallowedTools` / `permissionMode` / `effort` / `isolation` / `color`, etc. control behavior, not headers). In other words, channel ② (the explicit header) is not reachable from a stock Claude Code sub-agent launch — it only applies when your own orchestrator adds the header.

### Pointing Claude Code at CodeRouter with ANTHROPIC_BASE_URL

```bash
export ANTHROPIC_BASE_URL="http://localhost:8088"
export ANTHROPIC_AUTH_TOKEN="dummy"   # CodeRouter ignores auth; any non-empty value works
claude
```

`ANTHROPIC_BASE_URL` only changes *where* the request goes — it has no effect on *which model answers* (model selection is the job of the frontmatter/env settings above). With a custom `ANTHROPIC_BASE_URL`, Claude Code passes the model-name string through without allowlist validation, so CodeRouter can target arbitrary model-name strings with `model_pattern`. Source: <https://code.claude.com/docs/en/model-config>.

## 4. CodeRouter-side setup

### auto_router's `model_pattern` rules

auto_router only runs when `default_profile: auto`. Writing your own `rules` **completely replaces** the bundled defaults (image → multi / code-dense → coding / fallthrough → writing) — it does not merge with them.

```yaml
default_profile: auto
auto_router:
  default_rule_profile: coder
  rules:
    - id: user:opus-to-planner
      profile: planner
      match: { model_pattern: "(claude-)?opus.*" }     # fullmatch — routes opus-family models to planner
    - id: user:haiku-to-local
      profile: reviewer-light
      match: { model_pattern: "(claude-.*)?haiku.*" }   # fullmatch — routes haiku-family models to local reviewer
```

**Two things to watch for:**

- `model_pattern` uses **`re.fullmatch`**, not `re.search`. Your regex must match the *entire* string of whatever model name Claude Code actually sends (whether it stays `opus` or expands to `claude-opus-4-8` is environment-dependent). Forgetting the trailing `.*` is a common way for a rule to silently miss.
- The exact model-name string that arrives is environment- and version-dependent, and cannot be assumed. **Before relying on a rule in production, confirm the real value from the auto-router log** (the `signals.model` field of the `auto-router-resolved` event). See [§6](#6-verifying-it-works) for the procedure.

### The full list of auto_router matchers (8 total)

A `RuleMatcher` allows **exactly one matcher per rule** — a load-time validator enforces this, so AND-composing multiple conditions is not possible (see gap G1 in [§7](#7-limitations-and-known-gaps)). Rules are evaluated top-to-bottom, first match wins.

| Field | Type | Meaning | Evaluated against |
|---|---|---|---|
| `has_image` | `bool` (only `true` is valid) | Matches if the latest user message has an image block | Latest user message |
| `code_fence_ratio_min` | `float` (0.0–1.0) | Matches if the fraction of characters inside ` ``` ` fences is ≥ threshold | Latest user message |
| `cjk_ratio_min` | `float` (0.0–1.0) | Matches if the CJK character ratio is ≥ threshold | Latest user message |
| `content_contains` | `str` | Case-sensitive substring match | Latest user message |
| `content_regex` | `str` | `re.search` (compiled and validated at load time) | Latest user message |
| `model_pattern` | `str` | `re.fullmatch` against the body's `model` field (compiled and validated at load time) | Body's `model` |
| `content_token_count_min` | `int` (≥1) | Matches if the estimated token count (char/4 heuristic) over system + all messages is ≥ threshold | Entire request |
| `has_tools` | `bool` (only `true` is valid) | Matches if the body declares 1+ entries in `tools[]` (OpenAI/Anthropic common) or legacy OpenAI `functions[]` | Entire body |

`model_pattern`, `content_token_count_min`, and `has_tools` can fire even without a user message (e.g. a system-only request, or a body carrying just `model` + `tools`). Every other matcher requires a user message to be present. Boolean matchers (`has_image` / `has_tools`) reject an explicit `false` at load time (dead-rule prevention — omit the field instead if unused).

> **`content_token_count_min` means something very different as of v2.12.0.** The estimator now counts `tool_result`, `tool_use` and `thinking` blocks, which v2.11.x treated as zero characters, so the same conversation estimates an order of magnitude higher. Measured on a tool-driven session: v2.11.x reported 1,083 tokens after 200 turns; v2.12.0 reports 93,183. **An existing threshold will fire far earlier than you tuned it for** — including the `32000` used in the examples below. Retune it, or set `token_estimation_include_tool_content: false` to restore the v2.11.x estimate. See [context-budget.md](../concepts/context-budget.en.md).

### The X-CodeRouter-Profile header

Use this when there's no `profile` field in the body and your own orchestrator wants to drive each sub-call deterministically via header.

```bash
curl http://localhost:8088/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'X-CodeRouter-Profile: reviewer-audit' \
  -d '{"model":"opus","messages":[{"role":"user","content":"Say hi in one line"}]}'
```

Resolution order: body.profile → `X-CodeRouter-Profile` → `X-CodeRouter-Mode` (resolved to a profile via `mode_aliases`) → auto_router → `default_profile`. Naming a profile that doesn't exist returns 400.

## 5. Real-world patterns

### (a) opusplan style — Opus for planning, local/mid-tier for execution

Planning goes to Claude Opus (via `agent_cli`, read-only); execution goes to a mid-tier local model with a cloud fallback; review splits audit work (Opus) from light review (local).

```yaml
allow_paid: true            # needed for agent_cli (claude = Opus subscription)
default_profile: coder      # falls back to the day-to-day coding role when no header/rule matches

providers:
  - name: agent-claude-opus     # Planning role: Claude Opus (agent_cli, read-only)
    kind: agent_cli
    model: opus
    paid: true
    capabilities: { streaming: false, tools: false }   # agent_cli default
    agent_cli: { agent: claude, sandbox_mode: read_only, exec_timeout_s: 600 }
  - name: local-coder            # Execution role: mid-tier local (zero tax)
    kind: anthropic               # Ollama v0.23.1+ passthrough
    base_url: http://localhost:11434
    model: qwen3-coder:30b
  - name: cloud-mid               # Execution role fallback: cloud mid-tier
    kind: openai_compat
    base_url: https://openrouter.ai/api/v1
    model: qwen/qwen3-coder:free
    paid: true
  - name: agent-claude-review    # Review role: audits go to Opus
    kind: agent_cli
    model: opus
    paid: true
    capabilities: { streaming: false, tools: false }
    agent_cli: { agent: claude, sandbox_mode: read_only }
  - name: local-reviewer         # Review role: light review to a separate local model
    kind: anthropic
    base_url: http://localhost:11434
    model: qwen2.5-coder:7b

profiles:
  - name: planner
    providers: [agent-claude-opus]        # agent_cli is meant to be solo
  - name: coder
    providers: [local-coder, cloud-mid]   # local first, cloud on failure
  - name: reviewer-audit
    providers: [agent-claude-review]      # security audits go to Opus
  - name: reviewer-light
    providers: [local-reviewer]           # light review runs locally

auto_router:   # fallback for when the client declares no profile
  default_rule_profile: coder
  rules:
    - id: user:image-to-multi
      profile: planner
      match: { has_image: true }
    - id: user:dense-code-to-coder
      profile: coder
      match: { code_fence_ratio_min: 0.3 }
    - id: user:long-context-to-planner
      profile: planner
      match: { content_token_count_min: 32000 }
    - id: user:cjk-to-local
      profile: coder
      match: { cjk_ratio_min: 0.5 }
    - id: user:review-keyword
      profile: reviewer-audit
      match: { content_contains: "review" }

plugins:
  enabled: [agents]   # required since v2.9.0 for any kind: agent_cli provider
```

**A note on how this is actually driven**: the auto_router above is just the fallback for when the client declares no role. The intended way to drive opusplan is for an upper-layer orchestrator to attach `X-CodeRouter-Profile: planner|coder|reviewer-audit` explicitly to each sub-call (precedence as in [§2](#2-how-it-works--the-three-channels-and-their-precedence)). Note this is a different thing from **Claude Code's native `opusplan` alias** (which auto-switches from `opus` during plan mode to `sonnet` for execution) — don't conflate the two.

### (b) Minimal per-sub-agent model configuration

The same idea as the frontmatter example in [§3](#3-client-side-setup-claude-code): pin `reviewer` to `model: haiku` and `architect` to `model: opus`, then route on `model_pattern` in CodeRouter.

```yaml
default_profile: auto

providers:
  - name: local-reviewer
    kind: openai_compat
    base_url: http://localhost:11434/v1
    model: qwen2.5-coder:7b
  - name: cloud-planner
    kind: openai_compat
    base_url: https://openrouter.ai/api/v1
    model: anthropic/claude-opus-4-8
    paid: true

profiles:
  - name: reviewer-light
    providers: [local-reviewer]
  - name: planner
    providers: [cloud-planner]

auto_router:
  default_rule_profile: reviewer-light
  rules:
    - id: user:opus-to-planner
      profile: planner
      match: { model_pattern: "(claude-)?opus.*" }
    - id: user:haiku-to-local
      profile: reviewer-light
      match: { model_pattern: "(claude-.*)?haiku.*" }
```

### (c) Mixing in agent_cli — an audit role via an external claude CLI

`kind: agent_cli` registers an external CLI — claude / codex / grok / antigravity — as a single CodeRouter provider. **As of v2.9.0, this requires installing `coderouter-plugin-agents` and adding `plugins.enabled: [agents]` to `providers.yaml`** (without it, `coderouter-t serve` fails at startup as soon as any `kind: agent_cli` provider is present). See [`docs/backends/external-agents.md`](../backends/external-agents.en.md) for full details.

```bash
uv pip install "coderouter-plugin-agents @ git+https://github.com/zephel01/coderouter-plugin-agents"
```

```yaml
allow_paid: true
default_profile: reviewer-light

plugins:
  enabled: [agents]        # required since v2.9.0 for any kind: agent_cli provider

providers:
  - name: agent-claude-review
    kind: agent_cli
    model: opus
    paid: true
    capabilities: { streaming: false, tools: false }
    agent_cli: { agent: claude, sandbox_mode: read_only }
  - name: local-reviewer
    kind: openai_compat
    base_url: http://localhost:11434/v1
    model: qwen2.5-coder:7b

profiles:
  - name: reviewer-audit      # audit role = external claude CLI (Opus subscription)
    providers: [agent-claude-review]     # agent_cli is solo
  - name: reviewer-light      # light review = local
    providers: [local-reviewer]
```

`agent_cli` defaults to `capabilities: { streaming: false, tools: false }`, so it cannot sit mid-chain — it's limited to being the sole provider of a dedicated profile, or the terminus of a chain (gap G6 in [§7](#7-limitations-and-known-gaps)).

### (d) Content-based supplementary rules

You can also add a standalone content-based matcher as a fallback for when the client declares no role.

```yaml
auto_router:
  default_rule_profile: coder
  rules:
    - id: user:cjk-to-local
      profile: coder
      match: { cjk_ratio_min: 0.5 }        # send CJK-heavy turns to local (zero tax)
    - id: user:long-context-to-planner
      profile: planner
      match: { content_token_count_min: 32000 }   # send long-context turns to planner
```

### (e) Dropping the main orchestrator to a 9B-class model

If you want to run the main orchestrator on a 9B-class local model instead of a 30B one to save resources, the routing is unchanged, but the **selection criteria for the model behind the `main` profile** need care. The following extrapolates from the 30B (qwen3-coder:30b) measurements in §6.1; 9B-specific thresholds are unverified.

- **The main model must support tools**: sub-agent launches depend on the Task tool's `tool_use`. Main requests always carry `tools[]` (measured: main-originated requests show `has_tools: true` in report.md), so a non-tool-capable model — or a provider with `capabilities.tools: false` — behind the `main` profile makes sub-agent launch impossible. Tool-calling support is the first prerequisite when picking a 9B model (with Ollama, check that `ollama show <tag>` lists `tools` under Capabilities).
- **Weaker models are less reliable at Task calls**: in Claude Code 2.1.20x the Task tool launches sub-agents in the background and main waits for completion (§6.1). 9B-class models are more prone to `tool_use` format slips and mishandled async waits, so the false "agent unreachable" report (§6.1; observed even with 30B on the codex path) is expected to grow more frequent. Always verify sub-agent reachability with the §6 procedure before adopting one.
- **Recommended: a 9b→30b fallback chain**: build the `main` profile as a sequential failover `[<9b-tool-model>, <30b-tool-model>]` rather than a single provider, so a 9B that fails to emit correct `tool_use` (or returns an empty response) falls through to the 30B (pair with `empty_response_action: "fallback"`). Candidate examples (author's 2026-06/07 benches): **Ornith-1.0-9B (Q4_K_M) was verified in the 2026-07-12 E2E rerun** — tools-capable on Ollama, passed Phase C 4/4 on the first attempt each with no fallback to the 30B, and roughly halved the total run time vs the 30B (`results-20260712-103615`). Qwythos-9B-v2 (Q6_K) is the runner-up (its tools support on Ollama is still unverified — confirm first).

```yaml
providers:
  - name: main-9b
    kind: anthropic                 # e.g. Ollama passthrough; tool calling support is required
    base_url: http://localhost:11434
    model: <9b-tool-model>          # e.g. hf.co/deepreinforce-ai/Ornith-1.0-9B-GGUF:Q4_K_M (verified 2026-07-12)
  - name: main-30b
    kind: anthropic
    base_url: http://localhost:11434
    model: qwen3-coder:30b          # proven fallback
profiles:
  - name: main
    providers: [main-9b, main-30b]  # 9B first, falls back to 30B on failure
```

- **Cache-less local backends also trigger autocompact thrashing**: even at 9B, a main backend whose `cache_read_input_tokens` is always 0 hits the §6.1 auto-compact spin (`rapid_refill_breaker` abort). Consider disabling auto-compact or pairing with a prompt-cache-capable backend.

## 6. Verifying it works

1. **At startup**: check the `coderouter-t serve` startup log for `plugin-loaded` (if using agent_cli) and for the absence of config-load errors.
2. **Send one real request**: from Claude Code, or a curl request shaped like a sub-agent call.
3. **Read the auto-router log**: a matched rule is recorded as an `auto-router-resolved` event, and `signals.model` carries the actual `model` string that arrived — this is your primary source for confirming whether the alias stayed as-is or expanded to a full ID. The shape of the log line is roughly:

   ```json
   {"ts":"2026-07-11T10:03:21","level":"INFO","logger":"coderouter.routing.auto_router",
    "msg":"auto-router-resolved","rule_id":"user:opus-to-planner","resolved_profile":"planner",
    "signals":{"has_image":false,"code_fence_ratio":0.0,"content_len":42,
               "model":"opus","estimated_tokens":15,"has_tools":true}}
   ```

4. **Confirm the intended profile**: check that `resolved_profile` is the profile name you intended (`planner`, `reviewer-audit`, etc.). If it isn't, suspect a `fullmatch` miss in `model_pattern` (a missing trailing `.*` is the usual culprit).

### 6.1 Measured verification results (CodeRouter v2.9.0 × Claude Code 2.1.206–207, 2026-07-11)

The exact configuration this guide describes — frontmatter `model` (custom name) → auto_router `model_pattern` → profile → `agent_cli` — was verified end-to-end against all four backends: claude / codex / grok / antigravity (agy) (source: `_run/e2e-agents/results-20260711-232732/report.md` and the rerun transcript `_run/e2e-agents/codex-debug.jsonl`; Claude Code version 2.1.206 from this run, `report.md:5`, and 2.1.207 from the late-night rerun's `init` event in `codex-debug.jsonl`). The main orchestrator ran on Ollama qwen3-coder:30b (`kind: anthropic` passthrough); the four sub-agents were pinned via `.claude/agents/ext-*.md` frontmatter to `model: e2e-claude` / `e2e-codex` / `e2e-grok` / `e2e-agy`.

Confirmed:

- **Channel ② (`X-CodeRouter-Profile` header)**: 4/4 reachable (OpenAI ingress).
- **Channel ① (`model_pattern`)**: 4/4 rules fired; the custom names appeared **verbatim** in `signals.model` of `auto-router-resolved` (Anthropic ingress).
- **Sub-agent E2E**: in this run (results-20260711-232732), **3/4 (claude / grok / antigravity)** completed the round trip Task tool → CodeRouter → external CLI → answer back to the orchestrator (report.md:18,20,21). **codex flaky-failed in the same run** (report.md:19; the orchestrator mishandled the async Task wait and falsely reported "no agent named 'ext-codex' is reachable" — the known failure mode below) and passed on a late-night rerun. The rerun transcript (`_run/e2e-agents/codex-debug.jsonl`, lines 16, 54) also confirmed the upstream real model (`gpt-5.5`) coming back in the ext-codex response's `model` field.
- **Shape of the arriving model name (formerly UNCONFIRMED)**: sub-agent (frontmatter-derived) models arrive **as the custom string, unexpanded** (source: `signals.model` in report.md:26-29). The main conversation launched with `--model sonnet` arrived expanded to the **full ID `claude-sonnet-5`** (source: the `auto-router-fallthrough` event in serve.log, `"model":"claude-sonnet-5"`). When matching aliases with `model_pattern`, include the expanded form, e.g. `(claude-)?sonnet.*`.

Operational caveats (non-routing failure modes hit during testing):

- **Task launches asynchronously**: in Claude Code 2.1.20x the Task tool starts sub-agents in the background and the orchestrator waits for a completion notification. A weak local orchestrator can mishandle this wait and *falsely report* "the agent is unreachable". If the serve log has no `auto-router-resolved`, the failure is client-side — do not mistake it for a routing failure.
- **Autocompact thrashing**: with an Ollama-backed main, `cache_read_input_tokens` is always 0, so each turn re-reports ~30k tokens of prefill as fresh input; Claude Code's auto-compact can spin every few turns and abort via the `rapid_refill_breaker`. For long orchestrations, disable auto-compact or put a prompt-cache-capable backend behind the main profile.
- **Empty grok responses**: the grok CLI was observed returning exit 0 with `text: ""`. **Fixed** in coderouter-plugin-agents as of `c6096ed` (fix(grok): treat empty 'text' in grok JSON output as retryable AdapterError, 2026-07-11) — an empty/whitespace-only `text` now raises a retryable AdapterError so the fallback chain can advance (older versions pass the empty answer through to the client as a "success").
- **Two recurring serve-log warnings are expected (not routing failures)**: (1) `normalized-nonspec-message-roles` (hint: client is likely Claude Code CLI >= 2.1.154 (known regression)) records the adapter absorbing a known Claude Code regression and is benign. (2) `capability-degraded` with dropped:["cache_control"], reason:"translation-lossy" on tool-bearing sub-agent requests records cache_control being dropped when translating to a non-tools backend, and is expected (report.md:52,60,80). Both are fine as long as a `provider-ok` follows.

The reproducible test kit (providers.yaml, sub-agent definitions, verification script) lives in the repository under `_run/e2e-agents/`.

## 7. Limitations and known gaps

- **One matcher per rule (no AND)**: a `RuleMatcher` cannot express compound conditions. "CJK-heavy AND long" isn't expressible as a single rule today (gap G1). Work around it by ordering multiple single-condition rules and letting first-match-wins do the job.
- **No dedicated sub-agent-declaration channel (under consideration)**: there is currently no channel that carries "this is a sub-agent, and its role is X." In practice, the answer is what this guide describes — route on model name. A future proposal exists (not yet started) to add a top-priority rule ahead of auto_router that detects a tag embedded in the system prompt or first message, but this is not implemented.
- **How this differs from ccr's (claude-code-router) tag approach**: CodeRouter actively reads wire-level metadata (model name, headers) to route. ccr instead passively detects and extracts a tag embedded in the prompt body. Both are different answers to the same underlying constraint — there is no native sub-agent identifier on the Anthropic wire.
- **Measured — the shape of the model name that arrives (confirmed 2026-07-11)**: sub-agent (frontmatter-derived) model names arrive **verbatim, including custom strings like `e2e-codex`, with no full-ID expansion**; the main conversation launched with `--model sonnet` arrives expanded to the **full ID `claude-sonnet-5`** (confirmed via the `auto-router-fallthrough` event in serve.log; see [§6.1](#61-measured-verification-results-coderouter-v290--claude-code-21206207-2026-07-11)). Behavior may still change in future Claude Code versions, so confirming the real value via `signals.model` as described in [§6](#6-verifying-it-works) remains recommended whenever you write a `model_pattern` rule.

## 8. Related documents

- [`docs/backends/external-agents.md`](../backends/external-agents.en.md) — full configuration reference, authentication, and troubleshooting for `agent_cli` (claude/codex/grok/antigravity)
- [`docs/guides/usage-guide.md`](./usage-guide.en.md) — general CodeRouter usage guide
- Claude Code official docs: <https://code.claude.com/docs/en/sub-agents>, <https://code.claude.com/docs/en/model-config>
