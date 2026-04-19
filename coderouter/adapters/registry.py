"""Adapter factory — maps `kind` strings to adapter classes."""

from __future__ import annotations

from coderouter.adapters.base import BaseAdapter
from coderouter.adapters.openai_compat import OpenAICompatAdapter
from coderouter.config.schemas import ProviderConfig


def build_adapter(provider: ProviderConfig) -> BaseAdapter:
    """Construct an adapter from a ProviderConfig."""
    if provider.kind == "openai_compat":
        return OpenAICompatAdapter(provider)
    # v0.2 will add: if provider.kind == "anthropic": return AnthropicAdapter(provider)
    raise ValueError(f"Unknown adapter kind: {provider.kind!r}")
