import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.makedirs(os.path.join(sys.path[0], "persist"), exist_ok=True)

"""Flask monitoring dashboard (Sprint 8) — port 5000 by default, JSON + HTML UI.

Import policy: this module must not import ``main_worker``, ``training.*``, or other
heavy trading/sentiment stacks. Use only Flask, loguru, ``config``, and lazy imports
inside ``create_app`` from ``data.data_store`` + ``monitoring.dashboard_data``.
"""

import json
import dataclasses
import math
from pathlib import Path
import time
import threading
from datetime import datetime, timezone
from typing import Any

from flask import Flask, Response, jsonify, render_template_string, request
from loguru import logger

import config

_REFRESH_SEC = 30
DASHBOARD_SECRET = os.environ.get("DASHBOARD_SECRET", "")
_DEBUG_LOG_PATH = Path("debug-22f1f6.log")
_CLIENT_DEBUG_LOCK = threading.Lock()
_CLIENT_DEBUG_EVENTS: list[dict[str, Any]] = []


def _debug_log(hypothesis_id: str, message: str, data: dict[str, Any]) -> None:
    try:
        rec = {
            "sessionId": "22f1f6",
            "runId": "run2",
            "hypothesisId": hypothesis_id,
            "location": "monitoring/dashboard.py",
            "message": message,
            "data": data,
            "timestamp": int(time.time() * 1000),
        }
        with _DEBUG_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")
    except Exception:
        pass


def _client_debug_add(evt: dict[str, Any]) -> None:
    with _CLIENT_DEBUG_LOCK:
        _CLIENT_DEBUG_EVENTS.append(evt)
        if len(_CLIENT_DEBUG_EVENTS) > 400:
            del _CLIENT_DEBUG_EVENTS[:-200]

_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>QuantBot Dashboard</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
  <style>
    :root {
      --bg: #0a0e14;
      --card: #111827;
      --border: #1f2937;
      --text: #e5e7eb;
      --muted: #9ca3af;
      --accent: #38bdf8;
      --good: #34d399;
      --bad: #f87171;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: system-ui, Segoe UI, Roboto, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.45;
      font-size: 14px;
    }
    header {
      padding: 0.75rem 1rem;
      border-bottom: 1px solid var(--border);
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 0.75rem;
      justify-content: space-between;
    }
    header h1 { margin: 0; font-size: 1.1rem; font-weight: 700; letter-spacing: 0.04em; }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
    #dashError {
      display: none;
      width: 100%;
      padding: 0.5rem 0.75rem;
      background: rgba(248,113,113,0.12);
      border: 1px solid var(--bad);
      color: #fecaca;
      border-radius: 6px;
      font-size: 13px;
    }
    #dashStatus { color: var(--muted); font-size: 12px; }
    nav {
      display: flex;
      gap: 0.35rem;
      padding: 0.5rem 1rem;
      border-bottom: 1px solid var(--border);
      flex-wrap: wrap;
    }
    nav button {
      background: var(--card);
      border: 1px solid var(--border);
      color: var(--text);
      padding: 0.4rem 0.85rem;
      border-radius: 6px;
      cursor: pointer;
      font-size: 13px;
    }
    nav button.active { border-color: var(--accent); color: var(--accent); }
    main { padding: 0.75rem 1rem 2rem; max-width: 1200px; margin: 0 auto; }
    .tab-panel { display: none; }
    .tab-panel.active { display: block; }
    .grid-metrics {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
      gap: 0.5rem;
      margin-bottom: 1rem;
    }
    .metric {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 0.55rem 0.65rem;
    }
    .metric .lab { font-size: 11px; color: var(--muted); margin-bottom: 0.2rem; }
    .metric .val { font-size: 1rem; font-weight: 600; }
    .card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 0.65rem 0.75rem;
      margin-bottom: 0.75rem;
    }
    .card h2 { margin: 0 0 0.5rem; font-size: 0.95rem; font-weight: 600; }
    .card h3 { margin: 0.75rem 0 0.35rem; font-size: 0.85rem; color: var(--muted); font-weight: 600; }
    table.data {
      width: 100%;
      border-collapse: collapse;
      font-size: 12px;
    }
    table.data th, table.data td {
      border-bottom: 1px solid var(--border);
      padding: 0.35rem 0.45rem;
      text-align: left;
    }
    table.data th { color: var(--muted); font-weight: 600; }
    .empty-hint { color: var(--muted); font-size: 13px; margin: 0.35rem 0; }
    .chart-wrap { position: relative; height: 220px; margin-top: 0.35rem; }
    .foot { font-size: 11px; color: var(--muted); margin-top: 1rem; padding-top: 0.5rem; border-top: 1px solid var(--border); }
    .bt-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 0.5rem; align-items: end; }
    .bt-grid label { display: block; font-size: 11px; color: var(--muted); margin-bottom: 0.2rem; }
    .bt-grid input, .bt-grid select {
      width: 100%;
      padding: 0.35rem 0.45rem;
      border-radius: 6px;
      border: 1px solid var(--border);
      background: #0b1220;
      color: var(--text);
    }
    .bt-actions { display: flex; flex-wrap: wrap; gap: 0.4rem; margin-top: 0.65rem; }
    .bt-actions button {
      padding: 0.45rem 0.75rem;
      border-radius: 6px;
      border: 1px solid var(--accent);
      background: rgba(56,189,248,0.12);
      color: var(--accent);
      cursor: pointer;
      font-size: 13px;
    }
    .bt-actions button.primary { background: rgba(52,211,153,0.15); border-color: var(--good); color: var(--good); }
    #btStatus { margin-top: 0.5rem; font-size: 13px; color: var(--muted); }
    pre.sec { font-size: 11px; overflow: auto; max-height: 120px; margin: 0.35rem 0 0; color: var(--muted); }
  </style>
</head>
<body>
<div id="boot-debug" style="background:#300;color:#fff;padding:8px;font-family:monospace">
JS NOT STARTED
</div>
<script>
document.getElementById("boot-debug").textContent = "TINY SCRIPT RAN";
console.log("TINY SCRIPT RAN");
</script>
  <input type="hidden" id="dash-secret-holder" value="{{ dashboard_secret|e }}"/>
  <header>
    <h1 class="mono">QuantBot</h1>
    <span id="dashStatus">Loading…</span>
    <div id="dashError" role="alert"></div>
  </header>
  <nav aria-label="Tabs">
    <button type="button" class="tab-btn active" data-tab="overview">Overview</button>
    <button type="button" class="tab-btn" data-tab="positions">Positions</button>
    <button type="button" class="tab-btn" data-tab="activity">Activity</button>
    <button type="button" class="tab-btn" data-tab="backtest">Backtest</button>
  </nav>

  <main>
    <section id="panel-overview" class="tab-panel active">
      <div class="grid-metrics">
        <div class="metric"><div class="lab">Mode</div><div class="val mono" id="mMode">—</div></div>
        <div class="metric"><div class="lab">Total equity</div><div class="val mono" id="mEq">—</div></div>
        <div class="metric"><div class="lab">Live P&amp;L ($)</div><div class="val mono" id="mPnlD">—</div></div>
        <div class="metric"><div class="lab">Live P&amp;L (%)</div><div class="val mono" id="mPnlP">—</div></div>
        <div class="metric"><div class="lab">Cash</div><div class="val mono" id="mCash">—</div></div>
        <div class="metric"><div class="lab">Market</div><div class="val mono" id="mMkt">—</div></div>
        <div class="metric"><div class="lab">Capital stage</div><div class="val mono" id="mCap">—</div></div>
      </div>
      <div class="card">
        <h2>Equity</h2>
        <div class="chart-wrap"><canvas id="equityChart"></canvas></div>
        <p class="empty-hint" id="eqEmptyHint" style="display:none;">No equity series returned.</p>
      </div>
      <div class="card">
        <h2>Top open positions (5)</h2>
        <p class="empty-hint" id="posTopEmpty" style="display:none;">No positions returned.</p>
        <table class="data" id="tblOverviewPositions"><thead><tr>
          <th>Symbol</th><th>Qty</th><th>Entry</th><th>Mark</th><th>uPnL %</th>
        </tr></thead><tbody></tbody></table>
      </div>
      <div class="card">
        <h2>Last execution decisions (10)</h2>
        <p class="empty-hint" id="decEmpty" style="display:none;">No decisions returned.</p>
        <table class="data" id="tblOverviewDecisions"><thead><tr>
          <th>Time</th><th>Symbol</th><th>Side</th><th>Decision</th><th>Reason</th>
        </tr></thead><tbody></tbody></table>
      </div>
    </section>

    <section id="panel-positions" class="tab-panel">
      <div class="card">
        <h2>All open positions</h2>
        <p class="empty-hint" id="posAllEmpty" style="display:none;">No positions returned.</p>
        <div style="overflow-x:auto;">
          <table class="data" id="tblPositionsFull"><thead><tr>
            <th>Symbol</th><th>Class</th><th>Qty</th><th>Entry</th><th>Current</th><th>Mkt value</th><th>uPnL</th><th>uPnL %</th><th>Note</th>
          </tr></thead><tbody></tbody></table>
        </div>
      </div>
    </section>

    <section id="panel-activity" class="tab-panel">
      <div class="card">
        <h2>Recent trades</h2>
        <p class="empty-hint" id="actTradesEmpty" style="display:none;">No trades returned.</p>
        <div style="overflow-x:auto;"><table class="data" id="tblActivityTrades"><thead><tr>
          <th>Time</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Price</th><th>Notional</th><th>Status</th>
        </tr></thead><tbody></tbody></table></div>
      </div>
      <div class="card">
        <h2>Recent signals</h2>
        <p class="empty-hint" id="actSigEmpty" style="display:none;">No signals returned.</p>
        <div style="overflow-x:auto;"><table class="data" id="tblActivitySignals"><thead><tr>
          <th>Time</th><th>Symbol</th><th>Name</th><th>Dir</th><th>Value</th>
        </tr></thead><tbody></tbody></table></div>
      </div>
      <div class="card">
        <h2>Execution decisions</h2>
        <p class="empty-hint" id="actDecEmpty" style="display:none;">No execution decisions returned.</p>
        <div style="overflow-x:auto;"><table class="data" id="tblActivityDecisions"><thead><tr>
          <th>Time</th><th>Symbol</th><th>Side</th><th>Decision</th><th>Reason</th><th>Score</th>
        </tr></thead><tbody></tbody></table></div>
      </div>
      <div class="card">
        <h2>Performance</h2>
        <p class="mono" id="actPerfLine">—</p>
      </div>
      <div class="card">
        <h2>Calibration</h2>
        <p class="empty-hint" id="actCalEmpty" style="display:none;">No calibration rows.</p>
        <table class="data" id="tblCalibration"><thead><tr>
          <th>Leg</th><th>N</th><th>Acc %</th><th>Weight</th>
        </tr></thead><tbody></tbody></table>
      </div>
      <div class="card">
        <h2>Section status</h2>
        <pre class="sec mono" id="actSectionStatus">—</pre>
      </div>
    </section>

    <section id="panel-backtest" class="tab-panel">
      <div class="card">
        <h2>Backtest</h2>
        <div class="bt-grid">
          <div><label for="btStrategy">Strategy</label><select id="btStrategy"></select></div>
          <div style="grid-column: span 2;"><label for="btSymbols">Symbols (CSV)</label><input id="btSymbols" value="AAPL,MSFT"/></div>
          <div><label for="btStart">Start</label><input id="btStart" type="date" value="2025-01-01"/></div>
          <div><label for="btEnd">End</label><input id="btEnd" type="date" value="2026-01-01"/></div>
          <div><label for="btTimeframe">Timeframe</label><select id="btTimeframe"><option value="1Day">1Day</option><option value="1H">1H</option></select></div>
          <div><label for="btStartingCash">Starting cash</label><input id="btStartingCash" type="number" step="0.01" value="100"/></div>
        </div>
        <div class="bt-actions">
          <button type="button" class="primary" id="btRunBtn">Run Backtest</button>
          <button type="button" id="btCompareBtn">Compare Strategies</button>
          <button type="button" id="btCopyReportBtn" disabled>Copy Report</button>
          <button type="button" id="btDownloadReportBtn" disabled>Download Report</button>
        </div>
        <p id="btStatus">Load defaults when you open this tab.</p>
      </div>
    </section>

    <p class="foot mono">DB: {{ db }} · Poll every {{ refresh_sec }}s · <span id="pollFoot">HTTP only</span></p>
  </main>

