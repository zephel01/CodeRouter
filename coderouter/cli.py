"""CLI entry: `coderouter serve` (and friends)."""

from __future__ import annotations

import argparse
import sys

import uvicorn

from coderouter import __version__


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="coderouter",
        description="Local-first, free-first, fallback-built-in LLM router.",
    )
    parser.add_argument("--version", action="version", version=f"coderouter {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Run the HTTP server.")
    serve.add_argument("--host", default="127.0.0.1", help="Bind host (default 127.0.0.1)")
    serve.add_argument("--port", type=int, default=4000, help="Bind port (default 4000)")
    serve.add_argument(
        "--config",
        default=None,
        help="Path to providers.yaml. Defaults to $CODEROUTER_CONFIG, "
        "./providers.yaml, or ~/.coderouter/providers.yaml.",
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
            "docs/troubleshooting.md §5 for 1Password / direnv / sops "
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
            "looks for `./.env` then `~/.coderouter/.env`. "
            "See docs/troubleshooting.md §5 for the threat model."
        ),
    )
    doctor.add_argument(
        "--config",
        default=None,
        help=(
            "Path to providers.yaml. Defaults to $CODEROUTER_CONFIG, "
            "./providers.yaml, or ~/.coderouter/providers.yaml."
        ),
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
                    print(f"serve: --env-file: {exc}", file=sys.stderr)
                    return 1
                except EnvFileError as exc:
                    print(f"serve: --env-file: {exc}", file=sys.stderr)
                    return 1
                # Single-line summary so the operator can verify keys
                # actually landed (vs being skipped because they were
                # already in the environment). We deliberately log key
                # NAMES only, never values — secrets must not leak via
                # stdout / stderr.
                if applied:
                    print(
                        f"serve: --env-file {path}: loaded {len(applied)} "
                        f"variable(s): {', '.join(sorted(applied))}",
                        file=sys.stderr,
                    )
                else:
                    print(
                        f"serve: --env-file {path}: 0 variables applied "
                        f"(all keys already in environment, "
                        f"--env-file-override disabled)",
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

    if args.command == "stats":
        # v1.5-C: stats is intentionally a thin wrapper — all logic
        # (fetch, render, curses loop) lives in coderouter.cli_stats so
        # the CLI file stays focused on argparse wiring.
        from coderouter.cli_stats import main as stats_main

        return stats_main(args.url, interval=args.interval, once=args.once)

    print(f"unknown command: {args.command}", file=sys.stderr)
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
    if args.check_model is None and args.check_env is None:
        print(
            "doctor: provide --check-model PROVIDER and/or --check-env [PATH]",
            file=sys.stderr,
        )
        return 1

    worst_exit = 0

    # v1.6.3: --check-env runs first because it's cheap (no HTTP) and
    # because if .env is leaking secrets that's a more urgent thing for
    # the operator to see than a downstream model issue.
    if args.check_env is not None:
        worst_exit = max(worst_exit, _run_check_env(args.check_env))

    if args.check_model is not None:
        worst_exit = max(worst_exit, _run_check_model(args))

    return worst_exit


def _run_check_model(args: argparse.Namespace) -> int:
    """v0.7-B: per-provider HTTP capability probe."""
    from coderouter.config.loader import load_config
    from coderouter.doctor import (
        exit_code_for,
        format_report,
        run_check_model_sync,
    )

    try:
        config = load_config(args.config)
    except FileNotFoundError as exc:
        print(f"doctor: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # pydantic ValidationError, YAML parse error, etc.
        print(f"doctor: failed to load config: {exc}", file=sys.stderr)
        return 1

    try:
        report = run_check_model_sync(config, args.check_model)
    except KeyError as exc:
        print(f"doctor: {exc}", file=sys.stderr)
        return 1

    print(format_report(report))
    return exit_code_for(report)


def _run_check_env(arg_value: str) -> int:
    """v1.6.3: filesystem / git security checks for `.env`.

    ``arg_value`` is the value argparse hands us:
      * ``""``  → bare ``--check-env`` with no PATH; auto-discover
                  (./.env then ~/.coderouter/.env).
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
        candidates = [Path.cwd() / ".env", Path.home() / ".coderouter" / ".env"]
        target = next((c for c in candidates if c.exists()), candidates[0])
        # Even if neither exists, run check_env_security against the
        # first candidate — its existence check will SKIP loudly so the
        # operator knows nothing was found.

    report = check_env_security(target)
    print(format_env_security_report(report))
    return exit_code_for_env_security(report)


if __name__ == "__main__":
    sys.exit(main())
