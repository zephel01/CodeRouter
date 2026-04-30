"""v1.10: per-provider monthly USD budget tests.

Three test groups:

- **BudgetTracker (pure)**: record / accumulate / month-rollover /
  is_over_budget threshold semantics. No engine involved.
- **CostConfig schema**: ``monthly_budget_usd`` field acceptance,
  rejection of negatives.
- **Engine integration**: chain resolver skips a provider whose
  current-month total has hit its budget; falls through to the next
  provider (mirrors the v0.6-C paid-gate test pattern). When ALL
  providers in the chain are over budget, ``NoProvidersAvailableError``
  is raised AND a ``chain-budget-exceeded`` warn fires.

The pure tests pin :func:`coderouter.routing.budget._utc_month_key`'s
``now=`` injection point (production calls pass ``None`` to use the
live UTC clock) so month-rollover behavior is deterministic without
monkey-patching globals.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest

from coderouter.adapters.anthropic_native import AnthropicAdapter
from coderouter.adapters.base import BaseAdapter, ProviderCallOverrides
from coderouter.config.schemas import (
    CodeRouterConfig,
    CostConfig,
    FallbackChain,
    ProviderConfig,
)
from coderouter.routing import FallbackEngine
from coderouter.routing.budget import BudgetTracker
from coderouter.routing.fallback import NoProvidersAvailableError
from coderouter.translation.anthropic import (
    AnthropicMessage,
    AnthropicRequest,
    AnthropicResponse,
    AnthropicStreamEvent,
    AnthropicUsage,
)

# ----------------------------------------------------------------------
# Group 1: BudgetTracker (pure, no engine)
# ----------------------------------------------------------------------


def test_budget_tracker_records_and_accumulates_per_provider() -> None:
    """Multiple ``record`` calls fold into the per-provider running total."""
    tracker = BudgetTracker()
    tracker.record("anthropic-direct", 0.50)
    tracker.record("anthropic-direct", 1.25)
    tracker.record("openrouter-claude", 0.75)

    assert tracker.total_for_provider("anthropic-direct") == pytest.approx(1.75)
    assert tracker.total_for_provider("openrouter-claude") == pytest.approx(0.75)
    # Untracked providers default to 0.0 — no KeyError.
    assert tracker.total_for_provider("ollama-local") == 0.0


def test_budget_tracker_is_over_budget_threshold_is_inclusive() -> None:
    """Hitting the budget exactly counts as "over" (>=, not >).

    The conservative interpretation: when the operator says
    "5 USD/month" and the running total lands at 5.00, the next call
    should not bill. This is the safer default.
    """
    tracker = BudgetTracker()
    tracker.record("anthropic-direct", 4.99)
    assert tracker.is_over_budget("anthropic-direct", 5.0) is False

    tracker.record("anthropic-direct", 0.01)  # → 5.00 exactly
    assert tracker.is_over_budget("anthropic-direct", 5.0) is True

    tracker.record("anthropic-direct", 0.50)  # → 5.50
    assert tracker.is_over_budget("anthropic-direct", 5.0) is True


def test_budget_tracker_month_rollover_resets_totals() -> None:
    """When the UTC calendar month changes, all totals zero out lazily."""
    tracker = BudgetTracker()
    april = datetime(2026, 4, 30, 23, 59, tzinfo=UTC)
    may = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)

    tracker.record("anthropic-direct", 4.50, now=april)
    assert tracker.total_for_provider("anthropic-direct", now=april) == pytest.approx(4.50)
    assert tracker.current_month(now=april) == "2026-04"

    # First read in May rolls the bucket — totals reset to 0 before
    # this query is answered.
    assert tracker.total_for_provider("anthropic-direct", now=may) == 0.0
    assert tracker.current_month(now=may) == "2026-05"
    # And subsequent records on the May side accumulate fresh.
    tracker.record("anthropic-direct", 0.30, now=may)
    assert tracker.total_for_provider("anthropic-direct", now=may) == pytest.approx(0.30)


# ----------------------------------------------------------------------
# Group 2: CostConfig schema
# ----------------------------------------------------------------------


def test_cost_config_accepts_monthly_budget_usd() -> None:
    """Schema accepts a non-negative float for ``monthly_budget_usd``."""
    cfg = CostConfig(
        input_tokens_per_million=3.0,
        output_tokens_per_million=15.0,
        monthly_budget_usd=5.0,
    )
    assert cfg.monthly_budget_usd == 5.0

    # Default: None (no cap).
    default_cfg = CostConfig(input_tokens_per_million=3.0)
    assert default_cfg.monthly_budget_usd is None


def test_cost_config_rejects_negative_monthly_budget_usd() -> None:
    """Negative budgets are rejected at schema load (pydantic ge=0.0)."""
    with pytest.raises(ValueError, match=r"monthly_budget_usd|greater than or equal"):
        CostConfig(input_tokens_per_million=3.0, monthly_budget_usd=-1.0)


# ----------------------------------------------------------------------
# Group 3: Engine integration
# ----------------------------------------------------------------------


class _BudgetAwareAnthropicAdapter(AnthropicAdapter):
    """Test double that returns a fixed-cost AnthropicResponse.

    ``input_tokens`` / ``output_tokens`` plus the provider's
    :class:`CostConfig` (``input_tokens_per_million=1_000_000`` for an
    easy 1 USD per token-million math) lets the test author dial how
    quickly a provider exhausts its budget.
    """

    def __init__(
        self,
        config: ProviderConfig,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        super().__init__(config)
        self._input_tokens = input_tokens
        self._output_tokens = output_tokens

    async def healthcheck(self) -> bool:
        return True

    async def generate_anthropic(
        self,
        request: AnthropicRequest,
        *,
        overrides: ProviderCallOverrides | None = None,
    ) -> AnthropicResponse:
        return AnthropicResponse(
            id="msg_budget",
            model=self.config.model,
            content=[{"type": "text", "text": "ok"}],
            stop_reason="end_turn",
            usage=AnthropicUsage(
                input_tokens=self._input_tokens,
                output_tokens=self._output_tokens,
            ),
            coderouter_provider=self.name,
        )

    async def stream_anthropic(  # pragma: no cover — non-streaming path is what we test
        self,
        request: AnthropicRequest,
        *,
        overrides: ProviderCallOverrides | None = None,
    ) -> AsyncIterator[AnthropicStreamEvent]:
        if False:
            yield


def _provider_with_budget(
    name: str, *, monthly_budget_usd: float
) -> ProviderConfig:
    return ProviderConfig(
        name=name,
        kind="anthropic",
        base_url="https://api.anthropic.com",
        model="claude-sonnet-4-6",
        api_key_env="ANTHROPIC_API_KEY",
        cost=CostConfig(
            # 1 USD per token (1_000_000 USD/M tokens) → with
            # input_tokens=N the per-attempt cost is N USD. Lets the
            # test compose 1-USD attempts that step over a 5-USD
            # budget in 6 calls, no rounding noise.
            input_tokens_per_million=1_000_000.0,
            output_tokens_per_million=0.0,
            monthly_budget_usd=monthly_budget_usd,
        ),
    )


def _config_with_chain(
    providers: list[ProviderConfig], chain: list[str]
) -> CodeRouterConfig:
    return CodeRouterConfig(
        allow_paid=False,
        default_profile="default",
        providers=providers,
        profiles=[FallbackChain(name="default", providers=chain)],
    )


def _engine_with_adapters(
    config: CodeRouterConfig, adapters: dict[str, BaseAdapter]
) -> FallbackEngine:
    """Construct a real FallbackEngine and override ``_adapters`` map."""
    engine = FallbackEngine(config)
    engine._adapters = adapters  # type: ignore[assignment]
    return engine


def _request() -> AnthropicRequest:
    return AnthropicRequest(
        max_tokens=64,
        messages=[AnthropicMessage(role="user", content="hi")],
    )


@pytest.mark.asyncio
async def test_engine_skips_provider_over_budget_and_falls_through(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Provider over its monthly budget → ``skip-budget-exceeded`` info
    + chain falls through to the next provider, no warn (chain still
    has a usable provider)."""
    primary = _provider_with_budget("primary", monthly_budget_usd=5.0)
    fallback = _provider_with_budget("fallback", monthly_budget_usd=10.0)
    config = _config_with_chain(
        [primary, fallback], chain=["primary", "fallback"]
    )
    primary_adapter = _BudgetAwareAnthropicAdapter(
        primary, input_tokens=10
    )
    fallback_adapter = _BudgetAwareAnthropicAdapter(
        fallback, input_tokens=2
    )
    engine = _engine_with_adapters(
        config, {"primary": primary_adapter, "fallback": fallback_adapter}
    )

    # Pre-load the budget tracker: pretend ``primary`` already spent
    # 5.00 USD this month.
    engine._budget.record("primary", 5.00)

    with caplog.at_level(logging.INFO, logger="coderouter"):
        resp = await engine.generate_anthropic(_request())

    # Fallback served the request.
    assert resp.coderouter_provider == "fallback"
    # ``skip-budget-exceeded`` info fired exactly once for primary.
    skip_records = [r for r in caplog.records if r.msg == "skip-budget-exceeded"]
    assert len(skip_records) == 1
    rec = skip_records[0]
    assert rec.provider == "primary"
    assert rec.monthly_budget_usd == 5.0
    assert rec.current_total_usd == pytest.approx(5.00)
    # Chain warn must NOT fire — at least one provider survived.
    chain_records = [r for r in caplog.records if r.msg == "chain-budget-exceeded"]
    assert chain_records == []


