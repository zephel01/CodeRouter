"""Tests for the v2.13.0 CWD-config opt-in and restart_command warnings.

Both behaviours are implemented as of v2.13.0 (they were merely warned
about ahead of time in v2.12.0):

  * ``coderouter/config/loader.py``: implicit CWD ``providers.yaml``
    discovery is opt-in, gated behind ``CODEROUTER_ALLOW_CWD_CONFIG``.
    When enabled and the CWD step resolves, a ``cwd-config-loaded``
    warning fires; when disabled but a ``./providers.yaml`` exists and
    was ignored, a ``cwd-config-skipped`` warning fires.
  * ``coderouter/config/schemas.py``: ``ProviderConfig.restart_command``
    is dispatched via ``shlex.split`` + ``shell=False``; the validator
    warns (never raises) when a value relies on shell syntax that no
    longer works.

The opt-in / skip behaviour itself is covered further down this module;
these first cases cover the ``cwd-config-loaded`` warning under an
explicit opt-in.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import coderouter.config.loader as loader_module
from coderouter.config.loader import load_config, resolve_config_path
from coderouter.config.schemas import (
    Capabilities,
    CodeRouterConfig,
    FallbackChain,
    ProviderConfig,
)


@pytest.fixture(autouse=True)
def _reset_cwd_warning_once_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset the process-wide "already warned" guard before each test.

    ``_warn_if_cwd_config`` / ``_warn_if_cwd_config_skipped`` intentionally
    emit their warnings only once per process (see loader.py) — a real
    deployment loads config once at startup. Tests, however, run many
    independent scenarios in one pytest process, so each test needs its
    own clean slate to observe (or not observe) the warnings on its own
    terms.

    Windows Path.home parity is handled globally by conftest's
    ``_patch_home_for_windows`` autouse fixture (M-3 DRY fix); no per-file
    patch needed here.
    """
    monkeypatch.setattr(loader_module, "_cwd_config_warning_emitted", False)
    monkeypatch.setattr(loader_module, "_cwd_config_skip_warning_emitted", False)


def _minimal_config_yaml(**provider_overrides: object) -> str:
    provider_kwargs: dict[str, object] = dict(
        name="local",
        base_url="http://localhost:8080/v1",
        model="qwen-coder",
        paid=False,
        capabilities=Capabilities(),
    )
    provider_kwargs.update(provider_overrides)
    cfg = CodeRouterConfig(
        allow_paid=False,
        default_profile="default",
        providers=[ProviderConfig(**provider_kwargs)],
        profiles=[FallbackChain(name="default", providers=["local"])],
    )
    return yaml.safe_dump(cfg.model_dump(mode="json"), sort_keys=False)


# ---------------------------------------------------------------------------
# loader.py: implicit CWD providers.yaml discovery
# ---------------------------------------------------------------------------


def test_cwd_config_emits_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    monkeypatch.setenv("HOME", str(home_dir))

    cwd_dir = tmp_path / "cwd"
    cwd_dir.mkdir()
    (cwd_dir / "providers.yaml").write_text(_minimal_config_yaml(), encoding="utf-8")
    monkeypatch.chdir(cwd_dir)
    # v2.13.0: CWD discovery is opt-in, so the loaded-warning only fires
    # when the operator has explicitly enabled it.
    monkeypatch.setenv("CODEROUTER_ALLOW_CWD_CONFIG", "1")

    with caplog.at_level("WARNING", logger="coderouter.config.loader"):
        cfg = load_config(None)

    assert isinstance(cfg, CodeRouterConfig)
    warnings = [r for r in caplog.records if r.message == "cwd-config-loaded"]
    assert len(warnings) == 1
    assert str(cwd_dir / "providers.yaml") in warnings[0].path  # type: ignore[attr-defined]


