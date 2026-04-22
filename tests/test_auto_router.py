"""Test matrix for v1.6 ``auto_router`` (task-aware routing).

Tests are written **before** implementation — the whole module is gated
behind ``pytest.importorskip("coderouter.routing.auto_router")`` so CI
stays green until v1.6-A lands. When the implementation goes in, the
skip lifts and each test either passes or flags a missing behavior.

Design reference: ``docs/designs/v1.6-auto-router.md`` §4 schema / §5
bundled rules / §6 matcher semantics / §7 override semantics / §8
failure modes / §9 log events.

Grouping mirrors the spec:

- **Classifier (pure)** — ``classify()`` behavior without HTTP.
- **Bundled ruleset** — zero-config defaults.
- **Schema validators** — startup fast-fail paths.
- **Ingress precedence** — how auto slots into the existing 4-tier
  precedence (body > profile header > mode header > auto > default).
- **Log events** — ``auto-router-resolved`` / ``auto-router-fallthrough``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi.testclient import TestClient

# Skip the whole file until v1.6-A ships. Individual sub-modules may
# land at different sub-releases, so we guard on the main entry point.
pytest.importorskip(
    "coderouter.routing.auto_router",
    reason="v1.6-A: auto_router not yet implemented",
)

from coderouter.adapters.base import ChatRequest, ChatResponse, StreamChunk  # noqa: E402
from coderouter.config.schemas import (  # noqa: E402
    CodeRouterConfig,
    FallbackChain,
    ProviderConfig,
)
from coderouter.ingress.app import create_app  # noqa: E402
from coderouter.metrics import uninstall_collector  # noqa: E402
from coderouter.routing.auto_router import (  # noqa: E402
    BUNDLED_RULES,
    AutoRouteRule,
    AutoRouterConfig,
    RuleMatcher,
    classify,
)


# v1.6-B: the Prometheus counter test asserts an exact count (``... 2``),
# so we need a clean collector for every test in this file. Module-scope
# autouse fixture — same isolation pattern as tests/test_metrics_endpoint.py.
@pytest.fixture(autouse=True)
def _clean_metrics_collector() -> Iterator[None]:
    uninstall_collector()
    yield
    uninstall_collector()

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _provider(name: str, model: str = "stub") -> ProviderConfig:
    return ProviderConfig(
        name=name,
        base_url="http://localhost:8080/v1",
        model=model,
    )


@pytest.fixture
def three_profile_config() -> CodeRouterConfig:
    """Config with the three bundled-rule profiles (multi/coding/writing).

    ``default_profile: auto`` triggers the classifier on every request.
    ``auto_router`` is omitted so bundled rules apply.
    """
    return CodeRouterConfig(
        allow_paid=False,
        default_profile="auto",
        providers=[
            _provider("ollama-coder", "qwen2.5-coder:7b"),
            _provider("ollama-general", "qwen2.5:7b"),
            _provider("ollama-vl", "qwen2-vl:7b"),
        ],
        profiles=[
            FallbackChain(name="coding", providers=["ollama-coder"]),
            FallbackChain(name="writing", providers=["ollama-general"]),
            FallbackChain(name="multi", providers=["ollama-vl"]),
        ],
    )


class _RecordingEngine:
    """Drop-in FallbackEngine that records the profile actually used."""

    def __init__(self) -> None:
        self.seen_profiles: list[str | None] = []

    async def generate(self, request: ChatRequest) -> ChatResponse:
        self.seen_profiles.append(request.profile)
        return ChatResponse(
            id="chatcmpl-test",
            object="chat.completion",
            created=0,
            model="unused-upstream-model",
            choices=[
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            coderouter_provider="ollama-coder",
        )

    async def stream(
        self, request: ChatRequest
    ) -> AsyncIterator[StreamChunk]:  # pragma: no cover
        self.seen_profiles.append(request.profile)
        yield StreamChunk(
            id="x",
            object="chat.completion.chunk",
            created=0,
            model="unused",
            choices=[
                {
                    "index": 0,
                    "delta": {"content": "ok"},
                    "finish_reason": "stop",
                }
            ],
        )


@pytest.fixture
def client_and_engine(
    three_profile_config: CodeRouterConfig, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, _RecordingEngine]:
    monkeypatch.setattr(
        "coderouter.ingress.app.load_config",
        lambda path=None: three_profile_config,
    )
    app = create_app()
    engine = _RecordingEngine()
    app.state.engine = engine
    app.state.config = three_profile_config
    return TestClient(app), engine


_TEXT_BODY = {"messages": [{"role": "user", "content": "hello"}]}

_CODE_HEAVY_BODY = {
    "messages": [
        {
            "role": "user",
            "content": (
                "fix this bug:\n\n"
                "```python\n"
                + "\n".join(f"def f_{i}(): return {i}" for i in range(30))
                + "\n```\n\n"
                "I'm stuck."
            ),
        }
    ]
}

_IMAGE_OPENAI_BODY = {
    "messages": [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "what's in this?"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,AAAA"},
                },
            ],
        }
    ]
}

_IMAGE_ANTHROPIC_BODY = {
    "messages": [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "describe"},
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": "AAAA",
                    },
                },
            ],
        }
    ]
}


# ---------------------------------------------------------------------------
# Group 1: Classifier (pure, no HTTP)
# ---------------------------------------------------------------------------


def test_classify_image_attachment_openai_format_selects_multi(
    three_profile_config: CodeRouterConfig,
) -> None:
    """OpenAI-format ``image_url`` in message content → ``multi`` profile."""
    assert classify(_IMAGE_OPENAI_BODY, three_profile_config) == "multi"


def test_classify_image_attachment_anthropic_format_selects_multi(
    three_profile_config: CodeRouterConfig,
) -> None:
    """Anthropic-format ``type: image`` content block → ``multi`` profile."""
    assert classify(_IMAGE_ANTHROPIC_BODY, three_profile_config) == "multi"


def test_classify_code_fence_at_boundary_below_threshold(
    three_profile_config: CodeRouterConfig,
) -> None:
    """code_fence_ratio == 0.29 (just below 0.3 threshold) → ``writing``.

    Boundary test — documents that the bundled threshold is strict
    ``>=`` so callers designing alternate rules know the semantics.
    """
    # Prose padding so that the 3 lines of code sit just below 30%.
    body = {
        "messages": [
            {
                "role": "user",
                "content": (
                    "I need some help understanding this example. "
                    "Could you explain what the tradeoffs are here?"
                    "```\nprint(1)\nprint(2)\nprint(3)\n```"
                ),
            }
        ]
    }
    # Actual ratio depends on implementation (fence delimiters counted
    # or not); test calibrates once impl lands.
    assert classify(body, three_profile_config) == "writing"


def test_classify_code_fence_at_boundary_above_threshold(
    three_profile_config: CodeRouterConfig,
) -> None:
    """code_fence_ratio >= 0.3 → ``coding``."""
    assert classify(_CODE_HEAVY_BODY, three_profile_config) == "coding"


def test_classify_no_fences_falls_through_to_writing(
    three_profile_config: CodeRouterConfig,
) -> None:
    """No image, no fences → ``writing`` (bundled default)."""
    assert classify(_TEXT_BODY, three_profile_config) == "writing"


def test_classify_uses_only_latest_user_message(
    three_profile_config: CodeRouterConfig,
) -> None:
    """Older turns with images must not influence current classification.

    Spec §11 open question #2: latest user message only. This test
    pins that decision.
    """
    body = {
        "messages": [
            # Prior turn had an image.
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is this?"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,AAAA"},
                    },
                ],
            },
            {"role": "assistant", "content": "A cat."},
            # Latest user message is plain prose.
            {"role": "user", "content": "tell me a story instead."},
        ]
    }
    assert classify(body, three_profile_config) == "writing"


def test_classify_first_match_wins_image_over_code(
    three_profile_config: CodeRouterConfig,
) -> None:
    """Bundled order: image rule runs before code-fence rule.

    When a message has both an image *and* dense code, image wins
    because the rule order in ``BUNDLED_RULES`` is image-first.
    """
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,AAAA"},
                    },
                    {
                        "type": "text",
                        "text": "```\n" + "x = 1\n" * 50 + "```",
                    },
                ],
            }
        ]
    }
    assert classify(body, three_profile_config) == "multi"


def test_classify_content_contains_matcher(
    three_profile_config: CodeRouterConfig,
) -> None:
    """User-defined ``content_contains`` matches substring in any message.

    Used by intermediate users to add custom routing (e.g. Japanese
    keyword → writing profile).
    """
    cfg = three_profile_config.model_copy(
        update={
            "auto_router": AutoRouterConfig(
                rules=[
                    AutoRouteRule(
                        id="test:jp-writing",
                        profile="writing",
                        match=RuleMatcher(content_contains="日本語で"),
                    ),
                ],
                default_rule_profile="coding",
            ),
        }
    )
    body = {
        "messages": [
            {"role": "user", "content": "日本語で返事してください。```x=1```"}
        ]
    }
    assert classify(body, cfg) == "writing"


def test_classify_content_regex_matcher(
    three_profile_config: CodeRouterConfig,
) -> None:
    """``content_regex`` provides full pattern matching for power users."""
    cfg = three_profile_config.model_copy(
        update={
            "auto_router": AutoRouterConfig(
                rules=[
                    AutoRouteRule(
                        id="test:pr-review",
                        profile="writing",
                        match=RuleMatcher(content_regex=r"(?i)\bPR\s*review\b"),
                    ),
                ],
                default_rule_profile="coding",
            ),
        }
    )
    body = {"messages": [{"role": "user", "content": "can you do a PR review?"}]}
    assert classify(body, cfg) == "writing"


# ---------------------------------------------------------------------------
# Group 2: Bundled ruleset
# ---------------------------------------------------------------------------


def test_bundled_ruleset_has_three_rules() -> None:
    """Spec §5: exactly 3 bundled rules (image / code-fence / fallthrough).

    The fallthrough is encoded as ``default_rule_profile``, not as an
    explicit rule, so ``BUNDLED_RULES`` is length 2.
    """
    assert len(BUNDLED_RULES) == 2
    ids = [r.id for r in BUNDLED_RULES]
    assert ids == ["builtin:image-attachment", "builtin:code-fence-dense"]


def test_bundled_ruleset_used_when_auto_router_absent(
    three_profile_config: CodeRouterConfig,
) -> None:
    """No ``auto_router`` in yaml → bundled rules apply."""
    assert three_profile_config.auto_router is None
    assert classify(_IMAGE_OPENAI_BODY, three_profile_config) == "multi"
    assert classify(_CODE_HEAVY_BODY, three_profile_config) == "coding"
    assert classify(_TEXT_BODY, three_profile_config) == "writing"


def test_user_rules_replace_bundled_entirely(
    three_profile_config: CodeRouterConfig,
) -> None:
    """Spec §7: user ``rules`` are a full replacement, not a merge.

    If the user provides only 1 rule and it doesn't match, the default
    rule profile is used — the bundled image/code rules are **not**
    evaluated.
    """
    cfg = three_profile_config.model_copy(
        update={
            "auto_router": AutoRouterConfig(
                rules=[
                    AutoRouteRule(
                        id="user:only-this",
                        profile="writing",
                        match=RuleMatcher(content_contains="ZZZZZZ"),
                    ),
                ],
                default_rule_profile="coding",
            ),
        }
    )
    # Image body would hit bundled "multi" — but bundled is replaced,
    # so it falls through to ``default_rule_profile == "coding"``.
    assert classify(_IMAGE_OPENAI_BODY, cfg) == "coding"


# ---------------------------------------------------------------------------
# Group 3: Schema validators (startup fast-fail)
# ---------------------------------------------------------------------------


def test_startup_fails_when_bundled_needs_missing_profile() -> None:
    """Spec §5: bundled ruleset requires ``multi``/``coding``/``writing``.

    Missing any of the three at startup → fast-fail with a list of
    missing names. Same pattern as v0.6-A's ``default_profile`` and
    v0.6-D's ``mode_aliases`` validators.
    """
    with pytest.raises(ValueError, match="bundled auto_router.*missing.*multi"):
        CodeRouterConfig(
            default_profile="auto",
            providers=[_provider("p1")],
            profiles=[
                FallbackChain(name="coding", providers=["p1"]),
                FallbackChain(name="writing", providers=["p1"]),
                # "multi" missing on purpose.
            ],
        )


def test_startup_fails_when_user_rule_references_unknown_profile() -> None:
    """User-defined rule with ``profile`` not in ``profiles[]`` → fast-fail."""
    with pytest.raises(ValueError, match="unknown profile"):
        CodeRouterConfig(
            default_profile="auto",
            providers=[_provider("p1")],
            profiles=[
                FallbackChain(name="coding", providers=["p1"]),
                FallbackChain(name="writing", providers=["p1"]),
                FallbackChain(name="multi", providers=["p1"]),
            ],
            auto_router=AutoRouterConfig(
                rules=[
                    AutoRouteRule(
                        id="user:bad",
                        profile="nonexistent",
                        match=RuleMatcher(content_contains="x"),
                    ),
                ],
            ),
        )


def test_startup_fails_on_invalid_regex() -> None:
    """Spec §8: ``content_regex`` compilation happens at load, not request time."""
    with pytest.raises(ValueError, match="regex"):
        AutoRouteRule(
            id="user:bad-regex",
            profile="writing",
            match=RuleMatcher(content_regex=r"([unclosed"),
        )


def test_startup_fails_when_profile_named_auto() -> None:
    """Spec §11 open question #3: ``auto`` is reserved as a profile name."""
    with pytest.raises(ValueError, match="reserved"):
        CodeRouterConfig(
            default_profile="auto",
            providers=[_provider("p1")],
            profiles=[
                FallbackChain(name="auto", providers=["p1"]),  # reserved
                FallbackChain(name="coding", providers=["p1"]),
                FallbackChain(name="writing", providers=["p1"]),
                FallbackChain(name="multi", providers=["p1"]),
            ],
        )


