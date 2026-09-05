# Security posture

CodeRouter is a local-first router. It sits between a coding agent
(Claude Code, etc.) and one or more LLM endpoints — some of which are
remote paid APIs that hold state, cost money, and are authenticated
with long-lived secrets. That threat model shapes every policy below.

This document describes two things:

1. **What CodeRouter itself does to stay safe** — design invariants,
   CI gates, and policies that are enforced by code or process.
2. **What an operator should do when running CodeRouter** — choices
   that CodeRouter can't make for you (where keys live, which
   providers to trust, how the machine is networked).

The v1.0 baseline is "defense in depth, minimal attack surface."
Nothing here is absolute; this is a deliberately small project that
a single person can audit end-to-end, and the safety properties
come from keeping it that way.

---

## 1. Secrets and credentials

**Policy.** API keys never live in config files. `providers.yaml`
references them by env-var name (`api_key_env: OPENROUTER_API_KEY`),
and the loader resolves them at startup. If the named var is absent
the provider is skipped, not stubbed; callers see an explicit error
rather than silently falling through to a different tier.

**Why.** Config files get checked in by accident. Env vars get
`echo`-ed to the shell, not to `git`. The separation is mechanical,
not a convention — `ProviderConfig` has no field for a raw key, so
there is no place in a committed file for one to land.

**CI enforcement.** The `secret-scan` job runs `gitleaks` against the
full commit history on every push and pull request. A finding fails
the build.

### 1.1 `credential.source` — credentials that aren't env vars (v2.14.0)

Before v2.14.0 a provider's credential could only be `api_key_env`.
v2.14.0 adds a `credential` block with two sources:

- `source: env` — the spelled-out form of `api_key_env`
  (`credential.env: OPENROUTER_API_KEY`).
- `source: cli_session` — read the OAuth token a vendor CLI (Kimi Code
  CLI, Grok CLI, …) **has already written to disk**, and make the HTTP
  call from CodeRouter itself.

The point of `cli_session` is that a subscription-authenticated provider
becomes an ordinary `openai_compat` / `anthropic` entry instead of a
`kind: agent_cli` island — a one-shot CLI spawn per request has no
streaming, no tool-call repair, and does not sit in a fallback chain.

```yaml
- name: kimi-sub
  kind: openai_compat
  base_url: https://api.moonshot.cn/v1
  model: kimi-k2
  credential:
    source: cli_session
    path: ~/.kimi-code/credentials/kimi-code.json   # must live under $HOME
    field: access_token          # dotted, e.g. "tokens.access", when nested
    expiry_field: expires_at     # epoch seconds or ms; absent = 401 is the signal
    refresh:
      command: ["kimi", "auth", "status"]   # argv list, run with shell=False
      min_lead_s: 300            # always refresh 5 min before expiry
      early_ratio: 0.5           # refresh once half the lifetime is gone
      timeout_s: 30
```

Security invariants:

- **`credential` and `api_key_env` are mutually exclusive at load time.**
  Setting both is a validation error, because "which credential did this
  request actually use?" must never be unanswerable.
- **`credential.path` must live under `$HOME`**, validated at load time.
  A vendor CLI writes under the home directory; nothing legitimate needs
  `/etc/shadow`. (Enforced on POSIX; always permitted on Windows.)
- **`refresh.command` is an argv *list* dispatched with `shell=False`.**
  There is no string form in the schema, so a config file cannot become
  arbitrary shell execution — the same trust decision v2.13.0 made for
  `restart_command`.
- **OAuth is not reimplemented.** Refresh runs the vendor's own CLI and
  re-reads the file, because endpoints, client ids and rotation policies
  differ per vendor and keep changing; a bespoke implementation rots.
- Refresh is **single-flighted in-process** by a per-path lock and
  **serialised across processes** by an advisory `flock` on a sidecar
  `<session-file>.coderouter-lock`. After acquiring either lock the file
  is re-read before deciding to refresh — the lock holder may have done
  the work already.
