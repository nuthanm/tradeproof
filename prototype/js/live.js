/* TradeProof — load live APIs + session-aware quote stream */
(function () {
  window.TP = window.TP || {};
  TP.ready = false;
  TP.live = false;
  TP.session = TP.session || { state: "unknown", isOpen: false };

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
      TP.detail[s.symbol] = {
        thesis: `7-day model leans ${s.forecast7d.direction} at ${s.forecast7d.probability}% with band ₹${s.forecast7d.min} – ₹${s.forecast7d.max}.`,
        factors: [
          { name: "5D momentum", value: "from live scan", impact: "neutral", note: "Open stock detail for full drivers" },
          { name: "Volume intensity", value: `${Number(s.volumeZ || 0).toFixed(1)}σ`, impact: s.volumeZ >= 1 ? "bull" : "neutral" },
          { name: "Deal prints", value: `${s.deals || 0} · ₹${s.dealValue || 0} Cr`, impact: s.deals ? "bull" : "neutral" },
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
      // Merge into stocks table if present
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
    const sess = (payload && payload.session) || TP.session || {};
    const ms = sess.isOpen ? Math.max(1000, payload.pollHintMs || 1000) : Math.max(15000, payload.pollHintMs || 60000);
    quoteTimer = setTimeout(async () => {
      try {
        const next = await pollQuotesOnce();
        scheduleQuotePoll(next);
        const open = next.session && next.session.isOpen;
        setBanner(
          open
            ? `<span class="live-dot"></span> Market open · quotes refreshing · ${next.asOf || ""}`
            : `<span class="badge badge-neutral">Session closed</span> Last close held · ${next.asOf || ""}`
        );
      } catch (err) {
        console.warn("quote poll failed", err);
        quoteTimer = setTimeout(() => scheduleQuotePoll(payload || {}), 5000);
      }
    }, ms);
  }

  function startQuoteStream() {
    // Prefer polling (more reliable behind some proxies than SSE)
    pollQuotesOnce()
      .then((payload) => {
        const sess = payload.session || {};
        setBanner(
          sess.isOpen
            ? `<span class="live-dot"></span> Market open · live quotes · ${payload.asOf || ""}`
            : `<span class="badge badge-neutral">Session closed</span> ${sess.label || "Last close held until next open"}`
        );
        scheduleQuotePoll(payload);
      })
      .catch((err) => console.warn("quotes start failed", err));
  }

  async function loadLive() {
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
    document.dispatchEvent(new Event("tp:ready"));
  }

  window.TPQuotes = { start: startQuoteStream, stop: stopQuoteStream, pollOnce: pollQuotesOnce };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", loadLive);
  } else {
    loadLive();
  }
})();
