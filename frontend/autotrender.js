/* Autotrender calls panel — engine calls + rationale ("Why?").
 * Polls /api/autotrender/calls, renders inbound calls, explicit paper-trade button.
 * Paper mode only enforced server-side (409 when bot is in real mode).
 *
 * Each engine-fired call carries c.model = {version, score, v1_score,
 * direction, trend, trigger, positioning, volatility, v2{...witnesses}}.
 * Manual/external calls have no model — the Why block says so.
 */
(function () {
  var listEl = null;
  var countEl = null;
  var lastSig = "";

  function apiHeaders() {
    var h = { "Content-Type": "application/json" };
    try {
      var t = sessionStorage.getItem("tradebridge_api_token") ||
              sessionStorage.getItem("api_token") ||
              localStorage.getItem("api_token");
      if (t) h["X-API-Token"] = t;
    } catch (e) { /* no token storage */ }
    return h;
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function num(v) {
    var n = Number(v);
    return Number.isFinite(n) ? n : null;
  }

  function whyRow(label, value) {
    if (value == null || value === "") return "";
    return '<div class="why-row"><span>' + esc(label) + "</span><span>" + esc(value) + "</span></div>";
  }

  function witnessRow(v2, key, label, fmt) {
    var o = (v2 || {})[key];
    if (!o || typeof o !== "object") return "";
    var txt = fmt(o);
    if (!txt) return "";
    return whyRow(label, txt);
  }

  // Human-readable engine rationale for one call. Pure function of the
  // call record — tested via node (see bottom of file for harness notes).
  function whyHTML(c) {
    var m = c.model;
    if (!m || typeof m !== "object") {
      return '<div class="muted">No engine rationale recorded (manual / external call).</div>';
    }
    var v2 = m.v2 || {};
    var h = '<div class="why-score">score ' + esc(m.score) +
      (m.v1_score != null ? " (v1 " + esc(m.v1_score) + ")" : "") +
      (m.direction ? " · " + esc(m.direction) : "") +
      (m.version ? ' <span class="muted">' + esc(m.version) + "</span>" : "") + "</div>";
    h += whyRow("Trigger", m.trigger);
    var trend = m.trend || {};
    var parts = trend.parts || {};
    h += whyRow("Trend", "score " + trend.score +
      (parts.ema_stack != null || parts.vwap_side != null
        ? " (ema " + parts.ema_stack + ", vwap " + parts.vwap_side + ")" : ""));
    h += whyRow("Positioning", (m.positioning || {}).reason);
    h += whyRow("Volatility", (m.volatility || {}).reason);
    // Key witnesses, one line each — skip abstentions / empties.
    h += witnessRow(v2, "strength", "ADX", function (o) {
      return o.adx == null ? "" : "adx " + o.adx + " (" + (o.trending ? "trending" : "range") +
        ", DI+ " + o.plus_di + " DI- " + o.minus_di + ")";
    });
    h += witnessRow(v2, "htf", "HTF 15m", function (o) {
      return o.bias == null ? "" : o.bias + " (" + o.tf_bars + " bars)";
    });
    h += witnessRow(v2, "supertrend", "Supertrend", function (o) {
      return o.direction || "";
    });
    h += witnessRow(v2, "positioning_eff", "Pos composite", function (o) {
      return o.reason || "";
    });
    h += witnessRow(v2, "oi_flow", "OI flow", function (o) {
      return o.reason || "";
    });
    h += witnessRow(v2, "iv_skew", "IV skew", function (o) {
      return o.reason || "";
    });
    h += witnessRow(v2, "exhaustion", "VWAP stretch", function (o) {
      return o.z == null ? "" : "z " + o.z + (o.stretched ? " CLIMAX" : " ok");
    });
    h += witnessRow(v2, "extension", "Day extension", function (o) {
      return o.ratio == null ? "" : "ratio " + o.ratio + (o.vetoed ? " SPENT" : "");
    });
    h += witnessRow(v2, "straddle", "Straddle", function (o) {
      if (o.chg_pct == null) return "";
      return "chg " + o.chg_pct + "%" +
        (o.expanding === true ? " EXPAND" : o.expanding === false ? " CRUSH" : "");
    });
    h += witnessRow(v2, "vix", "VIX", function (o) {
      return o.state != null ? o.state + (o.vix != null ? " (" + o.vix + ")" : "") : "";
    });
    return h;
  }

  function scoreBadge(c) {
    var m = c.model;
    if (!m || typeof m !== "object" || m.score == null) return "";
    return ' <span class="panel-count" title="Engine score">⚡' + esc(m.score) +
      (m.direction ? " " + esc(m.direction) : "") + "</span>";
  }

  function render(calls) {
    if (!listEl) return;
    if (countEl) countEl.textContent = String(calls.length);
    if (!calls.length) {
      if (lastSig !== "empty") {
        listEl.innerHTML = '<div class="empty-state">No Autotrender calls yet</div>';
        lastSig = "empty";
      }
      return;
    }
    // Skip re-render when nothing changed — preserves open Why expanders.
    var sig = JSON.stringify(calls.map(function (c) {
      return [c.id, c.status, c.underlying, c.strike, c.side, c.entry_low,
              c.entry_high, c.stoploss, c.target, c.model && c.model.score];
    }));
    if (sig === lastSig) return;
    lastSig = sig;
    listEl.innerHTML = calls.map(function (c) {
      var label = esc(c.underlying) + " " + esc(c.strike) + esc(String(c.side || "").replace("BUY ", ""));
      return (
        '<div class="signal-card" data-at-id="' + esc(c.id) + '">' +
          "<div><strong>" + label + "</strong>" + scoreBadge(c) + " " +
          '<span class="muted">entry ' + esc(c.entry_low) + "-" + esc(c.entry_high) +
          " SL " + esc(c.stoploss) + " TGT " + esc(c.target) + "</span></div>" +
          '<div class="muted">status: ' + esc(c.status) + " • " + esc(c.created_at || "") + "</div>" +
          '<details class="why"><summary>Why this call?</summary>' + whyHTML(c) + "</details>" +
          (c.status === "sent_to_paper"
            ? '<div class="muted">Sent to paper engine — watch Signals/Trades panels</div>'
            : '<button class="btn btn-sm btn-secondary" data-paper="' + esc(c.id) + '">Send to paper</button>') +
        "</div>"
      );
    }).join("");
    Array.prototype.forEach.call(listEl.querySelectorAll("[data-paper]"), function (btn) {
      btn.addEventListener("click", function () { paperTrade(btn.getAttribute("data-paper"), btn); });
    });
  }

  function paperTrade(id, btn) {
    if (btn) btn.disabled = true;
    fetch("/api/autotrender/calls/" + encodeURIComponent(id) + "/paper-trade", {
      method: "POST",
      headers: apiHeaders(),
    })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        if (!res.ok) {
          alert("Paper-trade blocked: " + ((res.j && res.j.detail) || "see bot logs"));
          if (btn) btn.disabled = false;
          return;
        }
        lastSig = ""; // force refresh so the new status shows
        poll();
      })
      .catch(function () { if (btn) btn.disabled = false; });
  }

  function poll() {
    fetch("/api/autotrender/calls?limit=50")
      .then(function (r) { return r.json(); })
      .then(function (j) { render((j && j.calls) || []); })
      .catch(function () { /* keep old list on error */ });
  }

  if (typeof document !== "undefined") {
    document.addEventListener("DOMContentLoaded", function () {
      listEl = document.getElementById("autotrender-list");
      countEl = document.getElementById("autotrender-count");
      if (!listEl) return;
      poll();
      setInterval(poll, 5000);
    });
  }

  // Node test harness: `node -e "require('./autotrender.js')"` does nothing in
  // the browser (no module system); logic is verified by extracting whyHTML.
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { whyHTML: whyHTML, esc: esc };
  }
})();
