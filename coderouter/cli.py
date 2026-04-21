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
    doctor.add_argument(
        "--check-model",
        metavar="PROVIDER",
        required=True,
        help=(
            "Name of a provider declared in providers.yaml. The doctor "
            "targets exactly one provider per invocation; re-run with a "
            "different name to check another."
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
    """v0.7-B: drive ``coderouter doctor --check-model <provider>``.

    Kept as a small function rather than a nested import site so tests
    that monkeypatch the doctor module have a stable attribute
    (``coderouter.cli._run_doctor``) to target. The actual probe logic
    lives in ``coderouter.doctor`` — this just wires load_config + the
    doctor entry point together and pipes output to stdout.

    Errors surfaced here (config not found, unknown provider name) map
    to exit code 1 with a terse stderr message; probe-level failures
    map via ``doctor.exit_code_for()``.
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


if __name__ == "__main__":
    sys.exit(main())
