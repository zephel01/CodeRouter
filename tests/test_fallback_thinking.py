"""Engine-level tests for the v0.5-A thinking capability gate.

Focus: FallbackEngine.generate_anthropic / stream_anthropic with
requests that carry `thinking: {type: enabled}`. We verify:

  1. Chain reordering — capable providers are pulled in front of
     non-capable ones, relative order preserved within each bucket.
  2. Degraded-fallback path — if all capable providers fail (or there
     are none), the block is stripped before the non-capable provider
     is called and a `capability-degraded` log fires.
  3. Non-thinking requests — chain order is unchanged.
  4. Wire-body check — the stripped request's model_dump() has no
     `thinking` key (what the adapter actually sends upstream).

These reuse the FakeAnthropicAdapter / FakeOpenAIAdapter scaffolding
established by tests/test_fallback_anthropic.py.
"""

from __future__ import annotations

import logging

import pytest

from coderouter.adapters.base import AdapterError, BaseAdapter
from coderouter.config.schemas import (
    Capabilities,
    CodeRouterConfig,
    FallbackChain,
    ProviderConfig,
)
from coderouter.routing import FallbackEngine
from coderouter.translation.anthropic import (
    AnthropicMessage,
    AnthropicRequest,
)
from tests.test_fallback_anthropic import (
    FakeAnthropicAdapter,
    FakeOpenAIAdapter,
)

# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _anthropic_provider(
    name: str,
    *,
    model: str,
    thinking_override: bool | None = None,
) -> ProviderConfig:
    """Build a kind:anthropic ProviderConfig. `thinking_override` lets
    tests force the YAML flag on/off regardless of the heuristic."""
    caps = Capabilities(
        thinking=bool(thinking_override) if thinking_override is not None else False
    )
    return ProviderConfig(
        name=name,
        kind="anthropic",
        base_url="https://api.anthropic.com",
        model=model,
        api_key_env="ANTHROPIC_API_KEY",
        capabilities=caps,
    )


def _openai_provider(name: str) -> ProviderConfig:
    return ProviderConfig(
        name=name,
        kind="openai_compat",
        base_url="http://localhost:11434/v1",
        model="qwen-coder",
    )


def _config(providers: list[ProviderConfig], chain: list[str]) -> CodeRouterConfig:
    return CodeRouterConfig(
        allow_paid=False,
        default_profile="default",
        providers=providers,
        profiles=[FallbackChain(name="default", providers=chain)],
    )


def _engine(config: CodeRouterConfig, adapters: dict[str, BaseAdapter]) -> FallbackEngine:
    engine = FallbackEngine.__new__(FallbackEngine)
    engine.config = config
    engine._adapters = adapters  # type: ignore[attr-defined]
    return engine


def _thinking_request() -> AnthropicRequest:
    """Request carrying `thinking: {type: enabled}` via extras."""
    return AnthropicRequest.model_validate(
        {
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "hi"}],
            "thinking": {"type": "enabled", "budget_tokens": 4096},
        }
    )


def _plain_request() -> AnthropicRequest:
    return AnthropicRequest(
        max_tokens=64,
        messages=[AnthropicMessage(role="user", content="hi")],
    )


# ----------------------------------------------------------------------
# Chain reordering
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capable_provider_pulled_to_front() -> None:
    """Chain is [incapable-first, capable-second] in YAML. A thinking
    request must try the capable one first, even though it's listed
    second. Capability-driven preference overrides user ordering only
    for capability-requiring requests — plain requests still follow the
    declared order (tested below)."""
    incapable_cfg = _anthropic_provider("sonnet-4-5", model="claude-sonnet-4-5-20250929")
    capable_cfg = _anthropic_provider("sonnet-4-6", model="claude-sonnet-4-6")
    config = _config(
        [incapable_cfg, capable_cfg],
        chain=["sonnet-4-5", "sonnet-4-6"],
    )
    incapable = FakeAnthropicAdapter(incapable_cfg, text="should not be called")
    capable = FakeAnthropicAdapter(capable_cfg, text="capable handled it")
    engine = _engine(config, {"sonnet-4-5": incapable, "sonnet-4-6": capable})

    resp = await engine.generate_anthropic(_thinking_request())

    assert resp.coderouter_provider == "sonnet-4-6"
    assert capable.generate_calls
    # Capable succeeded → incapable must not have been touched.
    assert incapable.generate_calls == []


