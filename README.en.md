<h1 align="center">CodeRouter-t</h1>

<p align="center">
  <strong>Tool calling breaks when you run Claude Code on local LLMs.<br>One router fixes it.</strong>
</p>

<p align="center">
  <a href="https://github.com/OrgaiCom/CodeRouter/actions/workflows/ci.yml"><img src="https://github.com/OrgaiCom/CodeRouter/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI"></a>
  <a href="https://pypi.org/project/coderouter-t/"><img src="https://img.shields.io/pypi/v/coderouter-t?include_prereleases&color=blue&label=pypi" alt="pypi"></a>
  <a href=""><img src="https://img.shields.io/badge/python-3.12%2B-blue" alt="python"></a>
  <a href=""><img src="https://img.shields.io/badge/deps-5-brightgreen" alt="deps"></a>
  <a href=""><img src="https://img.shields.io/badge/license-MIT-yellow" alt="license"></a>
</p>

<p align="center">
  <strong>English</strong> · <a href="./README.md">日本語</a> · <a href="./docs/start/quickstart.en.md">Get started in 10 min</a> · <a href="./docs/concepts/architecture.md">Architecture</a>
</p>

---

## What it does — in 30 seconds

```
Your agent (Claude Code / codex / agy)
        │
        ▼
  ┌─ CodeRouter-t ──┐
  │  translate     │──→  ① Local (Ollama — free, fastest)
  │  repair        │──→  ② Free cloud (OpenRouter / NIM)
  │  guard + heal  │──→  ③ Paid (Claude — opt-in only)
  └────────────────┘
```

**What it does for you:**

- Repairs broken tool calling from local models before it reaches Claude Code
- Automatically falls back to the next provider when one goes down
- Only uses paid APIs when you explicitly allow it (free-only by default)
- Keeps your agent running for 8+ hours with 6 types of guards
- Diagnoses what's wrong with one command: `coderouter-t doctor`

---

## Install (3 lines)

```bash
# 1. Drop a sample config
mkdir -p ~/.coderouter-t
curl -fsSL https://raw.githubusercontent.com/OrgaiCom/CodeRouter/main/examples/providers.yaml \
  > ~/.coderouter-t/providers.yaml

# 2. Run (Python 3.12+)
uvx --from coderouter-t coderouter-t serve --port 8088
```

For a permanent install: `uv tool install coderouter-t`

---

## Use with Claude Code

```bash
# Terminal 1
coderouter-t serve --port 8088

# Terminal 2
ANTHROPIC_BASE_URL=http://localhost:8088 ANTHROPIC_AUTH_TOKEN=dummy claude
```

That's it. Claude Code works as usual, but your local Ollama is answering behind the scenes.

**From VSCode's integrated terminal**, avoid the env-var footgun with `coderouter-t vscode-init`. Run it once at your project root and it merges `terminal.integrated.env.*` into `.vscode/settings.json`, so `claude` in VSCode's terminal just works from then on. Cheat-sheet snippets for Cline / Roo Code / Continue.dev are printed at the end of the same run → [VSCode integration guide](./docs/guides/vscode.md) (JP only for now).

---

## Do you need it?

| Your situation | CodeRouter-t |
|---|---|
| Claude Code + local Ollama, tool calling breaks | **Yes** — wire translation + tool repair |
| Claude Code + local, dies after long sessions | **Helpful** — 6 guards + self-healing |
| codex / agy + Ollama works fine | Optional — if you want fallback |
| Using Claude API directly, no issues | Not needed |

Full decision matrix → [Do I need CodeRouter?](./docs/start/when-do-i-need-coderouter.en.md)

---

## Key Features

### Connection & Repair

| Feature | What it does |
|---|---|
| **Wire translation** | Claude Code (Anthropic format) ↔ Ollama (OpenAI format) auto-converted |
| **Tool-call repair** | JSON that local models emit as plain text → valid tool_use blocks |
| **3-tier fallback** | Local → free cloud → paid, automatic switching |
| **Output filters** | Strips leaked `<think>` tags, stop markers, XML tool tags |

### Long-running Session Guards

