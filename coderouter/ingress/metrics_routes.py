"""Metrics endpoint — ``GET /metrics.json`` (v1.5-A).

The endpoint returns a JSON-safe snapshot from the process-global
:class:`coderouter.metrics.MetricsCollector`. It is mounted at the
root (no ``/v1`` prefix) because the metrics payload is not part of
the OpenAI / Anthropic API surface and because Prometheus-shaped
exporters conventionally live at ``/metrics`` on the root.

v1.5 scope (plan.md §12.3.4)
    - ``GET /metrics.json``   — JSON shape, internal / dashboard consumer.
    - ``GET /metrics``        — v1.5-B: Prometheus text exposition
      format, content-type ``text/plain; version=0.0.4; charset=utf-8``.
      Same collector singleton as ``/metrics.json``.
    - ``GET /dashboard``      — HTML one-pager. Lands in v1.5-D.

The JSON handler merges a little context from ``app.state`` (namely the
resolved config's allow_paid + paid-vs-free provider classification)
so the dashboard can compute the "local / free / paid" usage-mix
without each UI re-reading providers.yaml. The Prometheus handler
stays strict-spec — only the metrics payload, no extra stanzas — so
``promtool check metrics`` round-trips cleanly.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from coderouter.metrics import format_prometheus, get_collector

router = APIRouter()

# Prometheus text exposition v0.0.4 content type. Prom parsers will fall
# back to plain ``text/plain`` if missing, but being explicit pins the
# negotiated media type when a Grafana Agent or OTel collector probes us.
_PROM_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


@router.get("/metrics.json")
async def metrics_json(request: Request) -> dict[str, Any]:
    """Return the current MetricsCollector snapshot as JSON.

    Merges in a ``config`` stanza sourced from ``app.state.config`` —
    this is static for the lifetime of the process (providers.yaml is
    loaded once at startup) so it's cheap to re-emit per request. The
    dashboard uses it to classify providers into local / free / paid
    for the usage-mix bar without a second endpoint round-trip.
    """
    snapshot = get_collector().snapshot()

    config = getattr(request.app.state, "config", None)
    if config is not None:
        snapshot["config"] = {
            "default_profile": config.default_profile,
            "allow_paid": config.allow_paid,
            # v1.5-E: display-only TZ hint for /dashboard + coderouter stats.
            # Stays ``None`` when unset so clients can keep their UTC fallback
            # without probing for a string value.
            "display_timezone": config.display_timezone,
            "providers": [
                {
                    "name": p.name,
                    "kind": p.kind,
                    "paid": p.paid,
                    # ``HttpUrl`` is not JSON-serializable directly in Pydantic v2;
                    # the cast also makes the shape stable if Pydantic switches types.
                    "base_url": str(p.base_url),
                }
                for p in config.providers
            ],
            "profiles": [
                {"name": pr.name, "providers": list(pr.providers)}
                for pr in config.profiles
            ],
        }
    return snapshot


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics_prometheus() -> PlainTextResponse:
    """Prometheus text exposition format (v1.5-B).

    Convention-compliant endpoint path for Prometheus scrapers. Returns
    the same counters the JSON snapshot surfaces, rendered per
    https://prometheus.io/docs/instrumenting/exposition_formats/ .
    Sits alongside :func:`metrics_json` (not a replacement) — JSON is
    for internal UI, Prometheus is for external time-series DBs.
    """
    body = format_prometheus(get_collector().snapshot())
    return PlainTextResponse(content=body, media_type=_PROM_CONTENT_TYPE)
