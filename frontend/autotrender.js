/* Autotrender panel — ISOLATED ADD-ON. Does not touch dashboard.js state.
 * Polls /api/autotrender/calls, renders inbound calls, explicit paper-trade button.
 * Paper mode only enforced server-side (409 when bot is in real mode).
 */
(function () {
  var listEl = null;
  var countEl = null;

  function apiHeaders() {
    var h = { "Content-Type": "application/json" };
    try {
      var t = sessionStorage.getItem("api_token") || localStorage.getItem("api_token");
      if (t) h["X-API-Token"] = t;
    } catch (e) { /* no token storage */ }
    return h;
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function render(calls) {
    if (!listEl) return;
    if (countEl) countEl.textContent = String(calls.length);
    if (!calls.length) {
      listEl.innerHTML = '<div class="empty-state">No Autotrender calls yet — send one from autotrender-personal</div>';
      return;
    }
    listEl.innerHTML = calls.map(function (c) {
      var label = esc(c.underlying) + " " + esc(c.strike) + esc(String(c.side || "").replace("BUY ", ""));
      return (
        '<div class="signal-card" data-at-id="' + c.id + '">' +
          "<div><strong>" + label + "</strong> " +
          '<span class="muted">entry ' + esc(c.entry_low) + "-" + esc(c.entry_high) +
          " SL " + esc(c.stoploss) + " TGT " + esc(c.target) + "</span></div>" +
          '<div class="muted">status: ' + esc(c.status) + " • " + esc(c.created_at || "") + "</div>" +
          (c.status === "sent_to_paper"
            ? '<div class="muted">Sent to paper engine — watch Signals/Trades panels</div>'
            : '<button class="btn btn-sm btn-secondary" data-paper="' + c.id + '">Send to paper</button>') +
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

  document.addEventListener("DOMContentLoaded", function () {
    listEl = document.getElementById("autotrender-list");
    countEl = document.getElementById("autotrender-count");
    if (!listEl) return;
    poll();
    setInterval(poll, 5000);
  });
})();
