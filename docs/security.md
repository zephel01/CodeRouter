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

**Operator checklist.**

- Put keys in `~/.zshenv` / `~/.bashrc` / `launchctl setenv`, or a
  secret manager (1Password CLI, `op run --env-file=...`, macOS
  Keychain). Not in `.env` files inside the repo.
- Rotate provider keys periodically; the router has no state tying
  a key to any ongoing request.
- Never paste a key into an issue or PR comment.

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

---

## 3. Network posture

CodeRouter binds to `127.0.0.1` by default (`coderouter serve --host`).
It does not expose itself on `0.0.0.0` unless the operator explicitly
opts in. There is no authentication on the HTTP ingress — the trust
boundary is "loopback only."

**Operator checklist.**

- Do not bind to `0.0.0.0` on a multi-user host without a separate
  reverse proxy that enforces auth.
- If exposing over the network (e.g. remote dev), tunnel over SSH
  or a VPN rather than opening a port.
- Upstream provider URLs are checked at config-load time; a typo
  in `base_url` fails fast rather than silently reaching the wrong
  endpoint.

---

## 4. What CI does (and does not) enforce

| Gate | Enforced in CI? | Rationale |
|---|---|---|
| `pytest` (453 tests) | Yes | Core regression surface |
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
