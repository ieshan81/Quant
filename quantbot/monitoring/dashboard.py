"""Flask monitoring dashboard (Sprint 8) — port 5000 by default, JSON + HTML UI.

Import policy: this module must not import ``main_worker``, ``training.*``, or other
heavy trading/sentiment stacks. Use only Flask, loguru, ``config``, and lazy imports
inside ``create_app`` from ``data.data_store`` + ``monitoring.dashboard_data``.
"""

from __future__ import annotations

import os
import sys

# Quant package root (parent of `monitoring/`) must be first so `data` resolves to
# `quantbot/data/` (the package), not a stray `./data` directory from cwd (Railway).
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.makedirs(os.path.join(_ROOT, "data"), exist_ok=True)

import json
from typing import Any

from flask import Flask, Response, render_template_string
from loguru import logger

import config

_REFRESH_SEC = 30

_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <meta http-equiv="refresh" content="{{ refresh_sec }}"/>
  <title>QuantBot — Monitoring</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
  <style>
    :root { font-family: system-ui, sans-serif; background: #0f1419; color: #e7ecf3; }
    body { margin: 0; padding: 1rem 1.25rem 2rem; max-width: 1200px; margin-inline: auto; }
    h1 { font-size: 1.25rem; font-weight: 600; }
    .grid { display: grid; gap: 1rem; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); }
    .card {
      background: #1a2332; border-radius: 8px; padding: 1rem;
      border: 1px solid #2a3545;
    }
    .card h2 { margin: 0 0 0.5rem; font-size: 0.85rem; color: #8b9bb4; font-weight: 600; }
    .big { font-size: 1.5rem; font-weight: 700; }
    .pos { color: #3ecf8e; }
    .neg { color: #f56565; }
    table { width: 100%; border-collapse: collapse; font-size: 0.8rem; }
    th, td { text-align: left; padding: 0.35rem 0.5rem; border-bottom: 1px solid #2a3545; }
    th { color: #8b9bb4; font-weight: 600; }
    .muted { color: #8b9bb4; font-size: 0.75rem; margin-top: 0.75rem; }
    .chart-wrap { height: 220px; margin-top: 0.5rem; }
    a { color: #63b3ed; }
  </style>
</head>
<body>
  <h1>QuantBot monitoring</h1>
  <p class="muted">Auto-refresh every {{ refresh_sec }}s (meta refresh + poll). DB: {{ db }}</p>
  <div class="grid">
    <div class="card"><h2>Live P&amp;L vs start</h2>
      <div class="big {{ pnl_class }}">{{ pnl_str }}</div>
    </div>
    <div class="card"><h2>Total equity</h2>
      <div class="big">{{ eq_str }}</div>
    </div>
    <div class="card"><h2>Mode</h2><div class="big">{{ mode_str }}</div></div>
    <div class="card"><h2>Deployed</h2>
      <div class="big">{{ dep_str }}</div>
    </div>
  </div>
  <div class="card" style="margin-top:1rem;">
    <h2>Equity (recent snapshots)</h2>
    <div class="chart-wrap"><canvas id="eqChart"></canvas></div>
  </div>
  <div class="grid" style="margin-top:1rem;">
    <div class="card">
      <h2>Open positions (net from fills)</h2>
      {% if positions %}
      <table><thead><tr><th>Class</th><th>Symbol</th><th>Net qty</th></tr></thead><tbody>
        {% for p in positions %}
        <tr><td>{{ p.asset_class }}</td><td>{{ p.symbol }}</td><td>{{ p.net_qty_fmt }}</td></tr>
        {% endfor %}
      </tbody></table>
      {% else %}<p class="muted">No open positions from trade history.</p>{% endif %}
    </div>
    <div class="card">
      <h2>Recent trades</h2>
      {% if trades %}
      <table><thead><tr><th>Time</th><th>Sym</th><th>Side</th><th>Qty</th><th>Status</th></tr></thead><tbody>
        {% for t in trades %}
        <tr>
          <td>{{ t.created_at }}</td>
          <td>{{ t.symbol }}</td>
          <td>{{ t.side }}</td>
          <td>{{ t.qty_fmt }}</td>
          <td>{{ t.status }}{% if t.reason_code %} ({{ t.reason_code }}){% endif %}</td>
        </tr>
        {% endfor %}
      </tbody></table>
      {% else %}<p class="muted">No trades yet.</p>{% endif %}
    </div>
  </div>
  <div class="card" style="margin-top:1rem;">
    <h2>Signal states (recent)</h2>
    {% if signals %}
    <table><thead><tr><th>Time</th><th>Sym</th><th>Signal</th><th>Dir</th><th>Score</th></tr></thead><tbody>
      {% for s in signals %}
      <tr>
        <td>{{ s.created_at }}</td>
        <td>{{ s.symbol }}</td>
        <td>{{ s.signal_name }}</td>
        <td>{{ s.direction }}</td>
        <td>{{ s.score_fmt }}</td>
      </tr>
      {% endfor %}
    </tbody></table>
    {% else %}<p class="muted">No signals logged.</p>{% endif %}
  </div>
  <p class="muted">JSON: <a href="/api/dashboard">/api/dashboard</a></p>
  <script id="dash-payload" type="application/json">{{ chart_data|tojson }}</script>
  <script>
    const REFRESH_MS = {{ refresh_sec }} * 1000;
    let chart;
    function readPayload() {
      const el = document.getElementById("dash-payload");
      return JSON.parse(el.textContent || "{}");
    }
    function buildChart(series) {
      const labels = series.map((r) => r.snapshot_at || "");
      const data = series.map((r) => Number(r.equity_total) || 0);
      const ctx = document.getElementById("eqChart");
      if (chart) chart.destroy();
      chart = new Chart(ctx, {
        type: "line",
        data: {
          labels,
          datasets: [{
            label: "Equity",
            data,
            borderColor: "#63b3ed",
            backgroundColor: "rgba(99,179,237,0.15)",
            fill: true,
            tension: 0.2,
          }],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: {
            x: { ticks: { maxTicksLimit: 8, color: "#8b9bb4" } },
            y: { ticks: { color: "#8b9bb4" } },
          },
        },
      });
    }
    buildChart(readPayload().equity_series || []);
    async function poll() {
      try {
        const r = await fetch("/api/dashboard", { cache: "no-store" });
        const j = await r.json();
        const el = document.getElementById("dash-payload");
        el.textContent = JSON.stringify(j);
        buildChart(j.equity_series || []);
      } catch (e) { console.warn(e); }
    }
    setInterval(poll, REFRESH_MS);
  </script>
</body>
</html>
"""


def _fmt_positions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["net_qty_fmt"] = f"{float(d['net_qty']):.6f}"
        except (TypeError, ValueError, KeyError):
            d["net_qty_fmt"] = str(d.get("net_qty", ""))
        out.append(d)
    return out


def _fmt_trades(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["qty_fmt"] = f"{float(d['quantity']):.4f}"
        except (TypeError, ValueError, KeyError):
            d["qty_fmt"] = str(d.get("quantity", ""))
        out.append(d)
    return out


def _fmt_signals(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        d = dict(r)
        cs = d.get("combined_score")
        if cs is None:
            d["score_fmt"] = "—"
        else:
            try:
                d["score_fmt"] = f"{float(cs):.3f}"
            except (TypeError, ValueError):
                d["score_fmt"] = "—"
        out.append(d)
    return out


def create_app() -> Flask:
    from data.data_store import get_connection, init_schema
    from monitoring.dashboard_data import build_dashboard_payload

    init_schema()
    app = Flask(__name__)

    @app.route("/health")
    def health():
        return {"status": "ok"}, 200

    @app.get("/api/dashboard")
    def api_dashboard() -> Response:
        with get_connection() as conn:
            payload = build_dashboard_payload(conn)
        return Response(
            json.dumps(payload, default=str),
            mimetype="application/json",
        )

    @app.get("/")
    def index() -> str:
        with get_connection() as conn:
            payload = build_dashboard_payload(conn)
        latest = payload.get("portfolio") or {}
        pnl = payload.get("pnl_vs_start_pct")
        pnl_str = f"{pnl:+.2f}%" if pnl is not None else "—"
        pnl_class = ""
        if pnl is not None:
            pnl_class = "pos" if pnl >= 0 else "neg"
        try:
            eq = float(latest["equity_total"]) if latest.get("equity_total") is not None else None
        except (TypeError, ValueError):
            eq = None
        eq_str = f"{eq:.2f}" if eq is not None else "—"
        try:
            dep = float(latest["deployed_pct"]) if latest.get("deployed_pct") is not None else None
        except (TypeError, ValueError):
            dep = None
        dep_str = f"{dep:.1f}%" if dep is not None else "—"
        mode_str = str(payload.get("mode") or latest.get("mode") or "—")
        chart_data = {"equity_series": payload.get("equity_series") or []}
        return render_template_string(
            _PAGE,
            refresh_sec=_REFRESH_SEC,
            db=str(config.DB_PATH),
            pnl_str=pnl_str,
            pnl_class=pnl_class,
            eq_str=eq_str,
            mode_str=mode_str,
            dep_str=dep_str,
            positions=_fmt_positions(payload.get("open_positions") or []),
            trades=_fmt_trades(payload.get("recent_trades") or []),
            signals=_fmt_signals(payload.get("recent_signals") or []),
            chart_data=chart_data,
        )

    return app


def run_dashboard() -> None:
    port = int(os.environ.get("PORT", "5000"))
    logger.info(
        "Monitoring dashboard | http://0.0.0.0:{} (refresh {}s)",
        port,
        _REFRESH_SEC,
    )
    app = create_app()
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)


if __name__ == "__main__":
    app = create_app()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
