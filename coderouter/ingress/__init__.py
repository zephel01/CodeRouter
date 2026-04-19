"""HTTP ingress (OpenAI-compatible in v0.1; Anthropic-compatible coming v0.2)."""

from coderouter.ingress.app import create_app

__all__ = ["create_app"]