<script>
(function () {
  "use strict";
  var boot = document.getElementById("boot-debug");
  if (boot) boot.textContent = "APP JS STARTED";
  var _dh = document.getElementById("dash-secret-holder");
  var DASHBOARD_SECRET = _dh ? _dh.value : "";
  var equityChart = null;

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/\x3c/g, "&lt;").replace(/\x3e/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function fmtMoney(v) {
    var n = Number(v);
    return n === n && Number.isFinite(n) ? "$" + n.toFixed(2) : "—";
  }

  function fmtPct(v) {
    var n = Number(v);
    return n === n && Number.isFinite(n) ? n.toFixed(2) + "%" : "—";
  }

  function num(v, fallback) {
    var n = Number(v);
    return n === n && Number.isFinite(n) ? n : fallback;
  }

  function numOr(v, fallback) {
    var n = Number(v);
    return n === n && Number.isFinite(n) ? n : fallback;
  }

  function mapDashboardPayload(payload) {
    var p = payload && typeof payload === "object" ? payload : {};
    var pf = p.portfolio && typeof p.portfolio === "object" ? p.portfolio : {};
    var cs = numOr(pf.cash_stocks, 0);
    var cc = numOr(pf.cash_crypto, 0);
    var eqN = Number(pf.equity_total);
    var eqOk = pf.equity_total != null && eqN === eqN && Number.isFinite(eqN);
    return {
      mode: p.mode != null ? String(p.mode) : "—",
      equity: eqOk ? eqN : null,
      cash: cs + cc,
      pnlDollars: num(p.pnl_vs_start_dollars, null),
      pnlPct: num(p.pnl_vs_start_pct, null),
      marketOpen: typeof p.market_open === "boolean" ? p.market_open : null,
      equitySeries: Array.isArray(p.equity_series) ? p.equity_series : [],
      positions: Array.isArray(p.open_positions) ? p.open_positions : [],
      recentTrades: Array.isArray(p.recent_trades) ? p.recent_trades : [],
      recentSignals: Array.isArray(p.recent_signals) ? p.recent_signals : [],
      executionDecisions: Array.isArray(p.execution_decisions) ? p.execution_decisions : [],
      capitalStage: p.capital_stage && typeof p.capital_stage === "object" ? p.capital_stage : {},
      performance: p.performance && typeof p.performance === "object" ? p.performance : {},
      calibration: p.calibration && typeof p.calibration === "object" ? p.calibration : {},
      sectionStatus: p.section_status && typeof p.section_status === "object" ? p.section_status : {}
    };
  }

  function positionNote(row, marketOpen) {
    var ac = String(row.asset_class || "").toLowerCase();
    if (ac === "crypto") return "Crypto can trade 24/7, waiting for signal";
    if (ac === "stock" && marketOpen === false) return "Stock exit blocked: market closed";
    return "Holding / no exit shown";
  }

  function renderEquityChart(vm) {
    var series = vm.equitySeries || [];
    var canvas = document.getElementById("equityChart");
    var eqHint = document.getElementById("eqEmptyHint");
    if (!canvas) return;
    if (typeof Chart === "undefined") {
      if (eqHint) {
        eqHint.style.display = "block";
        eqHint.textContent = "Chart.js not loaded.";
      }
      return;
    }
    if (!series.length) {
      if (eqHint) eqHint.style.display = "block";
      if (equityChart) {
        equityChart.destroy();
        equityChart = null;
      }
      return;
    }
    if (eqHint) eqHint.style.display = "none";
    var labels = series.map(function (r) { return String(r.snapshot_at || ""); });
    var vals = series.map(function (r) { return num(r.equity_total, 0); });
    if (!equityChart) {
      equityChart = new Chart(canvas.getContext("2d"), {
        type: "line",
        data: { labels: labels, datasets: [{ data: vals, borderColor: "#34d399", tension: 0.2, pointRadius: 0 }] },
        options: { animation: false, responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } } }
      });
    } else {
      equityChart.data.labels = labels;
      equityChart.data.datasets[0].data = vals;
      equityChart.update("none");
    }
  }

  function renderOverview(vm) {
    document.getElementById("mMode").textContent = vm.mode;
    document.getElementById("mEq").textContent = vm.equity != null ? fmtMoney(vm.equity) : "—";
    document.getElementById("mPnlD").textContent = vm.pnlDollars != null ? fmtMoney(vm.pnlDollars) : "—";
    document.getElementById("mPnlP").textContent = vm.pnlPct != null ? fmtPct(vm.pnlPct) : "—";
    document.getElementById("mCash").textContent = fmtMoney(vm.cash);
    var mo = vm.marketOpen;
    document.getElementById("mMkt").textContent = mo === true ? "OPEN" : mo === false ? "CLOSED" : "—";
    var st = vm.capitalStage || {};
    document.getElementById("mCap").textContent = st.stage != null ? String(st.stage) : (st.name != null ? String(st.name) : "—");

    renderEquityChart(vm);

    var top = (vm.positions || []).slice(0, 5);
    var tb = document.querySelector("#tblOverviewPositions tbody");
    if (tb) {
      document.getElementById("posTopEmpty").style.display = top.length ? "none" : "block";
      tb.innerHTML = top.map(function (r) {
        var q = num(r.net_qty, null);
        var qs = q != null ? String(q) : "—";
        var up = num(r.unrealized_pnl_pct, null);
        var ups = up != null ? fmtPct(up) : "—";
        return "<tr><td>" + esc(r.symbol) + "</td><td class=\"mono\">" + esc(qs) + "</td><td class=\"mono\">" + fmtMoney(r.avg_entry_price) + "</td><td class=\"mono\">" + fmtMoney(r.current_price) + "</td><td class=\"mono\">" + esc(ups) + "</td></tr>";
      }).join("");
    }

    var decs = (vm.executionDecisions || []).slice(0, 10);
    document.getElementById("decEmpty").style.display = decs.length ? "none" : "block";
    var dt = document.querySelector("#tblOverviewDecisions tbody");
    if (dt) {
      dt.innerHTML = decs.map(function (r) {
        var meta = r.meta && typeof r.meta === "object" ? r.meta : {};
        var reason = meta.reason != null ? String(meta.reason) : String(r.reason_code || "—");
        return "<tr><td class=\"mono\">" + esc(r.created_at || "") + "</td><td>" + esc(r.symbol || "") + "</td><td>" + esc(r.side || "") + "</td><td>" + esc(r.decision || "") + "</td><td>" + esc(reason) + "</td></tr>";
      }).join("");
    }
  }

  function renderPositionsTab(vm) {
    var rows = vm.positions || [];
    document.getElementById("posAllEmpty").style.display = rows.length ? "none" : "block";
    var pb = document.querySelector("#tblPositionsFull tbody");
    if (pb) {
      pb.innerHTML = rows.map(function (r) {
        var q = num(r.net_qty, null);
        var qs = q != null ? String(q) : "—";
        var mv = num(r.market_value, null);
        var up = num(r.unrealized_pnl, null);
        var upp = num(r.unrealized_pnl_pct, null);
        var note = positionNote(r, vm.marketOpen);
        return "<tr><td>" + esc(r.symbol) + "</td><td>" + esc(r.asset_class || "") + "</td><td class=\"mono\">" + esc(qs) + "</td><td class=\"mono\">" + fmtMoney(r.avg_entry_price) + "</td><td class=\"mono\">" + fmtMoney(r.current_price) + "</td><td class=\"mono\">" + (mv != null ? fmtMoney(mv) : "—") + "</td><td class=\"mono\">" + (up != null ? fmtMoney(up) : "—") + "</td><td class=\"mono\">" + (upp != null ? fmtPct(upp) : "—") + "</td><td><small>" + esc(note) + "</small></td></tr>";
      }).join("");
    }
  }

  function renderActivity(vm) {
    var tr = vm.recentTrades || [];
    document.getElementById("actTradesEmpty").style.display = tr.length ? "none" : "block";
    document.querySelector("#tblActivityTrades tbody").innerHTML = tr.map(function (t) {
      return "<tr><td class=\"mono\">" + esc(t.created_at || "") + "</td><td>" + esc(t.symbol || "") + "</td><td>" + esc(t.side || "") + "</td><td class=\"mono\">" + esc(String(t.quantity != null ? t.quantity : "")) + "</td><td class=\"mono\">" + fmtMoney(t.price) + "</td><td class=\"mono\">" + fmtMoney(t.notional) + "</td><td>" + esc(t.status || "") + "</td></tr>";
    }).join("");

    var sig = vm.recentSignals || [];
    document.getElementById("actSigEmpty").style.display = sig.length ? "none" : "block";
    document.querySelector("#tblActivitySignals tbody").innerHTML = sig.map(function (s) {
      return "<tr><td class=\"mono\">" + esc(s.created_at || "") + "</td><td>" + esc(s.symbol || "") + "</td><td>" + esc(s.signal_name || "") + "</td><td>" + esc(s.direction || "") + "</td><td class=\"mono\">" + esc(String(s.raw_value != null ? s.raw_value : "")) + "</td></tr>";
    }).join("");

    var ed = vm.executionDecisions || [];
    document.getElementById("actDecEmpty").style.display = ed.length ? "none" : "block";
    document.querySelector("#tblActivityDecisions tbody").innerHTML = ed.map(function (r) {
      var meta = r.meta && typeof r.meta === "object" ? r.meta : {};
      var reason = meta.reason != null ? String(meta.reason) : String(r.reason_code || "—");
      return "<tr><td class=\"mono\">" + esc(r.created_at || "") + "</td><td>" + esc(r.symbol || "") + "</td><td>" + esc(r.side || "") + "</td><td>" + esc(r.decision || "") + "</td><td>" + esc(reason) + "</td><td class=\"mono\">" + esc(String(r.score != null ? r.score : "")) + "</td></tr>";
    }).join("");

    var perf = vm.performance || {};
    var parts = [];
    if (perf.total_trades != null) parts.push("total_trades=" + perf.total_trades);
    if (perf.closed_round_trips != null) parts.push("closed_round_trips=" + perf.closed_round_trips);
    if (perf.win_rate_pct != null) parts.push("win_rate_pct=" + perf.win_rate_pct);
    document.getElementById("actPerfLine").textContent = parts.length ? parts.join(" · ") : "No performance summary.";

    var cal = vm.calibration || {};
    var legs = Object.keys(cal).sort();
    document.getElementById("actCalEmpty").style.display = legs.length ? "none" : "block";
    document.querySelector("#tblCalibration tbody").innerHTML = legs.map(function (leg) {
      var row = cal[leg] || {};
      var acc = row.accuracy;
      var res = Number(row.resolved);
      var accStr = res > 0 && acc != null ? String(acc) + "%" : "—";
      return "<tr><td>" + esc(leg) + "</td><td>" + esc(String(row.total != null ? row.total : "")) + "</td><td>" + esc(accStr) + "</td><td>" + esc(String(row.weight_suggestion != null ? row.weight_suggestion : "")) + "</td></tr>";
    }).join("");

    document.getElementById("actSectionStatus").textContent = JSON.stringify(vm.sectionStatus || {}, null, 2);
  }

  function paintViewModel(vm) {
    renderOverview(vm);
    renderPositionsTab(vm);
    renderActivity(vm);
  }

  async function fetchDashboard() {
    var errEl = document.getElementById("dashError");
    var st = document.getElementById("dashStatus");
    try {
      var response = await fetch("/api/dashboard", { cache: "no-store" });
      console.log("FETCH /api/dashboard status", response.status);
      if (!response.ok) throw new Error("HTTP " + response.status);
      var payload = await response.json();
      console.log("PAYLOAD", payload);
      var vm = mapDashboardPayload(payload);
      paintViewModel(vm);
      if (errEl) {
        errEl.style.display = "none";
        errEl.textContent = "";
      }
      if (st) st.textContent = "Updated " + new Date().toLocaleString();
    } catch (error) {
      console.error(error);
      if (errEl) {
        errEl.style.display = "block";
        errEl.textContent = "API failed: " + (error && error.message ? error.message : String(error));
      }
      if (st) st.textContent = "—";
    }
  }

  function bindTabs() {
    var tabs = document.querySelectorAll(".tab-btn");
    var panels = document.querySelectorAll(".tab-panel");
    function show(name) {
      var i;
      for (i = 0; i < tabs.length; i++) {
        tabs[i].classList.toggle("active", tabs[i].getAttribute("data-tab") === name);
      }
      for (i = 0; i < panels.length; i++) {
        panels[i].classList.toggle("active", panels[i].id === "panel-" + name);
      }
      try {
        localStorage.setItem("quantbot_dash_tab", name);
      } catch (e) {}
      if (name === "backtest" && !window.__btDefaultsLoaded) loadBacktestDefaultsOnce();
    }
    var t;
    for (t = 0; t < tabs.length; t++) {
      (function (btn) {
        btn.addEventListener("click", function () {
          show(btn.getAttribute("data-tab"));
        });
      })(tabs[t]);
    }
    var saved = "overview";
    try {
      saved = localStorage.getItem("quantbot_dash_tab") || "overview";
    } catch (e2) {}
    show(saved);
  }

  var btRunId = null;
  async function loadBacktestDefaultsOnce() {
    if (window.__btDefaultsLoaded) return;
    try {
      var r = await fetch("/api/backtest/defaults", { cache: "no-store" });
      var j = await r.json();
      var sel = document.getElementById("btStrategy");
      if (sel && Array.isArray(j.strategies)) {
        sel.innerHTML = j.strategies.map(function (s) { return "<option value=\"" + esc(s) + "\">" + esc(s) + "</option>"; }).join("");
      }
      if (j.default_timeframe) document.getElementById("btTimeframe").value = j.default_timeframe;
      if (Array.isArray(j.symbols)) document.getElementById("btSymbols").value = j.symbols.join(",");
      document.getElementById("btStatus").textContent = "Defaults loaded.";
      window.__btDefaultsLoaded = true;
    } catch (e) {
      document.getElementById("btStatus").textContent = "Could not load backtest defaults.";
    }
  }

  function wireBacktest() {
    document.getElementById("btRunBtn").addEventListener("click", async function () {
      document.getElementById("btStatus").textContent = "Running…";
      var payload = {
        strategy_name: (document.getElementById("btStrategy") || {}).value || "current_adaptive",
        starting_cash: Number(document.getElementById("btStartingCash").value || 100),
        symbols: String(document.getElementById("btSymbols").value || "").split(",").map(function (s) { return s.trim(); }).filter(Boolean),
        start_date: document.getElementById("btStart").value,
        end_date: document.getElementById("btEnd").value,
        timeframe: document.getElementById("btTimeframe").value,
        pyramiding_enabled: false
      };
      try {
        var r = await fetch("/api/backtest/run", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-Dashboard-Secret": DASHBOARD_SECRET },
          body: JSON.stringify(payload)
        });
        var j = await r.json();
        if (!r.ok || !j.run_id) throw new Error(j.error || "run failed");
        btRunId = j.run_id;
        document.getElementById("btCopyReportBtn").disabled = false;
        document.getElementById("btDownloadReportBtn").disabled = false;
        document.getElementById("btStatus").textContent = "Run complete. id=" + j.run_id;
      } catch (e) {
        document.getElementById("btStatus").textContent = String(e && e.message ? e.message : e);
      }
    });
    document.getElementById("btCompareBtn").addEventListener("click", async function () {
      document.getElementById("btStatus").textContent = "Comparing…";
      try {
        var r = await fetch("/api/backtest/compare", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-Dashboard-Secret": DASHBOARD_SECRET },
          body: JSON.stringify({
            strategy_names: ["current_adaptive", "simple_momentum"],
            starting_cash: Number(document.getElementById("btStartingCash").value || 100),
            symbols: String(document.getElementById("btSymbols").value || "AAPL").split(",").map(function (s) { return s.trim(); }).filter(Boolean),
            start_date: document.getElementById("btStart").value,
            end_date: document.getElementById("btEnd").value,
            timeframe: document.getElementById("btTimeframe").value,
            pyramiding_enabled: false
          })
        });
        var j = await r.json();
        if (!r.ok || !j.ok) throw new Error(j.error || "compare failed");
        document.getElementById("btStatus").textContent = "Compare finished (" + (j.rows || []).length + " rows).";
      } catch (e) {
        document.getElementById("btStatus").textContent = String(e && e.message ? e.message : e);
      }
    });
    async function getReportMd() {
      if (!btRunId) throw new Error("Run a backtest first.");
      var r = await fetch("/api/backtest/report/" + encodeURIComponent(btRunId) + "?format=markdown", { cache: "no-store" });
      var t = await r.text();
      if (!r.ok) throw new Error(t || "report failed");
      return t;
    }
    document.getElementById("btCopyReportBtn").addEventListener("click", async function () {
      try {
        var md = await getReportMd();
        await navigator.clipboard.writeText(md);
        document.getElementById("btStatus").textContent = "Report copied.";
      } catch (e) {
        document.getElementById("btStatus").textContent = String(e && e.message ? e.message : e);
      }
    });
    document.getElementById("btDownloadReportBtn").addEventListener("click", async function () {
      try {
        var md = await getReportMd();
        var blob = new Blob([md], { type: "text/markdown;charset=utf-8" });
        var u = URL.createObjectURL(blob);
        var a = document.createElement("a");
        a.href = u;
        a.download = "quantbot_backtest_" + btRunId + ".md";
        a.click();
        URL.revokeObjectURL(u);
        document.getElementById("btStatus").textContent = "Download started.";
      } catch (e) {
        document.getElementById("btStatus").textContent = String(e && e.message ? e.message : e);
      }
    });
  }

  function startDashboard() {
    bindTabs();
    wireBacktest();
    fetchDashboard();
    setInterval(fetchDashboard, 30000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", startDashboard);
  } else {
    startDashboard();
  }
})();
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


