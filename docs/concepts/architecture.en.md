# CodeRouter Architecture Details

> **See the README for the overview → [README.en.md](../../README.en.md)**
> This page explains the internal structure, configuration reference, and how the features work, with diagrams.

日本語版: [`architecture.md`](./architecture.md)

Last updated: v2.5.0 (2026-05-22)

> **Note (added 2026-08-10)**: This diagram and text cover the core structure as of v2.5.0; they have not been rewritten to track later changes. The main layers/features added since v2.5.0:
> - Language Tax measurement, routing, and visualization (v2.6.0) → [`docs/guides/language-tax.md`](../guides/language-tax.md)
> - Token-savings accounting (v2.6.1)
> - agent_cli (external coding-agent CLI integration) moved from an in-core adapter to the external plugin `coderouter-plugin-agents` in v2.9.0, and was **removed from Core** → [`docs/backends/external-agents.md`](../backends/external-agents.md)
> - Launcher model swap (v2.9.1) and backend variants (v2.11.0) → [`docs/backends/launcher.md`](../backends/launcher.md)
> - context-budget guard token-estimation fix (v2.12.0)
> - Credential hygiene: `credential.source: cli_session`, log secret scrubbing, `CODEROUTER_METRICS_TOKEN` (v2.14.0) → [`docs/guides/security.md`](../guides/security.md)
>
> See [`CHANGELOG.md`](../../CHANGELOG.md) for the current full picture.

---

## Big picture — 3-layer fallback + 6-family failure guards

```
┌──────────────────────────────────────────────────────────────┐
│  Claude Code / gemini-cli / codex / OpenAI SDK / curl        │
│  (Anthropic wire /v1/messages  or  OpenAI wire /v1/chat/…)   │
└──────────────────┬───────────────────────────────────────────┘
                   │
                   ▼
┌──────────────────────────────────────────────────────────────┐
│                      CodeRouter                              │
│                                                              │
│  ┌─────────────┐  ┌──────────────┐  ┌────────────────────┐  │
│  │ Wire xlate  │  │ 6-family Grd │  │ Diagnostic Layer   │  │
│  │             │  │              │  │                    │  │
│  │ Anthropic   │  │ L1 Context   │  │ doctor 7-probe     │  │
│  │   ↕         │  │ L2 Memory    │  │ continuous probe   │  │
│  │ OpenAI      │  │ L3 Tool loop │  │ audit log          │  │
│  │             │  │ L4 Drift     │  │ request journal    │  │
│  │ tool-call   │  │ L5 Health    │  │ replay A/B         │  │
│  │ repair      │  │ L6 Mid-strm  │  │ /dashboard         │  │
│  │             │  └──────────────┘  │ /launcher          │  │
│  └─────────────┘                    └────────────────────┘  │
│                                                              │
│  ┌──────────────────────────────────────────────────────────┐│
│  │          Fallback Engine (profile → chain)               ││
│  │  ① Local (Ollama/llama.cpp/LM Studio) ─ free, top prio  ││
│  │  ② Free Cloud (OpenRouter free / NVIDIA NIM) ─ free tier ││
│  │  ③ Paid Cloud (Claude / GPT) ─ ALLOW_PAID=true only     ││
│  └──────────────────────────────────────────────────────────┘│
│                                                              │
│  ┌──────────────────────────────────────────────────────────┐│
│  │          Persistent Layer (v2.0-K)                       ││
│  │  StateStore (sqlite3) │ AuditLog (JSONL) │ RequestLog    ││
│  └──────────────────────────────────────────────────────────┘│
└──────────────────────────────────────────────────────────────┘
                   │
      ┌────────────┼────────────┐
      ▼            ▼            ▼
  ┌────────┐  ┌─────────┐  ┌──────────┐
  │ Ollama │  │OpenRouter│  │ Claude   │
  │llama.cp│  │NVIDIA NIM│  │ GPT      │
  │LMStudio│  │(free)    │  │(paid)    │
  └────────┘  └─────────┘  └──────────┘
```

---

## The 6 failure families and how they're handled

Failures that occur in long (8h+) agent sessions, systematized and handled at each layer:

