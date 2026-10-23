"""Claude Code tool search on non-Anthropic upstreams (v2.16.0).

Claude Code 2.1.304 turned tool search on by default behind a custom
``ANTHROPIC_BASE_URL``. A tool-search request carries:

- a server-side search tool, ``{"type": "tool_search_tool_regex_...",
  "name": "tool_search_tool_regex"}`` (or the ``bm25`` variant), which only
  api.anthropic.com executes, and
- ordinary tools marked ``"defer_loading": true`` that the API keeps out
  of the model's context until a search surfaces them.

A local model behind CodeRouter cannot run the search: before v2.16.0 the
search tool reached it as an ordinary function it could "call", and every
deferred tool was loaded anyway. Claude Code 2.1.305 retries a request
without tool search when the gateway answers 400 / 422, so rejecting is the
cheapest correct answer. Which action applies is the profile's
``deferred_tools_action`` (see ``FallbackChain``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlparse

if TYPE_CHECKING:
    from coderouter.config.schemas import CodeRouterConfig
    from coderouter.translation import AnthropicRequest

TOOL_SEARCH_TYPE_PREFIX = "tool_search_tool"

#: Message for the 400. Mentions both trigger words so a client (or a human
#: reading the log) can tell why tool search was refused.
REJECT_MESSAGE = (
    "tool search is not supported by this gateway's upstreams: remove the "
    "tool_search_tool and defer_loading (CodeRouter deferred_tools_action)"
)

Action = Literal["reject", "expand", "passthrough"]


def _tool_dict(tool: Any) -> dict[str, Any]:
    if hasattr(tool, "model_dump"):
        dumped: dict[str, Any] = tool.model_dump()
        return dumped
    return dict(tool)


def _is_search_tool(t: dict[str, Any]) -> bool:
    kind = t.get("type")
    return isinstance(kind, str) and kind.startswith(TOOL_SEARCH_TYPE_PREFIX)


def uses_tool_search(req: AnthropicRequest) -> bool:
    """True when the request carries a search tool or a deferred tool."""
    for tool in req.tools or []:
        t = _tool_dict(tool)
        if _is_search_tool(t) or t.get("defer_loading") is True:
            return True
    return False


def _is_anthropic_api(provider: Any) -> bool:
    if getattr(provider, "kind", None) != "anthropic":
        return False
    host = urlparse(str(getattr(provider, "base_url", ""))).hostname or ""
    return host == "anthropic.com" or host.endswith(".anthropic.com")


def resolve_action(config: CodeRouterConfig, profile_name: str | None) -> Action:
    """Resolve the profile's ``deferred_tools_action`` (``auto`` → concrete)."""
    try:
        profile = config.profile_by_name(profile_name or config.default_profile)
    except (KeyError, ValueError):
        return "reject"
    action = getattr(profile, "deferred_tools_action", "auto")
    if action == "reject":
        return "reject"
    if action == "expand":
        return "expand"
    if action == "passthrough":
        return "passthrough"
    providers = {p.name: p for p in config.providers}
    chain = [providers.get(name) for name in profile.providers]
    if chain and all(p is not None and _is_anthropic_api(p) for p in chain):
        return "passthrough"
    return "reject"


def expand_tool_search(req: AnthropicRequest) -> AnthropicRequest:
    """Drop the search tool(s) and every ``defer_loading`` flag."""
    from coderouter.translation.anthropic import AnthropicTool

    kept: list[AnthropicTool] = []
    for tool in req.tools or []:
        t = _tool_dict(tool)
        if _is_search_tool(t):
            continue
        t.pop("defer_loading", None)
        kept.append(AnthropicTool.model_validate(t))
    return req.model_copy(update={"tools": kept or None})