- **The refresh command's stderr is deliberately never logged**: a
  failing auth CLI is exactly the thing most likely to print a token or
  a device code.
- A missing, malformed, or not-yet-logged-in session file resolves to
  `None` rather than raising. The request goes out unauthenticated, the
  upstream 401 fires, and the fallback chain moves on — a working chain
  beats a router that refuses to start.
- A token that was read is registered with the log scrubber (§1.2)
  before it goes anywhere near a header.

A complete working example lives in `examples/providers.cli-session.yaml`.

### 1.2 Secret scrubbing in logs (v2.14.0)

Before v2.14.0 nothing in this codebase knew that a given string was a
secret. Keys were read by `resolve_api_key`, dropped into `x-api-key` /
`Authorization`, and that was that. No call site logged a header dict,
so this was not a live leak — the problem was that the safety of every
*future* log line depended on whoever wrote it remembering what was in
their hand.

**Exact-match registry first, patterns only as a backstop.**

- Registration happens in two places: `resolve_api_key` (the choke point
  every key passes through, including keys for providers added by an
  adapter plugin) and `load_config` (so startup lines naming providers
  and profiles are already covered). `cli_session` tokens are registered
  when they resolve.
- `SecretRedactingFilter` is a `logging.Filter`, not a formatter, and is
  attached to **every** handler we install — the stderr handler,
  `RequestLogHandler` and `AuditLogHandler`. It mutates the record in
  place, so a later handler cannot re-expose what an earlier one
  scrubbed.
- It scrubs the message, the printf args, `exc_text`, and every
  `extra={...}` field, recursing into nested dicts, lists, tuples and
  sets.
- The backstop patterns are few and anchored: `sk-…`, `gh[pousr]_…`,
  `AIza…`, `Bearer <token>`, URL params (`?api_key=`, `access_token=`,
  `auth_token=`, `key=`) and `scheme://user:pass@host` userinfo. There is
  deliberately **no** "high entropy string" detection — that is how a
  scrubber starts eating commit hashes and base64 payloads.
- **Values shorter than 8 characters are refused by the registry.** A
  three-character "key" (a placeholder, an almost-empty env var, a test
  stub) would match inside unrelated words and corrupt ordinary log text.
- The `base_url` returned by `/metrics.json` also goes through the
  scrubber before it reaches a browser.

**Non-goals:** encryption at rest, repairing already-written logs
(detection is §1.3; the repair is rotation), and defending against
someone who can read the process environment. It closes exactly one
hole: credentials reaching log sinks.

### 1.3 `coderouter doctor --check-secrets` (v2.14.0)

`--check-env` asks "is the file holding my keys protected?".
`--check-secrets` asks "does the running process know its own secrets,
and have any of them already reached a log file?".

```bash
coderouter doctor --check-secrets
coderouter doctor --check-env --check-secrets   # combined; exit = worst of the two
```

Four checks run:

| Check | What it does |
|---|---|
| `redaction-filter` | Pushes a canary through a real `SecretRedactingFilter` and verifies it survives in neither the message nor the `extra` payload — evidence, not an assertion that the code exists |
| `registered-secrets` | How many declared `api_key_env` vars are actually set; any unset one is a `warn` |
| `config-embedded-credentials` | Flags credentials pasted into a `base_url`; a hit is an `error` |
| `written-log-scan` | Scans `requests.jsonl` / `audit.jsonl` under `state_dir` (including the rotated `.1`) for live credential values; a hit is an `error`. Read-only — nothing is rewritten |

Exit codes match the `--check-env` contract: **0 clean / 2 needs
attention / 1 blocker**. A config that fails to load is exit 1, not a
skip — we cannot claim anything about a file we could not read.

When `written-log-scan` finds something, the remediation is **rotating
the credential**, not deleting the log. Deleting the file does not
un-write the value.

Note that `registered-secrets` and `written-log-scan` only cover keys
declared via `api_key_env`. A `credential.source: cli_session` token is
registered only in a process that actually served a request, so a
one-shot `doctor` run has nothing to look for.

