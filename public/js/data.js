/* TradeProof — client shell (no sample market numbers; live API fills data) */
window.TP = window.TP || {};

TP.meta = {
  asOf: "—",
  session: "Awaiting live feed",
  universe: "Nifty 50 · India Top 100 · Penny (< ₹25)",
  disclaimer:
    "TradeProof is for educational and research purposes only. It is not investment advice, not a buy/sell recommendation, and not a registered advisory service. Signals, probabilities, option strikes, stop-losses, and targets are illustrative model outputs — not guarantees. Market data from free public sources may be delayed, incomplete, or incorrect. You alone are responsible for any trading decision.",
};

TP.indices = {
  nifty: { name: "NIFTY 50", exchange: "NSE", value: null, change: null, pct: null, quoteMode: "unavailable" },
  sensex: { name: "SENSEX", exchange: "BSE", value: null, change: null, pct: null, quoteMode: "unavailable" },
  vix: { name: "India VIX", exchange: "NSE", value: null, change: null, pct: null, quoteMode: "unavailable" },
};

TP.flows = {
  fii: { buy: null, sell: null, net: null },
  dii: { buy: null, sell: null, net: null },
  bias: null,
  biasScore: null,
  note: "FII/DII loads from live API",
};

TP.stocks = [];
TP.deals = [];
TP.pennies = [];
TP.optionsReversal = null;
TP.detail = {};

