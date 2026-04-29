# llama.cpp direct backend guide (rescue path for Qwen3.6 from Ollama failures)

> **What this is**: a recipe for running models that fail under Ollama (most notably Qwen3.6:35b-a3b) by going directly through `llama.cpp`'s `llama-server` and connecting it to CodeRouter. Real-machine verified in CodeRouter v1.8.3 on M3 Max 64GB with the Unsloth UD-Q4_K_M GGUF — native `tool_calls` work cleanly.

> Japanese version: [`llamacpp-direct.md`](./llamacpp-direct.md) (primary).

---

## Why this is needed

Real-machine sessions in v1.8.1 → v1.8.3, plus community reports on X / Reddit / r/ollama / r/LocalLLaMA, both confirm that **Qwen3.6 over Ollama is currently brittle**:

- `tool_calls [NEEDS TUNING]` (Ollama's chat template / tool spec is immature; the model returns neither native tool_calls nor repairable JSON)
- Hard crashes / reboots (Mac Metal, multiple reports)
- "Available memory" calculation bugs (despite plenty of RAM)
- Variants like `qwen3.6:35b-a3b-coding-nvfp4` 500-error on the MLX backend

**The model itself is healthy** — the issue is the framework layer (Ollama). Unsloth's published GGUFs run cleanly under llama.cpp's own `llama-server`, which produces native `tool_calls`. This is the most stable path today.

---

## Prerequisites

- macOS (Apple Silicon, Metal GPU offload). Linux + CUDA also works — swap `-DGGML_METAL=ON` for `-DGGML_CUDA=ON`.
- Recommended hardware: M3 Max 64GB (Q4_K_M = 22GB GGUF + KV cache + headroom fits comfortably). 32GB Macs work with less margin.
- Required tools: `git`, `cmake`, `huggingface-cli` (`uv tool install huggingface_hub[cli]`).
- Time: build 5-10 min + GGUF download 5-10 min ≈ 15-20 min total.

---

## Steps

### Step 1. Build llama.cpp

```bash
git clone https://github.com/ggml-org/llama.cpp ~/llama.cpp
cd ~/llama.cpp
cmake -B build -DGGML_METAL=ON -DLLAMA_CURL=ON
cmake --build build --config Release -j
```

Verify with `./build/bin/llama-server --version`.

> **Linux + CUDA**: replace with `cmake -B build -DGGML_CUDA=ON -DLLAMA_CURL=ON`. Requires the NVIDIA driver and CUDA toolkit.

### Step 2. Download the Unsloth GGUF

```bash
# huggingface-cli, if missing
uv tool install huggingface_hub[cli]

# Q4_K_M variant (UD = Unsloth Dynamic Quantization, ~22 GB)
huggingface-cli download unsloth/Qwen3.6-35B-A3B-GGUF \
  --include "*UD-Q4_K_M*" "*tokenizer*" "*chat_template*" \
  --local-dir ~/models/qwen3.6-35b-a3b-unsloth
```

> **Why UD-Q4_K_M?** Unsloth Dynamic Quantization — better accuracy at the same GGUF size than vanilla Q4_K_M. The de facto community standard per the Reddit / X reports.
>
> **More headroom?** Add `*UD-Q5_K_M*` etc. M3 Max 64GB handles Q5_K_M (~26GB) easily.

### Step 3. Launch llama-server

```bash
~/llama.cpp/build/bin/llama-server \
  --model ~/models/qwen3.6-35b-a3b-unsloth/Qwen3.6-35B-A3B-UD-Q4_K_M.gguf \
  --port 8080 \
  --ctx-size 32768 \
  --n-predict 4096 \
  --jinja \
  --threads 8 \
  -ngl 999 \
  --host 127.0.0.1
```

- `--port 8080`: where CodeRouter connects.
- `--ctx-size 32768`: leaves headroom for Claude Code's 15-20K-token system prompt.
- `--n-predict 4096`: per-call generation cap.
- `--jinja`: use the chat template embedded in GGUF metadata (Unsloth GGUFs ship one).
- `-ngl 999`: offload all layers to the Metal GPU.
- `--host 127.0.0.1`: bind to localhost only.

Look for `chat template: ... qwen3 ...`, `tool calling: enabled`, and `HTTP server listening on 127.0.0.1:8080` in the launch log.

### Step 4. Smoke test (no CodeRouter yet)

```bash
# 4-A. basic chat — use a generous max_tokens because reasoning eats some
curl -s http://localhost:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "qwen3.6",
    "messages": [{"role":"user","content":"Say hello in one word."}],
    "max_tokens": 500
  }' | jq '.choices[0].message'
# Expected: {"role":"assistant","content":"Hello","reasoning_content":"..."}

# 4-B. tool_calls — the actual point
curl -s http://localhost:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "qwen3.6",
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
    "max_tokens": 500
  }' | jq '.choices[0]'
# Expected: finish_reason="tool_calls" with a populated tool_calls[] array.
```

### Step 5. Register the provider in CodeRouter

Add this stanza to the `providers:` list in `~/.coderouter/providers.yaml`:

```yaml
- name: llamacpp-qwen3-6-35b-a3b
  kind: openai_compat
  base_url: http://localhost:8080/v1
  model: qwen3.6                # llama-server accepts any model id
  paid: false
  api_key_env: null              # llama-server is auth-less by default
  timeout_s: 120                 # generous — thinking model
  capabilities:
    chat: true
    streaming: true
    tools: true                  # native tool_calls verified
    thinking: true               # thinking model — doctor uses 1024-token probe budget
  output_filters:
    - strip_thinking             # strip stray <think>...</think> blocks
  # llama.cpp does not honor extra_body.options (Ollama-only); context /
  # max_tokens are pinned via launcher flags (--ctx-size / --n-predict).
```

Add to your `coding` profile chain as the primary candidate:

```yaml
profiles:
  - name: coding
    providers:
      - llamacpp-qwen3-6-35b-a3b   # top tier (requires the prep above)
      - ollama-qwen-coder-14b      # zero-prep fallback
      - ollama-gemma4-26b          # verified working
      ...
```

> The shipped `examples/providers.yaml` includes a fully-commented `llamacpp-qwen3-6-35b-a3b` entry. You can `cp examples/providers.yaml ~/.coderouter/providers.yaml` to start from there.

### Step 6. Doctor verification

```bash
coderouter doctor --check-model llamacpp-qwen3-6-35b-a3b
```

Expected:

```
[1/6] auth+basic-chat …… [OK]
[2/6] num_ctx ………………… [SKIP]    # port 8080 → not Ollama-shape, by design
[3/6] tool_calls ………… [OK]    # native tool_calls observed
[4/6] thinking ………… [SKIP]
[5/6] reasoning-leak …… [OK]    # reasoning_content detected, adapter strips it
[6/6] streaming ………… [SKIP]

Summary: all probes match declarations.
Exit: 0
```

`tool_calls [OK]` is the key win.

### Step 7. End-to-end via CodeRouter (Anthropic-compat)

```bash
coderouter serve --port 8088 --mode test-llamacpp &
sleep 2

curl -s -X POST http://localhost:8088/v1/messages \
  -H 'Content-Type: application/json' \
  -H 'anthropic-version: 2023-06-01' \
  -H 'x-api-key: dummy' \
  -d '{
    "model": "claude-3-5-sonnet-20241022",
    "max_tokens": 500,
    "messages": [{"role":"user","content":"Say hello in one word."}]
  }' | jq '.content'
# Expected: [{"type":"text","text":"Hello"}]
```

In the CodeRouter logs you should see `dropped=["reasoning", "reasoning_content"]` — that's the v1.8.3 strip in action for the llama.cpp-flavored field name.

---

## Troubleshooting

- **`tool_calls [NEEDS TUNING]` on CodeRouter ≤ v1.8.2** — the pre-v1.8.3 probe budget was eaten by `reasoning_content` before any tool_calls could surface. Upgrade: `uv tool upgrade coderouter-cli`.
- **`reasoning_content` leaks to the client (≤ v1.8.2)** — the older adapter only stripped Ollama's `reasoning`. Upgrade.
- **`chat template not found` on launch** — the GGUF lacks an embedded template. Either use Unsloth's GGUF (which ships one), or pass `--chat-template-file path/to/qwen3.jinja` explicitly.
- **Metal not engaging despite `-ngl 999`** — rebuild with `cmake -B build -DGGML_METAL=ON --fresh`. `./build/bin/llama-server --version` should report `Metal: yes`.
- **Different port** — change `--port` and `base_url` together.
- **CUDA build** — `cmake -B build -DGGML_CUDA=ON -DLLAMA_CURL=ON --fresh`; drop `-DGGML_METAL=ON` (Apple Silicon only).

---

## Related docs

- [`troubleshooting.md` §4-2-A](./troubleshooting.md#4-2-a-qwen36) — known Qwen3.6 + Ollama pitfalls.
- [`hf-ollama-models.md`](./hf-ollama-models.md) — pulling HF models through Ollama.
- [`examples/providers.yaml`](../examples/providers.yaml) — bundled `llamacpp-qwen3-6-35b-a3b` provider example.
- The session that produced this guide: [v1.8.1 note](./articles/v1-saga/note-1-v1-8-1-reality-check.md) → [v1.8.2 note](./articles/v1-saga/note-2-v1-8-2-doctor-self-diagnosis.md) → [v1.8.3 note](./articles/v1-saga/note-3-v1-8-3-llamacpp-rescue.md).

---

## The same recipe works for other models

`llama-server` runs almost any GGUF with the same flags above; `--jinja` picks up an embedded chat template if present. Unsloth publishes Dynamic Quantization GGUFs for many models, so this path generalizes:

- `unsloth/Llama-3.3-70B-Instruct-GGUF`
- `unsloth/gemma4-26b-GGUF`
- `unsloth/gpt-oss-120B-GGUF`
- … and so on.

Naming the provider `llamacpp-<model>` keeps things consistent with the conventions in `examples/providers.yaml`.