def test_matcher_with_zero_fields_rejected() -> None:
    """``RuleMatcher`` requires exactly one matcher field set."""
    with pytest.raises(ValueError, match="exactly one"):
        RuleMatcher()


def test_matcher_with_multiple_fields_rejected() -> None:
    """``RuleMatcher`` rejects multi-field rules (use two rules instead)."""
    with pytest.raises(ValueError, match="exactly one"):
        RuleMatcher(has_image=True, content_contains="foo")


# ---------------------------------------------------------------------------
# Group 4: Ingress precedence
# ---------------------------------------------------------------------------


def test_body_profile_overrides_auto(
    client_and_engine: tuple[TestClient, _RecordingEngine],
) -> None:
    """Spec §3: body.profile wins over auto (highest precedence)."""
    client, engine = client_and_engine
    resp = client.post(
        "/v1/chat/completions",
        json={**_IMAGE_OPENAI_BODY, "profile": "writing"},
    )
    assert resp.status_code == 200
    # Image body would auto-resolve to ``multi``, but explicit
    # ``profile: writing`` takes precedence.
    assert engine.seen_profiles == ["writing"]


def test_profile_header_overrides_auto(
    client_and_engine: tuple[TestClient, _RecordingEngine],
) -> None:
    """``X-CodeRouter-Profile`` header wins over auto."""
    client, engine = client_and_engine
    resp = client.post(
        "/v1/chat/completions",
        json=_IMAGE_OPENAI_BODY,
        headers={"X-CodeRouter-Profile": "coding"},
    )
    assert resp.status_code == 200
    assert engine.seen_profiles == ["coding"]


