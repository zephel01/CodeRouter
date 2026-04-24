# Troubleshooting — when things don't work

A single-page reference for "the router doesn't behave the way I expected" with CodeRouter.
Each symptom is laid out as: what's happening / what to type to confirm / how to fix it.

> **Stuck? Start here**: run `coderouter doctor --check-model <provider>` (see §0).
> If that doesn't help, search this page by symptom and jump to the relevant section.
>
> This page was split out of README §Troubleshooting (which carried the same content through v1.6.1).
> The old in-README anchor still resolves via a redirect link.

## Contents

- [0. First move: `coderouter doctor`](#0-first-move-coderouter-doctor)
- [1. Five startup / config gotchas (added in v1.6.2)](#1-five-startup--config-gotchas-added-in-v162)
- [2. Reading logs and common patterns](#2-reading-logs-and-common-patterns)
- [3. Ollama beginner — 5 silent-fail symptoms (v0.7-C)](#3-ollama-beginner--5-silent-fail-symptoms-v07-c)
- [4. Claude Code integration gotchas (added in v1.6.2)](#4-claude-code-integration-gotchas-added-in-v162)
- [5. `.env` security in practice (added in v1.6.3)](#5-env-security-in-practice-added-in-v163)
- [6. HF-on-Ollama reference profile](#6-hf-on-ollama-reference-profile)
- [Sister references](#sister-references)

---

## 0. First move: `coderouter doctor`

When you can't tell where the problem is, start by running `doctor` against the suspect provider:

```bash
coderouter doctor --check-model ollama-qwen-coder-7b
```

`doctor` runs six probes (auth + basic-chat / num_ctx / tool_calls / thinking / reasoning-leak / streaming) against the live provider and emits **copy-paste YAML patches** for any declaration mismatch.

Exit codes collapse into three buckets:

| Exit | Meaning | Next step |
|---|---|---|
| `0` | All probes OK | Config is healthy. Suspect the outside (client / network) |
| `2` | NEEDS_TUNING | Apply the emitted patch to `providers.yaml` |
| `1` | Fatal (auth fail / can't connect / etc.) | Check §1 / §4 startup & auth issues |

Run `doctor` from the **same shell that started the server** so env-var visibility matches.

---

## 1. Five startup / config gotchas (added in v1.6.2)

The class of "the server isn't even talking upstream correctly". If you see an upstream 401 like `Header of type 'authorization' was missing`, start here.

### 1-1. The CLI command is `serve`, the flag is `--mode`

Some old sample-YAML comments said `coderouter start --profile claude-code-nim`. The actual command is:

```bash
coderouter serve --mode claude-code-nim --port 8088
```

| Wrong | Right |
|---|---|
| `coderouter start ...` | `coderouter serve ...` (subcommand) |
| `--profile <name>` | `--mode <name>` (flag) |
| `--port` omitted | Default 4000. Match `--port 8088` (or whatever your `ANTHROPIC_BASE_URL` expects) |

`coderouter --help` / `coderouter serve --help` is authoritative.

### 1-2. `.env` requires `export`

As of v1.6.2 the project's `examples/.env.example` writes each key with `export`:

```bash
# OK — `source .env` propagates to child processes (coderouter serve / doctor)
export NVIDIA_NIM_API_KEY=nvapi-xxxxxxxxxxxxxxxx
export OPENROUTER_API_KEY=sk-or-v1-xxxxxxxxxxxxxxxx
```

```bash
# Wrong — only sets a shell-local variable; child processes see empty
NVIDIA_NIM_API_KEY=nvapi-xxxxxxxxxxxxxxxx
```

CodeRouter does **not auto-source `.env`**. Either `source .env` manually, or `set -a && source .env && set +a` to bulk-export plain `KEY=value` lines, before running `coderouter serve`.

### 1-3. Verifying that the env var actually reached the child

`echo` isn't enough — it shows both shell-local and exported vars. **`env` is the decisive test**:

```bash
env | grep -E 'NVIDIA|OPENROUTER|ANTHROPIC'
# → no row = not exported (= invisible to child processes)

# Confirm Python sees it too:
python3 -c "import os; print(len(os.environ.get('NVIDIA_NIM_API_KEY','')))"
# → 0 means the child can't see it; 70 means it's visible (NIM keys are ~70 chars)
```

### 1-4. `Header of type authorization was missing` 401

NIM, OpenRouter, Anthropic-direct — all of them get **no Authorization header at all** when the env var is empty. The CodeRouter code is intentionally simple:

```python
api_key = resolve_api_key(self.config.api_key_env)   # = os.environ.get(..., "").strip()
if api_key:
    headers["Authorization"] = f"Bearer {api_key}"
```

Empty `api_key` skips the `if`, so the header is never attached. NIM responds with "no auth header". When the entire chain fails this way, CodeRouter emits a `chain-uniform-auth-failure` WARN with `hint: probable-misconfig`.

Fix is §1-2 / §1-3.

### 1-5. `~/.zshrc` ignored

Depending on shell launch path, `.zshrc` may not be sourced (IDE-embedded terminals, `tmux`-spawned shells, `exec` chains).

```bash
# Reload manually and re-check in the same shell
source ~/.zshrc
env | grep NVIDIA_NIM_API_KEY

# If unsure where it actually lives, grep all the usual files
grep -rn NVIDIA_NIM_API_KEY ~/.zshrc ~/.zprofile ~/.bashrc ~/.bash_profile 2>/dev/null
```

Common causes: missing `export` keyword, line in `~/.zprofile` instead of `~/.zshrc`, etc.

---

## 2. Reading logs and common patterns

Thanks to v0.4-D, failed upstream requests now appear in the server log with the **exact upstream response body** attached. When a request fails, look for:

```
{"level": "WARNING", "msg": "provider-failed", "provider": "...",
 "status": 4xx, "retryable": true|false, "error": "[provider status=4xx] 4xx from upstream: {...}"}
```

Common patterns and what they mean:

- **`"Extra inputs are not permitted"` on a body field** — the upstream model (usually Anthropic) rejected a field it doesn't know. If the field is gated behind an `anthropic-beta` header (`context_management`, newer `cache_control` / `thinking` variants), check that the client actually set the header. CodeRouter forwards it verbatim as of v0.4-D, but if the client never sent one, no header will reach upstream.
- **`"adaptive thinking is not supported on this model"`** — as of v0.5-A this should no longer reach the user. The capability gate routes `thinking: {type: enabled}` requests to providers whose model accepts the field (heuristic: `claude-opus-4-*` / `claude-sonnet-4-6` / `claude-sonnet-4-7` / `claude-haiku-4-*`), and strips the block when the chain only has incapable providers. If you still see this error, either (a) your chain has a newer Anthropic family that isn't in the heuristic yet — set `capabilities.thinking: true` on that provider to opt in explicitly, or (b) file an issue with the model slug so the heuristic can be updated. Check the server log for `capability-degraded` lines to confirm the gate is firing.
- **`capability-degraded` log with `reason: "non-standard-field"` and `dropped: ["reasoning"]`** (v0.5-C) — the upstream model returned an OpenAI-spec-non-compliant `reasoning` field on a choice's `message` / `delta`. Some OpenRouter free models (notably `openai/gpt-oss-120b:free`) do this. The adapter strips the field before handing the response downstream, so this log is purely observational — nothing is broken. If you actually want the reasoning text passed through (e.g. you're fronting a reasoning-aware client), set `capabilities.reasoning_passthrough: true` on that provider and the strip turns off. Streaming: the log fires at most once per stream regardless of how many chunks carried the field.
- **`capability-degraded` log with `reason: "translation-lossy"` and `dropped: ["cache_control"]`** (v0.5-B) — your request carried a `cache_control` marker but the chosen provider is `kind: openai_compat`, so the marker was dropped during Anthropic → OpenAI translation. This is not an error (the request still succeeds), but Anthropic prompt caching will not kick in on that provider. Fix by either (a) putting a `kind: anthropic` provider earlier in the chain, or (b) if a future `openai_compat` upstream preserves cache markers, set `capabilities.prompt_cache: true` on that provider to opt out of the log. Note also the Anthropic-side 1024-token minimum: system prompts shorter than that report `cached_tokens: 0` even on supported providers — that's an upstream constraint, not a CodeRouter bug.
- **`rate_limit_error` / 429** — Anthropic org-level TPM cap. This is retryable (the engine will try the next provider); adjust profile order or lower Claude Code's context with `/compact`.
- **`unknown profile 'xxx'` (400)** — the `profile` field in the request body or `X-CodeRouter-Profile` header doesn't match any `profiles[].name` in your config. The response body shows the valid names.
- **`502 Bad Gateway: all providers failed`** — every provider in the chain returned a retryable error. Inspect the `provider-failed` log lines in order; the last `error` field shows why the chain bottomed out.
- **`chain-uniform-auth-failure` + `hint: probable-misconfig`** — every provider died with the same 401/403. Env var or API key misconfiguration; see §1-2 / §1-3.

Mid-stream failures surface as a single `event: error` with `type: api_error` inside the SSE stream (no 5xx HTTP status — headers have already shipped). This is distinct from "no provider could start" which emits `type: overloaded_error`.

---

## 3. Ollama beginner — 5 silent-fail symptoms (v0.7-C)

"I pointed the router at a fresh Ollama install and something is off" is by far the most common onboarding failure. The symptoms almost never look like errors — they look like the model shrugged. Here are the five we've collected in practice, with the one-liner that diagnoses each and the YAML patch that fixes it. `<provider>` below is the provider name from `providers.yaml`, e.g. `ollama-qwen-coder-7b`.

**1. Blank / gibberish reply even though the provider returned 200.** Ollama's default `num_ctx` is 2048 tokens. Claude Code's system prompt alone is 15–20K tokens per turn, so everything after the first 2048 gets silently dropped from the **front** of the prompt — tool definitions, task description, everything. The model replies from the leftover tail.

```bash
coderouter doctor --check-model <provider>
# → num_ctx: NEEDS_TUNING — canary missing from reply; upstream truncated
#   (no `extra_body.options.num_ctx` declared, Ollama default is 2048)
```

```yaml
# providers.yaml — patch suggested by doctor:
- name: <provider>
  extra_body:
    options:
      num_ctx: 32768    # or 16384 if you need to save VRAM
```

As of **v1.0-B** the doctor probe detects this directly — it sends a canary token at the front of a ~5K-token prompt and asks the model to echo it back. If the canary is missing, Ollama truncated the front of the prompt. The probe fires only for Ollama-shape providers (port 11434 in the base URL, or a declared `extra_body.options.num_ctx`), so other `kind: openai_compat` upstreams SKIP it silently.

**2. Claude Code keeps saying "I can't read files".** The model received the `tools` parameter, got confused, and returned an empty assistant message. Small quantized models (≤ 7B, Q4) frequently can't handle tool specs at all. CodeRouter's v0.3-A tool-call repair can recover *malformed* tool JSON, but this case is "model never attempted a tool call" — nothing to repair.

```bash
coderouter doctor --check-model <provider>
# → tool_calls: NEEDS_TUNING — model returned no tool_use and registry says tools=true
```

```yaml
# providers.yaml — patch suggested by doctor:
- name: <provider>
  capabilities:
    tools: false    # observed: model returned no tool_use block
```

With `tools: false` the chain moves on to the next provider when a tool-heavy request arrives. Pair this with a stronger model later in the chain (e.g. qwen2.5-coder:14b or a cloud fallback).

**3. `<think>...</think>` tags leak into the UI.** Qwen3-distilled models, DeepSeek-R1 distills, and some HF GGUF variants emit chain-of-thought inside the regular content channel (not an Anthropic `thinking` block). The tags land in Claude Code's terminal verbatim.

```bash
coderouter doctor --check-model <provider>
# → reasoning-leak: NEEDS_TUNING — observed `<think>` in content,
#   provider has no `output_filters` declared
```

As of **v1.0-A** the doctor probe emits a ready-to-apply filter patch. Two independent remediations — pick either or both:

```yaml
# providers.yaml — output-side scrub (v1.0-A, always works, recommended):
- name: <provider>
  output_filters: [strip_thinking]
  # Add `strip_stop_markers` too if you also see <|turn|> / <|channel>thought / ...
```

```yaml
# providers.yaml — source-side opt-out (cheap when the model honors it;
# Qwen3 / R1-distill families respect `/no_think`):
- name: <provider>
  append_system_prompt: "/no_think"
```

`output_filters` operates on the byte stream at the adapter boundary, so it works on every model — at the cost of one pass over the content. The two can be layered; the sample `ollama-qwen-coder-*` profiles in `examples/providers.yaml` ship with `output_filters: [strip_thinking]` enabled.

**4. First request to the chain always fails, then recovers.** The `model` field in `providers.yaml` has a typo or you forgot `ollama pull <tag>`. Ollama returns `404 model not found`, which is classified as retryable (bug fix from v0.2-x), so the chain falls through — but you lose the local-tier latency advantage on every turn.

```bash
coderouter doctor --check-model <provider>
# → auth+basic-chat: UNSUPPORTED — 404 from upstream (run `ollama pull <tag>`)
# → (remaining probes SKIP — no point running them until the model exists)
```

Fix: either `ollama pull <the-tag-in-your-YAML>` or correct the typo. The 404 is Ollama's way of saying "I have no GGUF with that tag loaded". Note that HF-on-Ollama model names are required to include the `:Q4_K_M`-style quant suffix — omitting it yields the same 404.

**5. Every provider in the chain fails uniformly.** Your `OPENROUTER_API_KEY` / `ANTHROPIC_API_KEY` isn't set (or is expired), and every cloud provider in the chain 401s in turn. As of v0.5.1 A-3 there's a `chain-uniform-auth-failure` WARN that identifies this pattern after the fact, but it's easier to catch before traffic starts.

```bash
coderouter doctor --check-model <the-cloud-provider>
# → auth+basic-chat: AUTH_FAIL — 401 from upstream (check env var <KEY_NAME>)
# → (remaining probes SKIP — auth dominates)
```

Fix: set the env var (see §1-2, `export` required), or add it to the `.env` loaded at server start (`cp examples/.env.example .env`). `coderouter doctor` reads the same env the running server would, so a successful probe from your shell is a reliable signal that the server will work too.

**Run the full set** with one `doctor` per provider:

```bash
for p in ollama-qwen-coder-7b ollama-qwen-coder-14b openrouter-free openrouter-gpt-oss-free; do
  coderouter doctor --check-model "$p" || true
done
```

Exit codes collapse into three buckets (0 clean / 2 patchable / 1 blocker) so the loop above can be wired into CI.

---

## 4. Claude Code integration gotchas (added in v1.6.2)

The class where doctor passes everything, no 401 / 5xx in the CodeRouter log, but Claude Code on top behaves strangely.

### 4-1. Greetings get rewritten as tool calls (Llama-3.3-70B-class)

```
❯ hello
⏺ Skill(hello)
  ⎿  Initializing…
  ⎿  Error: Unknown skill: hello. Did you mean help?
```

…or Claude Code volunteers an `AskUserQuestion`-style elicitation widget out of nowhere ("What is your name? 1. John / 2. Jane / ...").

**Cause**: the backend model is over-reacting to Claude Code's tool / skill declarations in the system prompt and trying to route **every user utterance** through some tool. Llama-3.3-70B exhibits this strongly (its agentic tuning is aggressive).

**Fix**: put an agentic-coding-specialized model at the head of the profile.

```yaml
# providers.yaml — reorder so an agentic-friendly model leads:
profiles:
  - name: claude-code-nim
    providers:
      - nim-qwen3-coder-480b   # ← first choice
      - nim-kimi-k2            # second
      - nim-llama-3.3-70b      # ← demoted to tail fallback
      - openrouter-free
      - ...
```

`examples/providers.nvidia-nim.yaml` (v1.6.2 onwards) ships with the Qwen-first ordering. Llama-3.3-70B works fine for many things, but for Claude Code chat traffic specifically, Qwen3-Coder-480B / Kimi-K2 are operationally more stable.

### 4-2. `UserPromptSubmit hook error` (third-party Claude Code plugins)

```
❯ hello
  ⎿  UserPromptSubmit hook error - Failed with non-blocking status code: No stderr output
```

**Cause**: a third-party Claude Code plugin (e.g. `claude-mem@thedotmack`) is making its own internal LLM call inside the `UserPromptSubmit` hook. That call expects the **real Anthropic API**, but `ANTHROPIC_BASE_URL=http://localhost:8088` now points it at CodeRouter (→ a local LLM), which can't parse the plugin's bespoke prompt and dies silently.

This is a structural mismatch on the plugin side, not a CodeRouter bug.

**Fix**: bisect by temporarily disabling the plugin in `~/.claude/settings.json`:

```jsonc
{
  "enabledPlugins": {
    "claude-mem@thedotmack": false   // ← temporarily disable
  }
}
```

If the error vanishes, it's the plugin. The proper fix is upstream — file feedback asking the plugin author to support OpenAI-compat / non-Anthropic backends.

### 4-3. "Compacting conversation…" takes ages

```
✻ Compacting conversation… (34s)
```

Claude Code's auto-compact (summarize old turns to compress context) being slow just means **the backend model is slow at the summarization task**. CodeRouter is not in the loop. Llama-3.3-70B is on the slow side here too; switching to Qwen3-Coder-480B or a local Ollama model improves it.

`DISABLE_CLAUDE_CODE_SM_COMPACT=1` disables the LLM-based smart compact, but a truncate-based fallback still runs. Manual `/compact` / `/clear` is the most reliable workaround.

### 4-4. Open the dashboard, every gotcha becomes visible in 10 seconds

Open `http://localhost:8088/dashboard` in a separate tab while you work. All of the above become directly visible:

- Which provider responded / where the chain fell through (RECENT EVENTS panel)
- `Capability degraded` fire counts (FALLBACK & GATES panel)
- `chain-uniform-auth-failure` events (`failed` column accumulates)

It's 10× faster than `grep`-ing the log. Recommended runtime habit while driving Claude Code.

---

## 5. `.env` security in practice (added in v1.6.3)

`.env` files contain plaintext API keys. Even for local development, the four points below are the minimum baseline; CodeRouter v1.6.3 ships scaffolding for each.

### 5-1. Threat model — what's defended, what isn't

| # | Threat | Helped by at-rest encryption? | What CodeRouter v1.6.3 supports |
|---|---|---|---|
| 1 | Accidental `git add .env && push` | △ rotation is still required after the push | `coderouter doctor --check-env` catches `.gitignore` and tracking issues before they fire |
| 2 | Stolen laptop / cloned disk image | ◎ | OS-level full-disk encryption (FileVault / dm-crypt / BitLocker) is the right layer; in-app encryption is strictly worse |
| 3 | Other users on the same OS reading `ps` / `/proc/$pid/environ` | ✕ post-decrypt env is fully visible | `--check-env` enforces 0600 file mode |
| 4 | `swap` / coredump / shell history | ✕ same | Don't put keys on the command line — use `--env-file` or an external secret manager |

Rolling our own AES would only address threats 2/6 (backups), and the decryption key still has to live somewhere. The v1.6.3 stance is to integrate with the tools that already solve this well (1Password, sops, OS Keychain, direnv) by handing CodeRouter the env via `--env-file`.

### 5-2. Quick checklist

```bash
# 1. Filesystem mode (owner-only)
chmod 0600 .env

# 2. Listed in .gitignore
echo '.env' >> .gitignore
git check-ignore -q .env && echo "ignored OK"

# 3. Not currently tracked by git
git ls-files --error-unmatch .env 2>/dev/null && echo "ALREADY TRACKED — rotate keys!"

# All three at once
coderouter doctor --check-env .env
```

`doctor --check-env` runs four checks (existence / permissions / .gitignore / git-tracking) and prints `OK` / `WARN` / `ERROR` per row. WARN entries come with a copy-paste fix command (`chmod 0600 ...` / `echo '.env' >> .gitignore`); ERROR (i.e. already tracked) emits a multi-step remediation including `git rm --cached`.

### 5-3. 1Password CLI integration (recommended)

Keep `.env` off disk entirely; have 1Password inject secrets into the env at process spawn. Works on macOS / Linux / Windows. Only `.env.tpl` (the template) lives in git.

**Setup**:

```bash
brew install 1password-cli   # macOS
# or follow https://developer.1password.com/docs/cli/get-started

op signin
```

**`.env.tpl`** — secret references instead of values:

```bash
export NVIDIA_NIM_API_KEY=op://Personal/NVIDIA NIM/credential
export OPENROUTER_API_KEY=op://Personal/OpenRouter/credential
export ANTHROPIC_API_KEY=op://Personal/Anthropic/credential
```

**Run** — `op run` resolves `op://` references and exports the values:

```bash
op run --env-file=.env.tpl -- coderouter serve --mode claude-code-nim --port 8088
```

Why this works well:

- `.env.tpl` is safe to commit (no secrets inside)
- No plaintext `.env` ever touches disk (defeats threats 2 and 6 outright)
- Per-team-member Vaults make offboarding a Vault swap, not a key rotation
- CodeRouter sees a normal exported env — `--env-file` is not involved

> **Use `coderouter serve --env-file <path>` when you do want a file in the loop** — for example, when piping 1Password output into a temporary file, or when integrating with direnv / sops which deliver the env via files. Direct `op run` injection doesn't need it.

### 5-4. direnv + sops for git-tracked encrypted secrets

When the team needs the secrets visible in git history (auditable, reviewable, recoverable from backup), encrypt with sops and let direnv decrypt them on `cd`:

```bash
# 1. Tooling
brew install sops age

# 2. Generate an age key (~/.config/sops/age/keys.txt)
age-keygen -o ~/.config/sops/age/keys.txt

# 3. Encrypt with the public key — safe to commit
sops -e --age <PUBLIC_KEY> .env > .env.enc
echo '.env' >> .gitignore
echo '!.env.enc' >> .gitignore

# 4. .envrc — direnv runs this on cd
cat > .envrc <<'EOF'
eval "$(sops -d .env.enc)"
EOF
direnv allow .

# 5. cd is now enough
cd ~/works/CodeRouter   # direnv decrypts in the background
coderouter serve --port 8088
```

CodeRouter just reads from `os.environ` as usual; `--env-file` is again not needed.

### 5-5. OS Keychain (macOS / Linux libsecret / Windows Credential Manager)

For users who don't want to install 1Password or who prefer the OS-native path:

**macOS**:

```bash
# Save the key once (interactive prompt for the value)
security add-generic-password -s NVIDIA_NIM_API_KEY -a $USER -w

# Pull it into env at startup
export NVIDIA_NIM_API_KEY=$(security find-generic-password -s NVIDIA_NIM_API_KEY -a $USER -w)
coderouter serve --port 8088
```

Drop the two lines into `~/.zshrc` and every new shell auto-resolves. Touch ID / password prompts may appear — that's an extra security layer, not an annoyance.

**Linux (libsecret)**:

```bash
secret-tool store --label='NVIDIA NIM' service NVIDIA_NIM_API_KEY

export NVIDIA_NIM_API_KEY=$(secret-tool lookup service NVIDIA_NIM_API_KEY)
coderouter serve --port 8088
```

### 5-6. Layering `--env-file` (intermediate)

`coderouter serve --env-file <path>` accepts the flag multiple times, applied left-to-right with **first occurrence wins**. **File values do NOT override variables already in the environment** by default (flip with `--env-file-override`). This makes layering ergonomic.

```bash
# Global defaults overridden by project-local values
coderouter serve \
  --env-file ~/.coderouter/global.env \
  --env-file ./project.env \
  --port 8088
```

```bash
# 1Password + project-local overrides
op run --env-file=.env.tpl -- \
  coderouter serve \
    --env-file ./project-local-overrides.env \
    --port 8088
```

When `--env-file` runs, CodeRouter logs the **names** (never values) of the keys it actually applied:

```
serve: --env-file ./project.env: loaded 2 variable(s): CODEROUTER_MODE, OPENROUTER_API_KEY
```

Values are never written to stdout / stderr — secrets must not leak through logs.

### 5-7. Minimize key scope (cheaper / shorter-lived keys)

Pre-encryption hygiene that's often more effective than encryption itself:

- **NVIDIA NIM**: per-key usage caps and expiry ([build.nvidia.com/account/keys](https://build.nvidia.com/account/keys)). Reserve unrestricted keys for production, cap dev keys at e.g. 100 requests/month.
- **OpenRouter**: scope a key to `:free` SKUs only — even a leaked key can't cost you anything.
- **Anthropic**: rotate monthly via the console; shrinks the leak window.

A short-lived, narrowly-scoped key is better than a long-lived encrypted one in most threat models.

---

## 6. HF-on-Ollama reference profile

When you run HF-hosted GGUFs through Ollama's `hf.co/<user>/<repo>:<quant>` loader, all 5 symptoms in §3 amplify — HF GGUFs frequently ship without chat templates, inherit `<think>` tags from their distillation source, and require the `:<quant>` suffix that's so easy to forget (symptom 4).

`examples/providers.yaml` carries a commented-out `ollama-hf-example` stanza that exemplifies each knob (`extra_body.options.num_ctx`, `append_system_prompt: "/no_think"`, `capabilities.tools: false`, `reasoning_passthrough`) with inline comments mapping each to its corresponding symptom. Copy it, replace `model:` with your pulled HF tag, and validate with `coderouter doctor --check-model ollama-hf-example`.

---

## Sister references

If you run both CodeRouter (the router layer) and [lunacode](https://github.com/zephel01/lunacode) (the editor harness) against the same local Ollama, lunacode's [`docs/MODEL_SETTINGS.md`](https://github.com/zephel01/lunacode/blob/main/docs/MODEL_SETTINGS.md) is the sister reference — it covers the same five symptoms at the editor / harness layer (per-model settings, chat template overrides, `/no_think` variants) where CodeRouter's provider-granularity declarations stop.

For NIM / OpenRouter free-tier strategy see [`docs/free-tier-guide.en.md`](./free-tier-guide.en.md), for the meaning of each config knob see [`docs/usage-guide.en.md`](./usage-guide.en.md), and for the shortest-path setup see [`docs/quickstart.en.md`](./quickstart.en.md).
