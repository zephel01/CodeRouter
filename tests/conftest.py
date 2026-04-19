"""Shared pytest fixtures."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from coderouter.config.schemas import (
    Capabilities,
    CodeRouterConfig,
    FallbackChain,
    ProviderConfig,
)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wipe env vars that the loader picks up so tests are deterministic."""
    for var in ("ALLOW_PAID", "CODEROUTER_CONFIG", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def basic_config() -> CodeRouterConfig:
    return CodeRouterConfig(
        allow_paid=False,
        default_profile="default",
        providers=[
            ProviderConfig(
                name="local",
                base_url="http://localhost:8080/v1",
                model="qwen-coder",
                paid=False,
                capabilities=Capabilities(),
            ),
            ProviderConfig(
                name="free-cloud",
                base_url="https://openrouter.ai/api/v1",
                model="qwen/qwen-2.5-coder-32b-instruct:free",
                api_key_env="OPENROUTER_API_KEY",
                paid=False,
                capabilities=Capabilities(),
            ),
            ProviderConfig(
                name="paid-cloud",
                base_url="https://openrouter.ai/api/v1",
                model="anthropic/claude-sonnet-4",
                api_key_env="OPENROUTER_API_KEY",
                paid=True,
                capabilities=Capabilities(tools=True),
            ),
        ],
        profiles=[
            FallbackChain(
                name="default",
                providers=["local", "free-cloud", "paid-cloud"],
            ),
            FallbackChain(name="free-only", providers=["local", "free-cloud"]),
        ],
    )


@pytest.fixture
def yaml_config_path(tmp_path: Path, basic_config: CodeRouterConfig) -> Path:
    """Write basic_config out as YAML and return the path."""
    file = tmp_path / "providers.yaml"
    file.write_text(
        yaml.safe_dump(
            basic_config.model_dump(mode="json"),
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return file
