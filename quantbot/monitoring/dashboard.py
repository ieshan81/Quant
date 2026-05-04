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

from flask import Flask, Response, render_template_string, request
from loguru import logger

import config

_REFRESH_SEC = 30

_PAGE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <meta http-equiv="refresh" content="{{ refresh_sec }}"/>
  <title>QuantBot — Terminal</title>
  <link rel="preconnect" href="https://fonts.googleapis.com"/>
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet"/>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
  <style>
    :root {
      --bg-primary: #0a0e1a;
      --bg-secondary: #0f1629;
      --bg-card: rgba(255, 255, 255, 0.03);
      --border: rgba(255, 255, 255, 0.08);
      --border-bright: rgba(0, 212, 255, 0.3);
      --accent-blue: #00d4ff;
      --accent-green: #00ff88;
      --accent-red: #ff4466;
      --accent-gold: #ffd700;
      --text-primary: #e8eaf6;
      --text-secondary: #7986cb;
      --text-muted: #3d4a6b;
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
    .mono { font-family: "JetBrains Mono", ui-monospace, monospace; }
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
    .clock-et { font-family: "JetBrains Mono", monospace; font-size: 1.1rem; color: var(--accent-blue); }
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
    .big { font-size: 1.65rem; font-weight: 700; font-family: "JetBrains Mono", monospace; }
    .pos { color: var(--accent-green); }
    .neg { color: var(--accent-red); }
    .muted { color: var(--text-muted); font-size: 0.78rem; }
    .spark-wrap { height: 48px; margin-top: 0.35rem; }

    .market-open { color: var(--accent-green); font-weight: 700; font-family: "JetBrains Mono", monospace; }
    .market-closed { color: var(--accent-red); font-weight: 700; font-family: "JetBrains Mono", monospace; }
    .countdown { font-size: 0.8rem; color: var(--text-secondary); margin-top: 0.35rem; font-family: "JetBrains Mono", monospace; }

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
      font-family: "JetBrains Mono", monospace;
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
  </style>
</head>
<body data-terminal="1">
  <span class="quantbot-terminal"></span>
  <header class="term-header">
    <div class="brand"><span class="live-dot" title="live"></span><span class="mono">⚡ QUANTBOT</span></div>
    <div class="header-center"><div class="clock-et" id="clockEt">—</div><div class="muted" id="lastUpd" style="font-size:0.68rem;margin-top:0.2rem;">Last sync: —</div></div>
    <div class="badge-paper">PAPER TRADING</div>
  </header>
  <div class="wrap">
    <div class="stats-row">
      <div class="card"><h2>Live P&amp;L</h2><div class="big {{ pnl_class }}">{{ pnl_str }}</div></div>
      <div class="card"><h2>Total equity</h2><div class="big mono">{{ eq_str }}</div><div class="spark-wrap"><canvas id="sparkEq"></canvas></div></div>
      <div class="card"><h2>Mode</h2><div class="big mono">{{ mode_str }}</div><p class="muted" style="margin:0.35rem 0 0;">DB: {{ db }}</p></div>
      <div class="card"><h2>Market (NYSE)</h2><div id="mktLine" class="market-closed">…</div><div class="countdown" id="mktCd"></div></div>
    </div>

    <div class="card" style="margin-top:1rem;"><h2>Equity curve</h2><div class="chart-wrap"><canvas id="eqChart"></canvas></div></div>

    <div class="mid-grid">
      <div class="card signal-feed">
        <h2>Signal feed</h2>
        {% if signals %}
        <div style="overflow-x:auto;">
        <table><thead><tr><th></th><th>Time</th><th>Symbol</th><th>Signal</th><th>Score</th><th></th></tr></thead><tbody id="sigFeedBody">
          {% for s in signals %}
          <tr class="sig-feed-row {{ s.score_row_class }}" data-sig-id="{{ s.id }}">
            <td class="mono {{ s.dir_class }}">{{ s.dir_arrow }}</td>
            <td class="mono" style="font-size:0.72rem;color:var(--text-secondary);">{{ s.created_at }}</td>
            <td class="mono" style="font-weight:700;">{{ s.symbol }}</td>
            <td>{{ s.signal_name }}</td>
            <td class="score-cell">
              <span class="score-txt mono">{{ s.score_fmt }}</span>
              <div class="score-bar-bg"><div class="score-bar-fill" style="width: {{ s.score_bar_pct }}%;"></div></div>
            </td>
            <td>
              {% if s.action_badge == 'BUY' %}<span class="action-pill pill-buy">BUY</span>
              {% elif s.action_badge == 'SELL' %}<span class="action-pill pill-sell">SELL</span>
              {% else %}<span class="action-pill pill-hold">HOLD</span>{% endif %}
            </td>
          </tr>
          {% endfor %}
        </tbody></table>
        </div>
        {% else %}<p class="muted">No signals logged.</p>{% endif %}
      </div>
      <div class="card social-panel">
        <h2><span>🔥</span> Social momentum</h2>
        <p class="muted" style="margin-top:0;">Top 10 · <a href="/api/social">/api/social</a></p>
        <div id="socialMoRoot" style="margin-top:0.5rem;"><p class="muted">Loading…</p></div>
      </div>
    </div>

    <div class="subrow">
      <div class="card"><h2>Open positions</h2>
        {% if positions %}<table class="data-table"><thead><tr><th>Class</th><th>Symbol</th><th>Net</th></tr></thead><tbody>
          {% for p in positions %}<tr><td>{{ p.asset_class }}</td><td class="mono">{{ p.symbol }}</td><td class="mono">{{ p.net_qty_fmt }}</td></tr>{% endfor %}
        </tbody></table>{% else %}<p class="muted">No open positions.</p>{% endif %}
      </div>
      <div class="card"><h2>Recent trades</h2>
        {% if trades %}<table class="data-table"><thead><tr><th>Time</th><th>Sym</th><th>Side</th><th>Qty</th><th>Status</th></tr></thead><tbody>
          {% for t in trades %}<tr><td class="mono" style="font-size:0.72rem;">{{ t.created_at }}</td><td class="mono">{{ t.symbol }}</td><td>{{ t.side }}</td><td class="mono">{{ t.qty_fmt }}</td><td>{{ t.status }}{% if t.reason_code %} ({{ t.reason_code }}){% endif %}</td></tr>{% endfor %}
        </tbody></table>{% else %}<p class="muted">No trades yet.</p>{% endif %}
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

    <p class="api-links">JSON: <a href="/api/dashboard">/api/dashboard</a> · <a href="/api/config">/api/config</a> · <a href="/api/calibration">/api/calibration</a> · <a href="/api/social">/api/social</a></p>
    <p class="last-upd" id="metaNote">Page meta-refresh: {{ refresh_sec }}s · live clock ET</p>
  </div>

  <script id="dash-payload" type="application/json">{{ chart_data|tojson }}</script>
  <script>
    const REFRESH_MS = {{ refresh_sec }} * 1000;
    const TZ = "America/New_York";
    let chart, spark;
    let lastPollMs = 0;

    function readPayload() {
      const el = document.getElementById("dash-payload");
      return JSON.parse(el.textContent || "{}");
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
    function pad(n) { return String(n).padStart(2, "0"); }
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
      const open = isNyseOpenAt(now);
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
      if (lastPollMs) {
        const ago = Math.floor((Date.now() - lastPollMs) / 1000);
        const lu = document.getElementById("lastUpd");
        if (lu) lu.textContent = "Last dashboard sync: " + ago + "s ago";
      }
    }
    setInterval(tickClock, 1000);
    tickClock();

    function renderSocial(rows) {
      const root = document.getElementById("socialMoRoot");
      if (!root) return;
      if (!rows || !rows.length) {
        root.innerHTML = "<p class=\"muted\">No momentum data (worker persists to SQLite).</p>";
        return;
      }
      let html = "<table class=\"social-table\"><thead><tr><th>Ticker</th><th>Mentions</th><th>Rank Δ</th><th>%Δ mentions</th><th>Source</th><th></th></tr></thead><tbody>";
      for (const r of rows) {
        const t = (r.ticker || "").toString();
        const br = !!r.is_breakout;
        const tcls = br ? "mono breakout-name" : "mono";
        const rc = Number(r.rank_change) || 0;
        const rcls = rc > 0 ? "rc-up" : (rc < 0 ? "rc-down" : "muted");
        const arr = rc > 0 ? "▲ " : (rc < 0 ? "▼ " : "— ");
        const mp = (r.mentions_change_pct != null && r.mentions_change_pct !== "") ? Number(r.mentions_change_pct).toFixed(1) + "%" : "—";
        const src = (r.source || "").toString();
        const badge = br ? "<span class=\"action-pill\" style=\"border:1px solid var(--accent-gold);color:var(--accent-gold);\">BREAKOUT</span>" : "";
        html += "<tr><td class=\"" + tcls + "\">" + t + "</td><td class=\"mono\">" + (r.mentions ?? "—") + "</td>";
        html += "<td class=\"mono " + rcls + "\">" + arr + rc + "</td><td class=\"mono\">" + mp + "</td><td style=\"font-size:0.72rem;\">" + src + "</td><td>" + badge + "</td></tr>";
      }
      html += "</tbody></table>";
      root.innerHTML = html;
    }
    async function pollSocial() {
      try {
        const res = await fetch("/api/social", { cache: "no-store" });
        const data = await res.json();
        renderSocial(data);
      } catch (e) {
        const root = document.getElementById("socialMoRoot");
        if (root) root.innerHTML = "<p class=\"muted\">Social feed unavailable.</p>";
      }
    }
    pollSocial();
    setInterval(pollSocial, 60000);

    function buildSpark(series) {
      const pts = (series || []).slice(-32);
      const labels = pts.map((r) => "");
      const data = pts.map((r) => Number(r.equity_total) || 0);
      const ctx = document.getElementById("sparkEq");
      if (!ctx) return;
      if (spark) spark.destroy();
      spark = new Chart(ctx, {
        type: "line",
        data: { labels, datasets: [{ data, borderColor: "#00d4ff", backgroundColor: "rgba(0,212,255,0.12)", fill: true, tension: 0.3, pointRadius: 0 }] },
        options: {
          responsive: true, maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: { x: { display: false }, y: { display: false } },
        },
      });
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
            borderColor: "#00d4ff",
            backgroundColor: "rgba(0, 212, 255, 0.12)",
            fill: true,
            tension: 0.2,
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
    buildChart(boot.equity_series || []);
    buildSpark(boot.equity_series || []);

    async function poll() {
      try {
        const r = await fetch("/api/dashboard", { cache: "no-store" });
        const j = await r.json();
        const el = document.getElementById("dash-payload");
        el.textContent = JSON.stringify(j);
        buildChart(j.equity_series || []);
        buildSpark(j.equity_series || []);
        lastPollMs = Date.now();
        document.querySelectorAll(".sig-feed-row").forEach((row) => { row.classList.add("row-flash"); });
        setTimeout(() => document.querySelectorAll(".sig-feed-row").forEach((row) => row.classList.remove("row-flash")), 650);
      } catch (e) { console.warn(e); }
    }
    setInterval(poll, REFRESH_MS);

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
        chart_data = {"equity_series": payload.get("equity_series") or []}
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
            chart_data=chart_data,
            bot_ui=bot_ui,
            perf=perf,
            rl_history=rl_history,
            calibration=calibration,
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
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    app = create_app()
    port = int(os.environ.get('PORT', 5000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
