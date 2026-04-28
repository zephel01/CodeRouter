"""Unit tests for v1.9-A Cache Observability Prometheus exposition.

Exercises the cache-specific extensions to
:func:`coderouter.metrics.format_prometheus` — the rest of the
formatter contract is covered in ``test_metrics_prometheus.py``.

Pure-function tests over canned snapshot dicts. End-to-end emission
is covered separately by the engine + endpoint tests.
"""

from __future__ import annotations

from typing import Any

from coderouter.metrics import format_prometheus


def _snapshot_with_cache(
    *,
    cache_read_tokens: dict[str, int],
    cache_creation_tokens: dict[str, int],
    cache_outcomes: dict[str, dict[str, int]],
    cache_read_total: int = 0,
    cache_creation_total: int = 0,
) -> dict[str, Any]:
    """Build a minimal snapshot that exercises the v1.9-A counters.

    Only the cache-relevant fields are populated; non-cache counters get
    empty defaults so the rest of the exposition behaves like an idle
    snapshot.
    """
    return {
        "uptime_s": 1.0,
        "started_at": "2026-04-28T12:00:00",
        "startup": {},
        "counters": {
            "requests_total": 0,
            "chain_paid_gate_blocked_total": 0,
            "chain_uniform_auth_failure_total": 0,
            "auto_router_fallthrough_total": 0,
            "cache_read_tokens_total": cache_read_total,
            "cache_creation_tokens_total": cache_creation_total,
            "provider_attempts": {},
            "provider_outcomes": {},
            "provider_skipped_paid": {},
            "provider_skipped_unknown": {},
            "capability_degraded": {},
            "output_filter_applied": {},
            "cache_read_tokens": cache_read_tokens,
            "cache_creation_tokens": cache_creation_tokens,
            "cache_outcomes": cache_outcomes,
        },
        "providers": [],
        "recent": [],
    }


def test_cache_help_and_type_lines_present_even_when_empty() -> None:
    """Idle snapshot still emits HELP + TYPE for the v1.9-A metrics.

    Per Prometheus conventions, an empty counter is fine — discovery
    relies on HELP/TYPE being present in the target metadata so dashboards
    know the metric exists before the first event fires.
    """
    out = format_prometheus(
        _snapshot_with_cache(
            cache_read_tokens={},
            cache_creation_tokens={},
            cache_outcomes={},
        )
    )
    for metric in (
        "coderouter_cache_read_tokens_total",
        "coderouter_cache_creation_tokens_total",
        "coderouter_cache_observed_total",
    ):
        assert f"# HELP {metric} " in out, f"missing HELP for {metric}"
        assert f"# TYPE {metric} counter" in out, f"missing TYPE for {metric}"


def test_cache_read_tokens_renders_per_provider_label() -> None:
    out = format_prometheus(
        _snapshot_with_cache(
            cache_read_tokens={"anthropic-direct": 2048, "lmstudio-9b": 512},
            cache_creation_tokens={},
            cache_outcomes={},
            cache_read_total=2560,
        )
    )
    assert (
        'coderouter_cache_read_tokens_total{provider="anthropic-direct"} 2048'
        in out
    )
    assert 'coderouter_cache_read_tokens_total{provider="lmstudio-9b"} 512' in out


def test_cache_creation_tokens_renders_per_provider_label() -> None:
    out = format_prometheus(
        _snapshot_with_cache(
            cache_read_tokens={},
            cache_creation_tokens={"anthropic-direct": 4096},
            cache_outcomes={},
            cache_creation_total=4096,
        )
    )
    assert (
        'coderouter_cache_creation_tokens_total{provider="anthropic-direct"} 4096'
        in out
    )


def test_cache_observed_renders_provider_outcome_pair_label() -> None:
    """4-class outcome breakdown surfaces as ``{provider, outcome}`` labels.

    PromQL queries that pivot on outcome (``sum by (outcome) (...)``)
    rely on the label being a separate dimension rather than baked into
    the metric name.
    """
    out = format_prometheus(
        _snapshot_with_cache(
            cache_read_tokens={},
            cache_creation_tokens={},
            cache_outcomes={
                "lmstudio": {
                    "cache_hit": 12,
                    "cache_creation": 3,
                    "no_cache": 2,
                    "unknown": 1,
                },
            },
        )
    )
    for outcome, count in (
        ("cache_creation", 3),
        ("cache_hit", 12),
        ("no_cache", 2),
        ("unknown", 1),
    ):
        line = (
            f'coderouter_cache_observed_total{{provider="lmstudio",outcome="'
            f'{outcome}"}} {count}'
        )
        assert line in out, f"missing {line!r} in:\n{out}"


def test_cache_metric_names_end_with_total() -> None:
    """v1.9-A counters follow the ``_total`` suffix convention."""
    out = format_prometheus(
        _snapshot_with_cache(
            cache_read_tokens={"x": 1},
            cache_creation_tokens={"x": 1},
            cache_outcomes={"x": {"cache_hit": 1}},
        )
    )
    for line in out.splitlines():
        if line.startswith("# TYPE coderouter_cache_") and line.endswith(" counter"):
            name = line.split()[2]
            assert name.endswith("_total"), f"counter {name} missing _total suffix"
