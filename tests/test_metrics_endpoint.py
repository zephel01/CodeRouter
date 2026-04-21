"""Integration tests for ``GET /metrics.json`` (v1.5-A).

Spins up the FastAPI app with a stubbed ``load_config`` (same pattern
as ``test_ingress_profile.py``), fires log events that the runtime
would normally emit, and asserts that ``/metrics.json`` returns the
expected shape.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from coderouter.config.schemas import CodeRouterConfig, FallbackChain, ProviderConfig
from coderouter.ingress.app import create_app
from coderouter.logging import get_logger
from coderouter.metrics import uninstall_collector


@pytest.fixture
def two_provider_config() -> CodeRouterConfig:
    return CodeRouterConfig(
        allow_paid=False,
        default_profile="default",
        providers=[
            ProviderConfig(
                name="local",
                base_url="http://localhost:8080/v1",
                model="qwen-coder",
                paid=False,
            ),
            ProviderConfig(
                name="paid-cloud",
                base_url="https://openrouter.ai/api/v1",
                model="anthropic/claude-sonnet-4",
                api_key_env="OPENROUTER_API_KEY",
                paid=True,
            ),
        ],
        profiles=[
            FallbackChain(name="default", providers=["local", "paid-cloud"]),
        ],
    )


@pytest.fixture
def client(
    two_provider_config: CodeRouterConfig, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """FastAPI app with config stubbed out and a fresh metrics collector."""
    monkeypatch.setattr(
        "coderouter.ingress.app.load_config",
        lambda path=None: two_provider_config,
    )
    # Clean slate so other tests' counters don't leak into our assertions.
    uninstall_collector()
    app = create_app()
    try:
        with TestClient(app) as tc:
            yield tc
    finally:
        uninstall_collector()


def test_metrics_json_initial_shape(client: TestClient) -> None:
    """Fresh install returns the expected top-level keys with zeroed counters."""
    resp = client.get("/metrics.json")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Top-level keys
    assert set(body).issuperset(
        {"uptime_s", "started_at", "startup", "counters", "providers", "recent", "config"}
    )

    # Counters start at zero (except provider_attempts / outcomes which
    # are empty dicts — same idea).
    counters = body["counters"]
    assert counters["requests_total"] == 0
    assert counters["chain_paid_gate_blocked_total"] == 0
    assert counters["provider_attempts"] == {}
    assert counters["provider_outcomes"] == {}


def test_metrics_json_surfaces_config_classification(client: TestClient) -> None:
    """``config`` stanza exposes the paid/free classification from providers.yaml."""
    body = client.get("/metrics.json").json()
    config = body["config"]

    assert config["default_profile"] == "default"
    assert config["allow_paid"] is False

    providers = {p["name"]: p for p in config["providers"]}
    assert providers["local"]["paid"] is False
    assert providers["paid-cloud"]["paid"] is True
    # base_url is serialized as str (not a pydantic URL object)
    assert isinstance(providers["local"]["base_url"], str)


def test_metrics_json_exposes_display_timezone_none_by_default(
    client: TestClient,
) -> None:
    """v1.5-E: ``display_timezone`` key is present and ``None`` by default.

    Presence matters — the dashboard's JS does a ``config.display_timezone
    || "UTC"`` fallback and relies on the key existing so the fallback
    operator fires.
    """
    body = client.get("/metrics.json").json()
    assert "display_timezone" in body["config"]
    assert body["config"]["display_timezone"] is None


def test_metrics_json_exposes_display_timezone_when_set(
    two_provider_config: CodeRouterConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v1.5-E: a configured ``display_timezone`` surfaces verbatim in the snapshot.

    The value is never transformed server-side — the dashboard and
    ``coderouter stats`` both expect an IANA zone name to feed into
    ``Intl.DateTimeFormat`` / ``zoneinfo.ZoneInfo`` respectively.
    """
    from coderouter.metrics import uninstall_collector

    # Rebuild the two-provider config with display_timezone set. We can't
    # mutate a frozen pydantic model, so construct a new one from its dump.
    patched = two_provider_config.model_copy(update={"display_timezone": "Asia/Tokyo"})
    monkeypatch.setattr(
        "coderouter.ingress.app.load_config", lambda path=None: patched
    )
    uninstall_collector()
    app = create_app()
    try:
        with TestClient(app) as tc:
            body = tc.get("/metrics.json").json()
            assert body["config"]["display_timezone"] == "Asia/Tokyo"
    finally:
        uninstall_collector()


