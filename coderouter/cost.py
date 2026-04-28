"""Cost calculation utilities (v1.9-D Cost-aware Dashboard).

Pure functions for translating per-request token counts into USD
spend, accounting for Anthropic's prompt-cache pricing model
(``cache_read`` at 10% of normal input, ``cache_creation`` at 125%).

Where this fits
===============

The engine's ``_emit_cache_observed`` site (v1.9-A) calls
:func:`compute_cost_for_attempt` to enrich the ``cache-observed``
log line with ``cost_usd`` + ``cost_savings_usd`` fields. The
MetricsCollector then aggregates per-provider totals over the
process lifetime, and the dashboard / ``coderouter stats --cost``
TUI render those aggregates.

Why a separate module
=====================

Pricing math is small, pure, and shared by:

  * the engine's per-request cost calc
  * the collector's snapshot rendering (recomputes a "what-if no
    cache" total for the savings panel)
  * the future ``coderouter stats --cost`` CLI

Keeping it as a leaf module with no engine / collector imports
prevents circular dependencies and makes the pricing semantics
trivially testable in isolation.

Anthropic pricing reference (verified 2026-04)
==============================================

For Sonnet / Opus / Haiku 4.x:

  * Normal input  : ``input_tokens_per_million``         x 1.0
  * Cache read    : ``input_tokens_per_million``         x 0.10
  * Cache creation: ``input_tokens_per_million``         x 1.25
  * Normal output : ``output_tokens_per_million``        x 1.0

Tokens reported by the upstream:

  * ``input_tokens`` — "fresh" input (cache reads / writes are
    excluded from this count and reported via the cache fields).
  * ``cache_read_input_tokens`` — served from prompt cache.
  * ``cache_creation_input_tokens`` — written to prompt cache.
  * ``output_tokens`` — completion.

So a single response's billable cost is the sum of the four buckets
billed at their respective rates. The "savings" figure is the
counterfactual: what the operator *would have* paid without prompt
caching, so it focuses on the cache_read tokens (those are the
ones that got the 90% discount). cache_creation is a premium, not
a savings, so it doesn't enter the savings figure even though it's
in the cost calc.
"""

from __future__ import annotations

from dataclasses import dataclass

from coderouter.config.schemas import CostConfig


@dataclass(frozen=True)
class CostBreakdown:
    """Per-attempt cost components, all in USD.

    All fields default to 0.0 so a free / unconfigured provider
    yields a zero breakdown without callers having to special-case
    None.

    Fields
        total_usd: full cost charged for this attempt (sum of the
            four token buckets at their respective rates).
        savings_usd: hypothetical "no-cache" delta — what the
            operator *would have* paid for ``cache_read_input_tokens``
            at full input rate, minus what they actually paid at
            ``cache_read_discount`` rate. Always >= 0.
        input_usd / output_usd / cache_read_usd / cache_creation_usd:
            per-bucket breakdown for the dashboard's stacked bar
            chart. ``input_usd`` is "fresh input only" (does not
            include cache buckets); cache_read_usd / cache_creation_usd
            are the post-discount / post-premium values.
    """

    total_usd: float = 0.0
    savings_usd: float = 0.0
    input_usd: float = 0.0
    output_usd: float = 0.0
    cache_read_usd: float = 0.0
    cache_creation_usd: float = 0.0


_PER_MILLION: float = 1_000_000.0


def compute_cost_for_attempt(
    cost_config: CostConfig | None,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_input_tokens: int,
    cache_creation_input_tokens: int,
) -> CostBreakdown:
    """Translate per-attempt token counts into a USD :class:`CostBreakdown`.

    Returns a zero-filled breakdown when:
      * ``cost_config`` is ``None`` (provider has no pricing
        declared — typical for local models)
      * Both ``input_tokens_per_million`` and ``output_tokens_per_million``
        are unset (a partial declaration is permitted but the
        resulting cost is whatever the set fields can compute)

    Negative or zero token counts are accepted and contribute zero
    cost — the engine never emits negatives, but this defensive
    handling keeps a malformed log line from corrupting the
    aggregate counters in the collector.
    """
    if cost_config is None:
        return CostBreakdown()

    input_rate = (cost_config.input_tokens_per_million or 0.0) / _PER_MILLION
    output_rate = (cost_config.output_tokens_per_million or 0.0) / _PER_MILLION

    safe_input = max(input_tokens, 0)
    safe_output = max(output_tokens, 0)
    safe_read = max(cache_read_input_tokens, 0)
    safe_create = max(cache_creation_input_tokens, 0)

    input_usd = safe_input * input_rate
    output_usd = safe_output * output_rate
    cache_read_usd = safe_read * input_rate * cost_config.cache_read_discount
    cache_creation_usd = safe_create * input_rate * cost_config.cache_creation_premium

    total_usd = input_usd + output_usd + cache_read_usd + cache_creation_usd

    # Savings = what the operator would have paid at full input rate
    # for the cache_read tokens, minus what they actually paid at
    # the discounted rate. cache_creation is a *premium* (not a
    # savings) so it doesn't enter the savings figure — including
    # it would let a cache miss show up as "negative savings" which
    # is semantically wrong and would confuse the dashboard.
    full_rate_for_cache_read = safe_read * input_rate
    savings_usd = full_rate_for_cache_read - cache_read_usd

    return CostBreakdown(
        total_usd=total_usd,
        savings_usd=max(savings_usd, 0.0),
        input_usd=input_usd,
        output_usd=output_usd,
        cache_read_usd=cache_read_usd,
        cache_creation_usd=cache_creation_usd,
    )
