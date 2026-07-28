/* TradeProof — load live APIs + session-aware quote stream */
(function () {
  window.TP = window.TP || {};
  TP.ready = false;
  TP.live = false;
  TP.session = TP.session || { state: "unknown", isOpen: false };

  function ensureLoader() {
    let el = document.getElementById("tpLoader");
    if (el) return el;
    el = document.createElement("div");
    el.id = "tpLoader";
    el.className = "tp-loader";
    el.setAttribute("role", "status");
    el.setAttribute("aria-live", "polite");
    el.innerHTML = `
      <div class="tp-loader-card">
        <div class="tp-spinner" aria-hidden="true"></div>
        <div class="tp-loader-text" id="tpLoaderText">Loading market data…</div>
      </div>`;
    document.body.appendChild(el);
    return el;
  }

  function showLoader(msg) {
    const el = ensureLoader();
    el.hidden = false;
    document.body.classList.add("tp-loading");
    const text = document.getElementById("tpLoaderText");
    if (text && msg) text.textContent = msg;
  }

  function hideLoader() {
    const el = document.getElementById("tpLoader");
    if (el) el.hidden = true;
    document.body.classList.remove("tp-loading");
  }

  window.TPLoader = { show: showLoader, hide: hideLoader };

  function ensureHelpers() {
    if (!TP.helpers || !TP.helpers.enrichStock) return;
    (TP.stocks || []).forEach((s) => {
      if (!s.forecast7d || s.forecast7d.min == null || !s.optionVolume) TP.helpers.enrichStock(s);
    });
  }

  function applyDetailFromStocks() {
    TP.detail = TP.detail || {};
    (TP.stocks || []).forEach((s) => {
      const r = s.rating3yDetail || {};
      const f = s.forecast7d || {};
      TP.detail[s.symbol] = {
        thesis: `7-day model leans ${f.direction} at ${f.probability}% with band ₹${f.min} – ₹${f.max}. EMA score ${f.emaScore ?? "—"}/10.`,
        factors: [
          { name: "5D momentum", value: "from live scan", impact: "neutral", note: "Open stock detail for full drivers" },
          { name: "Volume intensity", value: `${Number(s.volumeZ || 0).toFixed(1)}σ`, impact: s.volumeZ >= 1 ? "bull" : "neutral" },
          { name: "Deal prints", value: `${s.deals || 0} · ₹${s.dealValue || 0} Cr`, impact: s.deals ? "bull" : "neutral" },
          {
            name: "50/200 EMA score",
            value: `${f.emaScore ?? "—"} / 10`,
            impact: "neutral",
            note: (f.emaScoreDetail && f.emaScoreDetail.rationale) || f.probMethod || "",
          },
        ],
        kpis: [
          { name: "P/E (TTM)", value: String(s.pe ?? "n/a"), note: "Live Yahoo fundamental" },
          { name: "P/B", value: String(s.pb ?? "n/a"), note: "Book check" },
          { name: "ROE %", value: String(s.roe ?? "n/a"), note: "Profitability" },
          { name: "Debt / Equity", value: String(s.debtEquity ?? "n/a"), note: "Leverage" },
          { name: "Div. yield %", value: String(s.dividendYield ?? 0), note: "Income" },
          {
            name: "3Y performance rating",
            value: `${s.rating3y} / 10`,
            note: r.method || "45% return + 35% drawdown + 20% volatility",
          },
        ],
        events: s.events || [],
        series: s.series || [],
        seriesDates: s.seriesDates || [],
        rating3yDetail: r,
        news: s.news || [],
      };
    });
  }

  function setBanner(html) {
    const banner = document.getElementById("liveBanner");
    if (banner) banner.innerHTML = html;
  }

  function applyQuotesPayload(payload) {
    if (!payload || payload.error) return;
    TP.session = payload.session || TP.session;
    if (payload.indices) {
      TP.indices = Object.assign({}, TP.indices || {}, payload.indices);
    }
    if (payload.equities) {
      TP.liveEquities = Object.assign({}, TP.liveEquities || {}, payload.equities);
      (TP.stocks || []).forEach((s) => {
        const dual = payload.equities[s.symbol];
        if (!dual) return;
        if (dual.nse && dual.nse.value != null) {
          s.price = dual.nse.value;
          s.priceNse = dual.nse.value;
          s.change = dual.nse.pct;
          s.changeNse = dual.nse.pct;
          s.quoteMode = dual.nse.quoteMode;
          s.quoteLabel = dual.nse.label;
        }
        if (dual.bse && dual.bse.value != null) {
          s.priceBse = dual.bse.value;
          s.changeBse = dual.bse.pct;
        }
      });
    }
    TP.meta = Object.assign({}, TP.meta || {}, {
      asOf: payload.asOf,
      session: (payload.session && payload.session.label) || TP.meta.session,
      sessionState: payload.session && payload.session.state,
      isOpen: payload.session && payload.session.isOpen,
    });
    document.dispatchEvent(new CustomEvent("tp:quotes", { detail: payload }));
  }

  let quoteTimer = null;
  let quoteEs = null;
  let heavyTimer = null;

  function stopQuoteStream() {
    if (quoteTimer) {
      clearTimeout(quoteTimer);
      quoteTimer = null;
    }
    if (quoteEs) {
      quoteEs.close();
      quoteEs = null;
    }
  }

  function watchedSymbols() {
    const params = new URLSearchParams(location.search);
    const fromQuery = (params.get("symbol") || "").toUpperCase();
    const fromStocks = (TP.stocks || []).slice(0, 8).map((s) => s.symbol);
    const set = new Set();
    if (fromQuery) set.add(fromQuery);
    fromStocks.forEach((s) => set.add(s));
    return [...set].slice(0, 10);
  }

  async function pollQuotesOnce() {
    const syms = watchedSymbols().join(",");
    const url = `/api/quotes${syms ? `?symbols=${encodeURIComponent(syms)}` : ""}`;
    const res = await fetch(url, { cache: "no-store" });
    if (!res.ok) throw new Error("quotes " + res.status);
    const payload = await res.json();
    applyQuotesPayload(payload);
    return payload;
  }

  function scheduleQuotePoll(payload) {
    stopQuoteStream();
    // Always refresh displayed quotes about every 1s (session-close values stay frozen server-side).
    const ms = 1000;
    quoteTimer = setTimeout(async () => {
      try {
        const next = await pollQuotesOnce();
        scheduleQuotePoll(next);
        const open = next.session && next.session.isOpen;
        setBanner(
          open
            ? `<span class="live-dot"></span> Live · refreshing every 1s · ${next.asOf || ""}`
            : `<span class="badge badge-neutral">Session closed</span> Last close held · polling 1s · ${next.asOf || ""}`
        );
      } catch (err) {
        console.warn("quote poll failed", err);
        quoteTimer = setTimeout(() => scheduleQuotePoll(payload || {}), 2000);
      }
    }, ms);
  }

  function startQuoteStream() {
    pollQuotesOnce()
      .then((payload) => {
        const sess = payload.session || {};
        setBanner(
          sess.isOpen
            ? `<span class="live-dot"></span> Market open · live quotes (1s) · ${payload.asOf || ""}`
            : `<span class="badge badge-neutral">Session closed</span> ${sess.label || "Last close held until next open"}`
        );
        scheduleQuotePoll(payload);
      })
      .catch((err) => console.warn("quotes start failed", err));
  }

  async function refreshHeavyOnce() {
    try {
      const [pulseRes, dealsRes] = await Promise.all([
        fetch("/api/pulse", { cache: "no-store" }),
        fetch("/api/deals", { cache: "no-store" }),
      ]);
      if (pulseRes.ok) {
        const pulse = await pulseRes.json();
        TP.indices = pulse.indices;
        TP.flows = pulse.flows;
        TP.session = pulse.session || TP.session;
        TP.meta = Object.assign({}, TP.meta || {}, pulse.meta || {}, { asOf: pulse.asOf });
        document.dispatchEvent(new Event("tp:pulse"));
      }
      if (dealsRes.ok) {
        const d = await dealsRes.json();
        TP.deals = d.deals || [];
        document.dispatchEvent(new Event("tp:deals"));
      }
    } catch (err) {
      console.warn("heavy refresh failed", err);
    }
  }

  function startHeavyRefresh() {
    if (heavyTimer) clearInterval(heavyTimer);
    // Keep flows/deals fresh while quotes tick every second
    heavyTimer = setInterval(refreshHeavyOnce, 15000);
  }

  async function loadLive() {
    showLoader("Loading live market data…");
    setBanner("Loading live market data…");
    try {
      const pulseRes = await fetch("/api/pulse", { cache: "no-store" });
      if (!pulseRes.ok) throw new Error("pulse " + pulseRes.status);
      const pulse = await pulseRes.json();
      TP.indices = pulse.indices;
      TP.flows = pulse.flows;
      TP.session = pulse.session || TP.session;
      TP.meta = Object.assign({}, TP.meta || {}, pulse.meta || {}, { asOf: pulse.asOf, session: pulse.meta?.session });
      TP.live = true;
      setBanner(`<span class="live-dot"></span> Pulse · ${pulse.asOf} · loading equities…`);
      document.dispatchEvent(new Event("tp:pulse"));
      showLoader("Loading equities, deals & options…");

      const [sigRes, dealsRes, optRes, pennyRes] = await Promise.all([
        fetch("/api/signals?limit=30", { cache: "no-store" }),
        fetch("/api/deals", { cache: "no-store" }),
        fetch("/api/options/nifty", { cache: "no-store" }),
        fetch("/api/pennies", { cache: "no-store" }),
      ]);

      if (sigRes.ok) {
        const sig = await sigRes.json();
        TP.stocks = sig.stocks || [];
        if (sig.asOf) TP.meta.asOf = sig.asOf;
      }
      if (dealsRes.ok) {
        const d = await dealsRes.json();
        TP.deals = d.deals || [];
      }
      if (optRes.ok) TP.optionsReversal = await optRes.json();
      if (pennyRes.ok) {
        const p = await pennyRes.json();
        TP.pennies = (p.pennies || []).map((x) => ({ ...x, include: true }));
      }

      applyDetailFromStocks();
      ensureHelpers();
      TP.ready = true;
      document.body.classList.add("is-live");
      startQuoteStream();
      startHeavyRefresh();
    } catch (err) {
      console.warn("Live API unavailable:", err);
      TP.live = false;
      TP.ready = true;
      TP.indices = TP.indices || {};
      ["nifty", "sensex", "vix"].forEach((k) => {
        TP.indices[k] = Object.assign({ quoteMode: "unavailable", value: null }, TP.indices[k] || {});
      });
      setBanner(
        `<span class="badge badge-warn">Data unavailable</span> Live API unreachable — no sample numbers shown. Start: <code>uvicorn app.main:app --port 8080</code>`
      );
    }
    hideLoader();
    document.dispatchEvent(new Event("tp:ready"));
  }

  window.TPQuotes = { start: startQuoteStream, stop: stopQuoteStream, pollOnce: pollQuotesOnce };

  showLoader("Connecting…");
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", loadLive);
  } else {
    loadLive();
  }
})();
