"""Root exception hierarchy.

All CodeRouter-raised exceptions inherit from :class:`CodeRouterError` so
callers (tests, embedders, downstream integrations) can catch "anything
CodeRouter produced" with a single ``except CodeRouterError`` clause
without having to import each leaf type individually.

The concrete subclasses still live next to the code that raises them
(:mod:`coderouter.adapters.base` for :class:`AdapterError`,
:mod:`coderouter.routing.fallback` for :class:`NoProvidersAvailableError`
and :class:`MidStreamError`) — this module only defines the root and
re-exports the leaves for discoverability. Existing import paths are
preserved; nothing has to change at call sites.
"""

from __future__ import annotations


class CodeRouterError(Exception):
    """Base class for every exception CodeRouter raises internally.

    Exists so external code can write ``except CodeRouterError`` to catch
    any failure the router itself produces, without having to enumerate
    the leaves (which are free to grow over time). Does not add any
    behavior beyond :class:`Exception`.

    v2.15.0: optional ``message_id`` / ``hint`` carry the bilingual
    catalog id so callers can render ``str(exc)`` in the user's
    language without re-translating at the catch site.
    """

    def __init__(
        self,
        message: str = "",
        *,
        message_id: str | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message_id = message_id
        self.hint = hint


class ConfigNotFoundError(CodeRouterError, FileNotFoundError):
    """Config file not found — carries bilingual message_id for rendering."""


class ConfigValidationError(CodeRouterError, ValueError):
    """Config validation failed — carries bilingual message_id for rendering."""


__all__ = ["CodeRouterError", "ConfigNotFoundError", "ConfigValidationError"]
