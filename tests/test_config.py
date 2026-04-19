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