TP.helpers = {
  fmtCr(n) {
    if (n == null || !Number.isFinite(Number(n))) return "n/a";
    const sign = n > 0 ? "+" : "";
    return sign + "₹" + Math.abs(n).toLocaleString("en-IN") + " Cr";
  },
  fmtPct(n) {
    if (n == null || !Number.isFinite(Number(n))) return "n/a";
    const sign = n > 0 ? "+" : "";
    return sign + Number(n).toFixed(2) + "%";
  },
  fmtNum(n, d = 2) {
    if (n == null || !Number.isFinite(Number(n))) return "—";
    return Number(n).toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d });
  },
  fmtVol(n) {
    if (n == null || !Number.isFinite(Number(n))) return "n/a";
    if (n >= 1e7) return (n / 1e7).toFixed(2) + " Cr";
    if (n >= 1e5) return (n / 1e5).toFixed(2) + " L";
    return n.toLocaleString("en-IN");
  },
  /** Derive 7D band from forecast when price is known — never invent prices or volumes */
  enrichStock(stock) {
    const f = stock.forecast7d;
    const p = stock.price;
    if (!f || p == null || !Number.isFinite(Number(p))) return stock;
    const up = f.direction === "Up";
    const conf = (f.probability || 50) / 100;
    const upside = up ? 0.025 + conf * 0.045 : 0.012 + (1 - conf) * 0.02;
    const downside = up ? 0.012 + (1 - conf) * 0.02 : 0.025 + conf * 0.045;
    f.min = Math.round(p * (1 - downside) * 100) / 100;
    f.max = Math.round(p * (1 + upside) * 100) / 100;
    f.target = Math.round((up ? p * (1 + upside * 0.65) : p * (1 - downside * 0.65)) * 100) / 100;
    if (!Array.isArray(f.daily) || !f.daily.length) {
      f.daily = TP.helpers.buildDailyForecast(p, f);
    }
    return stock;
  },
  /** Client-side next-7-trading-day bands when API daily rows are missing */
  buildDailyForecast(price, f7) {
    const p = Number(price);
    if (!Number.isFinite(p) || p <= 0) return [];
    const baseDir = (f7 && f7.direction) || "Up";
    const baseProb = Number((f7 && f7.probability) || 55);
    const rows = [];
    const d = new Date();
    let dayI = 0;
    let guard = 0;
    const months = ["January","February","March","April","May","June","July","August","September","October","November","December"];
    const days = ["Sun","Mon","Tue","Wed","Thu","Fri","Sat"];
    while (rows.length < 7 && guard < 20) {
      guard += 1;
      d.setDate(d.getDate() + 1);
      if (d.getDay() === 0 || d.getDay() === 6) continue;
      dayI += 1;
      const decay = 1 - (dayI - 1) * 0.07;
      const move = 0.012 * Math.sqrt(dayI);
      const upBias = baseDir === "Up";
      let trend = "Sideways";
      if (Math.abs(baseProb - 50) * decay > 6) trend = upBias ? "Up" : "Down";
      const prob = Math.max(48, Math.min(92, Math.round(baseProb - (dayI - 1) * 2.2)));
      const upM = move * (trend === "Down" ? 0.55 : 1.1);
      const dnM = move * (trend === "Up" ? 0.55 : 1.1);
      const mid = p * (1 + (upBias ? 1 : -1) * move * 0.35 * (dayI / 7));
      const min = Math.round(mid * (1 - dnM) * 100) / 100;
      const max = Math.round(mid * (1 + upM) * 100) / 100;
      const date = `${days[d.getDay()]} ${d.getDate()} ${months[d.getMonth()]} ${d.getFullYear()}`;
      rows.push({
        day: dayI,
        date,
        currentPrice: Math.round(p * 100) / 100,
        min: Math.min(min, max),
        max: Math.max(min, max),
        trend,
        probability: prob,
        probabilityLabel: `${String(trend).toLowerCase()} — ${prob}%`,
        comment: dayI === 1
          ? `Near-term lean ${trend} from 7D model (${baseDir} ${baseProb}%)`
          : `Session ${dayI}: band widens with √day; conviction fades from day-1 bias`,
      });
    }
    return rows;
  },
  badge(trend) {
    const map = { Bullish: "badge-bull", Bearish: "badge-bear", Neutral: "badge-neutral" };
    return `<span class="badge ${map[trend] || "badge-neutral"}"><span class="dot"></span>${trend || "Neutral"}</span>`;
  },
  dirBadge(dir) {
    if (dir === "Sideways" || dir === "sideways") {
      return `<span class="badge badge-neutral">◆ Sideways</span>`;
    }
    if (dir === "Uptrend") {
      return `<span class="badge badge-bull">▲ Uptrend</span>`;
    }
    if (dir === "Downtrend") {
      return `<span class="badge badge-bear">▼ Downtrend</span>`;
    }
    const cls = dir === "Up" ? "badge-bull" : "badge-bear";
    return `<span class="badge ${cls}">${dir === "Up" ? "▲ Up" : "▼ Down"}</span>`;
  },
  breakoutBadge(b) {
    if (b === "confirmed") return `<span class="badge badge-bull">Breakout</span>`;
    if (b === "forming") return `<span class="badge badge-warn">Forming</span>`;
    return `<span class="badge badge-neutral">No breakout</span>`;
  },
  sideBadge(side) {
    const cls = side === "Buy" ? "badge-bull" : "badge-bear";
    return `<span class="badge ${cls}">${side}</span>`;
  },
  suggestBadge(s) {
    if (s.startsWith("Buy")) return `<span class="badge badge-bull">${s}</span>`;
    if (s === "Watch") return `<span class="badge badge-warn">${s}</span>`;
    return `<span class="badge badge-bear">${s}</span>`;
  },
  volumeSpeedBar(callVol, putVol, opts = {}) {
    const call = Number(callVol) || 0;
    const put = Number(putVol) || 0;
    if (call + put <= 0) {
      return `<div class="chart-empty">${opts.emptyLabel || "Options volume unavailable"}</div>`;
    }
    const total = call + put;
    const callPct = (call / total) * 100;
    const putPct = (put / total) * 100;
    const title = opts.title || "Active options volume · Call vs Put";
    return `
      <div class="speed-bar" role="img" aria-label="Call ${callPct.toFixed(0)} percent, Put ${putPct.toFixed(0)} percent">
        <div class="speed-bar-head">
          <span>${title}</span>
          <span style="color:var(--text-muted);font-weight:500">PCR vol ${(put / call).toFixed(2)}</span>
        </div>
        <div class="speed-bar-labels">
          <span class="up">CALL · ${this.fmtVol(call)} <strong>${callPct.toFixed(0)}%</strong></span>
          <span class="down">PUT · ${this.fmtVol(put)} <strong>${putPct.toFixed(0)}%</strong></span>
        </div>
        <div class="speed-bar-track">
          <div class="speed-bar-call" style="width:${callPct}%"></div>
          <div class="speed-bar-put" style="width:${putPct}%"></div>
          <div class="speed-bar-needle" style="left:${callPct}%"></div>
        </div>
        <div class="speed-bar-axis">
          <span>Call pressure</span>
          <span>Balance</span>
          <span>Put pressure</span>
        </div>
      </div>
    `;
  },
};