def test_metrics_json_reflects_fired_events(client: TestClient) -> None:
    """Firing the same log sequence the runtime uses bumps the counters."""
    logger = get_logger("coderouter.routing.fallback")
    logger.info("try-provider", extra={"provider": "local", "stream": False})
    logger.info("provider-ok", extra={"provider": "local", "stream": False})
    logger.info("try-provider", extra={"provider": "local", "stream": False})
    logger.warning(
        "provider-failed",
        extra={
            "provider": "local",
            "status": 429,
            "retryable": True,
            "error": "rate_limit",
        },
    )

    body = client.get("/metrics.json").json()
    counters = body["counters"]
    assert counters["requests_total"] == 2
    assert counters["provider_attempts"]["local"] == 2
    assert counters["provider_outcomes"]["local"] == {"ok": 1, "failed": 1}

    # providers[] rows include last_error for the failed provider
    rows = {p["name"]: p for p in body["providers"]}
    assert rows["local"]["attempts"] == 2
    assert rows["local"]["last_error"]["status"] == 429
    assert rows["local"]["last_error"]["error"] == "rate_limit"


def test_metrics_json_recent_ring_shape(client: TestClient) -> None:
    """Ring buffer carries flat, whitelisted fields only."""
    logger = get_logger("coderouter.routing.fallback")
    logger.info("try-provider", extra={"provider": "local", "stream": True})

    body = client.get("/metrics.json").json()
    recent = body["recent"]
    assert len(recent) == 1
    entry = recent[0]
    # Only whitelisted keys + ``ts`` + ``event``
    assert set(entry).issubset({"ts", "event", "provider", "stream", "status", "retryable"})
    assert entry["event"] == "try-provider"
    assert entry["provider"] == "local"
    assert entry["stream"] is True


# ---------------------------------------------------------------------------
# v1.5-B: Prometheus exposition endpoint
# ---------------------------------------------------------------------------


def test_metrics_prom_content_type(client: TestClient) -> None:
    """``GET /metrics`` returns the Prometheus text-exposition content type.

    Prometheus / Grafana Agent / OTel collector scrapers don't require
    this specifically, but pinning version=0.0.4 matches the format we
    hand-roll and prevents ambiguity if a scraper is strict.
    """
    resp = client.get("/metrics")
    assert resp.status_code == 200, resp.text
    # FastAPI / Starlette pass the media_type through verbatim but may
    # append its own charset — we check the prefix.
    assert resp.headers["content-type"].startswith("text/plain")
    assert "version=0.0.4" in resp.headers["content-type"]


def test_metrics_prom_reflects_fired_events(client: TestClient) -> None:
    """The same log flow ``/metrics.json`` sees also lands in Prometheus output."""
    logger = get_logger("coderouter.routing.fallback")
    logger.info("try-provider", extra={"provider": "local", "stream": False})
    logger.info("provider-ok", extra={"provider": "local", "stream": False})

    body = client.get("/metrics").text
    # Scalar counter value
    assert "coderouter_requests_total 1" in body
    # Labeled counter values — sorted alphabetically, so the order is stable
    assert 'coderouter_provider_attempts_total{provider="local"} 1' in body
    assert (
        'coderouter_provider_outcomes_total{provider="local",outcome="ok"} 1' in body
    )


def test_metrics_prom_has_help_and_type_before_samples(client: TestClient) -> None:
    """Spec-mandated ordering: HELP / TYPE precede the sample line for each metric.

    Hand-rolled formatter regression guard — if someone reorders the
    helpers, scraping tools will surface "missing HELP" warnings.
    """
    body = client.get("/metrics").text
    lines = body.splitlines()

    # Find the first sample line for requests_total and confirm HELP/TYPE
    # appeared earlier.
    for idx, line in enumerate(lines):
        if line.startswith("coderouter_requests_total "):
            earlier = lines[:idx]
            assert any(l.startswith("# HELP coderouter_requests_total ") for l in earlier)
            assert any(l.startswith("# TYPE coderouter_requests_total ") for l in earlier)
            break
    else:  # pragma: no cover - the fixture should always produce this line
        raise AssertionError("requests_total sample line not found")
