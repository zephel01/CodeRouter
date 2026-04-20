"""Config loader / schema tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from coderouter.config.loader import load_config, resolve_api_key
from coderouter.config.schemas import CodeRouterConfig


def test_load_from_explicit_path(yaml_config_path: Path) -> None:
    cfg = load_config(yaml_config_path)
    assert isinstance(cfg, CodeRouterConfig)
    assert cfg.allow_paid is False
    assert {p.name for p in cfg.providers} == {"local", "free-cloud", "paid-cloud"}


def test_env_overrides_allow_paid(
    yaml_config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALLOW_PAID", "true")
    cfg = load_config(yaml_config_path)
    assert cfg.allow_paid is True

    monkeypatch.setenv("ALLOW_PAID", "off")
    cfg = load_config(yaml_config_path)
    assert cfg.allow_paid is False


def test_provider_lookup(yaml_config_path: Path) -> None:
    cfg = load_config(yaml_config_path)
    assert cfg.provider_by_name("local").base_url.host == "localhost"
    with pytest.raises(KeyError):
        cfg.provider_by_name("nope")


def test_profile_lookup(yaml_config_path: Path) -> None:
    cfg = load_config(yaml_config_path)
    assert cfg.profile_by_name("default").providers[0] == "local"
    with pytest.raises(KeyError):
        cfg.profile_by_name("missing")


def test_resolve_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    assert resolve_api_key(None) is None
    assert resolve_api_key("MISSING_VAR") is None
    monkeypatch.setenv("CR_TEST_KEY", "  sk-abc  ")
    assert resolve_api_key("CR_TEST_KEY") == "sk-abc"


def test_env_overrides_default_profile(
    yaml_config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v0.6-A: CODEROUTER_MODE overrides the YAML default_profile.

    Parallels ``test_env_overrides_allow_paid`` — the env var is the single
    override channel used by both the ``--mode`` CLI flag (which sets the
    env before handing off to uvicorn) and direct shell usage. Unknown
    profile names surface at load time, not at first request.
    """
    # Baseline: config file says "default", env unset → YAML wins.
    cfg = load_config(yaml_config_path)
    assert cfg.default_profile == "default"

    # Env var wins over YAML.
    monkeypatch.setenv("CODEROUTER_MODE", "free-only")
    cfg = load_config(yaml_config_path)
    assert cfg.default_profile == "free-only"

    # Unknown profile → fast-fail at load time.
    monkeypatch.setenv("CODEROUTER_MODE", "no-such-profile")
    with pytest.raises(ValueError, match="no-such-profile"):
        load_config(yaml_config_path)


def test_env_override_default_profile_ignores_empty_string(
    yaml_config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty ``CODEROUTER_MODE`` should not silently override the file.

    Distinguishes "user set it to ''" (= unset) from "user set it to
    something meaningful". Matches shell semantics where ``export FOO=``
    clears rather than configures.
    """
    monkeypatch.setenv("CODEROUTER_MODE", "   ")
    cfg = load_config(yaml_config_path)
    assert cfg.default_profile == "default"


def test_unknown_default_profile_in_yaml_raises(tmp_path: Path) -> None:
    """The YAML itself referring to a missing profile must fail fast.

    Previously this surfaced only at first request (KeyError from
    ``profile_by_name``). Post v0.6-A, the ``CodeRouterConfig``
    model-validator rejects the config at load / construct time so a
    mis-typed ``default_profile:`` in YAML is caught before serve starts.
    """
    import yaml as _yaml

    (tmp_path / "providers.yaml").write_text(
        _yaml.safe_dump(
            {
                "allow_paid": False,
                "default_profile": "typo-here",
                "providers": [
                    {
                        "name": "local",
                        "base_url": "http://localhost:8080/v1",
                        "model": "m",
                    }
                ],
                "profiles": [{"name": "default", "providers": ["local"]}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="typo-here"):
        load_config(tmp_path / "providers.yaml")


def test_missing_config_path_is_helpful(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Run from an empty dir so Path.cwd()/providers.yaml (one of the loader's
    # fallback candidates) does not accidentally resolve to the project's
    # real providers.yaml when tests are invoked from the repo root.
    monkeypatch.chdir(tmp_path)
    # Also force HOME so ~/.coderouter/providers.yaml cannot satisfy either.
    monkeypatch.setenv("HOME", str(tmp_path))

    with pytest.raises(FileNotFoundError) as info:
        load_config(tmp_path / "nope.yaml")
    msg = str(info.value)
    assert "providers.yaml" in msg
    assert "examples/providers.yaml" in msg