| Failure | Symptom | CodeRouter's handling | Introduced in |
|---|---|---|---|
| **L1 Context overflow** | messages asymptote toward the context window → backend 400 | warn (80%) → auto trim (90%), atomic preservation of tool_use/tool_result pairs | v2.0.0 |
| **L2 Memory pressure** | Ollama/LM Studio OOM from insufficient VRAM | detect the OOM string in the error body (warn / skip, default warn). On `skip`, exclude the provider via cooldown (default 120s) → fall through to the next provider in the chain | v1.10.0 |
| **L3 Tool loop** | stuck loop repeating the same tool args | duplicate detection → 3-stage warn / inject / break | v1.10.0 |
| **L4 Drift** | response-quality degradation from KV cache pollution (empty responses/shortening/tool silence) | 6-signal rolling window → warn / promote / reload (default off) | v2.1.0 |
| **L5 Health** | backend crash / consecutive failures | state machine (HEALTHY→DEGRADED→UNHEALTHY) + self-healing (auto-exclude + restart + recovery probe) | v1.10.0 + v2.2.0 |
| **L6 Mid-stream** | backend dies mid-stream | return accumulated text (partial stitching) + clean error event | v2.1.0 |

---

## Choosing between `kind: openai_compat` and `kind: anthropic`

Each provider in `providers.yaml` has a `kind`. Which you choose changes the survival scope of wire-level features:

| Aspect | `kind: openai_compat` | `kind: anthropic` |
|---|---|---|
| Reachable from `/v1/chat/completions` | ✅ no conversion | ✅ via reverse conversion |
| Reachable from `/v1/messages` | ✅ via conversion + tool-call repair | ✅ native passthrough |
| Targets | Ollama, llama.cpp, OpenRouter, LM Studio, Groq, ... | `api.anthropic.com`, Bedrock Anthropic shim |
| `cache_control` / `thinking` | ❌ lost (no OpenAI equivalent) | ✅ preserved end-to-end |
| tool-call repair | ✅ for local models that emit broken JSON | n/a |

**Rules of thumb:**

- **Local models / OpenRouter free tier** → `kind: openai_compat`
- **Official Claude API + `cache_control` / `thinking`** → `kind: anthropic`
- **Mixed chain** (local first + Claude as last resort) → put both kinds in the same profile

---

## Profiles and routing

### Basic structure

```yaml
# providers.yaml
default_profile: claude-code

profiles:
  - name: claude-code
    providers:
      - ollama-qwen-coder-7b         # ① local (fastest)
      - ollama-qwen-coder-14b        # ② quality fallback
      - openrouter-free              # ③ free cloud
      - openrouter-claude            # ④ paid (only when ALLOW_PAID=true)

providers:
  - name: ollama-qwen-coder-7b
    kind: openai_compat
    base_url: http://localhost:11434/v1
    model: qwen3-coder:7b
```

### Per-profile parameter overrides

Give the same provider list different behavior in another profile:

```yaml
profiles:
  - name: claude-code-long
    timeout_s: 600             # widen the timeout in this profile
    append_system_prompt: ""   # explicitly clear the provider instruction
    providers:
      - ollama-qwen-coder-14b
      - openrouter-free
```

### Mode aliases

The client specifies **intent**:

```yaml
mode_aliases:
  coding: claude-code
  long:   claude-code-long
  fast:   ollama-only
```

```bash
curl http://localhost:8088/v1/chat/completions \
  -H 'X-CodeRouter-Mode: coding' \
  -d '{"messages": [{"role":"user","content":"hi"}]}'
```

**Priority** (first match wins): body `profile` > `X-CodeRouter-Profile` > `X-CodeRouter-Mode` > `default_profile`

---

## Doctor — provider diagnostics

```bash
coderouter doctor --check-model ollama-qwen-coder-14b
```

Runs 7 probes (each ≤100 tokens) against the given provider and outputs any mismatch between declared and actual behavior as a copy-pasteable YAML patch:

```
provider: ollama-qwen-coder-14b  (kind=openai_compat, model=qwen2.5-coder:14b)

probe                     verdict        detail
auth+basic-chat           OK             200 in 1.4s, 18 tokens in / 6 tokens out
tool_calls                NEEDS_TUNING   model emitted a tool_use block but registry says tools=false
thinking                  N/A            kind=openai_compat; thinking probe is anthropic-only
reasoning-leak            OK             no stray `reasoning` field on choice.message

suggested patch for ~/.coderouter-t/providers.yaml:
  providers:
    - name: ollama-qwen-coder-14b
      capabilities:
        tools: true
```

### Roles of the 7 probes

| Probe | What it checks |
|---|---|
| **auth+basic-chat** | reachability, auth, basic response. On failure, all remaining probes SKIP |
| **num_ctx** | mismatch between declared context length and actual behavior (detects implicit truncation on large inputs) |
| **tool_calls** | ability to emit tool_use with a dummy tool |
| **thinking** | acceptance of the Anthropic `thinking` block (anthropic kind only) |
| **reasoning-leak** | whether a `reasoning` field leaks in the raw body before stripping |
| **streaming** | consistency of SSE streaming responses |
| **cache** | Anthropic prompt cache read/creation behavior |

### Exit codes (intended for CI)

| Code | Meaning |
|---|---|
| `0` | all probes match, no patch needed |
| `2` | `NEEDS_TUNING` present, YAML patch is in the output |
| `1` | probe could not run (auth failure / unreachable) |

`--apply` writes the YAML patch back non-destructively (ruamel.yaml round-trip, preserving comments and key order). `--dry-run` previews a unified diff.

---

## Model capability registry

Bundled with the package at `coderouter/data/model-capabilities.yaml`. User overrides go in `~/.coderouter-t/model-capabilities.yaml`:

```yaml
version: 1
rules:
  - match: "qwen3-coder:*"
    kind: openai_compat
    capabilities:
      tools: true
      max_context_tokens: 32768
```

**Priority**: `providers.yaml` capabilities > user YAML > bundled YAML > unset (False)

---

## v2.2.0 new features: Self-healing + persistence + Replay

### Self-healing routing (v2.0-J)

Automatically excludes UNHEALTHY providers and restores them via a restart helper + recovery probe:

```
   HEALTHY ──(consecutive fails)──→ UNHEALTHY ──(exclude)──→ EXCLUDED
                                                      │
                              ┌─(restart command)─────┤
                              │                       │
                              │  ┌─(recovery probe)───┤
                              │  │ 30s → 60s → 120s → 300s (exponential backoff)
                              │  │                    │
                              │  └─(probe success)──→ RESTORED (back to original position)
                              │
                              └─(restart success + probe success)──→ RESTORED
```

```yaml
profiles:
  - name: self-healing
    backend_health_action: exclude    # UNHEALTHY → exclude + self-heal
    backend_health_threshold: 3       # consecutive failure count

providers:
  - name: ollama-qwen3
    restart_command: "ollama serve"   # auto-restart command
```

### Persistent layer (v2.0-K)

```yaml
state_dir: "~/.coderouter-t/state/"    # sqlite3 KV store + JSONL logs
audit_log: active                     # records 22 event types as JSONL
request_log: active                   # per-request metadata journal
```

- **StateStore**: sqlite3 KV (WAL mode) that retains budget/health/self-healing state across restarts
- **Audit log**: records guard firings, chain fallbacks, self-healing, etc. as JSONL. `coderouter audit --tail 20`
- **Request journal**: records metadata for cache-observed events (provider, tokens, cost) as JSONL. Bodies are not recorded = privacy safe

### Replay — statistical A/B comparison

Confirm the effect of a provider switch with numbers:

```bash
coderouter replay --compare anthropic-api openrouter-free --since 2026-05-01
```

```
Metric                    anthropic-api        openrouter-free           Delta
─────────────────────────────────────────────────────────────────────────────────
Requests                  150                  89                          -61
Avg input tokens          1234                 1180                        -54
Avg cost (USD)            $0.0082              $0.0000                 -0.0082
Total cost (USD)          $1.2300              $0.0000                 -1.2300
Cache hit ratio           42.3%                0.0%                     -42.3
Streaming ratio           85.0%                100.0%                   +15.0

Per-request: openrouter-free is 100.0% cheaper than anthropic-api
```