_SLIDER_LIMITS: dict[str, tuple[float, float, float]] = {
    "buy_threshold": (0.05, 0.40, 0.01),
    "sell_threshold": (-0.40, -0.05, 0.01),
    "crypto_buy_threshold": (0.05, 0.35, 0.01),
    "rsi_oversold": (10.0, 45.0, 0.5),
    "rsi_overbought": (55.0, 90.0, 0.5),
    "kelly_fraction": (0.01, 0.99, 0.01),
    "stop_loss_pct": (0.01, 0.25, 0.005),
    "take_profit_pct": (0.02, 0.50, 0.01),
    "crypto_take_profit_pct": (0.02, 0.50, 0.01),
    "crypto_stop_loss_pct": (0.01, 0.25, 0.005),
    "crypto_trailing_stop_pct": (0.005, 0.15, 0.005),
    "stock_take_profit_pct": (0.02, 0.50, 0.01),
    "stock_stop_loss_pct": (0.01, 0.25, 0.005),
    "stock_trailing_stop_pct": (0.005, 0.15, 0.005),
    "crypto_fast_exit_enabled": (0.0, 1.0, 1.0),
    "pdt_exit_block_seconds": (60.0, 3600.0, 30.0),
    "dashboard_exit_positions_limit": (5.0, 200.0, 1.0),
    "max_position_pct": (0.02, 0.25, 0.01),
}