**Operator checklist.**

- Put keys in `~/.zshenv` / `~/.bashrc` / `launchctl setenv`, or a
  secret manager (1Password CLI, `op run --env-file=...`, macOS
  Keychain). Not in `.env` files inside the repo.
- Rotate provider keys periodically; the router has no state tying
  a key to any ongoing request.
- Never paste a key into an issue or PR comment.
- Run `coderouter doctor --check-secrets` before handing logs or
  `requests.jsonl` to anyone.
- If `written-log-scan` finds a key, rotate that provider's credential
  before deleting the log.

---

## 2. Supply-chain hygiene

The 2023–2025 era has normalized supply-chain attacks in the Python
and GitHub Actions ecosystems: package hijacks (`ctx`), typosquats,
tag re-pointing (`tj-actions/changed-files`), and compromised
maintainer accounts. CodeRouter's policy is layered so no single
compromise upstream can land unnoticed.

### 2.1 Minimal runtime surface

Runtime dependencies are deliberately restricted to five packages:
`fastapi`, `uvicorn[standard]`, `httpx`, `pydantic`, and `pyyaml`.
Provider SDKs (`anthropic`, `openai`, `litellm`, `langchain`) are
**forbidden at the code level**: the CI job `test` greps the source
for `import anthropic|openai|litellm|langchain` and fails on any
match. The router speaks each wire protocol directly via `httpx`.

This is both a design invariant (plan.md §5.4) and an attack-surface
choice — five well-known packages with a lot of eyes on them are
easier to trust than a transitive graph pulled in by a convenience
SDK.

### 2.2 Lockfile-frozen installs

`uv.lock` pins every direct and transitive dependency to an exact
version + hash. CI runs `uv sync --frozen --extra dev`, which refuses
to install anything if `pyproject.toml` or the lockfile has drifted.
A new transitive cannot appear on `main` without an explicit
lockfile update that someone reviewed.

### 2.3 Multi-source CVE audit

Two scanners run on every push because their advisory databases
don't fully overlap:

| Scanner | Data source | Catches |
|---|---|---|
| `pip-audit` | PyPA Advisory Database (primary Python feed) | PyPA-mirrored CVEs |
| OSV-Scanner | Google OSV (GHSA + language-agnostic) | GHSA entries not yet mirrored to PyPA, cross-ecosystem advisories |

A non-empty finding fails the build. The advisory latency difference
between the two feeds is real — an advisory typically appears in OSV
hours to a day before it lands in PyPA — so the belt-and-braces
wiring is worth the extra minute of CI time.

### 2.4 Dependency review on PRs

`actions/dependency-review-action` runs only on pull requests and
fails the build if the PR introduces a **new** dependency with a
known High/Critical severity advisory. This catches the regression
at PR time rather than after it's merged.

### 2.5 GitHub Actions are dependencies too

The `dependabot.yml` file configures two ecosystems: `pip` for the
Python graph, and `github-actions` for the action versions referenced
in `.github/workflows/*.yml`. Actions are weekly-bumped just like
runtime libraries.

Action versions are currently referenced by major tag (`@v4`,
`@v3`, `@v2`). For a stricter pinning pass, each tag can be replaced
with a commit SHA (`@3df4ab11eba7bda6032a0b82a6bb43b11571feac # v4`).
Dependabot keeps SHA-pinned entries up to date as well.

### 2.6 A `providers.yaml` in a repo is an input too (v2.13.0)

`providers.yaml` carries fields that **name executables**:
`restart_command`, `launcher.backends[*].binary` and
`launcher.bench.command_template`. Before v2.13.0, starting `coderouter`
(or the GUI launcher) from a directory containing a `providers.yaml`
loaded that file implicitly — so cloning a repo and starting the router
inside it was a code-execution path.

Since v2.13.0 the search order is the following, and **step 3 is
opt-in**:

