(function () {
  "use strict";

  var _dh = document.getElementById("dash-secret-holder");
  var DASHBOARD_SECRET = _dh ? _dh.value : "";

  function _authHeaders() {
    var h = {};
    if (DASHBOARD_SECRET) h["X-Dashboard-Secret"] = DASHBOARD_SECRET;
    return h;
  }

  function _copyWithFallback(text, statusEl, okMsg) {
    var fb = document.getElementById("mcCopyFallback");
    function showFb() {
      if (fb) {
        fb.style.display = "block";
        fb.value = text;
        fb.select();
      }
      if (statusEl) statusEl.textContent = "Clipboard blocked — text in box below (select all, copy).";
    }
    if (!navigator.clipboard || !navigator.clipboard.writeText) {
      showFb();
      return Promise.resolve(false);
    }
    return navigator.clipboard.writeText(text).then(function () {
      if (fb) fb.style.display = "none";
      if (statusEl) statusEl.textContent = okMsg || "Copied.";
      return true;
    }).catch(function () {
      showFb();
      return false;
    });
  }

  function _downloadBlob(blob, filename) {
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(a.href);
  }

  var equityChart = null;
  var POLL_MS = 30000;
  var MIN_ORDER_NOTIONAL = 1.0;
  var _sellSubmitting = false;
  var _manualSellRow = null;

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

  /** Mission Control + exports use fmtUsd — alias to fmtMoney (was undefined in production). */
  function fmtUsd(v) {
    return fmtMoney(v);
  }

  function safeText(v, fallback) {
    if (v == null || v === "") return fallback !== undefined ? fallback : "—";
    try {
      return String(v);
    } catch (e1) {
      return fallback !== undefined ? fallback : "—";
    }
  }

  function safeFmtMoney(v) {
    try {
      return fmtMoney(v);
    } catch (e2) {
      return "—";
    }
  }

  function safeFmtPct(v) {
    try {
      return fmtPct(v);
    } catch (e3) {
      return "—";
    }
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
      exitEvaluationHealth: p.exit_evaluation_health && typeof p.exit_evaluation_health === "object" ? p.exit_evaluation_health : {},
      reconciliationHealth: p.reconciliation_health && typeof p.reconciliation_health === "object" ? p.reconciliation_health : {},
      mismatchDetails: Array.isArray(p.mismatch_details) ? p.mismatch_details : [],
      positionExitRows: pe,
      payloadDegraded: p.degraded === true,
      ghostPositionCount: num(p.ghost_position_count, null),
      dbLockCount24h: num(p.db_lock_count_24h, null),
      alpacaCacheAgeSeconds: p.alpaca_cache_age_seconds != null ? Number(p.alpaca_cache_age_seconds) : null,
      alpacaCacheLastError: p.alpaca_cache_last_error != null ? String(p.alpaca_cache_last_error) : "",
      liveSafetyEnabled: safety && safety.live_enabled === true,
      buyGate: p.buy_gate && typeof p.buy_gate === "object" ? p.buy_gate : {},
      capitalStatus: p.capital_status && typeof p.capital_status === "object" ? p.capital_status : {},
      dynamicCapitalPlan: p.dynamic_capital_plan && typeof p.dynamic_capital_plan === "object" ? p.dynamic_capital_plan : null,
      capitalAllocatorSummary: p.capital_allocator_summary && typeof p.capital_allocator_summary === "object" ? p.capital_allocator_summary : {}
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
    var cs = vm.capitalStatus || {};
    if (cs.new_buys_blocked === true) {
      var minN = num(cs.min_order_notional, MIN_ORDER_NOTIONAL);
      setOpsLine(
        "opsLineBuys",
        "New buys <span class=\"warn-t\">blocked</span>, below <span class=\"mono\">" +
          esc(fmtMoney(minN)) +
          "</span> minimum order size.",
        "warn"
      );
    } else if (ubp != null && ubp < MIN_ORDER_NOTIONAL) {
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

    var eeh = vm.exitEvaluationHealth || {};
    if (eeh.market_open && eeh.fresh === false) {
      var staleCount = (eeh.stale_symbols || []).length;
      setOpsLine(
        "opsLineExitHealth",
        "Exit evaluation is <span class=\"warn-t\">STALE</span>: " + staleCount +
          " symbol(s) not freshly evaluated. Worker/export mismatch requires attention.",
        "warn"
      );
    } else if (eeh.fresh === true) {
      setOpsLine("opsLineExitHealth", "Exit evaluation is <span class=\"good\">fresh</span>.", "ok");
    } else {
      setOpsLine("opsLineExitHealth", "Exit evaluation health: N/A.", "");
    }

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
    var rh = vm.reconciliationHealth || {};
    var blocked = numOr(eh.blocked_exits_count, 0);
    var stale = numOr(rh.stale_local_rows_count != null ? rh.stale_local_rows_count : eh.stale_local_positions_count, 0);
    var mismatch = numOr(rh.broker_local_mismatch_count != null ? rh.broker_local_mismatch_count : eh.broker_local_mismatch_count, 0);
    var currMismatch = numOr(rh.current_broker_position_mismatches, 0);
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
        if (mismatch > 0) {
          if (currMismatch === 0 && stale > 0) {
            parts.push(mismatch + " stale-only (current broker positions reconciled)");
          } else {
            parts.push(mismatch + " broker/local mismatch(es)");
          }
        }
        if (rh.message) parts.push(String(rh.message));
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
      html += tile(currMismatch > 0 ? "warn" : "", "Current broker mismatches", String(currMismatch));
      html += tile(stale > 0 ? "warn" : "", "Stale local rows", String(stale));
      html += tile("", "DB lock waits/retries (24h)", dblk);
      html += tile("", "Alpaca cache age", cacheAge);
      html += tile("", "Last broker snapshot", lastSync);
      html += tile(pdtCount > 0 ? "warn" : "", "PDT guarded symbols", String(pdtCount));
      grid.innerHTML = html;
    }

    var mmSec = document.getElementById("mismatchDetailsSec");
    var mmMsg = document.getElementById("mismatchReconciledMsg");
    var mmTb = document.querySelector("#tblMismatchDetails tbody");
    var details = vm.mismatchDetails || [];
    if (mmSec) {
      if (details.length > 0 || (rh.message && String(rh.message).length)) {
        mmSec.style.display = "block";
        if (mmMsg && currMismatch === 0 && stale > 0) {
          mmMsg.style.display = "block";
          mmMsg.textContent = rh.message || "Current broker positions are reconciled. Stale local rows remain separately.";
        } else if (mmMsg) mmMsg.style.display = "none";
      } else {
        mmSec.style.display = "none";
      }
    }
    if (mmTb) {
      mmTb.innerHTML = details.map(function (m) {
        return "<tr><td>" + esc(m.symbol) + "</td><td>" + esc(m.asset_class) + "</td><td class=\"mono\">" +
          esc(m.broker_qty) + "</td><td class=\"mono\">" + esc(m.local_qty) + "</td><td class=\"mono\">" +
          esc(m.delta) + "</td><td>" + esc(m.classification) + "</td><td>" + esc(m.recommended_action) + "</td></tr>";
      }).join("");
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

  function showToast(msg, isErr) {
    var el = document.getElementById("dashToast");
    if (!el) return;
    el.textContent = msg;
    el.style.display = "block";
    el.style.borderColor = isErr ? "rgba(248,113,113,0.45)" : "rgba(52,211,153,0.35)";
    el.style.background = isErr ? "rgba(248,113,113,0.18)" : "rgba(52,211,153,0.12)";
    if (showToast._t) clearTimeout(showToast._t);
    showToast._t = setTimeout(function () {
      el.style.display = "none";
    }, 4800);
  }

  function sameLocalCalendarDayAsToday(iso) {
    if (!iso) return false;
    var raw = String(iso);
    var d = new Date(raw.length === 19 ? raw + "Z" : raw);
    if (isNaN(d.getTime())) return false;
    var now = new Date();
    return (
      d.getFullYear() === now.getFullYear() &&
      d.getMonth() === now.getMonth() &&
      d.getDate() === now.getDate()
    );
  }

  function renderCapitalCard(vm) {
    var card = document.getElementById("capitalStatusCard");
    var cs = vm.capitalStatus || {};
    var elAvail = document.getElementById("capAvailMain");
    if (!card || !elAvail) return;
    var abp = num(cs.available_buying_power, null);
    elAvail.textContent = abp != null ? fmtMoney(abp) : "—";
    var csh = num(cs.cash, null);
    var bp = num(cs.buying_power, null);
    var us = num(cs.usable_buying_power, null);
    var dep = num(cs.capital_deployed_positions, null);
    var elC = document.getElementById("capCash");
    var elBp = document.getElementById("capBP");
    var elU = document.getElementById("capUsable");
    var elD = document.getElementById("capDeployed");
    if (elC) elC.textContent = csh != null ? fmtMoney(csh) : "—";
    if (elBp) elBp.textContent = bp != null ? fmtMoney(bp) : "—";
    if (elU) elU.textContent = us != null ? fmtMoney(us) : "—";
    if (elD) elD.textContent = dep != null ? fmtMoney(dep) : "—";
    var nb = document.getElementById("capNewBuys");
    var wn = document.getElementById("capWarn");
    var minN = num(cs.min_order_notional, MIN_ORDER_NOTIONAL);
    if (cs.new_buys_blocked === true) {
      card.classList.add("capital-warn-b");
      if (nb) {
        nb.innerHTML =
          "New buys: <span class=\"warn-t\">Blocked</span>, below <span class=\"mono\">" +
          esc(fmtMoney(minN)) +
          "</span> minimum order size.";
      }
      if (wn) {
        wn.style.display = "block";
        wn.textContent =
          "Available buying power is below minimum order size. New buys are blocked.";
      }
    } else {
      card.classList.remove("capital-warn-b");
      if (nb) nb.textContent = "New buys: eligible at current buying power.";
      if (wn) {
        wn.style.display = "none";
        wn.textContent = "";
      }
    }
  }

  function renderCapitalAllocatorPanel(vm) {
    var plan = vm.dynamicCapitalPlan;
    var sum = vm.capitalAllocatorSummary || {};
    var b = plan && plan.capital_buckets ? plan.capital_buckets : {};
    var w = plan && plan.dynamic_weights ? plan.dynamic_weights : {};
    var set = function (id, txt) {
      var el = document.getElementById(id);
      if (el) el.textContent = txt == null ? "—" : String(txt);
    };
    set("dcaFreeCash", b.free_cash != null ? fmtMoney(b.free_cash) : sum.free_cash != null ? fmtMoney(sum.free_cash) : "—");
    set("dcaCryptoAvail", b.crypto_available_cash != null ? fmtMoney(b.crypto_available_cash) : "—");
    set("dcaStockMv", b.stock_market_value != null ? fmtMoney(b.stock_market_value) : "—");
    set("dcaCryptoMv", b.crypto_market_value != null ? fmtMoney(b.crypto_market_value) : "—");
    set("dcaPdtTrap", b.pdt_trapped_stock_value != null ? fmtMoney(b.pdt_trapped_stock_value) : "—");
    set("dcaSessTrap", b.market_session_trapped_stock_value != null ? fmtMoney(b.market_session_trapped_stock_value) : "—");
    set("dcaTgtStock", w.target_stock_weight != null ? fmtPct(Number(w.target_stock_weight) * 100) : "—");
    set("dcaTgtCrypto", w.target_crypto_weight != null ? fmtPct(Number(w.target_crypto_weight) * 100) : "—");
    set("dcaTgtRes", w.target_reserve_weight != null ? fmtPct(Number(w.target_reserve_weight) * 100) : "—");
    set("dcaActStock", w.actual_stock_weight != null ? fmtPct(Number(w.actual_stock_weight) * 100) : "—");
    set("dcaActCrypto", w.actual_crypto_weight != null ? fmtPct(Number(w.actual_crypto_weight) * 100) : "—");
    set("dcaActCash", w.actual_cash_weight != null ? fmtPct(Number(w.actual_cash_weight) * 100) : "—");
    set("dcaRecAct", sum.recommended_next_action != null ? String(sum.recommended_next_action) : plan ? String(plan.recommended_next_action || "—") : "—");
    set("dcaBlocker", sum.main_blocker != null ? String(sum.main_blocker) : "—");
    var cep = plan && plan.crypto_engine_plan ? plan.crypto_engine_plan : {};
    var cryptoLine =
      "Enabled: " +
      (cep.enabled ? "yes" : "no") +
      " · Positions: " +
      (Array.isArray(cep.crypto_positions) ? String(cep.crypto_positions.length) : "0") +
      " · BP (crypto cash): " +
      (cep.cash_available_for_crypto != null ? fmtMoney(cep.cash_available_for_crypto) : "—") +
      " · Best: " +
      (cep.best_crypto_candidate || "—") +
      " · Block: " +
      (cep.blocked_reason || "—") +
      " · 24/7: crypto markets always on (Alpaca)";
    set("dcaCryptoEngineLine", cryptoLine);
    var ss = plan && plan.stock_session_state ? plan.stock_session_state : {};
    var extEn = plan && plan.engine_permissions ? plan.engine_permissions.stock_extended_enabled_now : false;
    var ovnEn = plan && plan.engine_permissions ? plan.engine_permissions.stock_overnight_enabled_now : false;
    var sess =
      "Regular: " +
      (ss.regular ? "yes" : "no") +
      " · Pre: " +
      (ss.pre_market ? "yes" : "no") +
      " · After: " +
      (ss.after_hours ? "yes" : "no") +
      " · Overnight: " +
      (ss.overnight ? "yes" : "no") +
      " · Closed: " +
      (ss.closed ? "yes" : "no") +
      " · Extended exec: " +
      (extEn ? "on" : "planning only") +
      " · Overnight exec: " +
      (ovnEn ? "on" : "planning only");
    set("dcaStockSessionLine", sess);
  }

  // ---------------------------------------------------------------------------
  // Equity chart with range selector
  // ---------------------------------------------------------------------------

  var _eqCurrentRange = "1D";

  function _fmtEqDate(raw) {
    if (!raw) return "";
    var d = new Date(String(raw).replace(" ", "T") + (raw.indexOf("Z") < 0 ? "Z" : ""));
    if (isNaN(d.getTime())) return String(raw);
    var months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
    var day = d.getDate();
    var mon = months[d.getMonth()];
    var yr = d.getFullYear();
    var hh = d.getHours();
    var mm = d.getMinutes();
    var ampm = hh >= 12 ? "PM" : "AM";
    hh = hh % 12 || 12;
    var mmStr = mm < 10 ? "0" + mm : "" + mm;
    return day + " " + mon + " " + yr + ", " + hh + ":" + mmStr + " " + ampm;
  }

  function _fmtEqLabel(raw) {
    if (!raw) return "";
    var d = new Date(String(raw).replace(" ", "T") + (raw.indexOf("Z") < 0 ? "Z" : ""));
    if (isNaN(d.getTime())) return String(raw);
    var months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
    var day = d.getDate();
    var mon = months[d.getMonth()];
    var hh = d.getHours();
    var mm = d.getMinutes();
    var ampm = hh >= 12 ? "PM" : "AM";
    hh = hh % 12 || 12;
    var mmStr = mm < 10 ? "0" + mm : "" + mm;
    if (_eqCurrentRange === "1D") return hh + ":" + mmStr + " " + ampm;
    return day + " " + mon + " " + hh + ":" + mmStr;
  }

  function _updateEqRangeChange(series) {
    var el = document.getElementById("eqRangeChange");
    if (!el) return;
    if (!series || series.length < 2) { el.textContent = ""; return; }
    var first = num(series[0].equity_total, 0);
    var last = num(series[series.length - 1].equity_total, 0);
    if (first <= 0) { el.textContent = ""; return; }
    var chg = last - first;
    var pct = (chg / first) * 100;
    var sign = chg >= 0 ? "+" : "";
    var color = chg >= 0 ? "#34d399" : "#f87171";
    el.style.color = color;
    el.textContent = _eqCurrentRange + ": " + sign + "$" + chg.toFixed(2) + " (" + sign + pct.toFixed(1) + "%)";
  }

  function renderEquityChart(vm) {
    var series = vm.equitySeries || [];
    var canvas = document.getElementById("equityChart");
    var eqHint = document.getElementById("eqEmptyHint");
    var sparseHint = document.getElementById("eqSparseHint");
    if (!canvas) return;
    if (typeof Chart === "undefined") {
      if (eqHint) { eqHint.style.display = "block"; eqHint.textContent = "Chart.js not loaded."; }
      return;
    }
    if (!series.length) {
      if (eqHint) eqHint.style.display = "block";
      if (sparseHint) sparseHint.style.display = "none";
      if (equityChart) { equityChart.destroy(); equityChart = null; }
      _updateEqRangeChange([]);
      return;
    }
    if (eqHint) eqHint.style.display = "none";
    if (sparseHint) {
      if (series.length < 3) {
        sparseHint.textContent = "Only " + series.length + " equity points available for " + _eqCurrentRange + ".";
        sparseHint.style.display = "block";
      } else {
        sparseHint.style.display = "none";
      }
    }
    _updateEqRangeChange(series);
    var labels = series.map(function (r) { return _fmtEqLabel(r.snapshot_at || ""); });
    var vals = series.map(function (r) { return num(r.equity_total, 0); });
    var rawDates = series.map(function (r) { return r.snapshot_at || ""; });
    if (!equityChart) {
      equityChart = new Chart(canvas.getContext("2d"), {
        type: "line",
        data: { labels: labels, datasets: [{ data: vals, borderColor: "#34d399", tension: 0.2, pointRadius: 1, rawDates: rawDates }] },
        options: {
          animation: false,
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: { display: false },
            tooltip: {
              callbacks: {
                title: function (ctx) {
                  var idx = ctx[0] && ctx[0].dataIndex;
                  var ds = ctx[0] && ctx[0].dataset;
                  if (ds && ds.rawDates && ds.rawDates[idx]) return _fmtEqDate(ds.rawDates[idx]);
                  return ctx[0].label;
                },
                label: function (ctx) { return "Equity: $" + Number(ctx.parsed.y).toFixed(2); }
              }
            }
          },
          scales: {
            y: {
              ticks: { callback: function (v) { return "$" + Number(v).toFixed(2); }, color: "#9ca3af" },
              grid: { color: "rgba(148,163,184,0.08)" }
            },
            x: {
              ticks: { color: "#9ca3af", maxTicksLimit: 8 },
              grid: { display: false }
            }
          }
        }
      });
    } else {
      equityChart.data.labels = labels;
      equityChart.data.datasets[0].data = vals;
      equityChart.data.datasets[0].rawDates = rawDates;
      equityChart.update("none");
    }
  }

  var _eqFetchGen = 0;

  function _loadEquityRange(range) {
    _eqCurrentRange = range;
    var gen = ++_eqFetchGen;
    document.querySelectorAll(".eq-range-btn").forEach(function (b) {
      b.classList.toggle("eq-range-active", b.getAttribute("data-range") === range);
    });
    if (equityChart) {
      equityChart.destroy();
      equityChart = null;
    }
    var eqHint = document.getElementById("eqEmptyHint");
    if (eqHint) {
      eqHint.style.display = "block";
      eqHint.textContent = "Loading " + range + "…";
    }
    var t0 = Date.now();
    fetch("/api/account/history?range=" + encodeURIComponent(range), { headers: _authHeaders(), cache: "no-store" })
      .then(function (r) {
        if (!r.ok) throw new Error("/api/equity/history HTTP " + r.status);
        return r.json();
      })
      .then(function (d) {
        if (gen !== _eqFetchGen) return;
        var raw = d.points || d.series || d.legacy_equity_series || [];
        var series = raw.map(function (p) {
          return {
            snapshot_at: p.timestamp || p.snapshot_at || p.ts,
            equity_total: p.equity != null ? p.equity : p.equity_total,
            cash_total: p.cash,
            buying_power: p.buying_power
          };
        });
        var meta = document.getElementById("eqRangeChange");
        var ms = Date.now() - t0;
        if (meta) {
          var start = series.length ? _fmtEqLabel(series[0].snapshot_at) : "—";
          var end = series.length ? _fmtEqLabel(series[series.length - 1].snapshot_at) : "—";
          meta.textContent = range + " · " + (d.count || series.length) + " pts · " + start + " → " + end + " · " + ms + "ms";
        }
        if (d.insufficient_history || !series.length) {
          if (eqHint) {
            eqHint.style.display = "block";
            var msg = d.message || "Not enough history for this range yet.";
            if ((d.count || series.length) <= 1) {
              msg = "Only one day of history is available, so 5D/1W/1M may look the same. " + msg;
            }
            eqHint.textContent = msg;
          }
        } else if (eqHint) {
          eqHint.style.display = "none";
        }
        renderEquityChart({ equitySeries: series });
        var noteEl = document.getElementById("eqHistoryNote");
        var sa = d.series_available || {};
        if (noteEl) {
          if (!sa.cash && !sa.buying_power && !sa.stock_exposure) {
            noteEl.style.display = "block";
            noteEl.textContent = "Only equity history is available. Cash/BP/exposure history will appear after worker snapshots accumulate.";
          } else {
            noteEl.style.display = "none";
          }
        }
      })
      .catch(function (e) {
        if (eqHint) {
          eqHint.style.display = "block";
          eqHint.textContent = safeText(e && e.message, "Equity history failed");
        }
      });
  }

  function wireEquityRangeButtons() {
    document.querySelectorAll(".eq-range-btn").forEach(function (btn) {
      btn.addEventListener("click", function () {
        _loadEquityRange(btn.getAttribute("data-range"));
      });
    });
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

    renderCapitalCard(vm);
    renderOperatorSummary(vm);
    renderExecutionHealth(vm);
    renderCapitalAllocatorPanel(vm);
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
      var openedDisp = r.opened_at_display != null ? String(r.opened_at_display) : "N/A";
      var openedTitle =
        r.opened_at_source === "broker_sync_fallback"
          ? "Date inferred from broker sync, not original fill."
          : "";
      var symU = String(r.symbol || "").trim().toUpperCase();
      var sellBtn =
        ac === "stock" && q != null && q > 0
          ? '<button type="button" class="tab-btn sell-open-btn" data-act="sell-open" data-symbol="' +
            esc(symU) +
            '" data-ac="stock"' +
            (_sellSubmitting ? " disabled" : "") +
            ">Sell</button>"
          : "—";
      return "<tr>" +
        "<td>" + esc(r.symbol) + "</td>" +
        "<td>" + esc(r.asset_class || "") + "</td>" +
        "<td" +
        (openedTitle ? ' title="' + esc(openedTitle) + '"' : "") +
        "><small>" +
        esc(openedDisp) +
        "</small></td>" +
        "<td class=\"mono\">" + esc(qs) + "</td>" +
        "<td class=\"mono\">" + esc(fmtPrice(r.avg_entry_price)) + "</td>" +
        "<td class=\"mono\">" + esc(fmtPrice(r.current_price)) + "</td>" +
        "<td class=\"mono\">" + esc(mv != null ? fmtMoney(mv) : "—") + "</td>" +
        "<td class=\"mono " + pnlClass(up) + "\">" + esc(up != null ? fmtMoneySigned(up) : "—") + "</td>" +
        "<td class=\"mono " + pnlClass(upp) + "\">" + esc(upp != null ? fmtPctSigned(upp) : "—") + "</td>" +
        "<td>" + exitBadge(st.status) + "</td>" +
        "<td><small>" + esc(st.explanation) + "</small>" + warnNote + "</td>" +
        "<td>" + sellBtn + "</td>" +
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

  function closeManualSellModal() {
    var m = document.getElementById("manualSellModal");
    if (m) {
      m.classList.remove("open");
      m.setAttribute("aria-hidden", "true");
    }
    _manualSellRow = null;
  }

  function openManualSellModal(globalVm, row) {
    _manualSellRow = row;
    var m = document.getElementById("manualSellModal");
    var b = document.getElementById("msBody");
    var sym = String(row.symbol || "").toUpperCase();
    var bqRaw = row.broker_qty != null ? row.broker_qty : row.broker_quantity;
    var bq = bqRaw != null && isFiniteNum(bqRaw) ? Number(bqRaw) : num(row.net_qty, null);
    var cur = num(row.current_price, null);
    var up = num(row.unrealized_pnl, null);
    var upp = num(row.unrealized_pnl_pct, null);
    var opened = row.opened_at_display != null ? String(row.opened_at_display) : "N/A";
    var notional = cur != null && bq != null ? bq * cur : null;
    var parts = [];
    parts.push("Symbol: <strong>" + esc(sym) + "</strong>");
    parts.push("Broker qty: <span class=\"mono\">" + esc(bq != null ? String(bq) : "—") + "</span>");
    parts.push("Current price: " + esc(cur != null ? fmtPrice(cur) : "—"));
    parts.push("Estimated notional: " + esc(notional != null ? fmtMoney(notional) : "—"));
    parts.push(
      "Unrealized P&amp;L: " +
        esc(up != null ? fmtMoneySigned(up) : "—") +
        " (" +
        esc(upp != null ? fmtPctSigned(upp) : "—") +
        ")"
    );
    parts.push("Opened: " + esc(opened));
    if (sameLocalCalendarDayAsToday(row.opened_at)) {
      parts.push(
        '<span style="color:#fbbf24">PDT: opened today — same-day round-trip rules may block the sell.</span>'
      );
    }
    if (globalVm && globalVm.marketOpen === false) {
      parts.push('<span style="color:#fbbf24">Stock market is closed.</span>');
    }
    parts.push(
      '<span style="color:#94a3b8">This will submit a paper sell order for broker quantity only.</span>'
    );
    if (b) b.innerHTML = parts.join("<br>");
    if (m) {
      m.classList.add("open");
      m.setAttribute("aria-hidden", "false");
    }
    var btn = document.getElementById("msConfirm");
    if (btn) btn.disabled = _sellSubmitting;
  }

  async function confirmManualSell() {
    if (!_manualSellRow || _sellSubmitting) return;
    var sym = String(_manualSellRow.symbol || "").toUpperCase();
    var btn = document.getElementById("msConfirm");
    _sellSubmitting = true;
    if (btn) btn.disabled = true;
    try {
      var r = await fetch("/api/positions/sell", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Dashboard-Secret": DASHBOARD_SECRET
        },
        body: JSON.stringify({
          symbol: sym,
          asset_class: "stock",
          quantity: "all",
          confirm: true
        })
      });
      var j = await r.json().catch(function () {
        return {};
      });
      if (j && j.ok) {
        showToast(j.message || "Manual paper sell submitted.", false);
      } else {
        showToast((j && j.message) || ("Sell blocked: " + ((j && j.reason_code) || "unknown")), true);
      }
    } catch (e) {
      showToast(String(e && e.message ? e.message : e), true);
    } finally {
      _sellSubmitting = false;
      if (btn) btn.disabled = false;
      closeManualSellModal();
      await fetchDashboard();
    }
  }

  function wireManualSell() {
    document.addEventListener("click", function (ev) {
      var t = ev.target;
      if (!t || !t.getAttribute) return;
      if (t.getAttribute("data-act") !== "sell-open") return;
      var sym = t.getAttribute("data-symbol");
      var vm = window.__dashVm;
      var rows = (vm && vm.positions) || [];
      var row = null;
      var i;
      for (i = 0; i < rows.length; i++) {
        if (String(rows[i].symbol || "").toUpperCase() === String(sym || "").toUpperCase()) {
          row = rows[i];
          break;
        }
      }
      if (row) openManualSellModal(vm, row);
    });
    var c = document.getElementById("msCancel");
    var k = document.getElementById("msConfirm");
    if (c) c.addEventListener("click", closeManualSellModal);
    if (k) k.addEventListener("click", function () {
      confirmManualSell();
    });
    var md = document.getElementById("manualSellModal");
    if (md) {
      md.addEventListener("click", function (ev) {
        if (ev.target === md) closeManualSellModal();
      });
    }
  }

  // ---------------------------------------------------------------------------
  // Fetch loop
  // ---------------------------------------------------------------------------

  async function fetchDashboard() {
    setApiChip("info", "API …");
    try {
      var response = await fetch("/api/dashboard", { cache: "no-store" });
      if (!response.ok) throw new Error("HTTP " + response.status);
      var payload = await response.json();
      var vm = mapDashboardPayload(payload);
      window.__dashVm = vm;
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
  // Tabs — URL hash is source of truth; default Overview (no persistence).
  // ---------------------------------------------------------------------------

  function tabNameFromHash() {
    var raw = (typeof location !== "undefined" && location.hash) ? String(location.hash) : "";
    var h = raw.replace(/^#/, "").trim().toLowerCase();
    var valid = ["mission", "overview", "positions", "activity", "backtest", "ai", "ops", "files", "config"];
    if (valid.indexOf(h) >= 0) return h;
    return "mission";
  }

  function syncHashToTab(name) {
    try {
      if (typeof history !== "undefined" && history.replaceState) {
        var base = location.pathname + (location.search || "");
        if (name === "overview") {
          history.replaceState(null, "", base);
        } else {
          history.replaceState(null, "", base + "#" + name);
        }
      } else if (typeof location !== "undefined") {
        location.hash = name === "overview" ? "" : "#" + name;
      }
    } catch (e1) {}
  }

  function bindTabs() {
    var tabs = document.querySelectorAll("nav .tab-btn");
    var panels = document.querySelectorAll(".tab-panel");
    function show(name) {
      var i;
      for (i = 0; i < tabs.length; i++) {
        tabs[i].classList.toggle("active", tabs[i].getAttribute("data-tab") === name);
      }
      for (i = 0; i < panels.length; i++) {
        panels[i].classList.toggle("active", panels[i].id === "panel-" + name);
      }
      syncHashToTab(name);
      if (name === "mission") {
        loadMissionTab(false);
        scheduleMissionGraphLoad();
      }
      if (name === "config") loadConfigEditor();
      if (name === "backtest" && !window.__btDefaultsLoaded) loadBacktestDefaultsOnce();
      if (name === "ops") loadOpsTab();
      if (name === "files") loadFilesTab();
      if (name === "ai") loadAiTab();
    }
    var t;
    for (t = 0; t < tabs.length; t++) {
      (function (btn) {
        btn.addEventListener("click", function () {
          show(btn.getAttribute("data-tab"));
        });
      })(tabs[t]);
    }
    try {
      localStorage.removeItem("quantbot_dash_tab");
    } catch (eRm) {}
    window.addEventListener("hashchange", function () {
      show(tabNameFromHash());
    });
    show(tabNameFromHash());
  }

  var btRunId = null;
  var btEquityChart = null;

  function btEl(id) {
    return document.getElementById(id);
  }

  function hideBtRunError() {
    var e = btEl("btRunError");
    if (e) {
      e.style.display = "none";
      e.textContent = "";
    }
  }

  function showBtRunError(msg) {
    var e = btEl("btRunError");
    if (e) {
      e.style.display = "block";
      e.textContent = msg || "Run failed.";
    }
  }

  function slimBacktestResultForDebug(row) {
    if (!row || typeof row !== "object") return row;
    var o = {};
    var k;
    for (k in row) {
      if (Object.prototype.hasOwnProperty.call(row, k)) o[k] = row[k];
    }
    var c = o.equity_curve;
    if (Array.isArray(c) && c.length > 300) {
      o.equity_curve = c.slice(0, 150).concat({ _note: "…truncated…" }).concat(c.slice(-80));
    }
    var tr = o.trades;
    if (Array.isArray(tr) && tr.length > 200) {
      o.trades = tr.slice(0, 120).concat({ _note: "…truncated…" });
    }
    return o;
  }

  function renderBacktestEquityChart(curve) {
    var canvas = btEl("btEquityChart");
    var hint = btEl("btEqEmptyHint");
    if (!canvas) return;
    if (typeof Chart === "undefined") {
      if (hint) {
        hint.style.display = "block";
        hint.textContent = "Chart.js not loaded.";
      }
      return;
    }
    var series = Array.isArray(curve) ? curve : [];
    if (!series.length) {
      if (hint) {
        hint.style.display = "block";
        hint.textContent = "No equity curve for this run.";
      }
      if (btEquityChart) {
        btEquityChart.destroy();
        btEquityChart = null;
      }
      return;
    }
    if (hint) hint.style.display = "none";
    var labels = series.map(function (p) {
      return p == null || p.timestamp == null ? "" : String(p.timestamp);
    });
    var vals = series.map(function (p) {
      return num(p && p.equity, 0);
    });
    if (!btEquityChart) {
      btEquityChart = new Chart(canvas.getContext("2d"), {
        type: "line",
        data: { labels: labels, datasets: [{ data: vals, borderColor: "#38bdf8", tension: 0.2, pointRadius: 0 }] },
        options: {
          animation: false,
          responsive: true,
          maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: {
            y: {
              ticks: {
                callback: function (v) {
                  return "$" + Number(v).toFixed(2);
                },
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
      btEquityChart.data.labels = labels;
      btEquityChart.data.datasets[0].data = vals;
      btEquityChart.update("none");
    }
  }

  function populateBacktestTradesTable(trades) {
    var tb = document.querySelector("#tblBacktestTrades tbody");
    var empty = btEl("btTradesEmpty");
    if (!tb) return;
    var t = Array.isArray(trades) ? trades : [];
    if (empty) empty.style.display = t.length ? "none" : "block";
    tb.innerHTML = t
      .map(function (tr) {
        var pnl = tr.pnl;
        var pnlStr = pnl != null && isFiniteNum(pnl) ? fmtMoneySigned(Number(pnl)) : "—";
        return (
          "<tr><td>" +
          esc(tr.timestamp) +
          "</td><td>" +
          esc(tr.symbol) +
          "</td><td>" +
          esc(tr.asset_class) +
          "</td><td>" +
          esc(tr.side) +
          "</td><td class=\"mono\">" +
          esc(tr.qty != null ? String(tr.qty) : "") +
          "</td><td class=\"mono\">" +
          esc(tr.price != null ? fmtPrice(tr.price) : "—") +
          "</td><td class=\"mono\">" +
          esc(pnlStr) +
          "</td><td>" +
          esc(tr.reason_code || "") +
          "</td></tr>"
        );
      })
      .join("");
  }

  function setBtMetric(id, text) {
    var n = btEl(id);
    if (n) n.textContent = text == null || text === "" ? "—" : String(text);
  }

  function populateBacktestFromResult(row) {
    var summ = (row && row.summary_json) || {};
    var hint = btEl("btNoRunHint");
    var wrap = btEl("btSummaryMetricsWrap");
    if (hint) hint.style.display = "none";
    if (wrap) wrap.style.display = "grid";
    setBtMetric("btMetricStartingCash", fmtMoney(summ.starting_cash));
    setBtMetric("btMetricFinalEquity", fmtMoney(summ.final_equity));
    setBtMetric("btMetricPnl", summ.pnl != null && isFiniteNum(summ.pnl) ? fmtMoneySigned(Number(summ.pnl)) : "—");
    setBtMetric("btMetricReturnPct", summ.return_pct != null && isFiniteNum(summ.return_pct) ? fmtPctSigned(Number(summ.return_pct)) : "—");
    var bh =
      summ.equal_weight_buy_and_hold_return_pct != null && isFiniteNum(summ.equal_weight_buy_and_hold_return_pct)
        ? Number(summ.equal_weight_buy_and_hold_return_pct)
        : summ.benchmark_return_pct != null && isFiniteNum(summ.benchmark_return_pct)
          ? Number(summ.benchmark_return_pct)
          : null;
    setBtMetric("btMetricBuyHold", bh != null ? fmtPctSigned(bh) : "—");
    var ex = summ.excess_return_pct;
    setBtMetric("btMetricExcessReturn", ex != null && isFiniteNum(ex) ? fmtPctSigned(Number(ex)) : "—");
    setBtMetric("btMetricMaxDd", summ.max_drawdown_pct != null && isFiniteNum(summ.max_drawdown_pct) ? fmtPct(Number(summ.max_drawdown_pct)) : "—");
    setBtMetric("btMetricTotalTrades", summ.trades_total != null ? String(summ.trades_total) : "—");
    setBtMetric("btMetricClosedTrades", summ.closed_trades != null ? String(summ.closed_trades) : "—");
    setBtMetric("btMetricWinRate", summ.win_rate_pct != null && isFiniteNum(summ.win_rate_pct) ? fmtPct(Number(summ.win_rate_pct)) : "—");
    setBtMetric("btMetricConfidence", summ.confidence_label != null ? String(summ.confidence_label) : "—");
    renderBacktestEquityChart(row && row.equity_curve);
    populateBacktestTradesTable(row && row.trades);
    var rej = (row && row.rejection_summary_json) || {};
    var pr = btEl("btRejectionsSummary");
    if (pr) pr.textContent = JSON.stringify(rej, null, 2);
    var dr = btEl("btLastRunDebug");
    if (dr) dr.textContent = JSON.stringify(slimBacktestResultForDebug(row), null, 2);
  }

  function scrollToBacktestSummary() {
    var sec = btEl("btResultSummarySection");
    if (sec && sec.scrollIntoView) {
      try {
        sec.scrollIntoView({ behavior: "smooth", block: "start" });
      } catch (e2) {
        sec.scrollIntoView(true);
      }
    }
  }

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
      document.getElementById("btStatus").textContent = "Defaults loaded. Configure inputs, then Run Backtest.";
      window.__btDefaultsLoaded = true;
    } catch (e) {
      document.getElementById("btStatus").textContent = "Could not load backtest defaults.";
    }
  }

  function wireBacktest() {
    document.getElementById("btRunBtn").addEventListener("click", async function () {
      hideBtRunError();
      document.getElementById("btCopyReportBtn").disabled = true;
      document.getElementById("btDownloadReportBtn").disabled = true;
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
        if (!r.ok || !j.run_id) throw new Error((j && j.error) || "run failed");
        btRunId = j.run_id;
        var rr = await fetch("/api/backtest/result/" + encodeURIComponent(btRunId), { cache: "no-store" });
        if (!rr.ok) throw new Error("Could not load run " + btRunId);
        var row = await rr.json();
        populateBacktestFromResult(row);
        document.getElementById("btCopyReportBtn").disabled = false;
        document.getElementById("btDownloadReportBtn").disabled = false;
        document.getElementById("btStatus").textContent = "Backtest completed.";
        scrollToBacktestSummary();
      } catch (e) {
        var msg = String((e && e.message) || e);
        showBtRunError(msg);
        document.getElementById("btStatus").textContent = "Run did not complete. Fix the issue below and try again.";
      }
    });
    document.getElementById("btCompareBtn").addEventListener("click", async function () {
      hideBtRunError();
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
        if (!r.ok || !j.ok) throw new Error((j && j.error) || "compare failed");
        var out = btEl("btCompareOutput");
        if (out) out.textContent = JSON.stringify(j.rows || [], null, 2);
        document.getElementById("btStatus").textContent = "Compare finished (" + (j.rows || []).length + " rows). See Advanced section for details.";
      } catch (e) {
        var msg2 = String((e && e.message) || e);
        showBtRunError(msg2);
        document.getElementById("btStatus").textContent = "Compare did not complete.";
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
        showBtRunError(String((e && e.message) || e));
        document.getElementById("btStatus").textContent = "Copy report failed.";
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
        showBtRunError(String((e && e.message) || e));
        document.getElementById("btStatus").textContent = "Download failed.";
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

  function wireBrokerDiagnosticCopy() {
    var btn = document.getElementById("btnCopyBrokerDiagnostic");
    var st = document.getElementById("brokerDiagExportStatus");
    if (!btn) return;
    function stamp(msg) {
      if (st) st.textContent = msg || "";
    }
    btn.addEventListener("click", async function () {
      try {
        var r = await fetch("/api/broker/diagnostic", { cache: "no-store" });
        if (!r.ok) throw new Error("HTTP " + r.status);
        var text = await r.text();
        await navigator.clipboard.writeText(text);
        stamp("Copied broker diagnostic JSON");
      } catch (e) {
        stamp(String(e && e.message ? e.message : e));
      }
    });
  }

  function wireCapitalAllocatorCopy() {
    var btn = document.getElementById("btnCopyCapitalAllocatorJson");
    var st = document.getElementById("dcaCopyStatus");
    if (!btn) return;
    btn.addEventListener("click", async function () {
      try {
        var vm = window.__dashVm || {};
        var blob = {
          dynamic_capital_plan: vm.dynamicCapitalPlan || null,
          capital_allocator_summary: vm.capitalAllocatorSummary || {}
        };
        var text = JSON.stringify(blob, null, 2);
        await navigator.clipboard.writeText(text);
        if (st) st.textContent = "Copied allocator JSON";
      } catch (e) {
        if (st) st.textContent = String(e && e.message ? e.message : e);
      }
    });
  }

  var _opsLoaded = false;
  var _opsStatusCache = null;
  var _opsResourceCache = null;
  var _opsLogsCache = null;

  function _opsStamp(msg) {
    var st = document.getElementById("opsCopyStatus");
    if (st) st.textContent = msg || "";
  }

  function _opsRingColor(pct) {
    if (pct == null || isNaN(pct)) return "var(--border)";
    if (pct >= 90) return "var(--bad)";
    if (pct >= 75) return "#f59e0b";
    return "var(--good)";
  }

  function renderOpsRing(label, value, unit, maxVal) {
    var pct = null;
    if (value != null && !isNaN(value) && maxVal > 0) {
      pct = Math.min(100, Math.max(0, (Number(value) / maxVal) * 100));
    } else if (value != null && !isNaN(value)) {
      pct = Math.min(100, Math.max(0, Number(value)));
    }
    var display = value == null || isNaN(value) ? "—" : String(Math.round(Number(value) * 10) / 10) + (unit || "");
    var border = _opsRingColor(pct);
    var wrap = document.createElement("div");
    wrap.className = "ops-ring-wrap";
    var ring = document.createElement("div");
    ring.className = "ops-ring";
    ring.style.borderColor = border;
    ring.textContent = display;
    var lab = document.createElement("div");
    lab.className = "ops-ring-lab";
    lab.textContent = label;
    wrap.appendChild(ring);
    wrap.appendChild(lab);
    return wrap;
  }

  function renderOpsRings(snap) {
    var host = document.getElementById("opsRings");
    if (!host) return;
    host.innerHTML = "";
    snap = snap || {};
    host.appendChild(renderOpsRing("CPU", snap.process_cpu_pct, "%", 100));
    host.appendChild(renderOpsRing("Memory", snap.system_memory_pct, "%", 100));
    host.appendChild(renderOpsRing("/data disk", snap.disk_used_pct, "%", 100));
    host.appendChild(renderOpsRing("Trading DB", snap.quantbot_db_mb, " MB", 1024));
    host.appendChild(renderOpsRing("AI memory DB", snap.ai_memory_db_mb, " MB", 512));
    host.appendChild(renderOpsRing("Ops DB/logs", (Number(snap.ops_db_mb) || 0) + (Number(snap.logs_dir_mb) || 0), " MB", 512));
  }

  function renderOpsLogsTable(logs) {
    var tb = document.querySelector("#tblOpsLogs tbody");
    if (!tb) return;
    tb.innerHTML = "";
    (logs || []).slice(0, 50).forEach(function (lg) {
      var tr = document.createElement("tr");
      tr.innerHTML =
        "<td class=\"mono\">" + esc(String(lg.created_at || "—")) + "</td>" +
        "<td>" + esc(String(lg.level || "—")) + "</td>" +
        "<td class=\"mono\">" + esc(String(lg.event_type || "—")) + "</td>" +
        "<td>" + esc(String(lg.message || "—")) + "</td>";
      tb.appendChild(tr);
    });
  }

  function loadOpsTab() {
    Promise.all([
      fetch("/api/ops/status", { cache: "no-store" }).then(function (r) {
        if (!r.ok) throw new Error("ops/status HTTP " + r.status);
        return r.json();
      }),
      fetch("/api/ops/resources/latest", { cache: "no-store" }).then(function (r) {
        if (!r.ok) throw new Error("ops/resources HTTP " + r.status);
        return r.json();
      }),
      fetch("/api/ops/logs?limit=50", { cache: "no-store" }).then(function (r) {
        if (!r.ok) throw new Error("ops/logs HTTP " + r.status);
        return r.json();
      })
    ]).then(function (parts) {
      var status = parts[0] || {};
      var resource = parts[1] || {};
      var logsPayload = parts[2] || {};
      _opsStatusCache = status;
      _opsResourceCache = resource;
      _opsLogsCache = logsPayload.logs || [];

      var snap = resource.created_at ? resource : (status.resource_snapshot || {});
      renderOpsRings(snap);

      var railway = status.railway || {};
      var rs = document.getElementById("opsRailwayStatus");
      if (rs) {
        if (railway.railway_api_connected) {
          rs.textContent = "Railway API: connected";
          rs.className = "empty-hint pos-good";
        } else if (railway.volume_ops_active || railway.reason === "api_polling_off") {
          rs.textContent =
            (railway.note || "Running on Railway — volume, DB, and ops logs are active.") +
            (railway.service_id ? " Service: " + railway.service_id : "");
          rs.className = "empty-hint pos-good";
        } else {
          var err = railway.safe_error || railway.note || railway.reason || "Railway data unavailable";
          rs.textContent = "Railway API: optional — " + err;
          rs.className = "empty-hint";
        }
      }

      var st = document.getElementById("opsSnapshotTime");
      if (st) st.textContent = "Last snapshot: " + (snap.created_at || "—");

      var lc = document.getElementById("opsLogCount");
      if (lc) lc.textContent = String((_opsLogsCache || []).length);

      var crit = (_opsLogsCache || []).filter(function (lg) {
        var lv = String(lg.level || "").toLowerCase();
        return lv === "critical" || lv === "error" || lv === "warning";
      });
      var cc = document.getElementById("opsCriticalCount");
      if (cc) cc.textContent = String(crit.length);

      var cost = status.runtime_cost_control_status || {};
      var cp = document.getElementById("opsCostPressure");
      if (cp) cp.textContent = String(cost.cost_pressure || "—");

      var up = document.getElementById("opsUptime");
      if (up) {
        var sec = Number(snap.uptime_seconds);
        up.textContent = isNaN(sec) ? "—" : Math.round(sec / 60) + " min";
      }

      renderOpsLogsTable(_opsLogsCache);
      if (!_opsLogsCache.length) {
        _opsStamp("No ops log rows yet — worker cycle events will appear after the next trading cycle.");
      }
      _opsLoaded = true;
    }).catch(function (e) {
      _opsStamp("Ops load failed: " + (e && e.message ? e.message : e));
      var rs = document.getElementById("opsRailwayStatus");
      if (rs) {
        rs.textContent = "Railway API: disconnected — ops endpoints unavailable";
        rs.className = "empty-hint pos-bad";
      }
    });
  }

  var _volLoaded = false;
  var _volRoot = "persist";
  var _volPath = "";
  var _volSelectedFile = null;

  function volHeaders(json) {
    var h = { "X-Dashboard-Secret": DASHBOARD_SECRET };
    if (json) {
      h["Content-Type"] = "application/json";
    }
    return h;
  }

  function volStatus(msg, isBad) {
    var el = document.getElementById("volStatus");
    if (!el) return;
    el.textContent = msg || "";
    el.className = isBad ? "pos-bad" : "";
  }

  function volJoinPath(base, name) {
    if (!base) return name;
    return base.replace(/\/+$/, "") + "/" + name;
  }

  function loadVolumeRoots() {
    return fetch("/api/volume/roots", { cache: "no-store" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (j) {
        var sel = document.getElementById("volRootSelect");
        if (!sel) return;
        sel.innerHTML = "";
        var roots = j.roots || {};
        Object.keys(roots).forEach(function (key) {
          var opt = document.createElement("option");
          opt.value = key;
          opt.textContent = key + " — " + (roots[key].path || "");
          sel.appendChild(opt);
        });
        if (sel.options.length) {
          sel.value = _volRoot;
          if (!sel.value) {
            sel.value = sel.options[0].value;
            _volRoot = sel.value;
          }
        }
        var meta = document.getElementById("volFileMeta");
        if (meta && j.db_path) {
          meta.setAttribute("data-db-path", j.db_path);
        }
      });
  }

  function renderVolumeTree(data) {
    var tree = document.getElementById("volTree");
    var crumb = document.getElementById("volBreadcrumb");
    if (!tree) return;
    tree.innerHTML = "";
    _volPath = data.path || "";
    if (crumb) {
      crumb.textContent = (_volRoot || "persist") + ":/" + (_volPath || "");
    }
    if (data.parent !== undefined && _volPath) {
      var up = document.createElement("button");
      up.type = "button";
      up.className = "vol-tree-item";
      up.textContent = "..";
      up.addEventListener("click", function () {
        loadVolumeDir(data.parent || "");
      });
      tree.appendChild(up);
    }
    (data.entries || []).forEach(function (ent) {
      var btn = document.createElement("button");
      btn.type = "button";
      var icon = ent.type === "dir" ? "📁 " : "📄 ";
      btn.className = "vol-tree-item" + (_volSelectedFile === volJoinPath(_volPath, ent.name) ? " active" : "");
      btn.textContent = icon + ent.name + (ent.size != null ? " (" + ent.size + " B)" : "");
      btn.addEventListener("click", function () {
        var full = volJoinPath(_volPath, ent.name);
        if (ent.type === "dir") {
          loadVolumeDir(full);
        } else {
          openVolumeFile(full);
        }
      });
      tree.appendChild(btn);
    });
  }

  function loadVolumeDir(path) {
    _volPath = path || "";
    var q = "/api/volume/list?root=" + encodeURIComponent(_volRoot) + "&path=" + encodeURIComponent(_volPath);
    return fetch(q, { cache: "no-store" })
      .then(function (r) {
        return r.json();
      })
      .then(function (j) {
        if (!j.ok) throw new Error(j.error || "list failed");
        renderVolumeTree(j);
        volStatus("");
      })
      .catch(function (e) {
        volStatus("List failed: " + (e && e.message ? e.message : e), true);
      });
  }

  function hideVolSqlitePanel() {
    var panel = document.getElementById("volSqlitePanel");
    if (panel) panel.style.display = "none";
  }

  function showVolSqliteTables(path) {
    var panel = document.getElementById("volSqlitePanel");
    var host = document.getElementById("volSqliteTables");
    var pre = document.getElementById("volSqlitePreview");
    if (!panel || !host) return;
    panel.style.display = "block";
    host.innerHTML = "<span class='empty-hint'>Loading tables…</span>";
    if (pre) pre.textContent = "";
    var q =
      "/api/volume/sqlite/tables?root=" +
      encodeURIComponent(_volRoot) +
      "&path=" +
      encodeURIComponent(path);
    fetch(q, { cache: "no-store" })
      .then(function (r) {
        return r.json();
      })
      .then(function (j) {
        if (!j.ok) throw new Error(j.error || "tables failed");
        host.innerHTML = "";
        (j.tables || []).forEach(function (t) {
          var b = document.createElement("button");
          b.type = "button";
          b.className = "tab-btn";
          b.style.fontSize = "11px";
          b.textContent = t.name + (t.row_count != null ? " (" + t.row_count + ")" : "");
          b.addEventListener("click", function () {
            loadVolSqlitePreview(path, t.name);
          });
          host.appendChild(b);
        });
        if (!(j.tables || []).length && pre) {
          pre.textContent = "No user tables in this database.";
        }
      })
      .catch(function (e) {
        host.innerHTML = "";
        if (pre) pre.textContent = "SQLite browse failed: " + (e && e.message ? e.message : e);
      });
  }

  function loadVolSqlitePreview(path, table) {
    var pre = document.getElementById("volSqlitePreview");
    if (!pre) return;
    pre.textContent = "Loading " + table + "…";
    var q =
      "/api/volume/sqlite/preview?root=" +
      encodeURIComponent(_volRoot) +
      "&path=" +
      encodeURIComponent(path) +
      "&table=" +
      encodeURIComponent(table) +
      "&limit=40";
    fetch(q, { cache: "no-store" })
      .then(function (r) {
        return r.json();
      })
      .then(function (j) {
        if (!j.ok) throw new Error(j.error || "preview failed");
        pre.textContent = JSON.stringify(
          { table: j.table, columns: j.columns, rows: j.rows },
          null,
          2
        );
      })
      .catch(function (e) {
        pre.textContent = "Preview failed: " + (e && e.message ? e.message : e);
      });
  }

  function openVolumeFile(path) {
    _volSelectedFile = path;
    hideVolSqlitePanel();
    var q =
      "/api/volume/read?root=" + encodeURIComponent(_volRoot) + "&path=" + encodeURIComponent(path);
    fetch(q, { cache: "no-store" })
      .then(function (r) {
        return r.json();
      })
      .then(function (j) {
        if (!j.ok) throw new Error(j.error || "read failed");
        var ed = document.getElementById("volEditor");
        var meta = document.getElementById("volFileMeta");
        if (meta) {
          meta.textContent =
            j.name +
            " · " +
            (j.size != null ? j.size + " bytes" : "") +
            (j.editable ? " · editable" : " · browse/download");
        }
        if (!ed) return;
        if (j.note === "sqlite_use_table_browser" || j.sqlite) {
          ed.disabled = true;
          ed.value =
            "SQLite database — use the table buttons below to preview rows (read-only).";
          showVolSqliteTables(path);
        } else if (j.note === "sqlite_sidecar_open_main_db") {
          ed.disabled = true;
          ed.value =
            "WAL/SHM sidecar file — open quantbot.sqlite3 (not -wal) to browse tables.";
        } else if (j.editable) {
          ed.disabled = false;
          ed.value = j.content != null ? j.content : "";
        } else {
          ed.disabled = true;
          ed.value = j.note || "Binary or non-text file — use Download.";
        }
        volStatus("Opened " + path);
        loadVolumeDir(_volPath);
      })
      .catch(function (e) {
        volStatus("Read failed: " + (e && e.message ? e.message : e), true);
      });
  }

  function loadFilesTab() {
    loadVolumeRoots()
      .then(function () {
        return loadVolumeDir(_volPath || "");
      })
      .then(function () {
        _volLoaded = true;
      })
      .catch(function (e) {
        volStatus(String(e && e.message ? e.message : e), true);
      });
  }

  function wireVolumeFiles() {
    var sel = document.getElementById("volRootSelect");
    var quick = document.querySelectorAll(".vol-quick");
    var btnRef = document.getElementById("btnVolRefresh");
    var btnSave = document.getElementById("btnVolSave");
    var btnNew = document.getElementById("btnVolNewFile");
    var btnMk = document.getElementById("btnVolNewFolder");
    var btnDel = document.getElementById("btnVolDelete");
    var btnDl = document.getElementById("btnVolDownload");
    if (!sel) return;

    quick.forEach(function (btn) {
      btn.addEventListener("click", function () {
        var rel = btn.getAttribute("data-rel") || "";
        if (rel && (rel.endsWith(".sqlite3") || rel.endsWith(".sqlite"))) {
          _volPath = "";
          loadVolumeDir("").then(function () {
            openVolumeFile(rel);
          });
        } else if (rel) {
          loadVolumeDir(rel);
        } else {
          loadVolumeDir("");
        }
      });
    });

    sel.addEventListener("change", function () {
      _volRoot = sel.value;
      _volPath = "";
      _volSelectedFile = null;
      loadVolumeDir("");
    });
    if (btnRef) {
      btnRef.addEventListener("click", function () {
        loadVolumeDir(_volPath);
      });
    }
    if (btnSave) {
      btnSave.addEventListener("click", function () {
        if (!_volSelectedFile) {
          volStatus("Select a file first", true);
          return;
        }
        var ed = document.getElementById("volEditor");
        fetch("/api/volume/write", {
          method: "PUT",
          headers: volHeaders(true),
          body: JSON.stringify({
            root: _volRoot,
            path: _volSelectedFile,
            content: ed ? ed.value : ""
          })
        })
          .then(function (r) {
            return r.json();
          })
          .then(function (j) {
            if (!j.ok) throw new Error(j.error || "save failed");
            volStatus("Saved " + _volSelectedFile);
            loadVolumeDir(_volPath);
          })
          .catch(function (e) {
            volStatus("Save failed: " + (e && e.message ? e.message : e), true);
          });
      });
    }
    if (btnNew) {
      btnNew.addEventListener("click", function () {
        var name = window.prompt("New file name (relative to current folder):", "notes.txt");
        if (!name) return;
        var rel = volJoinPath(_volPath, name.trim());
        var ed = document.getElementById("volEditor");
        var content = ed && !ed.disabled ? ed.value : "";
        fetch("/api/volume/write", {
          method: "PUT",
          headers: volHeaders(true),
          body: JSON.stringify({
            root: _volRoot,
            path: rel,
            content: content,
            create: true
          })
        })
          .then(function (r) {
            return r.json();
          })
          .then(function (j) {
            if (!j.ok) throw new Error(j.error || "create failed");
            _volSelectedFile = rel;
            volStatus("Created " + rel);
            loadVolumeDir(_volPath);
            openVolumeFile(rel);
          })
          .catch(function (e) {
            volStatus("Create failed: " + (e && e.message ? e.message : e), true);
          });
      });
    }
    if (btnMk) {
      btnMk.addEventListener("click", function () {
        var name = window.prompt("New folder name:", "archive");
        if (!name) return;
        fetch("/api/volume/mkdir", {
          method: "POST",
          headers: volHeaders(true),
          body: JSON.stringify({
            root: _volRoot,
            path: volJoinPath(_volPath, name.trim())
          })
        })
          .then(function (r) {
            return r.json();
          })
          .then(function (j) {
            if (!j.ok) throw new Error(j.error || "mkdir failed");
            volStatus("Created folder");
            loadVolumeDir(_volPath);
          })
          .catch(function (e) {
            volStatus("Folder failed: " + (e && e.message ? e.message : e), true);
          });
      });
    }
    if (btnDel) {
      btnDel.addEventListener("click", function () {
        var target = _volSelectedFile;
        if (!target) {
          volStatus("Select a file in the tree first", true);
          return;
        }
        if (!window.confirm("Delete " + target + " ? This cannot be undone.")) return;
        fetch("/api/volume/delete", {
          method: "DELETE",
          headers: volHeaders(true),
          body: JSON.stringify({ root: _volRoot, path: target })
        })
          .then(function (r) {
            return r.json();
          })
          .then(function (j) {
            if (!j.ok) throw new Error(j.error || "delete failed");
            _volSelectedFile = null;
            var ed = document.getElementById("volEditor");
            if (ed) {
              ed.value = "";
              ed.disabled = true;
            }
            volStatus("Deleted");
            loadVolumeDir(_volPath);
          })
          .catch(function (e) {
            volStatus("Delete failed: " + (e && e.message ? e.message : e), true);
          });
      });
    }
    if (btnDl) {
      btnDl.addEventListener("click", function () {
        if (!_volSelectedFile) {
          volStatus("Select a file first", true);
          return;
        }
        var url =
          "/api/volume/download?root=" +
          encodeURIComponent(_volRoot) +
          "&path=" +
          encodeURIComponent(_volSelectedFile);
        window.open(url, "_blank");
      });
    }
  }

  function wireOpsCenter() {
    var btnStatus = document.getElementById("btnCopyOpsStatus");
    var btnSnap = document.getElementById("btnCopyResourceSnapshot");
    var btnLogs = document.getElementById("btnCopyRecentOpsLogs");
    var btnCrit = document.getElementById("btnCopyCriticalOpsBundle");
    var btnCsv = document.getElementById("btnDownloadOpsLogsCsv");
    var btnXlsx = document.getElementById("btnDownloadDailyReportXlsx");
    if (!btnStatus) return;

    btnStatus.addEventListener("click", async function () {
      try {
        var r = await fetch("/api/ops/status", { cache: "no-store" });
        if (!r.ok) throw new Error("HTTP " + r.status);
        var j = await r.json();
        await navigator.clipboard.writeText(JSON.stringify(j, null, 2));
        _opsStamp("Copied ops status JSON");
      } catch (e) {
        _opsStamp(String(e && e.message ? e.message : e));
      }
    });

    btnSnap.addEventListener("click", async function () {
      try {
        var r = await fetch("/api/ops/resources/latest", { cache: "no-store" });
        if (!r.ok) throw new Error("HTTP " + r.status);
        var j = await r.json();
        await navigator.clipboard.writeText(JSON.stringify(j, null, 2));
        _opsStamp("Copied resource snapshot JSON");
      } catch (e) {
        _opsStamp(String(e && e.message ? e.message : e));
      }
    });

    btnLogs.addEventListener("click", async function () {
      try {
        var r = await fetch("/api/ops/logs?limit=50", { cache: "no-store" });
        if (!r.ok) throw new Error("HTTP " + r.status);
        var j = await r.json();
        await navigator.clipboard.writeText(JSON.stringify(j, null, 2));
        _opsStamp("Copied recent ops logs JSON");
      } catch (e) {
        _opsStamp(String(e && e.message ? e.message : e));
      }
    });

    btnCrit.addEventListener("click", async function () {
      try {
        var r = await fetch("/api/ops/critical-bundle", { cache: "no-store" });
        if (!r.ok) throw new Error("HTTP " + r.status);
        var j = await r.json();
        await navigator.clipboard.writeText(JSON.stringify(j, null, 2));
        _opsStamp("Copied critical ops bundle JSON");
      } catch (e) {
        _opsStamp(String(e && e.message ? e.message : e));
      }
    });

    btnCsv.addEventListener("click", function () {
      _opsStamp("Starting CSV download…");
      window.location.href = "/api/ops/logs/export.csv?limit=500";
      setTimeout(function () { _opsStamp("CSV download started"); }, 300);
    });

    btnXlsx.addEventListener("click", function () {
      _opsStamp("Starting daily report download…");
      window.location.href = "/api/ops/daily-report.xlsx";
      setTimeout(function () { _opsStamp("XLSX download started"); }, 300);
    });
  }

  // ── AI Console ──────────────────────────────────────────────────────────

  var _aiLoaded = false;

  function loadAiStatus() {
    var el = function (id) { return document.getElementById(id); };
    var foot = document.getElementById("aiStatusFootnote");
    if (foot) foot.textContent = "Loading Momo status from /api/ai/status…";
    fetch("/api/ai/status", { cache: "no-store", headers: _authHeaders() })
      .then(function (r) {
        if (!r.ok) {
          throw new Error("/api/ai/status HTTP " + r.status);
        }
        return r.json();
      })
      .then(function (d) {
        var momo = d.momo_status || {};
        var auth = d.momo_authority_status || {};
        var mem = d.memory_state_summary || {};
        var assistant = safeText(d.assistant_name || momo.name, "Momo");
        var provider = safeText(d.provider, "");
        if (!provider || provider === "disabled_missing_key") {
          provider = d.enabled ? "Momo (deterministic)" : "Momo (no Gemini key)";
        }
        if (el("aiProvider")) el("aiProvider").textContent = assistant + " · " + provider;
        if (el("aiModel")) {
          el("aiModel").textContent = d.model
            ? safeText(d.model)
            : (provider.indexOf("Gemini") >= 0 || provider.indexOf("gemini") >= 0 ? "—" : "rules + memory");
        }
        if (el("aiEnabled")) {
          el("aiEnabled").textContent = d.schema_initialized
            ? (d.enabled ? "observer on" : "observer off")
            : "schema pending";
        }
        if (el("aiNotesCount")) {
          el("aiNotesCount").textContent = d.notes_count != null
            ? String(d.notes_count)
            : (mem.useful_memory_count != null ? String(mem.useful_memory_count) : "—");
        }
        if (el("aiPatternsCount")) el("aiPatternsCount").textContent = d.patterns_count != null ? String(d.patterns_count) : "—";
        if (el("aiSkillsCount")) el("aiSkillsCount").textContent = d.skills_count != null ? String(d.skills_count) : "—";
        if (el("aiLastRun")) el("aiLastRun").textContent = safeText(d.last_run_at || mem.last_memory_note_at, "—");
        if (foot) {
          foot.innerHTML =
            "Assistant: <strong>" + esc(assistant) + "</strong> · authority: " +
            esc(safeText(momo.authority_level || auth.authority_level, "backtester")) +
            "<br>can_submit_orders: <strong style=\"color:var(--bad);\">false</strong> · " +
            "can_change_config: <strong style=\"color:var(--bad);\">false</strong> · " +
            "allowed_to_execute: <strong style=\"color:var(--bad);\">false</strong>";
          if (momo.can_touch_crypto_execution_loop === false) {
            foot.innerHTML += "<br>Crypto execution: deterministic math only (Momo not in execution loop).";
          }
        }
      })
      .catch(function (e) {
        if (el("aiProvider")) el("aiProvider").textContent = "Momo — status unavailable";
        if (el("aiModel")) el("aiModel").textContent = "—";
        if (el("aiEnabled")) el("aiEnabled").textContent = "error";
        if (foot) {
          foot.textContent = "Could not load /api/ai/status: " + safeText(e && e.message, String(e)) +
            " — check dashboard logs and AI_MEMORY_DB_PATH.";
        }
      });
  }

  function loadAiNotes() {
    fetch("/api/ai/observer/latest?limit=30").then(function (r) { return r.json(); }).then(function (d) {
      var tbody = document.querySelector("#tblAiNotes tbody");
      if (!tbody) return;
      tbody.innerHTML = "";
      (d.notes || []).forEach(function (n) {
        var sev = esc(n.severity || "");
        var sevColor = sev === "critical" ? "var(--bad)" : sev === "warning" ? "#fbbf24" : "var(--muted)";
        tbody.innerHTML += "<tr>" +
          "<td>" + esc((n.created_at || "").slice(0, 19)) + "</td>" +
          "<td style='color:" + sevColor + ";font-weight:600'>" + sev + "</td>" +
          "<td>" + esc(n.category || "") + "</td>" +
          "<td>" + esc(n.symbol || "") + "</td>" +
          "<td style='max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap'>" + esc(n.finding || "") + "</td>" +
          "<td style='max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap'>" + esc(n.suggested_action || "") + "</td>" +
          "<td>" + (n.confidence != null ? Number(n.confidence).toFixed(2) : "—") + "</td>" +
          "</tr>";
      });
    }).catch(function () {});
  }

  function loadAiPatterns() {
    fetch("/api/ai/patterns").then(function (r) { return r.json(); }).then(function (d) {
      var tbody = document.querySelector("#tblAiPatterns tbody");
      if (!tbody) return;
      tbody.innerHTML = "";
      (d.patterns || []).forEach(function (p) {
        var syms = "";
        try { syms = JSON.parse(p.symbols_seen_json || "[]").join(", "); } catch(e) { syms = p.symbols_seen_json || ""; }
        tbody.innerHTML += "<tr>" +
          "<td>" + esc(p.pattern_name || p.pattern_key || "") + "</td>" +
          "<td>" + (p.seen_count || 0) + "</td>" +
          "<td>" + esc(syms) + "</td>" +
          "<td style='max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap'>" + esc(p.risk_summary || "") + "</td>" +
          "<td>" + (p.confidence != null ? Number(p.confidence).toFixed(2) : "—") + "</td>" +
          "</tr>";
      });
    }).catch(function () {});
  }

  function loadAiSkills() {
    fetch("/api/ai/skills").then(function (r) { return r.json(); }).then(function (d) {
      var tbody = document.querySelector("#tblAiSkills tbody");
      if (!tbody) return;
      tbody.innerHTML = "";
      (d.skills || []).forEach(function (s) {
        var stColor = s.status === "rejected" ? "var(--bad)" : s.status === "approved_observe_only" ? "var(--good)" : "var(--muted)";
        tbody.innerHTML += "<tr>" +
          "<td>" + esc(s.skill_name || s.skill_key || "") + "</td>" +
          "<td style='max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap'>" + esc(s.purpose || "") + "</td>" +
          "<td style='color:" + stColor + "'>" + esc(s.status || "proposed") + "</td>" +
          "<td>" + (s.confidence != null ? Number(s.confidence).toFixed(2) : "—") + "</td>" +
          "<td style='color:var(--bad);font-weight:600'>false</td>" +
          "</tr>";
      });
    }).catch(function () {});
  }

  var _mcCache = null;
  var _mcPollTimer = null;

  function _mcTopline(d) {
    var t = d.topline || {};
    var ac = d.account || {};
    var elEq = document.getElementById("mcTopEq");
    var elCash = document.getElementById("mcTopCash");
    var elBp = document.getElementById("mcTopBp");
    var elMode = document.getElementById("mcTopMode");
    var elMis = document.getElementById("mcTopMission");
    var elCr = document.getElementById("mcTopCrypto");
    if (elEq) elEq.textContent = safeFmtMoney(t.equity != null ? t.equity : ac.equity);
    if (elCash) elCash.textContent = safeFmtMoney(t.cash != null ? t.cash : ac.cash);
    if (elBp) elBp.textContent = safeFmtMoney(t.buying_power != null ? t.buying_power : ac.buying_power);
    if (elMode) elMode.textContent = safeText(t.mode || ac.mode, "paper");
    if (elMis) elMis.textContent = safeText(t.mission_mode || (d.mission || {}).mission_mode, "—");
    if (elCr) {
      var elig = d.crypto_eligibility || {};
      if (elig.can_trade_crypto === true) elCr.textContent = "Ready";
      else if (elig.can_trade_crypto === false) elCr.textContent = "Blocked";
      else elCr.textContent = safeText(t.crypto_push_status, "—");
    }
  }

  function _mcProgressStart(label) {
    var wrap = document.getElementById("mcProgress");
    var bar = document.getElementById("mcProgressBar");
    var st = document.getElementById("mcStatus");
    if (wrap) wrap.style.display = "block";
    if (bar) bar.style.width = "15%";
    if (st) st.textContent = label || "Working…";
    _mcProgressT0 = Date.now();
  }

  function _mcProgressDone(msg, ok) {
    var wrap = document.getElementById("mcProgress");
    var bar = document.getElementById("mcProgressBar");
    var st = document.getElementById("mcStatus");
    if (bar) bar.style.width = ok === false ? "100%" : "100%";
    if (st) st.textContent = msg || "Done.";
    setTimeout(function () {
      if (wrap) wrap.style.display = "none";
      if (bar) bar.style.width = "0%";
    }, ok === false ? 4000 : 1200);
  }

  var _mcProgressT0 = 0;

  function renderMissionControl(d) {
    _mcCache = d;
    var ts = new Date().toLocaleString();
    _mcTopline(d);
    var dev = document.getElementById("mcDevJson");
    if (dev) {
      try {
        dev.textContent = JSON.stringify({
          broker_account_transition_status: d.broker_account_transition_status,
          topline: d.topline
        }, null, 2);
      } catch (eJ) {
        dev.textContent = "{}";
      }
    }
    var cards = [
      {
        id: "mcAccount",
        tone: "ok",
        render: function () {
          var ac = d.account || {};
          return (
            "Account is in " + safeText(ac.mode, "paper") + " mode with equity " + safeFmtMoney(ac.equity) +
            ", cash " + safeFmtMoney(ac.cash) + ", and buying power " + safeFmtMoney(ac.buying_power) + "."
          );
        }
      },
      {
        id: "mcMission",
        tone: "",
        render: function () {
          var mi = d.mission || {};
          var rg = d.recovery_gate || {};
          var mode = safeText(mi.mission_mode, "normal");
          var sess = safeText(mi.session_mode, "—");
          var lines = ["Mission mode is " + mode + ". Session: " + sess + "."];
          if (rg.recovery_active) {
            lines.push("Recovery is active: " + safeText(rg.recovery_reason, "see logs"));
          } else {
            lines.push("Recovery is not active — normal paper mode.");
          }
          if (rg.block_new_buys) {
            lines.push("New buys blocked: " + safeText(rg.block_new_buys_reason, "see capital card"));
          } else {
            lines.push("New buys are allowed (subject to capital rules).");
          }
          return lines.join("\n");
        }
      },
      {
        id: "mcCapital",
        tone: function () {
          var cp = d.capital_protection || {};
          var bp = (d.account || {}).buying_power;
          if (bp != null && Number(bp) <= 0.01) return "warn";
          return "";
        },
        render: function () {
          var cp = d.capital_protection || {};
          var diag = cp.buying_power_diagnostic || {};
          var human = diag.headline || cp.human_summary || cp.why_buying_power_low;
          if (human) return String(human);
          var pr = cp.dynamic_profile || {};
          return (
            "Capital profile " + safeText(pr.profile, "—") + ". Reserve " + safeFmtPct(pr.hard_cash_reserve_pct) +
            ". Available for new stock trades: " + safeFmtMoney(pr.available_for_stock) + "."
          );
        }
      },
      {
        id: "mcBroker",
        tone: function () {
          var tr = d.broker_account_transition_status || {};
          return tr.runtime_reset_recommended ? "warn" : (tr.aligned_with_broker ? "ok" : "");
        },
        render: function () {
          var tr = d.broker_account_transition_status || {};
          var lines = [safeText(tr.headline, "—")];
          if (tr.detection_reasons && tr.detection_reasons.length) {
            lines.push("Reasons: " + tr.detection_reasons.join(", "));
          }
          lines.push(
            "Confidence: " + safeText(tr.confidence, "low") +
            ". Broker positions: " + safeText(tr.broker_positions_count, "—") +
            ", runtime: " + safeText(tr.runtime_positions_count, "—") +
            ", mismatches: " + safeText(tr.broker_local_mismatch_count, "0") + "."
          );
          if (tr.runtime_reset_recommended) {
            lines.push("Runtime reset is recommended if issues persist after reviewing positions.");
          }
          return lines.join("\n");
        }
      },
      {
        id: "mcPositions",
        tone: "",
        render: function () {
          var pos = d.positions || {};
          var n = pos.count != null ? pos.count : (pos.open ? pos.open.length : 0);
          return n === 0
            ? "No open positions in runtime state."
            : "You have " + String(n) + " open position(s) in runtime state.";
        }
      },
      {
        id: "mcCrypto",
        tone: "",
        render: function () {
          var cr = d.crypto_night || {};
          var pol = cr.crypto_execution_policy || {};
          var push = cr.push_possible === true ? "may run" : (cr.push_possible === false ? "is blocked" : "status unknown");
          var parts = [];
          if (cr.crypto_block_headline) {
            parts.push(String(cr.crypto_block_headline));
          } else {
            parts.push(
              "Crypto night push " + push + ". " +
              safeText(cr.blocked_reason || pol.blocked_reason, "No block reason listed.")
            );
          }
          parts.push("Execution uses deterministic math only (Momo not in the loop).");
          var elig = d.crypto_eligibility || {};
          if (elig.latest_human_reason) {
            parts.push("Eligibility: " + (elig.can_trade_crypto ? "YES — " : "NO — ") + elig.latest_human_reason);
            if (elig.usable_crypto_buying_power != null) {
              parts.push("Usable crypto BP: " + safeFmtMoney(elig.usable_crypto_buying_power) + " (source: " + safeText(elig.usable_source, "?") + ").");
            }
          }
          var evts = Array.isArray(cr.latest_crypto_attempts) ? cr.latest_crypto_attempts
            : (Array.isArray(cr.latest_push_pull_events) ? cr.latest_push_pull_events : []);
          if (evts.length) {
            parts.push("Recent crypto attempts:");
            evts.slice(0, 5).forEach(function (e) {
              var hr = e.human_reason || e.reason_code || "?";
              parts.push(
                "  " + esc(e.symbol || "?") + " · " + esc(e.action || e.side || "") +
                " · " + esc(e.decision || "?") + " · " + esc(hr) +
                (e.created_at ? " · " + esc(String(e.created_at).slice(0, 19)) : "")
              );
            });
          } else {
            parts.push("No recent crypto execution decisions recorded.");
          }
          return parts.join("\n");
        }
      },
      {
        id: "mcMomo",
        tone: "",
        render: function () {
          var ms = d.momo_summary || {};
          var saw = Array.isArray(ms.saw) ? ms.saw.join(" ") : "";
          var att = Array.isArray(ms.attention) ? ms.attention.join(" ") : "";
          var learned = Array.isArray(ms.learned) ? ms.learned.join(" ") : "";
          var parts = [];
          if (saw) parts.push("Recently: " + saw);
          if (learned) parts.push("Learned: " + learned);
          if (att) parts.push("Attention: " + att);
          return parts.length ? parts.join("\n") : "Momo has no new summary items this cycle.";
        }
      },
      {
        id: "mcOps",
        tone: "",
        render: function () {
          var oh = d.ops_health || {};
          var tg = d.telegram_status || {};
          var parts = [
            "CPU " + (oh.process_cpu_pct != null ? safeFmtPct(oh.process_cpu_pct) : "—") +
            ", memory " + (oh.system_memory_pct != null ? safeFmtPct(oh.system_memory_pct) : "—") +
            ", disk " + (oh.disk_used_pct != null ? safeFmtPct(oh.disk_used_pct) : "—") + "."
          ];
          if (Object.keys(tg).length) {
            var tgLine = tg.status_message || (
              (tg.enabled ? "Telegram enabled" : "Telegram disabled") +
              (tg.polling_active ? ", polling active" : ", not polling")
            );
            if (tg.missing_config && tg.missing_config.length) {
              tgLine += " Missing: " + tg.missing_config.join(", ");
            }
            if (tg.last_error) tgLine += " Last error: " + esc(String(tg.last_error).slice(0, 100));
            parts.push(tgLine);
          }
          return parts.join("\n");
        }
      }
    ];
    cards.forEach(function (c) {
      try {
        var tone = typeof c.tone === "function" ? c.tone() : c.tone;
        setMcCard(c.id, c.render(), ts, tone);
      } catch (cardErr) {
        setMcCard(c.id, "Card error: " + safeText(cardErr && cardErr.message, "see console"), ts, "bad");
      }
    });
  }

  function _updateMcPerf(d, refreshing) {
    var perf = document.getElementById("mcPerfStatus");
    if (!perf) return;
    var parts = [];
    if (refreshing) parts.push("Refreshing…");
    if (d && d.stale_warning) parts.push(d.stale_warning);
    if (d && d.cache_age_seconds != null) parts.push("Cache " + d.cache_age_seconds + "s");
    if (d && d.backend_duration_ms != null) parts.push("API " + d.backend_duration_ms + "ms");
    parts.push("GPT bundle: not loaded");
    parts.push("Momo: on demand");
    perf.textContent = parts.join(" · ");
  }

  function loadMissionTab(force) {
    var st = document.getElementById("mcStatus");
    if (_mcCache) {
      try { renderMissionControl(_mcCache); } catch (e0) {}
      _updateMcPerf(_mcCache, true);
    }
    if (force) _mcProgressStart("Refreshing Mission Control…");
    if (st && !force) st.textContent = _mcCache ? "Refreshing quietly…" : "Loading Mission Control…";
    var url = "/api/mission-control/summary" + (force ? "?force=1" : "");
    fetch(url, { cache: "no-store", headers: _authHeaders() })
      .then(function (r) {
        if (!r.ok) {
          return r.text().then(function (txt) {
            var msg = "/api/mission-control/summary HTTP " + r.status;
            try {
              var errBody = JSON.parse(txt);
              if (errBody.error) msg += ": " + errBody.error;
            } catch (eJ) {
              if (txt) msg += ": " + txt.slice(0, 120);
            }
            throw new Error(msg);
          });
        }
        return r.json();
      })
      .then(function (d) {
        if (!d || d.ok === false) {
          if (st) st.textContent = "Mission Control: " + safeText(d && d.error, "unavailable");
          if (d) renderMissionControl(d);
          return;
        }
        if (force) {
          _mcProgressDone((d.stale_warning ? d.stale_warning + " · " : "") + "Updated " + new Date().toLocaleString(), true);
        } else if (st) {
          st.textContent = (d.stale_warning ? d.stale_warning + " · " : "") + "Updated " + new Date().toLocaleString();
        }
        _updateMcPerf(d, false);
        try {
          renderMissionControl(d);
        } catch (renderErr) {
          if (st) st.textContent = "Render error: " + safeText(renderErr && renderErr.message, String(renderErr));
        }
      })
      .catch(function (e) {
        if (force) _mcProgressDone(safeText(e && e.message, String(e)), false);
        else if (st) st.textContent = safeText(e && e.message, String(e));
        if (!_mcCache) {
          ["mcAccount", "mcMission", "mcCapital", "mcBroker", "mcPositions", "mcCrypto", "mcMomo", "mcOps"].forEach(function (id) {
            setMcCard(id, "—");
          });
        }
      });
  }

  function scheduleMissionGraphLoad() {
    setTimeout(function () {
      if (document.getElementById("panel-overview") && document.getElementById("panel-overview").classList.contains("active")) {
        _loadEquityRange(_eqCurrentRange || "1D");
      }
    }, 50);
  }

  function deepRefreshMission() {
    _mcProgressStart("Deep refresh (live broker, max ~3s)…");
    Promise.allSettled([
      fetch("/api/mission-control/summary?force=1&live=1", { cache: "no-store", headers: _authHeaders() })
        .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); }),
      fetch("/api/ops/logs?limit=30", { cache: "no-store", headers: _authHeaders() }).then(function (r) { return r.json(); })
    ]).then(function (results) {
      if (results[0].status === "fulfilled" && results[0].value) {
        try { renderMissionControl(results[0].value); } catch (e1) {}
        _mcProgressDone("Deep refresh complete.", true);
      } else {
        _mcProgressDone("Deep refresh failed.", false);
      }
    });
  }

  function setMcCard(id, text, ts, tone) {
    var el = document.getElementById(id);
    if (!el) return;
    el.classList.remove("mc-ok", "mc-warn", "mc-bad");
    if (tone === "ok") el.classList.add("mc-ok");
    if (tone === "warn") el.classList.add("mc-warn");
    if (tone === "bad") el.classList.add("mc-bad");
    var tsEl = el.querySelector(".mc-ts");
    if (tsEl && ts) tsEl.textContent = "Updated " + ts;
    var body = el.querySelector(".mc-body");
    if (body) body.textContent = text;
  }

  function wireMcMomoAsk() {
    var askBtn = document.getElementById("btnMcAskMomo");
    var input = document.getElementById("mcMomoInput");
    var out = document.getElementById("mcMomoAnswer");
    function sendQ(q) {
      if (!q) return;
      if (out) out.textContent = "Asking Momo…";
      fetch("/api/momo/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          question: q,
          include: {
            mission_control: true,
            broker_diagnostic: true,
            activity_export: true,
            capital_allocator: true,
            momo_memory: true,
            ops_logs: false
          }
        })
      })
        .then(function (r) {
          if (!r.ok) throw new Error("/api/momo/ask HTTP " + r.status);
          return r.json();
        })
        .then(function (d) {
          if (out) out.textContent = d.answer || safeText(d.error, "No answer");
        })
        .catch(function (e) {
          if (out) out.textContent = safeText(e && e.message, String(e));
        });
    }
    if (askBtn) askBtn.addEventListener("click", function () {
      sendQ(input ? input.value.trim() : "");
    });
    document.querySelectorAll(".mc-quick").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var q = btn.getAttribute("data-q") || "";
        if (input) input.value = q;
        sendQ(q);
      });
    });
  }

  function wireMissionActions() {
    function postReset(includeLogs) {
      var conf = window.prompt("Type RESET RUNTIME to confirm:");
      if (conf !== "RESET RUNTIME") return;
      fetch("/api/ops/reset-runtime", {
        method: "POST",
        headers: volHeaders(true),
        body: JSON.stringify({ confirm: "RESET RUNTIME", include_cycle_logs: includeLogs })
      }).then(function (r) { return r.json(); }).then(function (d) {
        var el = document.getElementById("mcResetStatus");
        if (el) el.textContent = JSON.stringify(d, null, 2);
        loadMissionTab();
      });
    }
    function backup() {
      fetch("/api/ops/backup-dbs", { method: "POST", headers: volHeaders(true) })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          var el = document.getElementById("mcResetStatus");
          if (el) el.textContent = "Backup: " + (d.backup_path || "ok");
        });
    }
    var b1 = document.getElementById("btnMcBackup");
    var b2 = document.getElementById("btnMcResetRuntime");
    var b3 = document.getElementById("btnMcResetRuntimeLogs");
    var b4 = document.getElementById("btnMcResetMomoMemory");
    if (b1) b1.addEventListener("click", backup);
    if (b2) b2.addEventListener("click", function () { postReset(false); });
    if (b3) b3.addEventListener("click", function () { postReset(true); });
    if (b4) b4.addEventListener("click", function () {
      if (!window.confirm("Delete ALL Momo memory? Type RESET MOMO MEMORY in next prompt.")) return;
      var c = window.prompt("Type RESET MOMO MEMORY:");
      if (c !== "RESET MOMO MEMORY") return;
      fetch("/api/ops/reset-momo-memory", {
        method: "POST",
        headers: volHeaders(true),
        body: JSON.stringify({ confirm: "RESET MOMO MEMORY" })
      }).then(function (r) { return r.json(); }).then(function (d) {
        var el = document.getElementById("mcResetStatus");
        if (el) el.textContent = JSON.stringify(d, null, 2);
      });
    });
    ["btnOpsBackup", "btnOpsResetRuntime", "btnFilesBackup", "btnFilesResetRuntime"].forEach(function (id) {
      var btn = document.getElementById(id);
      if (!btn) return;
      if (id.indexOf("Backup") >= 0) btn.addEventListener("click", backup);
      else btn.addEventListener("click", function () { postReset(false); });
    });
  }

  function wireGptAnalyze() {
    function fetchBundle() {
      return fetch("/api/ops/gpt-analyze-bundle", { cache: "no-store", headers: _authHeaders() }).then(function (r) {
        if (!r.ok) {
          throw new Error("/api/ops/gpt-analyze-bundle HTTP " + r.status);
        }
        return r.json();
      });
    }
    function fetchBundleText() {
      return fetch("/api/ops/gpt-analyze-bundle.txt", { cache: "no-store", headers: _authHeaders() }).then(function (r) {
        if (!r.ok) throw new Error("/api/ops/gpt-analyze-bundle.txt HTTP " + r.status);
        return r.text();
      });
    }
    var copy = document.getElementById("btnCopyGPTAnalyzeBundle");
    var dl = document.getElementById("btnDownloadGPTAnalyzeBundle");
    var tg = document.getElementById("btnSendGPTAnalyzeBundleTelegram");
    var main = document.getElementById("btnGPTAnalyzeLogs");
    var prev = document.getElementById("mcGptPreview");
    var rail = document.getElementById("btnExportRailwayEnv");
    var refresh = document.getElementById("btnMcRefresh");
    if (refresh) refresh.addEventListener("click", function () { loadMissionTab(true); });
    var deep = document.getElementById("btnMcDeepRefresh");
    if (deep) deep.addEventListener("click", deepRefreshMission);
    if (rail) rail.addEventListener("click", function () {
      fetch("/api/config/railway-env-template", { cache: "no-store" })
        .then(function (r) { return r.text(); })
        .then(function (t) { return navigator.clipboard.writeText(t); })
        .then(function () {
          var st = document.getElementById("mcStatus");
          if (st) st.textContent = "Railway env template copied (essential vars only).";
        })
        .catch(function (e) {
          var st = document.getElementById("mcStatus");
          if (st) st.textContent = safeText(e && e.message, String(e));
        });
    });
    if (main) main.addEventListener("click", function () {
      _mcProgressStart("Building GPT analyze bundle…");
      fetchBundle().then(function (d) {
        if (prev) {
          prev.style.display = "block";
          prev.textContent = JSON.stringify({
            generated_at: d.generated_at,
            keys: Object.keys(d).slice(0, 20),
            config_summary: !!(d.config_summary),
            mission_control_summary: !!(d.mission_control_summary)
          }, null, 2);
        }
        _mcProgressDone("GPT bundle ready — use Copy or Download.", true);
      }).catch(function (e) {
        _mcProgressDone(safeText(e && e.message, String(e)), false);
      });
    });
    if (copy) copy.addEventListener("click", function () {
      _mcProgressStart("Loading GPT bundle for copy…");
      fetchBundle().then(function (d) {
        return _copyWithFallback(JSON.stringify(d, null, 2), document.getElementById("mcStatus"), "Copied — GPT bundle JSON.");
      }).then(function () { _mcProgressDone("Copied — GPT bundle JSON.", true); })
        .catch(function (e) { _mcProgressDone(safeText(e && e.message, String(e)), false); });
    });
    if (dl) dl.addEventListener("click", function () {
      _mcProgressStart("Downloading GPT bundle JSON…");
      fetchBundle().then(function (d) {
        _downloadJson(d, "gpt_analyze_" + _timestamp() + ".json");
        _mcProgressDone("GPT bundle JSON downloaded.", true);
      }).catch(function (e) {
        _mcProgressDone(safeText(e && e.message, String(e)), false);
      });
    });
    var dlTxt = document.getElementById("btnDownloadGPTAnalyzeBundleTxt");
    if (dlTxt) dlTxt.addEventListener("click", function () {
      var st = document.getElementById("mcStatus");
      if (st) st.textContent = "Downloading GPT bundle TXT…";
      fetchBundleText().then(function (t) {
        _downloadBlob(new Blob([t], { type: "text/plain" }), "gpt_analyze_" + _timestamp() + ".txt");
        if (st) st.textContent = "GPT bundle TXT downloaded.";
      }).catch(function (e) {
        if (st) st.textContent = safeText(e && e.message, String(e));
      });
    });
    var btnCopyLogs = document.getElementById("btnCopyLogsBundle");
    var btnDlJson = document.getElementById("btnDownloadLogsJson");
    var btnDlTxt = document.getElementById("btnDownloadLogsTxt");
    var btnDlCsv = document.getElementById("btnDownloadLogsCsv");
    if (btnCopyLogs) btnCopyLogs.addEventListener("click", function () {
      var st = document.getElementById("mcStatus");
      if (st) st.textContent = "Loading ops logs…";
      fetch("/api/ops/logs/export.json?limit=500", { headers: _authHeaders(), cache: "no-store" })
        .then(function (r) {
          if (!r.ok) throw new Error("logs export.json HTTP " + r.status);
          return r.json();
        })
        .then(function (d) {
          return _copyWithFallback(JSON.stringify(d, null, 2), st, "Ops logs JSON copied.");
        })
        .catch(function (e) {
          if (st) st.textContent = safeText(e && e.message, String(e));
        });
    });
    function _dlLogsUrl(path, label) {
      return function () {
        var st = document.getElementById("mcStatus");
        if (st) st.textContent = "Starting " + label + "…";
        window.location.href = path + "?limit=500";
        setTimeout(function () { if (st) st.textContent = label + " download started."; }, 400);
      };
    }
    if (btnDlJson) btnDlJson.addEventListener("click", _dlLogsUrl("/api/ops/logs/export.json", "Logs JSON"));
    if (btnDlTxt) btnDlTxt.addEventListener("click", _dlLogsUrl("/api/ops/logs/export.txt", "Logs TXT"));
    if (btnDlCsv) btnDlCsv.addEventListener("click", _dlLogsUrl("/api/ops/logs/export.csv", "Logs CSV"));
    if (tg) tg.addEventListener("click", function () {
      var st = document.getElementById("mcStatus");
      if (st) st.textContent = "Sending bundle to Telegram…";
      fetch("/api/ops/gpt-analyze-bundle/send-telegram", {
        method: "POST",
        headers: volHeaders(true)
      })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (!d.ok && d.errors) {
            if (st) st.textContent = d.errors.join("; ");
            return;
          }
          if (st) {
          if (d.sent) st.textContent = "Sent to Telegram (" + (d.chunks_sent || 0) + " chunks" + (d.truncated ? ", truncated" : "") + ").";
          else st.textContent = d.reason || (d.errors && d.errors.join("; ")) || "Telegram send failed.";
        }
        })
        .catch(function (e) {
          if (st) st.textContent = safeText(e && e.message, String(e));
        });
    });
    var tgTest = document.getElementById("btnTelegramTestSend");
    if (tgTest) tgTest.addEventListener("click", function () {
      var st = document.getElementById("mcStatus");
      if (st) st.textContent = "Sending Telegram test message…";
      tgTest.disabled = true;
      fetch("/api/telegram/test-send", {
        method: "POST",
        headers: volHeaders(true)
      })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (st) {
            if (d.sent) {
              st.textContent = "✅ Telegram test sent successfully.";
            } else {
              var reason = d.reason || (d.config_errors && d.config_errors.join("; ")) || "Send failed.";
              st.textContent = "❌ Telegram test failed: " + reason;
            }
          }
        })
        .catch(function (e) {
          if (st) st.textContent = "Telegram test error: " + safeText(e && e.message, String(e));
        })
        .finally(function () { tgTest.disabled = false; });
    });
  }

  function wireAiChat() {
    var btn = document.getElementById("aiChatSend");
    if (!btn) return;
    btn.addEventListener("click", function () {
      var input = document.getElementById("aiChatInput");
      var msg = (input ? input.value : "").trim();
      if (!msg) return;
      btn.disabled = true;
      btn.textContent = "Thinking...";
      var body = {
        message: msg,
        include_activity_export: document.getElementById("aiIncExport") ? document.getElementById("aiIncExport").checked : true,
        include_broker_diagnostic: document.getElementById("aiIncBroker") ? document.getElementById("aiIncBroker").checked : false,
        include_memory: document.getElementById("aiIncMemory") ? document.getElementById("aiIncMemory").checked : true
      };
      fetch("/api/ai/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
      }).then(function (r) { return r.json();       }).then(function (d) {
        _lastMomoAnswer = d;
        _lastJarvisAnswer = d;
        var wrap = document.getElementById("aiChatResult");
        if (wrap) wrap.style.display = "block";
        var prov = document.getElementById("aiChatProvider");
        if (prov) prov.textContent = "(" + (d.provider || "unknown") + ", conf=" + (d.confidence != null ? Number(d.confidence).toFixed(2) : "—") + ")";
        var ans = document.getElementById("aiChatAnswer");
        if (ans) ans.textContent = d.answer || "No answer.";
        var ev = document.getElementById("aiChatEvidence");
        if (ev) ev.textContent = d.evidence_used && d.evidence_used.length ? "Evidence: " + d.evidence_used.join(", ") : "";
        var acts = document.getElementById("aiChatActions");
        if (acts) {
          var actions = d.suggested_operator_actions || [];
          acts.innerHTML = actions.length ? "<strong>Suggested actions:</strong> " + actions.map(esc).join("; ") : "";
        }
      }).catch(function (e) {
        var ans = document.getElementById("aiChatAnswer");
        if (ans) ans.textContent = "Error: " + e;
        var wrap = document.getElementById("aiChatResult");
        if (wrap) wrap.style.display = "block";
      }).finally(function () {
        btn.disabled = false;
        btn.textContent = "Ask Momo";
      });
    });
  }

  var _lastMomoAnswer = null;
  var _lastJarvisAnswer = null;

  function _aiStatus(msg) {
    var el = document.getElementById("aiMemoryCopyStatus");
    if (el) el.textContent = msg;
  }

  function _downloadJson(data, filename) {
    var blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(a.href);
  }

  function _timestamp() {
    var d = new Date();
    return d.getFullYear() +
      String(d.getMonth() + 1).padStart(2, "0") +
      String(d.getDate()).padStart(2, "0") + "_" +
      String(d.getHours()).padStart(2, "0") +
      String(d.getMinutes()).padStart(2, "0") +
      String(d.getSeconds()).padStart(2, "0");
  }

  function wireAiMemoryButtons() {
    var btnCopy = document.getElementById("btnCopyAiMemories");
    var btnBundle = document.getElementById("btnCopyFullAiBundle");
    var btnDlMem = document.getElementById("btnDownloadAiMemories");
    var btnDlBundle = document.getElementById("btnDownloadFullAiBundle");

    if (btnCopy) btnCopy.addEventListener("click", function () {
      _aiStatus("Fetching AI memories...");
      fetch("/api/ai/memories/export").then(function (r) { return r.json(); }).then(function (d) {
        return navigator.clipboard.writeText(JSON.stringify(d, null, 2)).then(function () {
          _aiStatus("AI memories copied.");
        });
      }).catch(function (e) { _aiStatus("Copy failed: " + (e.message || e)); });
    });

    if (btnBundle) btnBundle.addEventListener("click", function () {
      _aiStatus("Fetching full AI bundle...");
      fetch("/api/ai/bundle/export").then(function (r) { return r.json(); }).then(function (d) {
        if (_lastMomoAnswer) {
          d.momo_last_answer = _lastMomoAnswer;
          d.jarvis_last_answer = _lastMomoAnswer;
        }
        return navigator.clipboard.writeText(JSON.stringify(d, null, 2)).then(function () {
          _aiStatus("Full AI bundle copied.");
        });
      }).catch(function (e) { _aiStatus("Copy failed: " + (e.message || e)); });
    });

    if (btnDlMem) btnDlMem.addEventListener("click", function () {
      _aiStatus("Preparing download...");
      fetch("/api/ai/memories/export").then(function (r) { return r.json(); }).then(function (d) {
        _downloadJson(d, "ai_memories_" + _timestamp() + ".json");
        _aiStatus("Download ready.");
      }).catch(function (e) { _aiStatus("Copy failed: " + (e.message || e)); });
    });

    if (btnDlBundle) btnDlBundle.addEventListener("click", function () {
      _aiStatus("Preparing download...");
      fetch("/api/ai/bundle/export").then(function (r) { return r.json(); }).then(function (d) {
        if (_lastMomoAnswer) {
          d.momo_last_answer = _lastMomoAnswer;
          d.jarvis_last_answer = _lastMomoAnswer;
        }
        _downloadJson(d, "quantbot_ai_bundle_" + _timestamp() + ".json");
        _aiStatus("Download ready.");
      }).catch(function (e) { _aiStatus("Copy failed: " + (e.message || e)); });
    });
  }

  function loadAiTab() {
    loadAiStatus();
    if (!_aiLoaded) {
      _aiLoaded = true;
      loadAiNotes();
      loadAiPatterns();
      loadAiSkills();
    }
  }

  function loadConfigEditor() {
    var root = document.getElementById("configEditorRoot");
    var status = document.getElementById("configEditorStatus");
    if (!root) return;
    root.innerHTML = "<p class='empty-hint'>Loading config schema…</p>";
    if (status) status.textContent = "Fetching /api/config/schema and /api/config/summary…";
    fetch("/api/config/schema", { cache: "no-store", headers: _authHeaders() })
      .then(function (r) {
        if (!r.ok) throw new Error("/api/config/schema HTTP " + r.status);
        return r.json();
      })
      .then(function (schema) {
        return fetch("/api/config/summary", { cache: "no-store", headers: _authHeaders() })
          .then(function (r2) {
            if (!r2.ok) throw new Error("/api/config/summary HTTP " + r2.status);
            return r2.json();
          })
          .then(function (summary) { return { schema: schema, summary: summary }; });
      })
      .then(function (data) {
        var items = data.schema.items || [];
        var vals = data.summary.values || {};
        var sources = data.summary.sources || {};
        var byCat = {};
        items.forEach(function (it) {
          var c = it.category || "Other";
          if (!byCat[c]) byCat[c] = [];
          byCat[c].push(it);
        });
        root.innerHTML = "";
        Object.keys(byCat).sort().forEach(function (cat) {
          var h = document.createElement("div");
          h.className = "config-cat";
          h.textContent = cat;
          root.appendChild(h);
          byCat[cat].forEach(function (it) {
            var row = document.createElement("div");
            row.className = "config-row" + (it.dangerous ? " danger" : "");
            var cur = vals[it.key] !== undefined ? vals[it.key] : it.default;
            var src = sources[it.key] || "default";
            var input;
            if (it.type === "bool") {
              input = document.createElement("input");
              input.type = "checkbox";
              input.checked = !!cur;
            } else {
              input = document.createElement("input");
              input.type = it.type === "float" || it.type === "int" ? "number" : "text";
              input.value = cur != null ? String(cur) : "";
              if (it.allowed_values) input.setAttribute("list", "dl-" + it.key);
            }
            input.disabled = !it.editable;
            input.setAttribute("data-config-key", it.key);
            input.setAttribute("data-config-type", it.type);
            var lbl = document.createElement("label");
            lbl.innerHTML = "<strong>" + esc(it.key) + "</strong><div class='config-meta'>" + esc(it.description) + "</div>" +
              (it.dangerous ? "<div class='config-warn'>Dangerous</div>" : "") +
              (it.requires_restart ? "<div class='config-warn'>Restart required</div>" : "") +
              "<div class='config-meta'>default: " + esc(String(it.default)) + " · source: " + esc(src) + "</div>";
            var resetBtn = document.createElement("button");
            resetBtn.type = "button";
            resetBtn.className = "btn secondary";
            resetBtn.style.fontSize = "10px";
            resetBtn.textContent = "Reset";
            resetBtn.disabled = !it.editable;
            resetBtn.setAttribute("data-reset-key", it.key);
            row.appendChild(lbl);
            row.appendChild(input);
            row.appendChild(resetBtn);
            root.appendChild(row);
          });
        });
        if (status) status.textContent = "Config loaded. Edit values and Save. Secrets are not shown here.";
      })
      .catch(function (e) {
        var msg = safeText(e && e.message, String(e));
        if (root) {
          root.innerHTML = "<p class='empty-hint' style='color:var(--bad);'>Config failed to load: " + esc(msg) + "</p>";
        }
        if (status) status.textContent = "Error: " + msg;
      });
  }

  function wireConfigEditor() {
    var save = document.getElementById("btnConfigSave");
    var exp = document.getElementById("btnConfigExportSummary");
    var rail = document.getElementById("btnConfigRailwayTpl");
    var status = document.getElementById("configEditorStatus");
    if (save) save.addEventListener("click", function () {
      var updates = [];
      document.querySelectorAll("#configEditorRoot [data-config-key]").forEach(function (el) {
        var key = el.getAttribute("data-config-key");
        var typ = el.getAttribute("data-config-type");
        var val = typ === "bool" ? el.checked : (typ === "int" ? parseInt(el.value, 10) : typ === "float" ? parseFloat(el.value) : el.value);
        updates.push({ key: key, value: val });
      });
      fetch("/api/config/update", {
        method: "POST",
        headers: volHeaders(true),
        body: JSON.stringify({ updates: updates })
      }).then(function (r) { return r.json(); }).then(function (d) {
        if (status) status.textContent = d.ok ? "Saved: " + (d.applied || []).join(", ") : "Errors: " + (d.errors || []).join("; ");
      }).catch(function (e) {
        if (status) status.textContent = safeText(e && e.message, String(e));
      });
    });
    if (exp) exp.addEventListener("click", function () {
      fetch("/api/config/summary", { cache: "no-store" }).then(function (r) { return r.json(); })
        .then(function (d) { _downloadJson(d, "config_summary_" + _timestamp() + ".json"); });
    });
    if (rail) rail.addEventListener("click", function () {
      fetch("/api/config/railway-env-template").then(function (r) { return r.text(); })
        .then(function (t) { return navigator.clipboard.writeText(t); })
        .then(function () { if (status) status.textContent = "Railway template copied."; });
    });
    var cfgRoot = document.getElementById("configEditorRoot");
    if (cfgRoot) {
      cfgRoot.addEventListener("click", function (ev) {
        var t = ev.target;
        if (!t || !t.getAttribute || !t.getAttribute("data-reset-key")) return;
        var key = t.getAttribute("data-reset-key");
        fetch("/api/config/reset-key", {
          method: "POST",
          headers: volHeaders(true),
          body: JSON.stringify({ key: key })
        }).then(function () { loadConfigEditor(); });
      });
    }
  }

  function startDashboard() {
    bindTabs();
    wireMissionActions();
    wireGptAnalyze();
    wireMcMomoAsk();
    wireConfigEditor();
    function mcPollTick() {
      if (document.visibilityState === "hidden") return;
      var panel = document.getElementById("panel-mission");
      if (panel && panel.classList.contains("active")) loadMissionTab(false);
    }
    if (!_mcPollTimer) {
      _mcPollTimer = setInterval(mcPollTick, 35000);
      document.addEventListener("visibilitychange", function () {
        if (document.visibilityState === "visible") mcPollTick();
      });
    }
    wireBacktest();
    wireActivityExport();
    wireBrokerDiagnosticCopy();
    wireCapitalAllocatorCopy();
    wireOpsCenter();
    wireVolumeFiles();
    wireManualSell();
    wireAiChat();
    wireAiMemoryButtons();
    wireEquityRangeButtons();
    fetchDashboard();
    setInterval(fetchDashboard, POLL_MS);
    loadAiStatus();
    loadConfigEditor();
    document.querySelectorAll("nav .tab-btn").forEach(function (b) {
      b.addEventListener("click", function () {
        if (b.getAttribute("data-tab") === "ai") loadAiTab();
        if (b.getAttribute("data-tab") === "ops") loadOpsTab();
        if (b.getAttribute("data-tab") === "files") loadFilesTab();
        if (b.getAttribute("data-tab") === "config") loadConfigEditor();
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", startDashboard);
  } else {
    startDashboard();
  }
})();
