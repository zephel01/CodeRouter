"""CLI entry: `coderouter-t serve` (and `coderouter` alias)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uvicorn

from coderouter import __version__
from coderouter.messages import tr

# Bind addresses that keep the server loopback-only. Anything else means the
# operator is deliberately exposing CodeRouter beyond this machine, at which
# point the v2.7.0 Host-header validation (DNS-rebinding guard) will 403 every
# request whose Host is not allow-listed — a combination that has confused
# real users ("worked on 2.6, LAN access broken on 2.7").
_LOOPBACK_BIND_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

def _external_bind_warning(host: str, allowed_hosts_env: str | None) -> str | None:
    """Return a startup warning when binding beyond loopback needs config.

    Fires only for the confusing combination: non-loopback bind (the operator
    wants LAN/external access) while ``CODEROUTER_ALLOWED_HOSTS`` is unset
    (so every non-loopback Host header will be rejected with 403). Returns
    the warning text, or None when the configuration is coherent.
    """
    if host in _LOOPBACK_BIND_HOSTS:
        return None
    if allowed_hosts_env and allowed_hosts_env.strip():
        return None
    return tr("W1101_EXTERNAL_BIND", host=host)

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="coderouter-t",
        description="Local-first, free-first, fallback-built-in LLM router (translate fork).",
    )
    parser.add_argument("--version", action="version", version=f"coderouter-t {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Run the HTTP server.")
    serve.add_argument("--host", default="127.0.0.1", help="Bind host (default 127.0.0.1)")
    serve.add_argument("--port", type=int, default=4000, help="Bind port (default 4000)")
    serve.add_argument(
        "--config",
        default=None,
        help="Path to providers.yaml. Defaults to $CODEROUTER_CONFIG, "
        "./providers.yaml, or ~/.coderouter-t/providers.yaml.",
    )
    serve.add_argument(
        "--mode",
        default=None,
        help=(
            "Override the YAML default_profile for this server instance. "
            "Equivalent to setting CODEROUTER_MODE=<profile>. "
            "Per-request overrides via header/body still win. "
            "Unknown profile names fail fast at startup."
        ),
    )
    serve.add_argument(
        "--reload", action="store_true", help="Auto-reload on code change (dev only)."
    )
    serve.add_argument("--log-level", default="info", help="uvicorn log level (default: info)")

    # v1.6.3: `--env-file PATH` is a thin gateway between CodeRouter and any
    # tool that emits `.env` (1Password CLI `op run --env-file=...`, sops,
    # direnv, plain hand-edited files). Files are parsed by
    # ``coderouter.config.env_file`` (stdlib only, see env_file.py docstring
    # for the supported subset of `.env` syntax). Multiple --env-file flags
    # layer left-to-right; later files fill in gaps but DO NOT overwrite
    # already-set environment variables (so an explicit shell-level
    # `export FOO=...` always wins). Use `--env-file-override` to flip that.
    serve.add_argument(
        "--env-file",
        metavar="PATH",
        action="append",
        default=None,
        help=(
            "Load environment variables from a `.env`-style file BEFORE "
            "binding the server. Repeat to layer multiple files. By "
            "default, file values do NOT override variables already in "
            "the environment (the shell `export` wins). See "
            "docs/guides/troubleshooting.md §5 for 1Password / direnv / sops "
            "integration recipes."
        ),
    )
    serve.add_argument(
        "--env-file-override",
        action="store_true",
        help=(
            "When loading --env-file, overwrite variables that are already "
            "set in the environment. Off by default (shell wins)."
        ),
    )

    # v0.7-B: `coderouter doctor --check-model <provider>` runs a small
    # live-probe suite against one provider and reports per-capability
    # verdicts + suggested YAML patches. See coderouter/doctor.py for
    # probe details and exit-code semantics (0/1/2).
    doctor = sub.add_parser(
        "doctor",
        help="Diagnose a provider's capabilities (v0.7-B).",
        description=(
            "Run live probes against a provider from providers.yaml and "
            "compare observed behavior with the registry / providers.yaml "
            "declarations. Emits copy-paste YAML patches on mismatch. "
            "Exit codes: 0 match, 2 needs tuning, 1 probe failed to run."
        ),
    )
    # v0.7-B: --check-model targets one provider's HTTP capabilities.
    # v1.6.3: --check-env targets a `.env` file's local-fs security
    #         (perms / .gitignore / git tracking). Either is acceptable
    #         alone; both can be passed in one invocation, in which case
    #         env-security runs first and the exit code is the worst of
    #         the two reports (so CI guarding against leaks AND broken
    #         providers can use a single command).
    doctor.add_argument(
        "--check-model",
        metavar="PROVIDER",
        default=None,
        help=(
            "Name of a provider declared in providers.yaml. The doctor "
            "targets exactly one provider per invocation; re-run with a "
            "different name to check another."
        ),
    )
    doctor.add_argument(
        "--check-env",
        metavar="PATH",
        nargs="?",
        const="",  # bare `--check-env` (no PATH) → use default discovery
        default=None,
        help=(
            "Run env-security checks against a `.env`-style file: "
            "POSIX file mode (0600 expected), .gitignore coverage, "
            "and git-tracking state. Bare `--check-env` (no PATH) "
            "looks for `./.env` then `~/.coderouter-t/.env`. "
            "See docs/guides/troubleshooting.md §5 for the threat model."
        ),
    )
    # v2.14.0: --check-secrets is the runtime counterpart to --check-env.
    # --check-env asks "is the file holding my keys protected?"; this asks
    # "does the running process know its own secrets, and did any of them
    # already reach a log file?". The second question is the one that used
    # to have no answer at all.
    doctor.add_argument(
        "--check-secrets",
        action="store_true",
        help=(
            "Audit credential hygiene: prove the log-redaction filter "
            "actually scrubs, report which declared api_key_env vars are "
            "set, flag credentials pasted into a base_url, and scan the "
            "already-written requests.jsonl / audit.jsonl under state_dir "
            "for live key values. Exit 0 clean / 2 needs attention / 1 leak."
        ),
    )
    doctor.add_argument(
        "--config",
        default=None,
        help=(
            "Path to providers.yaml. Defaults to $CODEROUTER_CONFIG, "
            "./providers.yaml, or ~/.coderouter-t/providers.yaml."
        ),
    )
    # v1.7-B (#3): --apply writes the doctor-emitted YAML patches back
    # into providers.yaml / model-capabilities.yaml while preserving
    # comments and key order. --dry-run is the same path minus the file
    # write — prints a unified diff (``git apply``-compatible) for review.
    # Bare ``--dry-run`` (without ``--apply``) is the canonical "preview"
    # form; ``--apply --dry-run`` is also accepted as an explicit synonym
    # so muscle-memory from ``git apply --dry-run`` works either way.
    # Both flags are no-ops when --check-model is absent (--check-env
    # has its own remediation surface and is not in scope for --apply).
    # Implementation lives in coderouter/doctor_apply.py — round-trip
    # via the optional ``ruamel.yaml`` dependency, see that module's
    # docstring for the contract and shape invariants.
    doctor.add_argument(
        "--apply",
        action="store_true",
        help=(
            "After --check-model, write the suggested patches back into "
            "providers.yaml / model-capabilities.yaml. A `.bak` backup is "
            "created next to each modified file. Idempotent: a re-run "
            "after a successful apply is a no-op (no write, exit 0). "
            "Requires the optional `ruamel.yaml` dependency — install "
            "via `pip install coderouter-t[doctor]` (or `coderouter-cli[doctor]` for upstream)."
        ),
    )
    doctor.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Preview --apply changes as a unified diff without writing "
            "to disk. Implies --apply mode for diff generation. The "
            "output is `git apply`-compatible so it can be saved and "
            "applied later (or piped to `patch -p0`)."
        ),
    )

    # v2.14.0: `coderouter rollback` — the missing half of --apply.
    # Both writers (doctor --apply, vscode-init) already dropped a `.bak`
    # next to every file they rewrote; nothing could put it back. Restore
    # is a swap, not an overwrite, so running it twice returns you to
    # where you started rather than destroying the newer version.
    rollback = sub.add_parser(
        "rollback",
        help="Restore files a previous --apply / vscode-init rewrote (v2.14.0).",
        description=(
            "Swap each managed file with its .bak sibling: providers.yaml, "
            "~/.coderouter-t/model-capabilities.yaml, and (with --workspace) "
            ".vscode/settings.json and .envrc. The current contents become "
            "the new .bak, so a second run toggles back. "
            "Exit codes: 0 restored, 2 nothing to restore, 1 a restore failed."
        ),
    )
    rollback.add_argument(
        "--config",
        default=None,
        help=(
            "Path to providers.yaml. Defaults to the same file the loader "
            "would read ($CODEROUTER_CONFIG, then ~/.coderouter-t/providers.yaml)."
        ),
    )
    rollback.add_argument(
        "--workspace",
        metavar="DIR",
        default=None,
        help=(
            "Also restore the vscode-init outputs under DIR: "
            "DIR/.vscode/settings.json and DIR/.envrc."
        ),
    )
    rollback.add_argument(
        "--path",
        action="append",
        metavar="FILE",
        default=None,
        help=(
            "Restore exactly this file from its .bak sibling. Repeatable. "
            "When given, the default discovery set is not used."
        ),
    )
    rollback.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be restored without touching any file.",
    )

    # v1.5-C: `coderouter stats` — live TUI over GET /metrics.json.
    # Lazy-imports ``curses`` inside the runner so the CLI boot stays
    # snappy and environments without curses (rare, but e.g. minimal
    # containers) can still use ``--once`` for script-mode dumps.
    stats = sub.add_parser(
        "stats",
        help="Live TUI over the metrics endpoint (v1.5-C).",
        description=(
            "Connect to a running `coderouter serve` and render providers, "
            "fallback/gate counters, and a recent-events ring. Refreshes "
            "once per --interval seconds. Use --once for a single plain-"
            "text dump (also the default when stdout is not a TTY, so "
            "`coderouter stats | grep foo` works in scripts)."
        ),
    )
    from coderouter.cli_stats import DEFAULT_INTERVAL_S, DEFAULT_URL

    stats.add_argument(
        "--url",
        default=DEFAULT_URL,
        help=f"Metrics endpoint URL (default {DEFAULT_URL}).",
    )
    stats.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL_S,
        help=f"Refresh interval in seconds (default {DEFAULT_INTERVAL_S}).",
    )
    stats.add_argument(
        "--once",
        action="store_true",
        help="Print one snapshot as plain text and exit (scripts / non-tty).",
    )

    # v2.0-K: `coderouter audit` — read structured JSONL audit log.
    audit = sub.add_parser(
        "audit",
        help="Read the structured audit log (v2.0-K).",
        description=(
            "Read and filter the JSONL audit log written by `coderouter serve` "
            "when state_dir and audit_log are configured. Shows guard activations, "
            "chain fallbacks, budget warnings, self-healing events, and drift "
            "transitions in chronological order."
        ),
    )
    audit.add_argument(
        "--state-dir",
        default=None,
        help=(
            "Path to the state directory containing audit.jsonl. "
            "Defaults to ~/.coderouter-t/state/."
        ),
    )
    audit.add_argument(
        "--tail",
        type=int,
        default=None,
        metavar="N",
        help="Show only the last N entries.",
    )
    audit.add_argument(
        "--filter",
        default=None,
        metavar="EVENT",
        help="Only entries whose event name contains this substring (case-insensitive).",
    )
    audit.add_argument(
        "--since",
        default=None,
        metavar="DATETIME",
        help="Only entries with ts >= this ISO 8601 prefix (e.g. '2026-05-06').",
    )
    audit.add_argument(
        "--summary",
        action="store_true",
        help="Print event type → count summary instead of individual entries.",
    )

    # v2.0-K (Replay): `coderouter replay` — statistical A/B analysis
    # of request journal metadata across providers.
    replay = sub.add_parser(
        "replay",
        help="Statistical replay analysis of request journal (v2.0-K).",
        description=(
            "Read the request metadata journal and display per-provider "
            "statistics (token counts, cost, cache hit ratios). Optionally "
            "compare two providers side-by-side. Request/response bodies "
            "are not recorded, so this is statistical analysis — not "
            "literal re-execution."
        ),
    )
    replay.add_argument(
        "--state-dir",
        default=None,
        help=(
            "Path to the state directory containing requests.jsonl. "
            "Defaults to ~/.coderouter-t/state/."
        ),
    )
    replay.add_argument(
        "--log",
        default=None,
        metavar="PATH",
        help="Direct path to the request journal JSONL file (overrides --state-dir).",
    )
    replay.add_argument(
        "--provider",
        default=None,
        metavar="NAME",
        help="Filter entries to this provider only.",
    )
    replay.add_argument(
        "--compare",
        nargs=2,
        metavar=("A", "B"),
        default=None,
        help="Compare two providers side-by-side (e.g. --compare anthropic-api openrouter-free).",
    )
    replay.add_argument(
        "--since",
        default=None,
        metavar="DATETIME",
        help="Only entries with ts >= this ISO 8601 prefix (e.g. '2026-05-06').",
    )
    replay.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Use only the last N entries (applied after --since and --provider filters).",
    )
    # P1-6: --suggest-rules — statistical analysis → routing rule proposals.
    replay.add_argument(
        "--suggest-rules",
        action="store_true",
        help=(
            "P1-6: analyse the request journal and print actionable routing "
            "rule suggestions as copy-paste YAML snippets. Suggestions cover "
            "provider reordering by cost, prompt_cache enablement, drift "
            "detection configuration, and goal profile creation. "
            "Can be combined with --since / --limit to scope the analysis window."
        ),
    )

    # v2.10-A: `coderouter vscode-init` — scaffold a VSCode workspace so
    # Claude Code launched from the integrated terminal auto-points at
    # CodeRouter (no manual env-var juggling). Optionally writes .envrc
    # for direnv users. Cline / Roo / Continue are covered by the cheat
    # sheet printed on completion and docs/guides/vscode.md — this
    # command deliberately does NOT touch those extensions' settings
    # (their schemas change with their own release cadence).
    vscode_init = sub.add_parser(
        "vscode-init",
        help="Scaffold VSCode workspace settings for CodeRouter (v2.10-A).",
        description=(
            "Write .vscode/settings.json (terminal.integrated.env.*) so a "
            "Claude Code session launched from VSCode's integrated terminal "
            "auto-points at CodeRouter. Optionally emit a direnv .envrc. "
            "Idempotent: safe to re-run. Conflict-aware: refuses to overwrite "
            "differing existing values without --force."
        ),
    )
    vscode_init.add_argument(
        "--target",
        default=".",
        metavar="PATH",
        help="Workspace root (default: current directory).",
    )
    vscode_init.add_argument(
        "--port",
        type=int,
        default=None,
        metavar="PORT",
        help=(
            "CodeRouter port for ANTHROPIC_BASE_URL. Defaults to 8088 "
            "(matches every docs / quickstart example). Note that "
            "`coderouter serve` alone defaults to 4000; if you serve on "
            "4000, pass --port 4000 here too."
        ),
    )
    vscode_init.add_argument(
        "--profile",
        default=None,
        metavar="NAME",
        help=(
            "Preload CODEROUTER_MODE=<profile> so the VSCode terminal "
            "routes to a non-default profile without a per-request header."
        ),
    )
    vscode_init.add_argument(
        "--with-envrc",
        action="store_true",
        help=(
            "Also write .envrc (direnv). Run `direnv allow` once after "
            "generation. Tip: keep secrets in a separate .envrc.local."
        ),
    )
    vscode_init.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Compute all changes and print unified diffs, but do not write "
            "any files. Byte-identical to the write path minus the final "
            "os.replace."
        ),
    )
    vscode_init.add_argument(
        "--force",
        action="store_true",
        help=(
            "Overwrite existing conflicting values. Without this flag, a "
            "conflict is reported with a diff and the file is left "
            "untouched (exit 2)."
        ),
    )

    return parser

def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.command == "serve":
        # We pass the config path via env so the app factory (loaded by uvicorn
        # in a fresh process when --reload is on) can pick it up.
        import os

        # v1.6.3: --env-file is processed FIRST so subsequent --config /
        # --mode handling (and the worker's eventual os.environ.get(...)
        # lookups) can see file-loaded values. We don't auto-source ./.env
        # — the user must opt in explicitly with --env-file ./.env, which
        # keeps the "what env reaches the worker?" answer 1:1 with the
        # command line and prevents surprise hijacks of API keys.
        if args.env_file:
            from coderouter.config.env_file import EnvFileError, load_env_file

            for path in args.env_file:
                try:
                    applied = load_env_file(path, override=args.env_file_override)
                except FileNotFoundError as exc:
                    print(tr("E1102_ENV_FILE_NOT_FOUND", error=exc), file=sys.stderr)
                    return 1
                except EnvFileError as exc:
                    print(tr("E1103_ENV_FILE_ERROR", error=exc), file=sys.stderr)
                    return 1
                # Single-line summary so the operator can verify keys
                # actually landed (vs being skipped because they were
                # already in the environment). We deliberately log key
                # NAMES only, never values — secrets must not leak via
                # stdout / stderr.
                if applied:
                    print(
                        tr(
                            "I1104_ENV_FILE_LOADED",
                            path=path,
                            count=len(applied),
                            keys=", ".join(sorted(applied)),
                        ),
                        file=sys.stderr,
                    )
                else:
                    print(
                        tr("I1105_ENV_FILE_EMPTY", path=path),
                        file=sys.stderr,
                    )

        if args.config:
            os.environ["CODEROUTER_CONFIG"] = args.config

        # v0.6-A: --mode translates to CODEROUTER_MODE for the worker. Strip
        # surrounding whitespace defensively — quoting accidents like
        # ``--mode " coding "`` would otherwise surface as confusing
        # "profile not found: ' coding '" errors in the loader.
        if args.mode is not None:
            stripped = args.mode.strip()
            if stripped:
                os.environ["CODEROUTER_MODE"] = stripped

        # v2.7.5: warn about the "bound beyond loopback but Host validation
        # will reject everything" trap BEFORE uvicorn takes over the console,
        # so the hint is the first thing an operator sees.
        warning = _external_bind_warning(
            args.host, os.environ.get("CODEROUTER_ALLOWED_HOSTS")
        )
        if warning:
            print(f"WARNING: {warning}", file=sys.stderr)

        uvicorn.run(
            "coderouter.ingress.app:create_app",
            factory=True,
            host=args.host,
            port=args.port,
            reload=args.reload,
            log_level=args.log_level,
        )
        return 0

    if args.command == "doctor":
        return _run_doctor(args)

    if args.command == "rollback":
        return _run_rollback(args)

    if args.command == "stats":
        # v1.5-C: stats is intentionally a thin wrapper — all logic
        # (fetch, render, curses loop) lives in coderouter.cli_stats so
        # the CLI file stays focused on argparse wiring.
        from coderouter.cli_stats import main as stats_main

        return stats_main(args.url, interval=args.interval, once=args.once)

    if args.command == "audit":
        return _run_audit(args)

    if args.command == "replay":
        return _run_replay(args)

    if args.command == "vscode-init":
        return _run_vscode_init(args)

    print(tr("E1106_UNKNOWN_COMMAND", command=args.command), file=sys.stderr)
    return 2

def _run_doctor(args: argparse.Namespace) -> int:
    """Drive ``coderouter doctor`` (v0.7-B `--check-model`, v1.6.3 `--check-env`).

    Kept as a small function rather than a nested import site so tests
    that monkeypatch the doctor module have a stable attribute
    (``coderouter.cli._run_doctor``) to target. The actual probe logic
    lives in ``coderouter.doctor`` (HTTP probes) and
    ``coderouter.env_security`` (filesystem / git probes) — this just
    wires the entry points together and pipes output to stdout.

    When both flags are passed, env-security runs first (cheap, local)
    and the model probe runs second; the final exit code is the
    worst-case of the two reports so CI guarding against both leak
    risks AND broken providers can use a single command.
    """
    check_secrets = bool(getattr(args, "check_secrets", False))
    if args.check_model is None and args.check_env is None and not check_secrets:
        print(tr("E1107_DOCTOR_USAGE"), file=sys.stderr)
        return 1

    worst_exit = 0

    # v1.6.3: --check-env runs first because it's cheap (no HTTP) and
    # because if .env is leaking secrets that's a more urgent thing for
    # the operator to see than a downstream model issue.
    if args.check_env is not None:
        worst_exit = max(worst_exit, _run_check_env(args.check_env))

    if check_secrets:
        worst_exit = max(worst_exit, _run_check_secrets(args.config))

    if args.check_model is not None:
        worst_exit = max(worst_exit, _run_check_model(args))

    return worst_exit

def _run_check_model(args: argparse.Namespace) -> int:
    """v0.7-B: per-provider HTTP capability probe.

    v1.7-B (#3): when ``--apply`` or ``--dry-run`` is also set, we run
    the same probes and then route the emitted patches through
    :func:`coderouter.doctor_apply.apply_doctor_patches`. Bare probe
    (no apply / dry-run flags) keeps the original behavior verbatim
    so existing CI integrations don't change shape.
    """
    from coderouter.config.loader import load_config
    from coderouter.doctor import (
        exit_code_for,
        format_report,
        run_check_model_sync,
    )

    try:
        config = load_config(args.config)
    except FileNotFoundError as exc:
        print(tr("E1108_DOCTOR_CONFIG_NOT_FOUND", error=exc), file=sys.stderr)
        return 1
    except Exception as exc:  # pydantic ValidationError, YAML parse error, etc.
        print(tr("E1004_CONFIG_VALIDATION", error=exc), file=sys.stderr)
        return 1

    try:
        report = run_check_model_sync(config, args.check_model)
    except KeyError as exc:
        print(tr("E1108_DOCTOR_CONFIG_NOT_FOUND", error=exc), file=sys.stderr)
        return 1

    print(format_report(report))
    base_exit = exit_code_for(report)

    apply_mode = bool(getattr(args, "apply", False))
    dry_run_mode = bool(getattr(args, "dry_run", False))
    if apply_mode or dry_run_mode:
        # Resolve the same providers.yaml the loader picked up so the
        # apply step writes back to the exact file that was probed
        # (avoids a mismatch when CODEROUTER_CONFIG points elsewhere
        # than the default path).
        config_path = _resolve_config_path(args.config)
        return _run_apply_or_dry_run(
            report=report,
            config_path=config_path,
            write=apply_mode and not dry_run_mode,
            base_exit=base_exit,
        )

    return base_exit

def _resolve_config_path(explicit: str | None) -> Path:
    """Return the file the loader actually read, for ``--apply`` write-back.

    Delegates the search order to
    :func:`coderouter.config.loader.resolve_config_path` rather than
    re-implementing it. Re-implementing it here is exactly how the
    v2.13.0 CWD opt-in change would introduce a silent bug: if the loader
    gates ``./providers.yaml`` behind ``CODEROUTER_ALLOW_CWD_CONFIG`` but
    this copy still searched CWD unconditionally, ``doctor --apply`` would
    write patches back into a file that ``load_config`` never read. Keeping
    one source of truth makes that impossible.

    When nothing exists, falls back to ``~/.coderouter-t/providers.yaml``
    (the loader's last candidate) — the apply step surfaces a clearer
    error against that path than this resolver would.
    """
    from coderouter.config.loader import resolve_config_path

    resolved = resolve_config_path(explicit)
    if resolved is not None:
        return resolved
    return Path.home() / ".coderouter-t" / "providers.yaml"

def _write_verdict_line(*, write: bool, files_written: int) -> None:
    """Print the one-line "did we touch the disk?" verdict.

    Called on **every** exit path of :func:`_run_apply_or_dry_run`.
    The v1.x regression this guards against: an ``--apply`` run that
    silently rewrote providers.yaml while the summary said "already
    applied" and printed neither a diff nor a ``Backup:`` line. Making
    the statement unconditional means no future early-return can go
    quiet about a write again.
    """
    if not write:
        print("  0 files written (dry-run).")
    elif files_written == 0:
        print("  0 files written — no file contents changed.")
    else:
        print(f"  {files_written} file(s) written.")

def _run_apply_or_dry_run(
    *,
    report: object,
    config_path: Path,
    write: bool,
    base_exit: int,
) -> int:
    """v1.7-B (#3): drive ``apply_doctor_patches`` and render the result.

    Returns 0 when the apply step itself is clean (regardless of
    whether the underlying probes flagged ``NEEDS_TUNING``). The
    rationale: once the operator has applied the patches, the next
    ``doctor`` run is the right place to re-evaluate the chain — a
    successful apply should not propagate the "exit 2 / needs tuning"
    signal because the issue is now (presumably) addressed.
    """
    from coderouter.doctor_apply import (
        DoctorApplyError,
        MissingDependencyError,
        apply_doctor_patches,
    )

    print()  # blank line between probe report and apply section
    try:
        result = apply_doctor_patches(
            report=report,
            config_path=config_path,
            write=write,
        )
    except MissingDependencyError as exc:
        print(tr("E1109_DOCTOR_APPLY_ERROR", error=exc), file=sys.stderr)
        _write_verdict_line(write=write, files_written=0)
        return 1
    except DoctorApplyError as exc:
        # Every merge happens before the first byte is written, so an
        # abort here always leaves the disk untouched.
        print(tr("E1109_DOCTOR_APPLY_ERROR", error=exc), file=sys.stderr)
        _write_verdict_line(write=write, files_written=0)
        return 1

    label = "Apply" if write else "Dry-run"
    print(f"{label}: {len(result.target_paths)} target file(s).")
    if result.skipped_unknown_target:
        print(
            f"  warning: {len(result.skipped_unknown_target)} probe(s) "
            f"emitted an unknown target_file value: "
            f"{sorted(set(result.skipped_unknown_target))}",
            file=sys.stderr,
        )

    if result.is_no_op:
        # Distinguish "nothing to do because base_exit was 0" from
        # "nothing to do because everything already applied":
        if base_exit == 0:
            print("  No NEEDS_TUNING patches to apply — chain is healthy.")
        else:
            print(
                f"  All {result.no_op_patches} patch(es) already applied "
                f"— providers.yaml is up to date."
            )
        _write_verdict_line(write=write, files_written=len(result.written_paths))
        _print_reformat_notice(result, write=write)
        return 0

    print(
        f"  {result.changes_applied} patch(es) applied"
        + (f", {result.no_op_patches} already up to date" if result.no_op_patches else "")
        + "."
    )
    for path in result.target_paths:
        diff = result.diffs.get(str(path), "")
        if not diff:
            continue
        print()
        print(diff, end="" if diff.endswith("\n") else "\n")

    _write_verdict_line(write=write, files_written=len(result.written_paths))
    if write:
        for orig, bak in result.backups.items():
            print(f"  Backup: {orig} → {bak}")
    else:
        _print_reformat_notice(result, write=write)
        print()
        print("  (dry-run — no files were modified. Re-run with --apply to write.)")

    return 0

def _print_reformat_notice(result: object, *, write: bool) -> None:
    """Dry-run-only heads-up about cosmetic re-serialization deltas.

    ``reformat_only`` holds targets whose re-dump differs from disk
    without any patch having changed a value — today that is explicit
    ``key: null`` scalars, which ruamel re-emits as empty scalars.
    These bytes are never written, so the note is purely so an operator
    comparing "the diff I expected" with "the file I have" is not
    surprised. Suppressed under ``--apply``, where nothing about them
    is actionable.
    """
    reformat = getattr(result, "reformat_only", None) or {}
    if write or not reformat:
        return
    print(
        f"  note: {len(reformat)} file(s) would re-serialize with cosmetic "
        "differences (e.g. explicit `null` → empty scalar). These are NOT "
        "written — --apply only rewrites files whose values actually changed."
    )
    for path in sorted(reformat):
        print(f"    - {path}")

def _run_audit(args: argparse.Namespace) -> int:
    """v2.0-K: read and display the structured audit log.

    Resolves the audit log path from --state-dir (or default
    ~/.coderouter-t/state/) and renders entries with optional filtering.
    """
    import json

    from coderouter.state.audit_log import read_audit_log, summarize_audit_log

    state_dir = Path(args.state_dir).expanduser() if args.state_dir else (
        Path.home() / ".coderouter-t" / "state"
    )
    log_path = state_dir / "audit.jsonl"

    if not log_path.exists():
        print(tr("E1111_AUDIT_NO_LOG", path=log_path), file=sys.stderr)
        print(f"  {tr('E1112_AUDIT_HINT')}", file=sys.stderr)
        return 1

    entries = read_audit_log(
        log_path,
        tail=args.tail,
        event_filter=args.filter,
        since=args.since,
    )

    if not entries:
        print(tr("I1113_AUDIT_NO_ENTRIES"))
        return 0

    if args.summary:
        summary = summarize_audit_log(entries)
        print(f"Audit log summary ({len(entries)} entries):\n")
        for event, count in summary.items():
            print(f"  {event:<40s} {count:>6d}")
        return 0

    for entry in entries:
        ts = entry.get("ts", "")
        event = entry.get("event", "")
        level = entry.get("level", "")
        # Build a compact one-line display.
        extras = {
            k: v
            for k, v in entry.items()
            if k not in ("ts", "event", "level")
        }
        extra_str = ""
        if extras:
            extra_str = " " + json.dumps(extras, default=str, ensure_ascii=False)
        print(f"[{ts}] {level:<7s} {event}{extra_str}")

    return 0

def _run_replay(args: argparse.Namespace) -> int:
    """v2.0-K (Replay): statistical A/B analysis of request journal.

    Reads the request journal (requests.jsonl) and either displays a
    per-provider summary table or a side-by-side comparison of two
    providers.
    """
    from coderouter.state.replay import (
        compare_providers,
        format_comparison_table,
        format_summary_table,
        summarize_window,
    )
    from coderouter.state.request_log import read_request_log

    # Resolve the journal file path.
    if args.log:
        log_path = Path(args.log).expanduser()
    else:
        state_dir = Path(args.state_dir).expanduser() if args.state_dir else (
            Path.home() / ".coderouter-t" / "state"
        )
        log_path = state_dir / "requests.jsonl"

    if not log_path.exists():
        print(tr("E1114_REPLAY_NO_LOG", path=log_path), file=sys.stderr)
        print(f"  {tr('E1115_REPLAY_HINT')}", file=sys.stderr)
        return 1

    entries = read_request_log(
        log_path,
        provider_filter=args.provider,
        since=args.since,
    )

    if args.limit is not None and args.limit > 0:
        entries = entries[-args.limit:]

    if not entries:
        print(tr("I1116_REPLAY_NO_ENTRIES"))
        return 0

    if getattr(args, "suggest_rules", False):
        # P1-6: statistical rule suggestion mode.
        # Always compute a full window summary (ignores --compare / --provider).
        from coderouter.state.replay import summarize_window as _sw
        from coderouter.state.suggest_rules import format_suggestions, suggest_rules

        # Re-read without provider filter so we see all providers.
        all_entries = read_request_log(log_path, since=args.since)
        if args.limit is not None and args.limit > 0:
            all_entries = all_entries[-args.limit:]
        full_summary = _sw(all_entries)
        suggestions = suggest_rules(full_summary)
        print(f"Request journal: {len(all_entries)} entries analysed")
        print(f"  Window: {full_summary.first_ts} → {full_summary.last_ts}")
        print(f"  Providers: {', '.join(sorted(full_summary.providers))}")
        print()
        print(format_suggestions(suggestions))
        return 0

    if args.compare:
        provider_a, provider_b = args.compare
        comparison = compare_providers(entries, provider_a, provider_b)
        print(format_comparison_table(comparison))
    else:
        summary = summarize_window(entries)
        print(format_summary_table(summary))

    return 0

def _run_vscode_init(args: argparse.Namespace) -> int:
    """v2.10-A: drive :func:`coderouter.vscode_init.run_vscode_init`.

    Kept small and testable — the actual scaffolding logic lives in
    :mod:`coderouter.vscode_init` so tests can exercise it without
    argparse in the way. This wrapper's job is:

    * resolve the ``--target`` argument to a real directory
    * map ``--port`` (default None) to the module's ``DEFAULT_PORT``
    * translate exceptions (missing target directory) to a friendly
      stderr message + exit 1
    * print the formatted result and propagate the module's exit code
    """
    from coderouter.vscode_init import (
        DEFAULT_PORT,
        exit_code_for,
        format_result,
        run_vscode_init,
    )

    target = Path(args.target).expanduser()
    if not target.is_dir():
        print(tr("E1110_VSCODE_TARGET_MISSING", path=target), file=sys.stderr)
        return 1

    port = args.port if args.port is not None else DEFAULT_PORT

    try:
        result = run_vscode_init(
            target,
            port=port,
            profile=args.profile,
            with_envrc=args.with_envrc,
            dry_run=args.dry_run,
            force=args.force,
        )
    except FileNotFoundError as exc:
        # Raised only when target vanishes between the pre-check above
        # and the module's own check — unlikely in practice, but map
        # it to the same shape as the pre-check for consistency.
        print(f"vscode-init: {exc}", file=sys.stderr)
        return 1

    print(format_result(result, dry_run=args.dry_run, port=port))
    return exit_code_for(result)

def _run_check_env(arg_value: str) -> int:
    """v1.6.3: filesystem / git security checks for `.env`.

    ``arg_value`` is the value argparse hands us:
      * ``""``  → bare ``--check-env`` with no PATH; auto-discover
                  (./.env then ~/.coderouter-t/.env).
      * else    → operator-supplied path; use verbatim.
    """
    from pathlib import Path

    from coderouter.env_security import (
        check_env_security,
        exit_code_for_env_security,
        format_env_security_report,
    )

    if arg_value:
        target = Path(arg_value).expanduser()
    else:
        # Auto-discovery: cwd first (project-local), then user-global.
        candidates = [Path.cwd() / ".env", Path.home() / ".coderouter-t" / ".env"]
        target = next((c for c in candidates if c.exists()), candidates[0])
        # Even if neither exists, run check_env_security against the
        # first candidate — its existence check will SKIP loudly so the
        # operator knows nothing was found.

    report = check_env_security(target)
    print(format_env_security_report(report))
    return exit_code_for_env_security(report)

def _run_rollback(args: argparse.Namespace) -> int:
    """v2.14.0: put back what ``--apply`` / ``vscode-init`` overwrote.

    Explicit ``--path`` wins over discovery so an operator can restore one
    file without also reverting an unrelated ``model-capabilities.yaml``
    edit from last week. Discovery resolves providers.yaml through the
    loader's own search order rather than a second copy of it.
    """
    from coderouter.rollback import (
        discover_managed_files,
        exit_code_for_rollback,
        format_rollback_report,
        restore_many,
    )

    explicit = getattr(args, "path", None)
    if explicit:
        targets: list[Path] = [Path(p).expanduser() for p in explicit]
    else:
        try:
            config_path: Path | None = _resolve_config_path(args.config)
        except Exception:
            # No config anywhere is not fatal here — the workspace files
            # and the user-layer capabilities file may still have backups.
            config_path = None
        targets = discover_managed_files(
            config_path=config_path,
            workspace=getattr(args, "workspace", None),
        )

    outcomes = restore_many(targets, dry_run=bool(args.dry_run))
    print(format_rollback_report(outcomes, dry_run=bool(args.dry_run)))
    return exit_code_for_rollback(outcomes)

def _run_check_secrets(config_path: str | None) -> int:
    """v2.14.0: credential-hygiene audit for the running configuration.

    Loads the config through the normal loader — which is also what arms
    the redaction registry — then runs the suite from
    :mod:`coderouter.secret_redaction`. A config that fails to load is a
    blocker (exit 1) rather than a skip: we cannot claim anything about
    secret hygiene for a file we could not read.
    """
    from coderouter.config.loader import load_config
    from coderouter.secret_redaction import (
        check_secret_hygiene,
        exit_code_for_secret_report,
        format_secret_report,
    )

    try:
        config = load_config(config_path)
    except FileNotFoundError as exc:
        print(tr("E1108_DOCTOR_CONFIG_NOT_FOUND", error=exc), file=sys.stderr)
        return 1
    except Exception as exc:
        print(tr("E1004_CONFIG_VALIDATION", error=exc), file=sys.stderr)
        return 1

    report = check_secret_hygiene(config)
    print(format_secret_report(report))
    return exit_code_for_secret_report(report)

if __name__ == "__main__":
    sys.exit(main())