| Guard | What it protects against |
|---|---|
| **Context Budget** | Messages piling up → context window overflow. Auto-trim at 90% |
| **Drift Detection** | Model quality degrading over time → switch provider or flush KV cache (6 signals incl. `goal_progress_stall`; `goal_mode` for tighter thresholds) |
| **Self-healing** | Backend crashes → auto-exclude + restart + recovery probe → auto-restore |
| **Tool Loop Guard** | Agent calling the same tool forever → detect and break |
| **Memory Pressure** | Backend hits OOM → temporarily excluded, falls through to the next provider in the chain |
| **Mid-stream Guard** | Response dies mid-stream → safely return accumulated text |

### Diagnostics & Visibility

| Feature | What you learn |
|---|---|
| **`coderouter-t doctor`** | 7-probe diagnosis of provider issues + copy-paste YAML patches |
| **`/dashboard`** | Real-time browser view of what's happening |
| **`coderouter-t audit`** | Search guard activation history |
| **`coderouter-t replay`** | Compare providers statistically (A/B analysis) / `--suggest-rules` for automated rule suggestions |
| **Continuous Probe** | Background health monitoring even during idle |

### Language Tax tracking — v2.6.0

CJK text (Japanese / Chinese / Korean) costs more tokens on cloud tokenizers than the same meaning in English (**measured: ~1.6× on average with GPT-4o-era o200k, ~2.0× with GPT-4-era cl100k**). Local models bill nothing per token, so this "language tax" only bites on the cloud leg. CodeRouter-t v2.6.0 makes it **measurable, routable, and visible**.

| Feature | What it does |
|---|---|
| **Language-tax measurement** | Set a provider's `tokenizer_path` (a local `tokenizer.json`) to compute the real token multiplier vs the char/4 heuristic and the extra USD. No network; inert when unset. |
| **`cjk_ratio_min` routing** | Auto-route CJK-heavy turns to a local (tax-free) model; code/English falls through to the cloud chain. |
| **Dashboard panel** | The `/dashboard` "Cost & Language Tax" panel shows total spend, cache savings, and language-tax spend live. |

```yaml
# providers.yaml — steer CJK-heavy turns to local
auto_router:
  rules:
    - match: { cjk_ratio_min: 0.3 }   # >=30% CJK chars -> local
      profile: local
    - match: { has_tools: true }      # tool use -> cloud
      profile: cloud
  default_rule_profile: cloud

providers:
  - name: cloud-sonnet
    kind: anthropic
    base_url: https://api.anthropic.com
    model: claude-sonnet-4-6
    tokenizer_path: ~/.coderouter-t/tokenizers/sonnet.json   # accurate language-tax (optional)
```

Details → [Language Tax guide](./docs/guides/language-tax.en.md)

### Launcher — llama.cpp / vllm GUI

Browser UI at `http://localhost:8088/launcher` for starting and managing local inference backends.

| Feature | Detail |
|---|---|
| **Model scanner** | Recursively scans `model_dirs` for `.gguf` / `.safetensors` and lists them |
| **Option profiles** | Name your flag presets in `providers.yaml` — select from a dropdown, no CLI needed |
| **Multi-process** | Run llama.cpp and vllm side by side on different ports |
| **Log viewer** | stdout/stderr of each process shown live in the browser |
| **Provider auto-sync** (v2.7.4) | A started backend is auto-registered as a routable provider (`launcher-llamacpp-8085`); route to it with `X-CodeRouter-Profile: launcher` — zero providers.yaml edits. In-memory, shares the server's lifetime |
| **Model-id passthrough** (v2.7.4) | Providers with `model: ""` make `/v1/models` surface the upstream's actually-loaded model id (the GGUF name). Swap models without touching config — external benchmarks can tell runs apart |

```yaml
# Add to providers.yaml — no code changes needed
launcher:
  model_dirs:
    - ~/models
  option_profiles:
    llama.cpp:
      - name: "Full GPU"
        args:
          "-ngl": 99
          "--ctx-size": 4096
    vllm:
      - name: "Standard"
        args:
          "--dtype": "auto"
          "--max-model-len": 4096
```

Details → [Launcher guide](./docs/backends/launcher.md)

---

## Minimal Config

```yaml
# ~/.coderouter-t/providers.yaml
default_profile: claude-code

profiles:
  - name: claude-code
    providers: [ollama-local, openrouter-free]

providers:
  - name: ollama-local
    kind: openai_compat
    base_url: http://localhost:11434/v1
    model: qwen3-coder:7b

  - name: openrouter-free
    kind: openai_compat
    base_url: https://openrouter.ai/api/v1
    model: qwen/qwen3-coder:free
    api_key_env: OPENROUTER_API_KEY
```

