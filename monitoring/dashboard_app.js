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
    var ehIn = p.execution_health && typeof p.execution_health === "object" ? p.execution_health : {};
    var cs = numOr(pf.cash_stocks, 0);
    var cc = numOr(pf.cash_crypto, 0);
    var eqN = Number(pf.equity_total);
    var eqOk = pf.equity_total != null && eqN === eqN && Number.isFinite(eqN);
    var pe = Array.isArray(p.position_exit_rows) ? p.position_exit_rows : [];
    if (!pe.length && Array.isArray(ehIn.position_exit_rows)) {
      pe = ehIn.position_exit_rows;
    }
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
      sectionStatus: p.section_status && typeof p.section_status === "object" ? p.section_status : {},
      executionHealth: ehIn,
      positionExitRows: pe,
      payloadDegraded: p.degraded === true,
      ghostPositionCount: num(p.ghost_position_count, null),
      dbLockCount24h: num(p.db_lock_count_24h, null),
      alpacaCacheAgeSeconds: p.alpaca_cache_age_seconds != null ? Number(p.alpaca_cache_age_seconds) : null,
      alpacaCacheLastError: p.alpaca_cache_last_error != null ? String(p.alpaca_cache_last_error) : ""
    };
  }

  function renderExitRows(vm) {
    var rows = vm.positionExitRows || [];
    var cnt = document.getElementById("exitRowsCount");
    var empty = document.getElementById("exitRowsEmpty");
    var tbl = document.querySelector("#tblExitRows tbody");
    var wrap = document.getElementById("exitRowsWrap");
    if (!cnt || !tbl || !wrap) return;
    cnt.textContent = String(rows.length);
    if (empty) empty.style.display = rows.length ? "none" : "block";
    tbl.innerHTML = rows.map(function (r) {
      var rec = r.recommended_action != null ? String(r.recommended_action) : String(r.exit_eligibility || "");
      return "<tr><td>" + esc(r.symbol) + "</td><td>" + esc(r.asset_class || "") + "</td><td class=\"mono\">" + esc(String(r.local_qty != null ? r.local_qty : "")) + "</td><td class=\"mono\">" + esc(String(r.broker_qty != null ? r.broker_qty : "")) + "</td><td>" + esc(rec) + "</td><td>" + esc(String(r.exit_block_reason || "")) + "</td><td>" + esc(String(r.pdt_status || "")) + "</td><td class=\"mono\">" + esc(String(r.cooldown_remaining || "")) + "</td><td class=\"mono\">" + esc(String(r.pnl_pct || "")) + "</td></tr>";
    }).join("");
    try {
      wrap.open = rows.length <= 12;
    } catch (e) {}
  }

  function renderExecutionHealth(vm) {
    var eh = vm.executionHealth || {};
    var blocked = numOr(eh.blocked_exits_count, 0);
    var stale = numOr(eh.stale_local_positions_count, 0);
    var mismatch = numOr(eh.broker_local_mismatch_count, 0);
    var degraded = vm.payloadDegraded === true;
    var warnAny = blocked > 0 || stale > 0 || mismatch > 0 || degraded;
    var badAny = stale > 2 || mismatch > 2 || blocked > 5;

    var banner = document.getElementById("execHealthBanner");
    var sev = document.getElementById("execHealthSeverity");
    if (banner && sev) {
      if (warnAny) {
        banner.style.display = "block";
        var parts = [];
        if (blocked > 0) parts.push(blocked + " blocked exit(s)");
        if (stale > 0) parts.push(stale + " stale local row(s)");
        if (mismatch > 0) parts.push(mismatch + " broker/local mismatch(es)");
        if (degraded) parts.push("dashboard payload degraded");
        banner.textContent = "Attention: " + parts.join(" · ") + ".";
        banner.className = badAny ? "eh-banner bad" : "eh-banner warn";
        sev.style.display = "inline-block";
        sev.textContent = badAny ? "ALERT" : "WARN";
        sev.className = badAny ? "eh-severity warn" : "eh-severity warn";
      } else {
        banner.style.display = "none";
        sev.style.display = "inline-block";
        sev.textContent = "OK";
        sev.className = "eh-severity ok";
      }
    }

    var grid = document.getElementById("execHealthGrid");
    if (grid) {
      function tcls(n, isWarn) {
        if (isWarn) return "warn";
        return "";
      }
      function tile(cls, lab, val) {
        return '<div class="eh-tile ' + cls + '"><div class="eh-lab">' + esc(lab) + '</div><div class="eh-val mono">' + esc(val) + '</div></div>';
      }
      var cash = eh.cash != null ? fmtMoney(eh.cash) : "—";
      var bp = eh.buying_power != null ? fmtMoney(eh.buying_power) : "—";
      var ubp = eh.usable_buying_power != null ? fmtMoney(eh.usable_buying_power) : "—";
      var ghost = vm.ghostPositionCount != null ? String(vm.ghostPositionCount) : "—";
      var dblk = vm.dbLockCount24h != null ? String(vm.dbLockCount24h) : "—";
      var cacheAge = Number.isFinite(Number(vm.alpacaCacheAgeSeconds)) ? String(vm.alpacaCacheAgeSeconds) + " s" : "—";
      var ehAt = eh.created_at != null ? String(eh.created_at) : "—";
      var html = "";
      html += tile(tcls(blocked, blocked > 0), "Blocked exits", String(blocked));
      html += tile(tcls(stale, stale > 0), "Stale local rows", String(stale));
      html += tile(tcls(mismatch, mismatch > 0), "Broker/local mismatch", String(mismatch));
      html += tile("", "Broker cash", cash);
      html += tile("", "Buying power", bp);
      html += tile("", "Usable BP (buys)", ubp);
      html += tile("", "Ghost positions", ghost);
      html += tile("", "DB lock events (24h)", dblk);
      html += tile("", "Alpaca cache age", cacheAge);
      html += tile("", "EH snapshot at", ehAt);
      grid.innerHTML = html;
    }

    var cacheErr = vm.alpacaCacheLastError ? String(vm.alpacaCacheLastError).trim() : "";
    var helper = document.getElementById("execHealthHelper");
    if (helper) {
      var base = helper.getAttribute("data-base-help");
      if (!base) {
        base = helper.innerHTML;
        helper.setAttribute("data-base-help", base);
      }
      var extra = "";
      if (cacheErr) {
        extra = ' <span style="color:#f87171;">Alpaca cache error: ' + esc(cacheErr) + "</span>";
      }
      helper.innerHTML = base + extra;
    }

    var pdtWrap = document.getElementById("pdtBadgeRowWrap");
    var pdtRow = document.getElementById("pdtBadgeRow");
    var syms = Array.isArray(eh.pdt_blocked_symbols) ? eh.pdt_blocked_symbols : [];
    if (pdtWrap && pdtRow) {
      if (syms.length) {
        pdtWrap.style.display = "flex";
        pdtRow.innerHTML = syms.map(function (s) {
          return '<span class="badge mono">' + esc(String(s)) + "</span>";
        }).join("");
      } else {
        pdtWrap.style.display = "none";
        pdtRow.innerHTML = "";
      }
    }

    renderExitRows(vm);
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

    renderExecutionHealth(vm);
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
