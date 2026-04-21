"""CLI entrypoint tests (v0.6-A onward).

The CLI's primary contract is environment-variable handoff to the uvicorn
worker process. We don't actually launch uvicorn here — we monkeypatch
``uvicorn.run`` so the test asserts on the env / args the CLI prepared
just before the handoff.

This is the cleanest place to pin the precedence rules ('--mode' wins
over 'CODEROUTER_MODE' wins over the YAML default) without spinning up
a real server.
"""

from __future__ import annotations

import pytest

from coderouter import cli


@pytest.fixture
def fake_uvicorn(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Replace uvicorn.run with a recorder so serve() returns immediately."""
    captured: dict[str, object] = {}

    def _record(target: str, **kwargs: object) -> None:
        captured["target"] = target
        captured.update(kwargs)

    monkeypatch.setattr(cli.uvicorn, "run", _record)
    return captured


def test_serve_without_mode_does_not_set_mode_env(
    fake_uvicorn: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bare ``coderouter serve`` should leave CODEROUTER_MODE alone.

    Pre-existing env state must survive — the CLI is additive, not
    coercive. (If the user pre-exported CODEROUTER_MODE, the bare CLI
    invocation should not stomp it.)
    """
    monkeypatch.setenv("CODEROUTER_MODE", "preset-by-shell")
    rc = cli.main(["serve"])
    assert rc == 0
    import os

    assert os.environ["CODEROUTER_MODE"] == "preset-by-shell"


def test_mode_flag_sets_env_var_for_uvicorn_worker(
    fake_uvicorn: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--mode foo`` must export CODEROUTER_MODE=foo before uvicorn.run.

    The worker process (potentially launched fresh under --reload) reads
    the env var via ``coderouter.config.loader``; the CLI's only job is
    to plant the value in the parent process env.
    """
    rc = cli.main(["serve", "--mode", "claude-code"])
    assert rc == 0
    import os

    assert os.environ["CODEROUTER_MODE"] == "claude-code"


def test_mode_flag_overrides_preexisting_env(
    fake_uvicorn: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit ``--mode`` must beat a pre-set CODEROUTER_MODE.

    Precedence: CLI arg > env > YAML default. The arg is the loudest
    user signal — overriding shell env means a typo in your shell config
    can't lock you out of an explicit per-invocation choice.
    """
    monkeypatch.setenv("CODEROUTER_MODE", "stale-shell-value")
    rc = cli.main(["serve", "--mode", "free-only"])
    assert rc == 0
    import os

    assert os.environ["CODEROUTER_MODE"] == "free-only"


def test_mode_flag_strips_surrounding_whitespace(
    fake_uvicorn: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defensive: shell quoting accidents shouldn't propagate to the worker.

    ``coderouter serve --mode " coding "`` should set
    ``CODEROUTER_MODE=coding`` — anything else surfaces as a confusing
    "profile not found: ' coding '" error several layers down.
    """
    rc = cli.main(["serve", "--mode", "  coding  "])
    assert rc == 0
    import os

    assert os.environ["CODEROUTER_MODE"] == "coding"


def test_serve_passes_config_via_env(
    fake_uvicorn: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sanity for the existing --config path (regression guard for v0.6-A)."""
    rc = cli.main(["serve", "--config", "/tmp/whatever.yaml"])
    assert rc == 0
    import os

    assert os.environ["CODEROUTER_CONFIG"] == "/tmp/whatever.yaml"


# ---------------------------------------------------------------------------
# v0.7-B: `coderouter doctor --check-model <provider>` wiring tests.
#
# The probe logic itself lives in `coderouter.doctor` and has its own test
# suite (``tests/test_doctor.py``). These tests focus on the CLI's two
# jobs: (1) route the ``doctor`` subcommand to the probe entry point with
# the right arguments; (2) map load / probe errors to the right exit
# codes + stderr messages.
# ---------------------------------------------------------------------------


def _fake_config() -> object:
    """A sentinel object that `load_config` returns; the test doesn't need
    it to be a real CodeRouterConfig because ``run_check_model_sync`` is
    the next thing monkeypatched."""
    return object()


def test_doctor_check_model_required(monkeypatch: pytest.MonkeyPatch) -> None:
    """``coderouter doctor`` without --check-model should fail argparse.

    Keeps the CLI narrowly scoped to the v0.7-B deliverable — other
    subcommands (like a future static-lint mode) will be added in later
    releases with their own flags.
    """
    with pytest.raises(SystemExit):
        cli.main(["doctor"])


def test_doctor_invokes_run_check_model_sync_with_provider_name(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI must pass the --check-model arg through to the doctor module."""
    import coderouter.doctor as doctor_mod
    from coderouter.doctor import ProbeResult, ProbeVerdict

    called: dict[str, object] = {}

    def _fake_load_config(path: str | None) -> object:
        called["config_path"] = path
        return _fake_config()

    def _fake_run_check_model(config: object, provider_name: str) -> object:
        called["provider_name"] = provider_name
        # Return a minimal report so `format_report` can render it.
        from coderouter.config.capability_registry import ResolvedCapabilities

        report = doctor_mod.DoctorReport(
            provider_name=provider_name,
            provider=None,  # type: ignore[arg-type]
            resolved_caps=ResolvedCapabilities(),
        )
        report.results = [
            ProbeResult(name="auth+basic-chat", verdict=ProbeVerdict.OK, detail="ok"),
        ]
        return report

    def _fake_format_report(report: object) -> str:
        return "REPORT-TEXT"

    def _fake_exit_code(report: object) -> int:
        return 0

    monkeypatch.setattr("coderouter.config.loader.load_config", _fake_load_config)
    monkeypatch.setattr(doctor_mod, "run_check_model_sync", _fake_run_check_model)
    monkeypatch.setattr(doctor_mod, "format_report", _fake_format_report)
    monkeypatch.setattr(doctor_mod, "exit_code_for", _fake_exit_code)

    rc = cli.main(["doctor", "--check-model", "myprov"])
    assert rc == 0
    assert called["provider_name"] == "myprov"
    out = capsys.readouterr().out
    assert "REPORT-TEXT" in out


def test_doctor_propagates_needs_tuning_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """When the probe reports NEEDS_TUNING, CLI must exit 2 (CI contract)."""
    import coderouter.doctor as doctor_mod

    monkeypatch.setattr("coderouter.config.loader.load_config", lambda path: _fake_config())
    monkeypatch.setattr(doctor_mod, "run_check_model_sync", lambda cfg, name: object())
    monkeypatch.setattr(doctor_mod, "format_report", lambda r: "X")
    monkeypatch.setattr(doctor_mod, "exit_code_for", lambda r: 2)

    rc = cli.main(["doctor", "--check-model", "foo"])
    assert rc == 2


def test_doctor_returns_one_when_provider_not_in_config(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Unknown provider → KeyError from check_model → CLI exits 1 with stderr."""
    import coderouter.doctor as doctor_mod

    monkeypatch.setattr("coderouter.config.loader.load_config", lambda path: _fake_config())

    def _raise_key(cfg: object, name: str) -> object:
        raise KeyError(f"provider {name!r} not found. Known: ['foo']")

    monkeypatch.setattr(doctor_mod, "run_check_model_sync", _raise_key)

    rc = cli.main(["doctor", "--check-model", "missing"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "missing" in err
    assert "foo" in err


def test_doctor_returns_one_when_config_file_missing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Config file not found → CLI exits 1 with stderr."""

    def _raise_fnf(path: str | None) -> object:
        raise FileNotFoundError("providers.yaml not found. Searched: ...")

    monkeypatch.setattr("coderouter.config.loader.load_config", _raise_fnf)

    rc = cli.main(["doctor", "--check-model", "anything"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "providers.yaml not found" in err


def test_doctor_honors_config_path(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--config must be threaded into load_config."""
    import coderouter.doctor as doctor_mod

    captured: dict[str, str | None] = {}

    def _fake_load_config(path: str | None) -> object:
        captured["path"] = path
        return _fake_config()

    monkeypatch.setattr("coderouter.config.loader.load_config", _fake_load_config)
    monkeypatch.setattr(doctor_mod, "run_check_model_sync", lambda cfg, name: object())
    monkeypatch.setattr(doctor_mod, "format_report", lambda r: "")
    monkeypatch.setattr(doctor_mod, "exit_code_for", lambda r: 0)

    cli.main(["doctor", "--check-model", "foo", "--config", "/tmp/custom.yaml"])
    assert captured["path"] == "/tmp/custom.yaml"


# ---------------------------------------------------------------------------
# v1.5-C: `coderouter stats` wiring tests.
#
# The data / render layer is tested in ``tests/test_cli_stats.py``. Here
# we only validate that argparse plumbs through to ``cli_stats.main`` with
# the right args and that the exit code is propagated.
# ---------------------------------------------------------------------------


def test_stats_dispatches_to_cli_stats_main(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``coderouter stats --once`` calls cli_stats.main with the parsed args."""
    captured: dict[str, object] = {}

    def _fake_stats_main(url: str, *, interval: float, once: bool) -> int:
        captured["url"] = url
        captured["interval"] = interval
        captured["once"] = once
        return 0

    monkeypatch.setattr("coderouter.cli_stats.main", _fake_stats_main)

    rc = cli.main(["stats", "--once", "--url", "http://example/metrics.json"])
    assert rc == 0
    assert captured["url"] == "http://example/metrics.json"
    assert captured["once"] is True


def test_stats_defaults_match_cli_stats_constants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Omitting flags should fall back to DEFAULT_URL / DEFAULT_INTERVAL_S."""
    from coderouter.cli_stats import DEFAULT_INTERVAL_S, DEFAULT_URL

    captured: dict[str, object] = {}

    def _fake_stats_main(url: str, *, interval: float, once: bool) -> int:
        captured["url"] = url
        captured["interval"] = interval
        captured["once"] = once
        return 0

    monkeypatch.setattr("coderouter.cli_stats.main", _fake_stats_main)

    cli.main(["stats", "--once"])
    assert captured["url"] == DEFAULT_URL
    assert captured["interval"] == DEFAULT_INTERVAL_S


def test_stats_propagates_nonzero_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the stats runner returns 2 (fetch failure), the CLI must too."""
    monkeypatch.setattr(
        "coderouter.cli_stats.main", lambda url, *, interval, once: 2
    )
    rc = cli.main(["stats", "--once"])
    assert rc == 2
