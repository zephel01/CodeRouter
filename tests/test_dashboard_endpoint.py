"""Integration tests for ``GET /dashboard`` (v1.5-D).

The dashboard itself is almost entirely inline HTML + vanilla JS — the
JS isn't executed in these tests (no jsdom), so the assertions focus
on the server contract: right status, right content-type, and the
presence of the ``data-bind`` hooks that the JS updater walks. Any
regression that renames a hook will break this suite and also break
the live page, which is the whole point.

Shares the stubbed-``load_config`` scaffolding pattern with
``test_metrics_endpoint.py``.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from coderouter.config.schemas import CodeRouterConfig, FallbackChain, ProviderConfig
from coderouter.ingress.app import create_app
from coderouter.metrics import uninstall_collector


@pytest.fixture
def config() -> CodeRouterConfig:
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
        ],
        profiles=[FallbackChain(name="default", providers=["local"])],
    )


@pytest.fixture
def client(
    config: CodeRouterConfig, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """App with a stubbed load_config and a fresh metrics collector."""
    monkeypatch.setattr(
        "coderouter.ingress.app.load_config", lambda path=None: config
    )
    uninstall_collector()
    app = create_app()
    try:
        with TestClient(app) as tc:
            yield tc
    finally:
        uninstall_collector()


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


def test_dashboard_returns_200_html(client: TestClient) -> None:
    """Bare ``/dashboard`` GET must return HTML with a 200 status."""
    resp = client.get("/dashboard")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/html")


def test_dashboard_starts_with_doctype(client: TestClient) -> None:
    """Sanity: the body is a real HTML document, not a JSON leak."""
    body = client.get("/dashboard").text
    assert body.lstrip().lower().startswith("<!doctype html>")
    assert "</html>" in body


# ---------------------------------------------------------------------------
# Expected DOM hooks — ``data-bind`` is the generic updater contract.
# If any of these attributes disappear, the inline JS can't populate
# the cell and the dashboard silently shows "—" forever.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bind_key",
    [
        "profile",
        "uptime",
        "requests_total",
        "health_text",
        "fallback_rate",
        "fallback_fraction",
        "paid_gate_blocked",
        "allow_paid_state",
        "degraded_total",
        "degraded_breakdown",
        "filters_total",
        "filters_breakdown",
        "rate_last",
        "rate_meta",
        # v1.5-E: TZ label in header (e.g. "Asia/Tokyo" / "UTC").
        "display_timezone",
    ],
)
def test_dashboard_contains_required_data_bind_hooks(
    client: TestClient, bind_key: str
) -> None:
    """Every bind key the JS targets must exist in the initial HTML.

    Parameterized so a missing hook surfaces with a precise test name
    in CI — easier to triage than one monolithic ``assert X in body``.
    """
    body = client.get("/dashboard").text
    assert f'data-bind="{bind_key}"' in body, f"missing data-bind={bind_key!r}"


def test_dashboard_has_all_panels(client: TestClient) -> None:
    """The 4-panel layout + usage-mix footer must all be present.

    We pin the panel headings rather than CSS class names — class
    churn is routine, but a missing panel title would be a real UX
    regression.
    """
    body = client.get("/dashboard").text
    for title in (
        "Providers",
        "Fallback &amp; Gates",
        "Requests / min",
        "Recent Events",
        "Usage Mix",
    ):
        assert title in body, f"panel {title!r} not rendered"


def test_dashboard_has_sparkline_svg_scaffolding(client: TestClient) -> None:
    """Sparkline relies on two SVG elements — ``spark-line`` + ``spark-area``.

    The JS writer sets ``points=...`` on both; if either ID goes
    missing, the chart stays a flat baseline with no traceable error.
    """
    body = client.get("/dashboard").text
    assert 'id="spark-line"' in body
    assert 'id="spark-area"' in body
    assert "<svg" in body


def test_dashboard_has_providers_tbody_hook(client: TestClient) -> None:
    """Provider rows are populated into ``#providers-body``.

    The initial HTML ships a placeholder row so operators see
    "no requests seen yet" before the first poll completes.
    """
    body = client.get("/dashboard").text
    assert 'id="providers-body"' in body
    assert "no requests seen yet" in body


def test_dashboard_has_recent_list_hook(client: TestClient) -> None:
    """Recent events list is populated into ``#recent-list``."""
    body = client.get("/dashboard").text
    assert 'id="recent-list"' in body
    assert "no events yet" in body


def test_dashboard_has_usage_mix_hooks(client: TestClient) -> None:
    """Usage-mix footer needs both the bar container and the legend row."""
    body = client.get("/dashboard").text
    assert 'id="usage-bar"' in body
    assert 'id="usage-legend"' in body


# ---------------------------------------------------------------------------
# Polling contract
# ---------------------------------------------------------------------------


def test_dashboard_inline_script_polls_metrics_json(client: TestClient) -> None:
    """The page's JS must fetch ``/metrics.json`` on a ~2s interval.

    A regression that renames the endpoint or flips to SSE would
    break live updates with no visible error — this assertion pins the
    contract with the metrics route.
    """
    body = client.get("/dashboard").text
    assert 'fetch("/metrics.json"' in body
    assert "setInterval" in body


def test_dashboard_links_to_metrics_surface(client: TestClient) -> None:
    """Footer links to the raw JSON and Prometheus endpoints.

    Developer convenience — opening the dashboard and spotting an
    anomaly, the fastest drill-down is clicking through to the raw
    snapshot.
    """
    body = client.get("/dashboard").text
    assert 'href="/metrics.json"' in body
    assert 'href="/metrics"' in body


# ---------------------------------------------------------------------------
# v1.5-E: display_timezone wiring
# ---------------------------------------------------------------------------


def test_dashboard_has_tz_formatter_helpers(client: TestClient) -> None:
    """The JS carries the Intl-based formatter and a memoization cache.

    We pin the symbol names (``getTzFormatter`` / ``fmtTs``) because the
    TZ conversion is the only thing that transforms ring-buffer
    timestamps on render — if either helper is renamed or removed, the
    recent-events list would silently fall back to naive UTC slicing
    (the fallback we deliberately keep for robustness). Testing the
    symbols keeps that path from flipping from "defensive" to "the only
    path". ``Intl.DateTimeFormat`` is asserted because it's the one
    dependency the whole feature rides on.
    """
    body = client.get("/dashboard").text
    assert "Intl.DateTimeFormat" in body
    assert "getTzFormatter" in body
    assert "fmtTs" in body


def test_dashboard_reads_tz_from_snapshot_config(client: TestClient) -> None:
    """``renderRecent`` must read ``config.display_timezone`` from the snapshot.

    The string literal is what pins the wire contract to the metrics
    route — both sides have to agree on the key name for the feature
    to work end-to-end.
    """
    body = client.get("/dashboard").text
    assert "display_timezone" in body
