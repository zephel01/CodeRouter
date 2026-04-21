"""CodeRouter metrics collection (v1.5-A).

The metrics layer taps the existing structured-logging stream rather than
adding new instrumentation hooks throughout the routing/adapter code.
Rationale (plan.md §12.3.1): every metric the v1.5 dashboard needs is
already in a ``capability-degraded`` / ``provider-ok`` / ``try-provider``
/ ``output-filter-applied`` / ``chain-paid-gate-blocked`` / ``skip-paid-
provider`` / ``provider-failed`` record — so wiring a
``logging.Handler`` subclass onto the root logger gives us lossless
collection with zero risk of regression.

Public surface
    :class:`MetricsCollector`
        ``logging.Handler`` subclass that maintains in-memory counters,
        last-error snapshots per provider, and a ring buffer of recent
        events. ``snapshot()`` returns a JSON-safe dict consumed by the
        ``/metrics.json`` endpoint.

    :func:`get_collector` / :func:`install_collector`
        Module-level singleton accessors. The ingress ``create_app``
        lifespan calls ``install_collector()`` at startup; ``/metrics.json``
        and tests read via ``get_collector()``. Idempotent.
"""

from coderouter.metrics.collector import (
    MetricsCollector,
    get_collector,
    install_collector,
    uninstall_collector,
)
from coderouter.metrics.prometheus import format_prometheus

__all__ = [
    "MetricsCollector",
    "format_prometheus",
    "get_collector",
    "install_collector",
    "uninstall_collector",
]