1. A path passed explicitly via `--config`
2. The `CODEROUTER_CONFIG` env var
3. `./providers.yaml` in the current directory — only when
   `CODEROUTER_ALLOW_CWD_CONFIG` is set
4. `~/.coderouter-t/providers.yaml`

The opt-in is enabled only when `CODEROUTER_ALLOW_CWD_CONFIG` is
`1` / `true` / `yes` / `on` (surrounding whitespace ignored,
case-insensitive). Anything else — `0`, `false`, an empty string, a
spelling like `enabled` — counts as disabled.

```bash
# Enable only in directories you trust
CODEROUTER_ALLOW_CWD_CONFIG=1 coderouter-t serve
```

**"It used to work and now my config is ignored."** When a
`./providers.yaml` exists but was not loaded, a one-time
`cwd-config-skipped` warning names the skipped path and the ways out. If
no config is found at all, the `FileNotFoundError` message repeats the
same note. Three fixes, safest first:

1. `coderouter-t serve --config ./providers.yaml` — name the file
   explicitly
2. `export CODEROUTER_CONFIG=$PWD/providers.yaml` — the same, via env
3. `export CODEROUTER_ALLOW_CWD_CONFIG=1` — restore implicit discovery,
   **only in directories you trust**

The durable home for the file is `~/.coderouter-t/providers.yaml`. When
the opt-in is on and the CWD step actually served the config, a one-time
`cwd-config-loaded` warning fires. Neither warning fires when the file
was named explicitly via `--config` / `CODEROUTER_CONFIG` — even if that
path happens to be `./providers.yaml`, because an explicit choice is not
the implicit behaviour being gated.

---

## 3. Network posture

CodeRouter binds to `127.0.0.1` by default (`coderouter-t serve --host`).
It does not expose itself on `0.0.0.0` unless the operator explicitly
opts in. The trust boundary is "loopback only", and every route
validates the Host header (DNS-rebinding protection): requests whose
Host is not a loopback name are rejected with 403; deliberate external
exposure requires listing the extra hostnames in
`CODEROUTER_ALLOWED_HOSTS` (comma-separated). There is no
authentication on the chat ingress (`/v1/messages` /
`/v1/chat/completions`). The launcher API's state-changing endpoints
(start / stop / delete) support opt-in token auth: when
`CODEROUTER_LAUNCHER_TOKEN` is set, clients must send a matching
`X-CodeRouter-Token` header (unset = unauthenticated, as before).
Request bodies are capped at 64 MB by default (413 on overflow),
tunable via `CODEROUTER_MAX_BODY_BYTES`.

**Read-only endpoints are open by default too** (v2.14.0). `/dashboard`,
`/metrics.json` and `/metrics` change nothing, so they were never part
of the launcher token work — but "read-only" is not "harmless".
`/metrics.json` returns every provider's name, kind, paid flag and
`base_url`, plus the profile graph: the full topology of which models an
operator runs and which vendors they pay. On a laptop that is fine; on a
box where the port is reachable by anything else it is a free
reconnaissance endpoint.

Setting `CODEROUTER_METRICS_TOKEN` puts token auth on all three, using
the same mechanism and the same `X-CodeRouter-Token` header as
`CODEROUTER_LAUNCHER_TOKEN` (compared with `secrets.compare_digest`; a
missing or mismatched token is a 401). The token is deliberately **not**
accepted as a query parameter — a token in a URL lands in access logs,
`Referer` headers and browser history.

```bash
export CODEROUTER_METRICS_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
coderouter-t serve
curl -H "X-CodeRouter-Token: $CODEROUTER_METRICS_TOKEN" http://127.0.0.1:4000/metrics.json
```

Unset, the endpoints stay open exactly as before (no existing Prometheus
scrape breaks on upgrade) and a single `metrics-auth-disabled` warning
is logged the first time one of them is hit. The auth check runs
**before** the snapshot and provider list are assembled, so a 401 leaks
nothing. The `/dashboard` HTML receives only a boolean saying whether
auth is required — never the token itself (the same split as the v2.13.0
fix, after `curl /launcher | grep` recovered the launcher secret out of
the page). The browser keeps the operator-entered token in
`sessionStorage`, so it is dropped when the tab closes.

