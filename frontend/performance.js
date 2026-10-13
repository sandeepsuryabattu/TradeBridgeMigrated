/* Performance modal — ISOLATED ADD-ON, read-only. Opens from the 📈 header
 * icon, fetches /api/performance on open + every 30s while open. No trading.
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

  var timer = null;

  function poll() {
    fetch("/api/performance?mode=paper&days=30")
      .then(function (r) { return r.json(); })
      .then(render)
      .catch(function () { /* keep old view */ });
  }

  function open() {
    var m = document.getElementById("perf-modal");
    if (!m) return;
    m.style.display = "flex";
    poll();
    if (!timer) timer = setInterval(poll, 30000);
  }

  function close() {
    var m = document.getElementById("perf-modal");
    if (m) m.style.display = "none";
    if (timer) {
      clearInterval(timer);
      timer = null;
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    var btn = document.getElementById("btn-perf");
    if (btn) btn.addEventListener("click", open);
    var x = document.getElementById("btn-close-perf");
    if (x) x.addEventListener("click", close);
    var m = document.getElementById("perf-modal");
    if (m) m.addEventListener("click", function (e) { if (e.target === m) close(); });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape") close(); });
  });
})();