def test_cwd_warning_emitted_only_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    monkeypatch.setenv("HOME", str(home_dir))

    cwd_dir = tmp_path / "cwd"
    cwd_dir.mkdir()
    (cwd_dir / "providers.yaml").write_text(_minimal_config_yaml(), encoding="utf-8")
    monkeypatch.chdir(cwd_dir)
    monkeypatch.setenv("CODEROUTER_ALLOW_CWD_CONFIG", "1")

    with caplog.at_level("WARNING", logger="coderouter.config.loader"):
        load_config(None)
        load_config(None)

    warnings = [r for r in caplog.records if r.message == "cwd-config-loaded"]
    assert len(warnings) == 1


def test_explicit_config_path_does_not_warn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    monkeypatch.setenv("HOME", str(home_dir))

    # The explicit path happens to be a file named providers.yaml sitting in
    # CWD — a coincidence, not implicit discovery — and must stay quiet.
    cwd_dir = tmp_path / "cwd"
    cwd_dir.mkdir()
    explicit_path = cwd_dir / "providers.yaml"
    explicit_path.write_text(_minimal_config_yaml(), encoding="utf-8")
    monkeypatch.chdir(cwd_dir)

    with caplog.at_level("WARNING", logger="coderouter.config.loader"):
        cfg = load_config(explicit_path)

    assert isinstance(cfg, CodeRouterConfig)
    warnings = [r for r in caplog.records if r.message == "cwd-config-loaded"]
    assert warnings == []


def test_explicit_env_config_path_does_not_warn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    monkeypatch.setenv("HOME", str(home_dir))

    cwd_dir = tmp_path / "cwd"
    cwd_dir.mkdir()
    config_path = cwd_dir / "providers.yaml"
    config_path.write_text(_minimal_config_yaml(), encoding="utf-8")
    monkeypatch.chdir(cwd_dir)
    monkeypatch.setenv("CODEROUTER_CONFIG", str(config_path))

    with caplog.at_level("WARNING", logger="coderouter.config.loader"):
        cfg = load_config(None)

    assert isinstance(cfg, CodeRouterConfig)
    warnings = [r for r in caplog.records if r.message == "cwd-config-loaded"]
    assert warnings == []


def test_home_config_does_not_warn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    monkeypatch.setenv("HOME", str(home_dir))
    coderouter_dir = home_dir / ".coderouter-t"
    coderouter_dir.mkdir()
    (coderouter_dir / "providers.yaml").write_text(_minimal_config_yaml(), encoding="utf-8")

    # CWD has no providers.yaml of its own, so the search must fall through
    # to ~/.coderouter-t/providers.yaml.
    cwd_dir = tmp_path / "cwd_without_config"
    cwd_dir.mkdir()
    monkeypatch.chdir(cwd_dir)

    with caplog.at_level("WARNING", logger="coderouter.config.loader"):
        cfg = load_config(None)

    assert isinstance(cfg, CodeRouterConfig)
    warnings = [r for r in caplog.records if r.message == "cwd-config-loaded"]
    assert warnings == []


# ---------------------------------------------------------------------------
# loader.py: v2.13.0 CWD-config opt-in (CODEROUTER_ALLOW_CWD_CONFIG)
# ---------------------------------------------------------------------------


def _write_home_and_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    home_model: str | None,
    cwd_model: str | None,
) -> tuple[Path, Path]:
    """Set up an isolated HOME + CWD, optionally seeding each providers.yaml.

    Returns ``(cwd_dir, home_dir)``. ``*_model`` = None means "no file".
    Distinct ``model`` values let a test assert which file actually won.
    """
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    monkeypatch.setenv("HOME", str(home_dir))
    if home_model is not None:
        cr_dir = home_dir / ".coderouter-t"
        cr_dir.mkdir()
        (cr_dir / "providers.yaml").write_text(
            _minimal_config_yaml(model=home_model), encoding="utf-8"
        )

    cwd_dir = tmp_path / "cwd"
    cwd_dir.mkdir()
    if cwd_model is not None:
        (cwd_dir / "providers.yaml").write_text(
            _minimal_config_yaml(model=cwd_model), encoding="utf-8"
        )
    monkeypatch.chdir(cwd_dir)
    return cwd_dir, home_dir


