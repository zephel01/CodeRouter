# Quickstart — get it running in one sitting

> This guide is intentionally minimum-effort. Background on *why* each setting exists lives in [usage-guide.md](./usage-guide.md).

You'll have Claude Code or the codex CLI talking to a local Ollama stack for **$0** in about 10–15 minutes. The fallback chain is shared between the two patterns; only the last step differs per agent.

**Topology**

```
Claude Code / codex  →  CodeRouter (localhost:8088)
                            ├─ ① ollama qwen2.5-coder:7b   (local, primary)
                            ├─ ② ollama qwen2.5-coder:1.5b (local, lightweight fallback)
                            └─ ③ OpenRouter qwen3-coder:free (free cloud, last resort)
```

Paid APIs are **never** called unless you explicitly set `ALLOW_PAID`. Both macOS and Linux follow the same steps.

---

## Prerequisites

- Python 3.12+ (`python3 --version` to check)
- `git`
- ~6 GB free disk (7b ≈ 4.7 GB, 1.5b ≈ 1 GB)
- Ollama installed, or use the command in step 1 below

If you want the free-tier cloud escape hatch, create a free account at [openrouter.ai](https://openrouter.ai/) and issue one API key (optional — you can skip and stay fully local).

---

## Common setup (6 steps shared by Pattern A/B)

### 1. Install Ollama and pull two models

```bash
# Same command on macOS / Linux
curl -fsSL https://ollama.com/install.sh | sh

# Pull the models (~6 GB total, 5–15 min depending on your bandwidth)
ollama pull qwen2.5-coder:7b
ollama pull qwen2.5-coder:1.5b

# Start the Ollama service (auto-starts on macOS, systemd on Linux)
ollama serve &   # skip if it's already running
```

### 2. Install CodeRouter

Two paths depending on how you plan to use it. If you **just want to put CodeRouter in front of Claude Code / codex**, path (a) — `uv tool install` — is shortest. Pick path (b) — clone + venv — if you want to read the source or edit `auto_router:` rules locally.

> On 2026-era macOS (Homebrew Python), Ubuntu 23+, and Debian bookworm+, PEP 668 blocks bare `pip install` into the system Python. Paths (a) and (b) both sidestep that.

**(a) Just want to use it — one-shot `uv tool install`**

```bash
# Install uv if you don't have it yet
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install CodeRouter into an isolated tool env (puts `coderouter` on PATH)
uv tool install --from git+https://github.com/zephel01/CodeRouter.git coderouter
```

`pipx` works too: `pipx install git+https://github.com/zephel01/CodeRouter.git`. If you took path (a), grab the example config separately in step 3:

```bash
# Fetch just the example you need
curl -fsSL -o ~/.coderouter/providers.yaml \
  https://raw.githubusercontent.com/zephel01/CodeRouter/main/examples/providers.yaml
```

**(b) Want to read source / tweak `auto_router:` rules — clone + venv**

```bash
git clone https://github.com/zephel01/CodeRouter.git
cd CodeRouter
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e .
```

Every terminal that runs `coderouter serve` needs `source .venv/bin/activate` first (direnv or a shell-init hook works too if you want it automatic).

### 3. Drop in a `providers.yaml`

Copying the sample is enough — its contents match the topology diagram above.

```bash
mkdir -p ~/.coderouter
# Path (b) = you cloned the repo
cp examples/providers.yaml ~/.coderouter/providers.yaml

# Path (a) = uv tool install — fetch the sample directly
# curl -fsSL -o ~/.coderouter/providers.yaml \
#   https://raw.githubusercontent.com/zephel01/CodeRouter/main/examples/providers.yaml
```

### 4. (Optional) Set an OpenRouter API key

Skip this if the two local models are enough. Only needed when you want the free cloud escape hatch:

```bash
export OPENROUTER_API_KEY="sk-or-v1-xxxxxxxxxxxxxxxx"
```

Add the same line to `~/.zshrc` / `~/.bashrc` if you want it to persist.

### 5. Start CodeRouter

```bash
coderouter serve --port 8088
```

From a second terminal, confirm it's up:

```bash
curl http://localhost:8088/healthz
# → {"status":"ok"}
```

Optional browser check: http://localhost:8088/dashboard
(`/healthz` and `/dashboard` live on the same port. If you changed `--port`, match it.)

### 6. `coderouter doctor` sanity check (optional but recommended)

```bash
coderouter doctor --check-model ollama-qwen-coder-7b
```

An `OK` means the common gotchas for Claude Code / codex are already clear.

---

## Pattern A: use it with Claude Code

### A-1. Install Claude Code

```bash
npm install -g @anthropic-ai/claude-code
```

### A-2. Point Claude Code at CodeRouter

```bash
export ANTHROPIC_BASE_URL="http://localhost:8088"
export ANTHROPIC_AUTH_TOKEN="dummy"   # CodeRouter ignores auth; any placeholder works
```

### A-3. Start it

```bash
claude
```

The `claude-code` profile (local 2-tier + free cloud) is selected automatically. To confirm Ollama is actually answering, run `coderouter stats --once` from another terminal, or watch the Providers panel on the dashboard — `ollama-qwen-coder-7b` should stay green.

---

## Pattern B: use it with the codex CLI

### B-1. Install codex

```bash
npm install -g @openai/codex
```

### B-2. Point codex at CodeRouter

```bash
export OPENAI_BASE_URL="http://localhost:8088/v1"
export OPENAI_API_KEY="dummy"   # placeholder is fine (same as above)
```

### B-3. Run it

```bash
codex "write a python function that reverses a string"
```

Same backend chain, just spoken to in OpenAI shape. The `default` profile (local 7b + free cloud) is selected.

---

## Troubleshooting (the three you'll hit)

### (1) `coderouter serve` fails with `address already in use`

Something else is holding port 8088. Either free it or pick a different port:

```bash
lsof -i :8088        # see who's holding it
coderouter serve --port 8089   # sidestep it
```

If you change ports, update `ANTHROPIC_BASE_URL` / `OPENAI_BASE_URL` to match.

### (2) Claude Code / codex hangs with no reply

Usually Ollama itself isn't running:

```bash
curl http://localhost:11434/api/version
# → {"version":"0.x.x"}
```

If that doesn't connect, restart `ollama serve`. If it *does* connect, `coderouter doctor --check-model ollama-qwen-coder-7b` will pinpoint the issue.

### (3) Responses contain stray `<think>...</think>` blocks

Check that `providers.yaml` has `output_filters: [strip_thinking]` set on the model entry. The sample ships with it enabled.

### (4) `pip install` errors with `externally-managed-environment`

This is PEP 668 blocking bare `pip install` into the system Python on macOS (Homebrew), Ubuntu 23+, and Debian bookworm+. Switch to either path (a) (`uv tool install`) or path (b) (venv then `pip install -e .`) from step 2. Forcing it with `--break-system-packages` is discouraged — it will eventually break your OS-managed Python environment.

---

## Bonus: let CodeRouter pick the profile (v1.6 `auto_router`)

In Pattern A/B each client is pinned to a fixed profile (`claude-code` or `default`). Starting in v1.6 you can instead have CodeRouter read the request body — image attached / code-fence-heavy / plain prose — and pick a profile per request. This is the shortest path if you **just want it to work without thinking about profile names**.

### C-1. Pull three models

The default `auto_router` ruleset assumes three profiles (`multi` / `coding` / `writing`). The coder model is already in place from step 1; add the general and vision variants:

```bash
ollama pull qwen2.5:7b           # writing  (~4.7 GB)
ollama pull qwen2.5vl:7b         # multi    (~6 GB — skip if you never send images)
# qwen2.5-coder:7b was pulled in common-setup step 1
```

Skipping `qwen2.5vl:7b` is fine if you never send images. Image requests will then fail cleanly (fast-fail), and text requests keep working.

### C-2. Swap `providers.yaml` for `providers.auto.yaml`

```bash
cp examples/providers.auto.yaml ~/.coderouter/providers.yaml
```

The load-bearing bits are just two lines:

```yaml
default_profile: auto   # ← this sentinel turns auto_router on
# profiles: must declare multi / coding / writing
```

Restart `coderouter serve` and every subsequent request is classified by the bundled ruleset (image attached → `multi` / code-fence ratio ≥ 0.3 → `coding` / else → `writing`). Per-request overrides (`X-CodeRouter-Profile` header or `body.profile`) still win, so you can mix "let it decide normally, but pin this one request" freely.

### C-3. Customizing the rules

When you want your own rules ("translate requests go to writing", "anything containing 'Review this PR' goes to coding", …), copy `examples/providers.auto-custom.yaml` and edit the `auto_router:` block. Two things to keep in mind:

- When `auto_router:` is present, the bundled rules are **not** merged — your list replaces them entirely (first match wins).
- Each `match:` block must declare exactly one matcher (`has_image` / `code_fence_ratio_min` / `content_contains` / `content_regex`). Mixing matchers in one rule fails at startup.

---

## What to read next

- [usage-guide.md](./usage-guide.md) — per-setting meaning, multi-provider tuning, full doctor diagnostic catalog
- [security.md](./security.md) — caveats when opting into paid APIs
- [README.md](../README.md) § "Do I actually need CodeRouter?" — decision flow for whether this fits your use case
