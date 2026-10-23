"""v2.18.0: auto_router matchers on Claude Code gateway hint headers."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from coderouter.config.schemas import (
    AutoRouterConfig,
    AutoRouteRule,
    CodeRouterConfig,
    FallbackChain,
    ProviderConfig,
    RuleMatcher,
)
from coderouter.routing.auto_router import classify, extract_gateway_hints


def _config() -> CodeRouterConfig:
    return CodeRouterConfig(
        allow_paid=False,
        default_profile="auto",
        providers=[ProviderConfig(name="p", base_url="http://127.0.0.1:9931/v1", model="m")],
        profiles=[
            FallbackChain(name=name, providers=["p"])
            for name in ("main", "tiny", "explore", "longctx")
        ],
        auto_router=AutoRouterConfig(
            default_rule_profile="main",
            rules=[
                AutoRouteRule(id="aux", match=RuleMatcher(request_class="auxiliary"), profile="tiny"),
                AutoRouteRule(id="compact", match=RuleMatcher(compaction=True), profile="longctx"),
                AutoRouteRule(id="explore", match=RuleMatcher(agent_type="Explore"), profile="explore"),
            ],
        ),
    )


_BODY = {"model": "claude-sonnet-5", "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]}


def test_extract_hints_is_case_insensitive_with_starlette_headers() -> None:
    from starlette.datastructures import Headers

    h = Headers({"X-Claude-Code-Request-Class": "auxiliary", "x-claude-code-agent-type": "Explore"})
    assert extract_gateway_hints(h) == {"request_class": "auxiliary", "agent_type": "Explore"}


@pytest.mark.parametrize(
    ("hints", "expected"),
    [
        ({}, "main"),
        ({"request_class": "main"}, "main"),
        ({"request_class": "AUXILIARY"}, "tiny"),
        ({"compaction": "true"}, "longctx"),
        ({"compaction": "false"}, "main"),
        ({"request_class": "subagent", "agent_type": "explore"}, "explore"),
    ],
)
def test_classify_with_hints(hints: dict[str, str], expected: str) -> None:
    assert classify(_BODY, _config(), hints) == expected


def test_classify_without_hints_argument_is_backward_compatible() -> None:
    assert classify(_BODY, _config()) == "main"


def test_false_compaction_matcher_is_rejected() -> None:
    with pytest.raises(ValueError):
        RuleMatcher(compaction=False)


def test_two_matchers_rejected() -> None:
    with pytest.raises(ValueError):
        RuleMatcher(request_class="main", agent_type="Explore")


def test_route_passes_headers_to_classifier(monkeypatch: pytest.MonkeyPatch) -> None:
    from coderouter.ingress.app import create_app

    config = _config()
    seen: list[str | None] = []

    class _Engine:
        last_drift_severity = None

        def apply_context_budget(self, request):  # type: ignore[no-untyped-def]
            seen.append(request.profile)
            from coderouter.routing import NoProvidersAvailableError

            raise NoProvidersAvailableError(request.profile or "", [])

    monkeypatch.setattr("coderouter.ingress.app.load_config", lambda path=None: config)
    app = create_app()
    app.state.engine = _Engine()
    app.state.config = config
    client = TestClient(app, raise_server_exceptions=False)
    client.post("/v1/messages", json=_BODY, headers={"x-claude-code-request-class": "auxiliary"})
    assert seen == ["tiny"]
