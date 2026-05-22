/**
 * MoMo dashboard — client-side cache, diff rendering, polling helpers.
 * Loaded before dashboard_app.js (window.MomoDashPerf).
 */
(function (global) {
  "use strict";

  var SYM_LS = "momo_sym_meta_v1";
  var SYM_TTL_MS = 7 * 24 * 3600 * 1000;
  var LS_DASH = "momo_dash_vm_v1";
  var LS_MC = "momo_mc_summary_v1";
  var LS_TTL_MS = 24 * 3600 * 1000;

  var _symMem = {};
  var _symFetch = null;
  var _dashRefreshing = false;
  var _dashStale = false;

  function _now() {
    return Date.now();
  }

  function _stableJson(o) {
    try {
      return JSON.stringify(o);
    } catch (e) {
      return "";
    }
  }

  function symKey(ac, sym) {
    return String(ac || "stock").toLowerCase() + "|" + String(sym || "").trim().toUpperCase();
  }

  function _loadSymLs() {
    try {
      var raw = localStorage.getItem(SYM_LS);
      if (!raw) return;
      var parsed = JSON.parse(raw);
      if (parsed && parsed.data && typeof parsed.data === "object") {
        Object.keys(parsed.data).forEach(function (k) {
          var row = parsed.data[k];
          if (row && row.ts && _now() - row.ts < SYM_TTL_MS) _symMem[k] = row;
        });
      }
    } catch (e) {}
  }

  function _saveSymLs() {
    try {
      localStorage.setItem(SYM_LS, JSON.stringify({ saved: _now(), data: _symMem }));
    } catch (e) {}
  }

  _loadSymLs();

  function getSymMeta(ac, sym) {
    return _symMem[symKey(ac, sym)] || null;
  }

  function setSymMeta(ac, sym, row) {
    var k = symKey(ac, sym);
    _symMem[k] = {
      url: row.url || null,
      fallback_url: row.fallback_url || null,
      fallback_letter: row.fallback_letter || "?",
      ts: _now()
    };
  }

  function ensureSymbolMeta(pairs, authHeadersFn) {
    if (!pairs || !pairs.length) return Promise.resolve();
    var missing = [];
    var seen = {};
    pairs.forEach(function (p) {
      var k = symKey(p.ac, p.symbol);
      if (seen[k]) return;
      seen[k] = true;
      var m = getSymMeta(p.ac, p.symbol);
      if (!m || !m.url) missing.push(p);
    });
    if (!missing.length) return Promise.resolve();
    if (_symFetch) return _symFetch;
    var qs = missing.map(function (p) {
      return encodeURIComponent(String(p.symbol).trim().toUpperCase()) + "|" + encodeURIComponent(String(p.ac || "stock").toLowerCase());
    }).join(",");
    var headers = authHeadersFn ? authHeadersFn() : {};
    _symFetch = fetch("/api/symbols/metadata?symbols=" + qs, { cache: "force-cache", headers: headers })
      .then(function (r) {
        if (!r.ok) return null;
        return r.json();
      })
      .then(function (d) {
        (d && d.items || []).forEach(function (it) {
          if (it && it.symbol) setSymMeta(it.asset_class || "stock", it.symbol, it);
        });
        _saveSymLs();
      })
      .catch(function () {})
      .finally(function () {
        _symFetch = null;
      });
    return _symFetch;
  }

  function iconSrc(ac, sym) {
    var m = getSymMeta(ac, sym);
    if (m && m.url) return m.url;
    return "/api/symbol-icon?asset_class=" + encodeURIComponent(ac || "stock") +
      "&symbol=" + encodeURIComponent(sym || "");
  }

  function lsRead(key) {
    try {
      var raw = localStorage.getItem(key);
      if (!raw) return null;
      var o = JSON.parse(raw);
      if (!o || !o.saved || _now() - o.saved > LS_TTL_MS) return null;
      return o.payload;
    } catch (e) {
      return null;
    }
  }

  function lsWrite(key, payload) {
    try {
      localStorage.setItem(key, JSON.stringify({ saved: _now(), payload: payload }));
    } catch (e) {}
  }

  function setRefreshing(on, stale) {
    _dashRefreshing = !!on;
    _dashStale = !!stale;
    var stamp = document.getElementById("dashUpdatedAt");
    if (!stamp) return;
    var base = stamp.getAttribute("data-base") || stamp.textContent.replace(/\s*·\s*refreshing.*$/i, "").replace(/\s*·\s*stale.*$/i, "");
    if (!stamp.getAttribute("data-base")) stamp.setAttribute("data-base", base);
    var suffix = "";
    if (_dashRefreshing) suffix += " · refreshing…";
    if (_dashStale) suffix += " · stale";
    stamp.textContent = base + suffix;
  }

  function fetchWithAbort(url, opts, ctrlRef) {
    var ctrl = new AbortController();
    if (ctrlRef && ctrlRef.current) {
      try {
        ctrlRef.current.abort();
      } catch (e) {}
    }
    if (ctrlRef) ctrlRef.current = ctrl;
    var o = opts || {};
    o.signal = ctrl.signal;
    return fetch(url, o).finally(function () {
      if (ctrlRef && ctrlRef.current === ctrl) ctrlRef.current = null;
    });
  }

  function patchText(el, text, className) {
    if (!el) return;
    if (el.textContent !== text) el.textContent = text;
    if (className !== undefined && el.className !== className) el.className = className;
  }

  function patchHtmlIfChanged(el, sig, html) {
    if (!el) return;
    if (el._patchSig === sig) return;
    el._patchSig = sig;
    el.innerHTML = html;
  }

  function patchListByKey(host, rows, keyFn, itemHtmlFn, maxItems) {
    if (!host) return;
    var cap = maxItems == null ? 24 : maxItems;
    var slice = rows.slice(0, cap);
    var existing = {};
    Array.prototype.forEach.call(host.children, function (li) {
      var k = li.getAttribute("data-row-key");
      if (k) existing[k] = li;
    });
    var frag = document.createDocumentFragment();
    slice.forEach(function (row) {
      var key = keyFn(row);
      var html = itemHtmlFn(row);
      var li = existing[key];
      if (!li) {
        li = document.createElement("li");
        li.setAttribute("data-row-key", key);
      }
      if (li._rowSig !== html) {
        li._rowSig = html;
        li.innerHTML = html;
      }
      frag.appendChild(li);
    });
    if (host.replaceChildren) host.replaceChildren(frag);
    else {
      while (host.firstChild) host.removeChild(host.firstChild);
      host.appendChild(frag);
    }
  }

  function patchTableByKey(tbody, rows, keyFn, rowHtmlFn) {
    if (!tbody) return;
    var existing = {};
    Array.prototype.forEach.call(tbody.children, function (tr) {
      var k = tr.getAttribute("data-row-key");
      if (k) existing[k] = tr;
    });
    var used = {};
    rows.forEach(function (row) {
      var key = keyFn(row);
      used[key] = true;
      var html = rowHtmlFn(row);
      var tr = existing[key];
      if (!tr) {
        tr = document.createElement("tr");
        tr.setAttribute("data-row-key", key);
        tbody.appendChild(tr);
      }
      if (tr._rowSig !== html) {
        tr._rowSig = html;
        tr.innerHTML = html;
      }
    });
    Array.prototype.forEach.call(tbody.children, function (tr) {
      var k = tr.getAttribute("data-row-key");
      if (k && !used[k]) tr.remove();
    });
  }

  function seriesSignature(series) {
    if (!series || !series.length) return "";
    var last = series[series.length - 1];
    return series.length + "|" + String(last.snapshot_at || last.timestamp || "") + "|" + Number(last.equity_total || last.equity || 0).toFixed(4);
  }

  function vmSignature(vm) {
    if (!vm) return "";
    return _stableJson({
      eq: vm.equity,
      cash: vm.cash,
      mode: vm.mode,
      pos: (vm.positions || []).map(function (p) {
        return [p.symbol, p.net_qty, p.unrealized_pnl_pct, p.current_price, p.market_value];
      }),
      tr: (vm.recentTrades || []).length,
      dec: (vm.executionDecisions || []).length
    });
  }

  function mcSignature(d) {
    if (!d) return "";
    var pos = ((d.positions || {}).open) || [];
    return _stableJson({
      eq: (d.account || {}).equity,
      pos: pos.map(function (p) {
        return [p.symbol, p.net_qty, p.unrealized_pnl_pct, p.current_price, p.market_value];
      }),
      push: (d.crypto_push || {}).status,
      worker: (d.worker || {}).last_cycle_age_seconds
    });
  }

  function pollIntervalMs(visibleMs, hiddenMs) {
    if (typeof document !== "undefined" && document.visibilityState === "hidden") {
      return hiddenMs || 120000;
    }
    return visibleMs || 30000;
  }

  function debounce(fn, ms) {
    var t = null;
    return function () {
      var self = this;
      var args = arguments;
      if (t) clearTimeout(t);
      t = setTimeout(function () {
        t = null;
        fn.apply(self, args);
      }, ms);
    };
  }

  global.MomoDashPerf = {
    symKey: symKey,
    getSymMeta: getSymMeta,
    setSymMeta: setSymMeta,
    ensureSymbolMeta: ensureSymbolMeta,
    iconSrc: iconSrc,
    lsRead: lsRead,
    lsWrite: lsWrite,
    LS_DASH: LS_DASH,
    LS_MC: LS_MC,
    setRefreshing: setRefreshing,
    fetchWithAbort: fetchWithAbort,
    patchText: patchText,
    patchHtmlIfChanged: patchHtmlIfChanged,
    patchListByKey: patchListByKey,
    patchTableByKey: patchTableByKey,
    seriesSignature: seriesSignature,
    vmSignature: vmSignature,
    mcSignature: mcSignature,
    pollIntervalMs: pollIntervalMs,
    debounce: debounce,
    stableJson: _stableJson
  };
})(typeof window !== "undefined" ? window : globalThis);
