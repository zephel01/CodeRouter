# Claude Code × local LLM (llama.cpp / vLLM) connection guide — Ollama-free setup and troubleshooting

> 日本語版: [`claude-code-llamacpp-vllm.md`](./claude-code-llamacpp-vllm.md)

A practical guide for running a downloaded `.gguf` with **llama.cpp**, or running **vLLM**, without Ollama, and connecting to Claude Code through CodeRouter.
It covers how to build the configuration, and how to fix the errors you're most likely to hit in practice (400 / connection refused / 502 / connector disabled / `n_ctx=4096` staying stuck).

> Related: [Launcher Guide](./launcher.en.md) / [Backend Installation Guide](./install-backends.en.md) / [examples/README](../../examples/README.md)

---

## Overall setup

```
Claude Code ──(ANTHROPIC_BASE_URL)──► CodeRouter (:8088) ──► ① llama.cpp (:8080)
                                                          ├─► ② vLLM (:8000)
                                                          └─► ③ free cloud (fallback)
```

All CodeRouter needs is "a backend that speaks the OpenAI-compatible API." Ollama is just one option among several — llama.cpp / vLLM work equally well.

---

## Setup steps

### 1. Start the backend (no Ollama)

**llama.cpp (using a downloaded GGUF)**

```bash
brew install llama.cpp           # macOS / Linux (Windows: winget install ggml.llamacpp)

llama-server -m ~/models/<model>.gguf --host 127.0.0.1 --port 8080 \
  -ngl 99 --ctx-size 32768
# Always check "n_ctx = 32768" in the startup log
```

**vLLM (NVIDIA GPU)**

```bash
uv venv ~/.coderouter-t/backends/vllm
~/.coderouter-t/backends/vllm/bin/python -m pip install vllm
~/.coderouter-t/backends/vllm/bin/python -m vllm.entrypoints.openai.api_server \
  --model Qwen/Qwen2.5-Coder-7B-Instruct --port 8000 --max-model-len 32768
```

> If you'd rather let a GUI handle startup, see [Launcher](./launcher.en.md). Note that the Launcher only takes care of **starting the process** — registering it as a provider (below) is a separate step.

### 2. Register it as a provider in CodeRouter (`~/.coderouter-t/providers.yaml`)

```yaml
allow_paid: false
default_profile: default

providers:
  - name: llama-cpp-local
    kind: openai_compat
    base_url: http://localhost:8080/v1     # ← must match the port you started
    model: ""                              # llama-server doesn't care about the model name
    timeout_s: 120

  - name: vllm-local
    kind: openai_compat
    base_url: http://localhost:8000/v1     # ← vLLM's default is 8000
    model: Qwen/Qwen2.5-Coder-7B-Instruct
    timeout_s: 120

  - name: openrouter-free                  # escape hatch (optional but recommended)
    kind: openai_compat
    base_url: https://openrouter.ai/api/v1
    model: qwen/qwen3-coder:free
    api_key_env: OPENROUTER_API_KEY

profiles:
  - name: default
    providers: [llama-cpp-local, vllm-local, openrouter-free]
```

Copying `examples/providers.llamacpp-vllm.yaml` as a starting point is the fastest route.

### 3. Start CodeRouter and connect Claude Code

```bash
# Terminal 1
coderouter-t serve --port 8088

# Terminal 2 (scope the env vars to this shell only; don't set them globally)
ANTHROPIC_BASE_URL=http://localhost:8088 ANTHROPIC_AUTH_TOKEN=dummy claude
```

