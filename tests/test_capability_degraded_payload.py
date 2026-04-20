"""Unit tests for the ``capability-degraded`` payload contract (v0.5.1).

v0.5 shipped three gates (thinking / cache_control / reasoning) that all
emit a log record with the same three-field shape. v0.5.1 promotes that
shape to a ``TypedDict`` + a single ``log_capability_degraded`` chokepoint
so that:

  1. Field-name drift between callers is caught at type-check time (a
     future metrics consumer will not silently break when a caller
     spells ``provider`` as ``provider_name``).
  2. Adding a fourth reason in v0.6+ is a one-liner in ``capability.py``
     (extend the ``Literal``) rather than a six-file sweep.

These tests pin the runtime observable behavior. Type-level guarantees
are enforced by ruff's type-aware rules and mypy (not gated here), but
the smoke test at the bottom of this file ensures the TypedDict and the
Literal are importable and carry the exact reasons v0.5 ships with.
"""

from __future__ import annotations

import logging
from typing import cast, get_args

import pytest

from coderouter.routing.capability import (
    CapabilityDegradedPayload,
    CapabilityDegradedReason,
    log_capability_degraded,
)


# ---------------------------------------------------------------------------
# Shape / contract tests.
# ---------------------------------------------------------------------------


def test_literal_enumerates_the_three_v0_5_reasons() -> None:
    """``CapabilityDegradedReason`` must list exactly the 3 v0.5 reasons.

    Adding a new reason is deliberate — this test is the deliberate
    forcing function. When v0.6+ extends the literal, update this test
    at the same time so the retro / CHANGELOG / TypedDict stay in sync.
    """
    assert set(get_args(CapabilityDegradedReason)) == {
        "provider-does-not-support",
        "translation-lossy",
        "non-standard-field",
    }


def test_typeddict_required_keys() -> None:
    """The TypedDict must require exactly the 3 fields v0.5 emits.

    Implementation detail: TypedDict's required keys live in
    ``__required_keys__``; we check via that introspection to ensure the
    contract matches the gate design matrix in retro v0.5.
    """
    assert CapabilityDegradedPayload.__required_keys__ == frozenset(
        {"provider", "dropped", "reason"}
    )
    # And no optional keys — the shape is fixed.
    assert CapabilityDegradedPayload.__optional_keys__ == frozenset()


# ---------------------------------------------------------------------------
# Helper behavior tests.
# ---------------------------------------------------------------------------


def test_helper_emits_record_with_exact_shape(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``log_capability_degraded`` must produce a record with:
       - ``msg == "capability-degraded"``
       - level INFO
       - ``provider`` / ``dropped`` / ``reason`` fields reachable via
         the LogRecord's attributes (what JsonLineFormatter picks up).
    """
    test_logger = logging.getLogger("test.capability_helper")
    caplog.set_level(logging.INFO, logger="test.capability_helper")

    log_capability_degraded(
        test_logger,
        provider="openrouter-gpt-oss-free",
        dropped=["thinking"],
        reason="provider-does-not-support",
    )

    records = [r for r in caplog.records if r.msg == "capability-degraded"]
    assert len(records) == 1
    rec = records[0]
    assert rec.levelno == logging.INFO
    assert getattr(rec, "provider") == "openrouter-gpt-oss-free"
    assert getattr(rec, "dropped") == ["thinking"]
    assert getattr(rec, "reason") == "provider-does-not-support"


@pytest.mark.parametrize(
    "reason,dropped",
    [
        ("provider-does-not-support", ["thinking"]),
        ("translation-lossy", ["cache_control"]),
        ("non-standard-field", ["reasoning"]),
    ],
)
def test_helper_accepts_all_three_v0_5_reasons(
    caplog: pytest.LogCaptureFixture,
    reason: CapabilityDegradedReason,
    dropped: list[str],
) -> None:
    """Smoke test across all three reasons v0.5 ships.

    Mirrors the scenarios in ``scripts/verify_v0_5.sh`` but on mock
    callers. Any of the three should produce a structurally identical
    record.
    """
    test_logger = logging.getLogger("test.capability_helper.all_reasons")
    caplog.set_level(logging.INFO, logger="test.capability_helper.all_reasons")

    log_capability_degraded(
        test_logger, provider="p", dropped=dropped, reason=reason
    )

    rec = next(r for r in caplog.records if r.msg == "capability-degraded")
    assert getattr(rec, "reason") == reason
    assert getattr(rec, "dropped") == dropped


def test_helper_preserves_caller_logger_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Records must be attributed to the caller's logger.

    This is deliberately tested — the v0.5-verify evidence notes that
    the request-side gate (``coderouter.routing.fallback``) and the
    response-side strip (``coderouter.adapters.openai_compat``) both
    emit ``capability-degraded`` lines, and operators distinguish them
    by the ``logger`` field. That distinction must survive the helper.
    """
    fallback_logger = logging.getLogger("coderouter.routing.fallback")
    adapter_logger = logging.getLogger("coderouter.adapters.openai_compat")
    caplog.set_level(logging.INFO, logger="coderouter.routing.fallback")
    caplog.set_level(logging.INFO, logger="coderouter.adapters.openai_compat")

    log_capability_degraded(
        fallback_logger,
        provider="p",
        dropped=["thinking"],
        reason="provider-does-not-support",
    )
    log_capability_degraded(
        adapter_logger,
        provider="p",
        dropped=["reasoning"],
        reason="non-standard-field",
    )

    degraded = [r for r in caplog.records if r.msg == "capability-degraded"]
    names = {r.name for r in degraded}
    assert names == {
        "coderouter.routing.fallback",
        "coderouter.adapters.openai_compat",
    }


def test_helper_does_not_reuse_payload_dict_between_calls(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Each call must yield an independent record.

    Guards against a lazy "build the payload once and mutate" refactor
    that would alias previous emissions.
    """
    test_logger = logging.getLogger("test.capability_helper.independence")
    caplog.set_level(logging.INFO, logger="test.capability_helper.independence")

    log_capability_degraded(
        test_logger,
        provider="a",
        dropped=["thinking"],
        reason="provider-does-not-support",
    )
    log_capability_degraded(
        test_logger,
        provider="b",
        dropped=["cache_control"],
        reason="translation-lossy",
    )

    degraded = [r for r in caplog.records if r.msg == "capability-degraded"]
    assert len(degraded) == 2
    assert [getattr(r, "provider") for r in degraded] == ["a", "b"]
    assert [getattr(r, "reason") for r in degraded] == [
        "provider-does-not-support",
        "translation-lossy",
    ]


# ---------------------------------------------------------------------------
# Static-typing smoke test.
# ---------------------------------------------------------------------------


def test_typeddict_accepts_a_valid_payload_at_runtime() -> None:
    """Round-trip: constructing a ``CapabilityDegradedPayload`` with the
    three required fields succeeds, and each field carries the declared
    type.

    The ``cast`` at the end is intentional — it exercises the Literal
    annotation at runtime. Mypy / ruff would reject an invalid reason
    here at static-check time; at runtime we only assert the dict shape.
    """
    payload: CapabilityDegradedPayload = {
        "provider": "openrouter-gpt-oss-free",
        "dropped": ["reasoning"],
        "reason": cast(CapabilityDegradedReason, "non-standard-field"),
    }
    assert payload["provider"] == "openrouter-gpt-oss-free"
    assert payload["dropped"] == ["reasoning"]
    assert payload["reason"] == "non-standard-field"
