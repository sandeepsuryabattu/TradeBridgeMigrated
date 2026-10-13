/* Performance dashboard page — read-only analytics over closed trades.
 * Mode/days controls + auto-refresh. No trading of any kind.
 */
(function () {
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function fmt(n) {
    var v = Number(n);
    if (!Number.isFinite(v)) return "—";
    return (v > 0 ? "+" : "") + "₹" + v.toLocaleString("en-IN", { maximumFractionDigits: 0 });
  }

  function tone(v) {
    v = Number(v);
    if (!Number.isFinite(v) || v === 0) return "";
    return v > 0 ? "pos" : "neg";
  }

  function pf(v) {
    if (v === null || v === undefined) return "∞";
    return esc(v);
  }

  function card(label, value, cls) {
    return '<div class="perf-card"><div class="perf-label">' + esc(label) + "</div>" +
      '<div class="perf-value ' + (cls || "") + '">' + value + "</div></div>";
  }

  function aggRows(obj) {
    return Object.keys(obj).map(function (k) {
      var b = obj[k];
      return "<tr><td><span class='pill'>" + esc(k) + "</span></td><td>" + b.trades + "</td>" +
        "<td>" + b.win_rate + "%</td><td class='" + tone(b.pnl) + "'>" + fmt(b.pnl) + "</td>" +
        "<td>" + pf(b.profit_factor) + "</td></tr>";
    }).join("");
  }

  function params() {
    var modeEl = document.getElementById("perf-mode-sel");
    var daysEl = document.getElementById("perf-days");
    var srcEl = document.getElementById("perf-source");
    var mode = modeEl && (modeEl.value === "real" ? "real" : "paper");
    var days = daysEl ? Math.min(365, Math.max(1, parseInt(daysEl.value, 10) || 30)) : 30;
    var source = srcEl ? srcEl.value : "";
    return { mode: mode || "paper", days: days, source: source };
  }

  function section(title, thead, body) {
    return '<div class="perf-section"><h4>' + esc(title) + "</h4>" +
      '<div class="perf-scroll"><table class="perf-table"><thead><tr>' + thead +
      "</tr></thead><tbody>" + body + "</tbody></table></div></div>";
  }

  function render(p) {
    var el = document.getElementById("performance-body");
    var mode = document.getElementById("perf-mode");
    if (!el) return;
    syncSources(p);
    if (mode) mode.textContent = (p.mode || "paper") + " · " + (p.days || 30) + "d · " +
      (p.source && p.source !== "all" ? p.source + " · " : "") +
      (p.closed_trades || 0) + " closed";
    if (!p.closed_trades) {
      el.innerHTML = '<div class="empty-state">No closed trades in range</div>';
      return;
    }
    var o = p.overall;
    var html = '<div class="perf-hero">' +
      '<div class="perf-hero-main"><div class="perf-label">Total P&L</div>' +
      '<div class="num ' + tone(o.pnl) + '">' + fmt(o.pnl) + "</div>" +
      '<div class="perf-label">' + o.wins + "/" + o.trades + " wins · PF " + pf(o.profit_factor) + "</div></div>" +
      card("Win rate", o.win_rate + "%") +
      card("Expectancy / trade", fmt(o.expectancy), tone(o.expectancy)) +
      card("Max drawdown", fmt(p.max_drawdown), "neg") +
      "</div>";
    var days = Object.keys(p.by_day).slice(-30).reverse();
    html += section("By day", "<th>Day</th><th>#</th><th>Win%</th><th>P&L</th><th>PF</th>",
      days.map(function (d) {
        var b = p.by_day[d];
        return "<tr><td>" + esc(d) + "</td><td>" + b.trades + "</td><td>" + b.win_rate +
          "%</td><td class='" + tone(b.pnl) + "'>" + fmt(b.pnl) + "</td><td>" + pf(b.profit_factor) + "</td></tr>";
      }).join(""));
    html += section("By source", "<th>Source</th><th>#</th><th>Win%</th><th>P&L</th><th>PF</th>",
      aggRows(p.by_source));
    if (p.by_model && Object.keys(p.by_model).length) {
      html += section("By model", "<th>Model</th><th>#</th><th>Win%</th><th>P&L</th><th>PF</th>",
        aggRows(p.by_model));
    }
    if (p.ledger && p.ledger.length) {
      html += section("Ledger — all " + p.ledger.length + " closed trades (newest first)",
        "<th>Day</th><th>Symbol</th><th>Qty</th><th>Entry</th><th>Exit</th><th>P&L</th><th>Exit</th><th>Source</th>",
        p.ledger.map(function (t) {
          return "<tr><td>" + esc(t.day) + "</td><td>" + esc(t.symbol) + "</td>" +
            "<td>" + esc(t.qty) + "</td><td>" + fmt(t.entry) + "</td>" +
            "<td>" + fmt(t.exit) + "</td><td class='" + tone(t.pnl) + "'>" + fmt(t.pnl) + "</td>" +
            "<td>" + esc(t.reason) + "</td><td><span class='pill'>" + esc(t.source) + "</span></td></tr>";
        }).join(""));
    }
    el.innerHTML = html;
  }

  // Keep the source dropdown in sync with what the backend knows.
  // Autotrender appears here automatically once its first trade closes.
  var knownSources = [];
  function syncSources(p) {
    var sel = document.getElementById("perf-source");
    if (!sel || !p.by_source) return;
    if (params().source) return; // filtered view: keep options as-is
    var names = Object.keys(p.by_source).sort();
    if (names.join("|") === knownSources.join("|")) return;
    knownSources = names;
    var cur = sel.value;
    sel.innerHTML = '<option value="">all</option>' + names.map(function (n) {
      return '<option value="' + esc(n) + '"' + (n === cur ? " selected" : "") + ">" +
        esc(n) + "</option>";
    }).join("");
  }

  function poll() {
    var q = params();
    var url = "/api/performance?mode=" + encodeURIComponent(q.mode) + "&days=" + q.days;
    if (q.source) url += "&source=" + encodeURIComponent(q.source);
    fetch(url)
      .then(function (r) { return r.json(); })
      .then(render)
      .catch(function () { /* keep old view */ });
  }

  document.addEventListener("DOMContentLoaded", function () {
    if (!document.getElementById("performance-body")) return;
    var apply = document.getElementById("perf-apply");
    if (apply) apply.addEventListener("click", poll);
    poll();
    setInterval(poll, 30000);
  });
})();
