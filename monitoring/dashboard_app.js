(function () {
  "use strict";

  var bootDbg = document.getElementById("boot-debug");
  if (bootDbg) bootDbg.textContent = "APP JS STARTED";
  var bootMirror = document.getElementById("bootDebugMirror");
  if (bootMirror) bootMirror.textContent = "APP JS STARTED";

  var _dh = document.getElementById("dash-secret-holder");
  var DASHBOARD_SECRET = _dh ? _dh.value : "";
  var equityChart = null;
  var POLL_MS = 30000;
  var MIN_ORDER_NOTIONAL = 1.0;

  // ---------------------------------------------------------------------------
  // Formatting helpers
  // ---------------------------------------------------------------------------

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/\x3c/g, "&lt;")
      .replace(/\x3e/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function isFiniteNum(v) {
    var n = Number(v);
    return n === n && Number.isFinite(n);
  }

  function num(v, fallback) {
    return isFiniteNum(v) ? Number(v) : fallback;
  }
  function numOr(v, fallback) {
    return isFiniteNum(v) ? Number(v) : fallback;
  }

  // $114.04 (unsigned, plain dollar)
  function fmtMoney(v) {
    if (!isFiniteNum(v)) return "—";
    var n = Number(v);
    if (n < 0) return "-$" + Math.abs(n).toFixed(2);
    return "$" + n.toFixed(2);
  }

  // +$14.04 / -$0.74 / $0.00 (signed dollar)
  function fmtMoneySigned(v) {
    if (!isFiniteNum(v)) return "—";
    var n = Number(v);
    if (n > 0) return "+$" + n.toFixed(2);
    if (n < 0) return "-$" + Math.abs(n).toFixed(2);
    return "$0.00";
  }

  // 14.04% (unsigned)
  function fmtPct(v) {
    if (!isFiniteNum(v)) return "—";
    return Number(v).toFixed(2) + "%";
  }

  // +14.04% / -3.14% / 0.00%
  function fmtPctSigned(v) {
    if (!isFiniteNum(v)) return "—";
    var n = Number(v);
    if (n > 0) return "+" + n.toFixed(2) + "%";
    if (n < 0) return n.toFixed(2) + "%"; // Number.toFixed keeps the leading "-"
    return "0.00%";
  }

  // Price: $1+ → 2 decimals, $<1 → 4 decimals, with "$" prefix
  function fmtPrice(v) {
    if (!isFiniteNum(v)) return "—";
    var n = Math.abs(Number(v));
    var sign = Number(v) < 0 ? "-" : "";
    return sign + "$" + (n >= 1 ? n.toFixed(2) : n.toFixed(4));
  }

  // Quantity: stocks up to 4 decimals, crypto up to 8; trims trailing zeros.
  function fmtQty(v, isCrypto) {
    if (!isFiniteNum(v)) return "—";
    var max = isCrypto ? 8 : 4;
    var s = Number(v).toFixed(max);
    // Trim trailing zeros after the decimal (but keep at least one digit if integer)
    if (s.indexOf(".") !== -1) {
      s = s.replace(/0+$/, "").replace(/\.$/, "");
    }
    return s;
  }

  function pad2(n) { return (n < 10 ? "0" : "") + n; }

  // "12:34:56" — short local time stamp for header "Updated …"
  function fmtTimeShort(d) {
    if (!(d instanceof Date) || isNaN(d.getTime())) return "—";
    return pad2(d.getHours()) + ":" + pad2(d.getMinutes()) + ":" + pad2(d.getSeconds());
  }

  // Try to parse ISO-ish strings into a readable short local form, else return original.
  function fmtTimestamp(s) {
    if (s == null || s === "") return "—";
    var raw = String(s);
    var iso = raw.length === 19 ? raw + "Z" : raw;
    var d = new Date(iso);
    if (isNaN(d.getTime())) return raw;
    var now = new Date();
    var sameDay =
      d.getFullYear() === now.getFullYear() &&
      d.getMonth() === now.getMonth() &&
      d.getDate() === now.getDate();
    if (sameDay) {
      return pad2(d.getHours()) + ":" + pad2(d.getMinutes()) + ":" + pad2(d.getSeconds());
    }
    return (d.getMonth() + 1) + "/" + d.getDate() + " " + pad2(d.getHours()) + ":" + pad2(d.getMinutes());
  }

  function pnlClass(value) {
    if (value == null || !isFiniteNum(value)) return "";
    if (value > 0) return "pos-good";
    if (value < 0) return "pos-bad";
    return "";
  }

  // ---------------------------------------------------------------------------
  // Payload mapping
  // ---------------------------------------------------------------------------

  function mapDashboardPayload(payload) {
    var p = payload && typeof payload === "object" ? payload : {};
    var pf = p.portfolio && typeof p.portfolio === "object" ? p.portfolio : {};
    var ehIn = p.execution_health && typeof p.execution_health === "object" ? p.execution_health : {};
    var safety = p.live_safety && typeof p.live_safety === "object" ? p.live_safety : {};
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
      alpacaCacheLastError: p.alpaca_cache_last_error != null ? String(p.alpaca_cache_last_error) : "",
      liveSafetyEnabled: safety && safety.live_enabled === true,
      buyGate: p.buy_gate && typeof p.buy_gate === "object" ? p.buy_gate : {}
    };
  }

  // ---------------------------------------------------------------------------
  // Header chips + error banner
  // ---------------------------------------------------------------------------

  function setChip(id, state, text) {
    var el = document.getElementById(id);
    if (!el) return;
    var cls = "chip";
    if (state === "ok") cls += " ok";
    else if (state === "warn") cls += " warn";
    else if (state === "bad") cls += " bad";
    else if (state === "info") cls += " info";
    el.className = cls;
    el.setAttribute("data-state", state);
    var txt = el.querySelector(".chip-text");
    if (txt) txt.textContent = text;
  }

  function setApiChip(state, text) {
    setChip("chipApi", state, text);
  }

  function setError(msg) {
    var errEl = document.getElementById("dashError");
    if (!errEl) return;
    if (msg) {
      errEl.style.display = "block";
      errEl.textContent = "Dashboard error: " + msg;
    } else {
      errEl.style.display = "none";
      errEl.textContent = "";
    }
  }

  function applyHealthyChips(vm) {
    var modeText = vm.mode ? String(vm.mode).toLowerCase() : "—";
    var modeLabel = modeText === "paper" ? "Paper mode" : modeText === "live" ? "Live mode" : "Mode " + modeText;
    setChip("chipMode", modeText === "paper" ? "info" : (modeText === "live" ? "warn" : "info"), modeLabel);
    if (vm.liveSafetyEnabled === true) {
      setChip("chipLive", "warn", "Live enabled");
    } else if (vm.liveSafetyEnabled === false) {
      setChip("chipLive", "ok", "Live disabled");
    } else {
      setChip("chipLive", "info", "Live —");
    }
    setApiChip("ok", "API connected");

    var when = "Updated " + fmtTimeShort(new Date());
    var stamp = document.getElementById("dashUpdatedAt");
    if (stamp) stamp.textContent = when;
  }

  // ---------------------------------------------------------------------------
  // Operator summary (plain English sentences)
  // ---------------------------------------------------------------------------

  function setOpsLine(id, html, state) {
    var li = document.getElementById(id);
    if (!li) return;
    li.innerHTML = html;
    li.className = state ? state : "";
  }

  function renderOperatorSummary(vm) {
    var modeText = vm.mode ? String(vm.mode).toUpperCase() : "—";
    setOpsLine(
      "opsLineMode",
      "Account is in <span class=\"accent\">" + esc(modeText) + "</span> mode.",
      modeText === "PAPER" ? "ok" : (modeText === "LIVE" ? "warn" : "")
    );

    if (vm.liveSafetyEnabled === true) {
      setOpsLine("opsLineLive", "Live trading is <span class=\"warn-t\">enabled</span>.", "warn");
    } else {
      setOpsLine("opsLineLive", "Live trading is <span class=\"good\">disabled</span>.", "ok");
    }

    if (vm.marketOpen === true) {
      setOpsLine("opsLineMarket", "Market is <span class=\"good\">OPEN</span>.", "ok");
    } else if (vm.marketOpen === false) {
      setOpsLine("opsLineMarket", "Market is <span class=\"warn-t\">CLOSED</span>.", "warn");
    } else {
      setOpsLine("opsLineMarket", "Market state: N/A.", "");
    }

    var cashStr = isFiniteNum(vm.cash) ? fmtMoney(vm.cash) : "N/A";
    var cashState = isFiniteNum(vm.cash) && vm.cash < MIN_ORDER_NOTIONAL ? "warn" : "";
    setOpsLine("opsLineCash", "Cash available: <span class=\"mono\">" + esc(cashStr) + "</span>.", cashState);

    var posCount = (vm.positions || []).length;
    setOpsLine("opsLinePositions", "Open positions: <span class=\"mono\">" + posCount + "</span>.", "");

    var ubp = num((vm.executionHealth || {}).usable_buying_power, null);
    if (ubp != null && ubp < MIN_ORDER_NOTIONAL) {
      setOpsLine(
        "opsLineBuys",
        "New buys <span class=\"warn-t\">blocked</span>: usable cash <span class=\"mono\">" + esc(fmtMoney(ubp)) + "</span> below minimum order size.",
        "warn"
      );
    } else if (ubp != null) {
      setOpsLine(
        "opsLineBuys",
        "New buys eligible — usable cash <span class=\"mono\">" + esc(fmtMoney(ubp)) + "</span>.",
        "ok"
      );
    } else if (isFiniteNum(vm.cash) && vm.cash < MIN_ORDER_NOTIONAL) {
      setOpsLine("opsLineBuys", "New buys <span class=\"warn-t\">blocked</span>: insufficient cash.", "warn");
    } else {
      setOpsLine("opsLineBuys", "New buys: status N/A.", "");
    }

    if (vm.marketOpen === false) {
      setOpsLine("opsLineStockExits", "Stock exits <span class=\"warn-t\">blocked</span> while market is closed.", "warn");
    } else if (vm.marketOpen === true) {
      setOpsLine("opsLineStockExits", "Stock exits <span class=\"good\">available</span> (regular session).", "ok");
    } else {
      setOpsLine("opsLineStockExits", "Stock exits: market state N/A.", "");
    }

    setOpsLine(
      "opsLineCryptoExits",
      "Crypto exits allowed <span class=\"good\">24/7</span> only if broker quantity exists.",
      "ok"
    );

    var sec = vm.sectionStatus || {};
    var perf = vm.performance || {};
    var cycleStr = null;
    var keys = ["last_cycle", "exec_path", "execution_path", "cycle"];
    for (var i = 0; i < keys.length; i++) {
      var s = sec[keys[i]];
      if (s && typeof s === "object" && (s.analyzed != null || s.holds != null)) {
        cycleStr = "analyzed " + (s.analyzed != null ? s.analyzed : "?") +
          ", buys " + (s.buys != null ? s.buys : "?") +
          ", sells " + (s.sells != null ? s.sells : "?") +
          ", holds " + (s.holds != null ? s.holds : "?") +
          ", errs " + (s.errors != null ? s.errors : (s.errs != null ? s.errs : "?"));
        break;
      }
    }
    if (!cycleStr && perf && perf.total_trades != null) {
      cycleStr = "total_trades=" + perf.total_trades + ", closed_round_trips=" + (perf.closed_round_trips || 0);
    }
    if (cycleStr) {
      setOpsLine("opsLineLastCycle", "Last cycle: <span class=\"mono\">" + esc(cycleStr) + "</span>.", "");
    } else {
      setOpsLine("opsLineLastCycle", "Last cycle: N/A.", "");
    }
  }

  // ---------------------------------------------------------------------------
  // Execution health
  // ---------------------------------------------------------------------------

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
      wrap.open = rows.length > 0 && rows.length <= 12;
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
      function tile(cls, lab, val) {
        return '<div class="eh-tile ' + cls + '"><div class="eh-lab">' + esc(lab) + '</div><div class="eh-val mono">' + esc(val) + '</div></div>';
      }
      var cash = eh.cash != null ? fmtMoney(eh.cash) : "—";
      var bp = eh.buying_power != null ? fmtMoney(eh.buying_power) : "—";
      var ubp = eh.usable_buying_power != null ? fmtMoney(eh.usable_buying_power) : "—";
      var pdtCount = Array.isArray(eh.pdt_blocked_symbols) ? eh.pdt_blocked_symbols.length : 0;
      var cacheAge = isFiniteNum(vm.alpacaCacheAgeSeconds) ? Number(vm.alpacaCacheAgeSeconds).toFixed(1) + " s" : "—";
      var lastSync = eh.created_at != null ? fmtTimestamp(eh.created_at) : "—";
      var dblk = vm.dbLockCount24h != null ? String(vm.dbLockCount24h) : "—";
      var html = "";
      html += tile("", "Broker cash", cash);
      html += tile("", "Buying power", bp);
      html += tile("", "Usable buy power", ubp);
      html += tile(blocked > 0 ? "warn" : "", "Blocked exits", String(blocked));
      html += tile(mismatch > 0 ? "warn" : "", "Broker/local mismatches", String(mismatch));
      html += tile(stale > 0 ? "warn" : "", "Stale local rows", String(stale));
      html += tile("", "DB lock waits/retries (24h)", dblk);
      html += tile("", "Alpaca cache age", cacheAge);
      html += tile("", "Last broker snapshot", lastSync);
      html += tile(pdtCount > 0 ? "warn" : "", "PDT guarded symbols", String(pdtCount));
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
        extra = ' <span style="color:#fbbf24;">Alpaca cache error: ' + esc(cacheErr) + "</span>";
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

  // ---------------------------------------------------------------------------
  // Exit state classification (Positions tab)
  // ---------------------------------------------------------------------------

  // Returns { status: "HOLD"|"CAN SELL"|"BLOCKED"|"WAITING"|"STALE", explanation, mismatchWarn }
  function exitStateFor(row, vm) {
    var ac = String(row.asset_class || "").toLowerCase();
    var localQ = Math.abs(num(row.net_qty, 0));
    var brokerRaw = row.broker_qty != null ? row.broker_qty : row.broker_quantity;
    var brokerQ = brokerRaw != null && isFiniteNum(brokerRaw) ? Math.abs(Number(brokerRaw)) : null;
    var mismatchWarn = false;
    if (brokerQ != null) {
      if (localQ > 0 && brokerQ > 0 && (localQ > brokerQ * 1.5 || brokerQ > localQ * 1.5)) {
        mismatchWarn = true;
      }
    }

    // STALE: local row exists but broker says zero — broker is authoritative.
    if (brokerQ != null && brokerQ <= 1e-9 && localQ > 0) {
      return {
        status: "STALE",
        explanation: "Broker qty zero; local row needs reconciliation.",
        mismatchWarn: false
      };
    }

    if (ac === "crypto") {
      if (brokerQ != null && brokerQ <= 1e-9) {
        return {
          status: "STALE",
          explanation: "Broker qty zero; local row needs reconciliation.",
          mismatchWarn: false
        };
      }
      return {
        status: "CAN SELL",
        explanation: "Crypto can trade 24/7, waiting for signal.",
        mismatchWarn: mismatchWarn
      };
    }

    if (ac === "stock") {
      if (vm.marketOpen === false) {
        return {
          status: "BLOCKED",
          explanation: "Stock exit blocked: market closed.",
          mismatchWarn: mismatchWarn
        };
      }
      if (mismatchWarn) {
        return {
          status: "WAITING",
          explanation: "Broker qty is authoritative.",
          mismatchWarn: mismatchWarn
        };
      }
      return {
        status: "HOLD",
        explanation: "Holding: no exit signal.",
        mismatchWarn: mismatchWarn
      };
    }

    return {
      status: "HOLD",
      explanation: "Holding: no exit signal.",
      mismatchWarn: mismatchWarn
    };
  }

  function statusClass(status) {
    if (status === "CAN SELL") return "can-sell";
    if (status === "BLOCKED") return "blocked";
    if (status === "WAITING") return "waiting";
    if (status === "STALE") return "stale";
    return "hold";
  }

  function exitBadge(status) {
    return '<span class="exit-status ' + statusClass(status) + '">' + esc(status) + "</span>";
  }

  // ---------------------------------------------------------------------------
  // Trade / decision status badges
  // ---------------------------------------------------------------------------

  function tradeStatusBadge(raw) {
    if (raw == null || raw === "") return "—";
    var s = String(raw);
    var lo = s.toLowerCase();
    var cls = "skipped";
    if (lo === "filled" || lo === "ok" || lo === "complete" || lo === "completed") cls = "filled";
    else if (lo === "rejected" || lo === "failed" || lo === "error" || lo === "canceled" || lo === "cancelled") cls = "rejected";
    else if (lo === "skipped" || lo === "ignored") cls = "skipped";
    else if (lo === "pending" || lo === "new" || lo === "accepted" || lo === "submitted") cls = "pending";
    return '<span class="status-badge ' + cls + '">' + esc(s) + "</span>";
  }

  function decisionBadge(decision) {
    if (decision == null || decision === "") return "—";
    var s = String(decision);
    var lo = s.toLowerCase();
    var cls = "skipped";
    if (lo === "executed" || lo === "filled" || lo === "ok") cls = "filled";
    else if (lo === "rejected" || lo === "blocked" || lo === "failed" || lo === "error") cls = "rejected";
    else if (lo === "skipped" || lo === "noop" || lo === "ignored" || lo === "deferred") cls = "skipped";
    else if (lo === "pending" || lo === "queued" || lo === "submitted") cls = "pending";
    return '<span class="status-badge ' + cls + '">' + esc(s) + "</span>";
  }

  // ---------------------------------------------------------------------------
  // Equity chart
  // ---------------------------------------------------------------------------

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
        options: {
          animation: false,
          responsive: true,
          maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: {
            y: {
              ticks: {
                callback: function (v) { return "$" + Number(v).toFixed(2); },
                color: "#9ca3af"
              },
              grid: { color: "rgba(148,163,184,0.08)" }
            },
            x: {
              ticks: { color: "#9ca3af", maxTicksLimit: 6 },
              grid: { display: false }
            }
          }
        }
      });
    } else {
      equityChart.data.labels = labels;
      equityChart.data.datasets[0].data = vals;
      equityChart.update("none");
    }
  }

  // ---------------------------------------------------------------------------
  // Overview tab
  // ---------------------------------------------------------------------------

  function renderOverview(vm) {
    document.getElementById("mMode").textContent = vm.mode ? String(vm.mode).toUpperCase() : "—";
    document.getElementById("mEq").textContent = vm.equity != null ? fmtMoney(vm.equity) : "—";

    var pnlD = document.getElementById("mPnlD");
    var pnlP = document.getElementById("mPnlP");
    pnlD.textContent = vm.pnlDollars != null ? fmtMoneySigned(vm.pnlDollars) : "—";
    pnlD.className = "val mono " + pnlClass(vm.pnlDollars);
    pnlP.textContent = vm.pnlPct != null ? fmtPctSigned(vm.pnlPct) : "—";
    pnlP.className = "val mono " + pnlClass(vm.pnlPct);

    document.getElementById("mCash").textContent = isFiniteNum(vm.cash) ? fmtMoney(vm.cash) : "—";
    var mo = vm.marketOpen;
    document.getElementById("mMkt").textContent = mo === true ? "OPEN" : mo === false ? "CLOSED" : "—";
    var st = vm.capitalStage || {};
    var stageText = st.stage != null ? String(st.stage) : (st.name != null ? String(st.name) : "—");
    document.getElementById("mCap").textContent = stageText.toUpperCase();

    renderOperatorSummary(vm);
    renderExecutionHealth(vm);
    renderEquityChart(vm);

    var top = (vm.positions || []).slice(0, 5);
    var tb = document.querySelector("#tblOverviewPositions tbody");
    if (tb) {
      document.getElementById("posTopEmpty").style.display = top.length ? "none" : "block";
      tb.innerHTML = top.map(function (r) {
        var ac = String(r.asset_class || "").toLowerCase();
        var q = num(r.net_qty, null);
        var up = num(r.unrealized_pnl_pct, null);
        var stRow = exitStateFor(r, vm);
        return "<tr>" +
          "<td>" + esc(r.symbol) + "</td>" +
          "<td class=\"mono\">" + esc(q != null ? fmtQty(q, ac === "crypto") : "—") + "</td>" +
          "<td class=\"mono\">" + esc(fmtPrice(r.avg_entry_price)) + "</td>" +
          "<td class=\"mono\">" + esc(fmtPrice(r.current_price)) + "</td>" +
          "<td class=\"mono " + pnlClass(up) + "\">" + esc(up != null ? fmtPctSigned(up) : "—") + "</td>" +
          "<td>" + exitBadge(stRow.status) + "</td>" +
          "</tr>";
      }).join("");
    }

    var decs = (vm.executionDecisions || []).slice(0, 10);
    document.getElementById("decEmpty").style.display = decs.length ? "none" : "block";
    var dt = document.querySelector("#tblOverviewDecisions tbody");
    if (dt) {
      dt.innerHTML = decs.map(function (r) {
        var meta = r.meta && typeof r.meta === "object" ? r.meta : {};
        var reason = meta.reason != null ? String(meta.reason) : String(r.reason_code || "—");
        return "<tr><td class=\"mono\">" + esc(fmtTimestamp(r.created_at || "")) + "</td><td>" + esc(r.symbol || "") + "</td><td>" + esc(r.side || "") + "</td><td>" + decisionBadge(r.decision || "") + "</td><td>" + esc(reason) + "</td></tr>";
      }).join("");
    }
  }

  // ---------------------------------------------------------------------------
  // Positions tab
  // ---------------------------------------------------------------------------

  function renderPositionsTab(vm) {
    var rows = vm.positions || [];
    document.getElementById("posAllEmpty").style.display = rows.length ? "none" : "block";
    var pb = document.querySelector("#tblPositionsFull tbody");
    if (!pb) return;
    pb.innerHTML = rows.map(function (r) {
      var ac = String(r.asset_class || "").toLowerCase();
      var q = num(r.net_qty, null);
      var qs = q != null ? fmtQty(q, ac === "crypto") : "—";
      var mv = num(r.market_value, null);
      var up = num(r.unrealized_pnl, null);
      var upp = num(r.unrealized_pnl_pct, null);
      var st = exitStateFor(r, vm);
      var warnNote = st.mismatchWarn
        ? '<span class="row-warn-note" title="Local qty differs from broker qty. Broker qty is used for real orders.">⚠ broker qty differs</span>'
        : "";
      return "<tr>" +
        "<td>" + esc(r.symbol) + "</td>" +
        "<td>" + esc(r.asset_class || "") + "</td>" +
        "<td class=\"mono\">" + esc(qs) + "</td>" +
        "<td class=\"mono\">" + esc(fmtPrice(r.avg_entry_price)) + "</td>" +
        "<td class=\"mono\">" + esc(fmtPrice(r.current_price)) + "</td>" +
        "<td class=\"mono\">" + esc(mv != null ? fmtMoney(mv) : "—") + "</td>" +
        "<td class=\"mono " + pnlClass(up) + "\">" + esc(up != null ? fmtMoneySigned(up) : "—") + "</td>" +
        "<td class=\"mono " + pnlClass(upp) + "\">" + esc(upp != null ? fmtPctSigned(upp) : "—") + "</td>" +
        "<td>" + exitBadge(st.status) + "</td>" +
        "<td><small>" + esc(st.explanation) + "</small>" + warnNote + "</td>" +
        "</tr>";
    }).join("");
  }

  // ---------------------------------------------------------------------------
  // Activity tab
  // ---------------------------------------------------------------------------

  function renderActivity(vm) {
    var tr = vm.recentTrades || [];
    var trCount = document.getElementById("actTradesCount");
    if (trCount) trCount.textContent = String(tr.length);
    document.getElementById("actTradesEmpty").style.display = tr.length ? "none" : "block";
    document.querySelector("#tblActivityTrades tbody").innerHTML = tr.map(function (t) {
      var ac = String(t.asset_class || "").toLowerCase();
      var q = num(t.quantity, null);
      var qs = q != null ? fmtQty(q, ac === "crypto") : "—";
      return "<tr>" +
        "<td class=\"mono\">" + esc(fmtTimestamp(t.created_at || "")) + "</td>" +
        "<td>" + esc(t.symbol || "") + "</td>" +
        "<td>" + esc(t.side || "") + "</td>" +
        "<td class=\"mono\">" + esc(qs) + "</td>" +
        "<td class=\"mono\">" + esc(fmtPrice(t.price)) + "</td>" +
        "<td class=\"mono\">" + esc(t.notional != null ? fmtMoney(t.notional) : "—") + "</td>" +
        "<td>" + tradeStatusBadge(t.status) + "</td>" +
        "</tr>";
    }).join("");

    var sig = vm.recentSignals || [];
    var sigCount = document.getElementById("actSigCount");
    if (sigCount) sigCount.textContent = String(sig.length);
    document.getElementById("actSigEmpty").style.display = sig.length ? "none" : "block";
    document.querySelector("#tblActivitySignals tbody").innerHTML = sig.map(function (s) {
      var score = s.raw_value != null ? s.raw_value : "";
      return "<tr>" +
        "<td class=\"mono\">" + esc(fmtTimestamp(s.created_at || "")) + "</td>" +
        "<td>" + esc(s.symbol || "") + "</td>" +
        "<td>" + esc(s.signal_name || "") + "</td>" +
        "<td>" + esc(s.direction || "") + "</td>" +
        "<td class=\"mono\">" + esc(String(score)) + "</td>" +
        "</tr>";
    }).join("");

    var ed = vm.executionDecisions || [];
    var decCount = document.getElementById("actDecCount");
    if (decCount) decCount.textContent = String(ed.length);
    document.getElementById("actDecEmpty").style.display = ed.length ? "none" : "block";
    document.querySelector("#tblActivityDecisions tbody").innerHTML = ed.map(function (r) {
      var meta = r.meta && typeof r.meta === "object" ? r.meta : {};
      var reason = meta.reason != null ? String(meta.reason) : String(r.reason_code || "—");
      return "<tr>" +
        "<td class=\"mono\">" + esc(fmtTimestamp(r.created_at || "")) + "</td>" +
        "<td>" + esc(r.symbol || "") + "</td>" +
        "<td>" + esc(r.side || "") + "</td>" +
        "<td>" + decisionBadge(r.decision || "") + "</td>" +
        "<td>" + esc(reason) + "</td>" +
        "</tr>";
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

  function setDebugBlock(payload) {
    var block = document.getElementById("debugStateBlock");
    if (!block) return;
    try {
      block.textContent = JSON.stringify({
        boot: "APP JS STARTED",
        mode: payload && payload.mode,
        portfolio_present: !!(payload && payload.portfolio),
        positions: Array.isArray(payload && payload.open_positions) ? payload.open_positions.length : 0,
        market_open: payload && payload.market_open,
        section_status_keys: payload && payload.section_status ? Object.keys(payload.section_status) : []
      }, null, 2);
    } catch (e) {
      block.textContent = "[debug serialization failed]";
    }
  }

  // ---------------------------------------------------------------------------
  // Fetch loop
  // ---------------------------------------------------------------------------

  async function fetchDashboard() {
    setApiChip("info", "API …");
    try {
      var response = await fetch("/api/dashboard", { cache: "no-store" });
      console.log("FETCH /api/dashboard status", response.status);
      if (!response.ok) throw new Error("HTTP " + response.status);
      var payload = await response.json();
      console.log("PAYLOAD", payload);
      var vm = mapDashboardPayload(payload);
      paintViewModel(vm);
      setError("");
      applyHealthyChips(vm);
      setDebugBlock(payload);
    } catch (error) {
      console.error(error);
      var msg = error && error.message ? error.message : String(error);
      setError(msg);
      setApiChip("bad", "API error");
    }
  }

  // ---------------------------------------------------------------------------
  // Tabs + backtest wiring (unchanged behavior)
  // ---------------------------------------------------------------------------

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

  function wireActivityExport() {
    var copyBtn = document.getElementById("btnCopyActivityExport");
    var dlBtn = document.getElementById("btnDownloadActivityExport");
    var st = document.getElementById("actExportStatus");
    if (!copyBtn || !dlBtn) return;
    function stamp(msg) {
      if (st) st.textContent = msg || "";
    }
    copyBtn.addEventListener("click", async function () {
      try {
        var r = await fetch("/api/activity/export", { cache: "no-store" });
        if (!r.ok) throw new Error("HTTP " + r.status);
        var j = await r.json();
        var text = JSON.stringify(j, null, 2);
        await navigator.clipboard.writeText(text);
        stamp("Copied activity JSON");
      } catch (e) {
        stamp(String(e && e.message ? e.message : e));
      }
    });
    dlBtn.addEventListener("click", async function () {
      try {
        var r = await fetch("/api/activity/export", { cache: "no-store" });
        if (!r.ok) throw new Error("HTTP " + r.status);
        var j = await r.json();
        var text = JSON.stringify(j, null, 2);
        var blob = new Blob([text], { type: "application/json;charset=utf-8" });
        var u = URL.createObjectURL(blob);
        var a = document.createElement("a");
        a.href = u;
        var d = new Date();
        var pad = function (n) { return String(n).padStart(2, "0"); };
        var fname = "activity-export-" + d.getFullYear() + pad(d.getMonth() + 1) + pad(d.getDate()) + "-" + pad(d.getHours()) + pad(d.getMinutes()) + pad(d.getSeconds()) + ".json";
        a.download = fname;
        a.click();
        URL.revokeObjectURL(u);
        stamp("Download started");
      } catch (e) {
        stamp(String(e && e.message ? e.message : e));
      }
    });
  }

  function startDashboard() {
    bindTabs();
    wireBacktest();
    wireActivityExport();
    fetchDashboard();
    setInterval(fetchDashboard, POLL_MS);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", startDashboard);
  } else {
    startDashboard();
  }
})();