`ANTHROPIC_AUTH_TOKEN` can be a dummy value (CodeRouter doesn't validate it). Real API keys are managed on the `providers.yaml` side.

---

## Four key configuration points

### ① Match the ports

The port in `base_url` must match the running inference server character-for-character. A mismatch produces `transport error: All connection attempts failed` (connection refused).

| Backend | Default port | Check |
|---|---|---|
| llama.cpp | `8080` | `curl localhost:8080/health` |
| vLLM | `8000` | `curl localhost:8000/v1/models` |
| Ollama | `11434` | `curl localhost:11434/api/version` |

### ② Raise the context size for Claude Code

Claude Code sends **15-35K tokens** every turn. If `--ctx-size` / `--max-model-len` is too small, you'll get
`exceed_context_size_error (400)`. Default to **32768 or more**.
If that exceeds the model's trained context, use rope extensions like YaRN.

```bash
llama-server -m <model>.gguf --port 8080 -ngl 99 \
  --ctx-size 65536 --rope-scaling yarn --yarn-orig-ctx 32768
```

If a large ctx causes OOM: lower ctx / use KV quantization (`--cache-type-k q8_0 --cache-type-v q8_0`) / use a smaller model.

### ③ Put a fallback that actually works at the end of the chain

If `providers:` only lists local backends, there's nowhere to go the moment one goes down, and you get a **502**. Adding one free cloud entry at the end keeps Claude Code running even if the local backend crashes or can't fit the context. Paid providers are never called unless `ALLOW_PAID=true` is set (default `allow_paid: false`).

### ④ Applying edits takes 3 steps

Changes don't take effect on a running process immediately.

1. Put the config in **`~/.coderouter-t/providers.yaml`** (editing files under `examples/` alone has no effect)
2. **Restart `coderouter-t serve`** (`launcher:` / profiles are read at startup)
3. **Restart the backend** (`--ctx-size` and similar flags only take effect after a restart)

---

## Troubleshooting quick reference

| Symptom / log | Cause | Fix |
|---|---|---|
| `400 exceed_context_size_error` `n_ctx:4096` | ctx-size too small to fit Claude Code's prompt | **Restart** with `--ctx-size 32768` or higher. Use YaRN if exceeding trained ctx |
| `transport error: All connection attempts failed` (status: null) | Backend is down / **port mismatch** | Start the server + align `base_url`'s port with the real port |
| Everything shows `provider-failed` → **502 Bad Gateway** | The whole chain's escape hatches are dead | Recover one backend + add a free-cloud fallback at the end |
| Still `n_ctx=4096` after restarting | An old process is still alive / config not reflected under `~/.coderouter-t` / serve not restarted | Check the actual command with `ps aux \| grep llama-server` → follow the 3-step reflect process |
| Backend crashes immediately with OOM | ctx set too high | Lower ctx / KV quantization / smaller model |
| `capability-degraded: cache_control` | Just dropping the Anthropic prompt-cache marker when translating to OpenAI format | **Harmless**. No action needed |
| `claude.ai connectors are disabled …` | Env vars `ANTHROPIC_AUTH_TOKEN`/`API_KEY` take priority over the claude.ai login | Scope CodeRouter's env vars to that shell/folder only (`direnv`). `unset` them in your everyday shell |
| `/mcp` still shows `✘ failed` | An unneeded MCP server registration | `claude mcp remove <name> -s user` |

### Triage commands

```bash
# Is the backend alive?
curl -s localhost:8080/health ; echo            # llama.cpp
curl -s localhost:8000/v1/models ; echo         # vLLM
ps aux | grep -E '[l]lama-server|[v]llm'

# CodeRouter-side status
open http://localhost:8088/dashboard            # color-coded view of what's down
coderouter doctor --check-model llama-cpp-local # diagnose a provider with 7 probes
```

---

## Three backends at a glance

| | What you need | Model format | Unit of startup |
|---|---|---|---|
| Ollama | `ollama pull` | Managed by Ollama | One daemon, on-demand |
| **llama.cpp** | `.gguf` + `llama-server` | `.gguf` (a local file) | **1 launch = 1 model = 1 port** |
| **vLLM** | NVIDIA GPU + venv | HF ID / local path | 1 launch = 1 model = 1 port |

With llama.cpp / vLLM, only "the model you launched" is available (unlike Ollama, they don't serve multiple models out of one endpoint). To use several, split them across ports and register each as a provider.

---

最終更新: 2026-06-24
