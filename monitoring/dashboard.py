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
  <input type="hidden" id="dash-secret-holder" value="{{ dashboard_secret|e }}"/>
  <header>
    <h1 class="mono">QuantBot</h1>
    <div class="header-meta">
      <span id="dashUpdatedAt" class="updated-stamp">Updated —</span>
      <div class="chip-row" id="statusChips">
        <span class="chip" id="chipMode" data-state="info"><span class="dot"></span><span class="chip-text">— mode</span></span>
        <span class="chip" id="chipLive" data-state="info"><span class="dot"></span><span class="chip-text">Live —</span></span>
        <span class="chip" id="chipApi" data-state="info"><span class="dot"></span><span class="chip-text">API connecting…</span></span>
        <span class="chip info" id="chipPoll"><span class="dot"></span><span class="chip-text">Poll 30s</span></span>
      </div>
    </div>
  </header>
  <div id="dashError" role="alert"></div>
  <div id="dashToast" role="status" aria-live="polite"></div>
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

      <div class="overview-split">
        <div class="card">
          <h2>Equity</h2>
          <div class="chart-wrap"><canvas id="equityChart"></canvas></div>
          <p class="empty-hint" id="eqEmptyHint" style="display:none;">No equity series returned.</p>
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

    <section id="panel-positions" class="tab-panel">
      <div class="card">
        <h2>All open positions</h2>
        <p class="empty-hint" id="posAllEmpty" style="display:none;">No positions returned.</p>
        <div class="scroll-table">
          <table class="data" id="tblPositionsFull"><thead><tr>
            <th>Symbol</th><th>Class</th><th>Opened</th><th>Qty</th><th>Entry</th><th>Current</th><th>Market Value</th><th>uPnL $</th><th>uPnL %</th><th>Exit Status</th><th>Explanation</th><th>Actions</th>
          </tr></thead><tbody></tbody></table>
        </div>
      </div>
    </section>

    <section id="panel-activity" class="tab-panel">
      <div class="chip-row" style="margin-bottom:10px;">
        <button type="button" id="btnCopyActivityExport" class="tab-btn" style="font-size:12px;">Copy Activity JSON</button>
        <button type="button" id="btnDownloadActivityExport" class="tab-btn" style="font-size:12px;">Download Activity JSON</button>
        <button type="button" id="btnCopyBrokerDiagnostic" class="tab-btn" style="font-size:12px;">Copy Broker Diagnostic JSON</button>
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
      <details class="section" id="actStatusSec">
        <summary>Section status (raw)</summary>
        <div class="section-body">
          <pre class="sec mono" id="actSectionStatus">—</pre>
        </div>
      </details>
    </section>

    <section id="panel-backtest" class="tab-panel">
      <div class="card bt-setup-card">
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
        <p id="btStatus" class="bt-status-line" aria-live="polite">Open this tab to load defaults, then configure and run.</p>
        <div id="btRunError" class="bt-run-error" role="alert" style="display:none;"></div>
      </div>

      <section id="btResultSummarySection" class="card bt-results-card" aria-labelledby="btResultSummaryHeading">
        <h2 id="btResultSummaryHeading">Backtest Result Summary</h2>
        <p id="btNoRunHint" class="empty-hint">No backtest run yet. Configure inputs and click Run Backtest.</p>
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

    <details class="section dev-diagnostics" id="devDiagnosticsSec">
      <summary>Developer diagnostics</summary>
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