@pytest.mark.asyncio
async def test_plain_request_preserves_user_ordering() -> None:
    """Regression guard: a request without thinking must still go in the
    order declared in providers.yaml."""
    first = _anthropic_provider("sonnet-4-5", model="claude-sonnet-4-5-20250929")
    second = _anthropic_provider("sonnet-4-6", model="claude-sonnet-4-6")
    config = _config([first, second], chain=["sonnet-4-5", "sonnet-4-6"])
    first_fake = FakeAnthropicAdapter(first, text="first wins")
    second_fake = FakeAnthropicAdapter(second, text="second unused")
    engine = _engine(config, {"sonnet-4-5": first_fake, "sonnet-4-6": second_fake})

    resp = await engine.generate_anthropic(_plain_request())

    assert resp.coderouter_provider == "sonnet-4-5"
    assert second_fake.generate_calls == []


# ----------------------------------------------------------------------
# Degraded fallback
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_degrades_to_incapable_when_capable_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Capable provider errors out → fall through to incapable after
    stripping thinking. A `capability-degraded` log must fire before the
    incapable provider is tried."""
    capable_cfg = _anthropic_provider("sonnet-4-6", model="claude-sonnet-4-6")
    incapable_cfg = _anthropic_provider("sonnet-4-5", model="claude-sonnet-4-5-20250929")
    config = _config(
        [capable_cfg, incapable_cfg],
        chain=["sonnet-4-6", "sonnet-4-5"],
    )
    capable = FakeAnthropicAdapter(
        capable_cfg,
        fail_with=AdapterError("rate limited", provider="sonnet-4-6", retryable=True),
    )
    incapable = FakeAnthropicAdapter(incapable_cfg, text="degraded answer")
    engine = _engine(config, {"sonnet-4-6": capable, "sonnet-4-5": incapable})

    with caplog.at_level(logging.INFO, logger="coderouter"):
        resp = await engine.generate_anthropic(_thinking_request())

    assert resp.content[0]["text"] == "degraded answer"
    # Exactly one capability-degraded log, tagged with the right provider.
    degraded = [r for r in caplog.records if r.msg == "capability-degraded"]
    assert len(degraded) == 1
    assert degraded[0].provider == "sonnet-4-5"
    assert degraded[0].dropped == ["thinking"]


@pytest.mark.asyncio
async def test_degrades_strips_thinking_from_wire_body() -> None:
    """The request handed to the non-capable adapter must not contain a
    `thinking` field — that would produce the exact 400 the gate exists
    to prevent."""
    incapable_cfg = _anthropic_provider("sonnet-4-5", model="claude-sonnet-4-5-20250929")
    # Only one provider in chain, incapable — forces the degraded path.
    config = _config([incapable_cfg], chain=["sonnet-4-5"])
    incapable = FakeAnthropicAdapter(incapable_cfg, text="ok")
    engine = _engine(config, {"sonnet-4-5": incapable})

    await engine.generate_anthropic(_thinking_request())

    assert len(incapable.generate_calls) == 1
    called_with = incapable.generate_calls[0]
    assert "thinking" not in (called_with.model_extra or {})
    # And when the fake adapter would serialize for HTTP, the body is clean:
    body = called_with.model_dump(exclude_none=True)
    assert "thinking" not in body


@pytest.mark.asyncio
async def test_no_degraded_log_when_capable_handles_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If the capable provider succeeds, no capability-degraded log should
    fire (we only emit on the actual hand-off)."""
    capable_cfg = _anthropic_provider("sonnet-4-6", model="claude-sonnet-4-6")
    incapable_cfg = _anthropic_provider("sonnet-4-5", model="claude-sonnet-4-5-20250929")
    config = _config(
        [capable_cfg, incapable_cfg],
        chain=["sonnet-4-6", "sonnet-4-5"],
    )
    capable = FakeAnthropicAdapter(capable_cfg, text="ok")
    incapable = FakeAnthropicAdapter(incapable_cfg, text="unused")
    engine = _engine(config, {"sonnet-4-6": capable, "sonnet-4-5": incapable})

    with caplog.at_level(logging.INFO, logger="coderouter"):
        await engine.generate_anthropic(_thinking_request())

    degraded = [r for r in caplog.records if r.msg == "capability-degraded"]
    assert degraded == []


