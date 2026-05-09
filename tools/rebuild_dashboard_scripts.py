#!/usr/bin/env python3
"""Replace dashboard.py inline scripts with cockpit bundle + isolated backtest bundle."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASH = Path(__file__).resolve().parents[1] / "monitoring" / "dashboard.py"

COCKPIT = r'''
<script>
(function () {
  "use strict";
  window.DISABLE_OLD_DASHBOARD_LIVE = true;
  var REFRESH_MS = {{ refresh_sec }} * 1000;
  var DASHBOARD_SECRET = {{ dashboard_secret|tojson }};
  var ACTIVE_TAB_KEY = "quantbot_active_tab";
  var EQUITY_RANGE_KEY = "quantbot_equity_range";
  var POLL_MS = Math.min(10000, REFRESH_MS || 10000);
  var FETCH_TIMEOUT_MS = 25000;
  var inFlight = false;
  var eqChart = null;

  function tickClockEt() {
    var el = document.getElementById("clockEt");
    if (!el) return;
    var parts = new Intl.DateTimeFormat("en-US", {
      timeZone: "America/New_York",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }).formatToParts(new Date());
    var H = "", M = "", S = "";
    parts.forEach(function (p) {
      if (p.type === "hour") H = p.value;
      if (p.type === "minute") M = p.value;
      if (p.type === "second") S = p.value;
    });
    el.textContent = H + ":" + M + ":" + S + " ET";
  }
  setInterval(tickClockEt, 1000);
  tickClockEt();

  function bindTabs() {
    var ALLOWED = { overview: 1, positions: 1, backtest: 1, system: 1 };
    function showTab(name) {
      if (name === "dashboard") name = "overview";
      var tab = ALLOWED[name] ? name : "overview";
      document.querySelectorAll(".tab-btn").forEach(function (btn) {
        btn.classList.toggle("active", btn.dataset.tab === tab);
      });
      document.querySelectorAll(".tab-panel").forEach(function (panel) {
        panel.classList.toggle("active", panel.id === tab + "-tab");
      });
      try {
        localStorage.setItem(ACTIVE_TAB_KEY, tab);
      } catch (e) {}
      if (tab === "backtest" && typeof window.quantbotLoadBacktestRuns === "function") {
        window.quantbotLoadBacktestRuns();
      }
    }
    document.querySelectorAll(".tab-btn[data-tab]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        showTab(btn.dataset.tab);
      });
    });
    document.querySelectorAll("[data-tab-link]").forEach(function (a) {
      a.addEventListener("click", function (ev) {
        ev.preventDefault();
        showTab(a.dataset.tabLink);
      });
    });
    var fallback = "overview";
    try {
      var key = localStorage.getItem(ACTIVE_TAB_KEY);
      if (key === "dashboard") key = "overview";
      showTab(ALLOWED[key] ? key : fallback);
    } catch (e) {
      showTab(fallback);
    }
  }

  function bindCfg() {
    document.querySelectorAll(".cfg-range").forEach(function (r) {
      r.oninput = function () {
        var k = r.dataset.key;
        var n = document.querySelector('.cfg-num[data-key="' + k + '"]');
        if (n) n.value = r.value;
      };
    });
    document.querySelectorAll(".cfg-num").forEach(function (n) {
      n.oninput = function () {
        var k = n.dataset.key;
        var r = document.querySelector('.cfg-range[data-key="' + k + '"]');
        if (r) r.value = n.value;
      };
    });
    document.querySelectorAll("#system-tab .cfg-save, .cfg-save").forEach(function (btn) {
      btn.onclick = async function () {
        var k = btn.dataset.key;
        var n = document.querySelector('.cfg-num[data-key="' + k + '"]');
        var v = parseFloat(n && n.value);
        var res = await fetch("/api/config", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-Dashboard-Secret": DASHBOARD_SECRET },
          body: JSON.stringify({ key: k, value: v }),
        });
        if (res.ok) {
          btn.textContent = "Saved";
          setTimeout(function () { btn.textContent = "Save"; }, 1200);
        }
      };
    });
    var rst = document.getElementById("cfg-reset");
    if (rst) {
      rst.onclick = async function () {
        if (!confirm("Reset all bot parameters to defaults?")) return;
        await fetch("/api/config/reset", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-Dashboard-Secret": DASHBOARD_SECRET },
        });
        location.reload();
      };
    }
    var dr = document.getElementById("cfg-dynamic-risk");
    if (dr) {
      dr.onchange = async function () {
        var v = dr.checked ? 1 : 0;
        var res = await fetch("/api/config", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-Dashboard-Secret": DASHBOARD_SECRET },
          body: JSON.stringify({ key: "dynamic_risk_enabled", value: v }),
        });
        if (!res.ok) dr.checked = !dr.checked;
      };
    }
  }

  function mapUiToApiPeriod(ui) {
    var m = { "1D": "1D", "5D": "1W", "1W": "1W", "1M": "1M", "ALL": "3M" };
    return m[ui] || "1D";
  }
  function getStoredUiRange() {
    try {
      var v = localStorage.getItem(EQUITY_RANGE_KEY) || "1D";
      return ["1D", "5D", "1W", "1M", "ALL"].indexOf(v) >= 0 ? v : "1D";
    } catch (e) {
      return "1D";
    }
  }
  function highlightRangeButtons() {
    var cur = getStoredUiRange();
    document.querySelectorAll(".range-btn").forEach(function (btn) {
      btn.classList.toggle("active", btn.getAttribute("data-range") === cur);
    });
  }

  function byId(id) { return document.getElementById(id); }
  function esc(v) {
    return String(v == null ? "" : v)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }
  function text(id, v) { var n = byId(id); if (n) n.textContent = v == null ? "" : String(v); }
  function html(id, v) { var n = byId(id); if (n) n.innerHTML = v == null ? "" : String(v); }
  function toNum(v) { var n = Number(v); return Number.isFinite(n) ? n : null; }
  function moneyOrNA(v) { var n = toNum(v); return n == null ? "N/A" : ("$" + n.toFixed(2)); }
  function countOrNA(v) { var n = toNum(v); return n == null ? "N/A" : String(Math.trunc(n)); }
  function pick() {
    for (var i = 0; i < arguments.length; i += 1) {
      var v = arguments[i];
      if (v !== undefined && v !== null && v !== "") return v;
    }
    return null;
  }
  function safeRows(rows) {
    return Array.isArray(rows) ? rows.filter(function (r) { return r && typeof r === "object"; }) : [];
  }
  function readEmbedded() {
    try {
      var n = byId("dash-payload");
      var raw = n ? String(n.textContent || "").trim() : "";
      return raw ? JSON.parse(raw) : null;
    } catch (e) {
      return null;
    }
  }

  function adapt(raw) {
    var p = raw && typeof raw === "object" ? raw : {};
    var pf = p.portfolio && typeof p.portfolio === "object" ? p.portfolio : {};
    var eh = p.execution_health && typeof p.execution_health === "object" ? p.execution_health : {};
    var bg = p.buy_gate && typeof p.buy_gate === "object" ? p.buy_gate : {};
    var pfCash = pick(pf.cash);
    var cs = toNum(pf.cash_stocks);
    var cc = toNum(pf.cash_crypto);
    var cashCombined =
      pfCash != null ? pfCash : cs != null || cc != null ? (cs || 0) + (cc || 0) : pick(eh.cash, bg.cash);
    var bpCombined = pick(
      pf.buying_power,
      pf.buying_power_stock,
      eh.buying_power,
      eh.usable_buying_power,
      bg.buying_power,
      bg.usable_buying_power
    );
    var positions = safeRows(p.open_positions);
    var trades = safeRows(p.recent_trades);
    var signals = safeRows(p.recent_signals);
    var decisions = signals.length ? signals : safeRows(p.execution_decisions);
    var exits = safeRows(p.position_exit_rows);
    if (!exits.length && Array.isArray(eh.position_exit_rows)) exits = safeRows(eh.position_exit_rows);
    return {
      raw: p,
      positions: positions,
      trades: trades,
      decisions: decisions,
      equity: safeRows(p.equity_series),
      exits: exits,
      eh: eh,
      account: {
        equity: pick(pf.equity_total, pf.equity),
        pnlPct: pick(p.pnl_vs_start_pct),
        pnlDol: pick(p.pnl_vs_start_dollars),
        cash: cashCombined,
        bp: bpCombined,
      },
      mode: String(p.mode || "paper"),
      marketOpen: typeof p.market_open === "boolean" ? p.market_open : null,
    };
  }

  function renderTable(bodyId, emptyId, rows, rowToHtml) {
    var body = byId(bodyId);
    if (!body) return;
    if (!rows.length) {
      body.innerHTML = "";
      var empty = byId(emptyId);
      if (empty) empty.style.display = "block";
      return;
    }
    var emptyEl = byId(emptyId);
    if (emptyEl) emptyEl.style.display = "none";
    body.innerHTML = rows.map(rowToHtml).join("");
  }

  function renderEquity(series) {
    var canvas = byId("eqChart");
    var empty = byId("eqEmpty");
    if (!canvas) return;
    if (!series.length) {
      if (empty) empty.style.display = "block";
      return;
    }
    if (empty) empty.style.display = "none";
    if (typeof Chart === "undefined") return;
    var labels = series.map(function (r) { return String(r.snapshot_at || ""); });
    var values = series.map(function (r) { var n = toNum(r.equity_total); return n == null ? 0 : n; });
    if (!eqChart) {
      eqChart = new Chart(canvas.getContext("2d"), {
        type: "line",
        data: { labels: labels, datasets: [{ data: values, borderColor: "#00ff88", backgroundColor: "rgba(0,255,136,0.08)", fill: true, tension: 0.25, pointRadius: 0 }] },
        options: { animation: false, responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { x: { ticks: { maxTicksLimit: 6 } }, y: { ticks: { maxTicksLimit: 5 } } } }
      });
      return;
    }
    eqChart.data.labels = labels;
    eqChart.data.datasets[0].data = values;
    eqChart.update("none");
  }

  function render(payload) {
    try {
      renderInner(payload);
    } catch (e) {
      text("statusApi", "API: render error");
      text("dbgApiStatus", "render threw");
      var err = byId("dash-api-error");
      if (err) {
        err.style.display = "block";
        err.textContent = "Dashboard render failed: " + (e && e.message ? String(e.message) : String(e));
      }
    }
  }

  function renderInner(payload) {
    var d = adapt(payload);
    var now = new Date().toLocaleTimeString();
    var pnlDol = toNum(d.account.pnlDol);
    var pnlPct = toNum(d.account.pnlPct);
    var pnlText = "N/A";
    if (pnlDol != null && pnlPct != null) {
      var left = (pnlDol >= 0 ? "+$" : "-$") + Math.abs(pnlDol).toFixed(2);
      var right = (pnlPct >= 0 ? "+" : "") + pnlPct.toFixed(2) + "%";
      pnlText = left + " / " + right;
    }
    var pnlEl = byId("tilePnl");
    if (pnlEl) {
      pnlEl.textContent = pnlText;
      if (pnlDol != null && pnlPct != null) {
        pnlEl.className = "big " + (pnlPct >= 0 ? "pos" : "neg");
      } else {
        pnlEl.className = "big";
      }
    }
    text("tileEq", moneyOrNA(d.account.equity));
    text("tileCash", moneyOrNA(d.account.cash));
    text("tileBp", moneyOrNA(d.account.bp));
    text("statusApi", "API: connected");
    text("statusMode", "Mode: " + d.mode);
    text("statusLive", "Live trading: disabled");
    text("statusUpdated", "Last updated: " + now);
    text("last-sync", "Live via polling · " + now);
    var ls = byId("last-sync");
    if (ls) {
      ls.classList.remove("sync-reconnect");
      ls.classList.add("sync-live");
    }
    window.__quantbotLastDashOkMs = Date.now();
    text("systemApiStatus", "connected");
    text("systemWorkerStatus", d.raw.worker_status != null ? String(d.raw.worker_status) : "N/A");
    text("systemDbPath", d.raw.db_path != null ? String(d.raw.db_path) : "N/A");
    text("dbgApiStatus", "200/ok");
    text("dbgPosLen", String(d.positions.length));
    text("dbgSigLen", String(d.decisions.length));
    text("dbgTradeLen", String(d.trades.length));
    text("dbgEqLen", String(d.equity.length));
    text("dbgEhPresent", String(!!Object.keys(d.eh).length));
    text("dbgExitLen", String(d.exits.length));

    var mkt = byId("mktLine");
    if (mkt) {
      var lbl = d.marketOpen === true ? "OPEN" : d.marketOpen === false ? "CLOSED" : "N/A";
      mkt.textContent = lbl;
      mkt.className = d.marketOpen === true ? "market-open" : "market-closed";
    }

    renderEquity(d.equity);

    renderTable("posTableBody", "posEmpty", d.positions.slice(0, 5), function (r) {
      var up = toNum(r.unrealized_pnl_pct);
      var upTxt = up == null ? "—" : ((up >= 0 ? "+" : "") + up.toFixed(2) + "%");
      return "<tr><td>" + esc(r.symbol || "") + "</td><td>" + esc(String(toNum(r.net_qty) == null ? 0 : toNum(r.net_qty).toFixed(4))) + "</td><td>" + moneyOrNA(r.avg_entry_price) + "</td><td>" + moneyOrNA(r.current_price) + "</td><td>" + esc(upTxt) + "</td><td>Holding</td></tr>";
    });

    renderTable("sigFeedBody", "sigFeedEmpty", d.decisions.slice(0, 10), function (r) {
      var meta = r.meta && typeof r.meta === "object" ? r.meta : {};
      var reason = meta.reason != null ? String(meta.reason) : String(r.signal_name || r.reason_code || r.reason || "—");
      var action = meta.action != null ? String(meta.action) : String(r.side || "HOLD");
      var score = r.combined_score != null ? String(r.combined_score) : "";
      var reasonCol = reason + (score !== "" ? " · score " + score : "");
      return "<tr><td>" + esc(r.created_at || r.time || "—") + "</td><td>" + esc(r.symbol || "—") + "</td><td>" + esc(action) + "</td><td>" + esc(reasonCol) + "</td></tr>";
    });

    renderTable("posDetailedBody", "posDetailedEmpty", d.positions, function (r) {
      var up = toNum(r.unrealized_pnl);
      var upp = toNum(r.unrealized_pnl_pct);
      var upTxt = up == null ? "—" : ((up >= 0 ? "+$" : "-$") + Math.abs(up).toFixed(2));
      var uppTxt = upp == null ? "—" : ((upp >= 0 ? "+" : "") + upp.toFixed(2) + "%");
      return "<tr><td class=\"mono\">" + esc(r.symbol || "") + "</td><td>" + esc(r.asset_class || "") + "</td><td class=\"mono\">" + esc(String(toNum(r.net_qty) == null ? 0 : toNum(r.net_qty).toFixed(4))) + "</td><td class=\"mono\">—</td><td class=\"mono\">" + moneyOrNA(r.avg_entry_price) + "</td><td class=\"mono\">" + moneyOrNA(r.current_price) + "</td><td class=\"mono\">" + esc(upTxt) + "</td><td class=\"mono\">" + esc(uppTxt) + "</td><td>Holding</td><td class=\"muted\">No exit signal</td></tr>";
    });

    renderTable("execExitTableBody", "execExitEmpty", d.exits, function (r) {
      function cell(v) { return esc(v == null || v === "" ? "—" : String(v)); }
      return "<tr><td class=\"mono\">" + cell(r.symbol) + "</td><td>" + cell(r.asset_class) + "</td><td class=\"mono\">" + cell(r.local_qty) + "</td><td class=\"mono\">" + cell(r.broker_qty) + "</td><td class=\"mono\">" + cell(r.entry_price) + "</td><td class=\"mono\">" + cell(r.current_price) + "</td><td class=\"mono\">" + cell(r.pnl_pct) + "</td><td>" + cell(r.exit_eligibility) + "</td><td class=\"muted\">" + cell(r.exit_block_reason) + "</td><td>" + cell(r.pdt_status) + "</td><td class=\"mono\" style=\"font-size:0.68rem;\">" + cell(r.last_exit_attempt_at) + "</td><td class=\"mono\">" + cell(r.cooldown_remaining) + "</td><td>" + cell(r.recommended_action) + "</td></tr>";
    });

    text("execHealthCash", moneyOrNA(d.eh.cash));
    text("execHealthBuyingPower", moneyOrNA(d.eh.buying_power));
    text("execHealthUsable", moneyOrNA(d.eh.usable_buying_power));
    text("execHealthBlockedExits", countOrNA(d.eh.blocked_exits_count));
    text("execHealthStaleLocal", countOrNA(d.eh.stale_local_positions_count));
    text("execHealthMismatches", countOrNA(d.eh.broker_local_mismatch_count));
    text("execHealthCryptoFast", d.eh.crypto_fast_exit_enabled === true ? "on" : d.eh.crypto_fast_exit_enabled === false ? "off" : "N/A");
    text("execHealthPdtGuard", d.eh.stock_pdt_guard_enabled === true ? "on" : d.eh.stock_pdt_guard_enabled === false ? "off" : "N/A");
    text("execHealthExitEligible", countOrNA(d.eh.exit_eligible_positions_count));
    text("execHealthLastReconcile", d.eh.last_reconciliation_at != null ? String(d.eh.last_reconciliation_at) : "N/A");
    var pdtWrap = byId("execHealthPdtBadges");
    if (pdtWrap) {
      var syms = Array.isArray(d.eh.pdt_blocked_symbols) ? d.eh.pdt_blocked_symbols : [];
      pdtWrap.innerHTML = syms.length ? syms.map(function (s) { return '<span class="exec-health-badge">' + esc(s) + "</span>"; }).join("") : '<span class="muted exec-health-empty">—</span>';
    }
    var missing = byId("execHealthMissing");
    if (missing) missing.style.display = Object.keys(d.eh).length ? "none" : "block";

    var warns = [];
    var sec = d.raw.section_status && typeof d.raw.section_status === "object" ? d.raw.section_status : {};
    Object.keys(sec).forEach(function (k) { if (sec[k] && sec[k] !== "ok") warns.push("Section " + k + ": " + String(sec[k])); });
    if (d.raw.degraded === true) warns.push("Dashboard payload is degraded.");
    var warnWrap = byId("overviewWarnings");
    var warnList = byId("overviewWarningsList");
    if (warnWrap && warnList) {
      warnWrap.style.display = warns.length ? "" : "none";
      warnList.innerHTML = warns.map(function (w) { return "<li>" + esc(w) + "</li>"; }).join("");
    }
    html("overviewWarnInline", warns.length ? warns.map(function (w) { return "• " + esc(w); }).join("<br>") : "No active warnings.");

    var err = byId("dash-api-error");
    if (err) { err.style.display = "none"; err.textContent = ""; }
    var sb = byId("statusBanner");
    if (sb) {
      sb.style.borderColor = "#1f2937";
      sb.style.background = "rgba(16,24,40,0.55)";
    }
  }

  async function parseDashboardResponse(res) {
    var txt = await res.text();
    if (!txt || !String(txt).trim()) return {};
    try {
      return JSON.parse(txt);
    } catch (e) {
      var head = String(txt).replace(/\s+/g, " ").slice(0, 160);
      throw new Error("Response was not JSON (got " + head + ")");
    }
  }

  async function pollDashboard() {
    if (inFlight) return;
    inFlight = true;
    var ac = new AbortController();
    var tid = setTimeout(function () { ac.abort(); }, FETCH_TIMEOUT_MS);
    try {
      var period = mapUiToApiPeriod(getStoredUiRange());
      var url = "/api/dashboard?equity_period=" + encodeURIComponent(period);
      var res = await fetch(url, { cache: "no-store", signal: ac.signal });
      if (!res.ok) throw new Error("/api/dashboard HTTP " + res.status);
      var payload = await parseDashboardResponse(res);
      render(payload);
    } catch (e) {
      text("statusApi", "API: error");
      text("systemApiStatus", "failed");
      text("dbgApiStatus", "failed");
      var err = byId("dash-api-error");
      if (err) {
        err.style.display = "block";
        err.textContent = "Dashboard API failed: " + (e && e.message ? String(e.message) : String(e));
      }
    } finally {
      clearTimeout(tid);
      inFlight = false;
    }
  }

  async function pollSocial() {
    var root = byId("socialMoRoot");
    if (!root) return;
    try {
      var res = await fetch("/api/social", { cache: "no-store" });
      if (!res.ok) throw new Error("social HTTP " + res.status);
      var payload = await res.json();
      var rows = Array.isArray(payload) ? payload : (payload.rows || payload.data || []);
      if (!rows.length) { root.innerHTML = '<p class="muted">No social rows yet.</p>'; return; }
      root.innerHTML = '<table class="social-table"><thead><tr><th>Ticker</th><th>Mentions</th><th>Rank Δ</th><th>%Δ Mentions</th><th>Source</th></tr></thead><tbody>' +
        rows.slice(0, 10).map(function (r) {
          return "<tr><td>" + esc(r.ticker || r.symbol || "") + "</td><td>" + esc(r.mentions || "") + "</td><td>" + esc(r.rank_delta || r.rank_change || "") + "</td><td>" + esc(r.mentions_delta_pct || r.percent_delta || "") + "</td><td>" + esc(r.source || "") + "</td></tr>";
        }).join("") + "</tbody></table>";
    } catch (e) {
      root.innerHTML = '<p class="muted">Social feed unavailable.</p>';
    }
  }

  function bindEquityRangeButtons() {
    document.querySelectorAll(".range-btn").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var r = btn.getAttribute("data-range") || "1D";
        try { localStorage.setItem(EQUITY_RANGE_KEY, r); } catch (e) {}
        highlightRangeButtons();
        pollDashboard();
      });
    });
    highlightRangeButtons();
  }

  window.__quantbotRefreshDashboard = pollDashboard;

  function bootCockpit() {
    if (window.__quantbotCockpitBooted) return;
    window.__quantbotCockpitBooted = true;
    bindTabs();
    bindCfg();
    bindEquityRangeButtons();
    var embedded = readEmbedded();
    if (embedded) render(embedded);
    pollDashboard();
    pollSocial();
    setInterval(pollDashboard, POLL_MS);
    setInterval(pollSocial, 60000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bootCockpit);
  } else {
    bootCockpit();
  }
})();
</script>
'''.lstrip(
        "\n"
    )


def main() -> None:
    lines = DASH.read_text(encoding="utf-8").splitlines(keepends=True)
    prefix = lines[:937]
    suffix = lines[2856:]
    if "</body>" not in suffix[0]:
        raise SystemExit("unexpected suffix start")

    bt_head = "".join(lines[1931:1939])
    bt_tail = "".join(lines[1962:2493])
    backtest_inner = bt_head + bt_tail

    bt_wrap = (
        "<script>\n"
        "(function () {\n"
        '  "use strict";\n'
        "  const ACTIVE_TAB_KEY = \"quantbot_active_tab\";\n"
        "  const DASHBOARD_SECRET = {{ dashboard_secret|tojson }};\n"
        "  function esc(s) {\n"
        "    return String(s ?? \"\").replace(/&/g,\"&amp;\").replace(/</g,\"&lt;\").replace(/>/g,\"&gt;\").replace(/\"/g,\"&quot;\");\n"
        "  }\n"
        + backtest_inner
        + "})();\n"
        + "</script>\n"
    )

    out = "".join(prefix) + COCKPIT + "\n" + bt_wrap + "".join(suffix)
    DASH.write_text(out, encoding="utf-8")
    print("OK:", DASH)


if __name__ == "__main__":
    main()