More detail → [Usage guide](./docs/guides/usage-guide.en.md) · [Architecture](./docs/concepts/architecture.md)

---

## Documentation

| Goal | Document |
|---|---|
| Get running fast | [Quickstart](./docs/start/quickstart.en.md) |
| Use it well | [Usage guide](./docs/guides/usage-guide.en.md) |
| Run for free | [Free-tier guide](./docs/guides/free-tier-guide.en.md) |
| Launch llama.cpp / vllm via GUI | [Launcher guide](./docs/backends/launcher.md) |
| Measure & avoid the language tax | [Language Tax guide](./docs/guides/language-tax.en.md) |
| Use from VSCode / Cline / Continue | [VSCode integration guide](./docs/guides/vscode.md) (JP) |
| Reach it safely from another machine | [Remote access guide](./docs/guides/remote-access.en.md) |
| Stuck? | [Troubleshooting](./docs/guides/troubleshooting.en.md) |
| Understand the design | [Architecture](./docs/concepts/architecture.md) |
| Full release history | [CHANGELOG](./CHANGELOG.md) |

日本語: [Quickstart](./docs/start/quickstart.md) · [利用ガイド](./docs/guides/usage-guide.md) · [無料枠ガイド](./docs/guides/free-tier-guide.md) · [トラブルシューティング](./docs/guides/troubleshooting.md)

---

## Troubleshooting (cheat sheet)

**First move**: run `coderouter-t doctor --check-model <provider>`. It usually finds the problem.

| Symptom | Cause | Details |
|---|---|---|
| 401 error | API key not set / missing `export` in `.env` | [§1](./docs/guides/troubleshooting.en.md#1-five-startup--config-gotchas-added-in-v162) |
| Empty / garbage replies | Ollama `num_ctx` truncated to 2048 | [§3](./docs/guides/troubleshooting.en.md#3-ollama-beginner--5-silent-fail-symptoms-v07-c) |
| `<think>` tags leaking | Add `output_filters: [strip_thinking]` | [§3](./docs/guides/troubleshooting.en.md#3-ollama-beginner--5-silent-fail-symptoms-v07-c) |
| Tool calls misbehaving in Claude Code | Tool-call repair not kicking in | [§4](./docs/guides/troubleshooting.en.md#4-claude-code-integration-gotchas-added-in-v162) |

Open `http://localhost:8088/dashboard` while debugging — most issues become visible in 10 seconds.

---

## Tech Specs

- **Runtime deps**: `fastapi` / `uvicorn` / `httpx` / `pydantic` / `pyyaml` — only 5
- **Tests**: 1,500+ (the 5 runtime deps have never grown since v1)
- **OS**: macOS (Apple Silicon recommended) / Linux / Windows WSL2
- **Backends**: Ollama / llama.cpp / LM Studio / vLLM / MLX-LM / OpenRouter / NVIDIA NIM / Anthropic API
- **External agent CLIs**: bundle Claude Code / codex / grok / antigravity as a single `agent_cli` provider (requires `coderouter-plugin-agents`; details → [external-agents guide](./docs/backends/external-agents.en.md))
- **Plugins**: compress / memory / agents, all opt-in with zero impact on core dependencies (list & install → [docs/README.en.md](./docs/README.en.md#plugins))
- **License**: MIT

---

## Ecosystem

CodeRouter-t runs as an independent backend router layer. Point any project's `OPENAI_BASE_URL` at CodeRouter-t and it gets fallback + observability for free:

- **[Voice Bridge](https://github.com/zephel01/voice-bridge)** — Real-time voice translation + AI voice chat. Route through CodeRouter so your voice assistant doesn't go silent when the local LLM hiccups.

---

## Language

Human-facing messages (CLI / Doctor / startup warnings) support English and Japanese. JSON logs stay English.

```bash
# Japanese
CODEROUTER_T_LANG=ja coderouter-t serve

# English (default when no locale hint)
CODEROUTER_T_LANG=en coderouter-t serve
```

- `CODEROUTER_T_LANG=ja` / `en` takes precedence when set
- Otherwise `LANG` / `LC_MESSAGES` is auto-detected (`ja_JP.UTF-8` → Japanese)
- Invalid values fall back to English

## Security

Secrets go in env vars, not config files. See [`docs/security.en.md`](./docs/guides/security.en.md) for the full policy and reporting instructions.

## License

MIT
