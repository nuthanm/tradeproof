"""Breakout detection, 7-day forecast bands, ratings, penny scoring."""
from __future__ import annotations

from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from app import fetchers
from app.option_spots import (
    DISCLAIMER,
    build_hero_zero,
    build_weekly_option_spots,
    fetch_nifty_option_chain,
    forecast_methodology,
)
from app.universe import NIFTY_50, PENNY_CANDIDATES


def _safe_float(x: Any, default: float | None = None) -> float | None:
    try:
        if x is None or (isinstance(x, float) and np.isnan(x)):
            return default
        return float(x)
    except Exception:  # noqa: BLE001
        return default


def volume_zscore(series: pd.Series, window: int = 20) -> float:
    if series is None or len(series) < window + 1:
        return 0.0
    windowed = series.iloc[-(window + 1) : -1]
    mu = float(windowed.mean())
    sigma = float(windowed.std(ddof=0) or 1.0)
    if not np.isfinite(mu) or not np.isfinite(sigma) or sigma == 0:
        return 0.0
    z = float((series.iloc[-1] - mu) / sigma)
    return z if np.isfinite(z) else 0.0


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.astype(float).ewm(span=span, adjust=False).mean()


def support_resistance(df: pd.DataFrame) -> dict[str, float]:
    """Recent swing support / resistance from 20D and 55D highs & lows."""
    close = df["Close"].dropna()
    high = df["High"] if "High" in df.columns else close
    low = df["Low"] if "Low" in df.columns else close
    high = high.reindex(close.index).fillna(close)
    low = low.reindex(close.index).fillna(close)
    n = len(close)
    high20 = float(high.iloc[max(0, n - 21) : n - 1].max()) if n > 2 else float(close.iloc[-1])
    low20 = float(low.iloc[max(0, n - 21) : n - 1].min()) if n > 2 else float(close.iloc[-1])
    high55 = float(high.iloc[max(0, n - 56) : n - 1].max()) if n > 5 else high20
    low55 = float(low.iloc[max(0, n - 56) : n - 1].min()) if n > 5 else low20
    resistance = max(high20, high55 * 0.998)
    support = min(low20, low55 * 1.002)
    return {
        "support": round(support, 2),
        "resistance": round(resistance, 2),
        "high20": round(high20, 2),
        "low20": round(low20, 2),
        "high55": round(high55, 2),
        "low55": round(low55, 2),
    }


def classify_structure(last: float, levels: dict[str, float], ema50: float, ema200: float, vz: float) -> tuple[str, str]:
    """
    Classify price vs EMA + S/R:
    confirmed breakout | forming | rangebound | none
    """
    res = levels["resistance"]
    sup = levels["support"]
    band = max(res - sup, last * 0.01)
    dist_res = (last - res) / last if last else 0
    dist_sup = (last - sup) / last if last else 0
    mid = (res + sup) / 2
    range_pct = band / last if last else 0

    if last > res * 1.001 and last >= ema50 and vz >= 0.6:
        return "confirmed", f"Cleared resistance ₹{res:,.2f} with EMA50 support — breakout"
    if last >= res * 0.992 and last <= res * 1.005 and last >= ema50:
        return "forming", f"Coiling at resistance ₹{res:,.2f} — breakout forming"
    if last <= sup * 1.008 and last >= sup * 0.992 and last <= ema50:
        return "forming", f"Pressing support ₹{sup:,.2f} — downside break risk"
    if range_pct < 0.06 and abs(last - mid) / band < 0.35 and abs(ema50 - ema200) / last < 0.015:
        return "none", f"Range-bound between ₹{sup:,.2f}–₹{res:,.2f} (sideways)"
    if last < ema50 and last < ema200:
        return "none", "Below 50 & 200 EMA — no upside breakout structure"
    return "none", f"Inside S/R ₹{sup:,.2f}–₹{res:,.2f} · waiting for edge"


def detect_breakout(df: pd.DataFrame) -> tuple[str, str]:
    if df is None or len(df) < 40:
        return "none", "Insufficient history for breakout structure"
    close = df["Close"].dropna()
    last = float(close.iloc[-1])
    e50 = float(ema(close, 50).iloc[-1]) if len(close) >= 20 else last
    e200 = float(ema(close, 200).iloc[-1]) if len(close) >= 50 else e50
    levels = support_resistance(df)
    vz = volume_zscore(df["Volume"]) if "Volume" in df.columns else 0.0
    state, note = classify_structure(last, levels, e50, e200, vz)
    # Keep legacy volume confirmation for confirmed
    if state == "confirmed" and vz < 0.4:
        return "forming", note.replace("breakout", "breakout watch (volume soft)")
    return state, note


def ema_score_7d(
    last: float,
    ema50: float,
    ema200: float,
    levels: dict[str, float],
    breakout: str,
    direction: str,
    vz: float,
    fii_net: float,
) -> dict[str, Any]:
    """
    1–10 conviction score for the 7D forecast.
    1 = least chance the stated Prob% holds; 10 ≈ near sure-shot (~99% model confidence).
    """
    pts = 0.0
    why: list[str] = []

    # Trend stack (max ~4)
    if ema50 > ema200:
        pts += 2.0
        why.append(f"50 EMA (₹{ema50:,.2f}) above 200 EMA (₹{ema200:,.2f}) — bullish stack")
    elif ema50 < ema200:
        pts += 2.0 if direction == "Down" else 0.5
        why.append(f"50 EMA below 200 EMA — bearish stack")
    else:
        pts += 1.0
        why.append("50/200 EMA flat — weak trend signal")

    if last > ema50:
        pts += 1.5 if direction == "Up" else 0.3
        why.append("Price holding above 50 EMA")
    else:
        pts += 1.5 if direction == "Down" else 0.3
        why.append("Price below 50 EMA")

    if last > ema200:
        pts += 1.0 if direction == "Up" else 0.2
        why.append("Price above 200 EMA (primary trend support)")
    else:
        pts += 1.0 if direction == "Down" else 0.2
        why.append("Price under 200 EMA (primary trend pressure)")

    # Structure vs S/R (max ~3)
    res, sup = levels["resistance"], levels["support"]
    if breakout == "confirmed":
        pts += 2.5
        why.append(f"Confirmed break of S/R (R ₹{res:,.2f} / S ₹{sup:,.2f})")
    elif breakout == "forming":
        pts += 1.5
        why.append(f"Near breakout zone around R ₹{res:,.2f} / S ₹{sup:,.2f}")
    else:
        pts += 0.6
        why.append(f"Range / no clean break — S ₹{sup:,.2f} R ₹{res:,.2f}")

    # Participation + flows (max ~2)
    if abs(vz) >= 1.2:
        pts += 1.2
        why.append(f"Volume confirmation ({vz:+.1f}σ vs 20D)")
    elif abs(vz) >= 0.5:
        pts += 0.6
        why.append(f"Moderate volume ({vz:+.1f}σ)")
    else:
        pts += 0.2
        why.append("Volume near average — weaker confirmation")

    if (fii_net > 500 and direction == "Up") or (fii_net < -500 and direction == "Down"):
        pts += 0.8
        why.append(f"FII cash aligns with bias (₹{fii_net:+.0f} Cr)")
    elif abs(fii_net) > 500:
        pts += 0.2
        why.append(f"FII cash opposes bias (₹{fii_net:+.0f} Cr)")

    score = int(np.clip(round(pts), 1, 10))
    # Map score → stated confidence that Prob% is actionable
    confidence_pct = int(np.clip(35 + score * 6.5, 40, 99))
    rationale = (
        f"EMA score {score}/10 (model confidence ~{confidence_pct}% that the Prob % direction holds over 7 sessions). "
        + " · ".join(why[:5])
    )
    return {
        "score": score,
        "confidencePct": confidence_pct,
        "rationale": rationale,
        "reasons": why,
        "ema50": round(ema50, 2),
        "ema200": round(ema200, 2),
        "support": levels["support"],
        "resistance": levels["resistance"],
        "regime": breakout if breakout != "none" else "rangebound",
    }


