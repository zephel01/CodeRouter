"""Prometheus text exposition format (v1.5-B).

Reads a :class:`MetricsCollector` snapshot dict and renders it in the
text exposition format documented at
https://prometheus.io/docs/instrumenting/exposition_formats/ —
specifically the 0.0.4 variant that ``promtool check metrics`` validates.

Why a hand-roll instead of ``prometheus_client``
    plan.md §12.3.4: ~30 lines of format logic vs a 100kB+ dependency that
    also wants to install its own metric objects (double bookkeeping with
    our log-tap Collector). The format is stable (spec last changed 2017)
    and promtool gives us E2E validation at zero lib cost.

Metric naming
    Counters end in ``_total`` per convention. Gauges are plain names.
    All CodeRouter metrics are prefixed ``coderouter_`` to avoid
    collision when an operator already has app metrics on the same
    Prometheus target.

Label escaping
    Backslash, double-quote, and newline must be escaped inside label
    values (per spec). Metric names / label names are constructed
    internally and don't need escaping. Provider / profile / filter /
    capability names come from user config and ARE passed through the
    escape routine in case they contain funky characters.
"""

from __future__ import annotations

from typing import Any

# ``_total`` is the canonical Prometheus convention for monotone counters.
# ``coderouter_`` prefix keeps us from colliding with other apps when an
# operator scrapes multiple services onto one Prometheus target.
_PREFIX = "coderouter_"


def format_prometheus(snapshot: dict[str, Any]) -> str:
    """Render a MetricsCollector snapshot as Prometheus text exposition.

    Pure function over the dict returned by
    :meth:`coderouter.metrics.MetricsCollector.snapshot`, so unit tests
    can feed canned data without spinning up the handler. Returns a
    ``str`` terminated by a single newline — Prometheus parsers accept
    either trailing-newline or not, but ending on ``\\n`` keeps
    ``promtool`` happy.
    """
    lines: list[str] = []
    counters = snapshot.get("counters", {})

    # ---- Gauges ----------------------------------------------------------
    lines.extend(
        _gauge(
            name="uptime_seconds",
            help_text="Seconds since the CodeRouter process started.",
            value=snapshot.get("uptime_s", 0.0),
        )
    )

    # ---- Counters (scalar) ----------------------------------------------
    lines.extend(
        _counter(
            name="requests_total",
            help_text="Total requests dispatched to the fallback engine (``try-provider`` events).",
            samples=[((), counters.get("requests_total", 0))],
        )
    )
    lines.extend(
        _counter(
            name="chain_paid_gate_blocked_total",
            help_text="Chains where ALLOW_PAID=false filtered every provider out.",
            samples=[((), counters.get("chain_paid_gate_blocked_total", 0))],
        )
    )
    lines.extend(
        _counter(
            name="chain_uniform_auth_failure_total",
            help_text="Chains where every provider returned the same 401/403 auth failure.",
            samples=[((), counters.get("chain_uniform_auth_failure_total", 0))],
        )
    )

    # ---- Counters (per-provider) ----------------------------------------
    lines.extend(
        _counter(
            name="provider_attempts_total",
            help_text="``try-provider`` log events, broken down by provider.",
            samples=[
                ((("provider", p),), v)
                for p, v in sorted(counters.get("provider_attempts", {}).items())
            ],
        )
    )
    outcome_samples: list[tuple[tuple[tuple[str, str], ...], int]] = []
    for provider, outcomes in sorted(counters.get("provider_outcomes", {}).items()):
        for outcome, count in sorted(outcomes.items()):
            outcome_samples.append(
                ((("provider", provider), ("outcome", outcome)), count)
            )
    lines.extend(
        _counter(
            name="provider_outcomes_total",
            help_text="Per-provider outcomes: ok | failed | failed_midstream.",
            samples=outcome_samples,
        )
    )

    skipped_samples: list[tuple[tuple[tuple[str, str], ...], int]] = []
    for provider, count in sorted(counters.get("provider_skipped_paid", {}).items()):
        skipped_samples.append(
            ((("provider", provider), ("reason", "paid")), count)
        )
    for provider, count in sorted(counters.get("provider_skipped_unknown", {}).items()):
        skipped_samples.append(
            ((("provider", provider), ("reason", "unknown")), count)
        )
    lines.extend(
        _counter(
            name="provider_skipped_total",
            help_text="Providers skipped before a call was attempted, by reason.",
            samples=skipped_samples,
        )
    )

    # ---- Counters (per-capability / per-filter) -------------------------
    lines.extend(
        _counter(
            name="capability_degraded_total",
            help_text="Capability gate degradations, by dropped capability (thinking | cache_control | reasoning).",
            samples=[
                ((("capability", c),), v)
                for c, v in sorted(counters.get("capability_degraded", {}).items())
            ],
        )
    )
    lines.extend(
        _counter(
            name="output_filter_applied_total",
            help_text="Output-filter firings, by filter name (strip_thinking | strip_stop_markers).",
            samples=[
                ((("filter", f),), v)
                for f, v in sorted(counters.get("output_filter_applied", {}).items())
            ],
        )
    )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Internal helpers — compose HELP / TYPE / sample triples
# ---------------------------------------------------------------------------


def _counter(
    *,
    name: str,
    help_text: str,
    samples: list[tuple[tuple[tuple[str, str], ...], int]],
) -> list[str]:
    """Build HELP + TYPE + one line per (labels, value) sample.

    Prometheus permits emitting a counter with zero samples — HELP/TYPE
    still make the metric discoverable in the target metadata. We preserve
    that shape so a dashboard knows the metric exists even before the
    first event fires.
    """
    full_name = f"{_PREFIX}{name}"
    lines = [
        f"# HELP {full_name} {help_text}",
        f"# TYPE {full_name} counter",
    ]
    for labels, value in samples:
        lines.append(f"{full_name}{_fmt_labels(labels)} {value}")
    return lines


def _gauge(*, name: str, help_text: str, value: float) -> list[str]:
    """HELP + TYPE + a single sample for a scalar gauge.

    Gauges here are always scalar (no labels) in v1.5-B. When we add
    labeled gauges (e.g. per-provider last-tok/s), this helper will grow
    a ``samples`` parameter to match :func:`_counter`.
    """
    full_name = f"{_PREFIX}{name}"
    return [
        f"# HELP {full_name} {help_text}",
        f"# TYPE {full_name} gauge",
        f"{full_name} {value}",
    ]


def _fmt_labels(pairs: tuple[tuple[str, str], ...]) -> str:
    """Render a tuple of (key, value) pairs as ``{k="v",k2="v2"}`` or ``""``.

    Empty tuple → empty string (Prometheus permits unlabeled samples).
    Values pass through :func:`_escape_label_value`; keys are trusted
    (constructed internally).
    """
    if not pairs:
        return ""
    body = ",".join(f'{k}="{_escape_label_value(v)}"' for k, v in pairs)
    return "{" + body + "}"


def _escape_label_value(value: str) -> str:
    r"""Escape a label value per the Prometheus text format spec.

    From the spec: ``\`` → ``\\``, ``"`` → ``\"``, newline → ``\n``.
    Everything else (including dashes, dots, colons) is literal.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
