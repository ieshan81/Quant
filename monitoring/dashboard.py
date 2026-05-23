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

_DASHBOARD_APP_JS_PATH = Path(__file__).resolve().parent / "dashboard_app.js"
_DASHBOARD_PERF_JS_PATH = Path(__file__).resolve().parent / "dashboard_perf.js"
_DASHBOARD_THEME_PATH = Path(__file__).resolve().parent / "dashboard_theme.css"
_DASHBOARD_LOGO_PATH = Path(__file__).resolve().parent / "static" / "momo-logo.png"


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
  <title>MoMo · MORE MONEY</title>
  <meta name="application-name" content="MoMo"/>
  <meta name="theme-color" content="#060a12"/>
  <link rel="icon" type="image/png" href="/momo-logo.png"/>
  <link rel="apple-touch-icon" href="/momo-logo.png"/>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
  <link rel="stylesheet" href="/dashboard-theme.css"/>
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
      --ease-out: cubic-bezier(0.33, 1, 0.68, 1);
      --ease-spring: cubic-bezier(0.34, 1.56, 0.64, 1);
      --dur-fast: 0.14s;
      --dur-med: 0.22s;
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
      padding: 14px 24px;
      border-bottom: 1px solid var(--border);
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 0.85rem;
      justify-content: space-between;
      max-width: 1400px;
      margin: 0 auto;
      width: 100%;
    }
    header h1 { margin: 0; font-size: 1.1rem; font-weight: 700; letter-spacing: 0.04em; }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
    .header-meta {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 0.65rem;
    }
    .updated-stamp { color: var(--muted); font-size: 12px; }
    .chip-row { display: flex; flex-wrap: wrap; gap: 0.35rem; align-items: center; }
    .chip {
      display: inline-flex;
      align-items: center;
      gap: 0.35rem;
      font-size: 11px;
      letter-spacing: 0.02em;
      padding: 0.18rem 0.55rem;
      border-radius: 999px;
      border: 1px solid rgba(148, 163, 184, 0.28);
      background: rgba(148, 163, 184, 0.08);
      color: var(--muted);
      white-space: nowrap;
      transition: color var(--dur-fast) var(--ease-out), border-color var(--dur-fast) var(--ease-out), background var(--dur-fast) var(--ease-out);
    }
    .chip .dot {
      width: 0.45rem;
      height: 0.45rem;
      border-radius: 50%;
      background: var(--muted);
    }
    .chip.ok    { color: var(--good); border-color: rgba(52, 211, 153, 0.45); background: rgba(52, 211, 153, 0.08); }
    .chip.ok .dot    { background: var(--good); box-shadow: 0 0 6px rgba(52, 211, 153, 0.55); }
    .chip.warn  { color: #fbbf24; border-color: rgba(251, 191, 36, 0.45); background: rgba(251, 191, 36, 0.08); }
    .chip.warn .dot  { background: #fbbf24; box-shadow: 0 0 6px rgba(251, 191, 36, 0.55); }
    .chip.bad   { color: var(--bad); border-color: rgba(248, 113, 113, 0.55); background: rgba(248, 113, 113, 0.1); }
    .chip.bad .dot   { background: var(--bad); box-shadow: 0 0 6px rgba(248, 113, 113, 0.55); }
    .chip.info  { color: var(--accent); border-color: rgba(56, 189, 248, 0.35); background: rgba(56, 189, 248, 0.08); }
    .chip.info .dot  { background: var(--accent); }
    #dashError {
      display: none;
      max-width: 1400px;
      margin: 12px auto 0;
      padding: 0.5rem 0.75rem;
      background: rgba(248,113,113,0.12);
      border: 1px solid var(--bad);
      color: #fecaca;
      border-radius: 6px;
      font-size: 13px;
    }
    body { overflow-x: hidden; }
    nav {
      display: flex;
      gap: 0.35rem;
      padding: 0.55rem 24px;
      border-bottom: 1px solid var(--border);
      flex-wrap: wrap;
      max-width: 1400px;
      margin: 0 auto;
      width: 100%;
    }
    nav button {
      background: var(--card);
      border: 1px solid var(--border);
      color: var(--text);
      padding: 0.4rem 0.85rem;
      border-radius: 6px;
      cursor: pointer;
      font-size: 13px;
      transition:
        border-color var(--dur-fast) var(--ease-out),
        color var(--dur-fast) var(--ease-out),
        background var(--dur-fast) var(--ease-out),
        box-shadow var(--dur-fast) var(--ease-out),
        transform var(--dur-fast) var(--ease-spring);
    }
    nav button:hover {
      border-color: rgba(56, 189, 248, 0.45);
      background: rgba(56, 189, 248, 0.08);
      box-shadow: 0 2px 12px rgba(0, 0, 0, 0.25);
      transform: translateY(-1px);
    }
    nav button:active {
      transform: translateY(0);
    }
    nav button:focus-visible {
      outline: 2px solid var(--accent);
      outline-offset: 2px;
    }
    nav button.active {
      border-color: var(--accent);
      color: var(--accent);
      background: rgba(56, 189, 248, 0.12);
      box-shadow: 0 0 0 1px rgba(56, 189, 248, 0.2);
    }
    nav button.active:hover {
      border-color: #7dd3fc;
      background: rgba(56, 189, 248, 0.18);
    }
    main { padding: 20px 24px 48px; max-width: 1400px; margin: 0 auto; width: 100%; box-sizing: border-box; }
    .tab-panel { display: none; }
    .tab-panel.active { display: flex; flex-direction: column; gap: 16px; }
    .grid-metrics {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
      gap: 12px;
      margin-bottom: 0;
    }
    #brokerTransitionCard .bt-metric .val {
      font-size: 0.78rem;
      line-height: 1.35;
      word-break: break-word;
      overflow-wrap: anywhere;
      white-space: normal;
    }
    #brokerTransitionCard .bt-headline {
      font-size: 0.95rem;
      font-weight: 600;
      color: #e2e8f0;
      margin: 0 0 6px;
      line-height: 1.45;
    }
    #brokerTransitionCard .bt-section { margin: 10px 0 0; font-size: 12px; }
    #brokerTransitionCard .bt-section h4 {
      margin: 0 0 4px;
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      color: var(--muted);
      font-weight: 600;
    }
    #brokerTransitionCard .bt-list { margin: 0; padding-left: 1.1rem; color: #cbd5e1; }
    #brokerTransitionCard .bt-confirm {
      margin: 10px 0 0;
      padding: 8px 10px;
      border-radius: 6px;
      background: rgba(251, 191, 36, 0.12);
      border: 1px solid rgba(251, 191, 36, 0.35);
      font-size: 12px;
    }
    #brokerTransitionCard details.bt-raw { margin-top: 8px; font-size: 11px; }
    .metric {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 0.55rem 0.65rem;
      transition:
        transform var(--dur-fast) var(--ease-spring),
        border-color var(--dur-fast) var(--ease-out),
        box-shadow var(--dur-med) var(--ease-out);
    }
    .metric:hover {
      transform: translateY(-2px);
      border-color: rgba(56, 189, 248, 0.35);
      box-shadow: 0 6px 20px rgba(0, 0, 0, 0.35);
    }
    .metric .lab { font-size: 11px; color: var(--muted); margin-bottom: 0.2rem; }
    .metric .val { font-size: 1rem; font-weight: 600; }
    .card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 0.85rem 1rem;
      margin-bottom: 0;
      transition:
        border-color var(--dur-fast) var(--ease-out),
        box-shadow var(--dur-med) var(--ease-out);
    }
    .card:hover {
      border-color: rgba(56, 189, 248, 0.22);
      box-shadow: 0 8px 28px rgba(0, 0, 0, 0.32);
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
    table.data tbody tr {
      transition: background-color var(--dur-fast) var(--ease-out);
    }
    table.data tbody tr:hover {
      background: rgba(56, 189, 248, 0.07);
    }
    .empty-hint { color: var(--muted); font-size: 13px; margin: 0.35rem 0; }
    .eq-range-btn { background:var(--surface); border:1px solid var(--border); color:var(--text); border-radius:4px; padding:2px 10px; font-size:0.78rem; cursor:pointer; }
    .eq-range-btn:hover { border-color:var(--accent); }
    .eq-range-active { background:var(--accent); color:#fff; border-color:var(--accent); }
    .ops-rings { display:flex; flex-wrap:wrap; gap:1rem; margin:0.75rem 0; }
    .ops-ring-wrap { text-align:center; min-width:88px; }
    .ops-ring {
      width:72px; height:72px; border-radius:50%; margin:0 auto 6px;
      display:flex; align-items:center; justify-content:center;
      font-size:0.75rem; font-weight:600; background:var(--surface);
      border:3px solid var(--border);
    }
    .ops-ring-lab { font-size:0.7rem; color:var(--muted); }
    .ops-log-preview { max-height:280px; overflow:auto; font-size:12px; }
    .pos-good { color: var(--good); }
    .pos-bad  { color: var(--bad); }
    .overview-split {
      display: grid;
      grid-template-columns: minmax(0, 2fr) minmax(280px, 1fr);
      gap: 16px;
      align-items: start;
      margin-bottom: 0;
    }
    @media (max-width: 900px) {
      .overview-split { grid-template-columns: 1fr; }
    }
    .ops-card { padding: 0.85rem 1rem; }
    .ops-card h2 { margin: 0 0 0.55rem; font-size: 0.95rem; font-weight: 600; }
    .ops-narrative {
      list-style: none;
      margin: 0;
      padding: 0;
    }
    .ops-narrative li {
      position: relative;
      padding: 0.32rem 0 0.32rem 1.1rem;
      font-size: 13px;
      line-height: 1.45;
      color: var(--text);
      border-bottom: 1px dashed rgba(148, 163, 184, 0.14);
    }
    .ops-narrative li:last-child { border-bottom: none; }
    .ops-narrative li::before {
      content: "•";
      position: absolute;
      left: 0.2rem;
      top: 0.35rem;
      color: var(--muted);
      font-size: 12px;
    }
    .ops-narrative li.ok::before    { color: var(--good); }
    .ops-narrative li.warn::before  { color: #fbbf24; }
    .ops-narrative li.bad::before   { color: var(--bad); }
    .ops-narrative li .accent { color: var(--accent); }
    .ops-narrative li .good   { color: var(--good); }
    .ops-narrative li .warn-t { color: #fbbf24; }
    .ops-narrative li .bad-t  { color: var(--bad); }
    .ops-narrative li .mono   { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
    .chart-wrap { position: relative; height: 300px; max-width: 100%; }
    .health-pill {
      display: inline-flex;
      align-items: center;
      gap: 0.35rem;
      font-size: 12px;
      padding: 0.18rem 0.5rem;
      border-radius: 999px;
      border: 1px solid rgba(148, 163, 184, 0.25);
      background: rgba(148, 163, 184, 0.08);
      color: var(--muted);
      transition: color var(--dur-fast) var(--ease-out), border-color var(--dur-fast) var(--ease-out), background var(--dur-fast) var(--ease-out);
    }
    .health-pill .dot {
      width: 0.55rem;
      height: 0.55rem;
      border-radius: 50%;
      background: var(--muted);
      box-shadow: 0 0 0 2px rgba(148, 163, 184, 0.18);
    }
    .health-pill.ok    { color: var(--good); border-color: rgba(52, 211, 153, 0.45); background: rgba(52, 211, 153, 0.08); }
    .health-pill.ok .dot    { background: var(--good); box-shadow: 0 0 8px rgba(52, 211, 153, 0.55); }
    .health-pill.warn  { color: #fbbf24; border-color: rgba(251, 191, 36, 0.45); background: rgba(251, 191, 36, 0.08); }
    .health-pill.warn .dot  { background: #fbbf24; box-shadow: 0 0 8px rgba(251, 191, 36, 0.55); }
    .health-pill.bad   { color: var(--bad); border-color: rgba(248, 113, 113, 0.55); background: rgba(248, 113, 113, 0.1); }
    .health-pill.bad .dot   { background: var(--bad); box-shadow: 0 0 8px rgba(248, 113, 113, 0.55); }
    .exit-status {
      display: inline-block;
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.05em;
      padding: 0.18rem 0.45rem;
      border-radius: 4px;
      border: 1px solid currentColor;
      white-space: nowrap;
    }
    .exit-status.hold     { color: var(--muted); }
    .exit-status.can-sell { color: var(--good); background: rgba(52, 211, 153, 0.1); }
    .exit-status.blocked  { color: #fbbf24;     background: rgba(251, 191, 36, 0.08); }
    .exit-status.waiting  { color: var(--accent); background: rgba(56, 189, 248, 0.08); }
    .exit-status.stale    { color: var(--bad);    background: rgba(248, 113, 113, 0.08); }
    .row-warn-note {
      display: inline-block;
      margin-left: 0.4rem;
      font-size: 11px;
      color: #fbbf24;
      cursor: help;
    }
    .status-badge {
      display: inline-block;
      font-size: 10.5px;
      font-weight: 700;
      letter-spacing: 0.04em;
      padding: 0.14rem 0.4rem;
      border-radius: 4px;
      border: 1px solid currentColor;
      text-transform: uppercase;
      white-space: nowrap;
    }
    .status-badge.filled   { color: var(--good);   background: rgba(52, 211, 153, 0.1); }
    .status-badge.rejected { color: var(--bad);    background: rgba(248, 113, 113, 0.1); }
    .status-badge.skipped  { color: var(--muted);  background: rgba(148, 163, 184, 0.1); }
    .status-badge.pending  { color: var(--accent); background: rgba(56, 189, 248, 0.1); }
    .scroll-table {
      max-height: 420px;
      overflow: auto;
      border: 1px solid var(--border);
      border-radius: 6px;
      background: rgba(15, 23, 42, 0.45);
    }
    .scroll-table table.data { font-size: 12px; }
    .scroll-table table.data thead th {
      position: sticky;
      top: 0;
      background: var(--card);
      z-index: 1;
      border-bottom: 1px solid var(--border);
    }
    details.section {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 0.65rem 0.75rem;
      margin-bottom: 0.6rem;
      transition: border-color var(--dur-fast) var(--ease-out), box-shadow var(--dur-med) var(--ease-out);
    }
    details.section:hover { border-color: rgba(56, 189, 248, 0.22); box-shadow: 0 8px 28px rgba(0, 0, 0, 0.32); }
    details.section > summary {
      cursor: pointer;
      list-style-position: outside;
      font-size: 0.95rem;
      font-weight: 600;
      padding: 0.1rem 0;
      transition: color var(--dur-fast) var(--ease-out);
    }
    details.section > summary:hover { color: var(--accent); }
    details.section > summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 4px; border-radius: 3px; }
    details.section > summary::after {
      content: " ▸";
      color: var(--muted);
      font-weight: 400;
      transition: transform var(--dur-fast) var(--ease-out);
      display: inline-block;
    }
    details.section[open] > summary::after { content: " ▾"; }
    details.section .section-body { margin-top: 0.55rem; }
    .dev-diagnostics pre { font-size: 11px; color: var(--muted); white-space: pre-wrap; word-break: break-word; margin: 0.4rem 0 0; }
    /* Phase 1 — Execution Health (full-width; responsive tiles per execution-health-exit-safety plan) */
    .exec-health-panel {
      width: 100%;
      max-width: none;
      margin-left: 0;
      margin-right: 0;
      border-left: 3px solid rgba(56,189,248,0.35);
      transition: border-left-color var(--dur-med) var(--ease-out), box-shadow var(--dur-med) var(--ease-out);
    }
    .exec-health-panel:hover {
      border-left-color: rgba(56, 189, 248, 0.65);
      box-shadow: 4px 0 24px rgba(56, 189, 248, 0.06);
    }
    .exec-health-title-row {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 0.5rem;
      margin-bottom: 0.35rem;
    }
    .eh-helper {
      font-size: 12px;
      color: var(--muted);
      margin: 0 0 0.5rem;
      line-height: 1.4;
    }
    .eh-severity {
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.06em;
      padding: 0.15rem 0.45rem;
      border-radius: 4px;
    }
    .eh-severity.ok {
      background: rgba(52,211,153,0.15);
      color: var(--good);
      border: 1px solid rgba(52,211,153,0.35);
    }
    .eh-severity.warn {
      background: rgba(251,191,36,0.12);
      color: #fbbf24;
      border: 1px solid rgba(251,191,36,0.45);
    }
    .eh-banner {
      font-size: 13px;
      padding: 0.45rem 0.6rem;
      border-radius: 6px;
      margin-bottom: 0.6rem;
      border: 1px solid rgba(251,191,36,0.45);
      background: rgba(251,191,36,0.08);
      color: #fde68a;
    }
    .eh-banner.bad {
      border-color: rgba(248,113,113,0.55);
      background: rgba(248,113,113,0.1);
      color: #fecaca;
    }
    .eh-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(148px, 1fr));
      gap: 0.5rem;
      margin-bottom: 0.65rem;
    }
    @media (min-width: 900px) {
      .eh-grid { grid-template-columns: repeat(4, 1fr); }
    }
    @media (max-width: 520px) {
      .eh-grid { grid-template-columns: 1fr; }
    }
    .eh-tile {
      background: #0b1220;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 0.5rem 0.55rem;
      transition:
        transform var(--dur-fast) var(--ease-spring),
        border-color var(--dur-fast) var(--ease-out),
        box-shadow var(--dur-fast) var(--ease-out);
    }
    .eh-tile:hover {
      transform: translateY(-2px);
      border-color: rgba(148, 163, 184, 0.45);
      box-shadow: 0 6px 18px rgba(0, 0, 0, 0.28);
    }
    .eh-tile.warn {
      border-color: rgba(251,191,36,0.45);
      background: rgba(251,191,36,0.06);
    }
    .eh-tile.bad {
      border-color: rgba(248,113,113,0.45);
      background: rgba(248,113,113,0.08);
    }
    .eh-lab { font-size: 11px; color: var(--muted); margin-bottom: 0.2rem; }
    .eh-val { font-size: 0.95rem; font-weight: 600; word-break: break-word; }
    .badge-row {
      display: flex;
      flex-wrap: wrap;
      gap: 0.35rem;
      margin-bottom: 0.6rem;
      align-items: center;
    }
    .badge-row .lbl {
      font-size: 11px;
      color: var(--muted);
      margin-right: 0.25rem;
    }
    .badge {
      font-size: 11px;
      padding: 0.2rem 0.45rem;
      border-radius: 999px;
      border: 1px solid rgba(248,113,113,0.45);
      background: rgba(248,113,113,0.12);
      color: #fecaca;
      transition:
        transform var(--dur-fast) var(--ease-spring),
        filter var(--dur-fast) var(--ease-out),
        border-color var(--dur-fast) var(--ease-out);
      cursor: default;
    }
    .badge:hover {
      transform: scale(1.04);
      filter: brightness(1.12);
      border-color: rgba(248, 113, 113, 0.65);
    }
    .eh-details summary {
      cursor: pointer;
      font-size: 13px;
      color: var(--accent);
      margin-bottom: 0.35rem;
      transition: color var(--dur-fast) var(--ease-out), letter-spacing var(--dur-fast) var(--ease-out);
      list-style-position: outside;
    }
    .eh-details summary:hover {
      color: #7dd3fc;
      letter-spacing: 0.02em;
    }
    .eh-details summary:focus-visible {
      outline: 2px solid var(--accent);
      outline-offset: 3px;
      border-radius: 4px;
    }
    /* chart-wrap height defined above (300px for equity) */
    .dev-diagnostics { margin-top: 1rem; }
    .dev-diagnostics summary { cursor: pointer; color: var(--muted); font-size: 12px; }
    .dev-diagnostics .section-body { padding-top: 0.5rem; }
    .dev-db-meta { font-size: 11px; color: var(--muted); margin: 0 0 0.5rem; }
    .bt-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 0.5rem; align-items: end; }
    .bt-grid label { display: block; font-size: 11px; color: var(--muted); margin-bottom: 0.2rem; }
    .bt-grid input, .bt-grid select {
      width: 100%;
      padding: 0.35rem 0.45rem;
      border-radius: 6px;
      border: 1px solid var(--border);
      background: #0b1220;
      color: var(--text);
      transition:
        border-color var(--dur-fast) var(--ease-out),
        box-shadow var(--dur-fast) var(--ease-out);
    }
    .bt-grid input:hover, .bt-grid select:hover {
      border-color: rgba(56, 189, 248, 0.35);
    }
    .bt-grid input:focus-visible, .bt-grid select:focus-visible {
      outline: none;
      border-color: var(--accent);
      box-shadow: 0 0 0 3px rgba(56, 189, 248, 0.18);
    }
    .bt-actions-card .bt-actions { margin-top: 0; }
    .bt-actions { display: flex; flex-wrap: wrap; gap: 0.4rem; margin-top: 0.65rem; }
    .bt-actions button {
      padding: 0.45rem 0.75rem;
      border-radius: 6px;
      border: 1px solid var(--accent);
      background: rgba(56,189,248,0.12);
      color: var(--accent);
      cursor: pointer;
      font-size: 13px;
      transition:
        transform var(--dur-fast) var(--ease-spring),
        border-color var(--dur-fast) var(--ease-out),
        background var(--dur-fast) var(--ease-out),
        box-shadow var(--dur-fast) var(--ease-out),
        filter var(--dur-fast) var(--ease-out);
    }
    .bt-actions button:hover:not(:disabled) {
      transform: translateY(-2px);
      background: rgba(56, 189, 248, 0.22);
      box-shadow: 0 4px 16px rgba(56, 189, 248, 0.15);
      filter: brightness(1.08);
    }
    .bt-actions button:active:not(:disabled) {
      transform: translateY(0);
    }
    .bt-actions button:focus-visible {
      outline: 2px solid var(--accent);
      outline-offset: 2px;
    }
    .bt-actions button:disabled {
      opacity: 0.45;
      cursor: not-allowed;
      transform: none;
    }
    .bt-actions button.primary { background: rgba(52,211,153,0.15); border-color: var(--good); color: var(--good); }
    .bt-actions button.primary:hover:not(:disabled) {
      background: rgba(52, 211, 153, 0.28);
      box-shadow: 0 4px 16px rgba(52, 211, 153, 0.18);
      filter: brightness(1.06);
    }
    #btStatus { margin-top: 0.65rem; font-size: 13px; color: var(--muted); }
    .bt-run-error {
      display: none;
      margin-top: 0.65rem;
      padding: 0.5rem 0.65rem;
      border-radius: 6px;
      border: 1px solid var(--bad);
      background: rgba(248, 113, 113, 0.12);
      color: #fecaca;
      font-size: 13px;
    }
    .bt-sub { margin: 0.75rem 0 0.35rem; font-size: 0.8rem; color: var(--muted); font-weight: 600; }
    .vol-layout {
      display: grid;
      grid-template-columns: minmax(200px, 280px) 1fr;
      gap: 0.75rem;
      min-height: 420px;
    }
    @media (max-width: 800px) {
      .vol-layout { grid-template-columns: 1fr; }
    }
    .vol-tree {
      background: #0b1220;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 0.5rem;
      max-height: 520px;
      overflow: auto;
      font-size: 12px;
    }
    .vol-tree-item {
      display: block;
      width: 100%;
      text-align: left;
      border: none;
      background: transparent;
      color: var(--text);
      padding: 0.25rem 0.35rem;
      border-radius: 4px;
      cursor: pointer;
      font-family: ui-monospace, monospace;
      font-size: 12px;
    }
    .vol-tree-item:hover { background: rgba(56, 189, 248, 0.12); }
    .vol-tree-item.active { background: rgba(56, 189, 248, 0.2); color: var(--accent); }
    .vol-tree-dir { color: var(--muted); font-weight: 600; margin-top: 0.35rem; }
    .vol-editor-wrap {
      display: flex;
      flex-direction: column;
      gap: 0.5rem;
      min-height: 320px;
    }
    .vol-toolbar {
      display: flex;
      flex-wrap: wrap;
      gap: 0.4rem;
      align-items: center;
    }
    .vol-toolbar select, .vol-toolbar input[type="text"] {
      background: #0b1220;
      border: 1px solid var(--border);
      color: var(--text);
      border-radius: 6px;
      padding: 0.35rem 0.5rem;
      font-size: 12px;
    }
    .vol-breadcrumb {
      font-size: 12px;
      color: var(--muted);
      font-family: ui-monospace, monospace;
      word-break: break-all;
    }
    #volEditor {
      flex: 1;
      min-height: 280px;
      width: 100%;
      background: #0b1220;
      border: 1px solid var(--border);
      border-radius: 8px;
      color: var(--text);
      font-family: ui-monospace, monospace;
      font-size: 12px;
      padding: 0.6rem;
      resize: vertical;
      line-height: 1.45;
    }
    #volEditor:disabled { opacity: 0.55; cursor: not-allowed; }
    #volFileMeta { font-size: 11px; color: var(--muted); margin: 0; }
    #volStatus { font-size: 12px; color: var(--muted); margin: 0; }
    button.btn, .btn {
      border-radius: 8px; padding: 6px 14px; font-size: 12px; font-weight: 600; cursor: pointer;
      border: 1px solid var(--border); background: var(--surface); color: var(--text);
      transition: border-color 0.15s, background 0.15s, opacity 0.15s;
    }
    button.btn:hover:not(:disabled), .btn:hover:not(:disabled) { border-color: var(--accent); }
    button.btn:disabled, .btn:disabled { opacity: 0.45; cursor: not-allowed; }
    button.btn.primary, .btn.primary { background: rgba(56,189,248,0.18); border-color: var(--accent); color: #e0f2fe; }
    button.btn.secondary, .btn.secondary { background: var(--card); }
    button.btn.warning, .btn.warning { background: rgba(245,158,11,0.12); border-color: #f59e0b; color: #fcd34d; }
    button.btn.danger, .btn.danger { background: rgba(248,113,113,0.12); border-color: var(--bad); color: #fecaca; }
    .mc-top-metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 10px; margin-bottom: 12px; }
    .mc-top-metrics .metric { text-align: center; }
    .mc-top-metrics .val { font-size: 1.05rem; }
    .mc-command-strip {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(148px, 1fr));
      gap: 10px;
      margin-bottom: 14px;
    }
    .mc-cmd-card {
      background: linear-gradient(145deg, #0c1424 0%, #0a101c 100%);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 10px 12px;
      min-height: 88px;
      position: relative;
      overflow: hidden;
    }
    .mc-cmd-card::before {
      content: "";
      position: absolute;
      inset: 0 auto auto 0;
      width: 100%;
      height: 2px;
      background: linear-gradient(90deg, transparent, rgba(56,189,248,0.35), transparent);
      opacity: 0.6;
    }
    .mc-cmd-card.mc-fresh { border-color: rgba(52,211,153,0.4); }
    .mc-cmd-card.mc-warn { border-color: rgba(245,158,11,0.45); }
    .mc-cmd-card.mc-bad { border-color: rgba(248,113,113,0.45); }
    .mc-cmd-lab { font-size: 10px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); margin-bottom: 4px; }
    .mc-cmd-val { font-size: 1.12rem; font-weight: 600; font-family: ui-monospace, monospace; line-height: 1.2; }
    .mc-cmd-sub { font-size: 11px; color: var(--muted); margin-top: 4px; line-height: 1.35; }
    .mc-badge {
      display: inline-block;
      font-size: 10px;
      font-weight: 600;
      padding: 2px 7px;
      border-radius: 999px;
      border: 1px solid var(--border);
      margin-top: 4px;
    }
    .mc-badge.ok { color: #6ee7b7; border-color: rgba(52,211,153,0.45); background: rgba(52,211,153,0.08); }
    .mc-badge.warn { color: #fcd34d; border-color: rgba(245,158,11,0.45); background: rgba(245,158,11,0.08); }
    .mc-badge.bad { color: #fecaca; border-color: rgba(248,113,113,0.45); background: rgba(248,113,113,0.08); }
    .mc-pulse { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #34d399; margin-right: 6px;
      box-shadow: 0 0 0 0 rgba(52,211,153,0.5); animation: mc-pulse 2s infinite; vertical-align: middle; }
    @keyframes mc-pulse {
      0% { box-shadow: 0 0 0 0 rgba(52,211,153,0.45); }
      70% { box-shadow: 0 0 0 8px rgba(52,211,153,0); }
      100% { box-shadow: 0 0 0 0 rgba(52,211,153,0); }
    }
    .mc-mock-grid { display: flex; flex-direction: column; gap: 12px; margin-bottom: 14px; }
    .mc-row-charts {
      display: grid;
      grid-template-columns: minmax(0, 1.45fr) minmax(0, 0.85fr) minmax(0, 1.15fr);
      gap: 12px;
      align-items: stretch;
    }
    .mc-row-bottom {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
    }
    @media (max-width: 1100px) {
      .mc-row-charts { grid-template-columns: 1fr; }
      .mc-row-bottom { grid-template-columns: repeat(2, 1fr); }
    }
    @media (max-width: 640px) { .mc-row-bottom { grid-template-columns: 1fr; } }
    .mc-panel {
      background: linear-gradient(145deg, rgba(12, 20, 36, 0.95) 0%, rgba(8, 14, 24, 0.98) 100%);
      border: 1px solid rgba(56, 189, 248, 0.14);
      border-radius: 12px;
      padding: 12px 14px;
      margin-bottom: 0;
      box-shadow: 0 8px 28px rgba(0, 0, 0, 0.28);
    }
    .mc-panel h4 { margin: 0; font-size: 0.82rem; color: #7dd3fc; font-weight: 600; letter-spacing: 0.02em; }
    .mc-panel-head { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 8px; }
    .mc-range-select {
      background: rgba(15, 23, 42, 0.9);
      border: 1px solid var(--border);
      color: var(--text);
      border-radius: 6px;
      font-size: 11px;
      padding: 4px 8px;
    }
    .mc-chart-wrap {
      background: rgba(6, 12, 22, 0.85);
      border-radius: 10px;
      border: 1px solid rgba(56, 189, 248, 0.12);
      box-shadow: inset 0 0 40px rgba(56, 189, 248, 0.04);
    }
    .mc-chart-wrap.mc-chart-tall { height: 220px; min-height: 220px; }
    .mc-chart-wrap canvas { width: 100% !important; height: 100% !important; }
    .mc-donut-host { position: relative; width: 148px; height: 148px; margin: 4px auto 10px; }
    .mc-donut-host canvas { width: 100% !important; height: 100% !important; }
    .mc-donut-center-label {
      position: absolute; inset: 0;
      display: flex; flex-direction: column; align-items: center; justify-content: center;
      font-size: 10px; color: var(--muted); pointer-events: none; text-align: center; line-height: 1.3;
    }
    .mc-donut-center-label strong { font-size: 13px; color: var(--text); display: block; }
    .mc-alloc-legend { font-size: 11px; color: var(--muted); display: flex; flex-direction: column; gap: 5px; }
    .mc-alloc-legend .leg-row { display: flex; align-items: center; gap: 6px; }
    .mc-alloc-legend .leg-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
    .mc-holdings-table { width: 100%; font-size: 10px; border-collapse: collapse; }
    .mc-holdings-table th { text-align: left; color: var(--muted); font-weight: 500; padding: 5px 4px; border-bottom: 1px solid var(--border); white-space: nowrap; }
    .mc-holdings-table td { padding: 6px 4px; border-bottom: 1px solid rgba(51,65,85,0.3); vertical-align: middle; }
    .mc-holdings-footer { margin-top: 8px; padding-top: 8px; border-top: 1px solid var(--border); font-size: 11px; display: flex; justify-content: space-between; flex-wrap: wrap; gap: 6px; }
    .mc-status-held { display: inline-flex; align-items: center; gap: 4px; font-size: 10px; color: #6ee7b7; }
    .mc-status-held::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: #34d399; box-shadow: 0 0 6px rgba(52,211,153,0.6); }
    .mc-cmd-card.mc-cmd-equity .mc-cmd-val { font-size: 1.28rem; }
    .mc-cmd-spark { height: 30px; margin-top: 6px; opacity: 0.95; }
    .mc-cmd-spark svg { width: 100%; height: 100%; display: block; }
    .mc-pending-empty { text-align: center; padding: 20px 8px; color: var(--muted); }
    .mc-pending-empty .mc-check-icon { font-size: 2.2rem; line-height: 1; color: #34d399; opacity: 0.75; margin-bottom: 6px; }
    .mc-gpt-bar { margin: 14px 0 12px; padding: 14px 16px; border-radius: 12px; border: 1px solid rgba(167, 139, 250, 0.28); }
    .mc-gpt-bar h4 { margin: 0 0 4px; font-size: 0.85rem; color: #c4b5fd; }
    .mc-gpt-bar .mc-gpt-sub { font-size: 11px; color: var(--muted); margin: 0 0 10px; }
    .mc-gpt-bar .mc-action-btns { display: flex; flex-wrap: wrap; gap: 8px; }
    .mc-ask-footer { margin: 16px 0 12px; padding: 14px 16px; border-radius: 12px; }
    .mc-ask-footer .mc-ask-row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
    .mc-ask-footer input { flex: 1; min-width: 200px; }
    .mc-momo-row { display: flex; gap: 12px; align-items: flex-start; }
    .mc-momo-avatar {
      width: 48px; height: 48px; border-radius: 12px; flex-shrink: 0;
      background: linear-gradient(135deg, #4f46e5, #06b6d4);
      display: flex; align-items: center; justify-content: center;
      overflow: hidden;
      box-shadow: 0 0 20px rgba(79, 70, 229, 0.35);
      border: 1px solid rgba(56, 189, 248, 0.35);
    }
    .mc-momo-avatar .momo-avatar-img,
    .momo-avatar-img { width: 100%; height: 100%; object-fit: cover; display: block; }
    .mc-donut-row { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
    .mc-donut-bar { flex: 1; min-width: 120px; height: 10px; background: rgba(148,163,184,0.12); border-radius: 6px; overflow: hidden; display: flex; }
    .mc-donut-bar span { height: 100%; }
    .mc-mini-table { width: 100%; font-size: 11px; border-collapse: collapse; }
    .mc-mini-table th { text-align: left; color: var(--muted); font-weight: 500; padding: 4px 6px 4px 0; border-bottom: 1px solid var(--border); }
    .mc-mini-table td { padding: 5px 6px 5px 0; border-bottom: 1px solid rgba(51,65,85,0.35); }
    .sym-icon-wrap { display: inline-flex; align-items: center; gap: 6px; vertical-align: middle; }
    .sym-icon { width: 20px; height: 20px; border-radius: 50%; object-fit: cover; background: rgba(15,23,42,0.8); border: 1px solid rgba(56,189,248,0.2); flex-shrink: 0; }
    .sym-fallback { display: inline-flex; width: 20px; height: 20px; border-radius: 50%; align-items: center; justify-content: center; font-size: 10px; font-weight: 700; background: rgba(56,189,248,0.15); color: #7dd3fc; border: 1px solid rgba(56,189,248,0.35); }
    .mc-feed li { font-size: 11px; color: var(--muted); margin: 4px 0; list-style: none; padding-left: 0; }
    .mc-feed li strong { color: var(--text); }
    .mc-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 0.75rem; }
    .mc-card { background: #0b1220; border: 1px solid var(--border); border-radius: 10px; padding: 0.85rem; }
    .mc-card h3 { margin: 0 0 0.35rem; font-size: 0.9rem; color: var(--accent); }
    .mc-card .mc-ts { font-size: 10px; color: var(--muted); margin-bottom: 0.45rem; }
    .mc-card .mc-body { font-size: 13px; line-height: 1.5; white-space: pre-wrap; }
    .mc-card.mc-ok { border-color: rgba(52,211,153,0.35); }
    .mc-card.mc-warn { border-color: rgba(245,158,11,0.45); }
    .mc-card.mc-bad { border-color: rgba(248,113,113,0.45); }
    .mc-actions { display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: center; }
    .mc-action-group { margin: 0.35rem 0; }
    .mc-action-group summary { cursor: pointer; font-size: 12px; font-weight: 600; color: var(--muted); padding: 4px 0; }
    .mc-action-group .mc-action-btns { display: flex; flex-wrap: wrap; gap: 0.4rem; margin-top: 6px; }
    .mc-progress { height: 5px; background: rgba(148,163,184,0.15); border-radius: 4px; overflow: hidden; margin: 6px 0; display: none; }
    .mc-progress-bar { height: 100%; width: 0%; background: linear-gradient(90deg, #34d399, #22d3ee); transition: width 0.15s ease-out; }
    .mc-progress-bar.indeterminate {
      width: 40% !important;
      animation: dash-progress-slide 1.1s ease-in-out infinite;
    }
    @keyframes dash-progress-slide {
      0% { transform: translateX(-120%); }
      100% { transform: translateX(320%); }
    }
    .mc-momo-box { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 12px; }
    .config-cat { margin: 14px 0 8px; font-size: 13px; font-weight: 600; color: var(--accent); }
    .config-row { display: grid; grid-template-columns: 1fr 140px 90px; gap: 8px; align-items: start; padding: 8px 0; border-bottom: 1px solid var(--border); font-size: 12px; }
    .config-row.danger { background: rgba(248,113,113,0.06); border-radius: 6px; padding: 8px; }
    .config-row label { color: var(--text); }
    .config-meta { font-size: 10px; color: var(--muted); margin-top: 2px; }
    .config-warn { color: #fbbf24; font-size: 10px; }
    .mc-quick-btns { display: flex; flex-wrap: wrap; gap: 6px; margin: 8px 0; }
    .danger-zone { border: 1px solid rgba(248,113,113,0.45); border-radius: 8px; padding: 0.75rem; margin-top: 0.75rem; }
    .danger-zone h3 { color: #fecaca; margin: 0 0 0.5rem; font-size: 0.85rem; }
    .bt-sub:first-child { margin-top: 0; }
    #btStrategy { max-width: min(100%, 420px); }
    pre.sec { font-size: 11px; overflow: auto; max-height: 180px; margin: 0.35rem 0 0; color: var(--muted); }
    header h1.mono {
      transition: color var(--dur-med) var(--ease-out), text-shadow var(--dur-med) var(--ease-out);
    }
    header:hover h1.mono {
      color: #f1f5f9;
      text-shadow: 0 0 24px rgba(56, 189, 248, 0.15);
    }
    @media (prefers-reduced-motion: reduce) {
      *,
      *::before,
      *::after {
        animation-duration: 0.01ms !important;
        animation-iteration-count: 1 !important;
        transition-duration: 0.01ms !important;
      }
      nav button:hover,
      .metric:hover,
      .card:hover,
      .eh-tile:hover,
      .bt-actions button:hover:not(:disabled) {
        transform: none;
      }
    }
    .capital-card .capital-sub { color: var(--muted); font-size: 12px; margin: 4px 0 0; }
    .capital-card .capital-grid .muted { color: var(--muted); font-size: 11px; }
    .capital-card.capital-warn-b .capital-main { color: #fbbf24; }
    .modal-backdrop {
      position: fixed; inset: 0; background: rgba(0,0,0,0.55);
      display: none; align-items: center; justify-content: center; z-index: 9999;
      padding: 16px;
    }
    .modal-backdrop.open { display: flex; }
    .modal-box {
      background: var(--card); border: 1px solid var(--border); border-radius: 10px;
      max-width: 460px; width: 100%; padding: 18px 20px;
    }
    .modal-box h3 { margin: 0 0 10px; font-size: 1rem; }
    .modal-actions { display: flex; gap: 10px; justify-content: flex-end; margin-top: 16px; flex-wrap: wrap; }
    .btn-sell {
      background: rgba(248,113,113,0.15); border: 1px solid var(--bad); color: #fecaca;
      padding: 8px 14px; border-radius: 6px; cursor: pointer; font-size: 13px;
    }
    .btn-sell:disabled { opacity: 0.45; cursor: not-allowed; }
    .btn-cancel {
      background: transparent; border: 1px solid var(--border); color: var(--text);
      padding: 8px 14px; border-radius: 6px; cursor: pointer; font-size: 13px;
    }
    .sell-open-btn { font-size: 11px; padding: 4px 10px; border-radius: 5px; }
    #dashToast {
      position: fixed; bottom: 18px; left: 50%; transform: translateX(-50%);
      z-index: 10000; padding: 10px 16px; border-radius: 8px; border: 1px solid var(--border);
      font-size: 13px; display: none; max-width: min(520px, 92vw);
    }
  </style>
</head>
<body class="qb-app">
  <input type="hidden" id="dash-secret-holder" value="{{ dashboard_secret|e }}"/>
  <div id="dashError" role="alert" style="max-width:none;margin:0;border-radius:0;"></div>
  <div id="dashToast" role="status" aria-live="polite"></div>
  <div class="app-shell">
    <aside class="app-sidebar" aria-label="Primary navigation">
      <div class="sidebar-brand">
        <img class="brand-mark-img" src="/momo-logo.png" alt="MoMo" width="40" height="40"/>
        <div>
          <div class="brand-title">MoMo</div>
          <div class="brand-sub">More Money</div>
        </div>
      </div>
      <div class="sidebar-badges">
        <span class="status-badge ok" id="sidebarBadgePaper">Paper Trading</span>
        <span class="status-badge warn" id="sidebarBadgeLive">Live Disabled</span>
      </div>
      <nav class="sidebar-nav" aria-label="Tabs">
        <button type="button" class="tab-btn active" data-tab="mission">Mission Control</button>
        <button type="button" class="tab-btn" data-tab="overview">Overview</button>
        <button type="button" class="tab-btn" data-tab="positions">Positions</button>
        <button type="button" class="tab-btn" data-tab="activity">Activity</button>
        <button type="button" class="tab-btn" data-tab="backtest">Backtest</button>
        <button type="button" class="tab-btn" data-tab="ai">MoMo Console</button>
        <button type="button" class="tab-btn" data-tab="ops">Ops Center</button>
        <button type="button" class="tab-btn" data-tab="files">Files</button>
        <button type="button" class="tab-btn" data-tab="config">Config</button>
      </nav>
      <div class="sidebar-footer glass-card">
        <div class="sidebar-account-lab">Paper account</div>
        <div class="mono" id="sidebarAccountLine">Alpaca paper</div>
        <div class="sidebar-system" id="sidebarSystemLine"><span class="health-dot ok" id="sidebarHealthDot"></span><span id="sidebarSystemText">Connecting…</span></div>
      </div>
    </aside>
    <div class="app-main">
      <header class="header-strip">
        <div class="header-strip-left">
          <h1 id="headerTabTitle" class="header-title">Mission Control</h1>
          <p class="header-subtitle" id="headerTabSubtitle">Your command center. Calm execution. Compounding edge.</p>
          <span id="dashUpdatedAt" class="updated-stamp" style="display:block;margin-top:6px;">Updated —</span>
        </div>
        <div class="header-strip-metrics">
          <div class="header-metric"><span class="hm-lab">Equity</span><span class="hm-val mono" id="hdrEquity">—</span></div>
          <div class="header-metric"><span class="hm-lab">Cash / BP</span><span class="hm-val mono" id="hdrCashBp">—</span></div>
          <div class="header-metric"><span class="hm-lab">Mode</span><span class="hm-val" id="hdrMode">—</span></div>
          <div class="header-metric"><span class="hm-lab">Last sync</span><span class="hm-val mono" id="hdrSync">—</span><span class="health-dot ok" id="hdrHealthDot" title="API health"></span></div>
        </div>
        <div class="header-strip-chips chip-row" id="statusChips">
          <span class="chip" id="chipMode" data-state="info"><span class="dot"></span><span class="chip-text">— mode</span></span>
          <span class="chip" id="chipLive" data-state="info"><span class="dot"></span><span class="chip-text">Live —</span></span>
          <span class="chip" id="chipApi" data-state="info"><span class="dot"></span><span class="chip-text">API connecting…</span></span>
          <span class="chip info" id="chipPoll"><span class="dot"></span><span class="chip-text">Poll 30s</span></span>
        </div>
      </header>
      <main class="tab-content">
    <section id="panel-mission" class="tab-panel cockpit-tab active">
      <div class="mc-command-strip" id="mcCommandStrip"></div>
      <div class="mc-mock-grid" id="mcCockpitMain">
        <div class="mc-row-charts">
          <div class="mc-panel glass-card mc-equity-panel">
            <div class="mc-panel-head">
              <h4>Equity Curve</h4>
              <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;">
                <select id="mcEqRangeSelect" class="mc-range-select" aria-label="Equity range">
                  <option value="1D">Last 1 Day</option>
                  <option value="5D">Last 5 Days</option>
                  <option value="1W">Last 1 Week</option>
                  <option value="1M">Last 1 Month</option>
                  <option value="ALL">All History</option>
                </select>
                <button type="button" class="eq-range-btn mc-eq-range eq-range-active" data-range="1D">1D</button>
                <button type="button" class="eq-range-btn mc-eq-range" data-range="5D">5D</button>
                <button type="button" class="eq-range-btn mc-eq-range" data-range="1W">1W</button>
                <button type="button" class="eq-range-btn mc-eq-range" data-range="1M">1M</button>
                <button type="button" class="eq-range-btn mc-eq-range" data-range="ALL">ALL</button>
              </div>
            </div>
            <div id="mcEqRangeChange" class="mono" style="font-size:11px;margin-bottom:6px;color:var(--muted);"></div>
            <div class="chart-wrap mc-chart-wrap mc-chart-tall"><canvas id="mcEquityChart"></canvas></div>
            <p id="mcEqEmptyHint" class="empty-hint" style="display:none;margin:6px 0 0;font-size:11px;"></p>
          </div>
          <div class="mc-panel glass-card mc-alloc-panel">
            <h4>Capital Allocation</h4>
            <div class="mc-donut-host">
              <canvas id="mcAllocDonut" aria-label="Capital allocation chart"></canvas>
              <div id="mcAllocDonutCenter" class="mc-donut-center-label"><span>Total</span><strong>—</strong></div>
            </div>
            <div id="mcCapitalAllocLegend" class="mc-alloc-legend"></div>
          </div>
          <div class="mc-panel glass-card mc-holdings-panel">
            <h4>Holdings</h4>
            <div id="mcHoldingsMini"><span class="muted">Loading…</span></div>
            <div id="mcHoldingsFooter" class="mc-holdings-footer" style="display:none;"></div>
          </div>
        </div>
        <div class="mc-row-bottom">
          <div class="mc-panel glass-card"><h4>Pending Exits</h4><div id="mcPendingExits"><span class="muted">None</span></div></div>
          <div class="mc-panel mc-crypto-scanner glass-card" id="mcCryptoScannerPanel"><h4>Crypto Scanner</h4><div id="mcCryptoScanner"><span class="muted">Loading…</span></div></div>
          <div class="mc-panel glass-card"><h4>Last Actions</h4><ul class="mc-feed timeline-feed" id="mcActionFeed"><li>—</li></ul></div>
          <div class="mc-panel mc-growth-plan glass-card" id="growthPlanPanel">
            <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;margin:0 0 6px;">
              <h4 style="margin:0;">Growth Plan · Milestone Forecast</h4>
              <span class="growth-confidence-badge" id="growthConfidenceBadge" style="font-size:11px;color:#9ca3af;">Confidence: —</span>
            </div>
            <div class="growth-current-row" style="display:flex;gap:10px;align-items:center;font-size:12px;flex-wrap:wrap;margin-bottom:6px;">
              <span>Current: <strong id="growthCurrentEquity">—</strong></span>
              <span>Next: <strong id="growthNextMilestone">—</strong></span>
              <span id="growthProgressLabel">Progress —</span>
            </div>
            <div class="growth-progress-bar" style="height:6px;background:#1f2937;border-radius:3px;overflow:hidden;margin-bottom:6px;">
              <div id="growthProgressFill" style="height:100%;width:0%;background:linear-gradient(90deg,#38bdf8,#a78bfa);transition:width 0.5s;"></div>
            </div>
            <div id="growthRequiredBlock" style="font-size:11px;color:#9ca3af;margin-bottom:4px;">
              Required return: <span id="growthRequiredReturn">—</span><br/>
              Required daily compounded:<br/>
              <span class="mono" id="growthDailyTable" style="display:inline-block;margin-left:4px;">—</span>
            </div>
            <div id="growthMonteCarloBlock" style="font-size:11px;color:#9ca3af;margin-bottom:4px;">
              Monte Carlo (90d):<br/>
              <span class="mono" style="display:inline-block;margin-left:4px;">
                Hit: <span id="growthMcHit">—</span> ·
                Median: <span id="growthMcMedian">—</span> ·
                Ruin: <span id="growthMcRuin">—</span>
              </span>
            </div>
            <div id="growthBlockersBlock" style="font-size:11px;color:#fbbf24;margin-bottom:6px;display:none;">
              <strong>Blockers:</strong>
              <ul id="growthBlockersList" style="margin:2px 0 0 14px;padding:0;font-size:11px;"></ul>
            </div>
            <div class="mc-momo-row" style="margin-top:6px;">
              <div class="mc-momo-avatar" aria-hidden="true"><img src="/momo-logo.png" alt="" class="momo-avatar-img"/></div>
              <div id="growthVerdict" style="flex:1;min-width:0;font-size:11px;color:#e5e7eb;">Loading projection…</div>
            </div>
          </div>
        </div>
      </div>
      <div class="mc-gpt-bar glass-card" id="mcGptBundleBar">
        <h4>GPT analysis bundle</h4>
        <p class="mc-gpt-sub">Live scrubbed operator export from <span class="mono">/api/ops/gpt-analyze-bundle</span> — copy or download for ChatGPT.</p>
        <div class="mc-action-btns">
          <button type="button" id="btnGPTAnalyzeLogs" class="btn primary">Build bundle</button>
          <button type="button" id="btnCopyGPTAnalyzeBundle" class="btn secondary">Copy JSON</button>
          <button type="button" id="btnDownloadGPTAnalyzeBundle" class="btn secondary">Download JSON</button>
          <button type="button" id="btnDownloadGPTAnalyzeBundleTxt" class="btn secondary">Download TXT</button>
          <button type="button" id="btnCopyAiMemory" class="btn secondary">AI memory copy</button>
          <button type="button" id="btnSendGPTAnalyzeBundleTelegram" class="btn secondary">Telegram summary</button>
        </div>
        <div id="mcGptBundleProgress" class="mc-progress"><div id="mcGptBundleProgressBar" class="mc-progress-bar"></div></div>
        <p id="mcGptBundleStatus" class="empty-hint" style="margin:8px 0 0;font-size:11px;">GPT bundle: not loaded — click Build bundle or Copy/Download.</p>
        <pre id="mcGptPreview" class="mono sec" style="display:none;max-height:100px;margin-top:8px;font-size:10px;"></pre>
      </div>
      <div class="mc-ask-footer glass-card">
        <h3 style="margin:0 0 8px;font-size:0.9rem;color:#a78bfa;">Ask MoMo</h3>
        <div class="mc-ask-row">
          <input type="text" id="mcMomoInput" placeholder="Ask a question or request analysis…" style="background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:8px;padding:8px 10px;font-size:13px;" />
          <button type="button" id="btnMcAskMomo" class="btn primary">Send</button>
        </div>
        <div class="mc-quick-btns">
          <button type="button" class="btn secondary mc-quick" data-q="Why no crypto?">Why no crypto?</button>
          <button type="button" class="btn secondary mc-quick" data-q="Summarize risk">Summarize risk</button>
          <button type="button" class="btn secondary mc-quick" data-q="What changed?">What changed?</button>
          <button type="button" class="btn secondary mc-quick" data-q="Can it trade tonight?">Can it trade tonight?</button>
        </div>
        <div id="mcMomoAnswer" class="mc-body" style="margin-top:8px;min-height:1.25rem;max-height:100px;overflow:auto;font-size:12px;color:var(--muted);">—</div>
      </div>
      <details class="diag-drawer mc-diagnostics-zone" id="mcDiagnosticsZone">
        <summary>Advanced diagnostics &amp; exports (operator)</summary>
      <div class="mc-action-center">
        <p class="mc-gpt-sub" style="margin:0 0 8px;">GPT bundle controls are in the panel above. Use Refresh to reload Mission Control.</p>
        <div class="mc-action-btns" style="margin-top:6px;">
          <button type="button" id="btnMcRefresh" class="btn secondary">Refresh Mission Control</button>
        </div>
      </div>
      <div id="mcProgress" class="mc-progress"><div id="mcProgressBar" class="mc-progress-bar"></div></div>
      <p id="mcPerfStatus" class="empty-hint" style="margin:0.25rem 0;font-size:11px;"></p>
      <p id="mcStatus" class="empty-hint" style="margin:0.35rem 0;"></p>
      <textarea id="mcCopyFallback" class="mono sec" style="display:none;width:100%;max-height:160px;margin:6px 0;font-size:11px;" readonly placeholder="Copy fallback — select all and copy"></textarea>
      <pre id="mcGptPreview" class="mono sec" style="display:none;max-height:120px;"></pre>
      <div class="mc-grid" id="mcGrid">
        <div class="mc-card" id="mcAccount"><h3>Account</h3><div class="mc-ts"></div><div class="mc-body">Loading…</div></div>
        <div class="mc-card" id="mcMission"><h3>Mission</h3><div class="mc-ts"></div><div class="mc-body">Loading…</div></div>
        <div class="mc-card" id="mcCapital"><h3>Capital Protection</h3><div class="mc-ts"></div><div class="mc-body">Loading…</div></div>
        <div class="mc-card" id="mcBroker"><h3>Broker / Runtime</h3><div class="mc-ts"></div><div class="mc-body">Loading…</div></div>
        <div class="mc-card" id="mcPositions"><h3>Positions</h3><div class="mc-ts"></div><div class="mc-body">Loading…</div></div>
        <div class="mc-card" id="mcCrypto"><h3>Crypto Push</h3><div class="mc-ts"></div><div class="mc-body">Loading…</div></div>
        <div class="mc-card" id="mcCryptoPull"><h3>Crypto Pull</h3><div class="mc-ts"></div><div class="mc-body">Loading…</div></div>
        <div class="mc-card" id="mcMomo"><h3>MoMo Summary</h3><div class="mc-ts"></div><div class="mc-body">Loading…</div></div>
        <div class="mc-card" id="mcOps"><h3>Ops Health</h3><div class="mc-ts"></div><div class="mc-body">Loading…</div></div>
      </div>
      <pre id="mcDevJson" class="mono sec" style="display:none;">{}</pre>
      </details>
    </section>
    <section id="panel-overview" class="tab-panel cockpit-tab">
      <div class="tab-panel-header"><h2>Overview</h2><p>Executive summary — portfolio, engines, risk, and what the bot is doing.</p></div>
      <p id="overviewDataHint" class="muted" style="display:none;margin:0 0 12px;font-size:13px;"></p>
      <div class="card glass-card" id="overviewTruthCard">
        <h2 style="margin:0 0 8px;font-size:1rem;font-weight:600;">Live posture</h2>
        <div class="grid-metrics" style="margin-bottom:8px;">
          <div class="metric"><div class="lab">Buying power</div><div class="val mono" id="ovBp">—</div></div>
          <div class="metric"><div class="lab">Capital recovery</div><div class="val" id="ovRecovery">—</div></div>
          <div class="metric"><div class="lab">Crypto Re-Check Engine</div><div class="val" id="ovFastLoop">—</div></div>
          <div class="metric"><div class="lab">Live trading</div><div class="val" id="ovLiveAllowed">—</div></div>
        </div>
        <p class="mono" style="font-size:12px;margin:0 0 6px;line-height:1.5;" id="ovBlockers">—</p>
        <p class="empty-hint" style="margin:0;font-size:12px;line-height:1.5;" id="ovMomoMemo">—</p>
      </div>
      <div class="grid-metrics">
        <div class="metric"><div class="lab">Mode</div><div class="val mono" id="mMode">—</div></div>
        <div class="metric"><div class="lab">Total equity</div><div class="val mono" id="mEq">—</div></div>
        <div class="metric"><div class="lab">Live P&amp;L ($)</div><div class="val mono" id="mPnlD">—</div></div>
        <div class="metric"><div class="lab">Live P&amp;L (%)</div><div class="val mono" id="mPnlP">—</div></div>
        <div class="metric"><div class="lab">Cash</div><div class="val mono" id="mCash">—</div></div>
        <div class="metric"><div class="lab">Market</div><div class="val mono" id="mMkt">—</div></div>
        <div class="metric"><div class="lab">Capital stage</div><div class="val mono" id="mCap">—</div></div>
      </div>

      <div class="card capital-card" id="capitalStatusCard">
        <h2 style="margin:0 0 8px 0;font-size:0.95rem;font-weight:600;">Available Buying Power</h2>
        <div class="capital-main mono" id="capAvailMain" style="font-size:1.35rem;font-weight:600;">—</div>
        <p class="capital-sub" id="capAvailSub">Free cash available for new trades</p>
        <div class="capital-grid" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(138px,1fr));gap:8px;margin-top:10px;font-size:0.85rem;">
          <div><span class="muted">Cash</span><br><span class="mono" id="capCash">—</span></div>
          <div><span class="muted">Broker Buying Power</span><br><span class="mono" id="capBP">—</span></div>
          <div><span class="muted">Usable Buying Power</span><br><span class="mono" id="capUsable">—</span></div>
          <div><span class="muted">Capital Deployed</span><br><span class="mono" id="capDeployed">—</span></div>
        </div>
        <p id="capNewBuys" style="margin-top:10px;font-size:13px;color:var(--muted);">—</p>
        <p id="capWarn" style="display:none;margin-top:8px;color:#fbbf24;font-size:0.85rem;"></p>
      </div>

      <div class="card" id="capitalAllocatorCard">
        <h2 style="margin:0 0 8px 0;font-size:0.95rem;font-weight:600;">Capital Allocator</h2>
        <div class="grid-metrics" style="margin-bottom:10px;">
          <div class="metric"><div class="lab">Free cash</div><div class="val mono" id="dcaFreeCash">—</div></div>
          <div class="metric"><div class="lab">Crypto available cash</div><div class="val mono" id="dcaCryptoAvail">—</div></div>
          <div class="metric"><div class="lab">Stock value</div><div class="val mono" id="dcaStockMv">—</div></div>
          <div class="metric"><div class="lab">Crypto value</div><div class="val mono" id="dcaCryptoMv">—</div></div>
          <div class="metric"><div class="lab">PDT trapped stock</div><div class="val mono" id="dcaPdtTrap">—</div></div>
          <div class="metric"><div class="lab">Session-trapped stock</div><div class="val mono" id="dcaSessTrap">—</div></div>
          <div class="metric"><div class="lab">Target stock %</div><div class="val mono" id="dcaTgtStock">—</div></div>
          <div class="metric"><div class="lab">Target crypto %</div><div class="val mono" id="dcaTgtCrypto">—</div></div>
          <div class="metric"><div class="lab">Target reserve %</div><div class="val mono" id="dcaTgtRes">—</div></div>
          <div class="metric"><div class="lab">Actual stock %</div><div class="val mono" id="dcaActStock">—</div></div>
          <div class="metric"><div class="lab">Actual crypto %</div><div class="val mono" id="dcaActCrypto">—</div></div>
          <div class="metric"><div class="lab">Actual cash %</div><div class="val mono" id="dcaActCash">—</div></div>
          <div class="metric"><div class="lab">Recommended action</div><div class="val mono" id="dcaRecAct">—</div></div>
          <div class="metric"><div class="lab">Main blocker</div><div class="val mono" id="dcaBlocker">—</div></div>
        </div>
        <h3 style="margin:12px 0 6px;font-size:0.85rem;color:var(--muted);font-weight:600;">Crypto engine status</h3>
        <p class="mono" style="font-size:12px;margin:0;line-height:1.5;" id="dcaCryptoEngineLine">—</p>
        <h3 style="margin:12px 0 6px;font-size:0.85rem;color:var(--muted);font-weight:600;">Stock session status</h3>
        <p class="mono" style="font-size:12px;margin:0;line-height:1.5;" id="dcaStockSessionLine">—</p>
        <div class="chip-row" style="margin-top:10px;">
          <button type="button" class="tab-btn" style="font-size:12px;" id="btnCopyCapitalAllocatorJson">Copy Capital Allocator JSON</button>
          <span class="updated-stamp" id="dcaCopyStatus"></span>
        </div>
      </div>

      <div class="overview-split">
        <div class="card">
          <h2>Equity</h2>
          <div style="display:flex;gap:6px;margin-bottom:6px;align-items:center;">
            <button class="eq-range-btn eq-range-active" data-range="1D">1D</button>
            <button class="eq-range-btn" data-range="5D">5D</button>
            <button class="eq-range-btn" data-range="1W">1W</button>
            <button class="eq-range-btn" data-range="1M">1M</button>
            <button class="eq-range-btn" data-range="ALL">ALL</button>
            <span id="eqRangeChange" style="margin-left:auto;font-size:0.85rem;color:#9ca3af;"></span>
          </div>
          <div class="chart-wrap"><canvas id="equityChart"></canvas></div>
          <p class="empty-hint" id="eqEmptyHint" style="display:none;">No equity series returned.</p>
          <p class="empty-hint" id="eqSparseHint" style="display:none;color:#f59e0b;"></p>
          <p class="empty-hint" id="eqHistoryNote" style="display:none;margin-top:6px;"></p>
        </div>
        <div class="card ops-card" id="opsSummaryCard">
          <h2>Operator summary</h2>
          <ul class="ops-narrative" id="opsSummaryList">
            <li id="opsLineMode"     data-key="mode">Account mode unknown.</li>
            <li id="opsLineLive"     data-key="live">Live trading state unknown.</li>
            <li id="opsLineMarket"   data-key="market">Market state unknown.</li>
            <li id="opsLineCash"     data-key="cash">Cash available: N/A.</li>
            <li id="opsLinePositions" data-key="positions">Open positions: N/A.</li>
            <li id="opsLineBuys"     data-key="buys">New buys: status unknown.</li>
            <li id="opsLineStockExits" data-key="stock_exits">Stock exits: status unknown.</li>
            <li id="opsLineCryptoExits" data-key="crypto_exits">Crypto exits: allowed 24/7 only if broker quantity exists.</li>
            <li id="opsLineExitHealth" data-key="exit_health">Exit evaluation health: N/A.</li>
            <li id="opsLineLastCycle" data-key="last_cycle">Last cycle: N/A.</li>
          </ul>
        </div>
      </div>

      <div class="card exec-health-panel" id="execHealthPanel">
        <div class="exec-health-title-row">
          <h2 style="margin:0;font-size:0.95rem;font-weight:600;">Broker &amp; Execution Health</h2>
          <span id="execHealthSeverity" class="eh-severity ok" style="display:none;">OK</span>
        </div>
        <p class="eh-helper" id="execHealthHelper">
          Broker values are authoritative. Local rows are reconciled when broker quantity is zero.
          <strong>PDT</strong> badges list symbols where same-day stock exits were deferred.
        </p>
        <div id="execHealthBanner" class="eh-banner" style="display:none;"></div>
        <div class="eh-grid" id="execHealthGrid"></div>
        <div class="badge-row" id="pdtBadgeRowWrap" style="display:none;">
          <span class="lbl">PDT guarded symbols</span><span id="pdtBadgeRow"></span>
        </div>
        <details class="eh-details" id="exitRowsWrap">
          <summary>Position exit rows (<span id="exitRowsCount">0</span>)</summary>
          <p class="empty-hint" id="exitRowsEmpty" style="display:none;">No exit eligibility rows returned.</p>
          <div style="overflow-x:auto;">
            <table class="data" id="tblExitRows"><thead><tr>
              <th>Symbol</th><th>Class</th><th>Local qty</th><th>Broker qty</th><th>Recommended</th><th>Block reason</th><th>PDT</th><th>Cooldown</th><th>uPnL</th>
            </tr></thead><tbody></tbody></table>
          </div>
          <details id="mismatchDetailsSec" style="margin-top:0.75rem;display:none;">
            <summary>Mismatch details</summary>
            <p class="empty-hint" id="mismatchReconciledMsg" style="display:none;"></p>
            <div style="overflow-x:auto;">
              <table class="data" id="tblMismatchDetails"><thead><tr>
                <th>Symbol</th><th>Class</th><th>Broker qty</th><th>Local qty</th><th>Delta</th><th>Classification</th><th>Action</th>
              </tr></thead><tbody></tbody></table>
            </div>
          </details>
        </details>
      </div>

      <div class="card">
        <h2>Top open positions (5)</h2>
        <p class="empty-hint" id="posTopEmpty" style="display:none;">No positions returned.</p>
        <table class="data" id="tblOverviewPositions"><thead><tr>
          <th>Symbol</th><th>Qty</th><th>Entry</th><th>Current</th><th>uPnL %</th><th>Status</th>
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

    <section id="panel-positions" class="tab-panel cockpit-tab">
      <div class="tab-panel-header"><h2>Positions</h2><p>Broker-authoritative holdings, P&amp;L, and what happens next.</p></div>
      <div class="grid-metrics" style="margin-bottom:12px;">
        <div class="metric glass-card"><div class="lab">Market value</div><div class="val mono" id="posHdrMv">—</div></div>
        <div class="metric glass-card"><div class="lab">Open P&amp;L</div><div class="val mono" id="posHdrPnl">—</div></div>
        <div class="metric glass-card"><div class="lab">Pending exits</div><div class="val mono" id="posHdrPending">—</div></div>
        <div class="metric glass-card"><div class="lab">Broker alignment</div><div class="val" id="posHdrAlign">—</div></div>
      </div>
      <div class="card glass-card">
        <h2>All open positions</h2>
        <p class="empty-hint" id="posAllEmpty" style="display:none;">No positions returned.</p>
        <div class="scroll-table">
          <table class="data" id="tblPositionsFull"><thead><tr>
            <th>Symbol</th><th>Class</th><th>Opened</th><th>Qty</th><th>Entry</th><th>Current</th><th>Market Value</th><th>uPnL $</th><th>uPnL %</th><th>Exit Status</th><th>Explanation</th><th>Actions</th>
          </tr></thead><tbody></tbody></table>
        </div>
      </div>
    </section>

    <section id="panel-activity" class="tab-panel cockpit-tab">
      <div class="tab-panel-header"><h2>Activity</h2><p>Readable bot timeline — decisions, scans, orders, and real errors only.</p></div>
      <div class="activity-summary glass-card" id="activitySummary">
        <div class="metric"><div class="lab">Last decision</div><div class="val mono" id="actSumDecision">—</div></div>
        <div class="metric"><div class="lab">Crypto scan</div><div class="val mono" id="actSumCrypto">—</div></div>
        <div class="metric"><div class="lab">Recent trades</div><div class="val mono" id="actSumTrades">—</div></div>
      </div>
      <div class="activity-filters" id="activityFilters" role="group" aria-label="Activity filters">
        <button type="button" class="filter-btn active" data-act-filter="all">All</button>
        <button type="button" class="filter-btn" data-act-filter="orders">Orders</button>
        <button type="button" class="filter-btn" data-act-filter="crypto">Crypto</button>
        <button type="button" class="filter-btn" data-act-filter="stocks">Stocks</button>
        <button type="button" class="filter-btn" data-act-filter="warnings">Warnings</button>
        <button type="button" class="filter-btn" data-act-filter="errors">Errors</button>
      </div>
      <ul class="timeline-feed glass-card" id="activityTimeline" style="padding:10px 12px;margin-bottom:14px;display:none;"></ul>
      <div class="chip-row" style="margin-bottom:10px;">
        <button type="button" id="btnCopyActivityExport" class="tab-btn" style="font-size:12px;">Copy Activity JSON</button>
        <button type="button" id="btnDownloadActivityExport" class="tab-btn" style="font-size:12px;">Download Activity JSON</button>
        <button type="button" id="btnCopyBrokerDiagnostic" class="tab-btn" style="font-size:12px;">Copy Broker Diagnostic JSON</button>
        <div id="actExportProgress" class="mc-progress" style="margin-top:8px;"><div id="actExportProgressBar" class="mc-progress-bar"></div></div>
        <span class="updated-stamp" id="actExportStatus"></span>
        <span class="updated-stamp" id="brokerDiagExportStatus"></span>
      </div>
      <details class="section" id="actTradesSec" open>
        <summary>Recent trades (<span id="actTradesCount">0</span>)</summary>
        <div class="section-body">
          <p class="empty-hint" id="actTradesEmpty" style="display:none;">No trades returned.</p>
          <div class="scroll-table"><table class="data" id="tblActivityTrades"><thead><tr>
            <th>Time</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Price</th><th>Notional</th><th>Status</th>
          </tr></thead><tbody></tbody></table></div>
        </div>
      </details>
      <details class="section" id="actSigSec">
        <summary>Recent signals (<span id="actSigCount">0</span>)</summary>
        <div class="section-body">
          <p class="empty-hint" id="actSigEmpty" style="display:none;">No signals returned.</p>
          <div class="scroll-table"><table class="data" id="tblActivitySignals"><thead><tr>
            <th>Time</th><th>Symbol</th><th>Signal</th><th>Direction</th><th>Score</th>
          </tr></thead><tbody></tbody></table></div>
        </div>
      </details>
      <details class="section" id="actDecSec">
        <summary>Execution decisions (<span id="actDecCount">0</span>)</summary>
        <div class="section-body">
          <p class="empty-hint" id="actDecEmpty" style="display:none;">No execution decisions returned.</p>
          <div class="scroll-table"><table class="data" id="tblActivityDecisions"><thead><tr>
            <th>Time</th><th>Symbol</th><th>Side</th><th>Decision</th><th>Reason</th><th>Score</th>
          </tr></thead><tbody></tbody></table></div>
        </div>
      </details>
      <details class="section" id="actPerfSec">
        <summary>Performance &amp; calibration</summary>
        <div class="section-body">
          <p class="mono" id="actPerfLine">—</p>
          <p class="empty-hint" id="actCalEmpty" style="display:none;">No calibration rows.</p>
          <table class="data" id="tblCalibration"><thead><tr>
            <th>Leg</th><th>N</th><th>Acc %</th><th>Weight</th>
          </tr></thead><tbody></tbody></table>
        </div>
      </details>
      <details class="section" id="actStatusSec" hidden>
        <summary>Advanced — section status (raw)</summary>
        <div class="section-body">
          <pre class="sec mono" id="actSectionStatus">—</pre>
        </div>
      </details>
    </section>

    <section id="panel-backtest" class="tab-panel cockpit-tab">
      <div class="tab-panel-header"><h2>Backtest</h2><p>MoMo research lab — manual runs and strategy experiments (autonomous mode off unless enabled).</p></div>
      <div class="card bt-setup-card glass-card">
        <h2>Backtest Setup</h2>
        <div class="bt-grid">
          <div><label for="btStrategy">Strategy</label><select id="btStrategy"></select></div>
          <div style="grid-column: span 2;"><label for="btSymbols">Symbols (CSV)</label><input id="btSymbols" value="AAPL,MSFT"/></div>
          <div><label for="btStart">Start</label><input id="btStart" type="date" value="2025-01-01"/></div>
          <div><label for="btEnd">End</label><input id="btEnd" type="date" value="2026-01-01"/></div>
          <div><label for="btTimeframe">Timeframe</label><select id="btTimeframe"><option value="1Day">1Day</option><option value="1H">1H</option></select></div>
          <div><label for="btStartingCash">Starting cash</label><input id="btStartingCash" type="number" step="0.01" value="100"/></div>
        </div>
      </div>
      <div class="card bt-actions-card">
        <h2>Actions</h2>
        <div class="bt-actions">
          <button type="button" class="primary" id="btRunBtn">Run Backtest</button>
          <button type="button" id="btCompareBtn">Compare Strategies</button>
          <button type="button" id="btCopyReportBtn" disabled>Copy Report</button>
          <button type="button" id="btDownloadReportBtn" disabled>Download Report</button>
        </div>
        <p id="btStatus" class="bt-status-line" aria-live="polite">MoMo autonomous backtesting is not enabled yet. Manual backtest remains available.</p>
        <div id="btRunError" class="bt-run-error" role="alert" style="display:none;"></div>
      </div>

      <section id="btResultSummarySection" class="card bt-results-card" aria-labelledby="btResultSummaryHeading">
        <h2 id="btResultSummaryHeading">Backtest Result Summary</h2>
        <p id="btNoRunHint" class="empty-hint">No manual backtest run yet. Configure inputs and click Run Backtest.</p>
        <div id="btSummaryMetricsWrap" class="grid-metrics" style="display:none;">
          <div class="metric"><div class="lab">Starting Cash</div><div class="val mono" id="btMetricStartingCash">—</div></div>
          <div class="metric"><div class="lab">Final Equity</div><div class="val mono" id="btMetricFinalEquity">—</div></div>
          <div class="metric"><div class="lab">P&amp;L</div><div class="val mono" id="btMetricPnl">—</div></div>
          <div class="metric"><div class="lab">Return %</div><div class="val mono" id="btMetricReturnPct">—</div></div>
          <div class="metric"><div class="lab">Buy &amp; Hold Return</div><div class="val mono" id="btMetricBuyHold">—</div></div>
          <div class="metric"><div class="lab">Excess Return</div><div class="val mono" id="btMetricExcessReturn">—</div></div>
          <div class="metric"><div class="lab">Max Drawdown</div><div class="val mono" id="btMetricMaxDd">—</div></div>
          <div class="metric"><div class="lab">Total Trades</div><div class="val mono" id="btMetricTotalTrades">—</div></div>
          <div class="metric"><div class="lab">Closed Trades</div><div class="val mono" id="btMetricClosedTrades">—</div></div>
          <div class="metric"><div class="lab">Win Rate</div><div class="val mono" id="btMetricWinRate">—</div></div>
          <div class="metric"><div class="lab">Confidence Label</div><div class="val mono" id="btMetricConfidence">—</div></div>
        </div>
      </section>

      <div class="card">
        <h2>Equity Curve</h2>
        <div class="chart-wrap"><canvas id="btEquityChart"></canvas></div>
        <p class="empty-hint" id="btEqEmptyHint" style="display:none;">No equity curve for this run.</p>
      </div>

      <div class="card">
        <h2>Trades</h2>
        <p class="empty-hint" id="btTradesEmpty" style="display:none;">No trades in this run.</p>
        <div class="scroll-table">
          <table class="data" id="tblBacktestTrades"><thead><tr>
            <th>Time</th><th>Symbol</th><th>Class</th><th>Side</th><th>Qty</th><th>Price</th><th>PnL</th><th>Reason</th>
          </tr></thead><tbody></tbody></table>
        </div>
      </div>

      <details class="section" id="btAdvancedSec">
        <summary>Advanced — comparison, rejections, raw run data</summary>
        <div class="section-body">
          <h3 class="bt-sub">Strategy comparison</h3>
          <pre id="btCompareOutput" class="mono sec">Run Compare Strategies to see results here.</pre>
          <h3 class="bt-sub">Rejections summary</h3>
          <pre id="btRejectionsSummary" class="mono sec">—</pre>
          <h3 class="bt-sub">Last run (truncated JSON)</h3>
          <pre id="btLastRunDebug" class="mono sec">{}</pre>
        </div>
      </details>
    </section>

    <section id="panel-ai" class="tab-panel momo-tab">
      <div class="tab-panel-header momo-tab-header">
        <div class="momo-header-brand">
          <img src="/momo-logo.png" alt="" width="32" height="32" class="momo-header-logo"/>
          <div>
            <h2>MoMo Console</h2>
            <p>Observer intelligence — notes, patterns, proposals. Cannot trade live.</p>
          </div>
        </div>
      </div>
      <div class="card glass-card ai-hero momo-hero-card">
        <div class="ai-avatar momo-avatar-frame" aria-hidden="true"><img src="/momo-logo.png" alt="" class="momo-avatar-img"/></div>
        <div style="flex:1;min-width:200px;">
          <div class="status-badge warn" style="margin-bottom:8px;display:inline-flex;">Live trading not authorized</div>
          <p style="margin:0;font-size:12px;color:var(--muted);">MoMo observes, recommends, and proposes paper-mode config changes only. Operator approval required.</p>
        </div>
      </div>
      <div class="card glass-card">
        <h2 class="dash-section-title">MoMo Status</h2>
        <div class="grid-metrics" id="aiStatusMetrics">
          <div class="metric"><div class="lab">Provider</div><div class="val mono" id="aiProvider">—</div></div>
          <div class="metric"><div class="lab">Model</div><div class="val mono" id="aiModel">—</div></div>
          <div class="metric"><div class="lab">Observer</div><div class="val mono" id="aiEnabled">—</div></div>
          <div class="metric"><div class="lab">Notes</div><div class="val mono" id="aiNotesCount">—</div></div>
          <div class="metric"><div class="lab">Patterns</div><div class="val mono" id="aiPatternsCount">—</div></div>
          <div class="metric"><div class="lab">Skills</div><div class="val mono" id="aiSkillsCount">—</div></div>
          <div class="metric"><div class="lab">Last run</div><div class="val mono" id="aiLastRun">—</div></div>
        </div>
        <p id="aiStatusFootnote" style="margin:10px 0 0;font-size:12px;color:var(--muted);">
          Loading MoMo status…
        </p>
      </div>

      <div class="card glass-card momo-ask-card">
        <h2 class="dash-section-title">Ask MoMo</h2>
        <textarea id="aiChatInput" placeholder="e.g. Why did HAO not sell? What is the current capital allocation status?" rows="3" style="width:100%;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:6px;padding:8px;font-size:13px;resize:vertical;font-family:inherit;"></textarea>
        <div style="display:flex;gap:8px;margin-top:8px;align-items:center;flex-wrap:wrap;">
          <button type="button" class="btn primary" id="aiChatSend">Ask MoMo</button>
          <label style="font-size:12px;color:var(--muted);cursor:pointer;"><input type="checkbox" id="aiIncExport" checked> Activity export</label>
          <label style="font-size:12px;color:var(--muted);cursor:pointer;"><input type="checkbox" id="aiIncBroker"> Broker diagnostic</label>
          <label style="font-size:12px;color:var(--muted);cursor:pointer;"><input type="checkbox" id="aiIncMemory" checked> MoMo memory</label>
        </div>
        <div id="aiChatResult" class="momo-chat-result" style="display:none;">
          <div class="momo-chat-head"><img src="/momo-logo.png" alt="" width="22" height="22" class="momo-avatar-img"/><strong>MoMo</strong> <span id="aiChatProvider" class="mono" style="font-size:11px;color:var(--muted);"></span></div>
          <div id="aiChatAnswer" class="mono" style="font-size:13px;line-height:1.6;white-space:pre-wrap;"></div>
          <div id="aiChatEvidence" style="margin-top:8px;font-size:12px;color:var(--muted);"></div>
          <div id="aiChatActions" style="margin-top:8px;font-size:12px;"></div>
          <p class="momo-disclaimer">MoMo is observe-only. Cannot execute trades or change configuration.</p>
        </div>
      </div>

      <div class="card glass-card">
        <h2 class="dash-section-title">MoMo Memory Export</h2>
        <div class="mc-action-btns" style="margin-top:8px;">
          <button type="button" class="btn secondary" id="btnCopyAiMemories">Copy MoMo Memories</button>
          <button type="button" class="btn secondary" id="btnCopyFullAiBundle">Copy Full MoMo Bundle</button>
          <button type="button" class="btn secondary" id="btnDownloadAiMemories">Download MoMo Memories</button>
          <button type="button" class="btn secondary" id="btnDownloadFullAiBundle">Download Full MoMo Bundle</button>
        <div id="aiBundleProgress" class="mc-progress"><div id="aiBundleProgressBar" class="mc-progress-bar"></div></div>
        </div>
        <p id="aiMemoryCopyStatus" style="margin:8px 0 0;font-size:12px;color:var(--muted);"></p>
      </div>

      <div class="card glass-card">
        <h2 class="dash-section-title">Latest MoMo Notes</h2>
        <div class="scroll-table">
          <table class="data" id="tblAiNotes"><thead><tr>
            <th>Time</th><th>Severity</th><th>Category</th><th>Symbol</th><th>Finding</th><th>Action</th><th>Conf.</th>
          </tr></thead><tbody></tbody></table>
        </div>
      </div>

      <div class="card glass-card">
        <h2 class="dash-section-title">Patterns &amp; Skills</h2>
        <h3 style="font-size:0.85rem;font-weight:600;margin:0 0 6px;">Repeated Patterns</h3>
        <div class="scroll-table">
          <table class="data" id="tblAiPatterns"><thead><tr>
            <th>Pattern</th><th>Seen</th><th>Symbols</th><th>Risk</th><th>Conf.</th>
          </tr></thead><tbody></tbody></table>
        </div>
        <h3 style="font-size:0.85rem;font-weight:600;margin:12px 0 6px;">Candidate Skills</h3>
        <div class="scroll-table">
          <table class="data" id="tblAiSkills"><thead><tr>
            <th>Skill</th><th>Purpose</th><th>Status</th><th>Conf.</th><th>Executable</th>
          </tr></thead><tbody></tbody></table>
        </div>
      </div>
    </section>

    <section id="panel-ops" class="tab-panel cockpit-tab">
      <div class="tab-panel-header"><h2>Ops Center</h2><p>System health, timings, and diagnostics. Danger zone lives here only.</p></div>
      <div class="grid-metrics" style="margin-bottom:12px;">
        <div class="metric glass-card"><div class="lab">Last cycle</div><div class="val mono" id="opsHdrCycle">—</div></div>
        <div class="metric glass-card"><div class="lab">Next cycle</div><div class="val mono" id="opsHdrNext">—</div></div>
        <div class="metric glass-card"><div class="lab">System</div><div class="val" id="opsHdrHealth">—</div></div>
        <div class="metric glass-card"><div class="lab">Errors (24h)</div><div class="val mono" id="opsHdrErrors">—</div></div>
      </div>
      <div class="card glass-card">
        <h2 style="margin:0 0 8px;font-size:1rem;font-weight:600;">Resource monitor</h2>
        <p class="empty-hint" id="opsRailwayStatus" style="margin:0 0 10px;">Railway API: checking…</p>
        <p class="mono" id="opsSnapshotTime" style="font-size:12px;color:var(--muted);margin:0 0 8px;">Last snapshot: —</p>
        <div class="ops-rings" id="opsRings"></div>
        <div class="grid-metrics" style="margin-top:12px;">
          <div class="metric"><div class="lab">Ops logs (recent)</div><div class="val mono" id="opsLogCount">—</div></div>
          <div class="metric"><div class="lab">Critical logs</div><div class="val mono" id="opsCriticalCount">—</div></div>
          <div class="metric"><div class="lab">Cost pressure</div><div class="val mono" id="opsCostPressure">—</div></div>
          <div class="metric"><div class="lab">Uptime</div><div class="val mono" id="opsUptime">—</div></div>
        </div>
      </div>
      <div class="card">
        <h2 style="margin:0 0 8px;font-size:1rem;font-weight:600;">Ops export</h2>
        <div style="display:flex;gap:8px;flex-wrap:wrap;">
          <button type="button" id="btnCopyOpsStatus" class="tab-btn" style="font-size:12px;">Copy Ops Status JSON</button>
          <button type="button" id="btnCopyResourceSnapshot" class="tab-btn" style="font-size:12px;">Copy Resource Snapshot JSON</button>
          <button type="button" id="btnCopyRecentOpsLogs" class="tab-btn" style="font-size:12px;">Copy Recent Ops Logs JSON</button>
          <button type="button" id="btnCopyCriticalOpsBundle" class="tab-btn" style="font-size:12px;">Copy Critical Ops Bundle</button>
          <button type="button" id="btnDownloadOpsLogsCsv" class="tab-btn" style="font-size:12px;">Download Ops Logs CSV</button>
          <button type="button" id="btnDownloadDailyReportXlsx" class="tab-btn" style="font-size:12px;">Download Daily Report XLSX</button>
        <div id="opsExportProgress" class="mc-progress"><div id="opsExportProgressBar" class="mc-progress-bar"></div></div>
        </div>
        <p id="opsCopyStatus" style="margin:8px 0 0;font-size:12px;color:var(--muted);"></p>
      </div>
      <div class="danger-zone card" id="brokerTransitionCard">
        <h3>Broker Account Transition / Runtime Sync</h3>
        <p class="bt-headline" id="btWizardHeadline">Loading…</p>
        <p class="empty-hint" id="btWizardState" style="margin:0 0 8px;">Loading…</p>
        <div class="grid-metrics" style="margin-bottom:8px;">
          <div class="metric bt-metric"><div class="lab">Operator status</div><div class="val" id="btOperatorLabel">—</div></div>
          <div class="metric bt-metric"><div class="lab">Risk</div><div class="val" id="btRiskLevel">—</div></div>
          <div class="metric bt-metric"><div class="lab">Broker mode</div><div class="val mono" id="btBrokerMode">—</div></div>
          <div class="metric bt-metric"><div class="lab">Acceptance</div><div class="val" id="btAcceptance">—</div></div>
        </div>
        <p class="mono" id="btConfigLine" style="font-size:11px;color:var(--muted);margin:0 0 8px;"></p>
        <div class="bt-confirm" id="btConfirmBox" style="display:none;">
          <strong>Confirmation phrase:</strong> <span class="mono" id="btConfirmPhrase">—</span>
        </div>
        <div id="btPreviewSummary">
          <div class="bt-section"><h4>Preserved (not deleted)</h4><ul class="bt-list" id="btPreservedList"></ul></div>
          <div class="bt-section"><h4>Will clear on Apply</h4><ul class="bt-list" id="btClearList"></ul></div>
          <div class="bt-section"><h4>Stale local symbols (ghost — broker has none)</h4><ul class="bt-list" id="btStaleSymbolsList"></ul></div>
          <div class="bt-section"><h4>Broker snapshot (live Alpaca)</h4><ul class="bt-list" id="btBrokerSnapList"></ul></div>
        </div>
        <details class="bt-raw"><summary>Diagnostics (raw JSON)</summary><pre id="btPreviewRaw" class="sec" style="max-height:140px;font-size:10px;margin:8px 0 0;"></pre></details>
        <p class="mono" id="btMachineType" style="font-size:10px;color:var(--muted);margin:6px 0 0;"></p>
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px;">
          <button type="button" id="btnBtPreview" class="dash-action-btn">Refresh preview</button>
          <button type="button" id="btnBtApply" class="dash-action-btn dash-action-primary">Apply reset &amp; sync (auto-backup)</button>
          <button type="button" id="btnBtBackup" class="dash-action-btn">Backup only (optional)</button>
          <button type="button" id="btnBtAudit" class="dash-action-btn">Run acceptance audit</button>
          <button type="button" id="btnBtHistory" class="dash-action-btn">History</button>
        </div>
        <p id="btActionStatus" style="font-size:12px;color:var(--muted);margin:0;"></p>
      </div>
      <div class="danger-zone card">
        <h3>Danger zone</h3>
        <button type="button" id="btnOpsBackup" class="dash-action-btn">Backup DBs</button>
        <button type="button" id="btnOpsResetRuntime" class="dash-action-btn">Reset Runtime (legacy)</button>
      </div>
      <div class="card">
        <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;margin:0 0 8px;">
          <h2 style="margin:0;font-size:1rem;font-weight:600;">Recent ops logs</h2>
          <label style="font-size:12px;color:#9ca3af;">
            Level:
            <select id="opsLogLevelFilter" style="margin-left:6px;background:#0f172a;color:#e5e7eb;border:1px solid #334155;border-radius:4px;padding:2px 6px;font-size:12px;">
              <option value="all">All</option>
              <option value="critical">Critical</option>
              <option value="error">Error</option>
              <option value="warning">Warning</option>
              <option value="info">Info</option>
            </select>
          </label>
        </div>
        <div class="ops-log-preview">
          <table class="data" id="tblOpsLogs"><thead><tr>
            <th>Time</th><th>Level</th><th>Type</th><th>Message</th>
          </tr></thead><tbody></tbody></table>
        </div>
      </div>
    </section>

    <section id="panel-files" class="tab-panel cockpit-tab">
      <div class="tab-panel-header"><h2>Files</h2><p>Logs, GPT bundles, exports, and memory — no secrets on disk view.</p></div>
      <div class="files-vault-summary" id="filesVaultSummary">
        <div class="vault-card glass-card"><div class="vault-n" id="vaultBundles">—</div><div class="vault-l">GPT bundles</div></div>
        <div class="vault-card glass-card"><div class="vault-n" id="vaultLogs">—</div><div class="vault-l">Log files</div></div>
        <div class="vault-card glass-card"><div class="vault-n" id="vaultExports">—</div><div class="vault-l">Exports</div></div>
        <div class="vault-card glass-card"><div class="vault-n" id="vaultSize">—</div><div class="vault-l">Storage</div></div>
      </div>
      <div class="card glass-card">
        <h2 style="margin:0 0 6px;font-size:1rem;font-weight:600;">Volume files</h2>
        <p class="empty-hint" style="margin:0 0 10px;">
          Browse and edit bot storage (SQLite DBs, logs, exports). Paths stay inside the Railway persist volume.
          Editing <span class="mono">.sqlite</span> files while the worker runs can corrupt data — stop the worker or use copies.
        </p>
        <div class="vol-layout">
          <div>
            <div class="vol-toolbar" style="margin-bottom:0.5rem;">
              <label class="mono" style="font-size:11px;color:var(--muted);">Root</label>
              <select id="volRootSelect" aria-label="Volume root"></select>
              <button type="button" class="tab-btn" id="btnVolRefresh" style="font-size:12px;">Refresh</button>
            </div>
            <div class="vol-tree" id="volTree" aria-label="File tree"></div>
          </div>
          <div class="vol-editor-wrap">
            <div class="vol-toolbar">
              <button type="button" class="tab-btn" id="btnVolSave" style="font-size:12px;">Save</button>
              <button type="button" class="tab-btn" id="btnVolNewFile" style="font-size:12px;">New file</button>
              <button type="button" class="tab-btn" id="btnVolNewFolder" style="font-size:12px;">New folder</button>
              <button type="button" class="tab-btn" id="btnVolDelete" style="font-size:12px;">Delete</button>
              <button type="button" class="tab-btn" id="btnVolDownload" style="font-size:12px;">Download</button>
            </div>
            <p class="vol-breadcrumb" id="volBreadcrumb">/</p>
            <p id="volFileMeta">No file selected</p>
            <div id="volQuickPaths" class="vol-toolbar" style="margin-bottom:0.35rem;">
              <button type="button" class="tab-btn vol-quick" data-rel="" style="font-size:11px;">📂 volume root</button>
              <button type="button" class="tab-btn vol-quick" data-rel="quantbot.sqlite3" style="font-size:11px;">🗄 quantbot.sqlite3</button>
              <button type="button" class="tab-btn vol-quick" data-rel="ops.sqlite" style="font-size:11px;">🗄 ops.sqlite</button>
              <button type="button" class="tab-btn vol-quick" data-rel="logs" style="font-size:11px;">📁 logs/</button>
            </div>
            <div id="volSqlitePanel" class="card" style="display:none;padding:0.6rem;margin:0;">
              <h3 style="margin:0 0 6px;font-size:0.85rem;">SQLite tables</h3>
              <div id="volSqliteTables" class="vol-toolbar" style="margin-bottom:0.5rem;"></div>
              <pre id="volSqlitePreview" class="sec" style="max-height:220px;margin:0;"></pre>
            </div>
            <textarea id="volEditor" spellcheck="false" placeholder="Select a text file to edit, or a .sqlite3 file to browse tables…" disabled></textarea>
            <p id="volStatus"></p>
          </div>
        </div>
        <div class="danger-zone" style="margin-top:12px;">
          <h3>Danger zone</h3>
          <button type="button" id="btnFilesBackup" class="dash-action-btn">Backup DBs</button>
          <button type="button" id="btnFilesResetRuntime" class="dash-action-btn">Reset Runtime (legacy)</button>
        </div>
      </div>
    </section>

    <section id="panel-config" class="tab-panel cockpit-tab">
      <div class="tab-panel-header"><h2>Config</h2><p>Safe settings, MoMo proposals, and locked dangerous controls.</p></div>
      <div class="grid-metrics" style="margin-bottom:12px;">
        <div class="metric glass-card"><div class="lab">Config status</div><div class="val" id="cfgHdrStatus">—</div></div>
        <div class="metric glass-card"><div class="lab">Paper mode</div><div class="val" id="cfgHdrPaper">—</div></div>
        <div class="metric glass-card"><div class="lab">Pending changes</div><div class="val mono" id="cfgHdrPending">—</div></div>
        <div class="metric glass-card"><div class="lab">Live readiness</div><div class="val" id="cfgHdrLiveReady">—</div></div>
      </div>
      <div class="card glass-card">
        <h2 style="margin:0 0 8px;font-size:1rem;font-weight:600;">App configuration</h2>
        <p class="empty-hint" style="margin:0 0 10px;">
          Non-secret settings live in <span class="mono">bot_config</span>. Secrets stay in Railway env only.
          MoMo can recommend changes but cannot apply them — operator approval required.
        </p>
        <div class="mc-actions" style="margin-bottom:10px;">
          <button type="button" id="btnConfigSave" class="btn primary">Save changes</button>
          <button type="button" id="btnConfigExportSummary" class="btn secondary">Export config summary</button>
          <button type="button" id="btnConfigRailwayTpl" class="btn secondary">Export Railway env template</button>
        </div>
        <p id="configEditorStatus" class="empty-hint"></p>
        <div id="configEditorRoot"></div>
      </div>
    </section>

    <details class="section dev-diagnostics" id="devDiagnosticsSec" hidden>
      <summary>Developer diagnostics (Ops)</summary>
      <div class="section-body">
        <p class="dev-db-meta mono" id="devDbMeta">DB: {{ db }} · Poll every {{ refresh_sec }}s · <span id="pollFoot">HTTP only</span></p>
        <pre id="debugStateBlock">{}</pre>
      </div>
    </details>

    <div id="manualSellModal" class="modal-backdrop" aria-hidden="true">
      <div class="modal-box" role="dialog" aria-modal="true" aria-labelledby="msTitle">
        <h3 id="msTitle">Confirm manual sell</h3>
        <div id="msBody" class="mono" style="font-size:13px;line-height:1.6;"></div>
        <div class="modal-actions">
          <button type="button" class="btn-cancel" id="msCancel">Cancel</button>
          <button type="button" class="btn-sell" id="msConfirm">Sell position</button>
        </div>
      </div>
    </div>
      </main>
    </div>
  </div>

<script src="/dashboard-perf.js"></script>
<script src="/dashboard-app.js" defer></script>
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
    from monitoring.dashboard_data import (
        build_dashboard_payload,
        start_alpaca_background_cache_thread,
    )

    app = Flask(__name__)

    @app.get("/health")
    def health():
        """Railway liveness: no DB, Alpaca, or worker dependency."""
        return jsonify({"ok": True, "service": "quantbot-dashboard"}), 200

    @app.get("/api/debug-status")
    @app.get("/api/simple-status")
    def api_simple_status() -> Response:
        """
        Fast worker + account status — guaranteed < 500 ms, never blocks on Alpaca.
        Returns 200 even when the worker is stopped or the DB is empty.
        Includes: equity, cash, buying_power, mode, worker_health,
                  last_no_trade_reason, and the crypto block reason.
        """
        try:
            from monitoring.simple_status import build_simple_worker_status
            base = build_simple_worker_status()
        except Exception as exc:
            base = {"ok": False, "error": str(exc)[:120]}

        # Attach crypto block reason cheaply (DB + heartbeat only, no Alpaca REST)
        crypto_brief: dict[str, Any] = {}
        try:
            from core.paper_trading_path import load_runtime_config_for_worker
            from execution.crypto_trade_decision import build_crypto_trade_decision

            acct = base.get("account") or {}
            rt = load_runtime_config_for_worker(config.DB_PATH)
            dec = build_crypto_trade_decision({
                "rt": rt,
                "cash_available": acct.get("cash"),
                "buying_power": acct.get("buying_power"),
                "equity": acct.get("equity"),
            })
            crypto_brief = {
                "can_trade_crypto": dec.get("can_trade_crypto"),
                "reason_code": dec.get("reason_code"),
                "human_reason": dec.get("human_reason"),
                "push_allowed": dec.get("push_allowed"),
                "blockers": dec.get("blockers"),
            }
        except Exception as exc:
            crypto_brief = {"error": str(exc)[:120]}

        from core.deploy_info import resolve_deploy_info

        payload: dict[str, Any] = {
            **base,
            "crypto_status": crypto_brief,
            "mode": config.MODE,
            "git_commit": base.get("git_commit") or resolve_deploy_info().get("git_commit"),
            "deploy": base.get("deploy") or resolve_deploy_info(),
        }
        return Response(json.dumps(payload, default=str), mimetype="application/json")

    try:
        from backtesting.models import BacktestRequest
        from backtesting import runner as backtest_runner
        from backtesting import experiments as backtest_experiments
    except ImportError as exc:
        logger.warning("Backtesting unavailable (dashboard backtest API disabled): {}", exc)
        BacktestRequest = None  # type: ignore[misc, assignment]
        backtest_runner = None  # type: ignore[assignment]
        backtest_experiments = None  # type: ignore[assignment]

    def _backtest_unavailable() -> tuple[Any, int]:
        return jsonify({"ok": False, "error": "backtesting_module_unavailable"}), 503

    try:
        init_schema()
    except Exception as exc:
        logger.exception(
            "init_schema failed; /health still OK but DB-backed routes may fail: {}", exc
        )

    try:
        from monitoring.ai_observer import log_startup_status
        log_startup_status()
    except Exception as exc:
        logger.warning("[ai_memory] startup log failed: {}", str(exc)[:100])

    if not app.config.get("TESTING") and not os.environ.get("PYTEST_CURRENT_TEST"):
        start_alpaca_background_cache_thread()
        try:
            from monitoring.resource_monitor import start_resource_snapshot_collector
            start_resource_snapshot_collector(interval_sec=60.0, process_label="dashboard")
        except Exception as exc:
            logger.warning("[resource] snapshot collector failed to start: {}", exc)
        try:
            from monitoring.ops_log_store import write_ops_event
            write_ops_event(
                level="info",
                source="dashboard",
                event_type="startup",
                message="Momo dashboard started",
            )
        except Exception:
            logger.debug("[ops_log] dashboard startup event skipped", exc_info=True)
        try:
            from monitoring.telegram_momo import start_telegram_momo_polling
            start_telegram_momo_polling(owner="dashboard")
        except Exception as exc:
            logger.warning("[momo_telegram] start failed: {}", exc)

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
            with get_connection(timeout_sec=5.0) as conn:
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
            try:
                from monitoring.simple_status import build_simple_worker_status

                payload["simple_status"] = build_simple_worker_status()
                payload["overview_hint"] = (
                    (payload.get("simple_status") or {}).get("trading", {}).get("last_no_trade_reason")
                    or "no data yet"
                )
            except Exception:
                payload["simple_status"] = {"ok": False, "error": "simple_status_unavailable"}
                payload["overview_hint"] = "overview data unavailable"
            return payload
        except Exception:
            logger.exception("[dashboard] build_dashboard_payload failed — fallback open_conn")
            _debug_log("H8", "build_dashboard_payload_safe exception fallback", {"period": period})
            try:
                from monitoring.simple_status import build_simple_worker_status

                return {
                    "open_positions": [],
                    "recent_signals": [],
                    "equity_series": [],
                    "execution_health": {},
                    "simple_status": build_simple_worker_status(),
                    "overview_hint": "dashboard build failed — showing worker heartbeat only",
                }
            except Exception:
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

    @app.get("/api/equity/history")
    def api_equity_history() -> Response:
        from monitoring.account_history_store import fetch_account_history
        range_param = str(request.args.get("range", "1D") or "1D").upper().strip()
        data = fetch_account_history(range_param)
        if data.get("count", 0) < 3:
            from monitoring.dashboard_data import get_alpaca_background_snapshot
            period_map = {"1D": "1D", "5D": "1W", "1W": "1W", "1M": "1M", "ALL": "3M"}
            period = period_map.get(range_param, "1D")
            snap = get_alpaca_background_snapshot()
            curves = snap.get("equity_curves") or {}
            legacy = curves.get(period) if isinstance(curves, dict) else []
            if isinstance(legacy, list) and legacy:
                data["legacy_equity_series"] = legacy
                data["series"] = legacy
                data["warning"] = data.get("message") or f"Legacy equity only ({len(legacy)} pts)."
        return Response(json.dumps(data, default=str), mimetype="application/json")

    @app.get("/api/ops/status")
    def api_ops_status() -> Response:
        from monitoring.resource_monitor import resolve_resource_snapshot_for_api, fetch_railway_usage_hint
        from monitoring.usage_counters import build_runtime_cost_control_status
        snap = resolve_resource_snapshot_for_api()
        railway = fetch_railway_usage_hint()
        cost = build_runtime_cost_control_status(
            current_cycle_interval=30,
            recommended_cycle_interval=30,
            reason="dashboard_poll",
            railway_api_connected=bool(railway.get("railway_api_connected")),
        )
        return Response(json.dumps({
            "resource_snapshot": snap,
            "runtime_cost_control_status": cost,
            "railway": railway,
        }, default=str), mimetype="application/json")

    @app.get("/api/ops/railway/status")
    def api_ops_railway_status() -> Response:
        from monitoring.railway_status import get_railway_status

        force = str(request.args.get("force", "") or "").strip().lower() in ("1", "true", "yes")
        body = get_railway_status(force_refresh=force)
        return Response(json.dumps(body, default=str), mimetype="application/json")

    @app.get("/api/ops/critical-bundle")
    def api_ops_critical_bundle() -> Response:
        from monitoring.ops_log_store import fetch_ops_logs
        from monitoring.resource_monitor import resolve_resource_snapshot_for_api, fetch_railway_usage_hint
        from monitoring.usage_counters import increment_usage

        increment_usage("export_downloads")
        all_logs = fetch_ops_logs(limit=500)
        critical = [
            lg for lg in all_logs
            if str(lg.get("level") or "").lower() in ("critical", "error", "warning")
        ][:100]
        railway = fetch_railway_usage_hint()
        bundle = {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "resource_snapshot": resolve_resource_snapshot_for_api(),
            "railway": railway,
            "critical_logs": critical,
            "critical_log_count": len(critical),
            "recent_log_count": len(all_logs),
        }
        return Response(json.dumps(bundle, default=str), mimetype="application/json")

    @app.get("/api/ops/daily-report.xlsx")
    def api_ops_daily_report_xlsx() -> Response:
        from monitoring.ops_daily_report import build_daily_report_xlsx
        from monitoring.ops_log_store import fetch_ops_logs
        from monitoring.resource_monitor import resolve_resource_snapshot_for_api, fetch_railway_usage_hint
        from monitoring.usage_counters import build_runtime_cost_control_status, increment_usage

        increment_usage("export_downloads")
        snap = resolve_resource_snapshot_for_api()
        logs = fetch_ops_logs(limit=50)
        railway = fetch_railway_usage_hint()
        status = {
            "resource_snapshot": snap,
            "runtime_cost_control_status": build_runtime_cost_control_status(
                current_cycle_interval=30,
                recommended_cycle_interval=30,
                reason="daily_report",
                railway_api_connected=bool(railway.get("railway_api_connected")),
            ),
            "railway": railway,
        }
        data = build_daily_report_xlsx(
            resource_snapshot=snap,
            recent_logs=logs,
            ops_status=status,
        )
        fname = f"quantbot_daily_ops_{datetime.now(timezone.utc).strftime('%Y%m%d')}.xlsx"
        return Response(
            data,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    @app.get("/api/ops/resources/latest")
    def api_ops_resources_latest() -> Response:
        from monitoring.resource_monitor import resolve_resource_snapshot_for_api
        snap = resolve_resource_snapshot_for_api()
        return Response(json.dumps(snap or {}, default=str), mimetype="application/json")

    @app.get("/api/ops/resources/history")
    def api_ops_resources_history() -> Response:
        from monitoring.resource_monitor import fetch_resource_snapshots_history
        try:
            lim = max(1, min(500, int(request.args.get("limit", 50))))
        except ValueError:
            lim = 50
        items = fetch_resource_snapshots_history(lim)
        return Response(json.dumps({"items": items, "limit": lim}, default=str), mimetype="application/json")

    @app.get("/api/volume/roots")
    def api_volume_roots() -> Response:
        from monitoring.volume_files import volume_roots
        roots = {
            k: {"path": str(v), "label": k}
            for k, v in sorted(volume_roots().items())
        }
        return Response(
            json.dumps({"roots": roots, "db_path": str(config.DB_PATH)}, default=str),
            mimetype="application/json",
        )

    @app.get("/api/volume/list")
    def api_volume_list() -> Any:
        from monitoring import volume_files as vf
        root = str(request.args.get("root", "persist") or "persist")
        rel = str(request.args.get("path", "") or "")
        try:
            body = vf.list_directory(root, rel)
            return jsonify({"ok": True, **body})
        except (ValueError, FileNotFoundError, NotADirectoryError, PermissionError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/volume/read")
    def api_volume_read() -> Any:
        from monitoring import volume_files as vf
        root = str(request.args.get("root", "persist") or "persist")
        rel = str(request.args.get("path", "") or "")
        if not rel:
            return jsonify({"ok": False, "error": "path_required"}), 400
        try:
            body = vf.read_file(root, rel)
            return jsonify({"ok": True, **body})
        except (ValueError, FileNotFoundError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/volume/download")
    def api_volume_download() -> Any:
        from monitoring import volume_files as vf
        root = str(request.args.get("root", "persist") or "persist")
        rel = str(request.args.get("path", "") or "")
        if not rel:
            return jsonify({"ok": False, "error": "path_required"}), 400
        try:
            data, name, mime = vf.file_download_bytes(root, rel)
            return Response(
                data,
                mimetype=mime,
                headers={"Content-Disposition": f'attachment; filename="{name}"'},
            )
        except (ValueError, FileNotFoundError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.put("/api/volume/write")
    def api_volume_write() -> Any:
        if not _check_auth():
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        from monitoring import volume_files as vf
        body = request.get_json(force=True, silent=True) or {}
        root = str(body.get("root", "persist") or "persist")
        rel = str(body.get("path", "") or "")
        content = str(body.get("content", ""))
        create = bool(body.get("create", False))
        try:
            out = vf.write_file(root, rel, content, create=create)
            return jsonify({"ok": True, **out})
        except (ValueError, FileNotFoundError, IsADirectoryError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/volume/mkdir")
    def api_volume_mkdir() -> Any:
        if not _check_auth():
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        from monitoring import volume_files as vf
        body = request.get_json(force=True, silent=True) or {}
        root = str(body.get("root", "persist") or "persist")
        rel = str(body.get("path", "") or "")
        if not rel:
            return jsonify({"ok": False, "error": "path_required"}), 400
        try:
            out = vf.mkdir(root, rel)
            return jsonify({"ok": True, **out})
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/volume/sqlite/tables")
    def api_volume_sqlite_tables() -> Any:
        from monitoring import volume_files as vf
        root = str(request.args.get("root", "persist") or "persist")
        rel = str(request.args.get("path", "") or "")
        if not rel:
            return jsonify({"ok": False, "error": "path_required"}), 400
        try:
            body = vf.sqlite_list_tables(root, rel)
            return jsonify({"ok": True, **body})
        except (ValueError, FileNotFoundError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/volume/sqlite/preview")
    def api_volume_sqlite_preview() -> Any:
        from monitoring import volume_files as vf
        root = str(request.args.get("root", "persist") or "persist")
        rel = str(request.args.get("path", "") or "")
        table = str(request.args.get("table", "") or "")
        try:
            lim = int(request.args.get("limit", 40))
        except ValueError:
            lim = 40
        if not rel or not table:
            return jsonify({"ok": False, "error": "path_and_table_required"}), 400
        try:
            body = vf.sqlite_preview_table(root, rel, table, limit=lim)
            return jsonify({"ok": True, **body})
        except (ValueError, FileNotFoundError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/mission-control/summary")
    def api_mission_control_summary() -> Response:
        from monitoring.endpoint_timing import EndpointTimer
        from monitoring.mission_control_api import build_mission_control_summary
        from monitoring.mission_control_cache import get_mission_control_cached
        from monitoring.mission_control_api import build_mission_control_summary_fast

        force = str(request.args.get("force", "") or "").strip().lower() in ("1", "true", "yes")
        live = str(request.args.get("live", "") or "").strip().lower() in ("1", "true", "yes")
        full = str(request.args.get("full", "") or "").strip().lower() in ("1", "true", "yes")
        fast = str(request.args.get("fast", "") or "").strip().lower() in ("1", "true", "yes")
        timer = EndpointTimer("/api/mission-control/summary")
        if fast or (not force and not full):
            from monitoring.mission_control_api import build_mission_control_summary_minimal

            payload = build_mission_control_summary_minimal()
            payload["cache_hit"] = False
            payload["fast_path"] = True
        elif force and live:
            from monitoring.mission_control_api import build_mission_control_summary_full

            payload = build_mission_control_summary_full(live_broker=True)
            payload["cache_hit"] = False
            payload["live_broker_refresh"] = True
        elif full:
            from monitoring.mission_control_api import build_mission_control_summary_full

            payload = get_mission_control_cached(
                lambda: build_mission_control_summary_full(live_broker=False),
                force_refresh=force,
                ttl_sec=30.0,
                build_timeout_sec=12.0,
            )
        elif force:
            payload = build_mission_control_summary_fast(live_broker=live)
            payload["cache_hit"] = False
            payload["stale"] = False
            payload["live_broker_refresh"] = live
        else:
            payload = get_mission_control_cached(
                lambda: build_mission_control_summary_fast(live_broker=False),
                force_refresh=False,
                ttl_sec=8.0,
                build_timeout_sec=3.0,
            )
        if payload.get("simple_fallback") and "momo_status" not in payload:
            from monitoring.mission_control_api import build_mission_control_summary_minimal

            payload = build_mission_control_summary_minimal(
                degraded_reason=str(payload.get("degraded_reason") or "cache_incomplete")[:200],
            )
        ms = timer.finish(cache_hit=payload.get("cache_hit"))
        payload["backend_duration_ms"] = payload.get("backend_duration_ms") or ms
        if not payload or payload.get("ok") is False:
            from monitoring.mission_control_api import build_mission_control_summary_minimal

            payload = build_mission_control_summary_minimal(
                degraded_reason=str(payload.get("error") or "mission_control_unavailable")[:200],
            )
            payload["backend_duration_ms"] = ms
            payload["cache_hit"] = False
        body = json.dumps(payload, default=str)
        return Response(body, mimetype="application/json")

    @app.get("/api/account/history")
    def api_account_history() -> Response:
        from monitoring.account_history_store import fetch_account_history
        from monitoring.endpoint_timing import EndpointTimer
        rk = str(request.args.get("range", "1D") or "1D")
        timer = EndpointTimer("/api/account/history")
        data = fetch_account_history(rk)
        timer.finish()
        return Response(json.dumps(data, default=str), mimetype="application/json")

    @app.get("/api/symbols/metadata")
    def api_symbols_metadata() -> Response:
        """Batch symbol icon metadata (CDN URLs) for dashboard caching."""
        from monitoring.symbol_icons import resolve_symbols_metadata_batch

        raw = str(request.args.get("symbols", "") or "")
        entries: list[dict[str, str]] = []
        for part in raw.split(","):
            piece = part.strip()
            if not piece:
                continue
            if "|" in piece:
                sym, ac = piece.split("|", 1)
                entries.append({"symbol": sym.strip(), "asset_class": ac.strip().lower() or "stock"})
            else:
                entries.append({"symbol": piece, "asset_class": "stock"})
        items = resolve_symbols_metadata_batch(entries[:80])
        return Response(json.dumps({"items": items}, default=str), mimetype="application/json")

    @app.get("/api/symbol-icon")
    def api_symbol_icon() -> Response:
        """Resolve a stock/crypto logo URL for dashboard tables (redirect to CDN)."""
        from flask import redirect

        from monitoring.symbol_icons import resolve_symbol_icon

        info = resolve_symbol_icon(
            str(request.args.get("asset_class", "stock")),
            str(request.args.get("symbol", "")),
        )
        url = info.get("url")
        if not url:
            return Response(status=404)
        return redirect(url, code=302)

    @app.get("/api/config/schema")
    def api_config_schema() -> Response:
        from core.app_config_registry import build_config_schema
        return Response(json.dumps(build_config_schema(), default=str), mimetype="application/json")

    @app.get("/api/config/summary")
    def api_config_summary() -> Response:
        from core.app_config_registry import build_config_summary
        return Response(json.dumps(build_config_summary(), default=str), mimetype="application/json")

    @app.post("/api/config/update")
    def api_config_update() -> Any:
        if not _check_auth():
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        body = request.get_json(force=True, silent=True) or {}
        updates = body.get("updates") or ([body] if body.get("key") else [])
        from core.app_config_registry import apply_config_updates
        return jsonify(
            apply_config_updates(
                updates,
                operator_confirm=request.headers.get("X-Operator-Confirm"),
            )
        )

    @app.post("/api/config/reset-key")
    def api_config_reset_key() -> Any:
        if not _check_auth():
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        body = request.get_json(force=True, silent=True) or {}
        from core.app_config_registry import reset_config_key
        return jsonify(reset_config_key(str(body.get("key", "")).strip()))

    @app.get("/api/config/railway-env-template")
    def api_config_railway_template() -> Response:
        from core.app_config_registry import export_railway_env_template
        return Response(export_railway_env_template(), mimetype="text/plain")

    @app.get("/api/ops/storage-audit")
    def api_ops_storage_audit() -> Response:
        try:
            import os
            from tools.storage_audit import audit

            data_dir = os.environ.get("DATA_DIR") or os.environ.get("QUANTBOT_PERSIST_DIR") or "data"
            return Response(json.dumps(audit(data_dir), default=str), mimetype="application/json")
        except Exception as exc:
            return Response(
                json.dumps({"ok": False, "error": str(exc)[:200], "dbs": [], "corrupt_files": []}, default=str),
                mimetype="application/json",
                status=200,
            )

    @app.get("/api/connections/status")
    def api_connections_status() -> Response:
        from monitoring.connection_profiles import list_profiles

        return Response(json.dumps(list_profiles(), default=str), mimetype="application/json")

    @app.get("/api/ops/safe-flags")
    def api_ops_safe_flags() -> Response:
        from monitoring.dashboard_auth import safe_default_flags

        return Response(json.dumps(safe_default_flags(), default=str), mimetype="application/json")

    @app.get("/api/ops/fresh-start/preview")
    def api_fresh_start_preview() -> Response:
        out: dict[str, Any] = {"ok": True}
        try:
            from tools.fresh_start_runtime import preview
            out.update(preview({}))
        except Exception as exc:
            out = {
                "ok": False,
                "error": str(exc)[:200],
                "required_phrase": "FRESH START PAPER RUNTIME",
            }
        try:
            body = json.dumps(out, default=str)
        except Exception as exc:
            body = json.dumps({"ok": False, "error": f"json_encode_failed: {exc}", "required_phrase": "FRESH START PAPER RUNTIME"})
        return Response(body, mimetype="application/json", status=200)

    @app.post("/api/ops/fresh-start/apply")
    def api_fresh_start_apply() -> Any:
        from monitoring.dashboard_auth import admin_required, fresh_start_enabled
        from tools.fresh_start_runtime import apply as fs_apply
        from tools.fresh_start_runtime import REQUIRED_PHRASE

        if not fresh_start_enabled():
            return jsonify({"ok": False, "error": "fresh_start_disabled"}), 503
        body = request.get_json(force=True, silent=True) or {}
        phrase = str(body.get("confirmation_phrase") or "").strip()
        if phrase != REQUIRED_PHRASE:
            return jsonify({"ok": False, "error": "confirmation_phrase mismatch", "required": REQUIRED_PHRASE}), 400
        return jsonify(fs_apply(body.get("options") or {}, confirmation_phrase=phrase))

    @app.get("/api/ops/fresh-start/history")
    def api_fresh_start_history() -> Response:
        from tools.fresh_start_runtime import history

        return Response(json.dumps({"history": history()}, default=str), mimetype="application/json")

    @app.get("/api/monitoring/mode")
    def api_monitoring_mode() -> Response:
        from core.canonical_state import build_canonical_state
        from monitoring.monitoring_mode import build_monitoring_mode_summary

        try:
            ct = build_canonical_state()
        except Exception:
            ct = {}
        return Response(json.dumps(build_monitoring_mode_summary(ct), default=str), mimetype="application/json")

    @app.get("/api/momo/post_trade_reviews")
    def api_momo_post_trade_reviews() -> Response:
        from monitoring.momo_post_trade_review import fetch_post_trade_reviews

        return Response(
            json.dumps({"reviews": fetch_post_trade_reviews(limit=int(request.args.get("limit", 50)))}, default=str),
            mimetype="application/json",
        )

    @app.get("/api/momo/daily_pnl_autopsy")
    def api_momo_daily_pnl_autopsy() -> Response:
        from monitoring.momo_daily_pnl_autopsy import fetch_daily_autopsy

        return Response(
            json.dumps({"rows": fetch_daily_autopsy(limit=int(request.args.get("limit", 30)))}, default=str),
            mimetype="application/json",
        )

    @app.get("/api/momo/loss_patterns")
    def api_momo_loss_patterns() -> Response:
        from monitoring.momo_loss_pattern_detector import detect_loss_patterns

        return Response(json.dumps({"patterns": detect_loss_patterns()}, default=str), mimetype="application/json")

    @app.get("/api/momo/growth_projection")
    def api_momo_growth_projection() -> Response:
        from core.growth_projection import build_growth_projection_output
        from core.momo_brain import save_growth_projection
        from monitoring.equity_forensics import fetch_closed_trade_pnls

        try:
            from monitoring.canonical_account import resolve_canonical_account_metrics

            acct = resolve_canonical_account_metrics(live_broker=False) or {}
            current_eq = float(acct.get("equity") or 0.0)
        except Exception:
            current_eq = 200.0

        try:
            closed_trades = fetch_closed_trade_pnls()
        except Exception:
            closed_trades = []

        acceptance_pass = False
        live_readiness_ok = False
        try:
            from monitoring.live_readiness import build_live_readiness

            lr = build_live_readiness(account={"mode": "paper", "live_enabled": False})
            live_readiness_ok = bool(lr.get("live_allowed"))
        except Exception:
            pass

        risk_controls_present = False
        try:
            import core.risk_controls  # noqa: F401

            risk_controls_present = True
        except Exception:
            pass

        has_real_backtest = False
        has_paper_forward = False
        try:
            from core.momo_brain import _conn

            with _conn() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) FROM momo_parameter_proposals WHERE backtest_result_json IS NOT NULL AND backtest_result_json LIKE '%\"engine\": \"vectorbt\"%'"
                ).fetchone()
                has_real_backtest = bool(int(row[0]) if row else 0)
                row2 = conn.execute(
                    "SELECT COUNT(*) FROM momo_parameter_proposals WHERE paper_forward_result_json IS NOT NULL AND length(paper_forward_result_json) > 5"
                ).fetchone()
                has_paper_forward = bool(int(row2[0]) if row2 else 0)
        except Exception:
            pass

        projection = build_growth_projection_output(
            current_equity=current_eq,
            closed_trades=closed_trades,
            acceptance_pass=acceptance_pass,
            live_readiness_ok=live_readiness_ok,
            risk_controls_present=risk_controls_present,
            has_real_backtest=has_real_backtest,
            has_paper_forward=has_paper_forward,
        )
        try:
            save_growth_projection(projection)
        except Exception:
            pass
        return Response(json.dumps(projection, default=str), mimetype="application/json")

    @app.get("/api/momo/equity_forensics")
    def api_momo_equity_forensics() -> Response:
        from monitoring.equity_forensics import build_equity_forensics_report

        report = build_equity_forensics_report()
        return Response(json.dumps(report, default=str), mimetype="application/json")

    @app.post("/api/momo/ask")
    def api_momo_ask() -> Any:
        body = request.get_json(force=True, silent=True) or {}
        from monitoring.momo_ask import answer_momo_question
        inc = body.get("include") if isinstance(body.get("include"), dict) else None
        if inc is None:
            inc = {
                "mission_control": True,
                "canonical_truth": True,
                "momo_brain": True,
                "broker_diagnostic": True,
                "ops_logs": True,
                "order_flow": True,
                "momo_memory": True,
            }
        return jsonify(answer_momo_question(
            str(body.get("question", "")),
            include=inc,
        ))

    @app.post("/api/ops/gpt-analyze-bundle/send-telegram")
    def api_gpt_bundle_send_telegram() -> Any:
        if not _check_auth():
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        from monitoring.gpt_analyze_telegram import send_gpt_bundle_to_telegram
        return jsonify(send_gpt_bundle_to_telegram())

    @app.get("/api/telegram/status")
    def api_telegram_status() -> Response:
        """Return Telegram config status without making any Telegram API calls."""
        from monitoring.telegram_momo import build_telegram_momo_status
        return Response(json.dumps(build_telegram_momo_status(), default=str), mimetype="application/json")

    @app.post("/api/telegram/test-send")
    def api_telegram_test_send() -> Any:
        """Send a test message to the configured Telegram chat."""
        if not _check_auth():
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        from monitoring.telegram_momo import (
            build_telegram_momo_status,
            _send_reply,
            allowed_chat_id,
            telegram_can_send_without_polling,
        )
        from monitoring.gpt_analyze_telegram import telegram_send_config_errors
        st = build_telegram_momo_status()
        cfg_errors = telegram_send_config_errors()
        if cfg_errors:
            return jsonify({
                "ok": False, "sent": False, "config_errors": cfg_errors,
                "missing_config": cfg_errors, "reason": "; ".join(cfg_errors), "status": st,
            })
        if not telegram_can_send_without_polling():
            return jsonify({
                "ok": False, "sent": False,
                "reason": st.get("status_message") or "Telegram cannot send — check token and chat ID.",
                "missing_config": st.get("missing_config") or [],
                "status": st,
            })
        cid = allowed_chat_id()
        import config as _cfg
        msg = f"✅ QuantBot Telegram test from dashboard — mode={_cfg.MODE}. Momo is watching."
        sent = _send_reply(cid, msg)
        st = build_telegram_momo_status()
        return jsonify({
            "ok": sent, "sent": sent, "chat_id_hint": cid[:4] + "...",
            "status": st,
            "reason": None if sent else (st.get("last_error") or "Telegram API send failed"),
        })

    @app.get("/api/ops/gpt-analyze-bundle")
    def api_gpt_analyze_bundle() -> Response:
        from monitoring.gpt_analyze_bundle import build_gpt_analyze_bundle
        return Response(json.dumps(build_gpt_analyze_bundle(), default=str), mimetype="application/json")

    @app.get("/api/ops/gpt-analyze-bundle.json")
    def api_gpt_analyze_bundle_json() -> Response:
        from monitoring.gpt_analyze_bundle import build_gpt_analyze_bundle
        return Response(json.dumps(build_gpt_analyze_bundle(), default=str), mimetype="application/json")

    @app.get("/api/ops/gpt-analyze-bundle.txt")
    def api_gpt_analyze_bundle_txt() -> Response:
        from monitoring.gpt_analyze_bundle import build_gpt_analyze_bundle, bundle_as_text
        return Response(bundle_as_text(build_gpt_analyze_bundle()), mimetype="text/plain")

    @app.get("/api/ops/broker-transition/preview")
    def api_broker_transition_preview() -> Response:
        if not _check_auth():
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        from monitoring.broker_transition_service import preview_broker_transition

        return Response(json.dumps(preview_broker_transition(), default=str), mimetype="application/json")

    @app.post("/api/ops/broker-transition/apply")
    def api_broker_transition_apply() -> Any:
        if not _check_auth():
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        body = request.get_json(force=True, silent=True) or {}
        from monitoring.broker_transition_service import apply_broker_transition

        out = apply_broker_transition(
            transition_type_acknowledged=str(body.get("transition_type_acknowledged") or ""),
            confirmation_text=str(body.get("confirmation_text") or "").strip(),
            backup_first=bool(body.get("backup_first", True)),
            preserve_ai_memory=bool(body.get("preserve_ai_memory", True)),
            preserve_graphify=bool(body.get("preserve_graphify", True)),
            preserve_config=bool(body.get("preserve_config", True)),
            run_acceptance_audit=bool(body.get("run_acceptance_audit", True)),
            acknowledged_open_orders=bool(body.get("acknowledged_open_orders")),
            acknowledged_broker_positions=bool(body.get("acknowledged_broker_positions")),
            production_audit_url=str(body.get("production_audit_url") or "") or None,
            notes=str(body.get("notes") or "")[:500],
        )
        return jsonify(out), (200 if out.get("ok") else 400)

    @app.get("/api/ops/broker-transition/status")
    def api_broker_transition_status() -> Response:
        if not _check_auth():
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        from monitoring.broker_transition_service import build_transition_status

        return Response(json.dumps(build_transition_status(), default=str), mimetype="application/json")

    @app.post("/api/ops/broker-transition/audit")
    def api_broker_transition_audit() -> Response:
        if not _check_auth():
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        body = request.get_json(force=True, silent=True) or {}
        from monitoring.broker_transition_service import run_acceptance_audit_only

        out = run_acceptance_audit_only(production_url=str(body.get("production_url") or "") or None)
        return Response(json.dumps(out, default=str), mimetype="application/json")

    @app.get("/api/ops/broker-transition/history")
    def api_broker_transition_history() -> Response:
        if not _check_auth():
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        from monitoring.broker_transition_service import fetch_transition_history

        return Response(
            json.dumps({"history": fetch_transition_history()}, default=str),
            mimetype="application/json",
        )

    @app.post("/api/ops/backup-dbs")
    def api_ops_backup_dbs() -> Any:
        if not _check_auth():
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        from monitoring.runtime_reset import backup_databases
        return jsonify(backup_databases())

    @app.post("/api/ops/reset-runtime")
    def api_ops_reset_runtime() -> Any:
        if not _check_auth():
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        body = request.get_json(force=True, silent=True) or {}
        confirm = str(body.get("confirm", "") or "").strip().upper()
        if confirm != "RESET RUNTIME":
            return jsonify({"ok": False, "error": "confirm must be RESET RUNTIME"}), 400
        from monitoring.runtime_reset import reset_runtime_state
        from monitoring.telegram_momo_updates import send_momo_update
        out = reset_runtime_state(include_cycle_logs=bool(body.get("include_cycle_logs")))
        send_momo_update(action="runtime_reset_completed", reason="operator_reset")
        return jsonify(out)

    @app.post("/api/ops/reset-momo-memory")
    def api_ops_reset_momo_memory() -> Any:
        if not _check_auth():
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        body = request.get_json(force=True, silent=True) or {}
        if str(body.get("confirm", "")).strip().upper() != "RESET MOMO MEMORY":
            return jsonify({"ok": False, "error": "confirm must be RESET MOMO MEMORY"}), 400
        from monitoring.runtime_reset import reset_momo_memory
        return jsonify(reset_momo_memory())

    @app.get("/api/memory/state-summary")
    def api_memory_state_summary() -> Response:
        from core.memory_state import build_memory_state_summary
        return Response(json.dumps(build_memory_state_summary(), default=str), mimetype="application/json")

    @app.post("/api/momo/backtest/run")
    def api_momo_backtest_run() -> Any:
        if not _check_auth():
            return jsonify({"error": "unauthorized"}), 401
        body = request.get_json(force=True, silent=True) or {}
        from monitoring.momo_backtest import run_momo_backtest
        return jsonify(run_momo_backtest(strategy_name=str(body.get("strategy_name", "current_adaptive"))))

    @app.get("/api/momo/backtest/latest")
    def api_momo_backtest_latest() -> Response:
        from monitoring.momo_backtest import fetch_momo_backtest_latest
        return Response(json.dumps(fetch_momo_backtest_latest(), default=str), mimetype="application/json")

    @app.post("/api/momo/backtest/recommend")
    def api_momo_backtest_recommend() -> Any:
        from monitoring.momo_backtest import recommend_from_backtest
        return jsonify(recommend_from_backtest())

    @app.get("/api/telegram/momo/status")
    def api_telegram_momo_status() -> Response:
        from monitoring.telegram_momo import build_telegram_momo_status
        return Response(json.dumps(build_telegram_momo_status(), default=str), mimetype="application/json")

    @app.delete("/api/volume/delete")
    def api_volume_delete() -> Any:
        if not _check_auth():
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        from monitoring import volume_files as vf
        body = request.get_json(force=True, silent=True) or {}
        root = str(body.get("root", "persist") or body.get("root", "persist"))
        if request.args.get("root"):
            root = str(request.args.get("root"))
        rel = str(body.get("path", "") or request.args.get("path", "") or "")
        if not rel:
            return jsonify({"ok": False, "error": "path_required"}), 400
        try:
            out = vf.delete_path(root, rel)
            return jsonify({"ok": True, **out})
        except (ValueError, FileNotFoundError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/ops/buying-power-diagnostic")
    def api_ops_buying_power_diagnostic() -> Response:
        from monitoring.buying_power_diagnostic import build_buying_power_diagnostic
        from monitoring.dashboard_data import get_alpaca_background_snapshot
        from monitoring.mission_control_api import build_mission_control_summary_fast

        mc = build_mission_control_summary_fast()
        ac = mc.get("account") or {}
        cp = mc.get("capital_protection") or {}
        snap = get_alpaca_background_snapshot()
        out = build_buying_power_diagnostic(
            equity=float(ac.get("equity") or 0),
            cash=float(ac.get("cash") or 0),
            buying_power=float(ac.get("buying_power") or 0),
            positions_count=int((mc.get("positions") or {}).get("count") or 0),
            broker_snapshot=snap,
            allocator=cp.get("allocator") or {},
            execution_health={},
            dynamic_profile=cp.get("dynamic_profile") or {},
        )
        return Response(json.dumps(out, default=str), mimetype="application/json")

    @app.get("/api/ops/why-no-trade")
    def api_ops_why_no_trade() -> Response:
        return Response(json.dumps({"reasons": ["see_execution_health_and_cycle_summary"]}, default=str), mimetype="application/json")

    @app.get("/api/ops/why-no-sell")
    def api_ops_why_no_sell() -> Response:
        return Response(json.dumps({"reasons": ["see_position_exit_decisions"]}, default=str), mimetype="application/json")

    @app.get("/api/ops/cycle-journal/recent")
    def api_ops_cycle_journal_recent() -> Response:
        from monitoring.ops_log_store import fetch_cycle_journal_recent

        try:
            lim = max(1, min(200, int(request.args.get("limit", 20))))
        except ValueError:
            lim = 20
        rows = fetch_cycle_journal_recent(limit=lim)
        return Response(json.dumps({"items": rows}, default=str), mimetype="application/json")

    @app.get("/api/ops/cycle-journal")
    def api_ops_cycle_journal() -> Response:
        from monitoring.ops_log_store import fetch_cycle_journal_by_id

        cid = str(request.args.get("cycle_id") or "").strip()
        row = fetch_cycle_journal_by_id(cycle_id=cid) if cid else None
        return Response(json.dumps({"cycle": row}, default=str), mimetype="application/json")

    @app.get("/api/ops/logs")
    def api_ops_logs() -> Response:
        from monitoring.ops_log_store import fetch_ops_logs
        from monitoring.usage_counters import increment_usage
        increment_usage("dashboard_requests")
        level = request.args.get("level")
        logs = fetch_ops_logs(
            limit=int(request.args.get("limit", 200)),
            level=level,
            event_type=request.args.get("event_type"),
            symbol=request.args.get("symbol"),
            cycle_id=request.args.get("cycle_id"),
            reason_code=request.args.get("reason_code"),
            search=request.args.get("search"),
        )
        payload: dict[str, Any] = {"logs": logs, "count": len(logs)}
        if level and str(level).lower() == "error":
            payload["no_errors"] = len(logs) == 0
            if not logs:
                payload["message"] = "No error-level ops events in the requested window."
        return Response(json.dumps(payload, default=str), mimetype="application/json")

    @app.get("/api/ops/logs/export.csv")
    def api_ops_logs_csv() -> Response:
        import csv
        import io
        from datetime import datetime, timezone
        from monitoring.ops_log_store import fetch_ops_logs
        logs = fetch_ops_logs(limit=int(request.args.get("limit", 500)))
        buf = io.StringIO()
        if logs:
            w = csv.DictWriter(buf, fieldnames=list(logs[0].keys()), extrasaction="ignore")
            w.writeheader()
            w.writerows(logs)
        fname = f"ops_logs_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv"
        return Response(
            buf.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    @app.get("/api/ops/logs/export.json")
    def api_ops_logs_export_json() -> Response:
        from datetime import datetime, timezone
        from monitoring.ops_log_store import fetch_ops_logs
        from monitoring.usage_counters import increment_usage
        increment_usage("export_downloads")
        logs = fetch_ops_logs(limit=int(request.args.get("limit", 500)))
        fname = f"ops_logs_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
        return Response(
            json.dumps({"logs": logs, "count": len(logs)}, default=str, indent=2),
            mimetype="application/json",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    @app.get("/api/ops/logs/export.txt")
    def api_ops_logs_export_txt() -> Response:
        from datetime import datetime, timezone
        from monitoring.ops_log_store import fetch_ops_logs
        from monitoring.usage_counters import increment_usage
        increment_usage("export_downloads")
        logs = fetch_ops_logs(limit=int(request.args.get("limit", 500)))
        lines = []
        for lg in logs:
            lines.append(
                f"{lg.get('created_at', '')} [{lg.get('level', '')}] "
                f"{lg.get('event_type', '')} {lg.get('message', '')}"
            )
        body = "\n".join(lines) if lines else "No ops logs."
        fname = f"ops_logs_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.txt"
        return Response(
            body,
            mimetype="text/plain",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

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

    @app.post("/api/positions/sell")
    def api_positions_sell() -> Any:
        if not _check_auth():
            return jsonify(
                {"ok": False, "symbol": "", "reason_code": "unauthorized", "message": "Unauthorized."}
            ), 401
        body = request.get_json(silent=True) or {}
        import uuid as _uuid

        cycle = _uuid.uuid4().hex[:12]
        from monitoring.manual_positions import try_manual_sell

        out = try_manual_sell(
            symbol=str(body.get("symbol") or ""),
            asset_class=str(body.get("asset_class") or "stock"),
            quantity=str(body.get("quantity") or ""),
            confirm=bool(body.get("confirm")),
            cycle_id=cycle,
        )
        status = 200 if out.get("ok") else 400
        return jsonify(out), status

    @app.get("/api/activity/export")
    def api_activity_export() -> Response:
        limit_raw = request.args.get("limit", "50")
        try:
            lim = max(1, min(100, int(str(limit_raw))))
        except ValueError:
            lim = 50
        from monitoring.cycle_activity_export import build_activity_export_payload
        from monitoring.dashboard_data import _open_dashboard_sqlite

        with _open_dashboard_sqlite() as conn:
            payload = build_activity_export_payload(conn, limit=lim)
        return Response(json.dumps(payload, default=str), mimetype="application/json")

    @app.get("/api/broker/diagnostic")
    def api_broker_diagnostic() -> Response:
        from monitoring.broker_diagnostic import build_broker_diagnostic_payload, diagnostic_json_bytes
        from monitoring.dashboard_data import _open_dashboard_sqlite

        with _open_dashboard_sqlite() as conn:
            payload = build_broker_diagnostic_payload(conn)
        return Response(diagnostic_json_bytes(payload), mimetype="application/json")

    @app.get("/api/rotation/latest")
    def api_rotation_latest() -> Any:
        from execution.capital_rotation import fetch_latest_rotation_plan

        plan = fetch_latest_rotation_plan(str(config.DB_PATH))
        if plan is None:
            return jsonify(
                {
                    "ok": True,
                    "rotation_plan": None,
                    "message": "No rotation plan recorded yet",
                }
            )
        return jsonify({"ok": True, "rotation_plan": plan})

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
        if BacktestRequest is None or backtest_runner is None:
            return _backtest_unavailable()
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
            parameter_snapshot = {"backtest_config": bt_cfg}
            result = backtest_runner.execute(req, parameter_snapshot=parameter_snapshot)
            req_json = json.dumps(req.__dict__, default=str)
            param_json = json.dumps({"backtest_config": bt_cfg}, default=str)
            equity_rows = [p.__dict__ for p in result.equity_curve]
            trade_rows = [t.__dict__ for t in result.trades]
            rejection_rows = [r.__dict__ for r in result.rejections]
            signal_rows = [s.__dict__ for s in (getattr(result, "signal_events", []) or [])]
            summary_json = json.dumps(result.summary_json, default=str)
            rejection_summary_json = json.dumps(result.rejection_summary_json, default=str)

            def _persist_backtest() -> int:
                rid = data_store.create_backtest_run(
                    req_json,
                    strategy_name=req.strategy_name,
                    status=result.status,
                    parameter_snapshot_json=param_json,
                )
                data_store.insert_backtest_equity_curve(rid, equity_rows)
                data_store.insert_backtest_trades(rid, trade_rows)
                data_store.insert_backtest_rejections(rid, rejection_rows)
                data_store.insert_backtest_signal_events(rid, signal_rows)
                data_store.update_backtest_status(
                    rid,
                    status=result.status,
                    summary_json=summary_json,
                    rejection_summary_json=rejection_summary_json,
                )
                return rid

            run_id = data_store.with_sqlite_retry(_persist_backtest)
            return jsonify({"ok": True, "run_id": run_id, "status": result.status})
        except Exception as exc:
            logger.exception("api/backtest/run failed")
            import sqlite3 as _sqlite3
            msg = str(exc)
            if isinstance(exc, _sqlite3.OperationalError) and "locked" in msg.lower():
                msg = (
                    "Database is busy (worker and dashboard share the same SQLite file). "
                    "Wait a few seconds and try again."
                )
            return jsonify({"ok": False, "error": msg}), 400

    @app.post("/api/backtest/compare")
    def api_backtest_compare() -> Any:
        if BacktestRequest is None or backtest_runner is None:
            return _backtest_unavailable()
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
        if BacktestRequest is None or backtest_experiments is None:
            return _backtest_unavailable()
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

    @app.get("/momo-logo.png")
    def momo_logo() -> Response:
        if not _DASHBOARD_LOGO_PATH.is_file():
            return Response(status=404)
        return Response(
            _DASHBOARD_LOGO_PATH.read_bytes(),
            mimetype="image/png",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.get("/dashboard-theme.css")
    def dashboard_theme_css() -> Response:
        if not _DASHBOARD_THEME_PATH.is_file():
            return Response("/* theme missing */", status=404, mimetype="text/css")
        return Response(
            _DASHBOARD_THEME_PATH.read_text(encoding="utf-8"),
            mimetype="text/css",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/dashboard-perf.js")
    def dashboard_perf_js() -> Response:
        if not _DASHBOARD_PERF_JS_PATH.is_file():
            return Response("window.MomoDashPerf={};\n", mimetype="application/javascript")
        return Response(
            _DASHBOARD_PERF_JS_PATH.read_text(encoding="utf-8"),
            mimetype="application/javascript",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/dashboard-app.js")
    def dashboard_app_js() -> Response:
        """Serve dashboard bundle as its own file — avoids HTML `</script>` truncation bugs."""
        if not _DASHBOARD_APP_JS_PATH.is_file():
            return Response(
                "console.error('dashboard_app.js missing on server');\n",
                status=500,
                mimetype="application/javascript",
            )
        return Response(
            _DASHBOARD_APP_JS_PATH.read_text(encoding="utf-8"),
            mimetype="application/javascript",
            headers={"Cache-Control": "no-store"},
        )

    # ── AI Console / Momo endpoints ─────────────────────────────────────
    @app.get("/api/ai/status")
    def api_ai_status() -> Response:
        from monitoring.ai_observer import get_ai_status
        data = get_ai_status()
        return Response(json.dumps(data, default=str), mimetype="application/json")

    @app.post("/api/ai/chat")
    def api_ai_chat() -> Response:
        from monitoring.ai_observer import handle_chat
        body = request.get_json(silent=True) or {}
        message = str(body.get("message", "")).strip()
        if not message:
            return Response(json.dumps({
                "ok": False, "error": "message is required",
                "allowed_to_execute": False,
            }), status=400, mimetype="application/json")
        result = handle_chat(
            message,
            include_activity_export=bool(body.get("include_activity_export", True)),
            include_broker_diagnostic=bool(body.get("include_broker_diagnostic", False)),
            include_memory=bool(body.get("include_memory", True)),
        )
        return Response(json.dumps(result, default=str), mimetype="application/json")

    # ── AI Observer endpoints ──────────────────────────────────────────────
    @app.get("/api/ai/observer/latest")
    def api_ai_observer_latest() -> Response:
        from monitoring.ai_observer import fetch_latest_notes
        limit = int(request.args.get("limit", 50) or 50)
        notes = fetch_latest_notes(limit=limit)
        return Response(json.dumps({"notes": notes}, default=str), mimetype="application/json")

    @app.get("/api/ai/observer/history")
    def api_ai_observer_history() -> Response:
        from monitoring.ai_observer import fetch_latest_notes
        limit = int(request.args.get("limit", 50) or 50)
        notes = fetch_latest_notes(limit=limit)
        return Response(json.dumps({"notes": notes}, default=str), mimetype="application/json")

    @app.get("/api/ai/patterns")
    def api_ai_patterns() -> Response:
        from monitoring.ai_observer import fetch_patterns
        patterns = fetch_patterns(limit=50)
        return Response(json.dumps({"patterns": patterns}, default=str), mimetype="application/json")

    @app.get("/api/ai/skills")
    def api_ai_skills() -> Response:
        from monitoring.ai_observer import fetch_skills
        skills = fetch_skills(limit=50)
        return Response(json.dumps({"skills": skills}, default=str), mimetype="application/json")

    @app.get("/api/ai/memory/export")
    def api_ai_memory_export() -> Response:
        from monitoring.ai_observer import export_memory
        data = export_memory()
        return Response(json.dumps(data, default=str), mimetype="application/json")

    @app.get("/api/ai/memories/export")
    def api_ai_memories_export() -> Response:
        from monitoring.ai_observer import build_ai_memories_export
        data = build_ai_memories_export()
        return Response(json.dumps(data, default=str), mimetype="application/json")

    @app.get("/api/ai/bundle/export")
    def api_ai_bundle_export() -> Response:
        from monitoring.ai_observer import build_ai_bundle_export
        data = build_ai_bundle_export()
        return Response(json.dumps(data, default=str), mimetype="application/json")

    @app.post("/api/ai/skills/<int:skill_id>/approve_observe_only")
    def api_ai_skill_approve(skill_id: int) -> Any:
        if not _check_auth():
            return jsonify({"error": "unauthorized"}), 401
        from monitoring.ai_observer import get_ai_memory_connection, approve_skill_observe_only
        conn = get_ai_memory_connection()
        ok = approve_skill_observe_only(conn, skill_id)
        conn.close()
        return jsonify({"ok": ok, "status": "approved_observe_only", "allowed_to_execute": False})

    @app.post("/api/ai/skills/<int:skill_id>/reject")
    def api_ai_skill_reject(skill_id: int) -> Any:
        if not _check_auth():
            return jsonify({"error": "unauthorized"}), 401
        from monitoring.ai_observer import get_ai_memory_connection, reject_skill
        conn = get_ai_memory_connection()
        ok = reject_skill(conn, skill_id)
        conn.close()
        return jsonify({"ok": ok, "status": "rejected", "allowed_to_execute": False})

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
