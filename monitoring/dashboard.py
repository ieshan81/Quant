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
from datetime import datetime, timezone
from typing import Any

from flask import Flask, Response, jsonify, render_template_string, request
from loguru import logger

import config

_REFRESH_SEC = 30
DASHBOARD_SECRET = os.environ.get("DASHBOARD_SECRET", "")

_PAGE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
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
    .chart-controls { display: flex; gap: 8px; margin-bottom: 8px; }
    .range-btn {
      background: #1e293b; color: #64748b; border: 1px solid #1e293b;
      padding: 4px 12px; border-radius: 4px; cursor: pointer; font-size: 12px;
    }
    .range-btn.active {
      background: rgba(0,255,136,0.1); color: #00ff88;
      border-color: #00ff88;
    }
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

    .tab-nav {
      display: flex;
      gap: 0.5rem;
      max-width: 1700px;
      margin: 0 auto;
      padding: 0.75rem 1.1rem 0;
    }
    .tab-nav .tab-btn {
      background: #1e293b;
      color: #94a3b8;
      border: 1px solid #334155;
      border-radius: 8px;
      padding: 6px 12px;
      cursor: pointer;
      font-family: inherit;
      font-size: 0.875rem;
    }
    .tab-nav .tab-btn.active {
      color: #00ff88;
      border-color: #00ff88;
      background: rgba(0, 255, 136, 0.1);
    }
    .tab-panel { display: none; }
    .tab-panel.active { display: block; }

    /* Dashboard grid — scoped so Backtest tab never inherits 12-col placement rules */
    .dashboard-wrap {
      max-width: 1700px;
      margin: 0 auto;
      padding: 1rem 1.1rem 1.4rem;
      display: grid;
      gap: 1rem;
      grid-template-columns: repeat(12, minmax(0, 1fr));
    }
    #dashboard-tab .card {
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: 10px;
      box-shadow: none;
    }
    .dashboard-wrap .stats-row {
      grid-column: 1 / -1;
      display: grid;
      grid-template-columns: repeat(12, minmax(0, 1fr));
      gap: 1rem;
      margin-top: 0;
    }
    .dashboard-wrap .stats-row > .card { grid-column: span 3; }
    .dashboard-wrap .chart-main { grid-column: 1 / span 8; margin-top: 0 !important; }
    .dashboard-wrap .social-panel { grid-column: 9 / -1; }
    .dashboard-wrap .signal-feed { grid-column: 1 / span 8; }
    .dashboard-wrap .subrow {
      grid-column: 9 / -1;
      margin-top: 0;
      display: grid;
      grid-template-columns: 1fr;
      gap: 1rem;
    }
    .dashboard-wrap .bottom-grid {
      grid-column: 1 / -1;
      margin-top: 0;
      display: grid;
      grid-template-columns: repeat(12, minmax(0, 1fr));
      gap: 1rem;
    }
    .dashboard-wrap .bottom-grid > .card:nth-child(1) { grid-column: span 6; }
    .dashboard-wrap .bottom-grid > .card:nth-child(2) { grid-column: span 6; }
    .dashboard-wrap .bottom-grid > .card:nth-child(3) { grid-column: 1 / -1; }
    .dashboard-wrap .exec-health-card {
      grid-column: 1 / -1;
      margin-top: 0;
    }
    .exec-health-grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 0.65rem;
      margin-top: 0.35rem;
    }
    @media (max-width: 1100px) {
      .exec-health-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
    @media (max-width: 560px) {
      .exec-health-grid { grid-template-columns: 1fr; }
    }
    .exec-health-tile {
      background: rgba(0, 0, 0, 0.22);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 0.55rem 0.65rem;
      min-height: 3.25rem;
    }
    .exec-health-tile.exec-health-warn {
      border-color: rgba(251, 191, 36, 0.55);
      background: rgba(251, 191, 36, 0.06);
    }
    .exec-health-tile-wide {
      grid-column: span 2;
    }
    @media (max-width: 560px) {
      .exec-health-tile-wide { grid-column: span 1; }
    }
    .exec-health-tile-label {
      font-size: 0.68rem;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.04em;
      margin-bottom: 0.25rem;
    }
    .exec-health-tile-value { font-size: 1.05rem; font-weight: 600; }
    .exec-health-tile-sm { font-size: 0.82rem; word-break: break-word; }
    .exec-health-card.exec-health-card-warn {
      border-color: rgba(251, 191, 36, 0.45);
      box-shadow: 0 0 0 1px rgba(251, 191, 36, 0.12);
    }
    .exec-health-pdt-wrap {
      display: flex;
      flex-wrap: wrap;
      gap: 0.35rem;
      align-items: center;
      min-height: 1.35rem;
    }
    .exec-health-badge {
      display: inline-block;
      padding: 0.15rem 0.45rem;
      border-radius: 6px;
      font-size: 0.72rem;
      font-weight: 700;
      font-family: "JetBrains Mono", monospace;
      background: rgba(251, 191, 36, 0.12);
      border: 1px solid rgba(251, 191, 36, 0.35);
      color: #fcd34d;
    }
    .exec-health-hint {
      margin: 0.35rem 0 0;
      font-size: 0.72rem;
      line-height: 1.35;
    }
    .exec-exit-details { margin-top: 0.85rem; }
    .exec-exit-summary {
      cursor: pointer;
      font-weight: 600;
      font-size: 0.85rem;
      color: var(--accent-blue);
      list-style: none;
    }
    .exec-exit-summary::-webkit-details-marker { display: none; }
    .exec-exit-details[open] .exec-exit-summary { margin-bottom: 0.25rem; }
    .dashboard-wrap .api-links,
    .dashboard-wrap .last-upd { grid-column: 1 / -1; margin-top: 0; }
    @media (max-width: 1100px) {
      .dashboard-wrap .stats-row > .card { grid-column: span 6; }
      .dashboard-wrap .chart-main,
      .dashboard-wrap .social-panel,
      .dashboard-wrap .signal-feed,
      .dashboard-wrap .subrow,
      .dashboard-wrap .bottom-grid > .card:nth-child(1),
      .dashboard-wrap .bottom-grid > .card:nth-child(2),
      .dashboard-wrap .bottom-grid > .card:nth-child(3) {
        grid-column: 1 / -1;
      }
    }

    .backtest-wrap {
      max-width: 1400px;
      margin: 0 auto;
      padding: 1rem 1.1rem 2rem;
      display: grid;
      grid-template-columns: repeat(12, minmax(0, 1fr));
      gap: 1rem;
    }
    .backtest-wrap .card.bt-card-setup,
    .backtest-wrap .card.bt-card-actions,
    .backtest-wrap .card.bt-card-summary,
    .backtest-wrap .card.bt-card-interpretation,
    .backtest-wrap .card.bt-card-chart { grid-column: 1 / -1; }
    .backtest-wrap .card.bt-card-trades,
    .backtest-wrap .card.bt-card-rejections,
    .backtest-wrap .card.bt-card-runs,
    .backtest-wrap .card.bt-card-assumptions { grid-column: span 6; }
    @media (max-width: 1000px) {
      .backtest-wrap > .card { grid-column: 1 / -1 !important; }
    }
    .bt-action-btn {
      background: rgba(0, 212, 255, 0.12);
      border: 1px solid var(--border-bright);
      color: var(--accent-blue);
      border-radius: 8px;
      padding: 0.48rem 0.85rem;
      cursor: pointer;
      font-weight: 600;
    }
    .bt-action-btn:disabled { opacity: 0.55; cursor: not-allowed; }
    .bt-action-btn.bt-primary {
      background: linear-gradient(180deg, rgba(0, 255, 136, 0.20), rgba(0, 255, 136, 0.1));
      color: #e8fff5;
      border-color: #00ff88;
      font-size: 1rem;
      padding: 0.7rem 1rem;
    }
    .bt-action-btn.bt-primary:hover { filter: brightness(1.1); }
    .bt-setup-grid {
      display: grid;
      grid-template-columns: repeat(6, minmax(0, 1fr));
      gap: 0.6rem;
    }
    .bt-setup-grid label { font-size: 0.75rem; color: var(--text-secondary); display: block; }
    .bt-setup-grid input, .bt-setup-grid select {
      width: 100%;
      background: #0b1220; color: var(--text-primary);
      border: 1px solid var(--border); border-radius: 8px; padding: 0.45rem 0.55rem;
    }
    .bt-actions-row { display: flex; flex-wrap: wrap; gap: 0.6rem; align-items: center; }
    .bt-status { margin-top: 0.5rem; font-size: 0.85rem; }
    .bt-status.ok { color: #22c55e; }
    .bt-status.err { color: #ef4444; }
    .bt-mini-card {
      background: rgba(15, 23, 42, 0.55);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 0.45rem 0.55rem;
      min-width: 140px;
    }
    .bt-mini-card .label {
      color: var(--text-secondary);
      font-size: 0.7rem;
      margin-bottom: 0.2rem;
    }
    .bt-mini-card .value {
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 0.92rem;
      color: var(--text-main);
    }
    .dash-api-err { max-width: 42rem; line-height: 1.35; }
  </style>
</head>
<body data-terminal="1">
  <span class="quantbot-terminal"></span>
  <header class="term-header">
    <div class="brand"><span class="live-dot" title="live"></span><span class="mono">⚡ QUANTBOT</span></div>
    <div class="header-center">
      <div class="clock-et" id="clockEt">—</div>
      <div class="muted sync-reconnect" id="last-sync" style="font-size:0.68rem;margin-top:0.2rem;">Starting…</div>
      <div id="dash-api-error" class="dash-api-err" style="display:none;font-size:0.68rem;color:#f87171;margin-top:0.15rem;"></div>
      <div class="sym-legend muted" title="Symbol coloring in tables below">
        <span class="legend-stock">● STOCK</span>
        <span class="legend-crypto">● CRYPTO</span>
      </div>
    </div>
    <div class="badge-paper">PAPER TRADING</div>
  </header>
  <nav class="tab-nav" aria-label="Primary">
    <button type="button" class="tab-btn active" data-tab="dashboard">Dashboard</button>
    <button type="button" class="tab-btn" data-tab="backtest">Backtest</button>
  </nav>

  <main id="dashboard-tab" class="tab-panel active">
    <div class="dashboard-wrap">
    <div class="stats-row">
      <div class="card"><h2>Live P&amp;L</h2><div class="big {{ pnl_class }}" id="tilePnl">{{ pnl_str }}</div></div>
      <div class="card"><h2>Total equity</h2><div class="big mono" id="tileEq">{{ eq_str }}</div><div class="spark-wrap"><canvas id="sparkEq"></canvas></div></div>
      <div class="card"><h2>Mode</h2><div class="big mono" id="tileMode">{{ mode_str }}</div><p class="muted" style="margin:0.35rem 0 0;">DB: {{ db }}</p></div>
      <div class="card"><h2>Market (NYSE)</h2><div id="mktLine" class="market-closed">…</div><div class="countdown" id="mktCd"></div></div>
    </div>

    <div class="card chart-main">
      <h2>Equity curve</h2>
      <div class="chart-controls">
        <button class="range-btn" data-range="1D">1D</button>
        <button class="range-btn" data-range="5D">5D</button>
        <button class="range-btn" data-range="1W">1W</button>
        <button class="range-btn" data-range="1M">1M</button>
        <button class="range-btn" data-range="ALL">ALL</button>
      </div>
      <div class="chart-wrap"><canvas id="eqChart"></canvas></div>
    </div>

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
        <table class="data-table"><thead><tr><th>Class</th><th>Symbol</th><th>Qty</th><th>Entry</th><th>Current</th><th>Unrealized</th><th>Unrealized %</th></tr></thead><tbody id="posTableBody"></tbody></table>
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
        <p class="muted" style="margin-top:0.4rem;">
          Dynamic risk:
          <label class="mono">
            <input type="checkbox" id="cfg-dynamic-risk" {% if dynamic_risk_enabled %}checked{% endif %}/>
            enabled
          </label>
        </p>
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
        {% if perf.closed_round_trips and perf.closed_round_trips > 0 %}
        <p class="muted">Trades: <strong class="mono">{{ perf.total_trades }}</strong> · Round-trips: <strong class="mono">{{ perf.closed_round_trips }}</strong> · Win rate: <strong class="mono">{{ (perf.win_rate_pct | round(1)) if perf.win_rate_pct is not none else "—" }}%</strong> · Best: <strong class="mono">{{ "+" if perf.best_trade is not none and perf.best_trade >= 0 else "" }}${{ (perf.best_trade | round(2)) if perf.best_trade is not none else "—" }}</strong> · Worst: <strong class="mono">{{ "$" }}{{ (perf.worst_trade | round(2)) if perf.worst_trade is not none else "—" }}</strong></p>
        {% else %}
        <p class="muted">No completed round-trips yet</p>
        {% endif %}
        {% if rl_history %}
        <table class="data-table"><thead><tr><th>Time</th><th>Summary</th><th>Pairs</th><th>Win%</th></tr></thead><tbody>
          {% for e in rl_history %}
          <tr><td class="mono" style="font-size:0.72rem;">{{ e.created_at }}</td><td>{{ e.summary }}</td><td class="mono">{{ e.trade_count }}</td>
            <td>{% if e.win_rate is not none %}{{ (e.win_rate * 100) | round(1) }}%{% else %}—{% endif %}</td></tr>{% endfor %}
        </tbody></table>{% else %}<p class="muted">No RL nudges yet.</p>{% endif %}
      </div>
    </div>

    <div class="card exec-health-card" id="execHealthCard">
      <h2>🩺 Execution health</h2>
      <div class="exec-health-grid" id="execHealthGrid">
        <div class="exec-health-tile" id="execTileCash">
          <div class="exec-health-tile-label">Cash</div>
          <div class="exec-health-tile-value mono" id="execHealthCash">—</div>
        </div>
        <div class="exec-health-tile" id="execTileBp">
          <div class="exec-health-tile-label">Buying power</div>
          <div class="exec-health-tile-value mono" id="execHealthBuyingPower">—</div>
        </div>
        <div class="exec-health-tile" id="execTileUsable">
          <div class="exec-health-tile-label">Usable buying power</div>
          <div class="exec-health-tile-value mono" id="execHealthUsable">—</div>
        </div>
        <div class="exec-health-tile" id="execTileBlocked">
          <div class="exec-health-tile-label">Blocked exits</div>
          <div class="exec-health-tile-value mono" id="execHealthBlockedExits">0</div>
        </div>
        <div class="exec-health-tile exec-health-tile-wide" id="execTilePdt">
          <div class="exec-health-tile-label">PDT blocked symbols</div>
          <div class="exec-health-pdt-wrap" id="execHealthPdtBadges"><span class="muted exec-health-empty">—</span></div>
          <span class="exec-health-tile-fallback mono" id="execHealthPdtSymbols" style="display:none;">—</span>
        </div>
        <div class="exec-health-tile" id="execTileStale">
          <div class="exec-health-tile-label">Stale local positions</div>
          <div class="exec-health-tile-value mono" id="execHealthStaleLocal">0</div>
        </div>
        <div class="exec-health-tile" id="execTileMismatch">
          <div class="exec-health-tile-label">Broker/local mismatches</div>
          <div class="exec-health-tile-value mono" id="execHealthMismatches">0</div>
        </div>
        <div class="exec-health-tile" id="execTileCryptoFast">
          <div class="exec-health-tile-label">Fast crypto exits</div>
          <div class="exec-health-tile-value mono" id="execHealthCryptoFast">—</div>
        </div>
        <div class="exec-health-tile" id="execTilePdtGuard">
          <div class="exec-health-tile-label">Stock PDT guard</div>
          <div class="exec-health-tile-value mono" id="execHealthPdtGuard">—</div>
        </div>
        <div class="exec-health-tile" id="execTileEligible">
          <div class="exec-health-tile-label">Exit-eligible positions</div>
          <div class="exec-health-tile-value mono" id="execHealthExitEligible">—</div>
        </div>
        <div class="exec-health-tile" id="execTileReconcile">
          <div class="exec-health-tile-label">Last reconciliation</div>
          <div class="exec-health-tile-value mono exec-health-tile-sm" id="execHealthLastReconcile">—</div>
        </div>
      </div>
      <p class="muted exec-health-hint">PDT blocked exits mean Alpaca refused same-day stock exits.</p>
      <p class="muted exec-health-hint">Broker/local mismatches mean local SQLite and Alpaca position records differ.</p>
      <details class="exec-exit-details" id="execExitDetails">
        <summary class="exec-exit-summary">Position exit eligibility <span class="muted" id="execExitSummaryCount"></span></summary>
        <div style="overflow-x:auto;margin-top:0.5rem;">
          <table class="data-table"><thead><tr>
            <th>Symbol</th><th>Class</th><th>Local qty</th><th>Broker qty</th><th>Entry</th><th>Mark</th><th>P/L %</th>
            <th>Eligibility</th><th>Block reason</th><th>PDT</th><th>Last exit try</th><th>Cooldown</th><th>Action</th>
          </tr></thead><tbody id="execExitTableBody"></tbody></table>
          <p class="muted" id="execExitEmpty" style="display:none;margin-top:0.35rem;">No position exit rows.</p>
        </div>
      </details>
    </div>

    <p class="api-links">JSON: <a href="/api/dashboard">/api/dashboard</a> · <a href="/api/config">/api/config</a> · <a href="/api/calibration">/api/calibration</a> · <a href="/api/social">/api/social</a> · <a href="/api/backtest/runs">/api/backtest/runs</a> · <span class="muted">POST</span> <code>/api/sync-alpaca</code></p>
    <p class="last-upd" id="metaNote">Live dashboard via WebSocket (fallback poll {{ refresh_sec }}s) · clock ET</p>
    </div>
  </main>

  <main id="backtest-tab" class="tab-panel">
    <div class="backtest-wrap">
      <div class="card bt-card-setup">
        <h2>Backtest Setup</h2>
        <div class="bt-setup-grid">
          <div><label>Strategy</label><select id="btStrategy"></select></div>
          <div><label>Symbols (CSV)</label><input id="btSymbols" value="AAPL,MSFT,BTC/USD" /></div>
          <div><label>Start date</label><input id="btStart" type="date" value="2025-01-01" /></div>
          <div><label>End date</label><input id="btEnd" type="date" value="2026-01-01" /></div>
          <div><label>Timeframe</label><select id="btTimeframe"><option value="1Day">1Day</option><option value="1H">1H</option></select></div>
          <div><label>Starting cash</label><input id="btStartingCash" type="number" step="0.01" value="100.00" /></div>
          <div style="grid-column: span 2;"><label>Cost assumptions</label><input id="btCostsView" value="fee=5, spread=20, slippage=10" readonly /></div>
          <div style="display:flex;align-items:end;"><label style="display:flex;align-items:center;gap:6px;"><input id="btPyramiding" type="checkbox" />Pyramiding enabled</label></div>
        </div>
        <p class="muted mono" id="btThresholds" style="margin-top:0.6rem;">loading configured thresholds…</p>
        <div class="bt-actions-row" style="margin-top:0.6rem;">
          <span class="muted" style="margin-right:0.4rem;">Presets:</span>
          <button id="btPresetSanity" class="bt-action-btn" type="button">Small sanity test</button>
          <button id="btPresetCrypto" class="bt-action-btn" type="button">Crypto only</button>
          <button id="btPresetHoldings" class="bt-action-btn" type="button">Current holdings</button>
          <button id="btPresetStress" class="bt-action-btn" type="button">Stress test</button>
        </div>
      </div>
      <div class="card bt-card-actions">
        <h2>Primary Actions</h2>
        <div class="bt-actions-row">
          <button id="btRunBtn" class="bt-action-btn bt-primary" type="button">▶ Run Backtest</button>
          <button id="btCompareBtn" class="bt-action-btn" type="button">⚖ Compare Strategies</button>
          <button id="btCopyReportBtn" class="bt-action-btn" type="button" disabled>📋 Copy Backtest Report</button>
          <button id="btDownloadReportBtn" class="bt-action-btn" type="button" disabled>⬇ Download Backtest Report</button>
        </div>
        <p id="btStatus" class="bt-status muted">Run or select a backtest first.</p>
      </div>
      <div class="card bt-card-summary">
        <h2>Results Overview</h2>
        <p id="btSummaryEmpty" class="mono muted">No run selected.</p>
        <div id="btSummaryCards" class="stats-row" style="margin-top:0.5rem;"></div>
        <p id="btSampleWarning" class="muted" style="display:none;color:#fbbf24;margin-top:0.6rem;"></p>
      </div>
      <div class="card bt-card-interpretation">
        <h2>Interpretation</h2>
        <div id="btInterpretation" class="muted">No run selected.</div>
      </div>
      <div class="card bt-card-chart">
        <h2>Backtest Equity Curve</h2>
        <div class="chart-wrap" style="height:360px;"><canvas id="btChart"></canvas></div>
      </div>
      <div class="card bt-card-runs">
        <h2>Strategy Comparison</h2>
        <p id="btCompareEmpty" class="muted">Run comparison to populate this table.</p>
        <div style="overflow:auto;max-height:360px;">
          <table class="data-table"><thead><tr><th>Strategy</th><th>Status</th><th>Reason</th><th>Final Equity</th><th>Return %</th><th>Benchmark %</th><th>Excess %</th><th>Max Drawdown %</th><th>Closed Trades</th><th>Deployed Avg %</th><th>Rejections</th><th>Confidence</th><th>Interpretation</th></tr></thead><tbody id="btCompareBody"></tbody></table>
        </div>
      </div>
      <div class="card bt-card-rejections">
        <h2>Rejections Summary</h2>
        <div id="btRejBadges" style="display:flex;gap:8px;flex-wrap:wrap;"></div>
        <details style="margin-top:0.7rem;">
          <summary class="muted">Details (latest configured rows)</summary>
          <div style="overflow:auto;max-height:300px;margin-top:0.5rem;">
            <table class="data-table"><thead><tr><th>Time</th><th>Symbol</th><th>Reason</th></tr></thead><tbody id="btRejectionsBody"></tbody></table>
          </div>
        </details>
        <p class="muted" id="btRejectionsEmpty" style="display:none;">No rejections.</p>
      </div>
      <div class="card bt-card-trades">
        <h2>Simulated trades</h2>
        <div style="overflow:auto;max-height:340px;">
          <table class="data-table"><thead><tr><th>Time</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Fill</th><th>Entry reason</th><th>Exit reason</th><th>Score</th><th>Hold sec</th><th>PnL</th><th>PnL %</th></tr></thead><tbody id="btTradesBody"></tbody></table>
        </div>
        <p class="muted" id="btTradesEmpty" style="display:none;">No trades.</p>
      </div>
      <div class="card bt-card-runs">
        <h2>Recent Backtest Runs</h2>
        <table class="data-table"><thead><tr><th>ID</th><th>Created</th><th>Strategy</th><th>Status</th><th></th></tr></thead><tbody id="btRunsBody"></tbody></table>
      </div>
      <div class="card bt-card-runs">
        <h2>Signal Events</h2>
        <div id="btSigBadges" style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:0.6rem;"></div>
        <details>
          <summary class="muted">Details (latest configured rows)</summary>
          <div style="overflow:auto;max-height:300px;margin-top:0.5rem;">
          <table class="data-table"><thead><tr><th>Time</th><th>Symbol</th><th>Action</th><th>Class</th><th>Reason</th><th>Score</th></tr></thead><tbody id="btSignalEventsBody"></tbody></table>
          </div>
        </details>
        <p class="muted" id="btSignalEventsEmpty" style="display:none;">No signal events.</p>
      </div>
      <div class="card bt-card-assumptions">
        <h2>Assumptions &amp; Data Quality</h2>
        <div id="btAssumptions" class="mono muted">—</div>
        <div id="btDataQuality" class="mono muted" style="margin-top:0.5rem;">—</div>
        </div>
      <div class="card bt-card-runs">
        <details open>
          <summary><strong>Strategy Diagnostics</strong></summary>
          <div id="btStrategyDiagnostics" class="mono muted" style="margin-top:0.6rem;">Run or select a backtest to view diagnostics.</div>
        </details>
      </div>
      <div class="card bt-card-runs">
        <details>
          <summary><strong>Parameter Experiments</strong></summary>
          <div style="margin-top:0.6rem;">
            <div class="bt-setup-grid">
              <div><label>Base parameter set</label><select id="btParamSetSelect"><option value="">defaults</option></select></div>
              <div style="grid-column: span 3;"><label>Parameter grid (JSON)</label><input id="btParamGrid" value='{"buy_score_threshold":[0.5,0.6],"sell_score_threshold":[-0.5,-0.4]}' /></div>
              <div><label>Walk-forward</label><label style="display:flex;align-items:center;gap:6px;"><input id="btWalkForwardEnabled" type="checkbox" />Enable one split</label></div>
            </div>
            <div class="bt-actions-row" style="margin-top:0.6rem;">
              <button id="btRunExperimentBtn" class="bt-action-btn" type="button">🧪 Run Experiment</button>
            </div>
            <p id="btExperimentStatus" class="bt-status muted">No experiment run yet.</p>
          </div>
        </details>
      </div>
      <div class="card bt-card-runs">
        <details>
          <summary><strong>Experiment Results</strong></summary>
          <p id="btExperimentEmpty" class="muted">Run a parameter experiment to populate this table.</p>
          <div style="overflow:auto;max-height:320px;">
            <table class="data-table"><thead><tr><th>Rank</th><th>Status</th><th>Params</th><th>Return %</th><th>Benchmark %</th><th>Excess %</th><th>Drawdown %</th><th>Closed</th><th>Deployed Avg %</th><th>Rejections</th><th>Confidence</th><th>Score</th><th></th></tr></thead><tbody id="btExperimentBody"></tbody></table>
          </div>
        </details>
      </div>
      <div class="card bt-card-runs">
        <details open>
          <summary><strong>Reports</strong></summary>
          <p class="muted">Use Copy/Download to export backtest or experiment context.</p>
        </details>
      </div>
    </div>
  </main>

  <div id="sym-tooltip" aria-hidden="true"></div>
  <script id="dash-payload" type="application/json">{{ dash_snapshot|tojson }}</script>
  <script>
    const REFRESH_MS = {{ refresh_sec }} * 1000;
    const DASHBOARD_SECRET = {{ dashboard_secret|tojson }};
    const TZ = "America/New_York";
    const EQUITY_RANGE_KEY = "quantbot_equity_range";
    const ACTIVE_TAB_KEY = "quantbot_active_tab";
    const VALID_EQUITY_RANGES = ["1D", "5D", "1W", "1M", "ALL"];
    let selectedEquityRange = localStorage.getItem(EQUITY_RANGE_KEY) || "1D";
    if (!VALID_EQUITY_RANGES.includes(selectedEquityRange)) selectedEquityRange = "1D";
    let spark;
    let lastPollMs = 0;
    window.__dashWsConnected = false;
    window.__dashWsEnabled = typeof io !== "undefined";
    window.__dashPollTimer = null;
    window._symbolCache = {};
    let __lastDashMarketOpen = undefined;
    let __tooltipFetchTimer = null;
    let __symHoverLast = "";

    function readPayload() {
      try {
        const el = document.getElementById("dash-payload");
        const raw = el ? String(el.textContent || "").trim() : "";
        if (!raw) return {};
        return JSON.parse(raw);
      } catch (e) {
        console.error("readPayload", e);
        return {};
      }
    }
    function esc(s) {
      return String(s ?? "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
    }
    function setTextSafe(id, value) {
      const el = document.getElementById(id);
      if (!el) return;
      el.textContent = value != null && value !== undefined ? String(value) : "";
    }
    function setHtmlSafe(id, html) {
      const el = document.getElementById(id);
      if (!el) return;
      el.innerHTML = html != null ? String(html) : "";
    }
    function showDashboardRenderError(tag, err) {
      console.error(tag, err);
      const errEl = document.getElementById("dash-api-error");
      if (errEl) {
        errEl.style.display = "block";
        errEl.textContent = "Dashboard render error — " + tag + " (see console).";
      }
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
    function updateTile(id, value) {
      const el = document.getElementById(id);
      if (!el) return;
      const next = String(value);
      if (el.textContent !== next) el.textContent = next;
    }
    function updateTileClass(id, className) {
      const el = document.getElementById(id);
      if (!el) return;
      if (el.className !== className) el.className = className;
    }
    function applyLiveTiles(data) {
      const pnl = data.pnl_vs_start_pct;
      const pnlDol = data.pnl_vs_start_dollars;
      if (pnl == null || pnl === "" || Number.isNaN(Number(pnl))) {
        updateTile("tilePnl", "—");
        updateTileClass("tilePnl", "big");
      } else {
        const n = Number(pnl);
        const d = Number(pnlDol);
        const dTxt = Number.isFinite(d) ? ((d >= 0 ? "+$" : "-$") + Math.abs(d).toFixed(2)) : "—";
        updateTile("tilePnl", dTxt + " / " + (n >= 0 ? "+" : "") + n.toFixed(2) + "%");
        updateTileClass("tilePnl", "big " + (n >= 0 ? "pos" : "neg"));
      }
      const pf = data.portfolio || {};
      let eq = pf.equity != null ? Number(pf.equity) : (pf.equity_total != null ? Number(pf.equity_total) : null);
      if (eq == null || Number.isNaN(eq)) {
        const ser = data.equity_series || [];
        const last = ser.length ? ser[ser.length - 1] : null;
        if (last) eq = equityY(last);
      }
      updateTile("tileEq", (eq != null && !Number.isNaN(eq)) ? ("$" + eq.toFixed(2)) : "—");
      updateTile("tileMode", data.mode != null ? String(data.mode) : "—");
      if (typeof data.market_open === "boolean") {
        __lastDashMarketOpen = data.market_open;
      }
    }
    function fmtNum(v, d) {
      const n = Number(v);
      if (!Number.isFinite(n)) return "—";
      return n.toFixed(d);
    }
    function fmtMoney(v, d) {
      const n = Number(v);
      if (!Number.isFinite(n)) return "—";
      const dec = d !== undefined && d !== null && Number.isFinite(Number(d)) ? Number(d) : 2;
      return "$" + n.toFixed(dec);
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
    function updateTable(tbodyId, newRows, renderRow, getId, opts) {
      const maxRows = (opts && opts.maxRows != null) ? Number(opts.maxRows) : 50;
      const tbody = document.getElementById(tbodyId);
      if (!tbody) return;
      const rows = Array.isArray(newRows) ? newRows : [];
      const existingIds = new Set([...tbody.querySelectorAll("tr")].map(r => r.dataset.id));
      for (const row of rows.slice().reverse()) {
        const id = String(getId(row));
        if (!id) continue;
        if (existingIds.has(id)) continue;
        const tr = document.createElement("tr");
        tr.dataset.id = id;
        tr.innerHTML = renderRow(row);
        tr.style.opacity = "0";
        tbody.prepend(tr);
        requestAnimationFrame(() => {
          tr.style.transition = "opacity 0.3s";
          tr.style.opacity = "1";
        });
      }
      while (tbody.rows.length > maxRows) tbody.deleteRow(tbody.rows.length - 1);
    }

    function _renderSignalRow(s) {
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
      let html = "";
      html += '<td class="mono ' + dcls + '">' + arr + '</td>';
      html += '<td class="mono" style="font-size:0.72rem;color:var(--text-secondary);">' + esc(fmtDate(s.created_at)) + '</td>';
      html += '<td class="mono has-symbol ' + symCellCls + '" style="font-weight:700;" data-symbol="' + esc(sym) + '">' + symPre + '<span class="sym-txt">' + esc(sym) + '</span></td>';
      html += '<td>' + esc(typeName) + '</td>';
      html += '<td><span class="mono">' + esc(action) + '</span></td>';
      html += '<td class="score-cell"><span class="score-txt mono">' + scoreTxt + '</span>';
      html += '<div class="score-bar-bg"><div class="score-bar-fill" style="width:' + barPct + '%;"></div></div></td>';
      html += '<td></td>';
      return html;
    }

    function _renderPositionRow(p) {
      const sym = p.symbol != null ? String(p.symbol) : "";
      const ac = p.asset_class != null ? String(p.asset_class) : "";
      let netStr = "—";
      if (p.net_qty != null && p.net_qty !== "") {
        const nq = Number(p.net_qty);
        netStr = Number.isFinite(nq) ? nq.toFixed(6) : String(p.net_qty);
      } else if (p.net_qty_fmt != null) {
        netStr = String(p.net_qty_fmt);
      }
      const entry = fmtMoney(p.avg_entry_price, 4);
      const cur = fmtMoney(p.current_price, 4);
      const up = Number(p.unrealized_pnl);
      const upp = Number(p.unrealized_pnl_pct);
      const upCls = Number.isFinite(up) && up > 0 ? "pos" : (Number.isFinite(up) && up < 0 ? "neg" : "");
      const upTxt = Number.isFinite(up) ? ((up >= 0 ? "+$" : "-$") + Math.abs(up).toFixed(2)) : "—";
      const uppTxt = Number.isFinite(upp) ? ((upp >= 0 ? "+" : "") + upp.toFixed(2) + "%") : "—";
      return '<td>' + esc(ac) + '</td><td class="mono has-symbol" data-symbol="' + esc(sym) + '">' + esc(sym) + '</td><td class="mono">' + esc(netStr) + '</td><td class="mono">' + entry + '</td><td class="mono">' + cur + '</td><td class="mono ' + upCls + '">' + upTxt + '</td><td class="mono ' + upCls + '">' + uppTxt + '</td>';
    }

    function _renderTradeRow(t) {
      const sym = t.symbol != null ? String(t.symbol) : "";
      const sideRaw = t.side != null ? String(t.side) : "";
      const side = sideRaw.toLowerCase();
      const scls = side === "buy" ? "side-buy" : (side === "sell" ? "side-sell" : "");
      let st = t.status != null ? String(t.status) : "";
      if (t.reason_code != null && t.reason_code !== "") st += " (" + String(t.reason_code) + ")";
      const qty = t.quantity;
      let html = "";
      html += '<td class="mono" style="font-size:0.72rem;">' + esc(fmtDate(t.created_at)) + '</td>';
      html += '<td class="mono has-symbol" data-symbol="' + esc(sym) + '">' + esc(sym) + '</td>';
      html += '<td class="mono ' + scls + '">' + esc(sideRaw) + '</td>';
      html += '<td class="mono">' + fmtMoney(t.price, 4) + '</td><td class="mono">' + fmtNum(qty, 6) + '</td>';
      html += '<td class="mono">' + fmtMoney(t.notional, 2) + '</td><td>' + esc(st) + '</td>';
      return html;
    }

    function _dedupPositionsRows(rowsIn) {
      const seen = new Set();
      const out = [];
      for (const p of (Array.isArray(rowsIn) ? rowsIn : [])) {
        const symRaw = p && p.symbol != null ? String(p.symbol) : "";
        const key = symRaw.replace("/", "").toUpperCase();
        if (!key) continue;
        if (seen.has(key)) continue;
        seen.add(key);
        out.push(p);
      }
      return out;
    }

    const crosshairPlugin = {
      id: "crosshair",
      afterDraw(chart) {
        if (chart.tooltip && chart.tooltip._active && chart.tooltip._active.length) {
          const ctx = chart.ctx;
          const x = chart.tooltip._active[0].element.x;
          const top = chart.chartArea.top;
          const bottom = chart.chartArea.bottom;
          ctx.save();
          ctx.beginPath();
          ctx.moveTo(x, top);
          ctx.lineTo(x, bottom);
          ctx.lineWidth = 1;
          ctx.strokeStyle = "rgba(255,255,255,0.2)";
          ctx.setLineDash([4, 4]);
          ctx.stroke();
          ctx.restore();
        }
      }
    };
    Chart.register(crosshairPlugin);

    let _chart = null;
    let _equitySeries = [];
    function _hexToRgba(hex, alpha) {
      const h = String(hex || "").replace("#", "");
      if (h.length !== 6) return "rgba(0,255,136," + alpha + ")";
      const r = parseInt(h.slice(0, 2), 16);
      const g = parseInt(h.slice(2, 4), 16);
      const b = parseInt(h.slice(4, 6), 16);
      return `rgba(${r},${g},${b},${alpha})`;
    }
    function getChartColor(series) {
      if (!series.length) return "#00ff88";
      const start = Number(series[0].equity_total);
      const end = Number(series[series.length - 1].equity_total);
      if (!Number.isFinite(start) || !Number.isFinite(end)) return "#00ff88";
      return end >= start ? "#00ff88" : "#ff3b5c";
    }
    function filterSeries(series, range) {
      const now = new Date();
      const cutoff = {
        "1D": new Date(now.getTime() - 24 * 60 * 60 * 1000),
        "5D": new Date(now.getTime() - 5 * 24 * 60 * 60 * 1000),
        "1W": new Date(now.getTime() - 7 * 24 * 60 * 60 * 1000),
        "1M": new Date(now.getTime() - 30 * 24 * 60 * 60 * 1000),
        "ALL": new Date(0)
      }[range] || new Date(0);
      return (Array.isArray(series) ? series : []).filter(d => {
        const raw = d && d.snapshot_at != null ? String(d.snapshot_at) : "";
        const dt = new Date(raw.replace(" ", "T") + "Z");
        return Number.isFinite(dt.getTime()) && dt >= cutoff;
      });
    }
    function updateRangeButtons(active) {
      document.querySelectorAll(".dashboard-wrap .range-btn").forEach((btn) => {
        btn.classList.toggle("active", btn.dataset.range === active);
      });
    }
    function updateEquityChart(series) {
      const ser = Array.isArray(series) ? series : [];
      const labels = ser.map(d => String(d.snapshot_at || ""));
      const data = ser.map(d => equityY(d));
      const color = getChartColor(ser);
      const canvas = document.getElementById("eqChart");
      if (!canvas) return;
      if (!_chart) {
        const ctx = canvas.getContext("2d");
        _chart = new Chart(ctx, {
          type: "line",
          data: { labels, datasets: [{ data, borderColor: color, backgroundColor: _hexToRgba(color, 0.08), borderWidth: 1.5, pointRadius: 0, fill: true, tension: 0.3 }] },
          options: { animation: false, responsive: true, maintainAspectRatio: false,
            plugins: {
              legend: { display: false },
              tooltip: {
                mode: "index",
                intersect: false,
                callbacks: {
                  title: (items) => fmtDate(items[0].label),
                  label: (item) => `Portfolio: $${Number(item.raw).toFixed(2)}`
                },
                backgroundColor: "rgba(0,0,0,0.8)",
                borderColor: "#00ff88",
                borderWidth: 1,
                titleColor: "#64748b",
                bodyColor: "#00ff88",
                padding: 10
              },
              crosshair: false
            },
            interaction: {
              mode: "index",
              intersect: false
            },
            scales: { x: { display: false },
              y: { grid: { color: "#1e293b" }, ticks: { color: "#64748b" } } } }
        });
        return;
      }
      _chart.data.labels = labels;
      _chart.data.datasets[0].data = data;
      _chart.data.datasets[0].borderColor = color;
      _chart.data.datasets[0].backgroundColor = _hexToRgba(color, 0.08);
      _chart.update("none");
    }

    function updateSpark(series) {
      const pts = (Array.isArray(series) ? series : []).slice(-32);
      const labels = pts.map(() => "");
      const data = pts.map((r) => equityY(r));
      const ctx = document.getElementById("sparkEq");
      if (!ctx) return;
      if (!spark) {
        spark = new Chart(ctx, {
          type: "line",
          data: { labels, datasets: [{ data, borderColor: "#00ff88", backgroundColor: "rgba(0,255,136,0.08)", fill: true, tension: 0.35, pointRadius: 0 }] },
          options: { animation: false, responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { x: { display: false }, y: { display: false } } },
        });
        return;
      }
      spark.data.labels = labels;
      spark.data.datasets[0].data = data;
      spark.update("none");
    }

    function applyLiveDashboardSurgical(data) {
      if (!data || typeof data !== "object") return;
      const payload = data;
      try {
        applyLiveTiles(payload);
      } catch (e) {
        showDashboardRenderError("live tiles", e);
      }

      try {
        _equitySeries = Array.isArray(payload.equity_series) ? payload.equity_series : [];
        const ser = filterSeries(_equitySeries, selectedEquityRange);
        updateEquityChart(ser);
        updateSpark(ser);
      } catch (e) {
        showDashboardRenderError("equity chart", e);
      }

      try {
        const sigRows = Array.isArray(payload.recent_signals) ? payload.recent_signals : [];
        const tradeRows = Array.isArray(payload.recent_trades) ? payload.recent_trades : [];
        const posRows = _dedupPositionsRows(Array.isArray(payload.open_positions) ? payload.open_positions : []);

        const sigEmpty = document.getElementById("sigFeedEmpty");
        if (sigEmpty) sigEmpty.style.display = sigRows.length ? "none" : "block";
        const trEmpty = document.getElementById("tradesEmpty");
        if (trEmpty) trEmpty.style.display = tradeRows.length ? "none" : "block";
        const posEmpty = document.getElementById("posEmpty");
        if (posEmpty) posEmpty.style.display = posRows.length ? "none" : "block";

        updateTable("sigFeedBody", sigRows, _renderSignalRow, (r) => r.id ?? (String(r.created_at || "") + "|" + String(r.symbol || "") + "|" + String(r.signal_name || "")), { maxRows: 50 });
        updateTable("tradesTableBody", tradeRows, _renderTradeRow, (r) => r.id ?? (r.broker_order_id ?? (String(r.created_at || "") + "|" + String(r.symbol || "") + "|" + String(r.side || ""))), { maxRows: 50 });
        updateTable("posTableBody", posRows, _renderPositionRow, (r) => (String(r.symbol || "").replace("/", "").toUpperCase()), { maxRows: 50 });
      } catch (e) {
        showDashboardRenderError("tables", e);
      }

      const executionHealth = (payload.execution_health && typeof payload.execution_health === "object") ? payload.execution_health : {};
      const positionExitRows = Array.isArray(payload.position_exit_rows) ? payload.position_exit_rows : [];

      try {
        const eh = executionHealth;
        setTextSafe("execHealthCash", fmtMoney(eh.cash, 2));
        setTextSafe("execHealthBuyingPower", fmtMoney(eh.buying_power, 2));
        setTextSafe("execHealthUsable", fmtMoney(eh.usable_buying_power, 2));
        const blocked = Math.trunc(Number(eh.blocked_exits_count || 0));
        const stale = Math.trunc(Number(eh.stale_local_positions_count || 0));
        const mismatch = Math.trunc(Number(eh.broker_local_mismatch_count || 0));
        setTextSafe("execHealthBlockedExits", String(blocked));
        const pdtSyms = Array.isArray(eh.pdt_blocked_symbols) ? eh.pdt_blocked_symbols.map((s) => String(s || "").trim()).filter(Boolean) : [];
        const pdtWrap = document.getElementById("execHealthPdtBadges");
        const pdtFallback = document.getElementById("execHealthPdtSymbols");
        if (pdtWrap) {
          if (!pdtSyms.length) {
            pdtWrap.innerHTML = '<span class="muted exec-health-empty">—</span>';
          } else {
            pdtWrap.innerHTML = pdtSyms.map((s) => '<span class="exec-health-badge">' + esc(s) + "</span>").join("");
          }
        }
        if (pdtFallback) {
          pdtFallback.style.display = "none";
          pdtFallback.textContent = pdtSyms.join(", ") || "—";
        }
        setTextSafe("execHealthStaleLocal", String(stale));
        setTextSafe("execHealthMismatches", String(mismatch));
        const cryptoFast = eh.crypto_fast_exit_enabled;
        const pdtGuard = eh.stock_pdt_guard_enabled;
        setTextSafe("execHealthCryptoFast", cryptoFast === true ? "on" : cryptoFast === false ? "off" : "—");
        setTextSafe("execHealthPdtGuard", pdtGuard === true ? "on" : pdtGuard === false ? "off" : "—");
        const elig = eh.exit_eligible_positions_count;
        setTextSafe("execHealthExitEligible", elig != null && elig !== "" ? String(elig) : "—");
        const lr = eh.last_reconciliation_at;
        setTextSafe("execHealthLastReconcile", lr != null && lr !== "" ? String(lr) : "—");
        const warn = blocked > 0 || stale > 0 || mismatch > 0;
        const card = document.getElementById("execHealthCard");
        if (card) card.classList.toggle("exec-health-card-warn", warn);
        function tileWarn(id, on) {
          const el = document.getElementById(id);
          if (el) el.classList.toggle("exec-health-warn", !!on);
        }
        tileWarn("execTileBlocked", blocked > 0);
        tileWarn("execTileStale", stale > 0);
        tileWarn("execTileMismatch", mismatch > 0);
        const exitRows = positionExitRows.filter(function (row) { return row != null && typeof row === "object"; });
        const sumCt = document.getElementById("execExitSummaryCount");
        if (sumCt) sumCt.textContent = exitRows.length ? "(" + exitRows.length + ")" : "";
        const tbody = document.getElementById("execExitTableBody");
        const exEmpty = document.getElementById("execExitEmpty");
        if (tbody) {
          if (!exitRows.length) {
            tbody.innerHTML = "";
            if (exEmpty) exEmpty.style.display = "block";
          } else {
            if (exEmpty) exEmpty.style.display = "none";
            tbody.innerHTML = exitRows.map((r) => {
              const sym = esc(String(r.symbol || ""));
              const ac = esc(String(r.asset_class || ""));
              const lq = r.local_qty != null ? esc(String(r.local_qty)) : "—";
              const bq = r.broker_qty != null ? esc(String(r.broker_qty)) : "—";
              const ep = r.entry_price != null ? esc(String(r.entry_price)) : "—";
              const cp = r.current_price != null ? esc(String(r.current_price)) : "—";
              const pl = r.pnl_pct != null ? esc(String(r.pnl_pct)) : "—";
              const elg = esc(String(r.exit_eligibility || "—"));
              const br = esc(String(r.exit_block_reason || "—"));
              const pd = esc(String(r.pdt_status || "—"));
              const letm = esc(String(r.last_exit_attempt_at || "—"));
              const cd = esc(String(r.cooldown_remaining || "—"));
              const act = esc(String(r.recommended_action || "—"));
              const cls = String(r.asset_class || "").toLowerCase() === "crypto" ? "row-crypto" : "row-stock";
              return "<tr class=\"" + cls + "\"><td class=\"mono\">" + sym + "</td><td>" + ac + "</td><td class=\"mono\">" + lq + "</td><td class=\"mono\">" + bq + "</td><td class=\"mono\">" + ep + "</td><td class=\"mono\">" + cp + "</td><td class=\"mono\">" + pl + "</td><td>" + elg + "</td><td class=\"muted\">" + br + "</td><td>" + pd + "</td><td class=\"mono\" style=\"font-size:0.68rem;\">" + letm + "</td><td class=\"mono\">" + cd + "</td><td>" + act + "</td></tr>";
            }).join("");
          }
        }
      } catch (e) {
        showDashboardRenderError("execution health", e);
      }

      try {
        const el = document.getElementById("dash-payload");
        if (el) el.textContent = JSON.stringify(payload);
      } catch (e) {
        console.warn("dash-payload stringify", e);
      }
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
      if (lastPollMs) {
        const ago = Math.floor((Date.now() - lastPollMs) / 1000);
        const wsNote = window.__dashWsEnabled ? " · WS reconnecting" : "";
        lu.textContent = "Last sync: " + ago + "s ago" + wsNote;
        lu.className = "muted";
        return;
      }
      if (!window.__dashWsEnabled) {
        lu.textContent = "Starting…";
        lu.className = "muted";
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

    // buildSpark/buildChart replaced by updateSpark/updateEquityChart (no destroy).
    function bindChartRangeButtons() {
      document.querySelectorAll(".dashboard-wrap .range-btn").forEach((btn) => {
        btn.addEventListener("click", () => {
          const range = btn.dataset.range || "1D";
          selectedEquityRange = VALID_EQUITY_RANGES.includes(range) ? range : "1D";
          localStorage.setItem(EQUITY_RANGE_KEY, selectedEquityRange);
          updateRangeButtons(selectedEquityRange);
          poll();
        });
      });
      updateRangeButtons(selectedEquityRange);
    }
    bindChartRangeButtons();
    (function syncTabFromStorage() {
      const wantBt = localStorage.getItem(ACTIVE_TAB_KEY) === "backtest";
      document.querySelectorAll(".tab-nav .tab-btn").forEach((b) => {
        const isBt = b.dataset.tab === "backtest";
        b.classList.toggle("active", wantBt ? isBt : !isBt);
      });
      const dm = document.getElementById("dashboard-tab");
      const bm = document.getElementById("backtest-tab");
      if (dm) dm.classList.toggle("active", !wantBt);
      if (bm) bm.classList.toggle("active", wantBt);
    })();
    const boot = readPayload();
    if (typeof boot.market_open === "boolean") __lastDashMarketOpen = boot.market_open;
    tickClock();
    try { applyLiveDashboardSurgical(boot); } catch (e) { console.error("initial dashboard render", e); }

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
      const errEl = document.getElementById("dash-api-error");
      try {
        const periodMap = { "1D": "1D", "5D": "1W", "1W": "1W", "1M": "1M", "ALL": "3M" };
        const eqPeriod = periodMap[selectedEquityRange] || "1D";
        const r = await fetch("/api/dashboard?equity_period=" + encodeURIComponent(eqPeriod), { cache: "no-store" });
        if (!r.ok) {
          if (errEl) {
            errEl.style.display = "block";
            errEl.textContent = "Dashboard API error: HTTP " + r.status + " " + (r.statusText || "");
          }
          return;
        }
        let j;
        try {
          j = await r.json();
        } catch (parseErr) {
          console.error("dashboard JSON", parseErr);
          if (errEl) {
            errEl.style.display = "block";
            errEl.textContent = "Dashboard API returned invalid JSON.";
          }
          return;
        }
        if (errEl) {
          errEl.style.display = "none";
          errEl.textContent = "";
        }
        lastPollMs = Date.now();
        updateDashSyncStatus();
        try {
          applyLiveDashboardSurgical(j);
        } catch (err) {
          console.error("dashboard render failed", err);
          const banner = document.getElementById("dash-api-error");
          if (banner) {
            banner.style.display = "block";
            banner.textContent = "Dashboard render error — see console.";
          }
        }
      } catch (e) {
        console.warn("poll", e);
        if (errEl) {
          errEl.style.display = "block";
          errEl.textContent = "Dashboard refresh failed — check network.";
        }
      }
    }
    if (window.__dashWsEnabled) {
      const dashSocket = io({ transports: ["websocket", "polling"] });
      dashSocket.on("connect", function () {
        window.__dashWsConnected = true;
        updateDashSyncStatus();
      });
      dashSocket.on("disconnect", function () {
        window.__dashWsConnected = false;
        updateDashSyncStatus();
        startHttpFallbackPoll();
      });
      dashSocket.on("dashboard_update", function (data) {
        lastPollMs = Date.now();
        updateDashSyncStatus();
        try {
          applyLiveDashboardSurgical(data);
        } catch (e) {
          console.error("dashboard_update render failed", e);
          showDashboardRenderError("websocket payload", e);
        }
      });
    } else {
      console.warn("Socket.IO client not loaded; using HTTP poll only");
    }
    startHttpFallbackPoll();
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
      document.querySelectorAll("#dashboard-tab .cfg-save").forEach((btn) => {
        btn.onclick = async () => {
          const k = btn.dataset.key;
          const n = document.querySelector('.cfg-num[data-key="' + k + '"]');
          const v = parseFloat(n.value);
          const res = await fetch("/api/config", {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-Dashboard-Secret": DASHBOARD_SECRET },
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
          await fetch("/api/config/reset", {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-Dashboard-Secret": DASHBOARD_SECRET },
          });
          location.reload();
        };
      }
      const dr = document.getElementById("cfg-dynamic-risk");
      if (dr) {
        dr.onchange = async () => {
          const v = dr.checked ? 1 : 0;
          const res = await fetch("/api/config", {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-Dashboard-Secret": DASHBOARD_SECRET },
            body: JSON.stringify({ key: "dynamic_risk_enabled", value: v }),
          });
          if (!res.ok) {
            dr.checked = !dr.checked;
          }
        };
      }
    }
    bindCfg();

    let btChart = null;
    let btDefaults = null;
    let btSelectedRunId = null;
    let btCompareRows = [];
    let btCompareState = { status: "not_run", rows: [], error: "" };
    let btExperimentRows = [];
    let btParameterSets = [];
    const BT_DEBUG = localStorage.getItem("quantbot_bt_debug") === "1";
    function switchTab(tab) {
      const wantBacktest = tab === "backtest";
      document.querySelectorAll(".tab-nav .tab-btn").forEach((b) => {
        const isBt = b.dataset.tab === "backtest";
        b.classList.toggle("active", wantBacktest ? isBt : !isBt);
      });
      const dashMain = document.getElementById("dashboard-tab");
      const btMain = document.getElementById("backtest-tab");
      if (dashMain) dashMain.classList.toggle("active", !wantBacktest);
      if (btMain) btMain.classList.toggle("active", wantBacktest);
      localStorage.setItem(ACTIVE_TAB_KEY, wantBacktest ? "backtest" : "dashboard");
      if (wantBacktest) loadBacktestRuns();
    }
    document.querySelectorAll(".tab-nav .tab-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        switchTab(btn.dataset.tab === "backtest" ? "backtest" : "dashboard");
      });
    });
    if (localStorage.getItem(ACTIVE_TAB_KEY) === "backtest") loadBacktestRuns();

    function setBacktestStatus(msg, kind) {
      const el = document.getElementById("btStatus");
      if (!el) return;
      el.textContent = msg || "";
      el.className = "bt-status " + (kind || "muted");
    }
    function setBacktestBusy(on, label) {
      const ids = ["btRunBtn","btCompareBtn","btCopyReportBtn","btDownloadReportBtn","btPresetSanity","btPresetCrypto","btPresetHoldings","btPresetStress"];
      ids.forEach((id) => {
        const btn = document.getElementById(id);
        if (btn) btn.disabled = !!on;
      });
      if (on && label) setBacktestStatus(label, "muted");
    }
    function cfgInt(key, fallback) {
      const cfg = (btDefaults && btDefaults.backtest_config) || {};
      const n = Number(cfg[key]);
      return Number.isFinite(n) ? Math.trunc(n) : fallback;
    }
    function renderBacktestChart(points) {
      const canvas = document.getElementById("btChart");
      if (!canvas) return;
      const labels = (points || []).map(p => p.timestamp);
      const data = (points || []).map(p => Number(p.equity || 0));
      const looksIntraday = labels.some((ts) => String(ts || "").includes(":"));
      const maxTicks = cfgInt("backtest_chart_max_ticks", 10);
      if (!btChart) {
        btChart = new Chart(canvas.getContext("2d"), {
          type: "line",
          data: { labels, datasets: [{ data, borderColor: "#00ff88", pointRadius: 0, tension: 0.2 }] },
          options: {
            animation: false,
            responsive: true,
            maintainAspectRatio: false,
            plugins: { legend: { display: false } },
            scales: {
              x: {
                ticks: {
                  autoSkip: true,
                  maxTicksLimit: maxTicks,
                  maxRotation: 0,
                  callback: function(value, idx) {
                    const raw = String((this.getLabelForValue ? this.getLabelForValue(value) : labels[idx]) || "");
                    const d = new Date(raw.replace(" ", "T"));
                    if (Number.isNaN(d.getTime())) return raw.slice(0, 10);
                    return looksIntraday
                      ? d.toLocaleString("en-US", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })
                      : d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
                  }
                }
              }
            }
          }
        });
        return;
      }
      btChart.data.labels = labels;
      btChart.data.datasets[0].data = data;
      btChart.update("none");
    }

    function fmtMoney(v) {
      const n = Number(v || 0);
      return Number.isFinite(n) ? n.toLocaleString(undefined, { style: "currency", currency: "USD", maximumFractionDigits: 2 }) : "—";
    }
    function fmtPct(v) {
      const n = Number(v);
      return Number.isFinite(n) ? `${n.toFixed(2)}%` : "—";
    }
    function fmtPlain(v) {
      const n = Number(v);
      return Number.isFinite(n) ? n.toFixed(2) : "—";
    }
    function renderMiniCards(rootId, items) {
      const root = document.getElementById(rootId);
      if (!root) return;
      root.innerHTML = items.map((i) => (
        `<div class="bt-mini-card"><div class="label">${esc(i.label)}</div><div class="value">${esc(i.value)}</div></div>`
      )).join("");
    }
    function renderBacktestSummary(summary, rejectionSummary) {
      const empty = document.getElementById("btSummaryEmpty");
      if (empty) empty.style.display = "none";
      const closedTrades = Number(summary.closed_trades || 0);
      const confidence = String(summary.confidence_label || "—");
      renderMiniCards("btSummaryCards", [
        { label: "Starting Cash", value: fmtMoney(summary.starting_cash) },
        { label: "Final Equity", value: fmtMoney(summary.final_equity) },
        { label: "P&L", value: fmtMoney(summary.pnl) },
        { label: "Return %", value: fmtPct(summary.return_pct) },
        { label: "Max Drawdown %", value: fmtPct(summary.max_drawdown_pct) },
        { label: "Total Trades", value: String(summary.trades_total ?? 0) },
        { label: "Closed Trades", value: String(summary.closed_trades ?? 0) },
        { label: "Win Rate", value: fmtPct(summary.win_rate_pct) },
        { label: "Profit Factor", value: fmtPlain(summary.profit_factor) },
        { label: "Expectancy", value: fmtMoney(summary.expectancy) },
        { label: "Rejections Total", value: String(summary.rejections_total ?? 0) },
        { label: "Confidence Label", value: confidence },
        { label: "Strategy Return", value: fmtPct(summary.strategy_return_pct) },
        { label: "Buy & Hold Return", value: fmtPct(summary.equal_weight_buy_and_hold_return_pct) },
        { label: "Excess Return", value: fmtPct(summary.excess_return_pct) },
      ]);
      const warn = document.getElementById("btSampleWarning");
      if (warn) {
        warn.style.display = "none";
        const rationale = summary.confidence_rationale || {};
        const thr = rationale.thresholds || {};
        const lowMin = Number(thr.confidence_low_min_closed_trades || 0);
        if (closedTrades === 0) warn.textContent = "No completed round trips. Return may reflect open/unrealized positions only.";
        else if (closedTrades < lowMin) warn.textContent = "Sample is below configured confidence minimum; reliability is low.";
        if (warn.textContent) warn.style.display = "block";
      }
      const assumptions = summary.assumptions || {};
      const dataQuality = summary.data_quality || {};
      const diagEl = document.getElementById("btStrategyDiagnostics");
      if (diagEl) {
        diagEl.textContent =
          `capital_deployed_avg_pct: ${fmtPct(summary.capital_deployed_avg_pct)}\n` +
          `capital_deployed_max_pct: ${fmtPct(summary.capital_deployed_max_pct)}\n` +
          `idle_cash_avg_pct: ${fmtPct(summary.idle_cash_avg_pct)}\n` +
          `idle_cash_max_pct: ${fmtPct(summary.idle_cash_max_pct)}\n` +
          `time_in_market_pct: ${fmtPct(summary.time_in_market_pct)}\n` +
          `capital_turnover: ${fmtPlain(summary.capital_turnover)}\n` +
          `open_positions_end: ${String(summary.open_positions_end ?? 0)}\n` +
          `benchmark_return_pct: ${fmtPct(summary.equal_weight_buy_and_hold_return_pct)}\n` +
          `excess_return_pct: ${fmtPct(summary.excess_return_pct)}`;
      }
      const aEl = document.getElementById("btAssumptions");
      if (aEl) {
        aEl.textContent =
          `Assumptions\n` +
          `execution model: ${assumptions.execution_model || "—"}\n` +
          `fill model: ${assumptions.fills || "—"}\n` +
          `market hours enforced: ${String(assumptions.market_hours_enforced)}\n` +
          `fractionability enforced: ${String(assumptions.fractionability_rules_enforced)}\n` +
          `data source: ${assumptions.data_source || "—"}\n` +
          `fee bps: ${assumptions.fee_bps ?? "—"}\n` +
          `spread bps: ${assumptions.spread_bps ?? "—"}\n` +
          `slippage bps: ${assumptions.slippage_bps ?? "—"}`;
      }
      const dEl = document.getElementById("btDataQuality");
      if (dEl) {
        dEl.textContent =
          `Data Quality\n` +
          `symbols loaded: ${dataQuality.symbols_loaded ?? "—"}\n` +
          `points by symbol: ${JSON.stringify(dataQuality.points_by_symbol || {})}\n` +
          `warnings count: ${dataQuality.warnings_count ?? 0}\n` +
          `candle count: ${dataQuality.candle_count ?? 0}\n` +
          `provider warnings: ${JSON.stringify(dataQuality.provider_warnings || [])}`;
      }
      const rejPairs = Object.entries(rejectionSummary || {}).sort((a, b) => Number(b[1]) - Number(a[1]));
      renderMiniCards("btRejBadges", rejPairs.map(([k, v]) => ({ label: k, value: String(v) })));
      const tEl = document.getElementById("btThresholds");
      if (tEl) {
        const thr = ((summary.confidence_rationale || {}).thresholds || {});
        tEl.textContent =
          `confidence thresholds: low>=${thr.confidence_low_min_closed_trades ?? "—"}, medium>=${thr.confidence_medium_min_closed_trades ?? "—"}, high>=${thr.confidence_high_min_closed_trades ?? "—"}, warning_downgrade=${String(thr.confidence_warning_downgrade_enabled)}`;
      }
      const interpEl = document.getElementById("btInterpretation");
      if (interpEl) {
        const lines = [];
        const excess = Number(summary.excess_return_pct || 0);
        const rejTotal = Number(summary.rejections_total || 0);
        if (excess >= 0) lines.push("Strategy beat benchmark on excess return.");
        else lines.push("Strategy underperformed benchmark; profit alone can be misleading.");
        if (confidence === "low") lines.push("Confidence is low based on configured sample thresholds and warnings.");
        if (rejTotal > 0) lines.push("Execution rules blocked many actions; inspect rejection and signal-event summaries.");
        interpEl.innerHTML = lines.map((x) => `<p style="margin:0.25rem 0;">• ${esc(x)}</p>`).join("");
      }
    }

    async function loadBacktestResult(runId) {
      const r = await fetch("/api/backtest/result/" + encodeURIComponent(runId), { cache: "no-store" });
      const j = await r.json();
      btSelectedRunId = Number(j.id || runId);
      const summary = (j.summary_json && typeof j.summary_json === "object") ? j.summary_json : {};
      const rejectionSummary = (j.rejection_summary_json && typeof j.rejection_summary_json === "object") ? j.rejection_summary_json : {};
      renderBacktestSummary(summary, rejectionSummary);
      renderBacktestChart(j.equity_curve || []);
      const trades = Array.isArray(j.trades) ? j.trades : [];
      const rejects = Array.isArray(j.rejections) ? j.rejections : [];
      const signalEvents = Array.isArray(j.signal_events) ? j.signal_events : [];
      const maxTrades = cfgInt("backtest_max_report_trades", 80);
      const maxRejects = cfgInt("backtest_max_report_rejections", 100);
      const maxSignals = cfgInt("backtest_max_report_signal_events", 100);
      const tbTr = document.getElementById("btTradesBody");
      const tbRej = document.getElementById("btRejectionsBody");
      const tbSig = document.getElementById("btSignalEventsBody");
      const emptyTr = document.getElementById("btTradesEmpty");
      const emptyRej = document.getElementById("btRejectionsEmpty");
      const emptySig = document.getElementById("btSignalEventsEmpty");
      if (tbTr) {
        tbTr.innerHTML = trades.slice(-maxTrades).reverse().map((t) => {
          const ts = esc(t.timestamp || "");
          const sym = esc(t.symbol || "");
          const side = esc(t.side || "");
          const qn = Number(t.qty);
          const qty = esc(Number.isFinite(qn) ? (Math.abs(qn) < 1 ? qn.toFixed(6) : qn.toFixed(3)) : String(t.qty != null ? t.qty : ""));
          const fp = esc(String(t.fill_price != null ? t.fill_price : ""));
          const entryReason = esc(String((t.meta_json || {}).entry_reason || ""));
          const exitReason = esc(String((t.meta_json || {}).exit_reason || ""));
          const score = esc(String((t.meta_json || {}).strategy_score ?? ""));
          const hold = esc(String(t.hold_seconds != null ? t.hold_seconds : ""));
          const pnl = esc(String(t.pnl != null ? t.pnl : ""));
          const pnlPct = esc(String(t.pnl_pct != null ? t.pnl_pct : ""));
          return `<tr><td class="mono">${ts}</td><td class="mono">${sym}</td><td>${side}</td><td class="mono">${qty}</td><td class="mono">${fp}</td><td>${entryReason}</td><td>${exitReason}</td><td class="mono">${score}</td><td class="mono">${hold}</td><td class="mono">${pnl}</td><td class="mono">${pnlPct}</td></tr>`;
        }).join("");
      }
      if (tbRej) {
        tbRej.innerHTML = rejects.slice(-maxRejects).reverse().map((x) => {
          return `<tr><td class="mono">${esc(x.timestamp || "")}</td><td class="mono">${esc(x.symbol || "")}</td><td>${esc(x.reason_code || "")}</td></tr>`;
        }).join("");
      }
      if (emptyTr) emptyTr.style.display = trades.length ? "none" : "block";
      if (emptyRej) emptyRej.style.display = rejects.length ? "none" : "block";
      if (tbSig) {
        tbSig.innerHTML = signalEvents.slice(-maxSignals).reverse().map((x) => {
          return `<tr><td class="mono">${esc(x.timestamp || "")}</td><td class="mono">${esc(x.symbol || "")}</td><td>${esc(x.strategy_action || "")}</td><td>${esc(x.classification || "")}</td><td>${esc(x.reason_code || "")}</td><td class="mono">${esc(String(x.score != null ? x.score : ""))}</td></tr>`;
        }).join("");
      }
      const sigCounts = {};
      signalEvents.forEach((x) => {
        const k = String(x.reason_code || "UNKNOWN");
        sigCounts[k] = (sigCounts[k] || 0) + 1;
      });
      renderMiniCards("btSigBadges", Object.entries(sigCounts).map(([k, v]) => ({ label: k, value: String(v) })));
      if (emptySig) emptySig.style.display = signalEvents.length ? "none" : "block";
      const copyBtn = document.getElementById("btCopyReportBtn");
      const dlBtn = document.getElementById("btDownloadReportBtn");
      if (copyBtn) copyBtn.disabled = false;
      if (dlBtn) dlBtn.disabled = false;
      setBacktestStatus("Backtest run loaded.", "ok");
    }

    function setBacktestPreset(kind) {
      const tf = document.getElementById("btTimeframe");
      if (kind === "sanity") {
        document.getElementById("btSymbols").value = "AAPL,MSFT,BTC/USD";
        if (tf) tf.value = "1Day";
      } else if (kind === "crypto") {
        document.getElementById("btSymbols").value = "BTC/USD,ETH/USD";
        if (tf) tf.value = "1H";
      } else if (kind === "holdings") {
        document.getElementById("btSymbols").value = "ACHR,AMPX,FSLY";
      } else if (kind === "stress") {
        document.getElementById("btSymbols").value = "AAPL,MSFT,SPY,BTC/USD,ETH/USD";
        if (tf) tf.value = "1H";
      }
    }

    async function loadBacktestDefaults() {
      try {
        const r = await fetch("/api/backtest/defaults", { cache: "no-store" });
        btDefaults = await r.json();
        const strat = document.getElementById("btStrategy");
        if (strat && Array.isArray(btDefaults.strategies)) {
          strat.innerHTML = btDefaults.strategies.map((s) => `<option value="${esc(s)}">${esc(s)}</option>`).join("");
          strat.value = "current_adaptive";
        }
        const tf = document.getElementById("btTimeframe");
        if (tf && btDefaults && btDefaults.default_timeframe) tf.value = btDefaults.default_timeframe;
        const sym = document.getElementById("btSymbols");
        if (sym && Array.isArray(btDefaults.symbols)) sym.value = btDefaults.symbols.join(",");
        const costs = (btDefaults && btDefaults.costs) || {};
        const costsView = document.getElementById("btCostsView");
        if (costsView) costsView.value = `fee=${costs.fee_bps ?? "—"}, spread=${costs.spread_bps ?? "—"}, slippage=${costs.slippage_bps ?? "—"}`;
        const tEl = document.getElementById("btThresholds");
        const cfg = (btDefaults && btDefaults.backtest_config) || {};
        if (tEl) {
          tEl.textContent =
            `confidence thresholds: low>=${cfg.confidence_low_min_closed_trades ?? "—"}, medium>=${cfg.confidence_medium_min_closed_trades ?? "—"}, high>=${cfg.confidence_high_min_closed_trades ?? "—"}, warning_downgrade=${String(cfg.confidence_warning_downgrade_enabled)}`;
        }
        const gridEl = document.getElementById("btParamGrid");
        if (gridEl && btDefaults && btDefaults.parameter_defaults) {
          const d = btDefaults.parameter_defaults || {};
          const grid = {
            buy_score_threshold: [d.buy_score_threshold],
            sell_score_threshold: [d.sell_score_threshold],
            max_position_notional_pct: [d.max_position_notional_pct],
            take_profit_pct: [d.take_profit_pct],
            stop_loss_pct: [d.stop_loss_pct],
            cooldown_bars: [d.cooldown_bars],
          };
          gridEl.value = JSON.stringify(grid);
        }
      } catch (_) {
        setBacktestStatus("Failed to load backtest defaults.", "err");
      }
    }

    async function loadParameterSets() {
      try {
        const strategyName = (document.getElementById("btStrategy") || {}).value || "";
        const r = await fetch(`/api/backtest/parameter-sets?strategy_name=${encodeURIComponent(strategyName)}`, { cache: "no-store" });
        const j = await r.json();
        btParameterSets = Array.isArray(j.rows) ? j.rows : [];
        const sel = document.getElementById("btParamSetSelect");
        if (!sel) return;
        sel.innerHTML = `<option value="">defaults</option>` + btParameterSets.map((x) => `<option value="${esc(String(x.id))}">${esc(String(x.name || `set-${x.id}`))}</option>`).join("");
      } catch (_) {}
    }

    function renderExperimentRows(rows) {
      const body = document.getElementById("btExperimentBody");
      const empty = document.getElementById("btExperimentEmpty");
      if (!body) return;
      btExperimentRows = Array.isArray(rows) ? rows : [];
      body.innerHTML = btExperimentRows.map((r, idx) => {
        const m = r.metrics || {};
        const params = r.params || {};
        const paramSetText = Object.keys(params).length ? JSON.stringify(params) : "{}";
        const setId = Number(r.parameter_set_id || 0);
        const promoteBtn = setId > 0
          ? `<button type="button" class="bt-action-btn" data-promote="${setId}">Promote</button>`
          : "";
        return `<tr><td class="mono">${idx + 1}</td><td>${esc(String(r.status || ""))}</td><td class="mono">${esc(paramSetText)}</td><td class="mono">${esc(String(m.return_pct ?? ""))}</td><td class="mono">${esc(String(m.benchmark_return_pct ?? ""))}</td><td class="mono">${esc(String(m.excess_return_pct ?? ""))}</td><td class="mono">${esc(String(m.max_drawdown_pct ?? ""))}</td><td class="mono">${esc(String(m.closed_trades ?? ""))}</td><td class="mono">${esc(String(m.capital_deployed_avg_pct ?? ""))}</td><td class="mono">${esc(String(m.rejections_total ?? ""))}</td><td>${esc(String(m.confidence_label ?? ""))}</td><td class="mono">${esc(String(r.rank_score ?? ""))}</td><td>${promoteBtn}</td></tr>`;
      }).join("");
      if (empty) {
        empty.style.display = btExperimentRows.length ? "none" : "block";
      }
      body.querySelectorAll("button[data-promote]").forEach((btn) => {
        btn.addEventListener("click", async () => {
          const id = Number(btn.getAttribute("data-promote"));
          if (!Number.isFinite(id) || id <= 0) return;
          const rr = await fetch(`/api/backtest/parameter-sets/${id}/mark-paper-candidate`, {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-Dashboard-Secret": DASHBOARD_SECRET },
          });
          const jj = await rr.json();
          if (!rr.ok || !jj.ok) {
            setBacktestStatus(`Promote failed: ${String(jj.error || rr.status)}`, "err");
            return;
          }
          setBacktestStatus("Candidate marked for paper trial metadata.", "ok");
          await loadParameterSets();
        });
      });
    }

    async function loadBacktestRuns() {
      const r = await fetch("/api/backtest/runs?limit=20", { cache: "no-store" });
      const rows = await r.json();
      const body = document.getElementById("btRunsBody");
      if (!body) return;
      body.innerHTML = "";
      for (const row of rows) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td class="mono">${row.id}</td><td class="mono">${esc(row.created_at || "")}</td><td>${esc(row.strategy_name || "")}</td><td>${esc(row.status || "")}</td><td><button type="button" class="bt-action-btn" data-run="${row.id}">View</button></td>`;
        body.appendChild(tr);
      }
      body.querySelectorAll("button[data-run]").forEach((btn) => {
        btn.addEventListener("click", () => loadBacktestResult(btn.dataset.run));
      });
    }

    document.getElementById("btRunBtn")?.addEventListener("click", async () => {
      setBacktestBusy(true, "Running Backtest...");
      const payload = {
        strategy_name: (document.getElementById("btStrategy") || {}).value || "current_adaptive",
        starting_cash: Number((document.getElementById("btStartingCash") || {}).value || 100),
        symbols: String((document.getElementById("btSymbols") || {}).value || "AAPL").split(",").map(s => s.trim()).filter(Boolean),
        start_date: (document.getElementById("btStart") || {}).value || "2025-01-01",
        end_date: (document.getElementById("btEnd") || {}).value || "2026-01-01",
        timeframe: (document.getElementById("btTimeframe") || {}).value || ((btDefaults || {}).default_timeframe || "1Day"),
        pyramiding_enabled: !!((document.getElementById("btPyramiding") || {}).checked),
      };
      try {
        const r = await fetch("/api/backtest/run", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-Dashboard-Secret": DASHBOARD_SECRET },
          body: JSON.stringify(payload),
        });
        const j = await r.json();
        if (!r.ok || !j || !j.run_id) throw new Error(j.error || "Run failed");
        await loadBacktestRuns();
        await loadBacktestResult(j.run_id);
      } catch (e) {
        setBacktestStatus(`Backtest run failed: ${String(e && e.message ? e.message : e)}`, "err");
      } finally {
        setBacktestBusy(false);
      }
    });
    document.getElementById("btPresetSanity")?.addEventListener("click", () => setBacktestPreset("sanity"));
    document.getElementById("btPresetCrypto")?.addEventListener("click", () => setBacktestPreset("crypto"));
    document.getElementById("btPresetHoldings")?.addEventListener("click", () => setBacktestPreset("holdings"));
    document.getElementById("btPresetStress")?.addEventListener("click", () => setBacktestPreset("stress"));
    document.getElementById("btStrategy")?.addEventListener("change", () => { loadParameterSets(); });
    document.getElementById("btCompareBtn")?.addEventListener("click", async () => {
      setBacktestBusy(true, "Comparing Strategies...");
      const payload = {
        strategy_names: ((btDefaults && btDefaults.backtest_config && btDefaults.backtest_config.backtest_ui_compare_strategies) || ["current_adaptive", "simple_momentum", "crypto_scalper", "aggressive_micro_scalp"]),
        starting_cash: Number((document.getElementById("btStartingCash") || {}).value || 100),
        symbols: String((document.getElementById("btSymbols") || {}).value || "AAPL").split(",").map(s => s.trim()).filter(Boolean),
        start_date: (document.getElementById("btStart") || {}).value || "2025-01-01",
        end_date: (document.getElementById("btEnd") || {}).value || "2026-01-01",
        timeframe: (document.getElementById("btTimeframe") || {}).value || ((btDefaults || {}).default_timeframe || "1Day"),
        pyramiding_enabled: !!((document.getElementById("btPyramiding") || {}).checked),
      };
      try {
        const r = await fetch("/api/backtest/compare", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-Dashboard-Secret": DASHBOARD_SECRET },
          body: JSON.stringify(payload),
        });
        const j = await r.json();
        if (!r.ok || !j.ok) throw new Error(j.error || "Compare failed");
        if (!Array.isArray(j.rows) || !j.rows.every((x) => x && typeof x === "object" && !Array.isArray(x))) {
          if (BT_DEBUG) console.debug("Invalid compare shape", j);
          throw new Error("Invalid comparison response shape");
        }
        const body = document.getElementById("btCompareBody");
        const empty = document.getElementById("btCompareEmpty");
        btCompareRows = Array.isArray(j.rows) ? j.rows : [];
        btCompareState = { status: "ok", rows: btCompareRows, error: "" };
        if (body) {
          body.innerHTML = btCompareRows.map((x) => {
            return `<tr><td>${esc(x.strategy || "")}</td><td>${esc(String(x.status || ""))}</td><td>${esc(String(x.reason || ""))}</td><td class="mono">${esc(String(x.final_equity ?? ""))}</td><td class="mono">${esc(String(x.return_pct ?? ""))}</td><td class="mono">${esc(String(x.benchmark_return_pct ?? x.buy_and_hold_return_pct ?? ""))}</td><td class="mono">${esc(String(x.excess_return_pct ?? ""))}</td><td class="mono">${esc(String(x.max_drawdown_pct ?? ""))}</td><td class="mono">${esc(String(x.closed_trades ?? ""))}</td><td class="mono">${esc(String(x.capital_deployed_avg_pct ?? ""))}</td><td class="mono">${esc(String(x.rejections_total ?? ""))}</td><td>${esc(String(x.confidence_label || ""))}</td><td>${esc(String(x.interpretation || ""))}</td></tr>`;
          }).join("");
        }
        if (empty) {
          empty.textContent = btCompareRows.length ? "" : "No comparison rows returned.";
          empty.style.display = btCompareRows.length ? "none" : "block";
        }
        setBacktestStatus("Comparison complete.", "ok");
      } catch (e) {
        btCompareState = { status: "error", rows: [], error: String(e && e.message ? e.message : e) };
        const empty = document.getElementById("btCompareEmpty");
        if (empty) {
          const msg = String(e && e.message ? e.message : e);
          empty.textContent = msg.includes("Invalid comparison response shape")
            ? "Invalid comparison response."
            : "Comparison failed. See status message.";
          empty.style.display = "block";
        }
        setBacktestStatus(`Comparison failed: ${String(e && e.message ? e.message : e)}`, "err");
      } finally {
        setBacktestBusy(false);
      }
    });
    document.getElementById("btCopyReportBtn")?.addEventListener("click", async () => {
      if (!btSelectedRunId) return setBacktestStatus("Run or select a backtest first.", "err");
      setBacktestBusy(true, "Copying Report...");
      try {
        const r = await fetch(`/api/backtest/report/${encodeURIComponent(btSelectedRunId)}?format=markdown`, { cache: "no-store" });
        let md = await r.text();
        if (!r.ok) throw new Error(md || "Report fetch failed");
        if (btCompareState.status === "ok") {
          md += `\n\n## Strategy Comparison\n${JSON.stringify(btCompareState.rows, null, 2)}\n`;
        } else if (btCompareState.status === "error") {
          md += `\n\n## Strategy Comparison\nComparison failed: ${btCompareState.error}\n`;
        } else {
          md += `\n\n## Strategy Comparison\nStrategy comparison was not run.\n`;
        }
        await navigator.clipboard.writeText(md);
        setBacktestStatus("Backtest report copied to clipboard.", "ok");
      } catch (e) {
        setBacktestStatus(`Copy failed: ${String(e && e.message ? e.message : e)}`, "err");
      } finally {
        setBacktestBusy(false);
      }
    });
    document.getElementById("btDownloadReportBtn")?.addEventListener("click", async () => {
      if (!btSelectedRunId) return setBacktestStatus("Run or select a backtest first.", "err");
      setBacktestBusy(true, "Preparing Report Download...");
      try {
        const r = await fetch(`/api/backtest/report/${encodeURIComponent(btSelectedRunId)}?format=markdown`, { cache: "no-store" });
        let md = await r.text();
        if (!r.ok) throw new Error(md || "Report fetch failed");
        if (btCompareState.status === "ok") {
          md += `\n\n## Strategy Comparison\n${JSON.stringify(btCompareState.rows, null, 2)}\n`;
        } else if (btCompareState.status === "error") {
          md += `\n\n## Strategy Comparison\nComparison failed: ${btCompareState.error}\n`;
        } else {
          md += `\n\n## Strategy Comparison\nStrategy comparison was not run.\n`;
        }
        const blob = new Blob([md], { type: "text/markdown;charset=utf-8" });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = `quantbot_backtest_run_${btSelectedRunId}.md`;
        a.click();
        URL.revokeObjectURL(url);
        setBacktestStatus("Backtest report downloaded.", "ok");
      } catch (e) {
        setBacktestStatus(`Download failed: ${String(e && e.message ? e.message : e)}`, "err");
      } finally {
        setBacktestBusy(false);
      }
    });
    document.getElementById("btRunExperimentBtn")?.addEventListener("click", async () => {
      setBacktestBusy(true, "Running parameter experiment...");
      const statusEl = document.getElementById("btExperimentStatus");
      try {
        let grid = {};
        try {
          grid = JSON.parse(String((document.getElementById("btParamGrid") || {}).value || "{}"));
        } catch (_) {
          throw new Error("Invalid parameter grid JSON");
        }
        const payload = {
          name: `exp-${Date.now()}`,
          strategy_name: (document.getElementById("btStrategy") || {}).value || "current_adaptive",
          symbols: String((document.getElementById("btSymbols") || {}).value || "AAPL").split(",").map(s => s.trim()).filter(Boolean),
          start_date: (document.getElementById("btStart") || {}).value || "2025-01-01",
          end_date: (document.getElementById("btEnd") || {}).value || "2026-01-01",
          timeframe: (document.getElementById("btTimeframe") || {}).value || ((btDefaults || {}).default_timeframe || "1Day"),
          starting_cash: Number((document.getElementById("btStartingCash") || {}).value || 100),
          parameter_grid_json: grid,
          walk_forward: { enabled: !!((document.getElementById("btWalkForwardEnabled") || {}).checked) },
        };
        const r = await fetch("/api/backtest/experiments/run", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-Dashboard-Secret": DASHBOARD_SECRET },
          body: JSON.stringify(payload),
        });
        const j = await r.json();
        if (!r.ok || !j.ok) throw new Error(j.error || "Experiment failed");
        renderExperimentRows(Array.isArray(j.top_results) ? j.top_results : []);
        if (statusEl) statusEl.textContent = `Experiment ${j.experiment_id} completed.`;
        setBacktestStatus("Parameter experiment complete.", "ok");
      } catch (e) {
        const msg = String(e && e.message ? e.message : e);
        if (statusEl) statusEl.textContent = msg;
        setBacktestStatus(`Experiment failed: ${msg}`, "err");
      } finally {
        setBacktestBusy(false);
      }
    });
    loadBacktestDefaults();
    loadParameterSets();
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
    from monitoring.dashboard_data import build_dashboard_payload

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

    def _dashboard_ws_push() -> None:
        from execution import stock_broker

        sio = app.extensions["socketio"]
        while True:
            try:
                with app.app_context():
                    with get_connection() as conn:
                        payload = build_dashboard_payload(
                            conn,
                            rest_client=stock_broker.get_rest_client(),
                            equity_period="1D",
                        )
                sio.emit("dashboard_update", payload)
            except Exception:
                logger.exception("[ws] push error")
            sio.sleep(2)

    app.extensions["socketio"] = socketio
    if not app.config.get("TESTING"):
        socketio.start_background_task(_dashboard_ws_push)

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
        from execution import stock_broker

        period = str(request.args.get("equity_period", "1D") or "1D")
        if period not in ("1D", "1W", "1M", "3M"):
            period = "1D"
        cli = stock_broker.get_rest_client()
        with get_connection() as conn:
            payload = build_dashboard_payload(conn, rest_client=cli, equity_period=period)
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
        from execution import stock_broker

        period = str(request.args.get("equity_period", "1D") or "1D")
        if period not in ("1D", "1W", "1M", "3M"):
            period = "1D"
        cli = stock_broker.get_rest_client()
        with get_connection() as conn:
            payload = build_dashboard_payload(conn, rest_client=cli, equity_period=period)
            cfg_rows = data_store.fetch_all_bot_config_rows(conn)
            bot_ui = _bot_ui_rows(cfg_rows)
        latest = payload.get("portfolio") or {}
        pnl = payload.get("pnl_vs_start_pct")
        pnl_d = payload.get("pnl_vs_start_dollars")
        pnl_str = (
            f"{pnl_d:+.2f}".replace("+", "+$").replace("-", "-$") + f" / {pnl:+.2f}%"
            if pnl is not None and pnl_d is not None
            else "—"
        )
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
            dynamic_risk_enabled=bool(
                next((float(r.get("value", 1.0)) for r in cfg_rows if r.get("key") == "dynamic_risk_enabled"), 1.0)
            ),
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