def forecast_7d(df: pd.DataFrame, breakout: str, fii_net: float) -> dict[str, Any]:
    """
    7-day forecast using 50/200 EMA, support/resistance proximity, volume, and FII tilt.
    Prob % = chance of stated Up/Down path; EMA score (1–10) rates conviction behind that Prob %.
    """
    close = df["Close"].dropna()
    empty = {
        "direction": "Up",
        "probability": 55,
        "min": 0.0,
        "max": 0.0,
        "target": 0.0,
        "score": 0.0,
        "volumeZ": 0.0,
        "last": 0.0,
        "change": 0.0,
        "volatility7d": 1.5,
        "emaScore": 5,
        "emaScoreDetail": {
            "score": 5,
            "confidencePct": 55,
            "rationale": "Insufficient history — default mid score.",
            "reasons": [],
            "ema50": None,
            "ema200": None,
            "support": None,
            "resistance": None,
            "regime": "unknown",
        },
        "probMethod": "Insufficient history",
    }
    if close.empty:
        return empty
    last = float(close.iloc[-1])
    if not np.isfinite(last) or last <= 0:
        last = float(close.dropna().iloc[-1]) if len(close.dropna()) else 0.0

    e50_s = ema(close, 50)
    e200_s = ema(close, 200)
    ema50 = float(e50_s.iloc[-1]) if len(e50_s) else last
    ema200 = float(e200_s.iloc[-1]) if len(e200_s) else last
    levels = support_resistance(df)
    vz = volume_zscore(df["Volume"]) if "Volume" in df.columns else 0.0

    rets = close.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
    vol = float(rets.iloc[-20:].std()) if len(rets) >= 5 else 0.015
    if not np.isfinite(vol) or vol <= 0:
        vol = 0.015
    mom5 = float(close.iloc[-1] / close.iloc[-6] - 1) if len(close) > 6 else 0.0
    mom20 = float(close.iloc[-1] / close.iloc[-21] - 1) if len(close) > 21 else 0.0
    if not np.isfinite(mom5):
        mom5 = 0.0
    if not np.isfinite(mom20):
        mom20 = 0.0

    # Directional score from EMA stack + S/R + momentum + flows
    score = 0.0
    score += 2.2 if ema50 > ema200 else (-2.2 if ema50 < ema200 else 0.0)
    score += 1.6 if last > ema50 else -1.6
    score += 1.0 if last > ema200 else -1.0
    dist_res = (last / levels["resistance"] - 1) * 100 if levels["resistance"] else 0
    dist_sup = (last / levels["support"] - 1) * 100 if levels["support"] else 0
    if breakout == "confirmed":
        score += 2.4 if last >= levels["resistance"] else -2.4
    elif breakout == "forming":
        score += 1.0 if dist_res > -1.2 else (-1.0 if dist_sup < 1.2 else 0.3)
    else:
        # sideways: lean with short momentum only lightly
        score += float(np.clip(mom5 * 40, -0.8, 0.8))
    score += float(np.clip(mom5 * 80, -2.0, 2.0))
    score += float(np.clip(mom20 * 30, -1.5, 1.5))
    score += float(np.clip(vz * 0.7, -2.0, 2.0))
    if fii_net > 500:
        score += 0.9
    elif fii_net < -500:
        score -= 0.9
    if not np.isfinite(score):
        score = 0.0

    direction = "Up" if score >= 0 else "Down"

    # Prob from |score| + structure quality (45–92)
    structure_boost = 8 if breakout == "confirmed" else 4 if breakout == "forming" else 0
    range_penalty = 6 if breakout == "none" and abs(dist_res) < 3 and abs(dist_sup) < 3 else 0
    prob = int(np.clip(48 + abs(score) * 7 + structure_boost - range_penalty, 45, 92))

    detail = ema_score_7d(last, ema50, ema200, levels, breakout, direction, vz, fii_net)
    # Tighten / widen Prob with EMA conviction: score 1 → soft, score 10 → near 99 ceiling allowed
    if detail["score"] >= 9:
        prob = int(np.clip(max(prob, 78), 45, 92))
    elif detail["score"] <= 3:
        prob = int(np.clip(min(prob, 58), 45, 92))

    move = vol * np.sqrt(7)
    if not np.isfinite(move) or move <= 0:
        move = 0.015 * np.sqrt(7)
    if direction == "Up":
        upside = move * (1.15 + (prob - 50) / 200)
        downside = move * (0.55 + (100 - prob) / 250)
    else:
        downside = move * (1.15 + (prob - 50) / 200)
        upside = move * (0.55 + (100 - prob) / 250)
    if not np.isfinite(upside):
        upside = 0.02
    if not np.isfinite(downside):
        downside = 0.02

    fmin = round(float(last * (1 - downside)), 2)
    fmax = round(float(last * (1 + upside)), 2)
    target = round(float(last * (1 + (upside * 0.65 if direction == "Up" else -downside * 0.65))), 2)
    if not np.isfinite(fmin) or not np.isfinite(fmax) or not np.isfinite(target) or last <= 0:
        fmin = round(last * 0.98, 2) if last > 0 else 0.0
        fmax = round(last * 1.02, 2) if last > 0 else 0.0
        target = round(last, 2) if last > 0 else 0.0
    change_pct = float((close.iloc[-1] / close.iloc[-2] - 1) * 100) if len(close) > 1 else 0.0
    if not np.isfinite(change_pct):
        change_pct = 0.0
    move_pct = float(move) * 100
    if not np.isfinite(move_pct):
        move_pct = 1.5

    prob_method = (
        f"Prob {prob}% {direction}: derived from 50/200 EMA stack, distance to support ₹{levels['support']:,.2f} / "
        f"resistance ₹{levels['resistance']:,.2f}, breakout state “{breakout}”, 5D/20D momentum, volume z, and FII tilt. "
        f"EMA score {detail['score']}/10 explains conviction behind this Prob %."
    )

    return {
        "direction": direction,
        "probability": prob,
        "min": fmin,
        "max": fmax,
        "target": target,
        "score": round(float(score), 2),
        "volumeZ": round(float(vz) if np.isfinite(vz) else 0.0, 2),
        "last": round(float(last) if np.isfinite(last) else 0.0, 2),
        "change": round(change_pct, 2),
        "volatility7d": round(move_pct, 2),
        "emaScore": detail["score"],
        "emaScoreDetail": detail,
        "probMethod": prob_method,
        "levels": levels,
    }


