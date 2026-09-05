# External Coding Agent CLI (agent_cli)

> 日本語版: [`external-agents.md`](./external-agents.md)

`kind: "agent_cli"` is an adapter that registers an external coding-agent CLI, such as the Claude Code CLI, as a single CodeRouter provider. It was newly added in v2.7.7 (Phase 1a: claude), v2.7.8 added grok (Phase 1d), v2.7.9 added codex (Phase 1b), and v2.7.10 added antigravity (Phase 1c). **Phase 1 (all four backends) is now complete.** See [`docs/designs/external-agents-adapter.md`](../designs/external-agents-adapter.md) for the full design.

> **Breaking change in v2.9.0 (agent_cli plugin extraction, Phase 2c)** — the in-core `agent_cli` adapter was removed in v2.9.0. Using `kind: "agent_cli"` now **requires** installing the external plugin **`coderouter-plugin-agents`** and adding `plugins.enabled: [agents]` to `providers.yaml`. Through v2.8.x there was a transition period (Phase 2b) where the in-core implementation still won; that grace period ended with v2.9.0. See the [Quickstart](#quickstart) below for the exact steps. Without the plugin, a `kind: agent_cli` provider now makes `coderouter-t serve` fail at startup with an error that includes a targeted migration hint; `coderouter doctor` also detects and warns about the same misconfiguration. The `agent_cli:` sub-config schema (`AgentCliConfig`) itself and existing provider entries are unchanged — nothing there needs editing.

---

## Overview

A coding-agent CLI is normally a stateful control loop that runs many turns autonomously while editing files — a poor fit for CodeRouter's "one request = one transformation" ethos. `agent_cli` reconciles the two by collapsing the CLI into a **one-shot `exec`** (prompt in → final answer text out). Orchestration (multi-turn control, tool execution) stays entirely inside the agent CLI process; from CodeRouter's side it looks like just another provider that answers a single exchange.

- **Supported CLIs**: the `agent` field can declare `claude` / `codex` / `antigravity` / `grok`. `gemini` also remains in the schema's `Literal`, but it is always rejected (see below).
- **Implementation status (as of v2.7.10)**: **Phase 1 is complete — `claude` (Claude Code CLI, Phase 1a), `codex` (OpenAI Codex CLI, Phase 1b), `antigravity` (Google Antigravity CLI, Phase 1c), and `grok` (Grok CLI, Phase 1d) are all implemented**. `gemini` fell out of scope because Google discontinued the Gemini CLI for individual accounts in June 2026; constructing the adapter with it always raises a dedicated migration error:
- **Prerequisite as of v2.9.0**: the adapter bodies for the four backends above moved to `coderouter-plugin-agents` in Phase 2b (v2.8.1), and the in-core copy was removed in Phase 2c (v2.9.0). As of v2.9.0, using `kind: agent_cli` therefore requires installing `coderouter-plugin-agents` and setting `plugins.enabled: [agents]`. The `agent_cli:` sub-config (`AgentCliConfig`), each CLI's argv/behavior, and how provider entries are written are all unchanged (see the per-CLI sections below).

  ```
  AdapterError: Google discontinued Gemini CLI for individual accounts (June 2026).
  Use agent='antigravity' instead.
  ```

  This rejection is `retryable=False` — even if other providers exist in the fallback chain, it stops immediately as a configuration error. See [Gemini's discontinuation and the move to antigravity](#geminis-discontinuation-and-the-move-to-antigravity) for the full story.

- The shared parts of this document (authentication design, configuration reference, limitations) are written against the `claude` target; codex-, antigravity-, and grok-specific behavior are collected in the [codex (OpenAI Codex CLI)](#codex-openai-codex-cli), [antigravity (Google Antigravity CLI)](#antigravity-google-antigravity-cli), and [grok (Grok CLI)](#grok-grok-cli) sections, respectively.

### Gemini's discontinuation and the move to antigravity

The legacy Gemini CLI (`@google/gemini-cli` 0.50.x) has had individual-account OAuth **discontinued as of 2026-06-18** (per Google's official blog). On real hardware this now fails with:

```
IneligibleTierError: This client is no longer supported for Gemini Code Assist for individuals.
To continue using Gemini, please migrate to the Antigravity suite of products: https://antigravity.google
```

(`reasonCode: UNSUPPORTED_CLIENT`, `tierId: free-tier`)

The legacy gemini CLI was also hit during this adapter's field verification by its trusted-directory gate returning **exit 55** (requiring `--skip-trust` or `GEMINI_CLI_TRUST_WORKSPACE=true`) — a pre-existing constraint unrelated to the individual-account discontinuation.

Google's stated successor is the **Antigravity CLI** (command name `agy`). It is not a fork of gemini-cli but a separate Go rewrite, and it is where individual Google-account OAuth (including the free tier) lives on. In response, CodeRouter implemented the originally-planned Phase 1c (`agent: "gemini"`) as **`agent: "antigravity"`** instead (v2.7.10).

`agent: "gemini"` remains in the schema's `Literal`, but constructing the adapter rejects it with the migration message shown above. Configs that used `gemini` should switch to `agent: antigravity` — see the [antigravity (Google Antigravity CLI)](#antigravity-google-antigravity-cli) section for a working example.

---

## Quickstart

1. **Install `coderouter-plugin-agents` and enable it** (required as of v2.9.0 — skipping this makes `coderouter-t serve` fail at startup whenever a `kind: agent_cli` provider is present):

   ```bash
   # with uv
   uv pip install "coderouter-plugin-agents @ git+https://github.com/zephel01/coderouter-plugin-agents"

   # with pip
   pip install "coderouter-plugin-agents @ git+https://github.com/zephel01/coderouter-plugin-agents"

   # if CodeRouter itself is installed via uv tool install, the plugin must live in the same tool environment
   uv tool install coderouter-t \
     --with "coderouter-plugin-agents @ git+https://github.com/zephel01/coderouter-plugin-agents"
   ```

   Add `plugins.enabled: [agents]` to `providers.yaml` (a two-stage gate — installing the package alone does not activate it):

   ```yaml
   plugins:
     enabled: [agents]
   ```

2. **Install the Claude Code CLI** (verify with `claude --version`).
3. **Log in** — either run `claude` interactively and complete `/login`, or, on a headless machine, follow the [platform-specific authentication](#platform-specific-authentication) steps using `claude setup-token`.
4. **Start with the example config** (`examples/providers-agent-cli.yaml` already has `plugins.enabled: [agents]` set):

   ```bash
   uv run coderouter-t serve --config examples/providers-agent-cli.yaml --port 8088
   ```

5. **Verify it works**:

   ```bash
   curl http://localhost:8088/v1/chat/completions \
     -H 'Content-Type: application/json' \
     -H 'X-CodeRouter-Profile: claude-agent' \
     -d '{"model":"opus","messages":[{"role":"user","content":"1行でこんにちはと言って"}]}'
   ```

### The first call is slow

`agent_cli` launches a fresh CLI process on every call (nothing is kept resident). The first call pays for process startup plus one round trip with the real Claude backend, so it is noticeably slower than a typical HTTP-backed provider.

You may also notice `usage.prompt_tokens` in the tens of thousands. This is because Claude Code's own system prompt (hooks/CLAUDE.md discovery, the full tool-definition set) rides along on every call regardless of how much text CodeRouter actually sent. When running on subscription OAuth, this does **not** cost anything in dollars (it does consume the 5-hour window / weekly quota — see [Limitations](#limitations)).

From the second call onward, Anthropic's prompt cache kicks in and `usage.prompt_tokens_details.cached_tokens` grows. In one measured run against the same `workdir`, the first call's `coderouter_cost_usd` (a dollar-equivalent figure based on API pricing, not an actual charge — see [Configuration reference](#configuration-reference)) was **about $0.22-equivalent**, and once the cache took effect on subsequent calls it dropped to **about $0.05-equivalent**.

---

## Platform-specific authentication

`AgentCliAdapter` does **not** let the child process inherit the parent process's environment as-is. It explicitly injects a fixed, safe `PATH` / `NO_COLOR=1` / `TERM=dumb`, plus `HOME` / `USER` / `LOGNAME` (only when set), plus whatever names are listed in `passthrough_env`. As a result, `ANTHROPIC_API_KEY` is not forwarded by default, and subscription authentication (OAuth) takes priority.

| Platform | Where credentials live | Env vars that must be inherited | v2.7.7 status |
|---|---|---|---|
| **macOS** | Keychain | `USER` (required for Keychain entry resolution) | Fixed in v2.7.7, which now inherits `USER` / `LOGNAME`. Works as-is once `claude /login` has been done (field-verified) |
| **Linux** | `~/.claude/.credentials.json` (mode `0600`) | `HOME` | Works with `HOME` inheritance alone. Works as-is once `claude /login` has been done (field-verified) |
| **Headless server / container** (no browser) | Either of the above, or a long-lived token | `CLAUDE_CODE_OAUTH_TOKEN` (via explicit `passthrough_env`) | See the procedure below |
| **Windows** | Not natively supported | — | Run CodeRouter itself inside WSL2 (this is then treated the same as Linux) |

### macOS

The Claude Code CLI reads the `USER` environment variable when resolving its Keychain entry. Before v2.7.7, the env allowlist only inherited `HOME`, so `USER` was missing and headless/server runs on macOS failed Keychain resolution with `Not logged in`. v2.7.7 fixed `_build_child_env()` to also inherit `USER` / `LOGNAME`, resolving this. As long as `claude` has already been logged in interactively via `/login`, it works with no extra configuration — verified on real hardware.

### Linux

Credentials live in `~/.claude/.credentials.json` (mode `0600`). The child process only needs `HOME` inherited to read this file. As on macOS, once `claude /login` has been completed it works as-is — verified on real hardware.

### Headless server / container (no browser)

For environments where an interactive browser login isn't possible, there's a path via a long-lived token forwarded through an environment variable.

1. On a **machine with a browser**, run `claude setup-token` to issue an OAuth token valid for one year.
2. Put the issued token in the target server's `.env` as `CLAUDE_CODE_OAUTH_TOKEN=...`. Make sure the file is mode `0600` and excluded via `.gitignore` (both are checked by the `env_security` check in `coderouter doctor --check-env`).
3. In `providers.yaml`, set `agent_cli.passthrough_env: [CLAUDE_CODE_OAUTH_TOKEN]` on that provider to explicitly forward it into the child process.

### Windows

`AgentCliAdapter` is implemented assuming POSIX (`os.killpg`-based process-group kill, a fixed `PATH` in `/usr/local/bin`-style form, etc.), so it does not run natively on Windows. Running the whole CodeRouter stack inside WSL2 and calling `claude` from within WSL2 effectively makes this the same as the Linux case.

### Important note — API keys are not forwarded automatically

Because the child process does not inherit the parent environment, exporting `ANTHROPIC_API_KEY` in your shell will **not** reach the claude CLI. This is intentional: it prioritizes subscription authentication and prevents a stray API key left in the environment from silently overriding subscription auth. Only if you want to run on API-key metered billing should you explicitly list it, e.g. `passthrough_env: [ANTHROPIC_API_KEY]`.

---

## Configuration reference

> The `agent_cli:` sub-config schema (`AgentCliConfig`) is unchanged in v2.9.0. The only change is "install `coderouter-plugin-agents` and add `plugins.enabled: [agents]` to `providers.yaml`" — existing provider entries need no edits (see [Quickstart](#quickstart)).

All fields of the `agent_cli:` sub-config (`AgentCliConfig`) in `providers.yaml`. `extra: forbid` applies, so an unknown key fails immediately at config load.

| Field | Type | Default | Description |
|---|---|---|---|
| `agent` | `"claude" \| "codex" \| "antigravity" \| "gemini" \| "grok"` | (required) | Which CLI to invoke. **As of v2.7.10, `claude`, `codex`, `antigravity`, and `grok` are all implemented (Phase 1 complete); `gemini` remains in the schema but is rejected when the adapter is constructed** |
| `command` | `str \| null` | `null` (defaults to the same name as `agent`) | CLI executable name or absolute path, resolved via `PATH`. **`agent: antigravity` is the sole exception, defaulting to `agy`** (the binary name differs from the product name) |
| `workdir` | `str \| null` | `null` (defaults to `~/.coderouter-t/agents/<provider name>`) | Working directory for the one-shot exec. `~` / env-var expansion is applied; a path containing `..` is rejected |
| `exec_timeout_s` | `float` | `600.0` (range `1.0`–`1800.0`) | Forced timeout (seconds) for the whole exec. **Separate** from `ProviderConfig.timeout_s` (the latter is not used by agent_cli). For antigravity this value also generates `--print-timeout` (see below) |
| `allow_file_writes` | `bool` | `false` | Whether to allow file writes. When `false`, the effective mode is clamped to read-only regardless of `sandbox_mode` |
| `sandbox_mode` | `"read_only" \| "edit" \| "full_auto"` | `"read_only"` | Maps to each CLI's sandbox/approval flags (claude: [table below](#sandbox_mode--permission-mode-mapping-claude); codex: [codex section](#sandbox_mode--codex-flag-mapping); antigravity: [antigravity section](#sandbox_mode--antigravity-flag-mapping); grok: [grok section](#sandbox_mode--grok-flag-mapping)) |
| `model` | `str \| null` | `null` (defaults to `ProviderConfig.model`) | Model name passed to the CLI's `--model` / `-m` (claude: `opus` / `sonnet` / `haiku` / `fable` etc.; codex: `gpt-5.5` etc.; antigravity: a **display-string** like `"Gemini 3.5 Flash (Low)"`; grok: `grok-4.5` etc.) |
| `max_turns` | `int \| null` | `8` (range `1`–`50`) | Turn cap inside the CLI. Passed as `--max-turns`. **codex and antigravity have no corresponding CLI flag, so this is always ignored for both** (for those two, `exec_timeout_s` + process-group kill is the only time bound; antigravity additionally has its own `--print-timeout`) |
| `passthrough_env` | `list[str]` | `[]` | Allowlist of environment variable names forwarded from the parent process into the child. `ANTHROPIC_API_KEY` is not forwarded unless listed here |
| `agent_depth_limit` | `int` | `2` (range `1`–`4`) | Recursion nesting cap. When `CODEROUTER_AGENT_DEPTH` reaches or exceeds this, the call stops immediately with `AdapterError(retryable=False)` |

When `command` is unset it defaults to the same name as `agent`. Also, specifying `allow_file_writes: true` together with `sandbox_mode: read_only` is treated as a contradictory configuration and raises a **`ValueError` at config-load time** (set `sandbox_mode` to `edit` or `full_auto` if you want to permit writes).

### `sandbox_mode` → `--permission-mode` mapping (claude)

| `sandbox_mode` | claude `--permission-mode` | Notes |
|---|---|---|
| `read_only` (default) | `plan` | No file changes. Always clamped to this mode when `allow_file_writes=false` |
| `edit` | `acceptEdits` | Auto-approves file edits |
| `full_auto` | `acceptEdits` | For claude this maps the same as `edit` (claude has no separate full_auto-equivalent mode in use yet). grok distinguishes it via `--always-approve` (see the [grok section](#sandbox_mode--grok-flag-mapping)) |

### Why `paid: false`

The `agent-claude` provider in the example config `examples/providers-agent-cli.yaml` is set to `paid: false`. That's because running on subscription OAuth incurs **zero metered cost** (only the 5-hour window / weekly quota described below is consumed). If you want to run it on metered API-key billing instead, change it to `paid: true` and pass `ALLOW_PAID=true` as an environment variable when starting CodeRouter. Note that the `ALLOW_PAID` environment variable **overrides** whatever `allow_paid` value is written in `providers.yaml` at startup — a `paid: true` provider is excluded from routing whenever `ALLOW_PAID` is unset.

---

## codex (OpenAI Codex CLI)

v2.7.9 (Phase 1b) implements `agent: codex`. Like claude, it delivers the prompt via **stdin** (unlike grok's file-based delivery). It has its own behavior around JSONL output, `--ephemeral`, and running outside a git repository. Everything below is based on codex CLI **0.144.1** (field-verified on the author's Mac, 2026-07-11).

### Example configuration

```yaml
providers:
  - name: agent-codex
    kind: agent_cli
    model: gpt-5.5                # a current frontier-model example; the default depends on the environment/plan, so set it explicitly
    paid: false                   # ChatGPT-plan subscription OAuth = zero metered cost
    capabilities:
      streaming: false
      tools: false
    agent_cli:
      agent: codex
      command: codex
      workdir: ~/.coderouter-t/agents/codex
      exec_timeout_s: 600
      allow_file_writes: false
      sandbox_mode: read_only
      max_turns: 8                 # ignored by codex — no corresponding flag (see below)
      passthrough_env: []          # OAuth reads ~/.codex/auth.json via the inherited HOME, so empty is fine.
                                    # Only list CODEX_API_KEY (exec-only) or OPENAI_API_KEY
                                    # when using an API key in CI
```

### The argv the adapter builds

With `sandbox_mode: read_only` (the default), the adapter builds the following argv.

```
codex exec --json --skip-git-repo-check --ephemeral -m <model> -C <workdir> -s read-only -
```

### The prompt is delivered via stdin (same as claude, unlike grok)

codex's `exec` subcommand reads the prompt from stdin when the PROMPT argument is omitted (or explicitly given as `-`). The adapter places an explicit trailing `-` on the argv to force this path. Unlike grok, there's no need for a temp file (`--prompt-file`) inside the isolated workdir — this is the same stdin scheme as claude.

### Why `--skip-git-repo-check` is always passed

CodeRouter's isolated workdir is not a git repository. By default, codex runs a "trusted directory" check and, outside a git repo in an unrecognized directory, fails immediately with exit 1 and stderr `Not inside a trusted directory and --skip-git-repo-check was not specified.` (field-verified). The adapter always passes this flag, so this error message should never appear in normal operation.

### Why `--ephemeral` is always passed

`--ephemeral` prevents the session from being persisted to disk. For the same reason as grok's `--no-memory` — keeping with CodeRouter's "one request = one stateless transformation" ethos — the adapter always passes this flag (it cannot be turned off in config).

### `sandbox_mode` → codex flag mapping

As with claude/grok, when `allow_file_writes=false` the effective mode is clamped to `read_only` regardless of `sandbox_mode`.

| `sandbox_mode` | codex flags | Notes |
|---|---|---|
| `read_only` (default) | `-s read-only` | No file changes. Always clamped to this mode when `allow_file_writes=false` |
| `edit` | `-s workspace-write` | Permits file edits inside the workspace-write sandbox |
| `full_auto` | `-s workspace-write` | **`codex exec` has no approval flag (`-a` / `--ask-for-approval`)** — it's absent from `exec --help` in 0.144.1, since non-interactive execution has no approval prompt to control in the first place. So this maps identically to `edit`. `--dangerously-bypass-approvals-and-sandbox` is never used |

### No `--max-turns` / `--timeout` exist

codex exec has neither `--max-turns` nor `--timeout`. Consequently `AgentCliConfig.max_turns` is **ignored for codex**. The only time bound is the existing `exec_timeout_s` + process-group `SIGKILL`.

### JSONL output and usage normalization

`--json` output is JSONL (one event per line); progress goes to stderr, and the event stream (including the final answer) goes to stdout (confirmed by both the official docs and the real CLI). A verified one-shot run:

```
$ codex exec --json --skip-git-repo-check "What's 1+1? Answer with just the digit"
{"type":"thread.started","thread_id":"019f4e74-08fd-77b2-9cc6-9afa744df130"}
{"type":"turn.started"}
{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"2"}}
{"type":"turn.completed","usage":{"input_tokens":13810,"cached_input_tokens":9984,"output_tokens":5,"reasoning_output_tokens":0}}
```

- Final answer: the **last** `item.completed` whose `item.type=="agent_message"`, taking `item.text`.
- Usage normalization: `turn.completed.usage`'s `input_tokens` → `prompt_tokens`, `output_tokens` → `completion_tokens`, and their sum → `total_tokens`. `cached_input_tokens` is a **subset** of `input_tokens` (measured: input 13810 ⊃ cached 9984), so it is not added on top — it's kept as `prompt_tokens_details.cached_tokens`. When `reasoning_output_tokens` is greater than 0, it's kept as `completion_tokens_details.reasoning_tokens` (defensive). Multiple `turn.completed` events, if they occur, are summed.
- `thread_id` (from `thread.started`) is surfaced as the `coderouter_session_id` response metadata.
- If an `error` event or `turn.failed` is seen, or no agent_message is ever produced / stdout is empty / every line is non-JSON, this raises a retryable `AdapterError` and the fallback chain advances to the next provider. Individual non-JSON lines in the JSONL stream don't stop processing of the remaining lines (defense against stray stderr-like output mixed in).

### Authentication (ChatGPT-plan subscription OAuth / API key)

The codex CLI supports ChatGPT-plan OAuth login. Credentials are stored at `~/.codex/auth.json` (or the OS keyring), and the adapter's `HOME` inheritance makes it work with `passthrough_env: []`.

1. Run `codex login` to complete login. `codex login status` exiting 0 confirms you're logged in.
2. The OAuth token goes **stale after about 8 days**. It auto-refreshes on use, but a setup that doesn't call codex for a long stretch can fail while stale — running codex occasionally, or re-logging in, is recommended.

For CI or metered API-key billing, list `CODEX_API_KEY` (**exec-only**) or `OPENAI_API_KEY` (general) in `passthrough_env`. `CODEX_HOME` can also override the config/credentials directory itself.

### Error reporting

The codex CLI exits 0 on success, and on failure exits non-zero with the error text on stderr (e.g. the git-repo-check message). JSONL may also carry an `error` event or `turn.failed`; both of those also become a retryable `AdapterError` and the fallback chain advances to the next provider.

### Pre-1.0 caveat

The codex CLI is pre-1.0 and releases nearly daily. `--json`'s alias is still `--experimental-json`, and the JSONL schema is not frozen. **Version pinning is recommended** (you can point `command` at a pinned binary's full path). If the schema does change, defensive parsing turns it into a retryable `AdapterError` and the fallback chain demotes to the next provider.

---

## antigravity (Google Antigravity CLI)

v2.7.10 (Phase 1c) implements `agent: antigravity`, which completes **Phase 1 (claude, codex, antigravity, and grok — all four backends)**. This target replaces the originally-planned `gemini`, since Google discontinued that CLI for individual accounts — see [Gemini's discontinuation and the move to antigravity](#geminis-discontinuation-and-the-move-to-antigravity) for the story. Its distinguishing behaviors: the prompt rides on **argv** (unlike claude/codex's stdin or grok's `--prompt-file`), output is plain text (usage is always zero), and it's the only agent_cli target with its own CLI-side timeout. Everything below is based on agy **1.1.1** (field-verified on the author's Mac, 2026-07-11).

### Example configuration

```yaml
providers:
  - name: agent-antigravity
    kind: agent_cli
    model: "Gemini 3.5 Flash (Low)"   # a display string, not an API ID (see below)
    paid: false                       # Google-account OAuth (including free tier) = zero metered cost
    capabilities:
      streaming: false
      tools: false
    agent_cli:
      agent: antigravity
      # command may be omitted (defaults to "agy" — the binary name differs from the product name)
      workdir: ~/.coderouter-t/agents/antigravity
      exec_timeout_s: 600
      allow_file_writes: false
      sandbox_mode: read_only
      passthrough_env: []             # OAuth reads the OS keyring + ~/.gemini/antigravity-cli/ via
                                       # inherited HOME (and USER on macOS), so empty is fine.
                                       # API-key env is UNCONFIRMED (see below)
```

### The argv the adapter builds

With `sandbox_mode: read_only` (the default), the adapter builds the following argv.

```
agy -p <prompt> --model "Gemini 3.5 Flash (Low)" --mode plan --print-timeout 600s
```

### The prompt is delivered via argv (unlike claude/codex's stdin or grok's `--prompt-file`)

Prompt delivery splits into three patterns across the four backends.

| agent | Delivery mechanism | Notes |
|---|---|---|
| claude | stdin | `-p` reads stdin when the argument is omitted |
| codex | stdin (with an explicit trailing `-` on argv) | Same channel as claude |
| grok | `--prompt-file` (a mode-`0600` temp file inside the isolated workdir) | Neither argv nor stdin is accepted |
| antigravity | argv value (`-p <prompt>`) | stdin is not accepted; there's no `--prompt-file` equivalent either |

agy's `-p` / `--print` / `--prompt` requires a value on argv — there is no stdin channel for the prompt, and no `--prompt-file`-equivalent flag either. So the prompt has to ride on argv, which carries Linux's `MAX_ARG_STRLEN` (~128KiB) size cap and exposes the full prompt text to `ps` as known limitations (documented here since there's no alternative with agy's current flag surface). The argv is passed as a list (never `shell=True`), so it doesn't go through shell interpretation.

### Piping stdin causes a hang — a caveat for anyone scripting `agy` directly

The adapter itself never writes to stdin and closes it immediately (`communicate(input=None)`, field-verified to work correctly via `</dev/null`), so this is a non-issue through CodeRouter. However, **piping anything into agy's stdin makes it hang** (field-verified: `printf '...' | agy -p "..."` sits waiting and eventually reports `Error: timeout waiting for response`). agy has no facility for reading stdin as context. If you script `agy` directly, always leave stdin empty (e.g. `</dev/null`).

### `sandbox_mode` → antigravity flag mapping

As with claude/codex/grok, when `allow_file_writes=false` the effective mode is clamped to `read_only` regardless of `sandbox_mode`.

| `sandbox_mode` | agy flags | Notes |
|---|---|---|
| `read_only` (default) | `--mode plan` | No file changes. Always clamped to this mode when `allow_file_writes=false` |
| `edit` | `--mode accept-edits` | Auto-approves file edits |
| `full_auto` | `--mode accept-edits --dangerously-skip-permissions` | Auto-approves all tool execution (agy's `--help` only enumerates `accept-edits`/`plan` as mode values; full_auto is expressed by adding `--dangerously-skip-permissions`) |

### Plain-text output and always-zero usage

agy has **no `--output-format`-style flag at all** — output is plain text only. The adapter decodes stdout as UTF-8, defensively strips ANSI escapes (regex), and trims whitespace to produce the final answer; an empty result raises a retryable `AdapterError`. With no JSON output, token usage, session id, and structured errors are all unavailable — usage is reported as **always zero**, same as grok (`coderouter_cost_usd` stays 0 unless the operator sets unit prices in `ProviderConfig.cost`). No session id is ever surfaced in response metadata.

### `--print-timeout` — the first agent with its own CLI-side timeout

agy has a `--print-timeout <Go duration>` flag (default `5m0s`) that bounds how long print mode itself will wait. claude/codex/grok have no CLI-side timeout of this kind. The adapter generates `--print-timeout` from `AgentCliConfig.exec_timeout_s` (e.g. `exec_timeout_s=600` → `--print-timeout 600s`), giving the CLI a first wall to self-terminate against. This is on top of the existing outer `asyncio.wait_for` + process-group `SIGKILL` second wall, making antigravity the only agent with a **double timeout wall**.

### `max_turns` is ignored

agy has no `--max-turns`-equivalent flag. `AgentCliConfig.max_turns` always has no effect for antigravity (same as codex), and the time bound is carried entirely by the `--print-timeout` + `exec_timeout_s` double wall.

### Authentication (Google-account OAuth, free tier included)

agy supports Google-account OAuth, including the free tier. Credentials are stored primarily in the OS keyring, with `~/.gemini/antigravity-cli/` (`credentials.enc` / `settings.json`) as well. The adapter's `HOME` inheritance (plus `USER` on macOS) lets it run headless with `passthrough_env: []` (handled by the existing `_build_child_env()`). Setup steps:

1. Run `agy` for the first time; it opens a browser for Google-account login. Complete it.
2. Run `agy models` and smoke-check that the model list comes back.

API-key-based authentication (the environment variable name) has conflicting reports (`ANTIGRAVITY_API_KEY` is claimed by some, and others claim `GEMINI_API_KEY` is ignored) and is **UNCONFIRMED**. This document does not assert either way.

### Model names are display strings — Claude models are reachable through agy

The value passed to `--model` isn't an API ID — it's a **display string** returned by `agy models`. Verified output from a real install (agy 1.1.1):

```
Gemini 3.5 Flash (Medium/High/Low)
Gemini 3.1 Pro (Low/High)
Claude Sonnet 4.6 (Thinking)
Claude Opus 4.6 (Thinking)
GPT-OSS 120B (Medium)
```

Interestingly, agy can reach non-Google models this way — including Claude Opus 4.6. Write one of these display strings verbatim (spaces, parentheses and all) into `providers.yaml`'s `model` field.

### Early-days-product caveat

The Antigravity CLI is a brand-new product. Its flag surface and model list are likely to keep changing, so **version pinning is recommended** (point `command` at a pinned binary's full path). Because output is plain text, JSON-schema-churn-style parsing accidents are less likely, but unexpected empty responses or nonzero exit codes are handled defensively and become a retryable `AdapterError`, demoting to the next provider in the fallback chain.

---

## grok (Grok CLI)

v2.7.8 (Phase 1d) implements `agent: grok`. It uses the same one-shot exec scheme as claude, but grok has its own behavior around prompt delivery, cross-session memory disabling, and usage reporting. Everything below is based on grok CLI **v0.2.93** ([stable] channel, field-verified on 2026-07-10).

### Example configuration

```yaml
providers:
  - name: agent-grok
    kind: agent_cli
    model: grok-4.5              # the default model on a current install; list with `grok models`
    paid: false                  # subscription OAuth = zero metered cost
    capabilities:
      streaming: false
      tools: false
    agent_cli:
      agent: grok
      command: grok
      workdir: ~/.coderouter-t/agents/grok
      exec_timeout_s: 600
      allow_file_writes: false
      sandbox_mode: read_only
      max_turns: 8
      passthrough_env: []        # OAuth reads ~/.grok/auth.json via the inherited HOME, so empty is fine.
                                 # Only list GROK_CODE_XAI_API_KEY when using an API key in CI
```

### The argv the adapter builds

With `sandbox_mode: read_only` (the default), the adapter builds the following argv.

```
grok --prompt-file <workdir>/.coderouter-prompt-<uuid>.txt \
     --output-format json -m <model> --cwd <workdir> \
     --max-turns <N> --no-memory \
     --sandbox read-only --permission-mode plan
```

### The prompt is delivered via a file (`--prompt-file`)

grok's `-p` / `--single` accepts the prompt **only as an argv value** (verified on the real CLI: stdin is not accepted as the prompt). Putting a huge prompt on argv runs into Linux's `MAX_ARG_STRLEN` limit (~128KiB) and exposes the full prompt text to `ps`. The adapter therefore writes the prompt to a mode-`0600` temp file inside the isolated workdir (`.coderouter-prompt-<uuid>.txt`) and passes it via `--prompt-file`. That temp file is **always deleted** after the exec finishes, including the timeout and error paths.

### `sandbox_mode` → grok flag mapping

As with claude, when `allow_file_writes=false` the effective mode is clamped to `read_only` regardless of `sandbox_mode`.

| `sandbox_mode` | grok flags | Notes |
|---|---|---|
| `read_only` (default) | `--sandbox read-only --permission-mode plan` | No file changes. Always clamped to this mode when `allow_file_writes=false` |
| `edit` | `--sandbox workspace --permission-mode acceptEdits` | Auto-approves file edits inside the workspace sandbox |
| `full_auto` | `--sandbox workspace --always-approve` | Unlike claude, grok maps this distinctly from `edit` |

### `--no-memory` is always passed

The grok CLI has a cross-session memory feature. Letting a previous call's memory leak into the next response conflicts with CodeRouter's "one request = one stateless transformation" ethos, so the adapter **always** passes `--no-memory` to disable it (this cannot be turned off in config).

### JSON output and usage / cost

The `--output-format json` output is a single JSON object `{"text", "stopReason", "sessionId", "requestId", "thought"?}` (verified on grok v0.2.93). `text` becomes the final answer, and `sessionId` is surfaced as the `coderouter_session_id` response metadata. **There are no token-usage or cost fields**, so usage is reported as all zeros, and `coderouter_cost_usd` stays 0 unless the operator sets unit prices in `ProviderConfig.cost` (in contrast to claude, which emits `total_cost_usd` directly). The JSON is parsed defensively: anything malformed becomes an `AdapterError(retryable=True)` and the fallback chain advances to the next provider.

### Authentication (subscription OAuth / API key)

The grok CLI supports OAuth subscription login (SuperGrok / X Premium+). Credentials are stored at `~/.grok/auth.json` (7-day expiry with auto-refresh; `GROK_HOME` overrides the location), and the adapter's `HOME` inheritance makes OAuth work with `passthrough_env: []`. Setup steps:

1. Run `grok login` to complete the subscription login.
2. Smoke-check by running `grok models` and confirming the model list comes back. On a current install it lists `grok-4.5` (default) and `grok-composer-2.5-fast`.

Only when running on API-key metered billing (e.g. CI) should you list `passthrough_env: [GROK_CODE_XAI_API_KEY]`. Note that the environment variable name is **`GROK_CODE_XAI_API_KEY`, not `XAI_API_KEY`**. When the API key is forwarded, it takes precedence over OAuth.

### Error reporting

The grok CLI exits 0 on success, and exits 1 on auth/network/runtime errors with the error text on **stderr**. The adapter includes a tail of stderr in the `AdapterError` message, so you can use the message shown as your lead.

### Early-beta caveat

The grok CLI is early beta (v0.2.93 [stable] channel as of 2026-07-10). Its JSON schema may still churn, so **version pinning is recommended** (you can point `command` at a pinned binary's full path). If the schema does change, defensive parsing turns it into a retryable `AdapterError` and the fallback chain demotes to the next provider.

---

## Limitations

| Limitation | Details |
|---|---|
| **One-shot only** | No session continuation (resume). Every call launches a fresh CLI process; nothing from a previous call carries over (this is an intentional non-goal, not a bug) |
| **Pseudo-streaming** | No implemented CLI exposes a stable token-level stream, so `stream()` just splits the final text from `generate()` into fixed-size chunks and yields them in order. The example config explicitly sets `capabilities.streaming: false` (the default `true` must be overridden) |
| **May carry plan-mode framing** | The default `sandbox_mode: read_only` maps to `--permission-mode plan`. Plan mode is designed as a response format for an interactive human-review UI, so in one-shot execution the returned text can lean toward "here's my plan" phrasing rather than a direct answer, since no actual change is made |
| **Consumes subscription quota** | Uses up the Claude Code subscription's 5-hour window / weekly quota. Zero API billing doesn't mean unlimited calls |
| **Recursion cap** | Nested calls beyond `agent_depth_limit` (default 2, max 4) are rejected. Be careful if you build a setup where the agent CLI calls back into CodeRouter internally |

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Fails with `Not logged in · Please run /login` | The claude CLI isn't logged in for the user/environment it's running as | First run `claude` interactively and check the `/login` state. On macOS headless runs, versions before v2.7.7 failed to forward the `USER` environment variable to the child process, causing this error; v2.7.7 fixes it by also inheriting `USER` / `LOGNAME` |
| Requests are rejected / not routed, effectively "paid gate blocked" | The `agent_cli` provider has `paid: true` but `ALLOW_PAID` isn't set | For subscription usage, set `paid: false`. For metered API-key billing, set `ALLOW_PAID=true` when starting CodeRouter |
| `claude exited 1: ...` shows a specific reason | The claude CLI sometimes reports auth/API errors as an `is_error: true` JSON document on **stdout** (with stderr left empty) and exits with code 1 | v2.7.7's `_error_detail()` now prefers the `result` field of that stdout `is_error` JSON (the actual error text, e.g. `Not logged in · Please run /login`) even when stderr is empty, and includes it in the raised error message — use the message shown as your lead |
| Fails with `grok exited 1: ...` | The grok CLI exits with code 1 on auth/network/runtime errors, with the error text on stderr | The adapter includes a tail of stderr in the `AdapterError`, so use the message shown as your lead. For auth errors, re-run `grok login` and smoke-check that `grok models` works |
| Fails with `codex exited 1: ...` (suspect stale OAuth) | codex's OAuth token goes stale after about 8 days. It auto-refreshes on use, but a long gap between calls can leave it stale and failing | Check login state with `codex login status` and re-run `codex login` if needed. Running codex occasionally helps avoid staleness |
| `Not inside a trusted directory and --skip-git-repo-check was not specified.` appears | This should never happen — the adapter always passes `--skip-git-repo-check` | If you see this, it likely indicates a bug in CodeRouter's argv construction. Check your version and file an issue if it reproduces |
| `agent: gemini` raises an `AdapterError` / you see `IneligibleTierError` | Google discontinued the Gemini CLI for individual accounts in June 2026 (field-verified: `IneligibleTierError: This client is no longer supported for Gemini Code Assist for individuals...`) | Switch to `agent: antigravity`. See the [antigravity (Google Antigravity CLI)](#antigravity-google-antigravity-cli) section for a working example |
| `agy` hangs and never responds | Something is being piped into agy's stdin (e.g. `printf '...' \| agy -p "..."`) | Don't pipe stdin — write nothing and redirect from `</dev/null`. Calls made through CodeRouter already do this (the adapter closes stdin immediately) |
| CLI fails to launch (`failed to launch ...`) | `command` (defaults to the same name as `agent`, except antigravity which defaults to `agy`) isn't on `PATH` | Confirm `claude --version` / `codex --version` / `agy --version` / `grok --version` works. You can also point `command` at a full path |
| `coderouter-t serve` fails at startup even though a `kind: agent_cli` provider is configured | v2.9.0 (Phase 2c) removed the in-core `agent_cli` adapter; `coderouter-plugin-agents` plus `plugins.enabled: [agents]` are now required | Run `uv pip install "coderouter-plugin-agents @ git+https://github.com/zephel01/coderouter-plugin-agents"` (or the pip / `uv tool install coderouter-t --with "coderouter-plugin-agents @ git+https://github.com/zephel01/coderouter-plugin-agents"` variant), then add `plugins.enabled: [agents]` to `providers.yaml`. The startup error message itself includes the same migration steps (install + `plugins.enabled`) |
| `coderouter doctor` reports an `agent_cli`-related config warning | A `kind: agent_cli` provider exists but `plugins.enabled` doesn't list `agents` (or the plugin isn't installed) | Same install command as above plus adding `plugins.enabled: [agents]`. `doctor`'s output also prints a fix snippet |

---

## Related docs

- [External Agents Adapter design doc](../designs/external-agents-adapter.md) — authentication design, argv construction, and security requirements in detail
- [`examples/providers-agent-cli.yaml`](../../examples/providers-agent-cli.yaml) — a working example configuration
- [Secrets handling & security posture](../guides/security.md)