---

## Using it with Claude Code

```bash
# terminal 1: start CodeRouter
coderouter-t serve --port 8088

# terminal 2: point Claude Code at it
ANTHROPIC_BASE_URL=http://localhost:8088 \
ANTHROPIC_AUTH_TOKEN=dummy \
claude
```

### What to expect

- **Time to first byte** ≈ the upstream's total response time (CodeRouter's overhead is negligible)
- **Apple Silicon macOS**: ~30-60s/turn with 7b, ~2 min with 14b (mainly because of Claude Code's 15-20K-token system prompt)
- **Tool-selection quality** is limited by the model — CodeRouter only does wire repair
- **Mid-stream failure** is reported as a single clean `event: error`

---

## Dependency policy

Only 5 runtime dependencies:

| Package | Purpose |
|---|---|
| `fastapi` | HTTP ingress |
| `uvicorn` | ASGI server |
| `httpx` | outbound HTTP |
| `pydantic` | schema validation |
| `pyyaml` | config parsing |

No `litellm`, no `langchain`, no `openai`/`anthropic` SDK.

---

## Catching exceptions programmatically

```python
from coderouter import CodeRouterError

try:
    response = await engine.generate(chat_request)
except CodeRouterError as exc:
    # covers AdapterError / NoProvidersAvailableError / MidStreamError
    logger.error("coderouter-failed", extra={"reason": str(exc)})
```

---

## Launcher — llama.cpp / vllm process management (v2.5.0)

A browser UI opened at `/launcher`. Start and manage llama.cpp or vllm without the command line.

### Structure

```
coderouter/ingress/launcher_routes.py
  ├── LauncherRegistry  (app.state.launcher)
  │     └── ManagedProcess  × N processes
  ├── API  /api/launcher/*
  └── UI   GET /launcher  (HTML + inline JS)
```

### Process lifecycle

```
POST /api/launcher/start
  → asyncio.create_subprocess_exec (llama-server / python -m vllm…)
  → _tail_logs() background task (stdout+stderr → deque[200])
  → ManagedProcess.status = "running"
        │
        ├─ POST /api/launcher/stop/{id}
        │      → SIGTERM → (5s) → SIGKILL
        │      → status = "stopped"
        │
        └─ process exits on its own
               → status = "stopped" or "error"  (depending on returncode)

DELETE /api/launcher/processes/{id}  ← only "stopped" processes can be deleted
```

### YAML config reference

```yaml
launcher:
  # directories to scan (recursively searches for .gguf / .safetensors / .bin / .pt / .ggml)
  model_dirs:
    - ~/models
    - /data/gguf

  # per-backend option presets
  # key name = backend name ("llama.cpp" / "vllm")
  option_profiles:
    llama.cpp:
      - name: "Full GPU use"
        args:
          "-ngl": 99           # int → passed as "--flag value"
          "--ctx-size": 4096
          "--no-mmap": false   # bool false → flag omitted
          "--mlock": true      # bool true  → "--mlock" only (no value)
    vllm:
      - name: "Standard"
        args:
          "--dtype": "auto"
          "--max-model-len": 4096
```

Type-conversion rules for `args`:

| YAML value | CLI output |
|---|---|
| `"-ngl": 99` | `-ngl 99` |
| `"--mlock": true` | `--mlock` (no value) |
| `"--no-mmap": false` | omitted |
| `"--dtype": "auto"` | `--dtype auto` |

### Extra options (free input)

The UI always shows an "extra options" text field. You can specify flags not defined in a profile on the spot. It is parsed with `shlex.split()`, so quote paths containing spaces. Re-specifying the model via `-m` / `--model` is rejected with a 400 (the model is specified only in the "model path" field).

### Persistence of the process registry

Intentionally **non-persistent**. The process registry is empty when CodeRouter restarts (to prevent multiple launches competing for GPU memory). The running llama-server / vllm processes themselves continue at the OS level, but they become invisible from the Launcher UI.

Details → [Launcher guide](../backends/launcher.md)