@pytest.mark.asyncio
async def test_no_degraded_log_for_plain_request(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Plain request → gate doesn't fire at all, even if the chain is
    mixed-capability."""
    incapable_cfg = _anthropic_provider("sonnet-4-5", model="claude-sonnet-4-5-20250929")
    config = _config([incapable_cfg], chain=["sonnet-4-5"])
    incapable = FakeAnthropicAdapter(incapable_cfg, text="ok")
    engine = _engine(config, {"sonnet-4-5": incapable})

    with caplog.at_level(logging.INFO, logger="coderouter"):
        await engine.generate_anthropic(_plain_request())

    degraded = [r for r in caplog.records if r.msg == "capability-degraded"]
    assert degraded == []


# ----------------------------------------------------------------------
# Mixed kinds: openai_compat is always incapable, anthropic is heuristic.
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openai_compat_is_always_degraded_bucket() -> None:
    """kind: openai_compat with a Claude-looking model name (e.g. the
    OpenRouter slug) is still considered incapable because the OpenAI
    wire format has no thinking field. With a capable anthropic first,
    openai_compat only runs on fallback."""
    capable_cfg = _anthropic_provider("sonnet-4-6", model="claude-sonnet-4-6")
    compat_cfg = _openai_provider("openrouter-claude")
    config = _config(
        [capable_cfg, compat_cfg],
        chain=["sonnet-4-6", "openrouter-claude"],
    )
    capable = FakeAnthropicAdapter(capable_cfg, text="capable")
    compat = FakeOpenAIAdapter(compat_cfg, text="compat")
    engine = _engine(config, {"sonnet-4-6": capable, "openrouter-claude": compat})

    resp = await engine.generate_anthropic(_thinking_request())
    # Capable answered, compat never consulted.
    assert resp.coderouter_provider == "sonnet-4-6"
    assert compat.generate_calls == []


@pytest.mark.asyncio
async def test_yaml_thinking_true_promotes_provider_to_capable_bucket() -> None:
    """Escape hatch: user declares `capabilities.thinking: true` on an
    anthropic provider whose model name isn't in the heuristic (e.g. a
    future family). The engine must honor that and route thinking
    requests there first."""
    capable_cfg = _anthropic_provider(
        "future-family",
        model="claude-sonnet-5-0",  # not in heuristic
        thinking_override=True,
    )
    incapable_cfg = _anthropic_provider("sonnet-4-5", model="claude-sonnet-4-5-20250929")
    config = _config(
        [incapable_cfg, capable_cfg],
        chain=["sonnet-4-5", "future-family"],
    )
    incapable = FakeAnthropicAdapter(incapable_cfg, text="no")
    capable = FakeAnthropicAdapter(capable_cfg, text="future")
    engine = _engine(config, {"sonnet-4-5": incapable, "future-family": capable})

    resp = await engine.generate_anthropic(_thinking_request())
    assert resp.coderouter_provider == "future-family"
    assert incapable.generate_calls == []


# ----------------------------------------------------------------------
# Streaming path (mirror test)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_reordering_prefers_capable() -> None:
    """Same ordering guarantee holds for stream_anthropic."""
    incapable_cfg = _anthropic_provider("sonnet-4-5", model="claude-sonnet-4-5-20250929")
    capable_cfg = _anthropic_provider("sonnet-4-6", model="claude-sonnet-4-6")
    config = _config(
        [incapable_cfg, capable_cfg],
        chain=["sonnet-4-5", "sonnet-4-6"],
    )
    incapable = FakeAnthropicAdapter(incapable_cfg, text="nope")
    capable = FakeAnthropicAdapter(capable_cfg, text="streamed")
    engine = _engine(config, {"sonnet-4-5": incapable, "sonnet-4-6": capable})

    events = [ev async for ev in engine.stream_anthropic(_thinking_request())]

    assert events  # stream produced events
    assert capable.stream_calls
    assert incapable.stream_calls == []
