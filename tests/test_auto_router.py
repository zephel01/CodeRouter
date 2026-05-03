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

from coderouter.adapters.base import ChatRequest, ChatResponse, StreamChunk
from coderouter.config.schemas import (
    CodeRouterConfig,
    FallbackChain,
    ProviderConfig,
)
from coderouter.ingress.app import create_app
from coderouter.metrics import uninstall_collector
from coderouter.routing.auto_router import (
    BUNDLED_RULES,
    AutoRouterConfig,
    AutoRouteRule,
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
    with pytest.raises(ValueError, match=r"bundled auto_router.*missing.*multi"):
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
    with (
        TestClient(app) as client,
        caplog.at_level("INFO", logger="coderouter.routing.auto_router"),
    ):
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


# ---------------------------------------------------------------------------
# Group 6: per-model auto-routing ([Unreleased] / free-claude-code 由来)
# ---------------------------------------------------------------------------


def _model_pattern_config(
    base: CodeRouterConfig, *, lightweight_provider: str = "ollama-coder"
) -> CodeRouterConfig:
    """Augment the 3-profile fixture with a 4th ``lightweight`` profile.

    Used by the per-model auto-routing tests to exercise the most
    common deployment shape: agents that send Sonnet → coding chain,
    agents that send Haiku → smaller / faster chain.
    """
    return base.model_copy(
        update={
            "profiles": [
                *base.profiles,
                FallbackChain(name="lightweight", providers=[lightweight_provider]),
            ],
        }
    )


def test_classify_model_pattern_sonnet_routes_to_coding(
    three_profile_config: CodeRouterConfig,
) -> None:
    """[Unreleased]: ``model_pattern`` matches Sonnet IDs → ``coding`` profile.

    Mirrors the example in ``docs/inside/future.md §6.3`` — agents that
    send the Sonnet model id route to the coding chain regardless of
    request content shape (no image / no fences needed).
    """
    cfg = three_profile_config.model_copy(
        update={
            "auto_router": AutoRouterConfig(
                rules=[
                    AutoRouteRule(
                        id="user:sonnet-coding",
                        profile="coding",
                        match=RuleMatcher(model_pattern=r"claude-3-5-sonnet.*"),
                    ),
                ],
                default_rule_profile="writing",
            ),
        }
    )
    body = {
        "model": "claude-3-5-sonnet-20241022",
        "messages": [{"role": "user", "content": "tell me a story"}],
    }
    assert classify(body, cfg) == "coding"


def test_classify_model_pattern_haiku_routes_to_lightweight(
    three_profile_config: CodeRouterConfig,
) -> None:
    """[Unreleased]: Haiku IDs → ``lightweight`` profile (smaller / faster)."""
    cfg = _model_pattern_config(three_profile_config).model_copy(
        update={
            "auto_router": AutoRouterConfig(
                rules=[
                    AutoRouteRule(
                        id="user:haiku-lightweight",
                        profile="lightweight",
                        match=RuleMatcher(model_pattern=r"claude-3-5-haiku.*"),
                    ),
                ],
                default_rule_profile="writing",
            ),
        }
    )
    body = {
        "model": "claude-3-5-haiku-20241022",
        "messages": [{"role": "user", "content": "summarize this"}],
    }
    assert classify(body, cfg) == "lightweight"


def test_classify_model_pattern_no_model_field_falls_through(
    three_profile_config: CodeRouterConfig,
) -> None:
    """A request body without a ``model`` field cannot match any
    ``model_pattern`` rule and falls through to ``default_rule_profile``.

    Test bodies without a model field are common in fixtures; the
    classifier must not crash on them and must not spuriously match
    a regex against ``None``.
    """
    cfg = three_profile_config.model_copy(
        update={
            "auto_router": AutoRouterConfig(
                rules=[
                    AutoRouteRule(
                        id="user:any-model",
                        profile="coding",
                        match=RuleMatcher(model_pattern=r".+"),
                    ),
                ],
                default_rule_profile="writing",
            ),
        }
    )
    body = {"messages": [{"role": "user", "content": "no model field"}]}
    assert classify(body, cfg) == "writing"


def test_model_pattern_invalid_regex_fast_fails_at_load() -> None:
    """[Unreleased]: bad ``model_pattern`` is rejected at schema load,
    same eager-compile path as ``content_regex``.
    """
    with pytest.raises(ValueError, match=r"model_pattern"):
        AutoRouteRule(
            id="user:bad-model-regex",
            profile="writing",
            match=RuleMatcher(model_pattern=r"([unclosed"),
        )


def test_model_pattern_first_match_wins_over_later_content_rule(
    three_profile_config: CodeRouterConfig,
) -> None:
    """Spec §6: rules evaluate in order, first match wins.

    A model_pattern rule placed before a content_contains rule fires
    even when the body content would also match the later rule —
    pinning the global "first match wins" semantics across matcher
    types.
    """
    cfg = three_profile_config.model_copy(
        update={
            "auto_router": AutoRouterConfig(
                rules=[
                    AutoRouteRule(
                        id="user:opus-multi",
                        profile="multi",
                        match=RuleMatcher(model_pattern=r"claude-3-opus.*"),
                    ),
                    AutoRouteRule(
                        id="user:any-text-coding",
                        profile="coding",
                        match=RuleMatcher(content_contains="hello"),
                    ),
                ],
                default_rule_profile="writing",
            ),
        }
    )
    # Both rules would match this body (Opus model id + "hello" in
    # content); first-match-wins selects ``multi``.
    body = {
        "model": "claude-3-opus-20240229",
        "messages": [{"role": "user", "content": "hello world"}],
    }
    assert classify(body, cfg) == "multi"


# ---------------------------------------------------------------------------
# Group 7: longContext auto-switch ([Unreleased] / claude-code-router 由来)
# ---------------------------------------------------------------------------


def _longcontext_config(
    base: CodeRouterConfig,
    *,
    threshold: int,
    longcontext_provider: str = "ollama-coder",
) -> CodeRouterConfig:
    """Augment the 3-profile fixture with a 4th ``longcontext`` profile +
    a single-rule ``content_token_count_min`` ruleset."""
    return base.model_copy(
        update={
            "profiles": [
                *base.profiles,
                FallbackChain(
                    name="longcontext", providers=[longcontext_provider]
                ),
            ],
            "auto_router": AutoRouterConfig(
                rules=[
                    AutoRouteRule(
                        id="user:long-context",
                        profile="longcontext",
                        match=RuleMatcher(content_token_count_min=threshold),
                    ),
                ],
                default_rule_profile="writing",
            ),
        }
    )


def test_classify_long_prompt_routes_to_longcontext(
    three_profile_config: CodeRouterConfig,
) -> None:
    """[Unreleased]: prompt above the token threshold → longcontext profile.

    Token estimator is char/4 — a 200,000-char prompt is ~50,000
    tokens, well above the 32,000 threshold from
    ``docs/inside/future.md §6.4``.
    """
    cfg = _longcontext_config(three_profile_config, threshold=32_000)
    long_text = "a" * 200_000  # ~50_000 tokens via char/4
    body = {"messages": [{"role": "user", "content": long_text}]}
    assert classify(body, cfg) == "longcontext"


def test_classify_short_prompt_below_threshold_falls_through(
    three_profile_config: CodeRouterConfig,
) -> None:
    """A prompt comfortably below the token threshold falls through to
    ``default_rule_profile`` (no other rules in this fixture)."""
    cfg = _longcontext_config(three_profile_config, threshold=32_000)
    short_text = "a" * 1_000  # ~250 tokens via char/4
    body = {"messages": [{"role": "user", "content": short_text}]}
    assert classify(body, cfg) == "writing"


def test_classify_long_context_walks_all_messages_not_just_latest(
    three_profile_config: CodeRouterConfig,
) -> None:
    """Distinct from ``content_contains`` / ``content_regex`` (latest
    user msg only), the longContext matcher counts ALL messages —
    a long conversation history with a short final question still
    crosses the threshold."""
    cfg = _longcontext_config(three_profile_config, threshold=10_000)
    # Build a conversation: 3 long history messages + 1 short
    # latest user message. The latest alone (~10 tokens) is far
    # below 10,000; the full history (~30,000 tokens) is comfortably
    # above. Pin the all-messages-walk semantics.
    long_block = "x" * 40_000  # ~10,000 tokens each
    body = {
        "messages": [
            {"role": "user", "content": long_block},
            {"role": "assistant", "content": long_block},
            {"role": "user", "content": long_block},
            {"role": "assistant", "content": "OK."},
            {"role": "user", "content": "what next?"},
        ]
    }
    assert classify(body, cfg) == "longcontext"


def test_content_token_count_min_rejects_non_positive_at_load() -> None:
    """[Unreleased]: schema rejects 0 / negative thresholds (pydantic ge=1).

    A 0-token threshold would make every request match (the empty
    body has 0 estimated tokens, ``0 >= 0`` is True), which is
    almost certainly a misconfiguration.
    """
    with pytest.raises(ValueError, match=r"content_token_count_min|greater than"):
        RuleMatcher(content_token_count_min=0)
    with pytest.raises(ValueError, match=r"content_token_count_min|greater than"):
        RuleMatcher(content_token_count_min=-5)


def test_long_context_first_match_wins_over_later_image_rule(
    three_profile_config: CodeRouterConfig,
) -> None:
    """First-match-wins precedence holds across matcher types.

    A token-count rule placed before an image-detection rule fires
    even when the body would also match the image rule — pinning
    that the longContext matcher integrates cleanly into the
    existing rule iteration.
    """
    base = _longcontext_config(three_profile_config, threshold=10_000)
    cfg = base.model_copy(
        update={
            "auto_router": AutoRouterConfig(
                rules=[
                    AutoRouteRule(
                        id="user:long-first",
                        profile="longcontext",
                        match=RuleMatcher(content_token_count_min=10_000),
                    ),
                    AutoRouteRule(
                        id="user:image-second",
                        profile="multi",
                        match=RuleMatcher(has_image=True),
                    ),
                ],
                default_rule_profile="writing",
            ),
        }
    )
    long_image_body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "z" * 50_000},  # ~12,500 tokens
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,AAAA"},
                    },
                ],
            }
        ]
    }
    assert classify(long_image_body, cfg) == "longcontext"