def test_mode_header_overrides_auto(
    three_profile_config: CodeRouterConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``X-CodeRouter-Mode`` resolves via ``mode_aliases`` and beats auto."""
    cfg = three_profile_config.model_copy(
        update={"mode_aliases": {"jp": "writing"}}
    )
    monkeypatch.setattr(
        "coderouter.ingress.app.load_config", lambda path=None: cfg
    )
    app = create_app()
    engine = _RecordingEngine()
    app.state.engine = engine
    app.state.config = cfg
    with TestClient(app) as client:
        resp = client.post(
            "/v1/chat/completions",
            json=_IMAGE_OPENAI_BODY,
            headers={"X-CodeRouter-Mode": "jp"},
        )
        assert resp.status_code == 200
        # Image would → multi; mode header says → writing.
        assert engine.seen_profiles == ["writing"]


def test_default_profile_non_auto_never_invokes_classifier(
    three_profile_config: CodeRouterConfig,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When ``default_profile`` is a concrete profile, auto never fires.

    This is the back-compat guarantee: existing users on ``default_profile:
    coding`` see zero behavior change after upgrading to v1.6. The ingress
    leaves ``chat_req.profile=None`` (same as v0.6-D) and the engine
    resolves ``default_profile`` on its own — exactly what pre-v1.6 clients
    observe. The stronger assertion is the caplog check: no
    ``auto-router-*`` log event must fire.
    """
    cfg = three_profile_config.model_copy(update={"default_profile": "coding"})
    monkeypatch.setattr(
        "coderouter.ingress.app.load_config", lambda path=None: cfg
    )
    app = create_app()
    engine = _RecordingEngine()
    app.state.engine = engine
    app.state.config = cfg
    with TestClient(app) as client:
        with caplog.at_level("INFO", logger="coderouter.routing.auto_router"):
            # Image body would have triggered ``multi`` under auto, but
            # ``default_profile: coding`` means auto isn't consulted.
            client.post("/v1/chat/completions", json=_IMAGE_OPENAI_BODY)
    # Ingress behaves exactly like v0.6-D: profile stays None through
    # ingress, the engine picks ``default_profile`` later.
    assert engine.seen_profiles == [None]
    # Hard contract: classifier emits neither resolved nor fallthrough.
    auto_events = [
        r for r in caplog.records if r.message.startswith("auto-router-")
    ]
    assert auto_events == []


def test_auto_router_disabled_flag_skips_classification(
    three_profile_config: CodeRouterConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec §7: ``auto_router.disabled: true`` falls straight to default_rule_profile."""
    cfg = three_profile_config.model_copy(
        update={
            "auto_router": AutoRouterConfig(
                disabled=True,
                default_rule_profile="writing",
            ),
        }
    )
    monkeypatch.setattr(
        "coderouter.ingress.app.load_config", lambda path=None: cfg
    )
    app = create_app()
    engine = _RecordingEngine()
    app.state.engine = engine
    app.state.config = cfg
    with TestClient(app) as client:
        client.post("/v1/chat/completions", json=_IMAGE_OPENAI_BODY)
        # Image would have resolved to ``multi``, but disabled flag
        # bypasses classification.
        assert engine.seen_profiles == ["writing"]


# ---------------------------------------------------------------------------
# Group 5: Log events
# ---------------------------------------------------------------------------


def test_auto_router_resolved_event_carries_rule_id_and_profile(
    client_and_engine: tuple[TestClient, _RecordingEngine], caplog: pytest.LogCaptureFixture
) -> None:
    """Spec §9.1: ``auto-router-resolved`` fires with typed payload.

    Payload required keys: ``rule_id``, ``resolved_profile``. Optional
    ``signals`` dict for diagnostic extras (has_image, code_fence_ratio,
    content_len).
    """
    client, _ = client_and_engine
    with caplog.at_level("INFO", logger="coderouter.routing.auto_router"):
        client.post("/v1/chat/completions", json=_IMAGE_OPENAI_BODY)

    events = [r for r in caplog.records if r.message == "auto-router-resolved"]
    assert len(events) == 1
    assert events[0].rule_id == "builtin:image-attachment"  # type: ignore[attr-defined]
    assert events[0].resolved_profile == "multi"  # type: ignore[attr-defined]


def test_auto_router_fallthrough_event_fires_when_no_rule_matches(
    client_and_engine: tuple[TestClient, _RecordingEngine], caplog: pytest.LogCaptureFixture
) -> None:
    """Spec §9.2: ``auto-router-fallthrough`` is a distinct event.

    Separate event (not ``resolved`` with ``rule_id: fallthrough``) so
    Prometheus can expose ``coderouter_auto_router_fallthrough_total``
    as its own counter — operators want the percentage of requests that
    miss all rules as a stability signal.
    """
    client, _ = client_and_engine
    with caplog.at_level("INFO", logger="coderouter.routing.auto_router"):
        client.post("/v1/chat/completions", json=_TEXT_BODY)

    events = [r for r in caplog.records if r.message == "auto-router-fallthrough"]
    assert len(events) == 1
    assert events[0].resolved_profile == "writing"  # type: ignore[attr-defined]


def test_prometheus_fallthrough_counter_increments(
    client_and_engine: tuple[TestClient, _RecordingEngine],
) -> None:
    """Fallthrough events surface in ``/metrics`` as a counter."""
    client, _ = client_and_engine
    client.post("/v1/chat/completions", json=_TEXT_BODY)
    client.post("/v1/chat/completions", json=_TEXT_BODY)

    body = client.get("/metrics").text
    assert "coderouter_auto_router_fallthrough_total 2" in body
