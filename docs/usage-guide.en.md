# CodeRouter Usage Guide

A practical companion to [`README.md`](../README.en.md). The README tells you what CodeRouter *is*; this guide tells you how to pick a model for your hardware, which knobs to tune, and which OS flow to follow.

Sections:

1. [OS compatibility](#1-os-compatibility)
2. [Pick a model for your hardware](#2-pick-a-model-for-your-hardware)
3. [Tuning defaults per local model](#3-tuning-defaults-per-local-model)
4. [Ollama setup — the short version](#4-ollama-setup--the-short-version)
5. [Claude Code launch flow per OS](#5-claude-code-launch-flow-per-os)
6. [OpenRouter free-tier pairing](#6-openrouter-free-tier-pairing)
7. [Verify it works (`doctor` + `verify`)](#7-verify-it-works-doctor--verify)
8. [Troubleshooting quick index](#8-troubleshooting-quick-index)
9. [Attribution](#9-attribution)

Japanese version: [`docs/usage-guide.md`](./usage-guide.md)

---

## 1. OS compatibility

CodeRouter itself is pure Python 3.12+ and five pip dependencies — the server runs anywhere CPython runs. The constraints come from two adjacent pieces: **Ollama** (the local model backend most users pair this with) and **Claude Code** (the CLI client). OS support is effectively `min(coderouter, ollama, claude-code)`.

| OS | CodeRouter server | Ollama | Claude Code | Verified path |
|---|---|---|---|---|
| macOS — Apple Silicon (M1–M5) | ✅ | ✅ native (Metal) | ✅ via `npm install -g @anthropic-ai/claude-code` | **Primary dev target.** All v1.0 real-machine verify runs use this path. |
| macOS — Intel | ✅ | ✅ but slow (CPU only; no Metal GPU) | ✅ | Works for CodeRouter wire layer; local inference impractical — use cloud fallback only. |
| Linux — x86_64 (Ubuntu / Debian / Fedora) | ✅ | ✅ native (CUDA if NVIDIA GPU, else CPU) | ✅ | Fully supported. `uv` + `pip install` path identical to macOS. |
| Linux — ARM64 (Raspberry Pi 5 / AWS Graviton) | ✅ | ⚠️ CPU-only on Pi; cloud instances fine | ✅ | CodeRouter runs fine; usable primarily as a "route-to-cloud" proxy on Pi-class hardware. |
| Windows — native (PowerShell / cmd) | ⚠️ partial | ✅ native (CUDA) | ⚠️ `claude` CLI has known Windows-native quirks | `coderouter serve` works. `scripts/verify_*.sh` are bash-only — run them under WSL or Git Bash. |
| Windows — WSL2 (Ubuntu) | ✅ | ✅ (install inside WSL or bridge to Windows-host Ollama at `host.docker.internal:11434`) | ✅ | **Recommended Windows path.** Same UX as Linux from inside WSL2. |

Quick decision rules:

- **Apple Silicon Mac, ≥ 16 GB unified memory** — ideal setup. Ollama + qwen2.5-coder works out of the box.
- **Linux workstation with NVIDIA GPU (≥ 8 GB VRAM)** — also ideal. Ollama uses CUDA automatically.
- **Windows** — use WSL2 unless you have a specific reason not to. Bash shell scripts (`scripts/verify_v1_0.sh`) require a POSIX shell.
- **No local GPU** — CodeRouter still earns its keep. Skip local-tier providers and point the chain straight at `openrouter-free` → `openrouter-claude` (paid, opt-in). You get the routing / fallback / mid-stream guard value without local inference.

Known gaps:

- `scripts/verify_v0_5.sh` and `scripts/verify_v1_0.sh` assume macOS `/bin/bash` 3.2+ (Linux bash 4+ is fine). They do not target Windows cmd/PowerShell.
- No Docker image shipped yet — `plan.md §11` tracks this for v1.6 (originally planned as v1.1; re-scoped after v1.5 shipped ahead of the launcher block).

---

## 2. Pick a model for your hardware

Two separate questions: "how big a model will my machine load" and "how fast will inference be". The table below optimizes for the first; speed is dominated by memory bandwidth (Apple unified memory, CUDA GDDR, or CPU RAM — in that order).

| Your machine | Local model (Ollama tag) | Why |
|---|---|---|
| 8 GB VRAM / 8–16 GB RAM (entry Windows / Linux laptop, M1/M2 base Mac) | `qwen2.5-coder:1.5b` — then straight to OpenRouter free | 1.5b loads in ~1 GB and responds fast. Quality is marginal for Claude Code tool-use; treat as "only when offline" and let the chain fall through to free cloud for real work. |
| 16 GB VRAM / 16–24 GB RAM (RTX 4070 / M1 Pro / M2 / M3 base) | `qwen2.5-coder:7b` (default `Q4_K_M` quant, ~4.5 GB) | Sweet spot. Tool-capable, returns in 30–60s per Claude-Code turn on M-series. `examples/providers.yaml` ships with this as the lead local provider. |
| 24 GB+ VRAM / 24–36 GB RAM (RTX 4090 / M1 Max / M2 Max / M3 Max 32GB) | `qwen2.5-coder:14b` (~8.5 GB) | Better tool-selection quality than 7b. Typical turn ~2 min. Pair with 7b as a fast first attempt + 14b as quality fallback — that's what the `claude-code` profile does. |
| 48 GB+ / M-series Max/Ultra 64 GB+ | `qwen2.5-coder:32b` (~19 GB) or two 14b's in different quants | At this tier, local is good enough that cloud fallback becomes a nice-to-have rather than the primary path. |
| Mac 96 GB+ / dedicated GPU server 80 GB+ | Multi-model hot-swap (32b + 14b + 7b all warm) | Out of scope for this guide — if you have the hardware, you already have opinions. |

All tags above assume the default `Q4_K_M` quantization unless stated. You can tighten to `Q5_K_M` / `Q6_K_M` for slightly better quality at the cost of ~25 % more VRAM. `Q8_0` is rarely worth it at this parameter count.

Rule of thumb for VRAM: a `Q4_K_M` GGUF needs roughly `params × 0.55 GB` of VRAM, plus 1–2 GB for KV cache at 32K context. So `qwen2.5-coder:14b` Q4 ≈ 7.7 GB weights + 1.5 GB KV ≈ 9.2 GB — fits a 16 GB GPU comfortably but leaves little room for anything else.

### Models worth knowing beyond qwen2.5-coder

CodeRouter treats any OpenAI-compat endpoint the same way, so model choice is orthogonal to router choice. A few commonly-paired options, grouped by family. CodeRouter doesn't care which vendor shipped the weights — as long as Ollama (or any OpenAI-compat server) can load the tag, the router can route to it.

Dense coder families (the 2.5-coder profile below extends directly):

- **`qwen3-coder:7b` / `:14b`** — qwen3 dense coder family. Similar scale to 2.5-coder with a different reasoning style. Tends to leak `<think>` tags more — enable `output_filters: [strip_thinking]` and/or `append_system_prompt: "/no_think"`. Shipped as a reference profile in `examples/providers.yaml` under `ollama-hf-example` (commented out).
- **`deepseek-coder-v2:16b`** — DeepSeek-v2 MoE architecture (2.4B active, 16B total). Very fast for its size on macOS unified memory. Tool-use is hit-or-miss; set `capabilities.tools: false` if `coderouter doctor` says so.

MoE coders (big-parameter-count, small-active — easier on memory bandwidth than their headline size suggests):

- **`qwen3-coder:30b-a3b`** — 30 B total / ~3 B active MoE. Tool-capable, streams noticeably faster than dense 30B on Apple Silicon because only the ~3 B active path runs per token; VRAM still has to hold the whole graph (~18 GB Q4). Enable both `strip_thinking` and `strip_stop_markers`, and verify tool-call reliability with `coderouter doctor`.
- **`qwen3:32b`** (dense, general-purpose) — big general model; use `append_system_prompt: "/no_think"` to silence the chain-of-thought channel before it reaches Claude Code's UI. Lower tool-call hit rate than 2.5-coder:32b; probe to confirm.
- **`qwen3:30b-a3b`** (general MoE sibling to the coder) — same MoE footprint as qwen3-coder:30b-a3b but general-domain. Useful in a "fast" profile when you need long-context reasoning without coder bias.

Reasoning-tuned distills (emit `<think>` blocks on by default — always pair with `strip_thinking` + `strip_stop_markers`):

- **`deepseek-r1:distill-qwen-14b` / `:distill-qwen-32b`** — R1-distilled Qwen bases. Strong on plan-then-act reasoning, weak on structured tool JSON; leave `capabilities.tools: false` and use them as a quality-fallback for text answers rather than the tool-call tier.

General-purpose (not coder-tuned, so typically `capabilities.tools: false` unless the family is instruction-trained for tool use):

- **`gemma3:4b` / `:12b` / `:27b`** — Google's Gemma 3 family. Multilingual (Japanese included), but no tool-use training — treat as `capabilities.tools: false`. `gemma3:12b` Q4 fits comfortably in 12 GB VRAM and is a reasonable "fast chat" tier. There is no `/no_think` directive — Gemma 3 doesn't emit `<think>` blocks by default, so `output_filters` can stay empty unless you see leakage.
- **`gemma4:e2b` / `:e4b` / `:latest` / `:26b` / `:31b`** — Google's Gemma 4 family, published on Ollama as <https://ollama.com/library/gemma4>. A noticeably different lineup from Gemma 3: the `e2b` / `e4b` tags are *effective-parameter* builds tuned for edge / laptop deployment (128 K context at ~7–10 GB pull size), `:26b` is a Mixture-of-Experts with ~4 B active / 26 B total and a **256 K** context window, and `:31b` is the dense flagship. Gemma 4 is multimodal (text + image) and ships with **configurable thinking modes** — unlike Gemma 3, it *can* emit `<think>` blocks when reasoning is enabled, so **start with `output_filters: [strip_thinking]`** on all gemma4 tags and enable `strip_stop_markers` too if you see marker leakage. Tool-use on the family is lineage-dependent — Gemma 3 had none, Gemma 4 has some instruct tuning, so **always verify with `coderouter doctor`** before flipping `capabilities.tools: true`. The `:26b` MoE is the sweet spot on Apple Silicon (24–32 GB unified memory) because only the ~4 B active path runs per token while the 256 K window gives you Claude-Code-friendly headroom.
- **`llama3.3:70b`** — the 70 B instruct dense from Meta. Tool-capable when quantized cleanly; needs ≥ 48 GB VRAM or Mac 64 GB+ unified. For most users this sits in a "paid-tier replacement if I have the hardware" bucket.
- **`llama3.2:3b` / `phi4:14b`** — general-purpose, not coder-tuned. Useful as a "fast" profile for short chat replies outside of code sessions.
- **`gpt-oss:20b`** / **`gpt-oss:120b`** (OpenAI OSS family) — release tags vary by vendor mirror; the 20 B is the hardware sweet spot on a 24 GB GPU. Emits a non-standard `reasoning` field on each choice's `message` / `delta` that the v0.5-C `openai_compat` adapter already strips; probe once with `coderouter doctor` to confirm the strip fires and `reasoning-leak` returns `OK`.

Use `coderouter doctor --check-model <provider>` after adding any new model — its six probes (auth / num_ctx / tool_calls / thinking / reasoning-leak / streaming) will tell you which `capabilities.*` flags and `extra_body.options.*` values the model actually wants. The doctor's verdicts are the source of truth; the table in §3 is the known-good starting point it checks you against.

MoE footprint reminder: "N total / M active" means the router streams like an M-parameter model in latency terms but needs N-parameter weights resident in VRAM. Qwen3 30B-A3B loads like a 30 B but runs like a 3 B — an unusually good deal on Apple Silicon where memory bandwidth is the bottleneck, but only if you actually have the ~18 GB Q4 weights-fit budget.

---

## 3. Tuning defaults per local model

The values below are known-good starting points. `coderouter doctor --check-model` will tell you when a model wants something different for your specific Ollama build.

Rows are grouped by family so you can find the row that matches the tag you pulled. Columns left of `output_filters` go into `extra_body.options` in your `providers.yaml`; `output_filters` and `capabilities.tools` are top-level on the provider. `—` means "no filter needed by default; confirm with `coderouter doctor`".

| Model | `num_ctx` | `num_predict` | `temperature` | `output_filters` | `capabilities.tools` |
|---|---:|---:|---:|---|:---:|
| **qwen2.5-coder (dense, coder-tuned)** | | | | | |
| `qwen2.5-coder:1.5b` | 8192 | 2048 | 0.2 | `[strip_thinking]` | false (too small to reliably tool-call) |
| `qwen2.5-coder:7b` | 32768 | 4096 | 0.2 | `[strip_thinking]` | true |
| `qwen2.5-coder:14b` | 32768 | 4096 | 0.2 | `[strip_thinking]` | true |
| `qwen2.5-coder:32b` | 32768 | 4096 | 0.2 | `[strip_thinking]` | true |
| **qwen3 family (coder + general, dense + MoE)** | | | | | |
| `qwen3-coder:7b` / `:14b` | 32768 | 4096 | 0.2 | `[strip_thinking, strip_stop_markers]` | true (verify w/ doctor) |
| `qwen3-coder:30b-a3b` (MoE, ~3 B active) | 65536 | 4096 | 0.2 | `[strip_thinking, strip_stop_markers]` | true (verify w/ doctor) |
| `qwen3:30b-a3b` (general MoE) | 32768 | 4096 | 0.2 | `[strip_thinking, strip_stop_markers]` + `append_system_prompt: "/no_think"` | verify w/ doctor |
| `qwen3:32b` (dense, general) | 32768 | 4096 | 0.2 | `[strip_thinking, strip_stop_markers]` + `append_system_prompt: "/no_think"` | verify w/ doctor (often false) |
| **DeepSeek (coder MoE + R1 distills)** | | | | | |
| `deepseek-coder-v2:16b` | 16384 | 4096 | 0.2 | `[strip_thinking]` | verify w/ doctor (often false) |
| `deepseek-r1:distill-qwen-14b` / `:distill-qwen-32b` | 16384 | 4096 | 0.2 | `[strip_thinking, strip_stop_markers]` | false (R1 distills rarely tool-call cleanly) |
| **Gemma 3 (Google, general multilingual)** | | | | | |
| `gemma3:4b` | 8192 | 2048 | 0.3 | — | false |
| `gemma3:12b` | 16384 | 4096 | 0.3 | — | false |
| `gemma3:27b` | 32768 | 4096 | 0.3 | — | false |
| **Gemma 4 (Google, multimodal + configurable thinking; 2026)** | | | | | |
| `gemma4:e2b` (edge, ~2 B effective) | 32768 | 4096 | 0.3 | `[strip_thinking]` | verify w/ doctor |
| `gemma4:e4b` (edge, ~4 B effective) | 32768 | 4096 | 0.3 | `[strip_thinking]` | verify w/ doctor |
| `gemma4:latest` (~9.6 GB pull, 128 K ctx) | 32768 | 4096 | 0.3 | `[strip_thinking]` | verify w/ doctor |
| `gemma4:26b` (MoE: ~4 B active / 26 B total, 256 K ctx) | 65536 | 4096 | 0.3 | `[strip_thinking, strip_stop_markers]` | verify w/ doctor |
| `gemma4:31b` (dense flagship) | 32768 | 4096 | 0.3 | `[strip_thinking, strip_stop_markers]` | verify w/ doctor |
| **Llama (Meta, general instruct)** | | | | | |
| `llama3.2:3b` | 8192 | 2048 | 0.3 | — | false |
| `llama3.3:70b` | 32768 | 4096 | 0.2 | — | true (verify w/ doctor; needs ≥ 48 GB VRAM or 64 GB+ unified) |
| **gpt-oss (OpenAI open-weights)** | | | | | |
| `gpt-oss:20b` | 32768 | 4096 | 0.2 | `[strip_thinking]` | true (verify w/ doctor) |
| `gpt-oss:120b` | 65536 | 4096 | 0.2 | `[strip_thinking]` | true (verify w/ doctor; needs ≥ 80 GB VRAM) |
| **Other** | | | | | |
| `phi4:14b` | 16384 | 4096 | 0.2 | — | false |
| HF-GGUF `hf.co/<user>/<repo>:<quant>` | 8192 | 4096 | 0.2 | `[strip_thinking, strip_stop_markers]` + `append_system_prompt: "/no_think"` | false by default (probe to confirm) |

The table is deliberately opinionated for the Claude-Code-through-CodeRouter path: `num_ctx` defaults err on the high side (Claude Code's per-turn system prompt is 15–20 K tokens, so 2048 is always wrong and 8192 is borderline), `temperature` sits at 0.2 for tool-call reliability, and `output_filters` is pre-seeded for families that leak `<think>` or stop-marker tokens. If a family you want isn't listed, **pick the closest row** (qwen3 ≈ qwen3-coder, gemma3 ≈ general dense without tool-use), then run `coderouter doctor --check-model <provider>` — the probe verdicts are the authoritative signal; this table is just a first draft so the doctor has something to compare against.

Why `temperature: 0.2`? Claude Code issues tool calls via structured JSON. Higher temperature (the Ollama default is 0.7) produces more creative phrasing and more malformed JSON. CodeRouter's v0.3-A tool-call repair handles common breakage, but prevention is cheaper than repair. This is one of the findings the [claude-code-local](https://github.com/nicedreamzapp/claude-code-local) project surfaced during its own tool-call reliability work — CodeRouter adopts the same default independently.

How the values plug into `providers.yaml`:

```yaml
providers:
  - name: ollama-qwen-coder-7b
    kind: openai_compat
    base_url: http://localhost:11434/v1
    model: qwen2.5-coder:7b
    timeout_s: 120
    output_filters: [strip_thinking]
    extra_body:
      options:
        num_ctx: 32768
        num_predict: 4096
        temperature: 0.2
    capabilities:
      tools: true
```

`num_ctx` and `num_predict` live inside `extra_body.options` because that's Ollama's native JSON shape for these knobs (passed through unmodified to `/v1/chat/completions`). `temperature` *can* also be a top-level field on the OpenAI-shape request, but declaring it inside `options` keeps all three tuning values in one block and Ollama honors it identically either way.

### Which knobs matter most

1. **`num_ctx`** is the #1 silent-fail source with Ollama. Default is 2048 tokens; Claude Code's system prompt alone is 15–20 K. If you see blank / gibberish replies with 200 status, this is almost always it. `coderouter doctor` v1.0-B detects this directly (canary echo-back).
2. **`num_predict`** bounds the *output* length. Default 128 on older Ollama, 256 on some forks. Claude Code replies that look truncated mid-sentence are usually this. `coderouter doctor` v1.0-C detects via a deterministic "count 1 to 30" streaming probe.
3. **`temperature`** is a quality knob, not a correctness knob. If tool-call repair fires a lot in your logs (`recover_garbled_tool_json` / v0.3-A), dropping temperature to 0.2 is the first fix.
4. **`output_filters`** removes `<think>` / `<|turn|>` / other stop-marker leaks from the response byte stream at the adapter boundary. Costs one pass over the content; works on every model, every provider, every client.

See [README.md → Ollama beginner — 5 silent-fail symptoms](../README.en.md#ollama-beginner--5-silent-fail-symptoms-v07-c) for the symptom→fix mapping in narrative form.

---

## 4. Ollama setup — the short version

CodeRouter does not install or wrap Ollama — it just speaks to its OpenAI-compat endpoint at `http://localhost:11434/v1`. Setup is:

```bash
# macOS
brew install ollama
brew services start ollama

# Linux
curl -fsSL https://ollama.com/install.sh | sh

# Windows
#   Download installer from https://ollama.com/download
#   Or, recommended: install inside WSL2 using the Linux command above.
```

Pull the models you declared in `providers.yaml`:

```bash
ollama pull qwen2.5-coder:7b
ollama pull qwen2.5-coder:14b
```

Confirm it's up:

```bash
curl http://localhost:11434/v1/models
# → {"object":"list","data":[{"id":"qwen2.5-coder:7b", ...}, ...]}
```

Environment knobs worth knowing:

- `OLLAMA_KEEP_ALIVE=30m` — how long a loaded model stays resident. Default 5 min is aggressive if you use the local tier intermittently.
- `OLLAMA_NUM_PARALLEL=2` — number of concurrent requests the server will batch. Raise if you run CodeRouter + another client against the same Ollama.
- `OLLAMA_FLASH_ATTENTION=1` — experimental attention optimization. Sometimes faster on Apple Silicon; measure before leaving it on.

Deeper Ollama setup topics (choosing a quantization, HF-GGUF loading, custom `Modelfile`, multi-GPU) live in Ollama's own docs at <https://github.com/ollama/ollama>. CodeRouter's concern stops at "the `/v1/*` endpoint is reachable and the tag exists".

---

## 5. Claude Code launch flow per OS

The pattern is the same on every OS: start CodeRouter in one terminal, then start Claude Code with two env vars pointing at it.

### macOS / Linux

Terminal 1 — start CodeRouter:

```bash
cd /path/to/CodeRouter
uv run coderouter serve --port 8088 --mode claude-code
```

Terminal 2 — start Claude Code pointed at CodeRouter:

```bash
ANTHROPIC_BASE_URL=http://localhost:8088 \
ANTHROPIC_AUTH_TOKEN=dummy \
claude
```

The `ANTHROPIC_AUTH_TOKEN` value is arbitrary — CodeRouter ignores it; it just needs to be non-empty so Claude Code doesn't complain.

### Windows — WSL2 (recommended)

Same as Linux. Run both terminals inside WSL2. Claude Code's npm install runs inside WSL2 too:

```bash
npm install -g @anthropic-ai/claude-code
```

### Windows — native PowerShell

```powershell
# Terminal 1
cd C:\path\to\CodeRouter
uv run coderouter serve --port 8088 --mode claude-code

# Terminal 2
$env:ANTHROPIC_BASE_URL = "http://localhost:8088"
$env:ANTHROPIC_AUTH_TOKEN = "dummy"
claude
```

Known caveats on Windows native: `scripts/verify_*.sh` require bash (use Git Bash or WSL2). The server itself and the `coderouter doctor` subcommand work identically to Linux.

### Verifying the bridge

From any OS, once CodeRouter is running:

```bash
curl http://localhost:8088/v1/messages \
  -H 'Content-Type: application/json' \
  -H 'anthropic-version: 2023-06-01' \
  -d '{
    "model": "claude-sonnet-4-6",
    "max_tokens": 64,
    "messages": [{"role":"user","content":"say hi"}]
  }'
```

A 200 response with content is all you need before pointing Claude Code at it.

---

## 6. OpenRouter free-tier pairing

OpenRouter hosts several free-tier models you can layer into your chain as mid-tier fallback between local and paid Claude. The default `claude-code` profile in `examples/providers.yaml` already uses two — pairing two *different vendors* gives rate-limit escape: when qwen hits its per-minute cap, gpt-oss is still available.

Currently shipped as free-tier references:

| Provider (YAML name) | Model | Best at | Caveat |
|---|---|---|---|
| `openrouter-free` | `qwen/qwen3-coder:free` | Long-context coding (262K window), tool-use | Daily quota; rate-limits around 20 req/min |
| `openrouter-gpt-oss-free` | `openai/gpt-oss-120b:free` | General chat, rate-limit escape from qwen | Emits non-standard `reasoning` field — v0.5-C strips it; harmless |

The roster rotates — see [`docs/openrouter-roster/CHANGES.md`](./openrouter-roster/CHANGES.md) for the weekly diff. New free models appear, old ones get pulled without warning. Re-run `scripts/openrouter_roster_diff.py` weekly (or let the cron in `scripts/` handle it) to track.

Set `OPENROUTER_API_KEY` before launch — OpenRouter free still requires auth:

```bash
export OPENROUTER_API_KEY=sk-or-v1-...    # get one at https://openrouter.ai/keys
uv run coderouter serve --port 8088
```

Pairing strategy that works in practice:

1. **Local first** (7b for speed) — 95 % of short edits hit this and never leave the box.
2. **Local second** (14b for quality) — catches the 5 % the small model botches.
3. **OpenRouter free** (qwen3-coder) — when both local fail (timeout / 5xx / overload), long-context is an advantage.
4. **OpenRouter free different vendor** (gpt-oss-120b) — rate-limit escape from qwen.
5. **Claude paid** (`ALLOW_PAID=true`) — last resort.

That's exactly the `claude-code` profile in `examples/providers.yaml`.

---

## 7. Verify it works (`doctor` + `verify`)

CodeRouter ships two verification tools:

**Per-provider diagnostic** — `coderouter doctor --check-model <provider>`. Runs the six-probe chain (auth / num_ctx / tool_calls / thinking / reasoning-leak / streaming) against one provider and prints a verdict table plus copy-paste YAML patches for any mismatch. Use after every `providers.yaml` edit.

```bash
uv run coderouter doctor --check-model ollama-qwen-coder-7b
```

**Full-system real-machine verify** — `bash scripts/verify_v1_0.sh`. Runs three paired bare/tuned scenarios end-to-end to prove the transformation + probe loop is closed. See `scripts/verify_v1_0.sh --help` for the scenario breakdown and [`docs/retrospectives/v1.0-verify.md`](./retrospectives/v1.0-verify.md) for the reference evidence doc.

The earlier v0.5 series also has its own verify runner at `scripts/verify_v0_5.sh` covering the capability-gate (`thinking` / `cache_control` / `reasoning`) surface. Both scripts are idempotent and safe to re-run.

---

## 8. Troubleshooting quick index

Short map to the right README section for each common symptom:

- **Blank / gibberish reply, 200 status** → [silent-fail symptom #1](../README.en.md#ollama-beginner--5-silent-fail-symptoms-v07-c) (`num_ctx` too low).
- **"Cut off mid-word"** → silent-fail symptom not numbered yet (`num_predict` too low). v1.0-C doctor probe detects it.
- **"Can't read files" / no tool calls** → silent-fail symptom #2 (`capabilities.tools` mismatch).
- **`<think>` tags in UI** → silent-fail symptom #3 (`output_filters: [strip_thinking]`).
- **First request always 404** → silent-fail symptom #4 (typo in `model:` or missed `ollama pull`).
- **All cloud providers 401** → silent-fail symptom #5 (`OPENROUTER_API_KEY` / `ANTHROPIC_API_KEY` unset).
- **`capability-degraded` log line** → expected observability; see README Troubleshooting.
- **`502 Bad Gateway: all providers failed`** → read the `provider-failed` log lines in order; the last `error` field explains why the chain bottomed out.

Everything else: `coderouter doctor --check-model <provider>` first, log lines second.

---

## 9. Security and supply chain

Secrets belong in env vars (`OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`), never in `providers.yaml` or `.env` files that can be committed by accident. The router defaults to binding `127.0.0.1`; don't expose `0.0.0.0` without a reverse proxy that enforces auth.

CI enforces `gitleaks` (secret scan), `pip-audit` + OSV-Scanner (CVE audit across two advisory feeds), `uv sync --frozen` (lockfile drift rejection), and a forbidden-SDK grep (no `anthropic` / `openai` / `litellm` / `langchain` in the runtime path). Dependabot proposes weekly bumps for both Python deps and GitHub Actions versions.

Full policy, threat model, and vulnerability reporting path: [`docs/security.en.md`](./security.en.md).

---

## 10. Attribution

Tuning defaults and the "temperature 0.2 for tool-call reliability" heuristic are informed by independent work on [claude-code-local](https://github.com/nicedreamzapp/claude-code-local) (Matt Macosko), which ran the same problem end-to-end on MLX-native Apple Silicon and documented the knobs that matter. CodeRouter takes a different implementation path — cross-platform OpenAI-compat router vs. Apple-only MLX-native server — but converged on the same tuning values. Where the two projects overlap, this guide credits prior art; where they differ (routing / fallback / bidirectional wire translation / declarative filter chain), the design is CodeRouter's own.
