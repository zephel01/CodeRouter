# LM Studio direct backend guide (second rescue path for qwen35 / qwen35moe)

> **What this is**: a recipe for running `qwen35` (Qwen3.5 Dense) and `qwen35moe` (Qwen3.6 MoE) architecture models — which are currently brittle under Ollama or stand-alone llama.cpp builds — through LM Studio 0.4.12+'s Local Server, and connecting it to CodeRouter. Real-machine verified in CodeRouter v1.8.4 on M3 Max 64GB with Qwen3.5 9B / Qwen3.6 35B-A3B / Jackrong/Qwopus3.5-9B-v3-GGUF. Both the OpenAI-compatible (`/v1/chat/completions`) and Anthropic-compatible (`/v1/messages`) routes are documented.

> Japanese version: [`lmstudio-direct.md`](./lmstudio-direct.md) (primary).

---

## Why this is needed

Real-machine sessions in v1.8.1 → v1.8.4 surfaced three facts:

1. **`qwen35` (Qwen3.5 Dense / derivatives like Qwopus3.5)** is occasionally still unstable on stand-alone llama.cpp builds — `unable to load model: unknown model architecture: 'qwen35'` was observed during development.
2. **`qwen35moe` (Qwen3.6 35B-A3B)** runs under direct llama.cpp, but Ollama paths report `tool_calls [NEEDS TUNING]`, hard crashes, and memory-calculation bugs.
3. **LM Studio 0.4.12** landed Qwen 3.6 official support + Qwen 3.5 performance fixes + Anthropic-compatible `/v1/messages` in a single release.

Net result: **LM Studio is currently the most stable path for `qwen35` / `qwen35moe`**. Going through the Anthropic-compatible route lets CodeRouter connect with `kind: anthropic`, skipping adapter translation entirely — and **Anthropic prompt caching survives end-to-end** (`cache_read_input_tokens` observed live).

This sits next to [`docs/llamacpp-direct.md`](./llamacpp-direct.en.md) as a second canonical local-backend path.

---

## Prerequisites