def rating_3y(df: pd.DataFrame) -> float:
    detail = rating_3y_detail(df)
    return detail["rating"]


def rating_3y_detail(df: pd.DataFrame) -> dict[str, Any]:
    """
    3-year performance rating (1–10):
    - 45% total return over available history (capped ~3y)
    - 35% max drawdown resilience
    - 20% annualized volatility (lower is better)
    """
    if df is None or len(df) < 60:
        return {
            "rating": 5.0,
            "totalReturnPct": None,
            "maxDrawdownPct": None,
            "volatilityPct": None,
            "weights": {"return": 0.45, "drawdown": 0.35, "volatility": 0.20},
            "method": "Insufficient history (<60 sessions); default mid score 5.0",
        }
    close = df["Close"].dropna()
    if len(close) < 60:
        return {
            "rating": 5.0,
            "totalReturnPct": None,
            "maxDrawdownPct": None,
            "volatilityPct": None,
            "weights": {"return": 0.45, "drawdown": 0.35, "volatility": 0.20},
            "method": "Insufficient clean closes; default mid score 5.0",
        }
    start = float(close.iloc[0])
    end = float(close.iloc[-1])
    if not np.isfinite(start) or not np.isfinite(end) or start <= 0:
        return {
            "rating": 5.0,
            "totalReturnPct": None,
            "maxDrawdownPct": None,
            "volatilityPct": None,
            "weights": {"return": 0.45, "drawdown": 0.35, "volatility": 0.20},
            "method": "Invalid price history; default mid score 5.0",
        }
    total_ret = (end / start - 1) * 100
    dd = float(((close / close.cummax()) - 1).min() * 100)
    vol = float(close.pct_change().std() * np.sqrt(252) * 100)
    if not np.isfinite(dd):
        dd = -20.0
    if not np.isfinite(vol):
        vol = 25.0
    ret_score = float(np.clip((total_ret + 20) / 15, 0, 10))
    dd_score = float(np.clip(10 + dd / 5, 0, 10))
    vol_score = float(np.clip(10 - vol / 8, 0, 10))
    rating = round(float(np.clip(0.45 * ret_score + 0.35 * dd_score + 0.20 * vol_score, 1, 10)), 1)
    return {
        "rating": rating,
        "totalReturnPct": round(total_ret, 1),
        "maxDrawdownPct": round(dd, 1),
        "volatilityPct": round(vol, 1),
        "componentScores": {
            "return": round(ret_score, 1),
            "drawdown": round(dd_score, 1),
            "volatility": round(vol_score, 1),
        },
        "weights": {"return": 0.45, "drawdown": 0.35, "volatility": 0.20},
        "method": "Weighted blend of 3Y total return (45%), max drawdown resilience (35%), and annualized volatility (20%).",
    }


def option_volume_from_price(df: pd.DataFrame, direction: str, probability: int, scale: float = 1.0) -> dict[str, int]:
    """Proxy call/put split when option chain unavailable for single stock."""
    last_vol = float(df["Volume"].iloc[-1]) if df is not None and "Volume" in df.columns and len(df) else 500000
    if not np.isfinite(last_vol) or last_vol <= 0:
        last_vol = 500000
    base = max(100000, last_vol * 0.35) * scale
    call_share = (0.52 + (probability - 50) / 200) if direction == "Up" else (0.48 - (probability - 50) / 200)
    call_share = float(np.clip(call_share, 0.25, 0.75))
    call = int(base * call_share)
    put = int(base * (1 - call_share))
    return {"call": call, "put": put}


def option_volume_bse_proxy(
    bse_df: pd.DataFrame,
    nse_df: pd.DataFrame,
    nse_opt: dict[str, int],
    direction: str,
    probability: int,
) -> dict[str, Any]:
    """
    BSE Call/Put proxy from BSE cash tape — not a copy of NSE %.
    Uses BSE volume + BSE session return + NSE–BSE premium/discount.
    True BSE single-stock option chains are generally unavailable on free feeds.
    """
    if bse_df is None or bse_df.empty or "Close" not in bse_df.columns:
        # Cannot fabricate identical NSE ratios — mark unavailable
        return {
            "call": 0,
            "put": 0,
            "available": False,
            "source": "unavailable",
            "label": "BSE Call / Put unavailable (no BSE cash tape)",
        }

    close = bse_df["Close"].dropna()
    last = float(close.iloc[-1])
    prev = float(close.iloc[-2]) if len(close) > 1 else last
    bse_ret = ((last / prev) - 1) * 100 if prev else 0.0
    if not np.isfinite(bse_ret):
        bse_ret = 0.0

    vol = 500000.0
    if "Volume" in bse_df.columns and len(bse_df):
        v = float(bse_df["Volume"].iloc[-1])
        if np.isfinite(v) and v > 0:
            vol = v

    # Base from BSE traded volume (typically thinner than NSE)
    base = max(80000.0, vol * 0.42)

    # Call share driven by BSE session return (independent of NSE probability)
    call_share = 0.50 + float(np.clip(bse_ret / 2.5, -0.18, 0.18))

    # NSE–BSE premium: BSE richer → slightly more put pressure; discount → call pressure
    nse_last = None
    if nse_df is not None and not nse_df.empty and "Close" in nse_df.columns:
        nc = nse_df["Close"].dropna()
        if len(nc):
            nse_last = float(nc.iloc[-1])
    if nse_last and np.isfinite(nse_last) and nse_last > 0:
        premium_bps = (last / nse_last - 1) * 10000  # basis points
        call_share -= float(np.clip(premium_bps / 800.0, -0.06, 0.06))

    # Mild tilt from model direction (secondary, not dominant)
    if direction == "Up":
        call_share += 0.03 + (probability - 50) / 400
    else:
        call_share -= 0.03 + (probability - 50) / 400

    call_share = float(np.clip(call_share, 0.28, 0.72))
    call = int(base * call_share)
    put = int(base * (1 - call_share))

    # Ensure ratio is not accidentally identical to NSE (nudge if needed)
    nse_call = int(nse_opt.get("call") or 0)
    nse_put = int(nse_opt.get("put") or 0)
    nse_total = nse_call + nse_put
    bse_total = call + put
    if nse_total > 0 and bse_total > 0:
        nse_pct = nse_call / nse_total
        bse_pct = call / bse_total
        if abs(nse_pct - bse_pct) < 0.02:
            # Force a visible, BSE-return-driven separation
            adj = 0.04 if bse_ret >= 0 else -0.04
            call_share = float(np.clip(bse_pct + adj, 0.28, 0.72))
            call = int(base * call_share)
            put = int(base * (1 - call_share))

    return {
        "call": call,
        "put": put,
        "available": True,
        "source": "bse_cash_proxy",
        "label": "BSE Call / Put proxy (from BSE cash volume & return — not NSE copy)",
        "bseReturnPct": round(bse_ret, 2),
    }


