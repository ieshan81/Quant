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
from typing import Any

from flask import Flask, Response, jsonify, render_template_string, request
from loguru import logger

import config

_REFRESH_SEC = 30

_PAGE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <meta http-equiv="refresh" content="3600"/>
  <title>QuantBot — Terminal</title>
  <link rel="preconnect" href="https://fonts.googleapis.com"/>
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
  <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600;700&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet"/>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
  <script src="https://cdn.socket.io/4.5.4/socket.io.min.js" crossorigin="anonymous"></script>
  <style>
    :root {
      --bg-primary: #050508;
      --bg-secondary: #050508;
      --bg-card: #0d1117;
      --border: #1e293b;
      --border-bright: #1e293b;
      --accent-blue: #0ea5e9;
      --accent-green: #00ff88;
      --accent-red: #ff3b5c;
      --accent-gold: #f7931a;
      --text-primary: #e2e8f0;
      --text-secondary: #64748b;
      --text-muted: #64748b;
    }
    * { box-sizing: border-box; }
    html { scroll-behavior: smooth; }
    body {
      margin: 0;
      min-height: 100vh;
      font-family: Inter, system-ui, sans-serif;
      color: var(--text-primary);
      background: var(--bg-primary);
      background-image: radial-gradient(ellipse 120% 80% at 50% -20%, rgba(0, 212, 255, 0.08), transparent 55%),
        radial-gradient(ellipse 80% 50% at 100% 100%, rgba(0, 255, 136, 0.04), transparent 45%);
    }
    .mono { font-family: "IBM Plex Mono", ui-monospace, monospace; }
    ::-webkit-scrollbar { width: 8px; height: 8px; }
    ::-webkit-scrollbar-track { background: var(--bg-secondary); }
    ::-webkit-scrollbar-thumb { background: var(--text-muted); border-radius: 4px; }
    ::-webkit-scrollbar-thumb:hover { background: var(--text-secondary); }

    .term-header {
      display: flex; align-items: center; justify-content: space-between;
      padding: 0.75rem 1.25rem;
      background: linear-gradient(180deg, rgba(15, 22, 41, 0.95), rgba(10, 14, 26, 0.98));
      border-bottom: 1px solid var(--border);
      position: sticky; top: 0; z-index: 50;
      backdrop-filter: blur(12px);
    }
    .brand { display: flex; align-items: center; gap: 0.5rem; font-weight: 700; letter-spacing: 0.06em; }
    .brand .mono { font-size: 1rem; color: var(--accent-blue); }
    .live-dot {
      width: 8px; height: 8px; border-radius: 50%; background: var(--accent-green);
      box-shadow: 0 0 10px var(--accent-green);
      animation: blink 1.2s ease-in-out infinite;
    }
    @keyframes blink { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }
    .header-center { text-align: center; flex: 1; }
    .clock-et { font-family: "IBM Plex Mono", monospace; font-size: 1.1rem; color: var(--accent-blue); }
    .badge-paper {
      padding: 0.35rem 0.75rem; border-radius: 8px;
      border: 1px solid var(--accent-gold); color: var(--accent-gold);
      font-size: 0.7rem; font-weight: 700; letter-spacing: 0.08em;
    }

    .wrap { max-width: 1400px; margin: 0 auto; padding: 1rem 1.25rem 2rem; }
    .stats-row {
      display: grid; grid-template-columns: repeat(4, 1fr); gap: 1rem; margin-top: 1rem;
    }
    @media (max-width: 1024px) { .stats-row { grid-template-columns: repeat(2, 1fr); } }
    @media (max-width: 560px) { .stats-row { grid-template-columns: 1fr; } }

    .card {
      background: rgba(255, 255, 255, 0.03);
      backdrop-filter: blur(20px);
      -webkit-backdrop-filter: blur(20px);
      border: 1px solid rgba(255, 255, 255, 0.08);
      border-radius: 16px;
      box-shadow: 0 8px 32px rgba(0, 0, 0, 0.4), inset 0 1px 0 rgba(255, 255, 255, 0.05);
      padding: 1rem 1.1rem;
      transition: border-color 0.2s ease, box-shadow 0.2s ease;
    }
    .card:hover { border-color: var(--border-bright); }
    .card h2 {
      margin: 0 0 0.5rem; font-size: 0.72rem; font-weight: 600;
      color: var(--text-secondary); text-transform: uppercase; letter-spacing: 0.06em;
    }
    .big { font-size: 1.65rem; font-weight: 700; font-family: "IBM Plex Mono", monospace; }
    .pos { color: var(--accent-green); }
    .neg { color: var(--accent-red); }
    .muted { color: var(--text-muted); font-size: 0.78rem; }
    .sync-live { color: var(--accent-green) !important; font-weight: 600; }
    .sync-reconnect { color: #ffb020 !important; font-weight: 600; }
    .spark-wrap { height: 48px; margin-top: 0.35rem; }

    .market-open { color: var(--accent-green); font-weight: 700; font-family: "IBM Plex Mono", monospace; }
    .market-closed { color: var(--accent-red); font-weight: 700; font-family: "IBM Plex Mono", monospace; }
    .countdown { font-size: 0.8rem; color: var(--text-secondary); margin-top: 0.35rem; font-family: "IBM Plex Mono", monospace; }

    .mid-grid {
      display: grid; grid-template-columns: 1.5fr 1fr; gap: 1rem; margin-top: 1rem; align-items: start;
    }
    @media (max-width: 960px) { .mid-grid { grid-template-columns: 1fr; } }

    .signal-feed table, .social-table, .data-table { width: 100%; border-collapse: collapse; font-size: 0.8rem; }
    .signal-feed th, .signal-feed td, .social-table th, .social-table td, .data-table th, .data-table td {
      text-align: left; padding: 0.45rem 0.55rem; border-bottom: 1px solid var(--border);
    }
    .signal-feed th, .social-table th, .data-table th {
      color: var(--text-secondary); font-weight: 600; font-size: 0.68rem; text-transform: uppercase;
    }
    .sig-feed-row {
      transition: background 0.25s ease, border-color 0.2s ease;
      border-left: 3px solid transparent;
    }
    .sig-feed-row.sig-buy { border-left-color: var(--accent-green); }
    .sig-feed-row.sig-sell { border-left-color: var(--accent-red); }
    .sig-feed-row.sig-neutral { border-left-color: rgba(255,255,255,0.15); }
    .sig-feed-row.row-flash { animation: rowFlash 0.6s ease-out; }
    @keyframes rowFlash { from { background: rgba(0, 212, 255, 0.12); } to { background: transparent; } }
    .dir-up { color: var(--accent-green); font-weight: 700; }
    .dir-down { color: var(--accent-red); font-weight: 700; }
    .dir-flat { color: var(--text-muted); }
    .score-cell { min-width: 120px; }
    .score-bar-bg {
      height: 6px; border-radius: 3px; background: rgba(255,255,255,0.08); overflow: hidden; margin-top: 0.25rem;
    }
    .score-bar-fill { height: 100%; border-radius: 3px; transition: width 0.35s ease; }
    .sig-buy .score-txt { color: var(--accent-green); }
    .sig-sell .score-txt { color: var(--accent-red); }
    .sig-neutral .score-txt { color: var(--text-secondary); }
    .sig-buy .score-bar-fill { background: linear-gradient(90deg, var(--accent-green), var(--accent-blue)); }
    .sig-sell .score-bar-fill { background: linear-gradient(90deg, var(--accent-red), #ff8899); }
    .sig-neutral .score-bar-fill { background: var(--text-muted); }
    .action-pill {
      display: inline-block; padding: 0.15rem 0.45rem; border-radius: 6px; font-size: 0.65rem; font-weight: 700;
      font-family: "IBM Plex Mono", monospace;
    }
    .pill-buy { background: rgba(0, 255, 136, 0.15); color: var(--accent-green); }
    .pill-sell { background: rgba(255, 68, 102, 0.15); color: var(--accent-red); }
    .pill-hold { background: rgba(255,255,255,0.06); color: var(--text-muted); }

    .social-panel h2 { display: flex; align-items: center; gap: 0.35rem; }
    .breakout-name {
      color: var(--accent-gold); font-weight: 700;
      animation: pulseGold 2s ease-in-out infinite;
    }
    @keyframes pulseGold {
      0%, 100% { text-shadow: 0 0 6px rgba(255, 215, 0, 0.35); }
      50% { text-shadow: 0 0 14px rgba(255, 215, 0, 0.65); }
    }
    .rc-up { color: var(--accent-green); }
    .rc-down { color: var(--accent-red); }

    .bottom-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 1rem; margin-top: 1rem; }
    @media (max-width: 1100px) { .bottom-grid { grid-template-columns: 1fr; } }

    .cal-ok { color: var(--accent-green); }
    .cal-mid { color: #ffd54f; }
    .cal-bad { color: var(--accent-red); }

    .cfg-range { width: 100%; accent-color: var(--accent-blue); }
    .cfg-save, #cfg-reset {
      background: rgba(0, 212, 255, 0.12); border: 1px solid var(--border-bright); color: var(--accent-blue);
      border-radius: 8px; padding: 0.35rem 0.65rem; cursor: pointer; font-weight: 600; transition: background 0.2s;
    }
    .cfg-save:hover, #cfg-reset:hover { background: rgba(0, 212, 255, 0.22); }
    .api-links { margin-top: 1.25rem; font-size: 0.75rem; color: var(--text-muted); }
    .api-links a { color: var(--accent-blue); text-decoration: none; }
    .api-links a:hover { text-decoration: underline; }
    .chart-wrap { height: 200px; margin-top: 0.5rem; }
    .last-upd { font-size: 0.72rem; color: var(--text-muted); margin-top: 0.5rem; font-family: "JetBrains Mono", monospace; }
    .subrow { margin-top: 1rem; display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }
    @media (max-width: 800px) { .subrow { grid-template-columns: 1fr; } }
    .quantbot-terminal { display: none; }
    #sym-tooltip {
      position: fixed; display: none; z-index: 9999; pointer-events: none;
      background: rgba(10, 14, 26, 0.97); backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px);
      border: 1px solid rgba(0, 212, 255, 0.3); border-radius: 12px; padding: 14px 18px;
      min-width: 220px; max-width: 300px; box-shadow: 0 8px 32px rgba(0, 0, 0, 0.6);
      font-family: Inter, system-ui, sans-serif; font-size: 0.82rem;
    }
    #sym-tooltip .tt-name { color: #fff; font-weight: 700; }
    #sym-tooltip .tt-type-c { color: var(--accent-green); font-size: 0.65rem; margin-left: 0.35rem; font-weight: 700; }
    #sym-tooltip .tt-type-s { color: var(--accent-blue); font-size: 0.65rem; margin-left: 0.35rem; font-weight: 700; }
    #sym-tooltip .tt-line2 { color: var(--accent-blue); font-size: 0.72rem; margin-top: 0.35rem; }
    #sym-tooltip .tt-price { color: var(--accent-green); font-family: "JetBrains Mono", monospace; margin-top: 0.35rem; }
    #sym-tooltip .tt-desc { color: var(--text-muted); font-size: 0.72rem; margin-top: 0.35rem; line-height: 1.35; }
    .side-buy { color: var(--accent-green); font-weight: 700; }
    .side-sell { color: var(--accent-red); font-weight: 700; }
    .has-symbol { cursor: help; border-bottom: 1px dashed rgba(0, 212, 255, 0.35); }

    .sym-legend { display: flex; align-items: center; justify-content: center; gap: 1.25rem; flex-wrap: wrap; margin-top: 0.35rem; font-size: 0.72rem; font-family: "IBM Plex Mono", monospace; }
    .legend-stock { color: #00d4ff; font-weight: 600; }
    .legend-crypto { color: #f7931a; font-weight: 600; }

    .data-table tbody tr.row-stock { border-left: 3px solid #00d4ff; }
    .data-table tbody tr.row-crypto { border-left: 3px solid #f7931a; }

    .sym-badge { display: inline-block; margin-right: 0.28rem; font-weight: 700; vertical-align: middle; line-height: 1; }
    .sym-badge-c { color: #f7931a; font-size: 0.72rem; }
    .sym-badge-s { color: #00d4ff; font-size: 0.65rem; }
    .sig-sym-crypto .sym-txt { color: #f7931a; font-weight: 700; }
    .sig-sym-stock .sym-txt { color: #00d4ff; font-weight: 700; }

    /* Institutional grid override */
    .wrap { max-width: 1700px; padding: 1rem 1.1rem 1.4rem; display: grid; gap: 1rem; grid-template-columns: repeat(12, minmax(0, 1fr)); }
    .card { background: var(--bg-card); border: 1px solid var(--border); border-radius: 10px; box-shadow: none; }
    .stats-row { grid-column: 1 / -1; display: grid; grid-template-columns: repeat(12, minmax(0, 1fr)); gap: 1rem; margin-top: 0; }
    .stats-row > .card { grid-column: span 3; }
    .chart-main { grid-column: 1 / span 8; margin-top: 0 !important; }
    .social-panel { grid-column: 9 / -1; }
    .signal-feed { grid-column: 1 / span 8; }
    .subrow { grid-column: 9 / -1; margin-top: 0; display: grid; grid-template-columns: 1fr; gap: 1rem; }
    .bottom-grid { grid-column: 1 / -1; margin-top: 0; display: grid; grid-template-columns: repeat(12, minmax(0, 1fr)); gap: 1rem; }
    .bottom-grid > .card:nth-child(1) { grid-column: span 6; }
    .bottom-grid > .card:nth-child(2) { grid-column: span 6; }
    .bottom-grid > .card:nth-child(3) { grid-column: 1 / -1; }
    .api-links, .last-upd { grid-column: 1 / -1; margin-top: 0; }
    @media (max-width: 1100px) {
      .stats-row > .card { grid-column: span 6; }
      .chart-main, .social-panel, .signal-feed, .subrow, .bottom-grid > .card:nth-child(1), .bottom-grid > .card:nth-child(2), .bottom-grid > .card:nth-child(3) { grid-column: 1 / -1; }
    }
  </style>
</head>
<body data-terminal="1">
  <span class="quantbot-terminal"></span>
  <header class="term-header">
    <div class="brand"><span class="live-dot" title="live"></span><span class="mono">⚡ QUANTBOT</span></div>
    <div class="header-center">
      <div class="clock-et" id="clockEt">—</div>
      <div class="muted sync-reconnect" id="last-sync" style="font-size:0.68rem;margin-top:0.2rem;">Reconnecting…</div>
      <div class="sym-legend muted" title="Symbol coloring in tables below">
        <span class="legend-stock">● STOCK</span>
        <span class="legend-crypto">● CRYPTO</span>
      </div>
    </div>
    <div class="badge-paper">PAPER TRADING</div>
  </header>
  <div class="wrap">
    <div class="stats-row">
      <div class="card"><h2>Live P&amp;L</h2><div class="big {{ pnl_class }}" id="tilePnl">{{ pnl_str }}</div></div>
      <div class="card"><h2>Total equity</h2><div class="big mono" id="tileEq">{{ eq_str }}</div><div class="spark-wrap"><canvas id="sparkEq"></canvas></div></div>
      <div class="card"><h2>Mode</h2><div class="big mono" id="tileMode">{{ mode_str }}</div><p class="muted" style="margin:0.35rem 0 0;">DB: {{ db }}</p></div>
      <div class="card"><h2>Market (NYSE)</h2><div id="mktLine" class="market-closed">…</div><div class="countdown" id="mktCd"></div></div>
    </div>

    <div class="card chart-main"><h2>Equity curve</h2><div class="chart-wrap"><canvas id="eqChart"></canvas></div></div>

    <div class="card social-panel">
      <h2><span>🔥</span> Social momentum</h2>
      <p class="muted" style="margin-top:0;">Top 10 · <a href="/api/social">/api/social</a></p>
      <div id="socialMoRoot" style="margin-top:0.5rem;"><p class="muted">Loading…</p></div>
    </div>

    <div class="card signal-feed">
      <h2>Signal feed</h2>
      <div style="overflow-x:auto;">
      <table><thead><tr><th></th><th>Time</th><th>Symbol</th><th>Type</th><th>Signal</th><th>Score</th><th></th></tr></thead><tbody id="sigFeedBody"></tbody></table>
      </div>
      <p class="muted" id="sigFeedEmpty" style="display:none;margin-top:0.5rem;">No signals logged.</p>
    </div>

    <div class="subrow">
      <div class="card"><h2>Open positions</h2>
        <table class="data-table"><thead><tr><th>Class</th><th>Symbol</th><th>Net</th></tr></thead><tbody id="posTableBody"></tbody></table>
        <p class="muted" id="posEmpty" style="display:none;">No open positions.</p>
      </div>
      <div class="card"><h2>Recent trades</h2>
        <table class="data-table"><thead><tr><th>Time</th><th>Symbol</th><th>Side</th><th>Price</th><th>Qty</th><th>Notional</th><th>Status</th></tr></thead><tbody id="tradesTableBody"></tbody></table>
        <p class="muted" id="tradesEmpty" style="display:none;">No trades yet.</p>
      </div>
    </div>

    <div class="bottom-grid">
      <div class="card"><h2>📊 Signal calibration</h2>
        {% if calibration %}
        <table class="data-table"><thead><tr><th>Leg</th><th>N</th><th>Acc %</th><th>Weight</th></tr></thead><tbody>
          {% for leg, row in calibration.items()|sort %}
          <tr class="{% if row.resolved < 1 %}muted{% elif row.accuracy > 55 %}cal-ok{% elif row.accuracy >= 45 %}cal-mid{% else %}cal-bad{% endif %}">
            <td>{{ leg }}</td><td>{{ row.total }}</td>
            <td>{% if row.resolved > 0 %}{{ row.accuracy }}%{% else %}—{% endif %}</td><td>{{ row.weight_suggestion }}</td>
          </tr>{% endfor %}
        </tbody></table>{% else %}<p class="muted">No calibration data.</p>{% endif %}
      </div>
      <div class="card"><h2>⚙️ Bot parameters</h2>
        <p class="muted" style="margin-top:0;">SQLite — worker reads each cycle.</p>
        <table class="data-table"><thead><tr><th>Parameter</th><th>Slider</th><th>Value</th><th></th></tr></thead><tbody>
          {% for row in bot_ui %}
          <tr data-key="{{ row.key }}">
            <td title="{{ row.description }}">{{ row.key }}</td>
            <td colspan="2"><input type="range" class="cfg-range" data-key="{{ row.key }}" min="{{ row.min }}" max="{{ row.max }}" step="{{ row.step }}" value="{{ row.value }}"/></td>
            <td style="white-space:nowrap;"><input type="number" class="cfg-num mono" data-key="{{ row.key }}" min="{{ row.min }}" max="{{ row.max }}" step="{{ row.step }}" value="{{ row.value }}" style="width:4.5rem"/>
            <button type="button" class="cfg-save" data-key="{{ row.key }}">Save</button></td>
          </tr>{% endfor %}
        </tbody></table>
        <p style="margin-top:0.75rem;"><button type="button" id="cfg-reset">Reset defaults</button></p>
      </div>
      <div class="card"><h2>📈 Performance &amp; learning</h2>
        <p class="muted">Trades: <strong class="mono">{{ perf.total_trades }}</strong> · Round-trips: <strong class="mono">{{ perf.closed_round_trips }}</strong>
          {% if perf.win_rate_pct is not none %} · Win: <strong class="mono">{{ (perf.win_rate_pct | round(1)) }}%</strong>{% endif %}</p>
        {% if rl_history %}
        <table class="data-table"><thead><tr><th>Time</th><th>Summary</th><th>Pairs</th><th>Win%</th></tr></thead><tbody>
          {% for e in rl_history %}
          <tr><td class="mono" style="font-size:0.72rem;">{{ e.created_at }}</td><td>{{ e.summary }}</td><td class="mono">{{ e.trade_count }}</td>
            <td>{% if e.win_rate is not none %}{{ (e.win_rate * 100) | round(1) }}%{% else %}—{% endif %}</td></tr>{% endfor %}
        </tbody></table>{% else %}<p class="muted">No RL nudges yet.</p>{% endif %}
      </div>
    </div>

    <p class="api-links">JSON: <a href="/api/dashboard">/api/dashboard</a> · <a href="/api/config">/api/config</a> · <a href="/api/calibration">/api/calibration</a> · <a href="/api/social">/api/social</a> · <span class="muted">POST</span> <code>/api/sync-alpaca</code></p>
    <p class="last-upd" id="metaNote">Live dashboard via WebSocket (fallback poll {{ refresh_sec }}s) · clock ET</p>
  </div>

  <div id="sym-tooltip" aria-hidden="true"></div>
  <script id="dash-payload" type="application/json">{{ dash_snapshot|tojson }}</script>
  <script>
    const REFRESH_MS = {{ refresh_sec }} * 1000;
    const TZ = "America/New_York";
    let chart, spark;
    let lastPollMs = 0;
    window.__dashWsConnected = false;
    window.__dashWsEnabled = typeof io !== "undefined";
    window.__dashPollTimer = null;
    window._symbolCache = {};
    let __lastDashMarketOpen = undefined;
    let __tooltipFetchTimer = null;
    let __symHoverLast = "";

    function readPayload() {
      const el = document.getElementById("dash-payload");
      return JSON.parse(el.textContent || "{}");
    }
    function esc(s) {
      return String(s ?? "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
    }
    function equityY(row) {
      if (!row) return 0;
      const n = Number(row.equity_total);
      return Number.isFinite(n) ? n : 0;
    }
    function fmtDate(ts) {
      if (!ts) return "—";
      const d = new Date(ts.replace(" ", "T") + "Z");
      const months = ["Jan","Feb","Mar","Apr","May","Jun",
                      "Jul","Aug","Sep","Oct","Nov","Dec"];
      return `${d.getDate()} ${months[d.getMonth()]} ${d.getFullYear()} `
           + `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
    }
    function equityLabel(row) {
      if (!row) return "";
      return fmtDate(row.snapshot_at != null ? row.snapshot_at : "");
    }
    function applyLiveTiles(data) {
      const pnl = data.pnl_vs_start_pct;
      const el = document.getElementById("tilePnl");
      if (el) {
        if (pnl == null || pnl === "" || Number.isNaN(Number(pnl))) { el.textContent = "—"; el.className = "big"; }
        else {
          const n = Number(pnl);
          el.textContent = (n >= 0 ? "+" : "") + n.toFixed(2) + "%";
          el.className = "big " + (n >= 0 ? "pos" : "neg");
        }
      }
      const pf = data.portfolio || {};
      let eq = pf.equity != null ? Number(pf.equity) : (pf.equity_total != null ? Number(pf.equity_total) : null);
      if (eq == null || Number.isNaN(eq)) {
        const ser = data.equity_series || [];
        const last = ser.length ? ser[ser.length - 1] : null;
        if (last) eq = equityY(last);
      }
      const te = document.getElementById("tileEq");
      if (te) te.textContent = (eq != null && !Number.isNaN(eq)) ? eq.toFixed(2) : "—";
      const tm = document.getElementById("tileMode");
      if (tm) tm.textContent = data.mode != null ? String(data.mode) : "—";
      if (typeof data.market_open === "boolean") {
        __lastDashMarketOpen = data.market_open;
      }
    }
    function fmtNum(v, d) {
      const n = Number(v);
      if (!Number.isFinite(n)) return "—";
      return n.toFixed(d);
    }
    function signalMeta(s) {
      let m = s.meta;
      if (m == null) return {};
      if (typeof m === "string") {
        try { m = JSON.parse(m); } catch (e) { return {}; }
      }
      return typeof m === "object" && m !== null ? m : {};
    }
    function signalDirection(s) {
      const d = s.direction;
      if (d === 1 || d === "1") return 1;
      if (d === -1 || d === "-1") return -1;
      return 0;
    }
    function isCryptoSymbol(sym) {
      return String(sym).indexOf("/") >= 0;
    }
    function assetRowClass(sym) {
      return isCryptoSymbol(sym) ? "row-crypto" : "row-stock";
    }
    function renderSignalFeed(signals) {
      const tb = document.getElementById("sigFeedBody");
      const empty = document.getElementById("sigFeedEmpty");
      if (!tb) return;
      const rows = Array.isArray(signals) ? signals : [];
      if (!rows.length) {
        tb.innerHTML = "";
        if (empty) empty.style.display = "block";
        return;
      }
      if (empty) empty.style.display = "none";
      let html = "";
      for (const s of rows) {
        const sym = (s.symbol != null ? String(s.symbol) : "");
        const typeName = (s.signal_name != null ? String(s.signal_name) : "—");
        const meta = signalMeta(s);
        const action = meta.action != null ? String(meta.action) : "—";
        const dir = signalDirection(s);
        const arr = dir > 0 ? "▲" : (dir < 0 ? "▼" : "—");
        const dcls = dir > 0 ? "dir-up" : (dir < 0 ? "dir-down" : "dir-flat");
        let sc = 0;
        try { sc = s.combined_score != null ? Number(s.combined_score) : 0; } catch (e) { sc = 0; }
        if (!Number.isFinite(sc)) sc = 0;
        const scClamped = Math.max(-1, Math.min(1, sc));
        const barPct = Math.round((scClamped + 1) / 2 * 100);
        let rowCls = "sig-neutral";
        if (sc > 0.3) rowCls = "sig-buy";
        else if (sc < -0.3) rowCls = "sig-sell";
        const scoreTxt = Number.isFinite(Number(s.combined_score)) ? Number(s.combined_score).toFixed(3) : "—";
        const symCellCls = isCryptoSymbol(sym) ? "sig-sym-crypto" : "sig-sym-stock";
        const symPre = isCryptoSymbol(sym)
          ? '<span class="sym-badge sym-badge-c" aria-hidden="true">₿</span>'
          : '<span class="sym-badge sym-badge-s" aria-hidden="true">S</span>';
        html += '<tr class="sig-feed-row ' + rowCls + '" data-sig-id="' + esc(s.id) + '">';
        html += '<td class="mono ' + dcls + '">' + arr + '</td>';
        html += '<td class="mono" style="font-size:0.72rem;color:var(--text-secondary);">' + esc(fmtDate(s.created_at)) + '</td>';
        html += '<td class="mono has-symbol ' + symCellCls + '" style="font-weight:700;" data-symbol="' + esc(sym) + '">' + symPre + '<span class="sym-txt">' + esc(sym) + '</span></td>';
        html += '<td>' + esc(typeName) + '</td>';
        html += '<td><span class="mono">' + esc(action) + '</span></td>';
        html += '<td class="score-cell"><span class="score-txt mono">' + scoreTxt + '</span>';
        html += '<div class="score-bar-bg"><div class="score-bar-fill" style="width:' + barPct + '%;"></div></div></td>';
        html += '<td></td></tr>';
      }
      tb.innerHTML = html;
    }
    function renderPositions(pos) {
      const tb = document.getElementById("posTableBody");
      const empty = document.getElementById("posEmpty");
      if (!tb) return;
      const rows = Array.isArray(pos) ? pos : [];
      if (!rows.length) {
        tb.innerHTML = "";
        if (empty) empty.style.display = "block";
        return;
      }
      if (empty) empty.style.display = "none";
      let html = "";
      for (const p of rows) {
        const sym = p.symbol != null ? String(p.symbol) : "";
        const ac = p.asset_class != null ? String(p.asset_class) : "";
        let netStr = "—";
        if (p.net_qty != null && p.net_qty !== "") {
          const nq = Number(p.net_qty);
          netStr = Number.isFinite(nq) ? nq.toFixed(6) : String(p.net_qty);
        } else if (p.net_qty_fmt != null) {
          netStr = String(p.net_qty_fmt);
        }
        html += '<tr class="' + assetRowClass(sym) + '"><td>' + esc(ac) + '</td><td class="mono has-symbol" data-symbol="' + esc(sym) + '">' + esc(sym) + '</td><td class="mono">' + esc(netStr) + '</td></tr>';
      }
      tb.innerHTML = html;
    }
    function renderTrades(trades) {
      const tb = document.getElementById("tradesTableBody");
      const empty = document.getElementById("tradesEmpty");
      if (!tb) return;
      const rows = Array.isArray(trades) ? trades : [];
      if (!rows.length) {
        tb.innerHTML = "";
        if (empty) empty.style.display = "block";
        return;
      }
      if (empty) empty.style.display = "none";
      let html = "";
      for (const t of rows) {
        const sym = t.symbol != null ? String(t.symbol) : "";
        const sideRaw = t.side != null ? String(t.side) : "";
        const side = sideRaw.toLowerCase();
        const scls = side === "buy" ? "side-buy" : (side === "sell" ? "side-sell" : "");
        let st = t.status != null ? String(t.status) : "";
        if (t.reason_code != null && t.reason_code !== "") st += " (" + String(t.reason_code) + ")";
        const qty = t.quantity;
        html += '<tr class="' + assetRowClass(sym) + '"><td class="mono" style="font-size:0.72rem;">' + esc(fmtDate(t.created_at)) + '</td>';
        html += '<td class="mono has-symbol" data-symbol="' + esc(sym) + '">' + esc(sym) + '</td>';
        html += '<td class="mono ' + scls + '">' + esc(sideRaw) + '</td>';
        html += '<td class="mono">' + fmtNum(t.price, 4) + '</td><td class="mono">' + fmtNum(qty, 6) + '</td>';
        html += '<td class="mono">' + fmtNum(t.notional, 2) + '</td><td>' + esc(st) + '</td></tr>';
      }
      tb.innerHTML = html;
    }
    function applyLiveDashboard(data) {
      if (!data || typeof data !== "object") return;
      applyLiveTiles(data);
      const ser = Array.isArray(data.equity_series) ? data.equity_series : [];
      try { buildChart(ser); } catch (e) { console.error("buildChart", e); }
      try { buildSpark(ser); } catch (e) { console.error("buildSpark", e); }
      try { renderSignalFeed(data.recent_signals); } catch (e) { console.error("renderSignalFeed", e); }
      try { renderPositions(data.open_positions); } catch (e) { console.error("renderPositions", e); }
      try { renderTrades(data.recent_trades); } catch (e) { console.error("renderTrades", e); }
      const el = document.getElementById("dash-payload");
      if (el) el.textContent = JSON.stringify(data);
    }

    function fmtEtTime(d) {
      return d.toLocaleTimeString("en-US", { timeZone: TZ, hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" });
    }
    function etWeekdayShort(d) {
      return d.toLocaleDateString("en-US", { weekday: "short", timeZone: TZ });
    }
    function etHM(d) {
      const p = new Intl.DateTimeFormat("en-US", { timeZone: TZ, hour: "numeric", minute: "2-digit", hour12: false }).formatToParts(d);
      let h = 0, m = 0;
      for (const x of p) { if (x.type === "hour") h = +x.value; if (x.type === "minute") m = +x.value; }
      return h * 60 + m;
    }
    function isNyseOpenAt(d) {
      const w = etWeekdayShort(d);
      if (w === "Sat" || w === "Sun") return false;
      const mins = etHM(d);
      return mins >= 9 * 60 + 30 && mins < 16 * 60;
    }
    function fmtDur(ms) {
      if (ms < 0) ms = 0;
      const s = Math.floor(ms / 1000);
      const h = Math.floor(s / 3600);
      const m = Math.floor((s % 3600) / 60);
      if (h > 0) return h + "h " + m + "m";
      const sec = s % 60;
      if (m > 0) return m + "m " + sec + "s";
      return sec + "s";
    }
    function nextBoundaryFrom(now) {
      const step = 60000;
      const curOpen = isNyseOpenAt(now);
      let t = new Date(now.getTime() + step);
      const max = now.getTime() + 10 * 24 * 3600000;
      while (t.getTime() < max) {
        if (isNyseOpenAt(t) !== curOpen) return t;
        t = new Date(t.getTime() + step);
      }
      return null;
    }
    function tickClock() {
      const now = new Date();
      const el = document.getElementById("clockEt");
      if (el) el.textContent = fmtEtTime(now) + " ET";
      const open = (typeof __lastDashMarketOpen === "boolean") ? __lastDashMarketOpen : isNyseOpenAt(now);
      const line = document.getElementById("mktLine");
      const cd = document.getElementById("mktCd");
      if (line) {
        line.textContent = open ? "OPEN" : "CLOSED";
        line.className = open ? "market-open" : "market-closed";
      }
      if (cd) {
        const nb = nextBoundaryFrom(now);
        if (nb) {
          const label = open ? "CLOSES IN " : "OPENS IN ";
          cd.textContent = label + fmtDur(nb.getTime() - now.getTime());
        } else cd.textContent = "";
      }
      updateDashSyncStatus();
    }
    function updateDashSyncStatus() {
      const lu = document.getElementById("last-sync");
      if (!lu) return;
      if (window.__dashWsConnected) {
        lu.textContent = "Live ⚡";
        lu.className = "muted sync-live";
        return;
      }
      if (!window.__dashWsEnabled) {
        if (lastPollMs) {
          const ago = Math.floor((Date.now() - lastPollMs) / 1000);
          lu.textContent = "Last sync: " + ago + "s ago";
          lu.className = "muted";
        }
        return;
      }
      lu.textContent = "Reconnecting…";
      lu.className = "muted sync-reconnect";
    }
    function startHttpFallbackPoll() {
      if (window.__dashPollTimer) return;
      window.__dashPollTimer = setInterval(poll, REFRESH_MS);
    }
    function stopHttpFallbackPoll() {
      if (window.__dashPollTimer) {
        clearInterval(window.__dashPollTimer);
        window.__dashPollTimer = null;
      }
    }
    setInterval(tickClock, 1000);
    tickClock();

    function renderSocial(rows) {
      const root = document.getElementById("socialMoRoot");
      if (!root) return;
      if (!Array.isArray(rows) || !rows.length) {
        root.innerHTML = '<p class="muted">No Reddit data</p>';
        return;
      }
      let html = '<table class="social-table"><thead><tr><th>Ticker</th><th>Mentions</th><th>Rank Δ</th><th>%Δ mentions</th><th>Source</th></tr></thead><tbody>';
      for (const r of rows) {
        const t = r.ticker != null ? String(r.ticker) : "";
        const br = r.is_breakout === true || r.is_breakout === 1 || r.is_breakout === "1";
        const tcls = br ? "mono breakout-name has-symbol" : "mono has-symbol";
        const rcRaw = r.rank_change;
        const rc = rcRaw === null || rcRaw === undefined || rcRaw === "" ? 0 : Number(rcRaw);
        const rcSafe = Number.isFinite(rc) ? rc : 0;
        const rcls = rcSafe > 0 ? "rc-up" : (rcSafe < 0 ? "rc-down" : "muted");
        const arr = rcSafe > 0 ? "▲ " : (rcSafe < 0 ? "▼ " : "— ");
        const src = r.source != null ? String(r.source) : "";
        let mp = "—";
        if (r.mentions_change_pct != null && r.mentions_change_pct !== "") {
          const mpn = Number(r.mentions_change_pct);
          mp = Number.isFinite(mpn) ? mpn.toFixed(1) + "%" : esc(String(r.mentions_change_pct));
        }
        const men = r.mentions != null && r.mentions !== "" ? String(r.mentions) : "—";
        html += '<tr><td class="' + tcls + '" data-symbol="' + esc(t) + '">' + esc(t) + '</td><td class="mono">' + esc(men) + '</td>';
        html += '<td class="mono ' + rcls + '">' + arr + rcSafe + '</td><td class="mono">' + mp + '</td><td style="font-size:0.72rem;">' + esc(src) + '</td></tr>';
      }
      html += "</tbody></table>";
      root.innerHTML = html;
    }
    async function pollSocial() {
      const root = document.getElementById("socialMoRoot");
      try {
        const res = await fetch("/api/social", { cache: "no-store" });
        if (!res.ok) throw new Error("social HTTP " + res.status);
        const data = await res.json();
        const rows = Array.isArray(data) ? data : [];
        try {
          renderSocial(rows);
        } catch (e) {
          console.error("renderSocial", e);
          if (root) root.innerHTML = '<p class="muted">Social render error (see console).</p>';
        }
      } catch (e) {
        console.error("pollSocial", e);
        if (root) root.innerHTML = '<p class="muted">Social feed unavailable.</p>';
      }
    }
    pollSocial();
    setInterval(pollSocial, 60000);

    function buildSpark(series) {
      const pts = (series || []).slice(-32);
      const labels = pts.map((r) => "");
      const data = pts.map((r) => equityY(r));
      const ctx = document.getElementById("sparkEq");
      if (!ctx) return;
      if (spark) spark.destroy();
      spark = new Chart(ctx, {
        type: "line",
        data: { labels, datasets: [{ data, borderColor: "#00d4ff", backgroundColor: "rgba(0,212,255,0.12)", fill: true, tension: 0.35, pointRadius: 0 }] },
        options: {
          responsive: true, maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: { x: { display: false }, y: { display: false } },
        },
      });
    }
    function buildChart(series) {
      let ser = Array.isArray(series) ? series.slice() : [];
      let labels = ser.map((r) => equityLabel(r));
      let data = ser.map((r) => equityY(r));
      if (!data.length) {
        labels = ["", ""];
        data = [20000, 20000];
      }
      const ctx = document.getElementById("eqChart");
      if (chart) chart.destroy();
      chart = new Chart(ctx, {
        type: "line",
        data: {
          labels,
          datasets: [{
            label: "Equity",
            data,
            borderColor: "#00d4ff",
            backgroundColor: "rgba(0, 212, 255, 0.08)",
            fill: true,
            tension: 0.35,
            pointRadius: 0,
            pointHoverRadius: 0,
          }],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: {
            x: { ticks: { maxTicksLimit: 8, color: "#7986cb" } },
            y: { ticks: { color: "#7986cb" } },
          },
        },
      });
    }
    const boot = readPayload();
    if (typeof boot.market_open === "boolean") __lastDashMarketOpen = boot.market_open;
    tickClock();
    try { applyLiveDashboard(boot); } catch (e) { console.error("initial dashboard render", e); }

    function positionTooltip(ev, tip) {
      const pad = 15;
      let x = ev.clientX + pad, y = ev.clientY + pad;
      const w = tip.offsetWidth || 260;
      const h = tip.offsetHeight || 120;
      if (x + w > window.innerWidth - 8) x = window.innerWidth - w - 8;
      if (y + h > window.innerHeight - 8) y = window.innerHeight - h - 8;
      tip.style.left = x + "px";
      tip.style.top = y + "px";
    }
    function renderTooltipHtml(info) {
      const typ = (info.type || "").toLowerCase();
      const badge = typ === "crypto"
        ? '<span class="tt-type-c">CRYPTO</span>' : '<span class="tt-type-s">STOCK</span>';
      let line2 = "";
      if (typ === "crypto") {
        const rk = info.market_cap_rank;
        line2 = rk != null ? ("Rank #" + esc(String(rk)) + " by market cap") : "";
      } else {
        line2 = esc(info.exchange || "");
      }
      let line3 = "";
      if (info.current_price != null && Number.isFinite(Number(info.current_price))) {
        line3 = '<div class="tt-price">' + fmtNum(info.current_price, 4);
        if (info.previous_close != null && Number.isFinite(Number(info.previous_close))) {
          line3 += ' <span style="color:var(--text-secondary);">prev ' + fmtNum(info.previous_close, 4) + '</span>';
        }
        line3 += '</div>';
      }
      const thumb = (typ === "crypto" && info.thumb) ? '<img src="' + esc(info.thumb) + '" width="20" height="20" style="vertical-align:middle;border-radius:4px;margin-right:6px;" alt=""/>' : "";
      return thumb + '<div><span class="tt-name">' + esc(info.name || info.symbol) + '</span>' + badge + '</div>'
        + '<div class="tt-line2">' + line2 + '</div>' + line3
        + '<div class="tt-desc">' + esc(info.description || "") + '</div>';
    }
    function setupSymbolTooltips() {
      const tip = document.getElementById("sym-tooltip");
      if (!tip) return;
      document.body.addEventListener("mousemove", (ev) => {
        const el = ev.target;
        if (!el || !el.closest) return;
        const cell = el.closest("[data-symbol]");
        if (!cell) {
          if (__symHoverLast) {
            tip.style.display = "none";
            __symHoverLast = "";
          }
          return;
        }
        const sym = cell.getAttribute("data-symbol");
        if (!sym) return;
        if (sym !== __symHoverLast) {
          __symHoverLast = sym;
          tip.style.display = "block";
          tip.innerHTML = '<div class="tt-desc">Loading symbol data…</div>';
          positionTooltip(ev, tip);
          if (__tooltipFetchTimer) clearTimeout(__tooltipFetchTimer);
          __tooltipFetchTimer = setTimeout(() => {
            if (__symHoverLast === sym) tip.innerHTML = '<div class="tt-desc">Loading symbol data…</div>';
          }, 3000);
          const cache = window._symbolCache;
          if (cache[sym]) {
            clearTimeout(__tooltipFetchTimer);
            tip.innerHTML = renderTooltipHtml(cache[sym]);
            positionTooltip(ev, tip);
            return;
          }
          const url = "/api/symbol/" + encodeURIComponent(sym);
          const ac = new AbortController();
          const to = setTimeout(() => ac.abort(), 5000);
          fetch(url, { signal: ac.signal }).then((r) => r.json()).then((j) => {
            clearTimeout(to);
            clearTimeout(__tooltipFetchTimer);
            cache[sym] = j;
            if (__symHoverLast === sym) {
              tip.innerHTML = renderTooltipHtml(j);
              positionTooltip(ev, tip);
            }
          }).catch(() => {
            clearTimeout(to);
            clearTimeout(__tooltipFetchTimer);
            if (__symHoverLast === sym) tip.innerHTML = '<div class="tt-desc">Live data unavailable.</div>';
          });
        } else {
          positionTooltip(ev, tip);
        }
      });
      document.body.addEventListener("mouseleave", () => {
        tip.style.display = "none";
        __symHoverLast = "";
      });
    }
    setupSymbolTooltips();

    async function poll() {
      try {
        const r = await fetch("/api/dashboard", { cache: "no-store" });
        const j = await r.json();
        applyLiveDashboard(j);
        lastPollMs = Date.now();
        if (!window.__dashWsConnected) updateDashSyncStatus();
        document.querySelectorAll(".sig-feed-row").forEach((row) => { row.classList.add("row-flash"); });
        setTimeout(() => document.querySelectorAll(".sig-feed-row").forEach((row) => row.classList.remove("row-flash")), 650);
      } catch (e) { console.warn(e); }
    }
    if (window.__dashWsEnabled) {
      const dashSocket = io({ transports: ["websocket", "polling"] });
      dashSocket.on("connect", function () {
        window.__dashWsConnected = true;
        updateDashSyncStatus();
        stopHttpFallbackPoll();
      });
      dashSocket.on("disconnect", function () {
        window.__dashWsConnected = false;
        updateDashSyncStatus();
        startHttpFallbackPoll();
      });
      dashSocket.on("dashboard_update", function (data) {
        try {
          applyLiveDashboard(data);
        } catch (e) {
          console.error("dashboard_update", e);
        }
        lastPollMs = Date.now();
        updateDashSyncStatus();
        document.querySelectorAll(".sig-feed-row").forEach((row) => { row.classList.add("row-flash"); });
        setTimeout(() => document.querySelectorAll(".sig-feed-row").forEach((row) => row.classList.remove("row-flash")), 650);
      });
    } else {
      console.warn("Socket.IO client not loaded; using HTTP poll only");
    }
    if (!window.__dashWsEnabled) {
      startHttpFallbackPoll();
    }
    poll();
    updateDashSyncStatus();

    function bindCfg() {
      document.querySelectorAll(".cfg-range").forEach((r) => {
        r.oninput = () => {
          const k = r.dataset.key;
          const n = document.querySelector('.cfg-num[data-key="' + k + '"]');
          if (n) n.value = r.value;
        };
      });
      document.querySelectorAll(".cfg-num").forEach((n) => {
        n.oninput = () => {
          const k = n.dataset.key;
          const r = document.querySelector('.cfg-range[data-key="' + k + '"]');
          if (r) r.value = n.value;
        };
      });
      document.querySelectorAll(".cfg-save").forEach((btn) => {
        btn.onclick = async () => {
          const k = btn.dataset.key;
          const n = document.querySelector('.cfg-num[data-key="' + k + '"]');
          const v = parseFloat(n.value);
          const res = await fetch("/api/config", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ key: k, value: v }),
          });
          if (res.ok) {
            btn.textContent = "Saved";
            setTimeout(() => { btn.textContent = "Save"; }, 1200);
          }
        };
      });
      const rst = document.getElementById("cfg-reset");
      if (rst) {
        rst.onclick = async () => {
          if (!confirm("Reset all bot parameters to defaults?")) return;
          await fetch("/api/config/reset", { method: "POST" });
          location.reload();
        };
      }
    }
    bindCfg();
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
    "max_position_pct": (0.02, 0.25, 0.01),
}


def _bot_ui_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in rows:
        key = str(r["key"])
        if key.startswith("rl_"):
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


def create_app() -> Flask:
    from data import data_store
    from data.data_store import get_connection, init_schema
    from monitoring.dashboard_data import build_dashboard_payload

    init_schema()
    app = Flask(__name__)

    from flask_socketio import SocketIO

    preferred_async = os.environ.get("SOCKETIO_ASYNC_MODE", "eventlet")
    try:
        socketio = SocketIO(app, cors_allowed_origins="*", async_mode=preferred_async)
    except (ValueError, ImportError, ModuleNotFoundError):
        preferred_async = "threading"
        socketio = SocketIO(app, cors_allowed_origins="*", async_mode=preferred_async)
    app.config["SOCKETIO_ASYNC_MODE"] = preferred_async

    def _dashboard_ws_push() -> None:
        sio = app.extensions["socketio"]
        while True:
            try:
                with app.app_context():
                    with get_connection() as conn:
                        payload = build_dashboard_payload(conn)
                sio.emit("dashboard_update", payload)
            except Exception:
                logger.exception("[ws] push error")
            sio.sleep(2)

    app.extensions["socketio"] = socketio
    if not app.config.get("TESTING"):
        socketio.start_background_task(_dashboard_ws_push)

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

    @app.get("/api/config")
    def api_config_get() -> Response:
        with get_connection() as conn:
            rows = data_store.fetch_all_bot_config_rows(conn)
        return Response(json.dumps(rows, default=str), mimetype="application/json")

    @app.post("/api/config")
    def api_config_post() -> tuple[dict[str, Any], int]:
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
        data_store.reset_bot_config_to_defaults()
        return {"ok": True}, 200

    @app.post("/api/reset-db")
    def api_reset_db() -> Any:
        """Admin: wipe trade history and rescale bot_config keys from defaults."""
        try:
            result = data_store.reset_trading_history(str(config.DB_PATH))
            return jsonify({"status": "ok", "result": result})
        except Exception as e:
            logger.exception("api/reset-db failed")
            return jsonify({"status": "error", "message": str(e)}), 500

    @app.post("/api/sync-alpaca")
    def api_sync_alpaca() -> Any:
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
        with get_connection() as conn:
            payload = build_dashboard_payload(conn)
            bot_ui = _bot_ui_rows(data_store.fetch_all_bot_config_rows(conn))
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
        dash_snapshot = dict(payload)
        perf = payload.get("performance") or {}
        rl_history = payload.get("rl_learning_history") or []
        calibration = payload.get("calibration") or {}
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
            dash_snapshot=dash_snapshot,
            bot_ui=bot_ui,
            perf=perf,
            rl_history=rl_history,
            calibration=calibration,
        )

    return app


def run_dashboard() -> None:
    port = int(os.environ.get("PORT", "5000"))
    app = create_app()
    sio = app.extensions.get("socketio")
    mode = app.config.get("SOCKETIO_ASYNC_MODE", "?")
    logger.info(
        "Monitoring dashboard | http://0.0.0.0:{} (SocketIO async_mode={} · HTTP fallback {}s)",
        port,
        mode,
        _REFRESH_SEC,
    )
    if sio is not None:
        sio.run(app, host="0.0.0.0", port=port, debug=False, use_reloader=False)
    else:
        app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    app = create_app()
    port = int(os.environ.get("PORT", 5000))
    sio = app.extensions.get("socketio")
    if sio is not None:
        sio.run(app, host="0.0.0.0", port=port, debug=False, use_reloader=False)
    else:
        app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
