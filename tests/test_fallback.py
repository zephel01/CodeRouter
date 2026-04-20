"""Fallback engine tests — uses fake adapters, no httpx network calls."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic import ValidationError

from coderouter.adapters.base import (
    AdapterError,
    BaseAdapter,
    ChatRequest,
    ChatResponse,
    Message,
    ProviderCallOverrides,
    StreamChunk,
)
from coderouter.config.schemas import (
    Capabilities,
    CodeRouterConfig,
    FallbackChain,
    ProviderConfig,
)
from coderouter.routing import (
    FallbackEngine,
    MidStreamError,
    NoProvidersAvailableError,
)


class FakeAdapter(BaseAdapter):
    """Programmable adapter for tests."""

    def __init__(
        self,
        config,
        *,
        fail_with: AdapterError | None = None,
        text: str = "ok",
        chunks: list[str] | None = None,
        fail_after_chunks: int | None = None,
        midstream_error: AdapterError | None = None,
    ) -> None:
        super().__init__(config)
        self.fail_with = fail_with
        self.text = text
        self.chunks = chunks or [text]
        self.call_count = 0
        # When set, stream() yields `fail_after_chunks` chunks then raises
        # `midstream_error` (defaults to a generic retryable AdapterError).
        self.fail_after_chunks = fail_after_chunks
        self.midstream_error = midstream_error

    async def healthcheck(self) -> bool:
        return self.fail_with is None

    async def generate(
        self,
        request: ChatRequest,
        *,
        overrides: ProviderCallOverrides | None = None,
    ) -> ChatResponse:
        self.call_count += 1
        self.last_overrides = overrides
        if self.fail_with:
            raise self.fail_with
        return ChatResponse(
            id=f"fake-{self.name}-{self.call_count}",
            created=int(time.time()),
            model=self.config.model,
            choices=[
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": self.text},
                    "finish_reason": "stop",
                }
            ],
            coderouter_provider=self.name,
        )

    async def stream(
        self,
        request: ChatRequest,
        *,
        overrides: ProviderCallOverrides | None = None,
    ) -> AsyncIterator[StreamChunk]:
        self.call_count += 1
        self.last_overrides = overrides
        if self.fail_with:
            raise self.fail_with
        for i, piece in enumerate(self.chunks):
            if (
                self.fail_after_chunks is not None
                and i >= self.fail_after_chunks
            ):
                raise self.midstream_error or AdapterError(
                    "midstream failure",
                    provider=self.name,
                    retryable=True,
                )
            yield StreamChunk(
                id=f"fake-{self.name}-stream",
                created=int(time.time()),
                model=self.config.model,
                choices=[{"index": 0, "delta": {"content": piece}}],
            )


def _engine_with(
    config: CodeRouterConfig, fakes: dict[str, FakeAdapter]
) -> FallbackEngine:
    engine = FallbackEngine(config)
    engine._adapters = fakes  # type: ignore[assignment]  # tests poke internals
    return engine


def _request(profile: str | None = None) -> ChatRequest:
    return ChatRequest(messages=[Message(role="user", content="hi")], profile=profile)


@pytest.mark.asyncio
async def test_first_provider_wins(basic_config: CodeRouterConfig) -> None:
    fakes = {
        "local": FakeAdapter(basic_config.provider_by_name("local"), text="from local"),
        "free-cloud": FakeAdapter(basic_config.provider_by_name("free-cloud")),
        "paid-cloud": FakeAdapter(basic_config.provider_by_name("paid-cloud")),
    }
    engine = _engine_with(basic_config, fakes)
    resp = await engine.generate(_request())
    assert resp.coderouter_provider == "local"
    assert fakes["local"].call_count == 1
    assert fakes["free-cloud"].call_count == 0


@pytest.mark.asyncio
async def test_fallback_skips_failed_provider(
    basic_config: CodeRouterConfig,
) -> None:
    fakes = {
        "local": FakeAdapter(
            basic_config.provider_by_name("local"),
            fail_with=AdapterError("down", provider="local", retryable=True),
        ),
        "free-cloud": FakeAdapter(
            basic_config.provider_by_name("free-cloud"), text="from free"
        ),
        "paid-cloud": FakeAdapter(basic_config.provider_by_name("paid-cloud")),
    }
    engine = _engine_with(basic_config, fakes)
    resp = await engine.generate(_request())
    assert resp.coderouter_provider == "free-cloud"
    assert fakes["paid-cloud"].call_count == 0


@pytest.mark.asyncio
async def test_paid_blocked_when_allow_paid_false(
    basic_config: CodeRouterConfig,
) -> None:
    # Make local + free fail so only paid would be left
    fakes = {
        "local": FakeAdapter(
            basic_config.provider_by_name("local"),
            fail_with=AdapterError("down", provider="local", retryable=True),
        ),
        "free-cloud": FakeAdapter(
            basic_config.provider_by_name("free-cloud"),
            fail_with=AdapterError("rate", provider="free-cloud", retryable=True),
        ),
        "paid-cloud": FakeAdapter(basic_config.provider_by_name("paid-cloud")),
    }
    assert basic_config.allow_paid is False
    engine = _engine_with(basic_config, fakes)

    with pytest.raises(NoProvidersAvailableError):
        await engine.generate(_request())
    # paid was filtered out, never tried
    assert fakes["paid-cloud"].call_count == 0


@pytest.mark.asyncio
async def test_paid_used_when_allow_paid_true(
    basic_config: CodeRouterConfig,
) -> None:
    basic_config.allow_paid = True
    fakes = {
        "local": FakeAdapter(
            basic_config.provider_by_name("local"),
            fail_with=AdapterError("down", provider="local", retryable=True),
        ),
        "free-cloud": FakeAdapter(
            basic_config.provider_by_name("free-cloud"),
            fail_with=AdapterError("rate", provider="free-cloud", retryable=True),
        ),
        "paid-cloud": FakeAdapter(
            basic_config.provider_by_name("paid-cloud"), text="from paid"
        ),
    }
    engine = _engine_with(basic_config, fakes)
    resp = await engine.generate(_request())
    assert resp.coderouter_provider == "paid-cloud"


@pytest.mark.asyncio
async def test_non_retryable_error_aborts_chain(
    basic_config: CodeRouterConfig,
) -> None:
    fakes = {
        "local": FakeAdapter(
            basic_config.provider_by_name("local"),
            fail_with=AdapterError(
                "bad request", provider="local", status_code=400, retryable=False
            ),
        ),
        "free-cloud": FakeAdapter(basic_config.provider_by_name("free-cloud")),
        "paid-cloud": FakeAdapter(basic_config.provider_by_name("paid-cloud")),
    }
    engine = _engine_with(basic_config, fakes)
    with pytest.raises(NoProvidersAvailableError):
        await engine.generate(_request())
    # stopped at first non-retryable failure
    assert fakes["free-cloud"].call_count == 0


@pytest.mark.asyncio
async def test_streaming_first_provider_wins(
    basic_config: CodeRouterConfig,
) -> None:
    fakes = {
        "local": FakeAdapter(
            basic_config.provider_by_name("local"), chunks=["he", "llo"]
        ),
        "free-cloud": FakeAdapter(basic_config.provider_by_name("free-cloud")),
        "paid-cloud": FakeAdapter(basic_config.provider_by_name("paid-cloud")),
    }
    engine = _engine_with(basic_config, fakes)
    chunks: list[Any] = []
    req = _request()
    req.stream = True
    async for c in engine.stream(req):
        chunks.append(c)
    assert len(chunks) == 2
    assert fakes["free-cloud"].call_count == 0


@pytest.mark.asyncio
async def test_streaming_falls_back_when_first_errors_immediately(
    basic_config: CodeRouterConfig,
) -> None:
    fakes = {
        "local": FakeAdapter(
            basic_config.provider_by_name("local"),
            fail_with=AdapterError("down", provider="local", retryable=True),
        ),
        "free-cloud": FakeAdapter(
            basic_config.provider_by_name("free-cloud"), chunks=["hi"]
        ),
        "paid-cloud": FakeAdapter(basic_config.provider_by_name("paid-cloud")),
    }
    engine = _engine_with(basic_config, fakes)
    req = _request()
    req.stream = True
    chunks = [c async for c in engine.stream(req)]
    assert len(chunks) == 1
    assert fakes["paid-cloud"].call_count == 0


# ----------------------------------------------------------------------
# v0.3-B: Mid-stream fallback guard
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_midstream_failure_raises_midstream_error(
    basic_config: CodeRouterConfig,
) -> None:
    """If the active provider fails AFTER emitting chunks, the engine must
    NOT fall back — it raises MidStreamError so the caller can surface a
    terminal error to the client instead of silently switching providers
    (which would corrupt the partial stream the client has already seen).
    """
    fakes = {
        "local": FakeAdapter(
            basic_config.provider_by_name("local"),
            chunks=["hel", "lo", "_never_"],
            fail_after_chunks=2,
            midstream_error=AdapterError(
                "connection reset", provider="local", retryable=True
            ),
        ),
        "free-cloud": FakeAdapter(
            basic_config.provider_by_name("free-cloud"), chunks=["shouldnt-see"]
        ),
        "paid-cloud": FakeAdapter(basic_config.provider_by_name("paid-cloud")),
    }
    engine = _engine_with(basic_config, fakes)
    req = _request()
    req.stream = True

    received: list[Any] = []
    with pytest.raises(MidStreamError) as exc_info:
        async for c in engine.stream(req):
            received.append(c)

    # The client saw exactly the chunks that were emitted before the failure.
    assert len(received) == 2
    # The error carries the failing provider name + original AdapterError.
    assert exc_info.value.provider == "local"
    assert isinstance(exc_info.value.original, AdapterError)
    # Fallback did NOT happen — next provider was never called.
    assert fakes["free-cloud"].call_count == 0
    assert fakes["paid-cloud"].call_count == 0


@pytest.mark.asyncio
async def test_streaming_initial_error_still_falls_back(
    basic_config: CodeRouterConfig,
) -> None:
    """Sanity check: an error raised BEFORE the first chunk is emitted is
    still handled by the normal fallback path (it is not a mid-stream error).
    """
    fakes = {
        "local": FakeAdapter(
            basic_config.provider_by_name("local"),
            chunks=[],
            fail_after_chunks=0,  # fails before yielding anything
            midstream_error=AdapterError(
                "immediate failure", provider="local", retryable=True
            ),
        ),
        "free-cloud": FakeAdapter(
            basic_config.provider_by_name("free-cloud"), chunks=["ok"]
        ),
        "paid-cloud": FakeAdapter(basic_config.provider_by_name("paid-cloud")),
    }
    engine = _engine_with(basic_config, fakes)
    req = _request()
    req.stream = True
    chunks = [c async for c in engine.stream(req)]
    # Fell back cleanly to free-cloud.
    assert len(chunks) == 1
    assert fakes["free-cloud"].call_count == 1
    assert fakes["paid-cloud"].call_count == 0


# ----------------------------------------------------------------------
# v0.6-B: profile-level timeout_s / append_system_prompt override
# ----------------------------------------------------------------------


def _overrides_config(
    *,
    profile_timeout: float | None = None,
    profile_append: str | None = None,
    provider_timeout: float = 30.0,
    provider_append: str | None = None,
) -> CodeRouterConfig:
    """Build a minimal config with one provider and one profile, with
    the override fields selectively populated.

    Parameters default to values that simulate "no override" so individual
    tests only set what they actually care about — keeps the scenario
    tables readable.
    """
    return CodeRouterConfig(
        allow_paid=False,
        default_profile="prof",
        providers=[
            ProviderConfig(
                name="p0",
                base_url="http://localhost:8080/v1",
                model="m",
                timeout_s=provider_timeout,
                append_system_prompt=provider_append,
                capabilities=Capabilities(),
            ),
        ],
        profiles=[
            FallbackChain(
                name="prof",
                providers=["p0"],
                timeout_s=profile_timeout,
                append_system_prompt=profile_append,
            ),
        ],
    )


@pytest.mark.asyncio
async def test_profile_override_timeout_reaches_adapter() -> None:
    """Profile.timeout_s propagates to the adapter via ProviderCallOverrides."""
    cfg = _overrides_config(profile_timeout=7.5, provider_timeout=30.0)
    fake = FakeAdapter(cfg.provider_by_name("p0"))
    engine = _engine_with(cfg, {"p0": fake})
    await engine.generate(_request())
    assert fake.last_overrides is not None
    assert fake.last_overrides.timeout_s == 7.5
    # Provider default stays the authoritative fallback; override wins.
    assert fake.effective_timeout(fake.last_overrides) == 7.5


@pytest.mark.asyncio
async def test_profile_override_unset_defaults_to_provider() -> None:
    """When profile.timeout_s is None, effective_timeout falls through to
    the provider's own value. This is the baseline contract — unset
    overrides must not silently zero the timeout.
    """
    cfg = _overrides_config(profile_timeout=None, provider_timeout=42.0)
    fake = FakeAdapter(cfg.provider_by_name("p0"))
    engine = _engine_with(cfg, {"p0": fake})
    await engine.generate(_request())
    assert fake.last_overrides is not None
    assert fake.last_overrides.timeout_s is None
    assert fake.effective_timeout(fake.last_overrides) == 42.0


@pytest.mark.asyncio
async def test_profile_override_append_system_prompt_replaces_provider() -> None:
    """Profile-level append_system_prompt REPLACES the provider's value.

    Contrast with: no override → provider directive is used as-is. The
    replace semantic matches how timeout_s (a scalar limit) naturally
    behaves and keeps debugging predictable.
    """
    cfg = _overrides_config(
        profile_append="/profile-directive",
        provider_append="/provider-directive",
    )
    fake = FakeAdapter(cfg.provider_by_name("p0"))
    engine = _engine_with(cfg, {"p0": fake})
    await engine.generate(_request())
    assert (
        fake.effective_append_system_prompt(fake.last_overrides)
        == "/profile-directive"
    )


@pytest.mark.asyncio
async def test_profile_override_append_empty_string_clears_provider() -> None:
    """append_system_prompt="" on a profile = "clear the provider directive".

    Distinguishes "no override" (None → use provider value) from "override
    to none" (empty string → skip the directive entirely). This is the
    only way a profile can opt out of a directive its provider declares.
    """
    cfg = _overrides_config(
        profile_append="", provider_append="/provider-directive"
    )
    fake = FakeAdapter(cfg.provider_by_name("p0"))
    engine = _engine_with(cfg, {"p0": fake})
    await engine.generate(_request())
    assert fake.effective_append_system_prompt(fake.last_overrides) is None


def test_profile_override_fields_on_fallback_chain() -> None:
    """Schema-level sanity: both fields default to None and ``extra='forbid'``
    continues to reject misspelled keys — a regression guard for the
    v0.6-B addition.
    """
    chain = FallbackChain(name="x", providers=["p"])
    assert chain.timeout_s is None
    assert chain.append_system_prompt is None
    with pytest.raises(ValidationError):
        FallbackChain(name="x", providers=["p"], timeout_sec=10)  # type: ignore[call-arg]