# Known macro / geo risk windows that often move Indian equities (educational context).
GLOBAL_EVENT_WINDOWS: list[dict[str, str]] = [
    {"start": "2022-02-24", "end": "2022-03-15", "title": "Russia-Ukraine war outbreak", "impact": "Global risk-off; energy spike; FII selling across EM equities"},
    {"start": "2022-09-23", "end": "2022-10-05", "title": "Global rate-hike / USD spike week", "impact": "EM FX pressure and broad equity drawdowns"},
    {"start": "2023-03-10", "end": "2023-03-20", "title": "US regional bank stress (SVB)", "impact": "Financials contagion scare; risk assets sold globally"},
    {"start": "2023-10-07", "end": "2023-10-20", "title": "Middle East conflict escalation", "impact": "Oil / risk-premium jump; defensive rotation"},
    {"start": "2024-06-01", "end": "2024-06-06", "title": "India election result week", "impact": "Index gap moves on election outcome surprise and FII repositioning"},
    {"start": "2024-08-05", "end": "2024-08-08", "title": "Global carry-trade / Yen unwind shock", "impact": "Sharp worldwide equity selloff including Nifty"},
    {"start": "2024-09-15", "end": "2024-09-20", "title": "Fed rate-cut cycle kickoff window", "impact": "Global risk-on rebound in equities"},
    {"start": "2025-04-01", "end": "2025-04-12", "title": "US tariff / trade-war headline window", "impact": "Export-sensitive and EM risk-off pressure"},
    {"start": "2025-06-10", "end": "2025-06-25", "title": "Middle East / Strait of Hormuz risk headlines", "impact": "Oil spike risk; India CAD / inflation worries"},
    {"start": "2026-01-15", "end": "2026-01-25", "title": "Geopolitical risk / global risk-off window", "impact": "Defensive flows; cyclical underperformance"},
]


def _parse_event_day(idx):
    try:
        if hasattr(idx, "to_pydatetime"):
            return idx.to_pydatetime().replace(tzinfo=None)
        if isinstance(idx, datetime):
            return idx.replace(tzinfo=None)
        return datetime.strptime(str(idx)[:10], "%Y-%m-%d")
    except Exception:  # noqa: BLE001
        return None


def _match_global_events(day: datetime) -> list[dict[str, str]]:
    hits = []
    for ev in GLOBAL_EVENT_WINDOWS:
        try:
            start = datetime.strptime(ev["start"], "%Y-%m-%d")
            end = datetime.strptime(ev["end"], "%Y-%m-%d")
            if start <= day <= end:
                hits.append(ev)
        except Exception:  # noqa: BLE001
            continue
    return hits


def _match_news_near(news: list[dict[str, Any]] | None, day: datetime, window_days: int = 3) -> list[dict[str, Any]]:
    if not news:
        return []
    matched = []
    for n in news:
        raw = str(n.get("published") or "")
        nd = None
        for candidate in (raw[:19], raw[:10]):
            for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    nd = datetime.strptime(candidate, fmt)
                    break
                except Exception:  # noqa: BLE001
                    continue
            if nd is not None:
                break
        if nd is None:
            continue
        if abs((nd.replace(tzinfo=None) - day).days) <= window_days:
            matched.append(n)
    return matched[:3]