**Operator checklist.**

- Do not bind to `0.0.0.0` on a multi-user host without a separate
  reverse proxy that enforces auth. When serving under a non-loopback
  hostname, add it to `CODEROUTER_ALLOWED_HOSTS` — otherwise Host
  validation rejects the requests with 403.
- Set `CODEROUTER_LAUNCHER_TOKEN` whenever the launcher is reachable
  from beyond loopback.
- Set `CODEROUTER_METRICS_TOKEN` in the same situations.
  `CODEROUTER_LAUNCHER_TOKEN` guards only the launcher API; the
  `/metrics.json` topology stays open without it.
- If exposing over the network (e.g. remote dev), tunnel over SSH
  or a VPN rather than opening a port.
- Upstream provider URLs are checked at config-load time; a typo
  in `base_url` fails fast rather than silently reaching the wrong
  endpoint.

---

## 4. What CI does (and does not) enforce

| Gate | Enforced in CI? | Rationale |
|---|---|---|
| `pytest` (full suite) | Yes | Core regression surface |
| `ruff check` | Yes | Catches real bugs cheaply |
| Forbidden-SDK grep | Yes | Architectural invariant (§2.1) |
| `uv sync --frozen` | Yes | Lockfile drift = fail (§2.2) |
| `gitleaks` | Yes | Secret leak detection (§1) |
| `pip-audit` | Yes | PyPA CVE feed (§2.3) |
| OSV-Scanner | Yes | OSV CVE feed (§2.3) |
| `dependency-review-action` | PR only | Blocks new vulnerable deps at PR time (§2.4) |
| `ruff format --check` | **No** | Cosmetic; run locally |
| `mypy --strict` | **No** | Run locally if you want it; `pytest` is the functional source of truth |

Style and strict typing matter during development, but in CI they
compete for attention with the security gates. For a one-person
project, the explicit choice is to let them be local concerns.

### Run all CI gates locally

```bash
uv sync --frozen --extra dev
uv run ruff check .
uv run pytest -v
uv export --frozen --no-emit-project --no-hashes --extra dev --format requirements-txt -o requirements-audit.txt
uv run --with pip-audit pip-audit --strict -r requirements-audit.txt
grep -RnE "^\s*(import|from)\s+(anthropic|openai|litellm|langchain)" coderouter/ && echo FAIL || echo OK
```

---

## 5. Reporting a vulnerability

If you find an issue that could compromise a user's keys, leak
request content, or let an attacker pivot from the router to an
upstream provider account, do not open a public issue.

1. Open a GitHub Security Advisory:
   `https://github.com/zephel01/CodeRouter/security/advisories/new`
2. Include a reproducer if possible.
3. Expect acknowledgment within a few days — this is a personal
   project, not a 24×7 service.

Non-security bugs go in the normal issue tracker.

---

## 6. Policy update log

- **v1.0 (2026-04)** — Initial security.md. CI re-scoped to
  regression + supply-chain after the v1.0.0 umbrella. Dependabot
  enabled for both `pip` and `github-actions`. OSV-Scanner and
  dependency-review-action added; `mypy --strict` and
  `ruff format --check` dropped from CI.
- **v2.13.0 (2026-08)** — Implicit `./providers.yaml` discovery is now
  opt-in behind `CODEROUTER_ALLOW_CWD_CONFIG` (§2.6); `restart_command`
  moved from shell dispatch to argv (`shell=False`).
- **v2.14.0 (2026-08)** — `credential.source: cli_session` (§1.1),
  secret scrubbing across every log sink (§1.2),
  `coderouter doctor --check-secrets` (§1.3), and opt-in
  `CODEROUTER_METRICS_TOKEN` auth for `/dashboard`, `/metrics.json` and
  `/metrics` (§3).

*Last updated: 2026-08-10 (as of v2.14.0).*
