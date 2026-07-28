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
    const pairs = rawVals.map((v, i) => ({ v: Number(v), d: rawDates[i] || `Session ${i + 1}` })).filter((p) => Number.isFinite(p.v));
    const nums = pairs.map((p) => p.v);
    const dates = pairs.map((p) => p.d);
    if (nums.length < 2) {
      return `<div class="chart-empty">Not enough session closes to draw a path.</div>`;
    }
    const w = opts.w || 640;
    const h = opts.h || 280;
    const padL = 68;
    const padR = 16;
    const padT = 28;
    const padB = 36;
    const min = Math.min(...nums);
    const max = Math.max(...nums);
    const range = max - min || 1;
    const n = nums.length;
    const points = nums.map((v, i) => {
      const x = padL + (i / (n - 1)) * (w - padL - padR);
      const y = padT + (1 - (v - min) / range) * (h - padT - padB);
      return { x, y, v, d: dates[i] };
    });
    const d = points.map((p, i) => (i === 0 ? `M ${p.x.toFixed(1)} ${p.y.toFixed(1)}` : `L ${p.x.toFixed(1)} ${p.y.toFixed(1)}`)).join(" ");
    const area = `${d} L ${points[n - 1].x.toFixed(1)} ${(h - padB).toFixed(1)} L ${points[0].x.toFixed(1)} ${(h - padB).toFixed(1)} Z`;
    const last = nums[n - 1];
    const up = last >= nums[0];
    const stroke = up ? "#0f9a8d" : "#b91c1c";
    const gid = `g${(opts.id || "s").toString().replace(/[^a-z0-9]/gi, "")}`;
    const fmt = (v) => Number(v).toLocaleString("en-IN", { maximumFractionDigits: 2 });
    const yTicks = [max, (max + min) / 2, min];
    const xLabels = [0, Math.floor((n - 1) / 2), n - 1].map((i) => ({
      x: points[i].x,
      label: dates[i] || `S${i + 1}`,
    }));
    const lastPt = points[n - 1];
    const payload = encodeURIComponent(JSON.stringify(points.map((p) => ({ x: p.x, y: p.y, v: p.v, d: p.d }))));
    return `
      <div class="sparkline-wrap" data-spark-points="${payload}" data-spark-w="${w}" data-spark-h="${h}" data-spark-pad-l="${padL}" data-spark-pad-r="${padR}">
        <svg viewBox="0 0 ${w} ${h}" class="sparkline-svg" role="img" aria-label="Interactive price path ${fmt(min)} to ${fmt(max)}. Hover for value and date.">
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
          <circle class="spark-static-last" cx="${lastPt.x}" cy="${lastPt.y}" r="4.5" fill="${stroke}"/>
          <text class="spark-static-label" x="${lastPt.x - 6}" y="${lastPt.y - 10}" text-anchor="end" fill="#12202a" font-size="12" font-weight="600" font-family="DM Sans,sans-serif">${fmt(last)}</text>
          ${xLabels.map((t) => `<text x="${t.x}" y="${h - 10}" text-anchor="middle" fill="#5b6b76" font-size="11" font-family="DM Sans,sans-serif">${t.label}</text>`).join("")}
          <g class="spark-hover" opacity="0" pointer-events="none">
            <line class="spark-cross-x" x1="0" y1="${padT}" x2="0" y2="${h - padB}" stroke="rgba(18,32,42,0.35)" stroke-dasharray="3 3"/>
            <circle class="spark-dot" cx="0" cy="0" r="5" fill="${stroke}" stroke="#fff" stroke-width="2"/>
          </g>
          <rect class="spark-hit" x="${padL}" y="${padT}" width="${w - padL - padR}" height="${h - padT - padB}" fill="transparent" style="cursor:crosshair"/>
        </svg>
        <div class="spark-tooltip" hidden>
          <div class="spark-tip-val"></div>
          <div class="spark-tip-date"></div>
        </div>
      </div>
    `;
  }

  function bindSparklines(root) {
    const scope = root || document;
    scope.querySelectorAll(".sparkline-wrap").forEach((wrap) => {
      if (wrap.dataset.bound === "1") return;
      wrap.dataset.bound = "1";
      let points;
      try {
        points = JSON.parse(decodeURIComponent(wrap.getAttribute("data-spark-points") || "[]"));
      } catch {
        return;
      }
      if (!points.length) return;
      const svg = wrap.querySelector(".sparkline-svg");
      const tip = wrap.querySelector(".spark-tooltip");
      const tipVal = wrap.querySelector(".spark-tip-val");
      const tipDate = wrap.querySelector(".spark-tip-date");
      const hover = wrap.querySelector(".spark-hover");
      const cross = wrap.querySelector(".spark-cross-x");
      const dot = wrap.querySelector(".spark-dot");
      const hit = wrap.querySelector(".spark-hit");
      const staticLast = wrap.querySelector(".spark-static-last");
      const staticLabel = wrap.querySelector(".spark-static-label");
      if (!svg || !hit || !tip) return;

      const fmt = (v) => Number(v).toLocaleString("en-IN", { maximumFractionDigits: 2 });

      const nearest = (clientX) => {
        const rect = svg.getBoundingClientRect();
        const vb = svg.viewBox.baseVal;
        const scaleX = rect.width / vb.width;
        const x = (clientX - rect.left) / scaleX;
        let best = 0;
        let bestDist = Infinity;
        for (let i = 0; i < points.length; i++) {
          const dist = Math.abs(points[i].x - x);
          if (dist < bestDist) {
            bestDist = dist;
            best = i;
          }
        }
        return points[best];
      };

      const show = (pt, clientX) => {
        hover.setAttribute("opacity", "1");
        cross.setAttribute("x1", pt.x);
        cross.setAttribute("x2", pt.x);
        dot.setAttribute("cx", pt.x);
        dot.setAttribute("cy", pt.y);
        if (staticLast) staticLast.setAttribute("opacity", "0.35");
        if (staticLabel) staticLabel.setAttribute("opacity", "0");
        tip.hidden = false;
        tipVal.textContent = fmt(pt.v);
        tipDate.textContent = pt.d || "";
        const wrapRect = wrap.getBoundingClientRect();
        const left = Math.min(Math.max(clientX - wrapRect.left + 12, 8), wrapRect.width - 140);
        tip.style.left = `${left}px`;
        tip.style.top = `${Math.max(8, (pt.y / svg.viewBox.baseVal.height) * wrapRect.height - 52)}px`;
      };

      const hide = () => {
        hover.setAttribute("opacity", "0");
        tip.hidden = true;
        if (staticLast) staticLast.setAttribute("opacity", "1");
        if (staticLabel) staticLabel.setAttribute("opacity", "1");
      };

      hit.addEventListener("mousemove", (e) => show(nearest(e.clientX), e.clientX));
      hit.addEventListener("mouseenter", (e) => show(nearest(e.clientX), e.clientX));
      hit.addEventListener("mouseleave", hide);
      hit.addEventListener(
        "touchmove",
        (e) => {
          if (!e.touches[0]) return;
          e.preventDefault();
          show(nearest(e.touches[0].clientX), e.touches[0].clientX);
        },
        { passive: false }
      );
      hit.addEventListener("touchend", hide);
    });
  }

  function whenReady(fn) {
    if (window.TP && TP.ready) fn();
    else document.addEventListener("tp:ready", fn, { once: true });
  }

  window.TPUI = { markActiveNav, setupMobileNav, injectFooter, sparkline, bindSparklines, currentPage, whenReady };

  document.addEventListener("DOMContentLoaded", () => {
    markActiveNav();
    setupMobileNav();
    injectFooter();
  });
})();
