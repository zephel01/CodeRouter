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