def test_cwd_config_not_loaded_without_optin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_home_and_cwd(
        tmp_path, monkeypatch, home_model="home-model", cwd_model="cwd-model"
    )
    cfg = load_config(None)
    # Opt-in off → the CWD file is ignored and home wins.
    assert cfg.providers[0].model == "home-model"


def test_cwd_config_skipped_warns_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _write_home_and_cwd(
        tmp_path, monkeypatch, home_model="home-model", cwd_model="cwd-model"
    )
    with caplog.at_level("WARNING", logger="coderouter.config.loader"):
        load_config(None)
        load_config(None)
    skipped = [r for r in caplog.records if r.message == "cwd-config-skipped"]
    assert len(skipped) == 1
    assert "CODEROUTER_ALLOW_CWD_CONFIG" in skipped[0].hint  # type: ignore[attr-defined]


def test_cwd_config_skipped_error_names_the_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    cwd_dir, _ = _write_home_and_cwd(
        tmp_path, monkeypatch, home_model=None, cwd_model="cwd-model"
    )
    with caplog.at_level(
        "WARNING", logger="coderouter.config.loader"
    ), pytest.raises(FileNotFoundError) as excinfo:
        load_config(None)
    message = str(excinfo.value)
    assert str(cwd_dir / "providers.yaml") in message
    assert "CODEROUTER_ALLOW_CWD_CONFIG" in message
    # The skip warning still fires even on the not-found path.
    skipped = [r for r in caplog.records if r.message == "cwd-config-skipped"]
    assert len(skipped) == 1


def test_cwd_config_loaded_with_optin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_home_and_cwd(
        tmp_path, monkeypatch, home_model="home-model", cwd_model="cwd-model"
    )
    monkeypatch.setenv("CODEROUTER_ALLOW_CWD_CONFIG", "1")
    cfg = load_config(None)
    assert cfg.providers[0].model == "cwd-model"