@pytest.mark.asyncio
async def test_engine_raises_when_all_providers_over_budget(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """All providers in the chain are over budget → empty chain →
    ``NoProvidersAvailableError`` raised AND ``chain-budget-exceeded``
    warn fires once (mirror of the paid-gate behavior)."""
    primary = _provider_with_budget("primary", monthly_budget_usd=5.0)
    fallback = _provider_with_budget("fallback", monthly_budget_usd=2.0)
    config = _config_with_chain(
        [primary, fallback], chain=["primary", "fallback"]
    )
    engine = _engine_with_adapters(
        config,
        {
            "primary": _BudgetAwareAnthropicAdapter(primary),
            "fallback": _BudgetAwareAnthropicAdapter(fallback),
        },
    )

    # Pre-load: both providers exhausted.
    engine._budget.record("primary", 5.00)
    engine._budget.record("fallback", 2.50)

    with (
        caplog.at_level(logging.INFO, logger="coderouter"),
        pytest.raises(NoProvidersAvailableError),
    ):
        await engine.generate_anthropic(_request())

    skip_records = [r for r in caplog.records if r.msg == "skip-budget-exceeded"]
    assert len(skip_records) == 2
    chain_records = [r for r in caplog.records if r.msg == "chain-budget-exceeded"]
    assert len(chain_records) == 1
    chain_rec = chain_records[0]
    assert chain_rec.profile == "default"
    assert chain_rec.blocked_providers == ["primary", "fallback"]
    # Chain warn carries the YYYY-MM month bucket so cross-month
    # diagnostics are easy.
    assert isinstance(chain_rec.month, str)
    assert len(chain_rec.month) == 7  # "YYYY-MM"


@pytest.mark.asyncio
async def test_engine_records_cost_into_budget_tracker_on_success(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A successful response folds its USD cost into the BudgetTracker.

    Pins the wiring between ``_emit_cache_observed`` /
    ``_emit_cache_observed_streaming`` and the engine's
    ``BudgetTracker`` — without this, pre-loading via tests is the
    only way to exhaust a budget, which would leave a silent gap in
    real-world usage.
    """
    primary = _provider_with_budget("primary", monthly_budget_usd=5.0)
    config = _config_with_chain([primary], chain=["primary"])
    # 3 input tokens * 1 USD/token = 3 USD per attempt at the test's
    # exaggerated pricing.
    primary_adapter = _BudgetAwareAnthropicAdapter(primary, input_tokens=3)
    engine = _engine_with_adapters(config, {"primary": primary_adapter})

    assert engine._budget.total_for_provider("primary") == 0.0

    with caplog.at_level(logging.INFO, logger="coderouter"):
        await engine.generate_anthropic(_request())

    # Cost recorded by _emit_cache_observed → BudgetTracker.record.
    assert engine._budget.total_for_provider("primary") == pytest.approx(3.00)

    # And subsequent attempts compound — second call brings total to
    # 6.00 USD (over the 5.00 budget), so the third attempt's chain
    # resolution skips the provider.
    await engine.generate_anthropic(_request())
    assert engine._budget.total_for_provider("primary") == pytest.approx(6.00)
    with pytest.raises(NoProvidersAvailableError):
        await engine.generate_anthropic(_request())
