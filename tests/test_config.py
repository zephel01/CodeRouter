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


def test_env_overrides_allow_paid(yaml_config_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_mode_aliases_resolve_returns_target_profile(tmp_path: Path) -> None:
    """v0.6-D: ``resolve_mode`` maps a declared mode alias to its profile.

    Parallels ``profile_by_name`` — a lookup on a string key that either
    returns the target or raises ``KeyError``. The ingress layer relies
    on this contract to distinguish "caller sent a mode we don't know"
    (400) from "caller sent a profile we don't know" (also 400 but a
    different code path).
    """
    import yaml as _yaml

    (tmp_path / "providers.yaml").write_text(
        _yaml.safe_dump(
            {
                "allow_paid": False,
                "default_profile": "default",
                "providers": [
                    {
                        "name": "local",
                        "base_url": "http://localhost:8080/v1",
                        "model": "m",
                    },
                ],
                "profiles": [
                    {"name": "default", "providers": ["local"]},
                    {"name": "fast", "providers": ["local"]},
                ],
                "mode_aliases": {"coding": "default", "quick": "fast"},
            }
        ),
        encoding="utf-8",
    )
    cfg = load_config(tmp_path / "providers.yaml")
    assert cfg.resolve_mode("coding") == "default"
    assert cfg.resolve_mode("quick") == "fast"

    with pytest.raises(KeyError, match="no-such-mode"):
        cfg.resolve_mode("no-such-mode")


def test_mode_aliases_unknown_target_raises_at_load(tmp_path: Path) -> None:
    """v0.6-D: ``mode_aliases`` pointing to a missing profile must fast-fail.

    Same philosophy as ``_check_default_profile_exists`` — a typo in the
    YAML surfaces at load time, not on the first request that happens to
    hit the broken alias. The error message should list the known
    profile names to help the operator spot the typo.
    """
    import yaml as _yaml

    (tmp_path / "providers.yaml").write_text(
        _yaml.safe_dump(
            {
                "allow_paid": False,
                "default_profile": "default",
                "providers": [
                    {
                        "name": "local",
                        "base_url": "http://localhost:8080/v1",
                        "model": "m",
                    }
                ],
                "profiles": [{"name": "default", "providers": ["local"]}],
                "mode_aliases": {"coding": "typo-profile"},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="typo-profile"):
        load_config(tmp_path / "providers.yaml")


def test_mode_aliases_empty_by_default(yaml_config_path: Path) -> None:
    """A config without a ``mode_aliases:`` block parses with an empty dict.

    Guards the "feature off by default" contract — existing configs that
    predate v0.6-D must continue to load unchanged, and the ingress
    layer must see an empty dict (not None) so it can uniformly render
    ``available modes: []`` in error messages without a null check.
    """
    cfg = load_config(yaml_config_path)
    assert cfg.mode_aliases == {}


def test_missing_config_path_is_helpful(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


# ======================================================================
# v1.0-A: output_filters validation at config-load time
# ======================================================================


def test_output_filters_empty_by_default() -> None:
    """Providers without an ``output_filters:`` block default to an empty list.

    Guards backward compatibility with v0.7.x configs — existing files must
    continue to load unchanged.
    """
    from coderouter.config.schemas import ProviderConfig

    p = ProviderConfig(
        name="local",
        base_url="http://localhost:11434/v1",
        model="qwen2.5-coder:14b",
    )
    assert p.output_filters == []


def test_output_filters_accepts_known_names() -> None:
    from coderouter.config.schemas import ProviderConfig

    p = ProviderConfig(
        name="local",
        base_url="http://localhost:11434/v1",
        model="qwen2.5-coder:14b",
        output_filters=["strip_thinking", "strip_stop_markers"],
    )
    assert p.output_filters == ["strip_thinking", "strip_stop_markers"]


def test_output_filters_unknown_name_fails_at_load() -> None:
    """Fast-fail: typo'd ``strp_thinking`` must raise at construction time.

    Same shape as ``_check_default_profile_exists`` / ``_check_mode_alias_
    targets_exist`` — bad declarations surface at startup, not at first
    request.
    """
    from pydantic import ValidationError

    from coderouter.config.schemas import ProviderConfig

    with pytest.raises(ValidationError) as info:
        ProviderConfig(
            name="local",
            base_url="http://localhost:11434/v1",
            model="qwen2.5-coder:14b",
            output_filters=["strp_thinking"],
        )
    msg = str(info.value)
    assert "strp_thinking" in msg
    # Error lists known filters so the fix is a copy-paste.
    assert "strip_thinking" in msg