@pytest.mark.parametrize(
    ("value", "loads_cwd"),
    [
        ("1", True),
        ("true", True),
        ("yes", True),
        ("on", True),
        ("TRUE", True),
        ("  on  ", True),
        ("0", False),
        ("false", False),
        ("no", False),
        ("off", False),
        ("", False),
        ("maybe", False),
    ],
)
def test_cwd_optin_accepts_documented_truthy_values(
    value: str,
    loads_cwd: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_home_and_cwd(
        tmp_path, monkeypatch, home_model="home-model", cwd_model="cwd-model"
    )
    monkeypatch.setenv("CODEROUTER_ALLOW_CWD_CONFIG", value)
    cfg = load_config(None)
    expected = "cwd-model" if loads_cwd else "home-model"
    assert cfg.providers[0].model == expected


def test_explicit_config_path_bypasses_optin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    cwd_dir, _ = _write_home_and_cwd(
        tmp_path, monkeypatch, home_model="home-model", cwd_model="cwd-model"
    )
    explicit = cwd_dir / "providers.yaml"
    with caplog.at_level("WARNING", logger="coderouter.config.loader"):
        cfg = load_config(explicit)
    # Explicit naming wins regardless of the opt-in, and stays quiet.
    assert cfg.providers[0].model == "cwd-model"
    assert [r for r in caplog.records if r.message == "cwd-config-skipped"] == []
    assert [r for r in caplog.records if r.message == "cwd-config-loaded"] == []


def test_env_config_path_bypasses_optin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    cwd_dir, _ = _write_home_and_cwd(
        tmp_path, monkeypatch, home_model="home-model", cwd_model="cwd-model"
    )
    monkeypatch.setenv("CODEROUTER_CONFIG", str(cwd_dir / "providers.yaml"))
    with caplog.at_level("WARNING", logger="coderouter.config.loader"):
        cfg = load_config(None)
    assert cfg.providers[0].model == "cwd-model"
    assert [r for r in caplog.records if r.message == "cwd-config-skipped"] == []
    assert [r for r in caplog.records if r.message == "cwd-config-loaded"] == []


def test_stale_explicit_path_falls_through_and_skips_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    cwd_dir, _ = _write_home_and_cwd(
        tmp_path, monkeypatch, home_model="home-model", cwd_model="cwd-model"
    )
    # A --config pointing at a file that no longer exists must NOT silently
    # promote the CWD file — it falls through to home and warns about the skip.
    stale = tmp_path / "gone" / "providers.yaml"
    with caplog.at_level("WARNING", logger="coderouter.config.loader"):
        cfg = load_config(stale)
    assert cfg.providers[0].model == "home-model"
    skipped = [r for r in caplog.records if r.message == "cwd-config-skipped"]
    assert len(skipped) == 1
    assert str(cwd_dir / "providers.yaml") in skipped[0].path  # type: ignore[attr-defined]


def test_resolve_config_path_matches_load_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cwd_dir, home_dir = _write_home_and_cwd(
        tmp_path, monkeypatch, home_model="home-model", cwd_model="cwd-model"
    )
    home_path = home_dir / ".coderouter-t" / "providers.yaml"
    cwd_path = cwd_dir / "providers.yaml"

    # Opt-in off → resolver points at home (the file load_config reads).
    assert resolve_config_path(None) == home_path
    assert load_config(None).providers[0].model == "home-model"

    # Opt-in on → resolver points at CWD, matching load_config.
    monkeypatch.setenv("CODEROUTER_ALLOW_CWD_CONFIG", "1")
    assert resolve_config_path(None) == cwd_path
    assert load_config(None).providers[0].model == "cwd-model"


# ---------------------------------------------------------------------------
# schemas.py: ProviderConfig.restart_command shell-syntax warning
# ---------------------------------------------------------------------------


def test_restart_command_with_shell_metacharacters_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING", logger="coderouter.config.schemas"):
        ProviderConfig(
            name="local",
            base_url="http://localhost:8080/v1",
            model="qwen-coder",
            restart_command="pkill ollama && ollama serve",
        )

    warnings = [r for r in caplog.records if r.message == "restart-command-shell-syntax"]
    assert len(warnings) == 1
    assert warnings[0].provider == "local"  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "restart_command",
    [
        "~/bin/restart.sh",
        "OLLAMA_HOST=0.0.0.0 ollama serve",
        "pkill x && x",
        "a | b",
        "a; b",
        "a > out.log",
    ],
)
def test_restart_command_problematic_forms_warn(
    restart_command: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("WARNING", logger="coderouter.config.schemas"):
        ProviderConfig(
            name="local",
            base_url="http://localhost:8080/v1",
            model="qwen-coder",
            restart_command=restart_command,
        )
    warnings = [r for r in caplog.records if r.message == "restart-command-shell-syntax"]
    assert len(warnings) == 1


def test_restart_command_plain_does_not_warn(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING", logger="coderouter.config.schemas"):
        ProviderConfig(
            name="local",
            base_url="http://localhost:8080/v1",
            model="qwen-coder",
            restart_command="ollama serve",
        )
    warnings = [r for r in caplog.records if r.message == "restart-command-shell-syntax"]
    assert warnings == []


def test_restart_command_unset_does_not_warn(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING", logger="coderouter.config.schemas"):
        ProviderConfig(
            name="local",
            base_url="http://localhost:8080/v1",
            model="qwen-coder",
        )
    warnings = [r for r in caplog.records if r.message == "restart-command-shell-syntax"]
    assert warnings == []


def test_restart_command_warning_does_not_raise() -> None:
    # Must not raise even though the value would be refused under the
    # v2.13.0 shell=False dispatch — this validator only warns, so an
    # existing providers.yaml stays loadable.
    cfg = ProviderConfig(
        name="local",
        base_url="http://localhost:8080/v1",
        model="qwen-coder",
        restart_command="pkill ollama && ollama serve",
    )
    assert cfg.restart_command == "pkill ollama && ollama serve"