- macOS (Apple Silicon, Metal GPU offload), Windows, or Linux all supported.
- Recommended hardware: M3 Max 64GB (Q4_K_M = 22GB GGUF + KV cache + headroom fits comfortably). Qwen3.5 9B (Q4_K_M, 6.5GB) runs well even on a 32GB Mac.
- Required tool: LM Studio 0.4.12+ (https://lmstudio.ai/). Pre-0.4.12 builds lack Qwen 3.6 support and the Anthropic-compatible `/v1/messages` endpoint — upgrade is mandatory.
- Time: install 5 min + GGUF download 5-10 min ≈ 10-15 min total.

---

## Steps

### Step 1. Install LM Studio and download a model

1. Download LM Studio 0.4.12+ from https://lmstudio.ai/ and install.
2. Open the **Discover** tab and pull one of these GGUFs:

   | Model | Quant | Size | Use case |
   |---|---|---|---|
   | `lmstudio-community/Qwen3.5 9B` | Q4_K_M | ~6.5 GB | Lightweight, fast load — first choice for verification |
   | `lmstudio-community/Qwen3.6 35B A3B` | Q4_K_M | ~22 GB | Larger, production candidate (M3 Max 64GB recommended) |
   | `Jackrong/Qwopus3.5-9B-v3-GGUF` | Q4_K_M | ~5.6 GB | Claude Opus distillation, `qwen35` architecture |

> **GGUF vs MLX**: at the time of writing **Qwen3.6 is GGUF-only**. The MLX backend has not yet caught up. Apple Silicon's MLX speedup is currently available on Qwen3 30B-A3B / GPT-OSS 20B / Qwen3-VL family. Qwen3.6 stays on GGUF (llama.cpp lineage) for now.

### Step 2. Load the model from the Chat tab

In LM Studio's **Chat** tab, when loading the model, set:

- **Context Length: 32768** (the default 4096 cannot hold Claude Code's system prompt — must be raised)
- **GPU Offload: max** (offload all layers to Metal / CUDA)
- **Flash Attention: ON**
- **K/V Cache Quantization: OFF** (default F16 — keep experimental quantization off for verification)

After load, send "Hello" from the Chat tab — a 1-2 second reply confirms success. Also confirm `Estimated memory` against your unified-memory ceiling (M3 Max 64GB assumed).

### Step 3. Start the local server

Open the **Developer** (formerly Local Server) tab and **Start Server** with:

- **Port: 1234** (LM Studio default — must match `base_url` in `providers.yaml`)
- **Just-in-time Model Loading: ON**
- **Cross-Origin Resource Sharing (CORS): OFF** (default — CodeRouter is on localhost so unnecessary)
- **Serve on Local Network: OFF** (default — keeps it local only)

A startup log like the following confirms readiness:

```
[2026-04-27 21:00:00] [INFO] Server starting on port 1234
[2026-04-27 21:00:00] [INFO] Server started.
```

### Step 4. Verify (no CodeRouter, just curl)

#### 4-A. Model list

```bash
curl -s http://localhost:1234/v1/models | jq '.data[] | {id, owned_by}'
```

Expected:

```json
{"id": "qwen/qwen3.5-9b",      "owned_by": "organization_owner"}
{"id": "qwen/qwen3.6-35b-a3b", "owned_by": "organization_owner"}
```

#### 4-B. OpenAI-compatible `/v1/chat/completions` with native tool_calls

```bash
curl -s http://localhost:1234/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "qwen3.5-9b",
    "messages": [{"role":"user","content":"Call the echo tool with message=\"hi\""}],
    "tools": [{
      "type": "function",
      "function": {
        "name": "echo",
        "description": "Echo a message back.",
        "parameters": {
          "type": "object",
          "properties": {"message": {"type":"string"}},
          "required": ["message"]
        }
      }
    }],
    "tool_choice": "auto",
    "max_tokens": 500,
    "temperature": 0
  }' | jq '.choices[0]'
```

Expected:

```json
{
  "index": 0,
  "message": {
    "role": "assistant",
    "content": "\n\n",
    "reasoning_content": "The user wants me to call the echo tool with...",
    "tool_calls": [{
      "type": "function",
      "id": "573125855",
      "function": {"name": "echo", "arguments": "{\"message\":\"hi\"}"}
    }]
  },
  "finish_reason": "tool_calls"
}
```

✅ `finish_reason: "tool_calls"` plus a real OpenAI `tool_calls[]` array. The `reasoning_content` field is stripped by CodeRouter v1.8.3+'s adapter so it never reaches the client.

#### 4-C. Anthropic-compatible `/v1/messages` with native tool_use (the LM Studio 0.4.12 highlight)

```bash
curl -s http://localhost:1234/v1/messages \
  -H 'Content-Type: application/json' \
  -H 'anthropic-version: 2023-06-01' \
  -d '{
    "model": "qwen3.5-9b",
    "max_tokens": 500,
    "messages": [{"role":"user","content":"Call the echo tool with message=\"hi\""}],
    "tools": [{
      "name": "echo",
      "description": "Echo a message back.",
      "input_schema": {
        "type": "object",
        "properties": {"message": {"type":"string"}},
        "required": ["message"]
      }
    }]
  }' | jq '.'
```

Expected:

```json
{
  "id": "msg_4w8746j0jnngapmqvl0zi",
  "type": "message",
  "role": "assistant",
  "content": [
    {"type": "text", "text": "\n\n"},
    {"type": "tool_use", "id": "998041094", "name": "echo", "input": {"message": "hi"}}
  ],
  "model": "qwen3.5-9b",
  "stop_reason": "tool_use",
  "usage": {"input_tokens": 284, "output_tokens": 59, "cache_read_input_tokens": 0}
}
```

✅ `stop_reason: "tool_use"` plus an Anthropic-native `tool_use` block. The `cache_read_input_tokens` field is present, which means CodeRouter can connect via `kind: anthropic` directly.

### Step 5. Register the providers in CodeRouter

Add both routes side by side to `~/.coderouter/providers.yaml`'s `providers:` list (the same pattern is bundled in `examples/providers.yaml`):

```yaml
# OpenAI-compatible route (translation through CodeRouter's adapter)
- name: lmstudio-qwen3-5-9b
  kind: openai_compat
  base_url: http://localhost:1234/v1
  model: qwen3.5-9b              # LM Studio fuzzy-matches (qwen/qwen3.5-9b also works)
  paid: false
  api_key_env: null               # LM Studio default: no auth
  timeout_s: 120
  capabilities:
    chat: true
    streaming: true
    tools: true                   # native tool_calls confirmed
    thinking: true                # thinking model — doctor uses 1024-token budget
  output_filters:
    - strip_thinking              # belt-and-suspenders: strip `<think>...</think>`

# Anthropic-compatible route (the v1.8.4 highlight, zero adapter translation)
- name: lmstudio-qwen3-5-9b-anthropic
  kind: anthropic                 # ← the key part: skip the OpenAI shim
  base_url: http://localhost:1234  # `/v1/messages` is served by LM Studio
  model: qwen3.5-9b
  paid: false
  api_key_env: null
  timeout_s: 120
  capabilities:
    chat: true
    streaming: true
    tools: true
    thinking: true

# (For 35B-A3B, copy the two stanzas above and adjust `model` and `timeout_s`.)
```

Add a profile (verification setup):

```yaml
profiles:
  # OpenAI-compatible route (CodeRouter adapter translates)
  - name: test-lmstudio-openai
    providers:
      - lmstudio-qwen3-5-9b           # lightweight, fast load
      - lmstudio-qwen3-6-35b-a3b      # larger, production candidate
      - openrouter-free               # safety net

  # Anthropic-compatible route (zero translation)
  - name: test-lmstudio-anthropic
    providers:
      - lmstudio-qwen3-5-9b-anthropic
      - lmstudio-qwen3-6-35b-a3b-anthropic
      - openrouter-free
```

### Step 6. Verify with doctor

#### OpenAI-compatible route

```bash
coderouter doctor --check-model lmstudio-qwen3-5-9b
```

Expected:

```
[1/6] auth+basic-chat …… [OK]    200 OK (in=19, out=16)
[2/6] num_ctx ………………… [SKIP]    not Ollama-shape (port 1234) — by design
[3/6] tool_calls ………… [OK]    native `tool_calls` observed; matches declaration.
[4/6] thinking ………… [SKIP]    capabilities.thinking=true on openai_compat is informational
[5/6] reasoning-leak …… [OK]   upstream emits non-standard `reasoning_content`; stripped by v1.8.3 adapter
[6/6] streaming ………… [SKIP]    streaming-path detection is Ollama-shape gated

Summary: all probes match declarations.
Exit: 0
```

#### Anthropic-compatible route

```bash
coderouter doctor --check-model lmstudio-qwen3-5-9b-anthropic
```

Expected:

```
[1/6] auth+basic-chat …… [OK]    200 OK (in=19, out=16)
[2/6] num_ctx ………………… [SKIP]    not Ollama-shape
[3/6] tool_calls ………… [OK]    native `tool_calls` observed; matches declaration.
[4/6] thinking ………… [OK]    thinking block emitted; matches declaration.   ← !!
[5/6] reasoning-leak …… [SKIP]    only openai_compat emits the non-standard reasoning field
[6/6] streaming ………… [SKIP]

Summary: all probes match declarations.
Exit: 0
```

★ The Anthropic-compatible route shows `thinking [OK]`. Opt-in (`thinking: {type: enabled}`) in the request body returns thinking content blocks in Anthropic-native shape — fully compatible with the official Anthropic API (Claude Sonnet 4.5+).

### Step 7. End-to-end through CodeRouter (Anthropic-compatible)

```bash
coderouter serve --port 8088 --mode test-lmstudio-anthropic &
sleep 2

curl -s -X POST http://localhost:8088/v1/messages \
  -H 'Content-Type: application/json' \
  -H 'anthropic-version: 2023-06-01' \
  -H 'x-api-key: dummy' \
  -d '{
    "model": "claude-3-5-sonnet-20241022",
    "max_tokens": 500,
    "messages": [{"role":"user","content":"Call the echo tool with message=\"hi\""}],
    "tools": [{
      "name": "echo",
      "description": "Echo a message back.",
      "input_schema": {
        "type": "object",
        "properties": {"message": {"type":"string"}},
        "required": ["message"]
      }
    }]
  }' | jq '.'
```

Expected:

```json
{
  "id": "msg_pgpz5kcbudsu61h77z3yn",
  "type": "message",
  "role": "assistant",
  "model": "qwen3.5-9b",
  "content": [
    {"type": "text", "text": "\n\n"},
    {"type": "tool_use", "id": "311305849", "name": "echo", "input": {"message": "hi"}}
  ],
  "stop_reason": "tool_use",
  "usage": {
    "input_tokens": 284,
    "output_tokens": 59,
    "cache_read_input_tokens": 280   ← !!! prompt caching survived
  },
  "coderouter_provider": "lmstudio-qwen3-5-9b-anthropic"
}
```

CodeRouter log:

```
try-provider provider=lmstudio-qwen3-5-9b-anthropic stream=false native_anthropic=true
HTTP Request: POST http://localhost:1234/v1/messages "HTTP/1.1 200 OK"
provider-ok provider=lmstudio-qwen3-5-9b-anthropic stream=false native_anthropic=true
```

No `capability-degraded` log lines (Anthropic-native shape has nothing to strip).

★ `cache_read_input_tokens: 280` is the headline: **Anthropic prompt caching is alive for a local LLM (LM Studio)**. Claude Code's typical behavior — sending the same prompt prefix repeatedly across a session — triggers cache hits on the second call onward, skipping upstream prompt-token processing.

---

## Troubleshooting

### `unable to load model: unknown model architecture: 'qwen35'`

- Likely on LM Studio < 0.4.12. **Upgrade to 0.4.12+ (mandatory).**
- If still failing, the [llama.cpp direct path](./llamacpp-direct.en.md) is an alternative.

### Context still 4096 (Claude Code system prompt overflows)

- Re-load the model with **Context Length: 32768** in the Chat tab. Already-loaded models do not pick up dynamic changes — eject and re-load.

### `tool_calls [NEEDS TUNING]` (CodeRouter ≤ v1.8.2)

- Pre-v1.8.3, the `tool_calls` probe used `max_tokens=64` which got consumed by `reasoning_content` on thinking models, producing false positives.
- **Upgrade to v1.8.3+**: `uv tool upgrade coderouter-cli`.

### `reasoning_content` field leaking to the client (CodeRouter ≤ v1.8.2)

- Pre-v1.8.3, the adapter stripped only `reasoning` (Ollama naming) but not `reasoning_content` (LM Studio / llama.cpp naming).
- **Upgrade to v1.8.3+.**

### Anthropic-compatible `/v1/messages` returns 404

- LM Studio < 0.4.12 lacks this endpoint. **Upgrade to 0.4.12+.**
- Verify the server is running in the LM Studio Developer tab.

### Server fails to start / port 1234 in use

- Pick an unused port in the Developer tab (e.g. 1235) and update `providers.yaml`'s `base_url`.

### Memory pressure (`Estimated memory > unified memory` on Mac)

- Drop to a smaller quant (Q3_K_M / IQ3_XS) if you want to keep full GPU offload.
- Or change **GPU Offload: max → partial** so some layers run on CPU.

### Different port

- Change **Port** in the Developer tab and update `providers.yaml`'s `base_url` to match.

---

## Related docs

- [llamacpp-direct.en.md](./llamacpp-direct.en.md) — llama.cpp direct path (CLI build + Unsloth GGUF), Qwen3.6 35B-A3B verified.
- [troubleshooting.md §4-2](./troubleshooting.md) — known issues across Ollama / llama.cpp / LM Studio.
- [hf-ollama-models.md](./hf-ollama-models.md) — pulling HF models through Ollama.
- [examples/providers.yaml](../examples/providers.yaml) — bundled `lmstudio-*` provider stanzas + `test-lmstudio-openai` / `test-lmstudio-anthropic` profiles.
- Real-machine narrative: [v1.8.4 note (JP)](./articles/note-v1-8-4-lmstudio-revival.md) — the day a "framework wait" verdict expired in 24 hours.

---

## Choosing between this guide and `llamacpp-direct.md`

| Aspect | LM Studio (this guide) | llama.cpp direct (`llamacpp-direct.md`) |
|---|---|---|
| Install | GUI installer | git clone + cmake build |
| Model fetch | GUI Discover tab | `huggingface-cli download` |
| Switching models | Load / Eject in GUI | restart the server process |
| `qwen35` (Qwen3.5 Dense) | ✅ Officially supported in 0.4.12+ | ⚠️ Sometimes unsupported in upstream |
| `qwen35moe` (Qwen3.6 MoE) | ✅ Officially supported in 0.4.12+ | ✅ Supported |
| Anthropic-compatible `/v1/messages` | ✅ Official in 0.4.12+ | ❌ Not provided (CodeRouter must translate OpenAI→Anthropic) |
| Anthropic prompt caching | ✅ `cache_read_input_tokens` observed | ❌ Not possible via openai_compat |
| MoE active-params display | Detailed in GUI | Inspect the server log |
| Recommended for | GUI users / when Anthropic compatibility matters | CLI users / when build control matters |

The two paths are not exclusive — register both `lmstudio-*` and `llamacpp-*` providers in `providers.yaml` and use a profile chain to control fallback order.
