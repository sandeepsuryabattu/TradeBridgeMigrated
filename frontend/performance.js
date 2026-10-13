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
    return (v >= 0 ? "+" : "") + "₹" + v.toLocaleString("en-IN", { maximumFractionDigits: 0 });
  }

  function card(label, value, tone) {
    return '<div class="perf-card"><div class="perf-label">' + esc(label) + "</div>" +
      '<div class="perf-value ' + (tone || "") + '">' + esc(value) + "</div></div>";
  }

  function rows(obj) {
    return Object.keys(obj).map(function (k) {
      var b = obj[k];
      return "<tr><td>" + esc(k) + "</td><td>" + b.trades + "</td>" +
        "<td>" + b.win_rate + "%</td><td>" + fmt(b.pnl) + "</td>" +
        "<td>" + b.profit_factor + "</td></tr>";
    }).join("");
  }

  function params() {
    var modeEl = document.getElementById("perf-mode-sel");
    var daysEl = document.getElementById("perf-days");
    var mode = modeEl && (modeEl.value === "real" ? "real" : "paper");
    var days = daysEl ? Math.min(365, Math.max(1, parseInt(daysEl.value, 10) || 30)) : 30;
    return { mode: mode || "paper", days: days };
  }

  function render(p) {
    var el = document.getElementById("performance-body");
    var mode = document.getElementById("perf-mode");
    if (!el) return;
    if (mode) mode.textContent = (p.mode || "paper") + " · " + (p.days || 30) + "d · " +
      (p.closed_trades || 0) + " closed";
    if (!p.closed_trades) {
      el.innerHTML = '<div class="empty-state">No closed trades in range</div>';
      return;
    }
    var o = p.overall;
    var html = '<div class="perf-grid">' +
      card("Total P&L", fmt(o.pnl), o.pnl >= 0 ? "value-up" : "value-down") +
      card("Win rate", o.win_rate + "% (" + o.wins + "/" + o.trades + ")") +
      card("Profit factor", o.profit_factor) +
      card("Expectancy", fmt(o.expectancy)) +
      card("Max drawdown", fmt(p.max_drawdown), "value-down") +
      card("Best / Worst", fmt(o.best) + " / " + fmt(o.worst)) +
      "</div>";
    var days = Object.keys(p.by_day).slice(-30).reverse();
    html += '<div class="perf-section"><h4 class="perf-sub">By day</h4>' +
      '<div class="table-wrapper"><table class="data-table">' +
      "<thead><tr><th>Day</th><th>#</th><th>Win%</th><th>P&L</th><th>PF</th></tr></thead><tbody>" +
      days.map(function (d) {
        var b = p.by_day[d];
        return "<tr><td>" + esc(d) + "</td><td>" + b.trades + "</td><td>" + b.win_rate +
          "%</td><td>" + fmt(b.pnl) + "</td><td>" + b.profit_factor + "</td></tr>";
      }).join("") + "</tbody></table></div></div>";
    html += '<div class="perf-section"><h4 class="perf-sub">By source</h4>' +
      '<div class="table-wrapper"><table class="data-table">' +
      "<thead><tr><th>Source</th><th>#</th><th>Win%</th><th>P&L</th><th>PF</th></tr></thead><tbody>" +
      rows(p.by_source) + "</tbody></table></div></div>";
    if (p.by_model && Object.keys(p.by_model).length) {
      html += '<div class="perf-section"><h4 class="perf-sub">By model</h4>' +
        '<div class="table-wrapper"><table class="data-table">' +
        "<thead><tr><th>Model</th><th>#</th><th>Win%</th><th>P&L</th><th>PF</th></tr></thead><tbody>" +
        rows(p.by_model) + "</tbody></table></div></div>";
    }
    el.innerHTML = html;
  }

  function poll() {
    var q = params();
    fetch("/api/performance?mode=" + encodeURIComponent(q.mode) + "&days=" + q.days)
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
