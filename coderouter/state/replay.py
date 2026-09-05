"""Replay analysis engine (v2.0-K Replay framework).

Provides statistical A/B comparison of request journal entries across
providers.  Since the request journal records only metadata (token
counts, cost, streaming flag) — **not** request/response bodies — this
is *statistical replay*, not literal re-execution.

Typical use: an operator changes the fallback chain (swap provider A
for provider B) and wants to know how the new routing affected cost,
token counts, and request distribution compared to the previous window.

Usage::

    from coderouter.state.request_log import read_request_log
    from coderouter.state.replay import compare_providers, summarize_window

    entries = read_request_log("~/.coderouter-t/state/requests.jsonl")
    summary = summarize_window(entries)
    comparison = compare_providers(entries, "anthropic-api", "openrouter-free")
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# ------------------------------------------------------------------
# Per-provider summary
# ------------------------------------------------------------------


@dataclass
class ProviderSummary:
    """Aggregated statistics for one provider over a time window."""

    provider: str
    request_count: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0
    total_cost_savings_usd: float = 0.0
    total_cache_read_tokens: int = 0
    total_cache_creation_tokens: int = 0
    streaming_count: int = 0
    # Derived (populated by _finalize)
    avg_input_tokens: float = 0.0
    avg_output_tokens: float = 0.0
    avg_cost_usd: float = 0.0
    streaming_ratio: float = 0.0
    cache_hit_ratio: float = 0.0

    def _finalize(self) -> None:
        """Compute derived averages and ratios."""
        n = self.request_count
        if n > 0:
            self.avg_input_tokens = self.total_input_tokens / n
            self.avg_output_tokens = self.total_output_tokens / n
            self.avg_cost_usd = self.total_cost_usd / n
            self.streaming_ratio = self.streaming_count / n
        total_input = self.total_input_tokens + self.total_cache_read_tokens
        if total_input > 0:
            self.cache_hit_ratio = self.total_cache_read_tokens / total_input


@dataclass
class WindowSummary:
    """Full window summary across all providers."""

    total_requests: int = 0
    total_cost_usd: float = 0.0
    total_cost_savings_usd: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    providers: dict[str, ProviderSummary] = field(default_factory=dict)
    first_ts: str = ""
    last_ts: str = ""


def summarize_window(entries: list[dict[str, object]]) -> WindowSummary:
    """Aggregate request journal entries into a :class:`WindowSummary`.

    Parameters
    ----------
    entries
        Parsed JSONL dicts from :func:`read_request_log`.

    Returns
    -------
    WindowSummary
        Per-provider and overall statistics.
    """
    summary = WindowSummary()
    for entry in entries:
        provider = str(entry.get("provider", "unknown"))
        if provider not in summary.providers:
            summary.providers[provider] = ProviderSummary(provider=provider)

        ps = summary.providers[provider]
        ps.request_count += 1

        input_tokens = int(entry.get("input_tokens", 0))
        output_tokens = int(entry.get("output_tokens", 0))
        cost_usd = float(entry.get("cost_usd", 0.0))
        cost_savings = float(entry.get("cost_savings_usd", 0.0))
        cache_read = int(entry.get("cache_read_input_tokens", 0))
        cache_creation = int(entry.get("cache_creation_input_tokens", 0))
        streaming = bool(entry.get("streaming", False))

        ps.total_input_tokens += input_tokens
        ps.total_output_tokens += output_tokens
        ps.total_cost_usd += cost_usd
        ps.total_cost_savings_usd += cost_savings
        ps.total_cache_read_tokens += cache_read
        ps.total_cache_creation_tokens += cache_creation
        if streaming:
            ps.streaming_count += 1

        summary.total_requests += 1
        summary.total_cost_usd += cost_usd
        summary.total_cost_savings_usd += cost_savings
        summary.total_input_tokens += input_tokens
        summary.total_output_tokens += output_tokens

        ts = str(entry.get("ts", ""))
        if ts:
            if not summary.first_ts or ts < summary.first_ts:
                summary.first_ts = ts
            if not summary.last_ts or ts > summary.last_ts:
                summary.last_ts = ts

    for ps in summary.providers.values():
        ps._finalize()

    return summary


# ------------------------------------------------------------------
# A/B provider comparison
# ------------------------------------------------------------------


@dataclass
class ProviderComparison:
    """Side-by-side comparison of two providers."""

    provider_a: ProviderSummary
    provider_b: ProviderSummary
    # Deltas: B - A (positive = B is larger)
    delta_avg_input_tokens: float = 0.0
    delta_avg_output_tokens: float = 0.0
    delta_avg_cost_usd: float = 0.0
    delta_total_cost_usd: float = 0.0
    # Percentage changes (relative to A; NaN if A is zero)
    pct_avg_cost_change: float = 0.0
    pct_total_cost_change: float = 0.0


def compare_providers(
    entries: list[dict[str, object]],
    provider_a: str,
    provider_b: str,
) -> ProviderComparison:
    """Compare two providers' statistics from the same journal.

    Parameters
    ----------
    entries
        Parsed JSONL dicts from :func:`read_request_log`.
    provider_a, provider_b
        Provider names to compare. Entries not matching either are
        ignored.

    Returns
    -------
    ProviderComparison
        Side-by-side stats with deltas.
    """
    a_entries = [e for e in entries if str(e.get("provider", "")) == provider_a]
    b_entries = [e for e in entries if str(e.get("provider", "")) == provider_b]

    a_summary = summarize_window(a_entries)
    b_summary = summarize_window(b_entries)

    ps_a = a_summary.providers.get(
        provider_a, ProviderSummary(provider=provider_a)
    )
    ps_b = b_summary.providers.get(
        provider_b, ProviderSummary(provider=provider_b)
    )

    comparison = ProviderComparison(provider_a=ps_a, provider_b=ps_b)
    comparison.delta_avg_input_tokens = ps_b.avg_input_tokens - ps_a.avg_input_tokens
    comparison.delta_avg_output_tokens = ps_b.avg_output_tokens - ps_a.avg_output_tokens
    comparison.delta_avg_cost_usd = ps_b.avg_cost_usd - ps_a.avg_cost_usd
    comparison.delta_total_cost_usd = ps_b.total_cost_usd - ps_a.total_cost_usd

    if ps_a.avg_cost_usd > 0:
        comparison.pct_avg_cost_change = (
            (ps_b.avg_cost_usd - ps_a.avg_cost_usd) / ps_a.avg_cost_usd * 100.0
        )
    else:
        comparison.pct_avg_cost_change = float("nan")

    if ps_a.total_cost_usd > 0:
        comparison.pct_total_cost_change = (
            (ps_b.total_cost_usd - ps_a.total_cost_usd) / ps_a.total_cost_usd * 100.0
        )
    else:
        comparison.pct_total_cost_change = float("nan")

    return comparison


# ------------------------------------------------------------------
# CLI table formatting helpers
# ------------------------------------------------------------------


def format_summary_table(summary: WindowSummary) -> str:
    """Render a :class:`WindowSummary` as a CLI table.

    Returns a plain-text table suitable for terminal output.
    """
    lines: list[str] = []
    lines.append(f"Window: {summary.first_ts} → {summary.last_ts}")
    lines.append(f"Total: {summary.total_requests} requests, "
                 f"${summary.total_cost_usd:.4f} cost, "
                 f"${summary.total_cost_savings_usd:.4f} savings")
    lines.append("")

    # Header
    hdr = (
        f"{'Provider':<25} {'Reqs':>6} {'AvgIn':>8} {'AvgOut':>8} "
        f"{'AvgCost':>10} {'TotalCost':>10} {'Cache%':>7} {'Stream%':>8}"
    )
    lines.append(hdr)
    lines.append("-" * len(hdr))

    for ps in sorted(summary.providers.values(),
                     key=lambda p: p.total_cost_usd, reverse=True):
        lines.append(
            f"{ps.provider:<25} {ps.request_count:>6} "
            f"{ps.avg_input_tokens:>8.0f} {ps.avg_output_tokens:>8.0f} "
            f"${ps.avg_cost_usd:>9.4f} ${ps.total_cost_usd:>9.4f} "
            f"{ps.cache_hit_ratio * 100:>6.1f}% "
            f"{ps.streaming_ratio * 100:>7.1f}%"
        )

    return "\n".join(lines)


def format_comparison_table(comp: ProviderComparison) -> str:
    """Render a :class:`ProviderComparison` as a CLI table.

    Returns a plain-text side-by-side comparison table.
    """
    a = comp.provider_a
    b = comp.provider_b
    lines: list[str] = []

    hdr = f"{'Metric':<25} {a.provider:<20} {b.provider:<20} {'Delta':>12}"
    lines.append(hdr)
    lines.append("-" * len(hdr))

    def _row(label: str, va: object, vb: object, delta: float, fmt: str = ".0f") -> str:
        d_str = f"{delta:+{fmt}}"
        return f"{label:<25} {va!s:<20} {vb!s:<20} {d_str:>12}"

    lines.append(_row("Requests", a.request_count, b.request_count,
                       b.request_count - a.request_count))
    lines.append(_row("Avg input tokens", f"{a.avg_input_tokens:.0f}",
                       f"{b.avg_input_tokens:.0f}",
                       comp.delta_avg_input_tokens))
    lines.append(_row("Avg output tokens", f"{a.avg_output_tokens:.0f}",
                       f"{b.avg_output_tokens:.0f}",
                       comp.delta_avg_output_tokens))
    lines.append(_row("Avg cost (USD)", f"${a.avg_cost_usd:.4f}",
                       f"${b.avg_cost_usd:.4f}",
                       comp.delta_avg_cost_usd, fmt=".4f"))
    lines.append(_row("Total cost (USD)", f"${a.total_cost_usd:.4f}",
                       f"${b.total_cost_usd:.4f}",
                       comp.delta_total_cost_usd, fmt=".4f"))
    lines.append(_row("Cache hit ratio", f"{a.cache_hit_ratio * 100:.1f}%",
                       f"{b.cache_hit_ratio * 100:.1f}%",
                       (b.cache_hit_ratio - a.cache_hit_ratio) * 100, fmt=".1f"))
    lines.append(_row("Streaming ratio", f"{a.streaming_ratio * 100:.1f}%",
                       f"{b.streaming_ratio * 100:.1f}%",
                       (b.streaming_ratio - a.streaming_ratio) * 100, fmt=".1f"))

    # Cost change summary
    lines.append("")
    if not math.isnan(comp.pct_avg_cost_change):
        direction = "cheaper" if comp.pct_avg_cost_change < 0 else "more expensive"
        lines.append(
            f"Per-request: {b.provider} is {abs(comp.pct_avg_cost_change):.1f}% "
            f"{direction} than {a.provider}"
        )
    if not math.isnan(comp.pct_total_cost_change):
        direction = "less" if comp.pct_total_cost_change < 0 else "more"
        lines.append(
            f"Total spend: {b.provider} spent {abs(comp.pct_total_cost_change):.1f}% "
            f"{direction} than {a.provider}"
        )

    return "\n".join(lines)


__all__ = [
    "ProviderComparison",
    "ProviderSummary",
    "WindowSummary",
    "compare_providers",
    "format_comparison_table",
    "format_summary_table",
    "summarize_window",
]
