/* Performance panel — ISOLATED ADD-ON, read-only. Polls /api/performance,
 * renders stat cards + per-day / per-source / per-model tables. No trading. */
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

  function render(p) {
    var el = document.getElementById("performance-body");
    var mode = document.getElementById("perf-mode");
    if (!el) return;
    if (mode) mode.textContent = (p.mode || "paper") + " · " + (p.days || 30) + "d";
    if (!p.closed_trades) {
      el.innerHTML = '<div class="empty-state">No closed trades yet</div>';
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
    var days = Object.keys(p.by_day).slice(-14).reverse();
    html += '<h4 class="perf-sub">By day</h4><div class="table-wrapper"><table class="data-table">' +
      "<thead><tr><th>Day</th><th>#</th><th>Win%</th><th>P&L</th><th>PF</th></tr></thead><tbody>" +
      days.map(function (d) {
        var b = p.by_day[d];
        return "<tr><td>" + esc(d) + "</td><td>" + b.trades + "</td><td>" + b.win_rate +
          "%</td><td>" + fmt(b.pnl) + "</td><td>" + b.profit_factor + "</td></tr>";
      }).join("") + "</tbody></table></div>";
    html += '<h4 class="perf-sub">By source</h4><div class="table-wrapper"><table class="data-table">' +
      "<thead><tr><th>Source</th><th>#</th><th>Win%</th><th>P&L</th><th>PF</th></tr></thead><tbody>" +
      rows(p.by_source) + "</tbody></table></div>";
    if (p.by_model && Object.keys(p.by_model).length) {
      html += '<h4 class="perf-sub">By model</h4><div class="table-wrapper"><table class="data-table">' +
        "<thead><tr><th>Model</th><th>#</th><th>Win%</th><th>P&L</th><th>PF</th></tr></thead><tbody>" +
        rows(p.by_model) + "</tbody></table></div>";
    }
    el.innerHTML = html;
  }

  function poll() {
    fetch("/api/performance?mode=paper&days=30")
      .then(function (r) { return r.json(); })
      .then(render)
      .catch(function () { /* keep old view */ });
  }

  document.addEventListener("DOMContentLoaded", function () {
    if (!document.getElementById("performance-body")) return;
    poll();
    setInterval(poll, 30000);
  });
})();