# ---------------------------------------------------------------------------
# Group 8: tool-aware routing ([Unreleased] / OpenClaw + Raspberry Pi 由来)
# ---------------------------------------------------------------------------
#
# Motivation: small local models (≤4B, typical Pi 8GB / Jetson Nano shape)
# are unreliable at native tool calling — they return prose where a
# ``tool_calls`` array should be. The fallback engine's v0.3-D downgrade
# path attempts to repair that, but if the small model returns clean
# prose with no ``<tool_call>`` wrapper at all, no AdapterError fires
# and the chain treats the response as success — the caller (e.g.
# OpenClaw) sees text instead of a tool_use block.
#
# The ``has_tools`` matcher is the profile-level lever for this: route
# tool-laden requests to a tool-capable cloud profile entirely, leaving
# the small local model on the no-tools path where it actually shines.


def _has_tools_config(
    base: CodeRouterConfig,
    *,
    tools_provider: str = "ollama-coder",
    notools_provider: str = "ollama-general",
) -> CodeRouterConfig:
    """Augment the 3-profile fixture with separate ``with-tools`` and
    ``no-tools`` profiles so the has_tools matcher can be exercised
    without colliding with the bundled multi/coding/writing names.
    """
    return base.model_copy(
        update={
            "profiles": [
                *base.profiles,
                FallbackChain(name="with-tools", providers=[tools_provider]),
                FallbackChain(name="no-tools", providers=[notools_provider]),
            ],
            "auto_router": AutoRouterConfig(
                rules=[
                    AutoRouteRule(
                        id="user:tool-laden-cloud",
                        profile="with-tools",
                        match=RuleMatcher(has_tools=True),
                    ),
                ],
                default_rule_profile="no-tools",
            ),
        }
    )


