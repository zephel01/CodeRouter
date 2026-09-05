# CodeRouter Documentation

日本語版: [`README.md`](./README.md)

Index of CodeRouter's public documentation — find the right page by what you want to do.

> Developer-internal notes and article drafts live in `inside/` and `articles/` (local-only, not shipped in the public repo).

---

## Quick start by goal

| Goal | Read |
|---|---|
| Get running now | [start/quickstart](start/quickstart.en.md) |
| Is it for me? | [start/when-do-i-need-coderouter](start/when-do-i-need-coderouter.en.md) |
| Run for free | [guides/free-tier-guide](guides/free-tier-guide.en.md) |
| Learn the features | [guides/usage-guide](guides/usage-guide.en.md) |
| Measure & avoid the language tax | [guides/language-tax](guides/language-tax.en.md) |
| Something broke | [guides/troubleshooting](guides/troubleshooting.en.md) |
| Launch a local LLM | [backends/launcher-quickstart](backends/launcher-quickstart.md) |
| Run on a low-memory (8–16GB) host | [low-memory-integration](low-memory-integration.en.md) |
| Secrets & security | [guides/security](guides/security.en.md) |
| Connect securely from another PC | [guides/remote-access](guides/remote-access.en.md) |
| Understand the design | [concepts/architecture](concepts/architecture.en.md) |
| Extend with plugins | [Plugins](#plugins) |

---

## Layout

```
docs/
├── start/             Getting started
├── guides/            How-to guides
├── backends/          Local LLM backends
├── concepts/          Architecture & internals
├── designs/           Design docs
├── retrospectives/    Release retrospectives
├── evidence/          Verification logs
├── openrouter-roster/ OpenRouter model roster
└── assets/            Images
```

Many documents have a Japanese version (`.md`) and an English version (`.en.md`).

---

## 1. Getting started — `start/`

For first-time users.

- **quickstart** — Get running in one sitting · [日本語](start/quickstart.md) · [English](start/quickstart.en.md)
- **when-do-i-need-coderouter** — Decide whether you need it · [日本語](start/when-do-i-need-coderouter.md) · [English](start/when-do-i-need-coderouter.en.md)

## 2. How-to guides — `guides/`

Day-to-day usage.

- **usage-guide** — Full feature guide · [日本語](guides/usage-guide.md) · [English](guides/usage-guide.en.md)
- **language-tax** — Measure, route around, and visualize the CJK language tax · [日本語](guides/language-tax.md) · [English](guides/language-tax.en.md)
- **free-tier-guide** — Zero-cost operation with NVIDIA NIM × OpenRouter Free · [日本語](guides/free-tier-guide.md) · [English](guides/free-tier-guide.en.md)
- **troubleshooting** — Fixing problems · [日本語](guides/troubleshooting.md) · [English](guides/troubleshooting.en.md)
- **security** — Secrets handling & security posture · [日本語](guides/security.md) · [English](guides/security.en.md)
- **remote-access** — Reach CodeRouter safely from another machine (four options: SSH tunnel, Tailscale, etc.) · [日本語](guides/remote-access.md) · [English](guides/remote-access.en.md)
- **subagent-routing** — Route Claude Code sub-agents to different models · [日本語](guides/subagent-routing.md) · [English](guides/subagent-routing.en.md)

## 3. Local LLM backends — `backends/`

Installing, launching, and connecting local inference backends.

- **install-backends** — Installing the three backends (llama.cpp / vLLM / MLX) · [日本語](backends/install-backends.md) · [English](backends/install-backends.en.md)
- **launcher-quickstart** — Install a backend and launch, the shortest path · [日本語](backends/launcher-quickstart.md) · [English](backends/launcher-quickstart.en.md)
- **launcher** — Launcher guide (Web & Desktop GUI) · [日本語](backends/launcher.md) · [English](backends/launcher.en.md)
- **external-agents** — External coding-agent CLI (agent_cli, 4 CLIs: claude/codex/grok/antigravity; requires `coderouter-plugin-agents` since v2.9.0) · [日本語](backends/external-agents.md) · [English](backends/external-agents.en.md)
- **llamacpp-direct** — Connect llama.cpp directly · [日本語](backends/llamacpp-direct.md) · [English](backends/llamacpp-direct.en.md)
- **lmstudio-direct** — Connect LM Studio directly · [日本語](backends/lmstudio-direct.md) · [English](backends/lmstudio-direct.en.md)
- **claude-code-llamacpp-vllm** — Connect Claude Code to llama.cpp / vLLM without Ollama, plus fixes for the errors you'll hit in practice · [日本語](backends/claude-code-llamacpp-vllm.md) · [English](backends/claude-code-llamacpp-vllm.en.md)
- **hf-ollama-models** — Use HF models via Ollama · [日本語](backends/hf-ollama-models.md) · [English](backends/hf-ollama-models.en.md)
- **gguf_dl** — GGUF download helper · [日本語](backends/gguf_dl.md) · [English](backends/gguf_dl.en.md)
- **verify-ollama-0.23.1** — Ollama v0.23.1 verification checklist · [日本語](backends/verify-ollama-0.23.1.md)

## 4. Architecture & internals — `concepts/`

How CodeRouter works and its reliability mechanisms.

- **architecture** — Architecture overview · [日本語](concepts/architecture.md) · [English](concepts/architecture.en.md)
- **context-budget** — Context budget management (v2.0.0) · [日本語](concepts/context-budget.md) · [English](concepts/context-budget.en.md)
- **drift-detection** — Drift detection (v2.0-G) · [日本語](concepts/drift-detection.md) · [English](concepts/drift-detection.en.md)
- **partial-stitch** — Mid-stream partial stitching (v2.0-H) · [日本語](concepts/partial-stitch.md) · [English](concepts/partial-stitch.en.md)
- **continuous-probing** — Continuous probing (v2.0-I) · [日本語](concepts/continuous-probing.md) · [English](concepts/continuous-probing.en.md)
- **stream-truncation** — Stream truncation detection (v2.15.0) · [日本語](concepts/stream-truncation.md) · [English](concepts/stream-truncation.en.md)
- **low-memory-integration** — Integration guide for the proactive memory-budget guard on low-memory (8–16GB) hosts — VRAM/RAM detection, GGUF header introspection, and KV-cache-aware pre-dispatch fit decisions (`off`/`warn`/`fit`). **Implemented (v2.5.3)** · [日本語](low-memory-integration.md) · [English](low-memory-integration.en.md)

## 5. Design docs & records

### designs/ — Feature design docs

Implementation status is noted for each doc.

- **v1.6-auto-router** — Initial design of auto_router. **Implemented (v1.6.0); matchers have since expanded to 8 kinds** · [日本語](designs/v1.6-auto-router.md)
- **v1.6-auto-router-verification** — Release verification record for v1.6.0. **Completed (2026-04-22), archived** · [日本語](designs/v1.6-auto-router-verification.md)
- **external-agents-adapter** — External coding-agent CLI adapter (Phase 1a–1d). **Implemented (v2.7.7–v2.7.10); the in-core implementation was removed in v2.9.0 and moved to `coderouter-plugin-agents`** · [日本語](designs/external-agents-adapter.md)
- **agent-cli-plugin-extraction** — Extracting agent_cli into an out-of-tree plugin (Phase 2a/2b/2c). **Implemented (v2.8.0 / v2.8.1 / v2.9.0)** · [日本語](designs/agent-cli-plugin-extraction.md)
- **launcher-model-swap** — On-demand model launch & TTL-based unload. **Phase 1 implemented (v2.9.1); Phase 2 (memory accounting + exclusive swap) not started** · [日本語](designs/launcher-model-swap.md)
- **launcher-multi-build** — Switching between multiple llama.cpp builds (backend variants). **Implemented (v2.11.0)** · [日本語](designs/launcher-multi-build.md)
- **orchestration-companion** — Concept for a separate-process orchestrator. **Concept only, not started** · [日本語](designs/orchestration-companion.md)

### retrospectives/ — Release retrospectives

- [v0.4](retrospectives/v0.4.md) · [v0.5](retrospectives/v0.5.md) · [v0.5-verify](retrospectives/v0.5-verify.md) · [v0.6](retrospectives/v0.6.md) · [v0.7](retrospectives/v0.7.md) · [v1.0](retrospectives/v1.0.md) · [v1.0-verify](retrospectives/v1.0-verify.md)

### Other

- **evidence/** — Verification run logs
- **openrouter-roster/** — OpenRouter model roster — [README](openrouter-roster/README.md) · [change log](openrouter-roster/CHANGES.md)

---

## Plugins

CodeRouter's **Plugin SDK** (since v2.3.0) loads out-of-tree plugins *opt-in*: a plugin runs only when its name is listed in `plugins.enabled` (supply-chain defense), so installing one does nothing by itself. Each plugin ships as a separate PyPI package, so **the core's dependencies never grow**.

| Plugin | What it does | Install | Repo |
|---|---|---|---|
| **compress** | Compresses tool output (JSON / logs) before it reaches the LLM to cut tokens; originals kept locally and reversible (CCR). `cache-align` also aligns Anthropic prompt caching. | Not yet on PyPI — install from git+https:<br>`uv pip install "coderouter-plugin-compress[accuracy] @ git+https://github.com/zephel01/coderouter-plugin-compress"` | [coderouter-plugin-compress](https://github.com/zephel01/coderouter-plugin-compress) |
| **memory** | Extracts key facts from responses into `facts.jsonl` and auto-injects them into the next session's system prompt — solving "explain it every time" at the wire layer. | `pip install coderouter-plugin-memory` | [coderouter-plugin-memory](https://github.com/zephel01/coderouter-plugin-memory) |
| **agents** | Registers external coding-agent CLIs (claude / codex / grok / antigravity) as `kind: agent_cli` providers. The in-core implementation was removed in v2.9.0, making this plugin required. See [backends/external-agents](backends/external-agents.en.md). | Not yet on PyPI — install from git+https:<br>`uv pip install "coderouter-plugin-agents @ git+https://github.com/zephel01/coderouter-plugin-agents"` | [coderouter-plugin-agents](https://github.com/zephel01/coderouter-plugin-agents) |

Enable by adding to `providers.yaml`; a `plugin-loaded` line in the startup log confirms activation.

```yaml
plugins:
  enabled:
    - compress          # compress tool output
    - compress-stats    # report compression ratio in coderouter stats
    - cache-align       # align prompt-cache breakpoints
    - memory            # cross-session memory
    - agents            # external agent CLIs (agent_cli)
  config:
    compress:
      mode: safe        # off | safe | aggressive
      ccr: true         # reversible re-expansion (default on)
    memory:
      consolidate_model: qwen3:1.7b
```

When using an `agent_cli` provider, enabling `agents` is not optional — it's **required** (without it, `coderouter-t serve` fails at startup). See [backends/external-agents](backends/external-agents.en.md) for details.

See each plugin's repo README for full configuration.

---

Last updated: 2026-08-10