def _bot_ui_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in rows:
        key = str(r["key"])
        if key.startswith("rl_") or key == "dynamic_risk_enabled":
            continue
        lo, hi, st = _SLIDER_LIMITS.get(key, (0.0, 1.0, 0.01))
        d = dict(r)
        d["min"] = lo
        d["max"] = hi
        d["step"] = st
        d["value"] = float(d["value"])
        out.append(d)
    return out


def _fmt_signals(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        d = dict(r)
        cs = d.get("combined_score")
        if cs is None:
            d["score_fmt"] = "—"
            sc = 0.0
        else:
            try:
                sc = float(cs)
                d["score_fmt"] = f"{sc:.3f}"
            except (TypeError, ValueError):
                d["score_fmt"] = "—"
                sc = 0.0
        sc_clamped = max(-1.0, min(1.0, sc))
        d["score_bar_pct"] = int(round((sc_clamped + 1.0) / 2.0 * 100))
        if sc > 0.3:
            d["score_row_class"] = "sig-buy"
        elif sc < -0.3:
            d["score_row_class"] = "sig-sell"
        else:
            d["score_row_class"] = "sig-neutral"
        try:
            dir_v = int(d.get("direction") or 0)
        except (TypeError, ValueError):
            dir_v = 0
        d["dir_arrow"] = "▲" if dir_v > 0 else ("▼" if dir_v < 0 else "—")
        d["dir_class"] = "dir-up" if dir_v > 0 else ("dir-down" if dir_v < 0 else "dir-flat")
        d["action_badge"] = "BUY" if dir_v > 0 else ("SELL" if dir_v < 0 else "HOLD")
        out.append(d)
    return out


def _build_backtest_interpretation(summary: dict[str, Any], rejection_summary: dict[str, int], cfg: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    excess = float(summary.get("excess_return_pct") or 0.0)
    ret = float(summary.get("return_pct") or 0.0)
    closed = int(summary.get("closed_trades") or 0)
    rejections_total = int(summary.get("rejections_total") or 0)
    thr = dict((summary.get("confidence_rationale") or {}).get("thresholds") or {})
    low_min = int(thr.get("confidence_low_min_closed_trades", cfg.get("confidence_low_min_closed_trades", 10)))
    if excess >= 0:
        lines.append("Strategy beat benchmark on excess return.")
    else:
        lines.append("Strategy underperformed benchmark on excess return.")
    if closed < low_min:
        lines.append("Sample size is low relative to configured confidence thresholds.")
    if ret > 0 and excess < 0:
        lines.append("Positive absolute return is misleading because benchmark did better.")
    if rejections_total > 0:
        top = sorted(rejection_summary.items(), key=lambda kv: int(kv[1]), reverse=True)[:3]
        lines.append("High action blocking from execution rules: " + ", ".join(f"{k}={v}" for k, v in top))
    return lines


def _to_json_safe(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return _to_json_safe(dataclasses.asdict(value))
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _to_json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_json_safe(v) for v in value]
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return _to_json_safe(item())
        except Exception:
            pass
    to_py_dt = getattr(value, "to_pydatetime", None)
    if callable(to_py_dt):
        try:
            return _to_json_safe(to_py_dt())
        except Exception:
            pass
    return str(value)


def _safe_float(value: Any) -> float | None:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(n):
        return None
    return n


def _safe_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _build_backtest_report_payload(
    run_row: dict[str, Any],
    bt_cfg: dict[str, Any],
    *,
    comparison_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    summary_raw = run_row.get("summary_json")
    if isinstance(summary_raw, str) and summary_raw.strip():
        try:
            summary = json.loads(summary_raw)
        except json.JSONDecodeError:
            summary = {}
    elif isinstance(summary_raw, dict):
        summary = summary_raw
    else:
        summary = {}
    rej_raw = run_row.get("rejection_summary_json")
    if isinstance(rej_raw, str) and rej_raw.strip():
        try:
            rejection_summary = json.loads(rej_raw)
        except json.JSONDecodeError:
            rejection_summary = {}
    elif isinstance(rej_raw, dict):
        rejection_summary = rej_raw
    else:
        rejection_summary = {}
    trades = list(run_row.get("trades") or [])
    rejections = list(run_row.get("rejections") or [])
    signal_events = list(run_row.get("signal_events") or [])
    max_trades = int(bt_cfg.get("backtest_max_report_trades", 80))
    max_rejections = int(bt_cfg.get("backtest_max_report_rejections", 100))
    max_signal_events = int(bt_cfg.get("backtest_max_report_signal_events", 100))
    request_json = run_row.get("request_json")
    request_obj: dict[str, Any] = {}
    if isinstance(request_json, str) and request_json.strip():
        try:
            request_obj = json.loads(request_json)
        except json.JSONDecodeError:
            request_obj = {}
    elif isinstance(request_json, dict):
        request_obj = request_json
    assumptions = dict(summary.get("assumptions") or {})
    data_quality = dict(summary.get("data_quality") or {})
    for t in trades:
        raw = t.get("meta_json")
        if isinstance(raw, str) and raw.strip():
            try:
                t["meta_json"] = json.loads(raw)
            except json.JSONDecodeError:
                t["meta_json"] = {}
    for s in signal_events:
        raw = s.get("meta_json")
        if isinstance(raw, str) and raw.strip():
            try:
                s["meta_json"] = json.loads(raw)
            except json.JSONDecodeError:
                s["meta_json"] = {}
    interpretation_lines = _build_backtest_interpretation(summary, rejection_summary, bt_cfg)
    raw_payload = {
        "id": run_row.get("id"),
        "created_at": run_row.get("created_at"),
        "strategy_name": run_row.get("strategy_name"),
        "status": run_row.get("status"),
        "summary_json": summary,
        "rejection_summary_json": rejection_summary,
        "signal_events_summary": {
            k: int(v)
            for k, v in sorted(
                ((str(x.get("reason_code") or "UNKNOWN"), 0) for x in signal_events),
                key=lambda kv: kv[0],
            )
        },
    }
    sig_counts: dict[str, int] = {}
    for s in signal_events:
        key = str(s.get("reason_code") or "UNKNOWN")
        sig_counts[key] = sig_counts.get(key, 0) + 1
    raw_payload["signal_events_summary"] = sig_counts
    return {
        "report_generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "run_id": run_row.get("id"),
        "request": request_obj,
        "summary": summary,
        "interpretation": interpretation_lines,
        "comparison_rows": _to_json_safe(comparison_rows or []),
        "rejection_summary": rejection_summary,
        "signal_events_summary": sig_counts,
        "trades_detail": trades[-max_trades:],
        "rejections_detail": rejections[-max_rejections:],
        "signal_events_detail": signal_events[-max_signal_events:],
        "assumptions": assumptions,
        "data_quality": data_quality,
        "report_config": {
            "max_trades": max_trades,
            "max_rejections": max_rejections,
            "max_signal_events": max_signal_events,
            "effective_backtest_config": bt_cfg,
        },
        "raw_json": _to_json_safe(raw_payload),
    }


def _backtest_report_markdown(report: dict[str, Any]) -> str:
    req = dict(report.get("request") or {})
    summary = dict(report.get("summary") or {})
    assumptions = dict(report.get("assumptions") or {})
    cfg = dict((report.get("report_config") or {}).get("effective_backtest_config") or {})
    costs = dict(cfg.get("backtest_cost_defaults") or {})
    lines = [
        "# QuantBot Backtest Report",
        "",
        "## Request",
        f"- Strategy: {req.get('strategy_name', '—')}",
        f"- Symbols: {', '.join(req.get('symbols', []) or [])}",
        f"- Start date: {req.get('start_date', '—')}",
        f"- End date: {req.get('end_date', '—')}",
        f"- Timeframe: {req.get('timeframe', '—')}",
        f"- Starting cash: {req.get('starting_cash', '—')}",
        f"- Pyramiding: {req.get('pyramiding_enabled', '—')}",
        "- Costs:",
        f"  - fee_bps: {costs.get('fee_bps', req.get('fee_bps', '—'))}",
        f"  - spread_bps: {costs.get('spread_bps', req.get('spread_bps', '—'))}",
        f"  - slippage_bps: {costs.get('slippage_bps', req.get('slippage_bps', '—'))}",
        "",
        "## Summary",
        f"- Starting cash: {summary.get('starting_cash', '—')}",
        f"- Final equity: {summary.get('final_equity', '—')}",
        f"- P&L: {summary.get('pnl', '—')}",
        f"- Return %: {summary.get('return_pct', '—')}",
        f"- Buy & Hold Return %: {summary.get('equal_weight_buy_and_hold_return_pct', '—')}",
        f"- Excess Return %: {summary.get('excess_return_pct', '—')}",
        f"- Max Drawdown %: {summary.get('max_drawdown_pct', '—')}",
        f"- Total trades: {summary.get('trades_total', '—')}",
        f"- Closed trades: {summary.get('closed_trades', '—')}",
        f"- Win rate: {summary.get('win_rate_pct', '—')}",
        f"- Profit factor: {summary.get('profit_factor', '—')}",
        f"- Expectancy: {summary.get('expectancy', '—')}",
        f"- Rejections total: {summary.get('rejections_total', '—')}",
        f"- Confidence label: {summary.get('confidence_label', '—')}",
        f"- Confidence rationale: {json.dumps(summary.get('confidence_rationale', {}), default=str)}",
        f"- Capital deployed avg %: {summary.get('capital_deployed_avg_pct', '—')}",
        f"- Capital deployed max %: {summary.get('capital_deployed_max_pct', '—')}",
        f"- Idle cash avg %: {summary.get('idle_cash_avg_pct', '—')}",
        f"- Idle cash max %: {summary.get('idle_cash_max_pct', '—')}",
        f"- Time in market %: {summary.get('time_in_market_pct', '—')}",
        f"- Capital turnover: {summary.get('capital_turnover', '—')}",
        f"- Open positions end: {summary.get('open_positions_end', '—')}",
        "",
        "## Benchmark Definitions",
        f"- theoretical_equal_weight_buy_and_hold: {((assumptions.get('benchmark_definitions') or {}).get('theoretical_equal_weight_buy_and_hold') or 'reference benchmark')}",
        f"- executable_buy_and_hold: {((assumptions.get('benchmark_definitions') or {}).get('executable_buy_and_hold') or 'simulated benchmark strategy')}",
        "",
        "## Interpretation",
    ]
    for ln in report.get("interpretation", []) or []:
        lines.append(f"- {ln}")
    lines.extend(
        [
            "",
            "## Strategy Comparison",
            (
                "Strategy comparison was not run."
                if not (report.get("comparison_rows") or [])
                else json.dumps(report.get("comparison_rows", []), default=str)
            ),
            "",
            "## Rejection Summary",
            json.dumps(report.get("rejection_summary", {}), default=str),
            "",
            "## Signal Events Summary",
            json.dumps(report.get("signal_events_summary", {}), default=str),
            "",
            "## Simulated Trades",
            json.dumps(report.get("trades_detail", []), default=str),
            "",
            "## Rejections Detail",
            json.dumps(report.get("rejections_detail", []), default=str),
            "",
            "## Signal Events Detail",
            json.dumps(report.get("signal_events_detail", []), default=str),
            "",
            "## Assumptions",
            json.dumps(report.get("assumptions", {}), default=str),
            "",
            "## Data Quality",
            json.dumps(report.get("data_quality", {}), default=str),
            "",
            "## Raw JSON",
            json.dumps(report.get("raw_json", {}), default=str),
        ]
    )
    return "\n".join(lines)


def _dashboard_host_port() -> tuple[str, int]:
    """Host/port for Railway and local runs; PORT wins over FLASK_PORT."""
    host = os.getenv("FLASK_HOST", "0.0.0.0").strip()
    port = int(os.getenv("PORT", os.getenv("FLASK_PORT", "5000")))
    return host, port


def create_app() -> Flask:
    from data import data_store
    from data.data_store import get_connection, init_schema
    from backtesting.models import BacktestRequest
    from backtesting import runner as backtest_runner
    from backtesting import experiments as backtest_experiments
    from monitoring.dashboard_data import (
        build_dashboard_payload,
        start_alpaca_background_cache_thread,
    )

    app = Flask(__name__)

    @app.get("/health")
    def health():
        """Railway liveness: no DB, Alpaca, or worker dependency."""
        return jsonify({"ok": True, "service": "quantbot-dashboard"}), 200

    try:
        init_schema()
    except Exception as exc:
        logger.exception(
            "init_schema failed; /health still OK but DB-backed routes may fail: {}", exc
        )

    if not app.config.get("TESTING") and not os.environ.get("PYTEST_CURRENT_TEST"):
        start_alpaca_background_cache_thread()

    from flask_socketio import SocketIO

    preferred_async = os.environ.get("SOCKETIO_ASYNC_MODE", "eventlet")
    try:
        socketio = SocketIO(app, cors_allowed_origins="*", async_mode=preferred_async)
    except (ValueError, ImportError, ModuleNotFoundError):
        preferred_async = "threading"
        socketio = SocketIO(app, cors_allowed_origins="*", async_mode=preferred_async)
    app.config["SOCKETIO_ASYNC_MODE"] = preferred_async

    def _check_auth() -> bool:
        if not DASHBOARD_SECRET:
            return True
        supplied = request.headers.get("X-Dashboard-Secret", "") or request.args.get("secret", "")
        return supplied == DASHBOARD_SECRET

    def _build_dashboard_payload_safe(period: str) -> dict[str, Any]:
        """Dashboard JSON; broker/Alpaca slices are merged from the background cache only."""
        _debug_log(
            "H7",
            "build_dashboard_payload_safe entry",
            {"period": period, "alpaca_from_background_cache": True},
        )

        try:
            with get_connection() as conn:
                payload = build_dashboard_payload(
                    conn, rest_client=None, equity_period=period
                )
            _debug_log(
                "H7",
                "build_dashboard_payload_safe success",
                {
                    "has_payload": isinstance(payload, dict),
                    "positions": len(payload.get("open_positions", []))
                    if isinstance(payload, dict)
                    and isinstance(payload.get("open_positions"), list)
                    else -1,
                },
            )
            return payload
        except Exception:
            logger.exception("[dashboard] build_dashboard_payload failed — fallback open_conn")
            _debug_log("H8", "build_dashboard_payload_safe exception fallback", {"period": period})
            return build_dashboard_payload(None, rest_client=None, equity_period=period)

    app.extensions["socketio"] = socketio
    # WebSocket push disabled — dashboard uses HTTP polling only.

    @app.get("/health/ready")
    def health_ready():
        """
        Readiness: SQLite reachable and optional portfolio snapshot age.
        Use for ops; Railway should use lightweight GET /health.
        """
        import time as _time
        from datetime import datetime as _dt
        from datetime import timezone as _tz

        max_age_sec = int(os.environ.get("HEALTH_MAX_SNAPSHOT_AGE_SEC", "1800"))
        status: dict[str, Any] = {"status": "ok", "checks": {}}
        http_code = 200

        try:
            with get_connection() as conn:
                row = conn.execute("SELECT 1").fetchone()
                status["checks"]["db"] = "ok" if row else "degraded"
                row2 = conn.execute(
                    "SELECT snapshot_at FROM portfolio_state ORDER BY id DESC LIMIT 1"
                ).fetchone()
            if row2 is None or row2[0] is None:
                status["checks"]["snapshot"] = "missing"
            else:
                raw_ts = str(row2[0])
                snap_dt: _dt | None = None
                try:
                    s = raw_ts.replace("T", " ").rstrip("Z")
                    snap_dt = _dt.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    snap_dt = None
                if snap_dt is None:
                    status["checks"]["snapshot"] = "unparseable"
                else:
                    age = (
                        _dt.now(_tz.utc).replace(tzinfo=None) - snap_dt
                    ).total_seconds()
                    status["checks"]["snapshot_age_sec"] = int(age)
                    if age > max_age_sec:
                        status["checks"]["snapshot"] = "stale"
                        status["status"] = "degraded"
                        http_code = 503
                    else:
                        status["checks"]["snapshot"] = "fresh"
        except Exception as exc:
            status["status"] = "error"
            status["checks"]["db"] = f"error: {exc!s}"
            http_code = 503

        status["checked_at"] = int(_time.time())
        return status, http_code

    @app.get("/api/dashboard")
    def api_dashboard() -> Response:
        period = str(request.args.get("equity_period", "1D") or "1D")
        if period not in ("1D", "1W", "1M", "3M"):
            period = "1D"
        payload = _build_dashboard_payload_safe(period)
        _debug_log(
            "H9",
            "api_dashboard response summary",
            {
                "period": period,
                "positions": len(payload.get("open_positions", []))
                if isinstance(payload.get("open_positions"), list)
                else -1,
                "signals": len(payload.get("recent_signals", []))
                if isinstance(payload.get("recent_signals"), list)
                else -1,
                "eq": len(payload.get("equity_series", []))
                if isinstance(payload.get("equity_series"), list)
                else -1,
                "has_eh": isinstance(payload.get("execution_health"), dict),
            },
        )
        return Response(
            json.dumps(payload, default=str),
            mimetype="application/json",
        )

    @app.post("/api/client-debug")
    def api_client_debug_post() -> tuple[dict[str, Any], int]:
        body = request.get_json(silent=True) or {}
        evt = {
            "sessionId": str(body.get("sessionId", "")),
            "runId": str(body.get("runId", "")),
            "hypothesisId": str(body.get("hypothesisId", "")),
            "message": str(body.get("message", "")),
            "location": str(body.get("location", "client")),
            "data": body.get("data", {}),
            "timestamp": body.get("timestamp"),
            "server_received_ms": int(time.time() * 1000),
        }
        _client_debug_add(evt)
        _debug_log("H10", "client debug event received", {"hypothesisId": evt["hypothesisId"], "message": evt["message"]})
        return {"ok": True}, 200

    @app.get("/api/client-debug")
    def api_client_debug_get() -> Response:
        limit_raw = request.args.get("limit", "120")
        try:
            limit = max(1, min(500, int(str(limit_raw))))
        except ValueError:
            limit = 120
        with _CLIENT_DEBUG_LOCK:
            rows = _CLIENT_DEBUG_EVENTS[-limit:]
        return Response(json.dumps({"rows": rows}, default=str), mimetype="application/json")

    @app.get("/api/config")
    def api_config_get() -> Response:
        with get_connection() as conn:
            rows = data_store.fetch_all_bot_config_rows(conn)
        return Response(json.dumps(rows, default=str), mimetype="application/json")

    @app.post("/api/config")
    def api_config_post() -> tuple[dict[str, Any], int]:
        if not _check_auth():
            return {"ok": False, "error": "unauthorized"}, 401
        body = request.get_json(force=True, silent=True) or {}
        key = str(body.get("key", "")).strip()
        if not key or key.startswith("rl_") or key not in data_store.BOT_CONFIG_DEFAULTS:
            return {"ok": False, "error": "invalid key"}, 400
        try:
            val = float(body["value"])
        except (TypeError, ValueError, KeyError):
            return {"ok": False, "error": "invalid value"}, 400
        data_store.set_config(key, val)
        return {"ok": True}, 200

    @app.post("/api/config/reset")
    def api_config_reset() -> tuple[dict[str, Any], int]:
        if not _check_auth():
            return {"ok": False, "error": "unauthorized"}, 401
        data_store.reset_bot_config_to_defaults()
        return {"ok": True}, 200

    @app.post("/api/reset-db")
    def api_reset_db() -> Any:
        """Admin: wipe trade history and rescale bot_config keys from defaults."""
        if not _check_auth():
            return jsonify({"error": "unauthorized"}), 401
        try:
            result = data_store.reset_trading_history(str(config.DB_PATH))
            return jsonify({"status": "ok", "result": result})
        except Exception as e:
            logger.exception("api/reset-db failed")
            return jsonify({"status": "error", "message": str(e)}), 500

    @app.post("/api/reset-paper")
    def api_reset_paper() -> Any:
        """Hard reset paper-mode rows + scalp / decision tables."""
        if not _check_auth():
            return jsonify({"error": "unauthorized"}), 401
        try:
            result = data_store.reset_paper_trading_state(str(config.DB_PATH))
            return jsonify({"status": "ok", "result": result})
        except Exception as e:
            logger.exception("api/reset-paper failed")
            return jsonify({"status": "error", "message": str(e)}), 500

    @app.get("/api/promotion-gates")
    def api_promotion_gates() -> Response:
        from risk import promotion_gates as _pg

        try:
            data = _pg.evaluate_all(str(config.DB_PATH))
        except Exception as e:
            logger.exception("api/promotion-gates failed")
            data = {"passed": False, "gates": [], "error": str(e)}
        return Response(json.dumps(data, default=str), mimetype="application/json")

    @app.get("/api/safety-status")
    def api_safety_status() -> Response:
        return Response(
            json.dumps(
                {
                    "live_safety": config.live_safety_status(),
                    "is_live": config.trading_is_live(),
                    "scalper_paper_enabled": config.scalper_paper_enabled(),
                    "scalper_live_allowed": config.scalper_live_allowed(),
                    "mode": config.MODE,
                },
                default=str,
            ),
            mimetype="application/json",
        )

    @app.get("/api/buy-gate-status")
    def api_buy_gate_status() -> Response:
        from monitoring.dashboard_data import fetch_latest_buy_gate

        with get_connection() as conn:
            payload = fetch_latest_buy_gate(conn)
        return Response(json.dumps(payload, default=str), mimetype="application/json")

    @app.get("/api/strategy-parameters")
    def api_strategy_parameters() -> Response:
        strategy_name = str(request.args.get("strategy_name", "aggressive_micro_scalp") or "aggressive_micro_scalp")
        capital_stage = str(request.args.get("capital_stage", "MICRO") or "MICRO").upper()
        rows = data_store.fetch_strategy_parameters(strategy_name, capital_stage)
        return Response(json.dumps(rows, default=str), mimetype="application/json")

    @app.get("/api/strategy-effective-parameters")
    def api_strategy_effective_parameters() -> Response:
        strategy_name = str(request.args.get("strategy_name", "aggressive_micro_scalp") or "aggressive_micro_scalp")
        capital_stage = str(request.args.get("capital_stage", "MICRO") or "MICRO").upper()
        row = data_store.fetch_strategy_runtime_state(strategy_name, capital_stage) or {}
        payload: dict[str, Any] = {}
        raw = row.get("current_state_json")
        if raw:
            try:
                payload = json.loads(str(raw))
            except json.JSONDecodeError:
                payload = {}
        return Response(json.dumps(payload, default=str), mimetype="application/json")

    @app.get("/api/adaptive-parameter-changes")
    def api_adaptive_parameter_changes() -> Response:
        strategy_name = str(request.args.get("strategy_name", "aggressive_micro_scalp") or "aggressive_micro_scalp")
        capital_stage = str(request.args.get("capital_stage", "MICRO") or "MICRO").upper()
        limit = int(request.args.get("limit", 20) or 20)
        rows = data_store.fetch_adaptive_parameter_changes(strategy_name, capital_stage, limit=limit)
        return Response(json.dumps(rows, default=str), mimetype="application/json")

    @app.post("/api/strategy-parameters/reset")
    def api_strategy_parameters_reset() -> Any:
        if not _check_auth():
            return jsonify({"error": "unauthorized"}), 401
        body = request.get_json(force=True, silent=True) or {}
        strategy_name = str(body.get("strategy_name", "aggressive_micro_scalp") or "aggressive_micro_scalp")
        capital_stage = str(body.get("capital_stage", "MICRO") or "MICRO").upper()
        equity = body.get("equity")
        result = data_store.reset_strategy_parameters_to_defaults(
            strategy_name,
            capital_stage,
            equity=float(equity) if equity is not None else None,
        )
        return jsonify({"ok": True, "result": result})

    @app.post("/api/strategy-parameters/pause")
    def api_strategy_parameters_pause() -> Any:
        if not _check_auth():
            return jsonify({"error": "unauthorized"}), 401
        body = request.get_json(force=True, silent=True) or {}
        strategy_name = str(body.get("strategy_name", "aggressive_micro_scalp") or "aggressive_micro_scalp")
        capital_stage = str(body.get("capital_stage", "MICRO") or "MICRO").upper()
        pause = bool(body.get("pause", True))
        data_store.set_strategy_parameter(
            strategy_name,
            capital_stage,
            "paused",
            1 if pause else 0,
            value_type="bool",
            min_value=0,
            max_value=1,
            source="dashboard_pause",
        )
        return jsonify({"ok": True, "paused": pause})

    @app.get("/api/backtest/defaults")
    def api_backtest_defaults() -> Response:
        bt_cfg = data_store.fetch_backtest_config(config.DB_PATH)
        cost_defaults = dict(bt_cfg.get("backtest_cost_defaults") or {})
        default_tf = str(bt_cfg.get("backtest_default_timeframe") or "1Day")
        default_symbols = list(bt_cfg.get("backtest_default_symbols") or ["AAPL", "MSFT", "BTC/USD"])
        default_days = int(bt_cfg.get("backtest_default_date_range_days", 365))
        defaults = {
            "strategies": [
                "combined_stock",
                "crypto_scalper",
                "aggressive_micro_scalp",
                "current_adaptive",
                "simple_buy_and_hold",
                "simple_momentum",
            ],
            "symbols": default_symbols,
            "timeframes": ["1Day", "1H"],
            "date_range_days": default_days,
            "costs": {
                "fee_bps": float(cost_defaults.get("fee_bps", 5.0)),
                "slippage_bps": float(cost_defaults.get("slippage_bps", 10.0)),
                "spread_bps": float(cost_defaults.get("spread_bps", 20.0)),
            },
            "default_timeframe": default_tf,
            "backtest_config": bt_cfg,
            "parameter_defaults": dict(bt_cfg.get("backtest_parameter_defaults") or {}),
            "parameter_allowed_ranges": dict(bt_cfg.get("backtest_parameter_allowed_ranges") or {}),
            "ranking_weights": dict(bt_cfg.get("backtest_ranking_weights") or {}),
            "experiment_runtime_caps": dict(bt_cfg.get("backtest_experiment_runtime_caps") or {}),
            "presets": [
                {"id": "sanity", "label": "Small sanity test", "symbols": ["AAPL", "MSFT", "BTC/USD"], "timeframe": "1Day", "days": 365},
                {"id": "crypto", "label": "Crypto only", "symbols": ["BTC/USD", "ETH/USD"], "timeframe": "1H", "days": 90},
                {"id": "holdings", "label": "Current holdings", "symbols": ["ACHR", "AMPX", "FSLY"], "timeframe": default_tf, "days": 180},
                {"id": "stress", "label": "Stress test", "symbols": ["AAPL", "MSFT", "SPY", "BTC/USD", "ETH/USD"], "timeframe": "1H", "days": 90},
            ],
            "runtime_effective": data_store.fetch_strategy_runtime_state("aggressive_micro_scalp", "MICRO") or {},
        }
        return Response(json.dumps(defaults, default=str), mimetype="application/json")

    @app.post("/api/backtest/run")
    def api_backtest_run() -> Any:
        if not _check_auth():
            return jsonify({"error": "unauthorized"}), 401
        body = request.get_json(force=True, silent=True) or {}
        try:
            bt_cfg = data_store.fetch_backtest_config(config.DB_PATH)
            cost_defaults = dict(bt_cfg.get("backtest_cost_defaults") or {})
            default_timeframe = str(bt_cfg.get("backtest_default_timeframe") or "1Day")
            req = BacktestRequest(
                strategy_name=str(body.get("strategy_name", "current_adaptive")),
                asset_class=str(body.get("asset_class", "mixed")),
                symbols=[str(x).strip() for x in body.get("symbols", ["AAPL"]) if str(x).strip()],
                start_date=str(body.get("start_date", "2025-01-01")),
                end_date=str(body.get("end_date", "2026-01-01")),
                timeframe=str(body.get("timeframe", default_timeframe)),
                starting_cash=float(body.get("starting_cash", 100.0)),
                max_position_notional=float(body.get("max_position_notional", 5.0)),
                max_positions=int(body.get("max_positions", 3)),
                max_trades_per_hour=int(body.get("max_trades_per_hour", 6)),
                fee_bps=float(body.get("fee_bps", cost_defaults.get("fee_bps", 5.0))),
                slippage_bps=float(body.get("slippage_bps", cost_defaults.get("slippage_bps", 10.0))),
                spread_bps=float(body.get("spread_bps", cost_defaults.get("spread_bps", 20.0))),
                min_order_notional=float(body.get("min_order_notional", 1.0)),
                allow_fractional=bool(body.get("allow_fractional", True)),
                use_fractionability_rules=bool(body.get("use_fractionability_rules", True)),
                use_market_hours=bool(body.get("use_market_hours", True)),
                pyramiding_enabled=bool(body.get("pyramiding_enabled", False)),
            )
            run_id = data_store.create_backtest_run(
                json.dumps(req.__dict__, default=str),
                strategy_name=req.strategy_name,
                status="running",
                parameter_snapshot_json=json.dumps({"backtest_config": bt_cfg}, default=str),
            )
            parameter_snapshot = {"backtest_config": bt_cfg}
            result = backtest_runner.execute(req, parameter_snapshot=parameter_snapshot)
            data_store.insert_backtest_equity_curve(run_id, [p.__dict__ for p in result.equity_curve])
            data_store.insert_backtest_trades(run_id, [t.__dict__ for t in result.trades])
            data_store.insert_backtest_rejections(run_id, [r.__dict__ for r in result.rejections])
            data_store.insert_backtest_signal_events(
                run_id, [s.__dict__ for s in (getattr(result, "signal_events", []) or [])]
            )
            data_store.update_backtest_status(
                run_id,
                status=result.status,
                summary_json=json.dumps(result.summary_json, default=str),
                rejection_summary_json=json.dumps(result.rejection_summary_json, default=str),
            )
            return jsonify({"ok": True, "run_id": run_id, "status": result.status})
        except Exception as exc:
            logger.exception("api/backtest/run failed")
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/backtest/compare")
    def api_backtest_compare() -> Any:
        if not _check_auth():
            return jsonify({"error": "unauthorized"}), 401
        body = request.get_json(force=True, silent=True) or {}
        bt_cfg = data_store.fetch_backtest_config(config.DB_PATH)
        if not isinstance(bt_cfg, dict):
            bt_cfg = {}
        cost_defaults_raw = bt_cfg.get("backtest_cost_defaults")
        cost_defaults = dict(cost_defaults_raw) if isinstance(cost_defaults_raw, dict) else {}
        default_timeframe = str(bt_cfg.get("backtest_default_timeframe") or "1Day")
        strategies = [str(x).strip() for x in body.get("strategy_names", []) if str(x).strip()]
        if not strategies:
            strategies = ["current_adaptive", "simple_momentum", "crypto_scalper", "aggressive_micro_scalp"]
        try:
            req = BacktestRequest(
                strategy_name=strategies[0],
                asset_class=str(body.get("asset_class", "mixed")),
                symbols=[str(x).strip() for x in body.get("symbols", ["AAPL"]) if str(x).strip()],
                start_date=str(body.get("start_date", "2025-01-01")),
                end_date=str(body.get("end_date", "2026-01-01")),
                timeframe=str(body.get("timeframe", default_timeframe)),
                starting_cash=float(body.get("starting_cash", 100.0)),
                max_position_notional=float(body.get("max_position_notional", 5.0)),
                max_positions=int(body.get("max_positions", 3)),
                max_trades_per_hour=int(body.get("max_trades_per_hour", 6)),
                fee_bps=float(body.get("fee_bps", cost_defaults.get("fee_bps", 5.0))),
                slippage_bps=float(body.get("slippage_bps", cost_defaults.get("slippage_bps", 10.0))),
                spread_bps=float(body.get("spread_bps", cost_defaults.get("spread_bps", 20.0))),
                min_order_notional=float(body.get("min_order_notional", 1.0)),
                allow_fractional=bool(body.get("allow_fractional", True)),
                use_fractionability_rules=bool(body.get("use_fractionability_rules", True)),
                use_market_hours=bool(body.get("use_market_hours", True)),
                pyramiding_enabled=bool(body.get("pyramiding_enabled", False)),
            )
            rows = backtest_runner.execute_comparison(
                strategies,
                req,
                parameter_snapshot={"backtest_config": bt_cfg},
            )
            if not isinstance(rows, list):
                raise ValueError("Invalid comparison response shape")
            normalized_rows: list[dict[str, Any]] = []
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError("Invalid comparison response shape")
                norm = _to_json_safe(row)
                if not isinstance(norm, dict):
                    raise ValueError("Invalid comparison response shape")
                final_equity = _safe_float(norm.get("final_equity"))
                return_pct = _safe_float(norm.get("return_pct"))
                bh_pct = _safe_float(norm.get("buy_and_hold_return_pct"))
                benchmark_return_pct = _safe_float(norm.get("benchmark_return_pct"))
                if benchmark_return_pct is None:
                    benchmark_return_pct = bh_pct
                excess_pct = _safe_float(norm.get("excess_return_pct"))
                if excess_pct is None and return_pct is not None and bh_pct is not None:
                    excess_pct = return_pct - bh_pct
                status = str(norm.get("status") or "completed")
                reason = str(norm.get("reason") or "")
                interp = str(norm.get("interpretation") or ("Beat benchmark" if (excess_pct is not None and excess_pct >= 0) else "Underperformed benchmark"))
                normalized_rows.append(
                    {
                        "strategy": str(norm.get("strategy") or ""),
                        "status": status,
                        "reason": reason,
                        "final_equity": final_equity,
                        "return_pct": return_pct,
                        "benchmark_return_pct": benchmark_return_pct,
                        "buy_and_hold_return_pct": bh_pct,
                        "excess_return_pct": excess_pct,
                        "max_drawdown_pct": _safe_float(norm.get("max_drawdown_pct")),
                        "total_trades": _safe_int(norm.get("total_trades")),
                        "closed_trades": _safe_int(norm.get("closed_trades")),
                        "open_positions_end": _safe_int(norm.get("open_positions_end")),
                        "rejections_total": _safe_int(norm.get("rejections_total")),
                        "confidence_label": str(norm.get("confidence_label") or "unknown"),
                        "capital_deployed_avg_pct": _safe_float(norm.get("capital_deployed_avg_pct")),
                        "capital_deployed_max_pct": _safe_float(norm.get("capital_deployed_max_pct")),
                        "idle_cash_avg_pct": _safe_float(norm.get("idle_cash_avg_pct")),
                        "idle_cash_max_pct": _safe_float(norm.get("idle_cash_max_pct")),
                        "capital_turnover": _safe_float(norm.get("capital_turnover")),
                        "time_in_market_pct": _safe_float(norm.get("time_in_market_pct")),
                        "interpretation": interp,
                    }
                )
            return jsonify({"ok": True, "rows": normalized_rows, "backtest_config": _to_json_safe(bt_cfg)})
        except Exception as exc:
            logger.exception("api/backtest/compare failed")
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/backtest/runs")
    def api_backtest_runs() -> Response:
        limit = int(request.args.get("limit", 20) or 20)
        rows = data_store.fetch_backtest_runs(limit=limit)
        return Response(json.dumps(rows, default=str), mimetype="application/json")

    @app.get("/api/backtest/result/<int:run_id>")
    def api_backtest_result(run_id: int) -> Any:
        row = data_store.fetch_backtest_result(run_id)
        if row is None:
            return jsonify({"ok": False, "error": "not found"}), 404
        summary_raw = row.get("summary_json")
        if isinstance(summary_raw, str) and summary_raw.strip():
            try:
                summary_obj = json.loads(summary_raw)
            except json.JSONDecodeError:
                summary_obj = {}
            if isinstance(summary_obj, dict):
                row["summary_json"] = summary_obj
                row["assumptions"] = summary_obj.get("assumptions", {})
                row["data_quality"] = summary_obj.get("data_quality", {})
                row["warnings"] = summary_obj.get("warnings", [])
        rejection_raw = row.get("rejection_summary_json")
        if isinstance(rejection_raw, str) and rejection_raw.strip():
            try:
                rejection_obj = json.loads(rejection_raw)
            except json.JSONDecodeError:
                rejection_obj = {}
            if isinstance(rejection_obj, dict):
                row["rejection_summary_json"] = rejection_obj
        for t in row.get("trades", []) or []:
            raw = t.get("meta_json")
            if isinstance(raw, str) and raw.strip():
                try:
                    t["meta_json"] = json.loads(raw)
                except json.JSONDecodeError:
                    t["meta_json"] = {}
        for s in row.get("signal_events", []) or []:
            raw = s.get("meta_json")
            if isinstance(raw, str) and raw.strip():
                try:
                    s["meta_json"] = json.loads(raw)
                except json.JSONDecodeError:
                    s["meta_json"] = {}
        return jsonify(row)

    @app.get("/api/backtest/report/<int:run_id>")
    def api_backtest_report(run_id: int) -> Any:
        row = data_store.fetch_backtest_result(run_id)
        if row is None:
            return jsonify({"ok": False, "error": "not found"}), 404
        bt_cfg = data_store.fetch_backtest_config(config.DB_PATH)
        fmt = str(request.args.get("format", "markdown") or "markdown").strip().lower()
        report = _build_backtest_report_payload(row, bt_cfg)
        if fmt == "json":
            return jsonify(report)
        md = _backtest_report_markdown(report)
        return Response(md, mimetype="text/markdown")

    @app.get("/api/backtest/parameter-defaults")
    def api_backtest_parameter_defaults() -> Any:
        bt_cfg = data_store.fetch_backtest_config(config.DB_PATH)
        return jsonify(
            {
                "ok": True,
                "parameter_defaults": bt_cfg.get("backtest_parameter_defaults") or {},
                "parameter_allowed_ranges": bt_cfg.get("backtest_parameter_allowed_ranges") or {},
                "ranking_weights": bt_cfg.get("backtest_ranking_weights") or {},
                "runtime_caps": bt_cfg.get("backtest_experiment_runtime_caps") or {},
                "confidence_thresholds": {
                    "confidence_low_min_closed_trades": int(bt_cfg.get("confidence_low_min_closed_trades", 10)),
                    "confidence_medium_min_closed_trades": int(bt_cfg.get("confidence_medium_min_closed_trades", 30)),
                    "confidence_high_min_closed_trades": int(bt_cfg.get("confidence_high_min_closed_trades", 60)),
                },
            }
        )

    @app.get("/api/backtest/parameter-sets")
    def api_backtest_parameter_sets() -> Any:
        strategy_name = str(request.args.get("strategy_name", "") or "").strip()
        rows = data_store.fetch_strategy_parameter_sets(strategy_name=(strategy_name or None), limit=200)
        return jsonify({"ok": True, "rows": rows})

    @app.post("/api/backtest/parameter-sets")
    def api_backtest_parameter_sets_create() -> Any:
        if not _check_auth():
            return jsonify({"error": "unauthorized"}), 401
        body = request.get_json(force=True, silent=True) or {}
        name = str(body.get("name", "") or "").strip()
        strategy_name = str(body.get("strategy_name", "") or "").strip()
        params = body.get("params_json")
        if not name or not strategy_name or not isinstance(params, dict):
            return jsonify({"ok": False, "error": "invalid payload"}), 400
        set_id = data_store.create_strategy_parameter_set(
            name=name,
            strategy_name=strategy_name,
            source=str(body.get("source") or "manual"),
            params=params,
            notes=str(body.get("notes") or ""),
            status=str(body.get("status") or "draft"),
            active=False,
        )
        return jsonify({"ok": True, "id": set_id})

    @app.post("/api/backtest/parameter-sets/<int:set_id>/mark-paper-candidate")
    def api_backtest_parameter_set_mark_paper_candidate(set_id: int) -> Any:
        if not _check_auth():
            return jsonify({"error": "unauthorized"}), 401
        data_store.mark_parameter_set_paper_candidate(set_id)
        return jsonify({"ok": True, "id": set_id, "status": "paper_candidate", "active": False})

    @app.post("/api/backtest/experiments/run")
    def api_backtest_experiments_run() -> Any:
        if not _check_auth():
            return jsonify({"error": "unauthorized"}), 401
        body = request.get_json(force=True, silent=True) or {}
        bt_cfg = data_store.fetch_backtest_config(config.DB_PATH)
        cost_defaults = dict(bt_cfg.get("backtest_cost_defaults") or {})
        default_timeframe = str(bt_cfg.get("backtest_default_timeframe") or "1Day")
        strategy_name = str(body.get("strategy_name", "current_adaptive") or "current_adaptive")
        symbols = [str(x).strip() for x in body.get("symbols", ["AAPL"]) if str(x).strip()]
        req = BacktestRequest(
            strategy_name=strategy_name,
            asset_class=str(body.get("asset_class", "mixed")),
            symbols=symbols,
            start_date=str(body.get("start_date", "2025-01-01")),
            end_date=str(body.get("end_date", "2026-01-01")),
            timeframe=str(body.get("timeframe", default_timeframe)),
            starting_cash=float(body.get("starting_cash", 100.0)),
            max_position_notional=float(body.get("max_position_notional", 5.0)),
            max_positions=int(body.get("max_positions", 3)),
            max_trades_per_hour=int(body.get("max_trades_per_hour", 6)),
            fee_bps=float(body.get("fee_bps", cost_defaults.get("fee_bps", 5.0))),
            slippage_bps=float(body.get("slippage_bps", cost_defaults.get("slippage_bps", 10.0))),
            spread_bps=float(body.get("spread_bps", cost_defaults.get("spread_bps", 20.0))),
            min_order_notional=float(body.get("min_order_notional", 1.0)),
            allow_fractional=bool(body.get("allow_fractional", True)),
            use_fractionability_rules=bool(body.get("use_fractionability_rules", True)),
            use_market_hours=bool(body.get("use_market_hours", True)),
            pyramiding_enabled=bool(body.get("pyramiding_enabled", False)),
        )
        parameter_grid = body.get("parameter_grid_json")
        if not isinstance(parameter_grid, dict):
            parameter_grid = {}
        runtime_caps = dict(bt_cfg.get("backtest_experiment_runtime_caps") or {})
        ranking_weights = dict(bt_cfg.get("backtest_ranking_weights") or {})
        req_weights = body.get("ranking_weights_json")
        if isinstance(req_weights, dict):
            ranking_weights.update(req_weights)
        confidence_thresholds = {
            "confidence_low_min_closed_trades": int(bt_cfg.get("confidence_low_min_closed_trades", 10)),
            "confidence_medium_min_closed_trades": int(bt_cfg.get("confidence_medium_min_closed_trades", 30)),
            "confidence_high_min_closed_trades": int(bt_cfg.get("confidence_high_min_closed_trades", 60)),
        }
        experiment_id = data_store.create_backtest_experiment(
            name=str(body.get("name") or f"{strategy_name}-experiment"),
            strategy_name=strategy_name,
            symbols=symbols,
            start_date=req.start_date,
            end_date=req.end_date,
            timeframe=req.timeframe,
            starting_cash=req.starting_cash,
            cost_assumptions={
                "fee_bps": req.fee_bps,
                "slippage_bps": req.slippage_bps,
                "spread_bps": req.spread_bps,
            },
            parameter_grid=parameter_grid,
            ranking_weights=ranking_weights,
            status="running",
        )
        try:
            result = backtest_experiments.run_parameter_experiment(
                strategy_name=strategy_name,
                base_request=req,
                parameter_grid=parameter_grid,
                weights=ranking_weights,
                confidence_thresholds=confidence_thresholds,
                caps=runtime_caps,
                walk_forward=(body.get("walk_forward") if isinstance(body.get("walk_forward"), dict) else bt_cfg.get("backtest_walk_forward_defaults")),
                parameter_snapshot={"backtest_config": bt_cfg},
            )
            for row in result.get("rows", []):
                data_store.insert_backtest_experiment_result(
                    experiment_id,
                    params=row.get("params") if isinstance(row.get("params"), dict) else {},
                    metrics=row.get("metrics") if isinstance(row.get("metrics"), dict) else {},
                    rank_score=_safe_float(row.get("rank_score")),
                    status=str(row.get("status") or "completed"),
                    warnings=row.get("warnings") if isinstance(row.get("warnings"), list) else [],
                )
            data_store.update_backtest_experiment(
                experiment_id,
                status="completed",
                best_result=(result.get("best_result") if isinstance(result.get("best_result"), dict) else {}),
                summary=(result.get("summary") if isinstance(result.get("summary"), dict) else {}),
            )
            return jsonify(
                {
                    "ok": True,
                    "experiment_id": experiment_id,
                    "top_results": result.get("rows", [])[:10],
                    "summary": result.get("summary", {}),
                }
            )
        except Exception as exc:
            data_store.update_backtest_experiment(
                experiment_id,
                status="failed",
                summary={"error": str(exc), "runtime_caps": runtime_caps},
            )
            return jsonify({"ok": False, "error": str(exc), "experiment_id": experiment_id}), 400

    @app.get("/api/backtest/experiments")
    def api_backtest_experiments() -> Any:
        limit = int(request.args.get("limit", 20) or 20)
        rows = data_store.fetch_backtest_experiments(limit=limit)
        return jsonify({"ok": True, "rows": rows})

    @app.get("/api/backtest/experiments/<int:experiment_id>")
    def api_backtest_experiment(experiment_id: int) -> Any:
        row = data_store.fetch_backtest_experiment(experiment_id)
        if row is None:
            return jsonify({"ok": False, "error": "not found"}), 404
        return jsonify({"ok": True, "experiment": row})

    @app.get("/api/backtest/experiments/<int:experiment_id>/report")
    def api_backtest_experiment_report(experiment_id: int) -> Any:
        row = data_store.fetch_backtest_experiment(experiment_id)
        if row is None:
            return jsonify({"ok": False, "error": "not found"}), 404
        fmt = str(request.args.get("format", "json") or "json").strip().lower()
        result_rows = list(row.get("results") or [])
        report_limit = int((data_store.fetch_backtest_config(config.DB_PATH).get("backtest_max_report_trades") or 80))
        payload = {
            "experiment_id": row.get("id"),
            "created_at": row.get("created_at"),
            "request": {
                "name": row.get("name"),
                "strategy_name": row.get("strategy_name"),
                "symbols": row.get("symbols_json") or [],
                "start_date": row.get("start_date"),
                "end_date": row.get("end_date"),
                "timeframe": row.get("timeframe"),
                "starting_cash": row.get("starting_cash"),
            },
            "parameter_grid": row.get("parameter_grid_json") or {},
            "ranking_weights": row.get("ranking_weights_json") or {},
            "top_candidates": result_rows[:10],
            "full_results_limited": result_rows[:report_limit],
            "summary": row.get("summary_json") or {},
            "best_result": row.get("best_result_json") or {},
            "warnings": [w for r in result_rows for w in (r.get("warnings_json") or [])],
        }
        if fmt == "markdown":
            text = "\n".join(
                [
                    f"# QuantBot Experiment Report {payload['experiment_id']}",
                    "",
                    "## Request",
                    json.dumps(payload["request"], default=str),
                    "",
                    "## Parameter Grid",
                    json.dumps(payload["parameter_grid"], default=str),
                    "",
                    "## Ranking Weights",
                    json.dumps(payload["ranking_weights"], default=str),
                    "",
                    "## Top Candidates",
                    json.dumps(payload["top_candidates"], default=str),
                    "",
                    "## Summary",
                    json.dumps(payload["summary"], default=str),
                ]
            )
            return Response(text, mimetype="text/markdown")
        return jsonify({"ok": True, "report": payload})

    @app.post("/api/sync-alpaca")
    def api_sync_alpaca() -> Any:
        if not _check_auth():
            return jsonify({"error": "unauthorized"}), 401
        from execution import stock_broker

        cli = stock_broker.get_rest_client()
        if cli is None:
            return jsonify({"status": "error", "message": "Alpaca client unavailable"}), 400
        try:
            summary = data_store.sync_from_alpaca(config.DB_PATH, cli)
            return jsonify({"status": "ok", **summary})
        except Exception as e:
            logger.exception("api/sync-alpaca failed")
            return jsonify({"status": "error", "message": str(e)}), 500

    @app.get("/api/calibration")
    def api_calibration() -> Response:
        from learning.calibrator import get_leg_accuracies

        with get_connection() as conn:
            data = get_leg_accuracies(conn)
        return Response(json.dumps(data, default=str), mimetype="application/json")

    @app.get("/api/social")
    def api_social() -> Response:
        try:
            rows = data_store.fetch_reddit_signals_public(10)
        except Exception:
            logger.exception("api/social: failed to read reddit_signals from SQLite")
            rows = []
        return Response(json.dumps(rows, default=str), mimetype="application/json")

    @app.get("/api/new-listings")
    def api_new_listings() -> Any:
        from social import kraken_listings

        return jsonify(kraken_listings.get_listings_status())

    @app.route("/api/symbol/<path:symbol>")
    def symbol_info(symbol: str) -> Any:
        import json as _json
        from urllib.error import URLError, HTTPError
        from urllib.parse import quote
        from urllib.request import Request, urlopen

        symbol_upper = symbol.upper().strip()
        is_crypto = "/" in symbol_upper

        if is_crypto:
            base = symbol_upper.split("/")[0].lower()
            try:
                url = f"https://api.coingecko.com/api/v3/search?query={quote(base)}"
                req = Request(url, headers={"User-Agent": "QuantBot/1.0"})
                with urlopen(req, timeout=5) as resp:
                    raw = _json.loads(resp.read().decode("utf-8", errors="replace"))
                coins = raw.get("coins") or []
                if coins:
                    c = coins[0]
                    rk = c.get("market_cap_rank")
                    return jsonify(
                        {
                            "symbol": symbol_upper,
                            "name": c.get("name", symbol_upper),
                            "type": "crypto",
                            "market_cap_rank": rk,
                            "thumb": c.get("thumb", ""),
                            "description": f"Rank #{rk if rk is not None else '?'} crypto by market cap",
                        }
                    )
            except (OSError, URLError, HTTPError, ValueError, KeyError, TypeError):
                logger.debug("symbol_info CoinGecko failed for {}", symbol_upper, exc_info=True)
            return jsonify(
                {
                    "symbol": symbol_upper,
                    "name": base.upper(),
                    "type": "crypto",
                    "description": "Cryptocurrency",
                }
            )

        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol_upper, safe='')}?interval=1d&range=1d"
            req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(req, timeout=5) as resp:
                data = _json.loads(resp.read().decode("utf-8", errors="replace"))
            meta = data["chart"]["result"][0]["meta"]
            prev = meta.get("chartPreviousClose", meta.get("previousClose"))
            return jsonify(
                {
                    "symbol": symbol_upper,
                    "name": meta.get("longName") or meta.get("shortName", symbol_upper),
                    "type": "stock",
                    "exchange": meta.get("exchangeName", ""),
                    "currency": meta.get("currency", "USD"),
                    "current_price": meta.get("regularMarketPrice"),
                    "previous_close": prev,
                    "description": f"{meta.get('instrumentType', 'Stock')} on {meta.get('exchangeName', '')}",
                }
            )
        except (OSError, URLError, HTTPError, ValueError, KeyError, IndexError, TypeError):
            logger.debug("symbol_info Yahoo failed for {}", symbol_upper, exc_info=True)
        return jsonify(
            {
                "symbol": symbol_upper,
                "name": symbol_upper,
                "type": "stock",
                "description": "Stock — live data unavailable",
            }
        )

    @app.get("/")
    def index() -> str:
        """Static shell; live data from GET /api/dashboard (browser polling)."""
        return render_template_string(
            _PAGE,
            refresh_sec=_REFRESH_SEC,
            db=str(config.DB_PATH),
            dashboard_secret=DASHBOARD_SECRET,
        )

    return app


def run_dashboard() -> None:
    host, port = _dashboard_host_port()
    app = create_app()
    sio = app.extensions.get("socketio")
    mode = app.config.get("SOCKETIO_ASYNC_MODE", "?")
    logger.info(
        "Monitoring dashboard | http://{}:{} (SocketIO async_mode={} · HTTP fallback {}s)",
        host,
        port,
        mode,
        _REFRESH_SEC,
    )
    if sio is not None:
        sio.run(app, host=host, port=port, debug=False, use_reloader=False)
    else:
        app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    host, port = _dashboard_host_port()
    app = create_app()
    sio = app.extensions.get("socketio")
    if sio is not None:
        sio.run(app, host=host, port=port, debug=False, use_reloader=False)
    else:
        app.run(host=host, port=port, debug=False, use_reloader=False)