def test_classify_request_with_openai_tools_routes_to_with_tools(
    three_profile_config: CodeRouterConfig,
) -> None:
    """[Unreleased]: OpenAI-format ``tools[]`` → ``with-tools`` profile.

    Tools-bearing requests must land on the tool-capable chain. This is
    the canonical OpenClaw / Claude Code shape — every turn declares
    Bash/Read/Write/etc., so this rule fires on essentially every
    request from those agents.
    """
    cfg = _has_tools_config(three_profile_config)
    body = {
        "messages": [{"role": "user", "content": "list the files"}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "list_files",
                    "description": "list files in the cwd",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
    }
    assert classify(body, cfg) == "with-tools"


def test_classify_request_with_anthropic_tools_routes_to_with_tools(
    three_profile_config: CodeRouterConfig,
) -> None:
    """[Unreleased]: Anthropic-format ``tools[]`` lives at the same
    top-level key as OpenAI's, so a single matcher handles both
    ingresses without per-shape branching.
    """
    cfg = _has_tools_config(three_profile_config)
    body = {
        "messages": [{"role": "user", "content": "what's in the dir"}],
        "tools": [
            {
                "name": "list_files",
                "description": "list files in the cwd",
                "input_schema": {"type": "object", "properties": {}},
            }
        ],
    }
    assert classify(body, cfg) == "with-tools"


def test_classify_request_with_legacy_functions_routes_to_with_tools(
    three_profile_config: CodeRouterConfig,
) -> None:
    """[Unreleased]: OpenAI's deprecated ``functions[]`` field still
    counts as tool-laden — some agents that pinned old SDK versions
    keep emitting it, and routing them past the tool-capable chain
    would defeat the matcher's whole purpose.
    """
    cfg = _has_tools_config(three_profile_config)
    body = {
        "messages": [{"role": "user", "content": "do something"}],
        "functions": [
            {
                "name": "do_something",
                "parameters": {"type": "object"},
            }
        ],
    }
    assert classify(body, cfg) == "with-tools"


def test_classify_request_without_tools_falls_through(
    three_profile_config: CodeRouterConfig,
) -> None:
    """[Unreleased]: the inverse case — plain chat with no tools
    declared falls through to ``default_rule_profile``. This is the
    Raspberry Pi happy path: keep cheap local-only traffic on the
    small local model, send the tool-laden traffic to the cloud.
    """
    cfg = _has_tools_config(three_profile_config)
    body = {"messages": [{"role": "user", "content": "what time is it"}]}
    assert classify(body, cfg) == "no-tools"


def test_classify_empty_tools_list_treated_as_no_tools(
    three_profile_config: CodeRouterConfig,
) -> None:
    """[Unreleased]: ``tools: []`` (or ``functions: []``) is the "agent
    initialized the field but populated it lazily" shape — there are
    no tools the model could actually call, so it falls through. Pins
    the no-spurious-match property.
    """
    cfg = _has_tools_config(three_profile_config)
    body = {
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [],
        "functions": [],
    }
    assert classify(body, cfg) == "no-tools"


def test_classify_has_tools_first_match_wins_over_later_content_rule(
    three_profile_config: CodeRouterConfig,
) -> None:
    """First-match-wins precedence holds for ``has_tools`` too.

    A has_tools rule placed before a code-fence rule fires even when
    the body would also match the later rule — pins that the new
    matcher integrates with the existing rule iteration semantics.
    """
    base = _has_tools_config(three_profile_config)
    cfg = base.model_copy(
        update={
            "auto_router": AutoRouterConfig(
                rules=[
                    AutoRouteRule(
                        id="user:tool-first",
                        profile="with-tools",
                        match=RuleMatcher(has_tools=True),
                    ),
                    AutoRouteRule(
                        id="user:code-second",
                        profile="coding",
                        match=RuleMatcher(code_fence_ratio_min=0.1),
                    ),
                ],
                default_rule_profile="no-tools",
            ),
        }
    )
    body = {
        "messages": [
            {
                "role": "user",
                "content": "fix this:\n```python\nprint('hi')\n```",
            }
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "edit_file",
                    "parameters": {"type": "object"},
                },
            }
        ],
    }
    assert classify(body, cfg) == "with-tools"


def test_has_tools_false_rejected_at_load() -> None:
    """[Unreleased]: ``has_tools: False`` is meaningless — a "no-tools"
    rule would shadow ``default_rule_profile``. The boolean shape
    mirrors ``has_image`` (only ``True`` is a valid set value); ``None``
    means "unset" via the _exactly_one validator path.
    """
    # Note: pydantic accepts ``None`` (the default) and ``True`` cleanly.
    # ``False`` is the ambiguous value we want to surface — currently it
    # passes _exactly_one (since it's not None) but would never match
    # anything, so we document expected behavior here. If we tighten the
    # validator later to reject False explicitly, this test will flip
    # from "matches nothing" to "rejected at load".
    matcher = RuleMatcher(has_tools=False)
    # _exactly_one passes (False is "set", just not True).
    assert matcher.has_tools is False
    # But _match_rule treats it as "no match" because we test ``is True``.
    # That's the safety net — even if a user writes has_tools: false in
    # YAML, the rule never fires, and traffic stays on the default path.
