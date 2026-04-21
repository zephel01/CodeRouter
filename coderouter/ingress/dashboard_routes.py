"""Dashboard endpoint — ``GET /dashboard`` (v1.5-D).

Single-page HTML view over the same :class:`MetricsCollector` snapshot
that ``/metrics.json`` exposes. Self-refreshing via vanilla JS + fetch
on a 2-second timer — no websocket, no SSE, no HTML fragment swapping.

Design choices (plan.md §12.3.5 + §12.3.6)
    - **tailwind CDN only**. No React, no htmx, no d3. The original
      design memo suggested htmx; we dropped it because `hx-swap=none`
      + JS listeners ends up the same size as `setInterval` + `fetch`
      with one less CDN request and one less concept to grok.
    - **One file, no separate template dir**. The HTML template is a
      module-level string rendered via ``.format()``. Keeps the route
      wiring trivial (no Jinja2, no StaticFiles mount).
    - **Sparkline is hand-rolled SVG**. 60-sample ring in JS, polyline
      with stroke + gradient area. Same trick as the static mockup.
    - **Dark theme default**. Matches the TUI and avoids flashing
      white on dev monitors.
    - **No per-user state**. Dashboard is stateless-on-server — every
      render reads the same shared MetricsCollector; the ring of
      throughput samples lives client-side in JS.
    - **v1.5-E: configurable display timezone**. The underlying
      ``/metrics.json`` snapshot keeps UTC ISO timestamps (stable wire
      format); conversion to ``Asia/Tokyo`` or any IANA zone happens
      client-side via ``Intl.DateTimeFormat`` when
      ``config.display_timezone`` is set. Unset → UTC.

Why inline JS / CSS
    This dashboard is served from the same process as the API, on
    ``localhost:4040`` by default. External hosting / CSP is explicitly
    out of scope (§12.3.7: "認証は v1.5 スコープ外, reverse proxy で
    挟む前提"). Inlining keeps the single-file shipping story clean.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()


# ---------------------------------------------------------------------------
# Static HTML template.
#
# Single-page document with tailwind (CDN) for styling and a ~120-line
# inline script that fetches /metrics.json every 2s and updates the DOM
# in place. All data-bound elements carry a ``data-bind="<path>"``
# attribute so the updater is a single generic walker instead of 40
# hand-written ``document.getElementById()`` calls.
#
# The template uses ``{{`` / ``}}`` to escape CSS / JS braces through
# Python's ``.format()`` — currently there are no format slots (this
# page takes no server-side variables), so strictly ``.format()`` is a
# no-op. Left in place as an extension point for future server-side
# injection (e.g. embedding the initial snapshot to eliminate the
# first-poll flicker).
# ---------------------------------------------------------------------------


_DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>CodeRouter Dashboard</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <style>
    .spark { width: 100%; height: 60px; }
    .dot { width: .5rem; height: .5rem; border-radius: 9999px; display: inline-block; }
    .tabnum { font-variant-numeric: tabular-nums; }
  </style>
</head>
<body class="bg-slate-950 text-slate-100 min-h-screen font-sans">

  <header class="border-b border-slate-800 px-6 py-3">
    <div class="max-w-7xl mx-auto flex flex-wrap items-center gap-x-6 gap-y-2 text-sm">
      <span class="text-lg font-semibold tracking-tight">CodeRouter</span>
      <span class="text-slate-400">profile: <span data-bind="profile" class="text-slate-100 font-mono">—</span></span>
      <span class="text-slate-400">uptime: <span data-bind="uptime" class="text-slate-100 font-mono tabnum">—</span></span>
      <span class="text-slate-400">requests: <span data-bind="requests_total" class="text-slate-100 font-mono tabnum">0</span></span>
      <span class="text-slate-400">tz: <span data-bind="display_timezone" class="text-slate-100 font-mono">UTC</span></span>
      <span id="health-badge" class="ml-auto inline-flex items-center gap-2 text-slate-400">
        <span class="dot bg-slate-500"></span><span data-bind="health_text">connecting…</span>
      </span>
    </div>
  </header>

  <main class="max-w-7xl mx-auto p-4 md:p-6 grid grid-cols-1 md:grid-cols-2 gap-4">

    <!-- Panel 1: Providers -->
    <section class="bg-slate-900/60 border border-slate-800 rounded-lg p-4">
      <h2 class="text-sm font-semibold uppercase tracking-wider text-slate-400 mb-3">Providers</h2>
      <table class="w-full text-sm tabnum">
        <thead class="text-slate-500">
          <tr class="text-left">
            <th class="font-medium pb-2">provider</th>
            <th class="font-medium pb-2 text-right">att</th>
            <th class="font-medium pb-2 text-right">ok%</th>
            <th class="font-medium pb-2 text-right">failed</th>
            <th class="font-medium pb-2">last error</th>
          </tr>
        </thead>
        <tbody id="providers-body" class="divide-y divide-slate-800">
          <tr><td colspan="5" class="py-3 text-slate-500">no requests seen yet</td></tr>
        </tbody>
      </table>
    </section>

    <!-- Panel 2: Fallback & Gates -->
    <section class="bg-slate-900/60 border border-slate-800 rounded-lg p-4">
      <h2 class="text-sm font-semibold uppercase tracking-wider text-slate-400 mb-3">Fallback &amp; Gates</h2>
      <div class="grid grid-cols-2 gap-3">
        <div class="rounded-md bg-slate-800/50 p-3">
          <div class="text-xs text-slate-400">Fallback rate</div>
          <div class="text-2xl font-semibold tabnum" data-bind="fallback_rate">0.0%</div>
          <div class="text-xs text-slate-500 tabnum" data-bind="fallback_fraction">0 / 0</div>
        </div>
        <div class="rounded-md bg-slate-800/50 p-3">
          <div class="text-xs text-slate-400">Paid-gate blocked</div>
          <div class="text-2xl font-semibold tabnum" data-bind="paid_gate_blocked">0</div>
          <div class="text-xs text-slate-500" data-bind="allow_paid_state">ALLOW_PAID=?</div>
        </div>
        <div class="rounded-md bg-slate-800/50 p-3">
          <div class="text-xs text-slate-400">Capability degraded</div>
          <div class="text-2xl font-semibold tabnum" data-bind="degraded_total">0</div>
          <div class="text-xs text-slate-500" data-bind="degraded_breakdown">—</div>
        </div>
        <div class="rounded-md bg-slate-800/50 p-3">
          <div class="text-xs text-slate-400">Output-filter applied</div>
          <div class="text-2xl font-semibold tabnum" data-bind="filters_total">0</div>
          <div class="text-xs text-slate-500" data-bind="filters_breakdown">—</div>
        </div>
      </div>
    </section>

    <!-- Panel 3: Throughput sparkline -->
    <section class="bg-slate-900/60 border border-slate-800 rounded-lg p-4">
      <h2 class="text-sm font-semibold uppercase tracking-wider text-slate-400 mb-3">Requests / min (last 60 samples)</h2>
      <div class="flex items-baseline gap-4 mb-2">
        <span class="text-3xl font-semibold tabnum text-green-400" data-bind="rate_last">0</span>
        <span class="text-xs text-slate-500 tabnum" data-bind="rate_meta">avg 0 · peak 0</span>
      </div>
      <svg class="spark" viewBox="0 0 300 60" preserveAspectRatio="none" aria-hidden="true">
        <defs>
          <linearGradient id="g" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="#22c55e" stop-opacity="0.35" />
            <stop offset="100%" stop-color="#22c55e" stop-opacity="0" />
          </linearGradient>
        </defs>
        <polygon id="spark-area" fill="url(#g)" points="0,60 300,60" />
        <polyline id="spark-line" fill="none" stroke="#22c55e" stroke-width="2" points="" />
      </svg>
    </section>

    <!-- Panel 4: Recent requests -->
    <section class="bg-slate-900/60 border border-slate-800 rounded-lg p-4">
      <h2 class="text-sm font-semibold uppercase tracking-wider text-slate-400 mb-3">Recent Events</h2>
      <ul id="recent-list" class="text-xs font-mono space-y-1 tabnum">
        <li class="text-slate-500">no events yet</li>
      </ul>
    </section>

  </main>

  <footer class="max-w-7xl mx-auto px-4 md:px-6 pb-8">
    <section class="bg-slate-900/60 border border-slate-800 rounded-lg p-4">
      <h2 class="text-sm font-semibold uppercase tracking-wider text-slate-400 mb-3">Usage Mix</h2>
      <div id="usage-bar" class="flex h-3 rounded-full overflow-hidden bg-slate-800" role="img" aria-label="usage mix"></div>
      <div id="usage-legend" class="flex justify-between text-xs mt-2 text-slate-400 tabnum">
        <span class="text-slate-500">no classified providers yet</span>
      </div>
    </section>
    <p class="text-xs text-slate-500 mt-3">
      v1.5-D · polls <code>/metrics.json</code> every 2s · <a class="underline" href="/metrics">Prometheus</a> · <a class="underline" href="/metrics.json">JSON</a>
    </p>
  </footer>

<script>
(() => {
  "use strict";

  // Poll interval — 2s matches plan.md §12.3.5. Short enough to feel
  // live on a local dev machine, long enough that HTTP overhead is noise.
  const POLL_MS = 2000;
  // Sparkline ring — 60 samples at 2s/sample → 2 minutes of history.
  const SPARK_POINTS = 60;
  const sparkBuffer = [];
  // Previous cumulative requests_total; used to derive a delta-per-poll
  // rate that the sparkline plots. First fetch seeds the value and
  // pushes 0, so the line starts flat and moves up when real traffic
  // arrives (avoids a huge first spike from the "0 → N" step).
  let prevRequestsTotal = null;

  // v1.5-E: timezone formatter cache keyed by IANA zone name. Intl
  // DateTimeFormat construction is O(ms) — cheap, but not free at 2Hz,
  // so we memoize. A malformed zone (e.g. a typo that slipped past the
  // server-side zoneinfo validator) would throw from the constructor;
  // we swallow it here so the page still renders with UTC fallback
  // instead of blanking out.
  const _tzFormatters = new Map();
  const getTzFormatter = (tz) => {
    const key = tz || "UTC";
    if (_tzFormatters.has(key)) return _tzFormatters.get(key);
    let fmt;
    try {
      fmt = new Intl.DateTimeFormat("en-GB", {
        hour: "2-digit", minute: "2-digit", second: "2-digit",
        hour12: false, timeZone: key,
      });
    } catch (_) {
      fmt = null;  // fall back to naive slice in fmtTs
    }
    _tzFormatters.set(key, fmt);
    return fmt;
  };

  // tsUtc is the server-side "YYYY-MM-DDTHH:MM:SS" (no Z suffix — UTC
  // by convention, matches JsonLineFormatter). Treat as UTC and render
  // in the requested zone. If anything goes wrong (empty input, bad
  // date, unsupported zone) we fall back to the raw HH:MM:SS slice so
  // the panel always shows SOMETHING.
  const fmtTs = (tsUtc, tz) => {
    if (!tsUtc) return "";
    const naive = tsUtc.split("T")[1] || tsUtc;
    const fmt = getTzFormatter(tz);
    if (!fmt) return naive;
    const d = new Date(tsUtc + "Z");
    if (isNaN(d.getTime())) return naive;
    try {
      return fmt.format(d);
    } catch (_) {
      return naive;
    }
  };

  const fmtUptime = (s) => {
    s = Math.floor(s || 0);
    if (s < 60) return s + "s";
    if (s < 3600) return Math.floor(s / 60) + "m " + String(s % 60).padStart(2, "0") + "s";
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    return h + "h " + String(m).padStart(2, "0") + "m";
  };

  const fmtBreakdown = (obj) => {
    const entries = Object.entries(obj || {}).sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0);
    return entries.length ? entries.map(([k, v]) => k + ":" + v).join(", ") : "—";
  };

  const setBind = (key, value) => {
    document.querySelectorAll('[data-bind="' + key + '"]').forEach((el) => {
      el.textContent = value;
    });
  };

  const healthFromRate = (rate, total) => {
    if (!total || total <= 0) return ["gray", "idle"];
    if (rate < 5) return ["green", "healthy"];
    if (rate < 20) return ["yellow", "degraded"];
    return ["red", "unhealthy"];
  };

  const healthClasses = {
    green: { dot: "bg-green-500", text: "text-green-400" },
    yellow: { dot: "bg-yellow-500", text: "text-yellow-400" },
    red: { dot: "bg-red-500", text: "text-red-400" },
    gray: { dot: "bg-slate-500", text: "text-slate-400" },
  };

  const rowHealth = (attempts, ok, failedMid) => {
    if (!attempts) return "gray";
    if (failedMid > 0) return "red";
    const r = ok / attempts;
    if (r >= 0.95) return "green";
    if (r >= 0.80) return "yellow";
    return "red";
  };

  const renderHealthBadge = (token, label) => {
    const badge = document.getElementById("health-badge");
    badge.className = "ml-auto inline-flex items-center gap-2 " + healthClasses[token].text;
    const dot = badge.querySelector(".dot");
    dot.className = "dot " + healthClasses[token].dot;
    setBind("health_text", label);
  };

  const renderProviders = (snap) => {
    const providers = (snap.providers || []).slice().sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
    const tbody = document.getElementById("providers-body");
    if (!providers.length) {
      tbody.innerHTML = '<tr><td colspan="5" class="py-3 text-slate-500">no requests seen yet</td></tr>';
      return;
    }
    tbody.innerHTML = providers.map((p) => {
      const attempts = p.attempts || 0;
      const outcomes = p.outcomes || {};
      const ok = outcomes.ok || 0;
      const failed = (outcomes.failed || 0) + (outcomes.failed_midstream || 0);
      const okPct = attempts > 0 ? Math.round(ok * 100 / attempts) : 100;
      const health = rowHealth(attempts, ok, outcomes.failed_midstream || 0);
      const dotCls = healthClasses[health].dot;
      const pctCls = healthClasses[health].text;
      const lastErr = p.last_error
        ? (p.last_error.status ? p.last_error.status + " " : "") + (p.last_error.error || "")
        : "—";
      return (
        '<tr>' +
        '<td class="py-2"><span class="dot ' + dotCls + ' mr-2"></span>' + escapeHTML(p.name) + '</td>' +
        '<td class="text-right">' + attempts + '</td>' +
        '<td class="text-right ' + pctCls + '">' + okPct + '%</td>' +
        '<td class="text-right">' + failed + '</td>' +
        '<td class="text-slate-400 truncate max-w-[12rem]">' + escapeHTML(lastErr) + '</td>' +
        '</tr>'
      );
    }).join("");
  };

  const renderGates = (snap) => {
    const counters = snap.counters || {};
    const total = counters.requests_total || 0;
    let totalFailed = 0;
    for (const v of Object.values(counters.provider_outcomes || {})) {
      totalFailed += (v.failed || 0) + (v.failed_midstream || 0);
    }
    const rate = total > 0 ? (totalFailed * 100 / total) : 0;
    setBind("fallback_rate", rate.toFixed(1) + "%");
    setBind("fallback_fraction", totalFailed + " / " + total);
    setBind("paid_gate_blocked", counters.chain_paid_gate_blocked_total || 0);
    const cfg = snap.config || {};
    setBind("allow_paid_state", "ALLOW_PAID=" + (cfg.allow_paid ? "true" : "false"));
    const degraded = counters.capability_degraded || {};
    setBind("degraded_total", Object.values(degraded).reduce((a, b) => a + b, 0));
    setBind("degraded_breakdown", fmtBreakdown(degraded));
    const filters = counters.output_filter_applied || {};
    setBind("filters_total", Object.values(filters).reduce((a, b) => a + b, 0));
    setBind("filters_breakdown", fmtBreakdown(filters));

    // Top-line health badge — driven by fallback rate + total.
    const [token, label] = healthFromRate(rate, total);
    renderHealthBadge(token, label);
  };

  const renderSparkline = (snap) => {
    const total = (snap.counters || {}).requests_total || 0;
    const delta = prevRequestsTotal === null ? 0 : Math.max(0, total - prevRequestsTotal);
    prevRequestsTotal = total;
    sparkBuffer.push(delta);
    while (sparkBuffer.length > SPARK_POINTS) sparkBuffer.shift();

    const peak = Math.max(1, ...sparkBuffer);
    const avg = sparkBuffer.reduce((a, b) => a + b, 0) / sparkBuffer.length;
    setBind("rate_last", delta);
    setBind("rate_meta", "avg " + avg.toFixed(1) + " · peak " + peak);

    // Map samples to SVG coordinates. viewBox is 300x60; 0 is at the
    // top in SVG, so we invert the y-axis. When only a few samples
    // exist we still draw — left-justified so the movement is visible.
    const n = sparkBuffer.length;
    const dx = n > 1 ? 300 / (SPARK_POINTS - 1) : 0;
    const pts = sparkBuffer.map((v, i) => {
      const x = i * dx;
      const y = 60 - (v / peak) * 55;
      return x.toFixed(1) + "," + y.toFixed(1);
    });
    document.getElementById("spark-line").setAttribute("points", pts.join(" "));
    if (pts.length) {
      const areaPts = ["0,60", ...pts, ((n - 1) * dx).toFixed(1) + ",60"];
      document.getElementById("spark-area").setAttribute("points", areaPts.join(" "));
    }
  };

  const renderRecent = (snap) => {
    const list = document.getElementById("recent-list");
    const recent = (snap.recent || []).slice().reverse(); // newest first
    if (!recent.length) {
      list.innerHTML = '<li class="text-slate-500">no events yet</li>';
      return;
    }
    const tz = (snap.config || {}).display_timezone || "UTC";
    list.innerHTML = recent.slice(0, 15).map((r) => {
      const ts = fmtTs(r.ts || "", tz);
      const isFailure = (r.event || "").startsWith("provider-failed");
      const rowCls = isFailure ? "bg-red-950/40 rounded px-1 " : "";
      const statusText = (() => {
        if (r.event === "provider-ok") return '<span class="text-green-400">ok</span>';
        if (r.event === "try-provider") return '<span class="text-slate-400">try</span>';
        if (isFailure) return '<span class="text-red-400">FAIL' + (r.status ? " (" + r.status + ")" : "") + '</span>';
        return '<span class="text-slate-400">' + escapeHTML(r.event || "") + '</span>';
      })();
      return (
        '<li class="' + rowCls + 'grid grid-cols-[auto_auto_1fr_auto] gap-x-3 items-center">' +
        '<span class="text-slate-500">' + escapeHTML(ts) + '</span>' +
        '<span class="text-slate-300">' + escapeHTML(r.event || "") + '</span>' +
        '<span>' + escapeHTML(r.provider || "") + '</span>' +
        statusText +
        '</li>'
      );
    }).join("");
  };

  const renderUsageMix = (snap) => {
    const cfg = snap.config || {};
    const byName = Object.fromEntries((cfg.providers || []).map((p) => [p.name, p]));
    const counts = (snap.counters || {}).provider_attempts || {};
    let local = 0, free = 0, paid = 0;
    for (const [name, n] of Object.entries(counts)) {
      const p = byName[name];
      if (!p) continue;
      if (p.paid) paid += n;
      else if ((p.base_url || "").includes("localhost") || (p.base_url || "").includes("127.0.0.1")) local += n;
      else free += n;
    }
    const total = local + free + paid;
    const bar = document.getElementById("usage-bar");
    const legend = document.getElementById("usage-legend");
    if (total === 0) {
      bar.innerHTML = "";
      legend.innerHTML = '<span class="text-slate-500">no classified providers yet</span>';
      return;
    }
    const pct = (x) => (x * 100 / total).toFixed(0);
    bar.innerHTML =
      '<div class="bg-green-500" style="width: ' + pct(local) + '%"></div>' +
      '<div class="bg-sky-500" style="width: ' + pct(free) + '%"></div>' +
      '<div class="bg-amber-500" style="width: ' + pct(paid) + '%"></div>';
    legend.innerHTML =
      '<span><span class="dot bg-green-500 mr-1"></span>local ' + pct(local) + '% (' + local + ')</span>' +
      '<span><span class="dot bg-sky-500 mr-1"></span>free ' + pct(free) + '% (' + free + ')</span>' +
      '<span><span class="dot bg-amber-500 mr-1"></span>paid ' + pct(paid) + '% (' + paid + ')</span>';
  };

  const escapeHTML = (s) => String(s).replace(/[&<>"']/g, (c) => (
    {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]
  ));

  const renderSnapshot = (snap) => {
    const startup = snap.startup || {};
    const cfg = snap.config || {};
    const profile = startup.default_profile || cfg.default_profile || "—";
    setBind("profile", profile);
    setBind("uptime", fmtUptime(snap.uptime_s || 0));
    setBind("requests_total", (snap.counters || {}).requests_total || 0);
    // v1.5-E: surface the active display TZ in the header so operators
    // can tell at a glance "I'm looking at events in Asia/Tokyo" vs UTC.
    setBind("display_timezone", cfg.display_timezone || "UTC");

    renderProviders(snap);
    renderGates(snap);
    renderSparkline(snap);
    renderRecent(snap);
    renderUsageMix(snap);
  };

  const renderError = (msg) => {
    renderHealthBadge("red", "error: " + msg);
  };

  const poll = async () => {
    try {
      const resp = await fetch("/metrics.json", {cache: "no-store"});
      if (!resp.ok) {
        renderError("HTTP " + resp.status);
        return;
      }
      const snap = await resp.json();
      renderSnapshot(snap);
    } catch (e) {
      renderError(String(e && e.message || e));
    }
  };

  poll();
  setInterval(poll, POLL_MS);
})();
</script>

</body>
</html>
"""


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard() -> HTMLResponse:
    """Serve the single-page dashboard HTML.

    The page is entirely static — all data comes from polling
    ``/metrics.json`` on a 2s timer (see the inline script). We return
    an :class:`HTMLResponse` directly (not a ``FileResponse``) because
    the template lives in this module as a string, keeping the shipping
    unit a single Python file with no template dir to configure.
    """
    return HTMLResponse(content=_DASHBOARD_HTML)
