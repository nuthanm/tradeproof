/* TradeProof — shared UI helpers */
(function () {
  function currentPage() {
    const path = location.pathname.split("/").pop() || "index.html";
    return path.toLowerCase();
  }

  function markActiveNav() {
    const page = currentPage();
    document.querySelectorAll("[data-nav]").forEach((el) => {
      const target = (el.getAttribute("data-nav") || "").toLowerCase();
      if (target === page || (page === "" && target === "index.html")) {
        el.classList.add("active");
      }
    });
  }

  function setupMobileNav() {
    const toggle = document.querySelector(".nav-toggle");
    const links = document.querySelector(".nav-links");
    if (!toggle || !links) return;
    toggle.addEventListener("click", () => {
      links.classList.toggle("open");
    });
  }

  function injectFooter() {
    const el = document.querySelector("[data-footer]");
    if (!el || !window.TP) return;
    const disc = (TP.meta && TP.meta.disclaimer) || "";
    el.innerHTML = `
      <div class="container">
        <div>
          <strong style="color:var(--text)">TradeProof</strong>
          <div style="margin-top:0.35rem">Nifty 50 · Sensex · NSE / BSE flows · Educational research</div>
        </div>
        <p class="disclaimer">${disc}</p>
      </div>
    `;
  }

  function sparkline(values, opts = {}) {
    const rawVals = values || [];
    const rawDates = opts.dates || [];
    const pairs = rawVals.map((v, i) => ({ v: Number(v), d: rawDates[i] })).filter((p) => Number.isFinite(p.v));
    const nums = pairs.map((p) => p.v);
    const dates = pairs.map((p) => p.d);
    if (nums.length < 2) {
      return `<div class="chart-empty">Not enough session closes to draw a path.</div>`;
    }
    const w = opts.w || 640;
    const h = opts.h || 280;
    const padL = 68;
    const padR = 16;
    const padT = 20;
    const padB = 36;
    const min = Math.min(...nums);
    const max = Math.max(...nums);
    const range = max - min || 1;
    const n = nums.length;
    const points = nums.map((v, i) => {
      const x = padL + (i / (n - 1)) * (w - padL - padR);
      const y = padT + (1 - (v - min) / range) * (h - padT - padB);
      return [x, y, v];
    });
    const d = points.map((p, i) => (i === 0 ? `M ${p[0].toFixed(1)} ${p[1].toFixed(1)}` : `L ${p[0].toFixed(1)} ${p[1].toFixed(1)}`)).join(" ");
    const area = `${d} L ${points[n - 1][0].toFixed(1)} ${(h - padB).toFixed(1)} L ${points[0][0].toFixed(1)} ${(h - padB).toFixed(1)} Z`;
    const last = nums[n - 1];
    const up = last >= nums[0];
    const stroke = up ? "#0f9a8d" : "#b91c1c";
    const gid = `g${(opts.id || "s").toString().replace(/[^a-z0-9]/gi, "")}`;
    const fmt = (v) => Number(v).toLocaleString("en-IN", { maximumFractionDigits: 2 });
    const yTicks = [max, (max + min) / 2, min];
    const xLabels = [0, Math.floor((n - 1) / 2), n - 1].map((i) => ({
      x: points[i][0],
      label: dates[i] || `S${i + 1}`,
    }));
    const lastPt = points[n - 1];
    return `
      <svg viewBox="0 0 ${w} ${h}" class="sparkline-svg" role="img" aria-label="Price path ${fmt(min)} to ${fmt(max)}">
        <defs>
          <linearGradient id="${gid}" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="${stroke}" stop-opacity="0.32"/>
            <stop offset="100%" stop-color="${stroke}" stop-opacity="0"/>
          </linearGradient>
        </defs>
        ${yTicks.map((v) => {
          const y = padT + (1 - (v - min) / range) * (h - padT - padB);
          return `<line x1="${padL}" y1="${y}" x2="${w - padR}" y2="${y}" stroke="rgba(15,40,50,0.08)" stroke-dasharray="3 4"/>
            <text x="${padL - 8}" y="${y + 4}" text-anchor="end" fill="#5b6b76" font-size="11" font-family="DM Sans,sans-serif">${fmt(v)}</text>`;
        }).join("")}
        <path d="${area}" fill="url(#${gid})"/>
        <path d="${d}" fill="none" stroke="${stroke}" stroke-width="2.5"
          stroke-linecap="round" stroke-linejoin="round"/>
        <circle cx="${lastPt[0]}" cy="${lastPt[1]}" r="4.5" fill="${stroke}"/>
        <text x="${lastPt[0] - 6}" y="${lastPt[1] - 10}" text-anchor="end" fill="#12202a" font-size="12" font-weight="600" font-family="DM Sans,sans-serif">${fmt(last)}</text>
        ${xLabels.map((t) => `<text x="${t.x}" y="${h - 10}" text-anchor="middle" fill="#5b6b76" font-size="11" font-family="DM Sans,sans-serif">${t.label}</text>`).join("")}
      </svg>
    `;
  }

  function whenReady(fn) {
    if (window.TP && TP.ready) fn();
    else document.addEventListener("tp:ready", fn, { once: true });
  }

  window.TPUI = { markActiveNav, setupMobileNav, injectFooter, sparkline, currentPage, whenReady };

  document.addEventListener("DOMContentLoaded", () => {
    markActiveNav();
    setupMobileNav();
    injectFooter();
  });
})();