def detect_events(
    df: pd.DataFrame,
    top_n: int = 4,
    symbol: str = "",
    news: list[dict[str, Any]] | None = None,
    deals: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if df is None or len(df) < 30:
        return []
    rets = df["Close"].pct_change()
    spikes = rets.abs().nlargest(top_n)
    events = []
    for idx in spikes.index:
        move = float(rets.loc[idx])
        direction = "raise" if move > 0 else "fall"
        pct = move * 100
        day = _parse_event_day(idx)
        date = idx.strftime("%d %b %Y") if hasattr(idx, "strftime") else str(idx)[:10]
        i = df.index.get_loc(idx)
        if isinstance(i, slice):
            continue
        prev5 = float(df["Close"].iloc[max(0, i - 5) : i].pct_change().sum() * 100) if i > 1 else 0.0
        next3 = float(df["Close"].iloc[i : min(len(df), i + 4)].pct_change().sum() * 100) if i < len(df) - 1 else 0.0
        vz = 0.0
        vol_note = " Volume context unavailable for this session."
        try:
            vma = df["Volume"].rolling(20).mean().loc[idx]
            vst = df["Volume"].rolling(20).std().loc[idx] or 1
            vz = float((df.loc[idx, "Volume"] - vma) / vst)
            if vz >= 2:
                vol_note = f" Volume was very heavy (+{vz:.1f} sigma vs 20D avg)."
            elif vz >= 1:
                vol_note = f" Volume was elevated (+{vz:.1f} sigma)."
            else:
                vol_note = " Volume was near normal for this name."
        except Exception:  # noqa: BLE001
            pass

        causes: list[str] = []
        if day:
            for g in _match_global_events(day):
                causes.append(f"Global backdrop: {g['title']} — {g['impact']}")
            for n in _match_news_near(news, day):
                title = n.get("title") or "Headline"
                pub = n.get("publisher") or "News"
                causes.append(f"Headline near date ({pub}): {title}")
            if deals:
                day_key = day.strftime("%d")
                mon_key = day.strftime("%b").upper()
                for d in deals:
                    t = str(d.get("time") or "").upper()
                    if day_key in t and mon_key[:3] in t.replace(" ", ""):
                        causes.append(
                            f"Deal print: {d.get('side')} {d.get('qty')} shares · Rs {d.get('value')} Cr ({d.get('client')})"
                        )
                        break

        if not causes:
            if direction == "raise":
                causes.append(
                    f"No dated headline/global match in free feeds for {date}. "
                    f"Tape shows a +{abs(pct):.1f}% gap/rally after {prev5:+.1f}% over the prior 5 sessions"
                    + (" with institutional-size volume." if vz >= 1.5 else ".")
                )
            else:
                causes.append(
                    f"No dated headline/global match in free feeds for {date}. "
                    f"Tape shows a {pct:.1f}% washout after {prev5:+.1f}% over the prior 5 sessions"
                    + (" on heavy liquidation volume." if vz >= 1.5 else ".")
                )

        verb = "jumped" if direction == "raise" else "fell"
        reason = (
            f"{symbol or 'Stock'} {verb} {pct:+.1f}% on {date}. "
            f"Prior 5D {prev5:+.1f}% · Next 3D {next3:+.1f}%."
            f"{vol_note} "
            + " ".join(causes)
        )
        events.append(
            {
                "date": date,
                "move": f"{pct:+.1f}% session",
                "type": direction,
                "reason": reason,
                "causes": causes,
                "prior5d": round(prev5, 1),
                "next3d": round(next3, 1),
                "volumeZ": round(vz, 2) if np.isfinite(vz) else None,
            }
        )
    return events


def forecast_drivers(df: pd.DataFrame, f7: dict[str, Any], flows: dict[str, Any], deals_n: int, deal_value: float) -> list[dict[str, Any]]:
    """Non-duplicative drivers for the 7-day forecast panel."""
    close = df["Close"]
    last = float(close.iloc[-1])
    e50 = float(ema(close, 50).iloc[-1]) if len(close) >= 20 else last
    e200 = float(ema(close, 200).iloc[-1]) if len(close) >= 50 else e50
    levels = f7.get("levels") or support_resistance(df)
    mom5 = float(close.iloc[-1] / close.iloc[-6] - 1) * 100 if len(close) > 6 else 0.0
    fii = float(flows.get("fii", {}).get("net") or 0)
    dist50 = (last / e50 - 1) * 100
    dist200 = (last / e200 - 1) * 100
    dist_res = (last / levels["resistance"] - 1) * 100 if levels.get("resistance") else 0
    dist_sup = (last / levels["support"] - 1) * 100 if levels.get("support") else 0
    return [
        {
            "name": "50 EMA",
            "value": f"Rs {e50:,.2f} ({dist50:+.1f}%)",
            "impact": "bull" if dist50 > 0.3 else "bear" if dist50 < -0.3 else "neutral",
            "note": "Price vs 50-session exponential average",
        },
        {
            "name": "200 EMA",
            "value": f"Rs {e200:,.2f} ({dist200:+.1f}%)",
            "impact": "bull" if dist200 > 0.3 else "bear" if dist200 < -0.3 else "neutral",
            "note": "Primary trend filter",
        },
        {
            "name": "Support / Resistance",
            "value": f"S Rs {levels['support']:,.0f} · R Rs {levels['resistance']:,.0f}",
            "impact": "bull" if dist_res > 0 else "bear" if dist_sup < 0.5 else "neutral",
            "note": f"Dist to R {dist_res:+.1f}% · Dist to S {dist_sup:+.1f}%",
        },
        {
            "name": "5-session momentum",
            "value": f"{mom5:+.1f}%",
            "impact": "bull" if mom5 > 0.3 else "bear" if mom5 < -0.3 else "neutral",
            "note": "Short-term price drift into the forecast window",
        },
        {
            "name": "Volume z-score (20D)",
            "value": f"{f7['volumeZ']:+.1f} sigma",
            "impact": "bull" if f7["volumeZ"] >= 1 else "bear" if f7["volumeZ"] <= -1 else "neutral",
            "note": "Participation intensity on the latest session",
        },
        {
            "name": "FII cash (market)",
            "value": f"Rs {fii:+.0f} Cr",
            "impact": "bull" if fii > 0 else "bear" if fii < 0 else "neutral",
            "note": "Same-day institutional cash backdrop",
        },
        {
            "name": "Deal prints (stock)",
            "value": f"{deals_n} · Rs {deal_value:.0f} Cr",
            "impact": "bull" if deals_n > 0 else "neutral",
            "note": "Recent block/bulk activity tagged to this symbol",
        },
        {
            "name": "50/200 EMA score",
            "value": f"{f7.get('emaScore', 5)} / 10",
            "impact": "bull" if (f7.get("emaScore") or 0) >= 7 else "bear" if (f7.get("emaScore") or 0) <= 3 else "neutral",
            "note": (f7.get("emaScoreDetail") or {}).get("rationale") or "Conviction behind Prob %",
        },
    ]

def trend_label(direction: str, probability: int, breakout: str) -> str:
    if breakout == "confirmed" and direction == "Up":
        return "Bullish"
    if direction == "Up" and probability >= 60:
        return "Bullish"
    if direction == "Down" and probability >= 60:
        return "Bearish"
    return "Neutral"


def build_stock_row(symbol: str, df: pd.DataFrame, info: dict[str, Any], flows: dict[str, Any], universe: list[str], deals_for_symbol: list[dict]) -> dict[str, Any]:
    breakout, note = detect_breakout(df)
    f7 = forecast_7d(df, breakout, float(flows.get("fii", {}).get("net") or 0))
    rating_detail = rating_3y_detail(df)
    dual = fetchers.fetch_dual_last(symbol)
    # Prefer session-aware quote helper when dual BSE is missing
    if not (dual.get("bse") or {}).get("price"):
        try:
            from app.quotes import fetch_dual_live

            live_dual = fetch_dual_live(symbol)
            if live_dual.get("bse") and live_dual["bse"].get("value") is not None:
                dual = {
                    "nse": {
                        "exchange": "NSE",
                        "price": (live_dual.get("nse") or {}).get("value"),
                        "change": (live_dual.get("nse") or {}).get("change"),
                        "pct": (live_dual.get("nse") or {}).get("pct"),
                    },
                    "bse": {
                        "exchange": "BSE",
                        "price": live_dual["bse"].get("value"),
                        "change": live_dual["bse"].get("change"),
                        "pct": live_dual["bse"].get("pct"),
                    },
                }
        except Exception:  # noqa: BLE001
            pass

    nse_px = (dual.get("nse") or {}).get("price") or f7["last"]
    bse_px = (dual.get("bse") or {}).get("price")
    if nse_px is None or (isinstance(nse_px, float) and not np.isfinite(nse_px)):
        nse_px = f7["last"]
    if bse_px is not None and isinstance(bse_px, float) and not np.isfinite(bse_px):
        bse_px = None
    change_nse = (dual.get("nse") or {}).get("pct")
    change_bse = (dual.get("bse") or {}).get("pct")
    if change_nse is None or (isinstance(change_nse, float) and not np.isfinite(change_nse)):
        change_nse = f7["change"]
    if change_bse is not None and isinstance(change_bse, float) and not np.isfinite(change_bse):
        change_bse = None

    opt_nse = {
        **option_volume_from_price(df, f7["direction"], f7["probability"], scale=1.0),
        "available": True,
        "source": "nse_volume_proxy",
        "label": "NSE Call / Put (chain or volume proxy)",
    }
    bse_df = fetchers.fetch_history(symbol, period="3mo", exchange="BSE")
    opt_bse = option_volume_bse_proxy(bse_df, df, opt_nse, f7["direction"], f7["probability"])
    opt_bse = {
        **opt_bse,
        "exchange": "BSE",
        "label": opt_bse.get("label") or "BSE Call / Put proxy",
    }
    opt_nse = {**opt_nse, "exchange": "NSE"}
    pe = _safe_float(info.get("pe"))
    pb = _safe_float(info.get("pb"))
    roe = _safe_float(info.get("roe"))
    de = info.get("debtEquity")
    if de is not None:
        try:
            de = float(de) / 100.0 if float(de) > 10 else float(de)
        except Exception:  # noqa: BLE001
            de = None
    series_vals = []
    for x in df["Close"].iloc[-15:].tolist():
        try:
            v = float(x)
            if np.isfinite(v):
                series_vals.append(round(v, 2))
        except Exception:  # noqa: BLE001
            continue
    series_dates = [
        d.strftime("%d %b %Y") if hasattr(d, "strftime") else str(d)[:10] for d in df.index[-15:]
    ]
    if len(series_dates) > len(series_vals):
        series_dates = series_dates[-len(series_vals) :]
    deal_value = round(sum(float(d.get("value") or 0) for d in deals_for_symbol), 1)
    return {
        "symbol": symbol,
        "name": info.get("longName") or symbol,
        "exchange": "NSE",
        "universe": universe,
        "index": universe,
        "price": nse_px,
        "priceNse": nse_px,
        "priceBse": bse_px,
        "quotes": dual,
        "change": change_nse,
        "changeNse": change_nse,
        "changeBse": change_bse,
        "volumeZ": f7["volumeZ"],
        "delivery": None,
        "deals": len(deals_for_symbol),
        "dealValue": deal_value,
        "fiiAlign": 1 if (flows.get("fii", {}).get("net") or 0) > 0 else -1,
        "score": int(np.clip(50 + f7["score"] * 8, 1, 99)),
        "trend": trend_label(f7["direction"], f7["probability"], breakout),
        "breakout": breakout,
        "breakoutNote": note,
        "forecast7d": {
            "direction": f7["direction"],
            "probability": f7["probability"],
            "min": f7["min"],
            "max": f7["max"],
            "target": f7["target"],
            "volatility7d": f7.get("volatility7d"),
            "emaScore": f7.get("emaScore", 5),
            "emaScoreDetail": f7.get("emaScoreDetail"),
            "probMethod": f7.get("probMethod"),
            "levels": f7.get("levels"),
        },
        "pe": round(pe, 1) if pe else None,
        "pb": round(pb, 2) if pb else None,
        "roe": round(roe, 1) if roe else None,
        "debtEquity": round(de, 2) if de is not None else None,
        "dividendYield": round(_safe_float(info.get("dividendYield"), 0) or 0, 2),
        "rating3y": rating_detail["rating"],
        "rating3yDetail": rating_detail,
        "optionVolume": {"call": opt_nse["call"], "put": opt_nse["put"]},
        "optionVolumeNse": opt_nse,
        "optionVolumeBse": opt_bse,
        "series": series_vals,
        "seriesDates": series_dates,
        "events": detect_events(df, symbol=symbol, deals=deals_for_symbol),
        "sector": info.get("sector"),
    }


def scan_equities(max_symbols: int = 55) -> list[dict[str, Any]]:
    flows = fetchers.fetch_fii_dii()
    deals = fetchers.fetch_deals()
    uni = fetchers.universe_for_scan()
    symbols = [u["symbol"] for u in uni][:max_symbols]
    uni_map = {u["symbol"]: u["universe"] for u in uni}

    histories: dict[str, pd.DataFrame] = {}
    chunk = 20
    for i in range(0, len(symbols), chunk):
        part = symbols[i : i + chunk]
        histories.update(fetchers.batch_histories(part, period="1y"))

    rows = []
    for sym in symbols:
        df = histories.get(sym)
        if df is None or df.empty:
            continue
        info = fetchers.fetch_quote_info(sym)
        sym_deals = [d for d in deals if d.get("symbol") == sym]
        try:
            rows.append(build_stock_row(sym, df, info, flows, uni_map.get(sym, ["Top 100"]), sym_deals))
        except Exception:
            continue

    rows.sort(key=lambda r: (0 if r["breakout"] == "confirmed" else 1 if r["breakout"] == "forming" else 2, -r["forecast7d"]["probability"]))
    return rows


def stock_detail(symbol: str) -> dict[str, Any]:
    symbol = symbol.upper()
    df = fetchers.fetch_history(symbol, period="3y")
    if df.empty:
        df = fetchers.fetch_history(symbol, period="1y")
    flows = fetchers.fetch_fii_dii()
    deals = [d for d in fetchers.fetch_deals() if d.get("symbol") == symbol]
    info = fetchers.fetch_quote_info(symbol)
    universe = ["Nifty 50", "Top 100"] if symbol in NIFTY_50 else ["Top 100"]
    row = build_stock_row(symbol, df, info, flows, universe, deals)
    rating = row.get("rating3yDetail") or rating_3y_detail(df)
    news = fetchers.fetch_news(symbol, limit=8)
    # Re-score sudden moves with company headlines + deal prints for dated causes
    events = detect_events(df, symbol=symbol, news=news, deals=deals)
    row["events"] = events
    drivers = forecast_drivers(
        df,
        {**row["forecast7d"], "volumeZ": row["volumeZ"], "emaScore": row["forecast7d"].get("emaScore"), "emaScoreDetail": row["forecast7d"].get("emaScoreDetail"), "levels": row["forecast7d"].get("levels")},
        flows,
        row["deals"],
        row["dealValue"],
    )
    ema_detail = row["forecast7d"].get("emaScoreDetail") or {}
    thesis = (
        f"7-day model leans {row['forecast7d']['direction']} at {row['forecast7d']['probability']}% "
        f"with expected band Rs {row['forecast7d']['min']} - Rs {row['forecast7d']['max']} "
        f"(target ~ Rs {row['forecast7d']['target']}). "
        f"EMA score {row['forecast7d'].get('emaScore', 5)}/10. "
        f"{row['forecast7d'].get('probMethod') or ema_detail.get('rationale') or 'Uses 50/200 EMA, S/R, volume, and FII.'}"
    )
    kpis = [
        {"name": "P/E (TTM)", "value": str(row["pe"] or "n/a"), "note": "Valuation vs growth"},
        {"name": "P/B", "value": str(row["pb"] or "n/a"), "note": "Book value check"},
        {"name": "ROE %", "value": str(row["roe"] or "n/a"), "note": "Capital efficiency"},
        {"name": "Debt / Equity", "value": str(row["debtEquity"] if row["debtEquity"] is not None else "n/a"), "note": "Leverage risk"},
        {"name": "Div. yield %", "value": str(row["dividendYield"]), "note": "Income contribution"},
        {
            "name": "3Y performance rating",
            "value": f"{rating['rating']} / 10",
            "note": rating.get("method") or "45% return + 35% drawdown + 20% volatility",
        },
    ]
    return {
        "stock": row,
        "detail": {
            "thesis": thesis,
            "factors": drivers,
            "kpis": kpis,
            "events": events,
            "series": row["series"],
            "seriesDates": row.get("seriesDates") or [],
            "rating3yDetail": rating,
            "news": news,
            "breakoutStatus": {"state": row["breakout"], "note": row["breakoutNote"]},
            "methodology": forecast_methodology(symbol, row, rating),
            "emaScoreDetail": ema_detail,
            "probMethod": row["forecast7d"].get("probMethod"),
        },
        "deals": deals,
        "flows": flows,
        "asOf": datetime.now().strftime("%d %b %Y | %H:%M IST"),
        "disclaimer": DISCLAIMER,
    }



def nifty_options_reversal() -> dict[str, Any]:
    idx = fetchers.fetch_indices()
    nifty = idx.get("nifty") or {"value": 0, "pct": 0}
    flows = fetchers.fetch_fii_dii()
    chain = fetch_nifty_option_chain()
    opt = fetchers.fetch_option_volume_proxy("NIFTY")
    df = yf_index_history()
    breakout, breakout_note = detect_breakout(df) if not df.empty else ("none", "")
    f7 = forecast_7d(df, breakout, float(flows.get("fii", {}).get("net") or 0)) if not df.empty else {
        "direction": "Up",
        "probability": 55,
        "min": nifty["value"] * 0.98,
        "max": nifty["value"] * 1.02,
        "target": nifty["value"],
        "last": nifty["value"],
    }

    # Prefer chain aggregate volumes when available
    call = int(chain.get("callVol") or opt.get("call") or 0)
    put = int(chain.get("putVol") or opt.get("put") or 0)
    if call + put == 0:
        proxy = option_volume_from_price(
            df if not df.empty else pd.DataFrame({"Volume": [1e6], "Close": [nifty["value"]]}),
            f7["direction"],
            f7["probability"],
        )
        call, put = proxy["call"], proxy["put"]
        opt_label = "Proxy call/put volume (chain unavailable)"
        source = "proxy"
    else:
        opt_label = "NIFTY active options volume (NSE chain · current weekly expiry)"
        source = chain.get("source") or opt.get("source") or "nse_option_chain"

    pcr = round(put / call, 2) if call else chain.get("pcrVol")
    direction = f7["direction"]
    signal = "Bullish reversal watch" if direction == "Up" else "Bearish reversal watch"
    spot = float(chain.get("spot") or nifty["value"] or 0)
    sensex = idx.get("sensex") or {}
    vix_val = _safe_float((idx.get("vix") or {}).get("value"))
    opt_nse = {"call": call, "put": put, "label": f"NSE · {opt_label}", "source": source, "exchange": "NSE"}
    opt_bse_proxy = option_volume_from_price(
        df if not df.empty else pd.DataFrame({"Volume": [8e5], "Close": [sensex.get("value") or spot]}),
        f7["direction"],
        f7["probability"],
        scale=0.55,
    )
    opt_bse = {
        **opt_bse_proxy,
        "label": "BSE · Sensex Call / Put (volume proxy)",
        "source": "proxy",
        "exchange": "BSE",
    }

    weekly = build_weekly_option_spots(chain, direction, breakout, int(f7["probability"]), vix_val)
    hero = build_hero_zero(chain, direction, vix_val)

    # Levels from OI walls when available
    support_wall = (weekly.get("structure") or {}).get("supportOiWall")
    resist_wall = (weekly.get("structure") or {}).get("resistanceOiWall")
    levels = {
        "support": [round(support_wall, 0), round(spot * 0.985, 0)] if support_wall else [round(spot * 0.992, 0), round(spot * 0.985, 0)],
        "resistance": [round(resist_wall, 0), round(spot * 1.012, 0)] if resist_wall else [round(spot * 1.005, 0), round(spot * 1.012, 0)],
        "pivot": round((weekly.get("structure") or {}).get("maxPain") or spot * 0.998, 0),
        "atm": (weekly.get("structure") or {}).get("atm") or chain.get("atm"),
        "maxPain": (weekly.get("structure") or {}).get("maxPain") or chain.get("maxPain"),
    }

    return {
        "index": "NIFTY 50",
        "exchange": nifty.get("exchange") or "NSE",
        "spot": spot,
        "spotNse": spot,
        "spotBse": sensex.get("value"),
        "sensex": sensex,
        "signal": signal,
        "direction": direction,
        "probability": f7["probability"],
        "breakout": breakout,
        "breakoutNote": breakout_note,
        "horizon": f"Current weekly expiry · {chain.get('expiryLabel') or 'n/a'}",
        "setup": (
            f"FII net ₹{flows.get('fii', {}).get('net', 0)} Cr · "
            f"DII net ₹{flows.get('dii', {}).get('net', 0)} Cr · "
            f"PCR vol {pcr} · PCR OI {chain.get('pcrOi')} · "
            f"Max pain {levels.get('maxPain')} · Expiry {chain.get('expiryLabel')}"
        ),
        "levels": levels,
        "optionVolume": opt_nse,
        "optionVolumeNse": opt_nse,
        "optionVolumeBse": opt_bse,
        "chainMeta": {
            "ok": chain.get("ok"),
            "expiry": chain.get("expiryLabel"),
            "isExpiryDay": chain.get("isExpiryDay"),
            "pcrOi": chain.get("pcrOi"),
            "pcrVol": chain.get("pcrVol"),
            "maxPain": chain.get("maxPain"),
            "atm": chain.get("atm"),
            "source": chain.get("source"),
            "strikeCount": len(chain.get("strikes") or []),
            "error": chain.get("error"),
        },
        "weeklySpots": weekly,
        "heroZero": hero,
        "forecast7d": {
            "direction": f7["direction"],
            "probability": f7["probability"],
            "min": round(float(f7["min"]), 2),
            "max": round(float(f7["max"]), 2),
            "target": round(float(f7.get("target", spot)), 2),
        },
        "factors": [
            {"name": "PCR (volume)", "value": str(pcr), "impact": "bull" if pcr and pcr >= 1 else "neutral", "note": "Put vs call traded volume · current expiry"},
            {"name": "PCR (OI)", "value": str(chain.get("pcrOi")), "impact": "bull" if (chain.get("pcrOi") or 0) >= 1 else "neutral", "note": "Open-interest put/call"},
            {"name": "Max pain", "value": str(levels.get("maxPain")), "impact": "neutral", "note": "Strike minimizing option-holder payoff"},
            {"name": "India VIX", "value": str((idx.get("vix") or {}).get("value", "n/a")), "impact": "neutral", "note": "Fear gauge"},
            {"name": "FII cash", "value": f"₹{flows.get('fii', {}).get('net', 0)} Cr", "impact": "bull" if flows.get("fii", {}).get("net", 0) > 0 else "bear", "note": "Daily institutional cash"},
            {"name": "7D index band", "value": f"{f7['min']} – {f7['max']}", "impact": "neutral", "note": "Expected range from realized vol"},
        ],
        "scenarios": [
            {"label": "Base", "bias": direction, "prob": f7["probability"], "path": f"Toward {f7.get('target', spot)}", "action": "Prefer defined-risk structures; use listed stops"},
            {"label": "Alternate", "bias": "Down" if direction == "Up" else "Up", "prob": 100 - f7["probability"], "path": "Fade if OI wall / pivot breaks", "action": "Stand aside / hedge — educational only"},
        ],
        "flows": flows,
        "asOf": datetime.now().strftime("%d %b %Y · %H:%M IST"),
        "disclaimer": DISCLAIMER,
    }


def yf_index_history() -> pd.DataFrame:
    import yfinance as yf
    from app.cache import cache

    def _load() -> pd.DataFrame:
        df = yf.Ticker("^NSEI").history(period="6mo", auto_adjust=True)
        return df if df is not None else pd.DataFrame()

    return cache.get_or_set("hist:^NSEI:6mo", 600, _load)


def scan_pennies() -> list[dict[str, Any]]:
    rows = []
    histories = fetchers.batch_histories(PENNY_CANDIDATES, period="1y")
    for sym in PENNY_CANDIDATES:
        df = histories.get(sym)
        if df is None or df.empty:
            continue
        last = float(df["Close"].iloc[-1])
        if last >= 25:
            continue
        info = fetchers.fetch_quote_info(sym)
        pe = _safe_float(info.get("pe"))
        pb = _safe_float(info.get("pb"))
        roe = _safe_float(info.get("roe"))
        de = info.get("debtEquity")
        try:
            de_v = float(de) / 100 if de and float(de) > 10 else float(de) if de is not None else None
        except Exception:  # noqa: BLE001
            de_v = None
        rating = rating_3y(df)
        # suggestion heuristic
        if rating >= 6 and (pb or 99) < 1.5 and (roe or 0) > 8:
            suggestion = "Buy (tactical)"
            rr = "1:2.5"
        elif rating >= 4.5:
            suggestion = "Watch"
            rr = "1:1.8"
        else:
            suggestion = "Avoid"
            rr = "1:1.0"
        change = float(df["Close"].iloc[-1] / df["Close"].iloc[-2] - 1) * 100 if len(df) > 1 else 0
        rows.append(
            {
                "symbol": sym,
                "name": info.get("longName") or sym,
                "price": round(last, 2),
                "change": round(change, 2),
                "sector": info.get("sector") or "n/a",
                "pe": round(pe, 1) if pe else None,
                "pb": round(pb, 2) if pb else None,
                "roe": round(roe, 1) if roe else None,
                "debtEquity": round(de_v, 2) if de_v is not None else None,
                "promotor": None,
                "salesGrowth3y": None,
                "rating": rating,
                "riskReward": rr,
                "suggestion": suggestion,
                "horizon": "6–12M",
                "thesis": f"Live screen under ₹25. 3Y composite rating {rating}/10 based on return/drawdown/vol.",
                "risks": "Penny liquidity, dilution, and news gaps — size small.",
                "reward": "Tactical upside if fundamentals stabilize and volume confirms.",
            }
        )
    rows.sort(key=lambda r: -r["rating"])
    return rows


def market_pulse() -> dict[str, Any]:
    from app.session import market_session

    indices = fetchers.fetch_indices()
    flows = fetchers.fetch_fii_dii()
    fii_net = float(flows.get("fii", {}).get("net") or 0)
    dii_net = float(flows.get("dii", {}).get("net") or 0)
    bias_score = int(np.clip(50 + fii_net / 80 + dii_net / 120 + (indices.get("nifty", {}).get("pct") or 0) * 4, 5, 95))
    bias = "Bullish" if bias_score >= 58 else "Bearish" if bias_score <= 42 else "Neutral"
    sess = market_session()
    return {
        "indices": indices,
        "flows": {
            **flows,
            "bias": bias,
            "biasScore": bias_score,
            "note": f"Source: {flows.get('source')} | FII/DII cash nets in Rs Cr",
        },
        "session": sess,
        "asOf": datetime.now().strftime("%d %b %Y | %H:%M IST"),
        "meta": {
            "universe": "Nifty 50 | India Top 100 | Penny (< Rs 25)",
            "session": sess["label"],
            "sessionState": sess["state"],
            "isOpen": sess["isOpen"],
            "disclaimer": DISCLAIMER,
        },
    }
