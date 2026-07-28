"""NSE Nifty option-chain fetch + weekly spot / Hero Zero helpers."""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

from app.cache import cache

log = logging.getLogger("tradeproof.option_chain")

DISCLAIMER = (
    "TradeProof is for educational and research purposes only. "
    "It is not investment advice, not a buy/sell recommendation, and not a registered advisory service. "
    "Signals, probabilities, option strikes, stop-losses, and targets are illustrative model outputs — "
    "not guarantees. Market data from free public sources may be delayed, incomplete, or incorrect. "
    "You alone are responsible for any trading decision. Do not treat this website as a substitute for "
    "licensed financial advice."
)


def _parse_expiry(raw: Any) -> date | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        # NSE sometimes sends epoch ms
        try:
            ts = int(raw)
            if ts > 1_000_000_000_000:
                ts //= 1000
            return datetime.utcfromtimestamp(ts).date()
        except Exception:  # noqa: BLE001
            return None
    s = str(raw).strip()
    for fmt in ("%d-%b-%Y", "%d-%b-%y", "%Y-%m-%d", "%d %b %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _side(row: dict[str, Any], key: str) -> dict[str, Any]:
    side = row.get(key) or {}
    if not isinstance(side, dict):
        return {}
    return {
        "oi": int(side.get("openInterest") or 0),
        "chgOi": int(side.get("changeinOpenInterest") or 0),
        "volume": int(side.get("totalTradedVolume") or 0),
        "ltp": float(side.get("lastPrice") or 0),
        "chgPct": float(side.get("pchangeinOpenInterest") or side.get("pChange") or 0),
        "iv": float(side.get("impliedVolatility") or 0) if side.get("impliedVolatility") is not None else None,
        "bid": float(side.get("bidprice") or 0),
        "ask": float(side.get("askPrice") or 0),
    }


def fetch_nifty_option_chain(prefer_expiry: date | None = None) -> dict[str, Any]:
    """
    Full NSE Nifty option chain for the nearest (or preferred) weekly expiry.
    Uses nselib's NSE session helper (more reliable than a bare HTTP call).
    """

    def _load() -> dict[str, Any]:
        empty = {
            "ok": False,
            "source": "none",
            "spot": None,
            "expiry": None,
            "expiryLabel": None,
            "isExpiryDay": False,
            "strikes": [],
            "callVol": 0,
            "putVol": 0,
            "callOi": 0,
            "putOi": 0,
            "pcrVol": None,
            "pcrOi": None,
            "maxPain": None,
            "atm": None,
            "error": "unavailable",
        }
        try:
            from nselib import derivatives
            from nselib.derivatives.derivative_data import get_nse_option_chain

            exp_map = derivatives.expiry_dates_option_index() or {}
            expiry_labels = exp_map.get("NIFTY") or []
            expiries = [d for d in (_parse_expiry(x) for x in expiry_labels) if d]
            today = date.today()
            target_expiry = prefer_expiry
            if target_expiry is None:
                future = [d for d in expiries if d >= today]
                target_expiry = future[0] if future else (expiries[0] if expiries else None)
            if target_expiry is None:
                empty["error"] = "No NIFTY expiry dates from NSE"
                return empty

            # nselib expects dd-Mmm-YYYY (e.g. 28-Jul-2026)
            expiry_str = target_expiry.strftime("%d-%b-%Y")
            resp = get_nse_option_chain("NIFTY", expiry_str)
            if not getattr(resp, "ok", False):
                empty["error"] = f"HTTP {getattr(resp, 'status_code', '?')}"
                return empty
            payload = resp.json()
            records = payload.get("records") or {}
            data = records.get("data") or []
            # Prefer filtered (selected expiry) when present
            filtered = (payload.get("filtered") or {}).get("data")
            if filtered:
                data = filtered

            spot = float(records.get("underlyingValue") or 0) or None
            strikes: list[dict[str, Any]] = []
            call_vol = put_vol = call_oi = put_oi = 0
            for row in data:
                # Some rows only carry CE or PE; strike always present
                strike = float(row.get("strikePrice") or (row.get("CE") or {}).get("strikePrice") or (row.get("PE") or {}).get("strikePrice") or 0)
                if strike <= 0:
                    continue
                # Skip far expiries if mixed in
                ed = _parse_expiry(row.get("expiryDate") or (row.get("CE") or {}).get("expiryDate") or (row.get("PE") or {}).get("expiryDate"))
                if ed and target_expiry and ed != target_expiry:
                    # Also accept dd-mm-YYYY same calendar day
                    if ed != target_expiry:
                        continue
                ce = _side(row, "CE")
                pe = _side(row, "PE")
                call_vol += ce["volume"]
                put_vol += pe["volume"]
                call_oi += ce["oi"]
                put_oi += pe["oi"]
                pcr_strike = round(pe["oi"] / ce["oi"], 2) if ce["oi"] else None
                strikes.append({"strike": strike, "ce": ce, "pe": pe, "pcrOi": pcr_strike})

            merged: dict[float, dict[str, Any]] = {}
            for s in strikes:
                k = s["strike"]
                if k not in merged or (s["ce"]["oi"] + s["pe"]["oi"]) > (merged[k]["ce"]["oi"] + merged[k]["pe"]["oi"]):
                    merged[k] = s
            strikes = [merged[k] for k in sorted(merged)]

            max_pain = _max_pain(strikes)
            atm = None
            if spot and strikes:
                atm = min(strikes, key=lambda x: abs(x["strike"] - spot))["strike"]

            is_expiry_day = bool(target_expiry == today)
            return {
                "ok": bool(strikes),
                "source": "nse_option_chain",
                "spot": round(spot, 2) if spot else None,
                "expiry": target_expiry.isoformat(),
                "expiryLabel": target_expiry.strftime("%d %b %Y"),
                "isExpiryDay": is_expiry_day,
                "allExpiries": [d.strftime("%d %b %Y") for d in expiries[:6]],
                "strikes": strikes,
                "callVol": call_vol,
                "putVol": put_vol,
                "callOi": call_oi,
                "putOi": put_oi,
                "pcrVol": round(put_vol / call_vol, 2) if call_vol else None,
                "pcrOi": round(put_oi / call_oi, 2) if call_oi else None,
                "maxPain": max_pain,
                "atm": atm,
                "error": None if strikes else "No strikes for selected expiry",
            }
        except Exception as exc:  # noqa: BLE001
            log.warning("Nifty option chain parse failed: %s", exc)
            empty["error"] = str(exc)
            return empty

    return cache.get_or_set("nifty:option_chain:weekly", 180, _load)


def _max_pain(strikes: list[dict[str, Any]]) -> float | None:
    if not strikes:
        return None
    levels = [s["strike"] for s in strikes]
    best = None
    best_pain = None
    for level in levels:
        pain = 0.0
        for s in strikes:
            k = s["strike"]
            # Call holders lose if spot settles below strike; writers' pain ≈ OI * intrinsic
            pain += s["ce"]["oi"] * max(0.0, level - k)
            pain += s["pe"]["oi"] * max(0.0, k - level)
        if best_pain is None or pain < best_pain:
            best_pain = pain
            best = level
    return best


def build_weekly_option_spots(
    chain: dict[str, Any],
    direction: str,
    breakout: str,
    probability: int,
    vix: float | None,
) -> dict[str, Any]:
    """
    Pick educational weekly-expiry option spots from OI walls, ATM, PCR, volume, and trend bias.
    Stop-loss / targets are strict premium rules for illustration only.
    """
    strikes = chain.get("strikes") or []
    spot = chain.get("spot")
    atm = chain.get("atm")
    max_pain = chain.get("maxPain")
    if not strikes or spot is None or atm is None:
        return {
            "active": False,
            "expiry": chain.get("expiryLabel"),
            "method": "NSE chain unavailable — cannot propose spots without live OI/LTP.",
            "spots": [],
            "structure": {},
        }

    # OI walls
    pe_wall = max(strikes, key=lambda s: s["pe"]["oi"])
    ce_wall = max(strikes, key=lambda s: s["ce"]["oi"])
    support = pe_wall["strike"]
    resistance = ce_wall["strike"]

    bias = direction
    if breakout == "confirmed" and direction == "Up":
        bias_note = "Confirmed index breakout + Up bias → prefer liquid Calls near ATM / slight OTM."
    elif breakout == "confirmed" and direction == "Down":
        bias_note = "Confirmed downside break + Down bias → prefer liquid Puts near ATM / slight OTM."
    elif direction == "Up":
        bias_note = "Up bias without confirmed breakout → cautious Calls; respect CE OI wall as resistance."
    else:
        bias_note = "Down bias → cautious Puts; respect PE OI wall as support."

    # Candidate window: ATM ± 150 for weekly
    window = [s for s in strikes if abs(s["strike"] - atm) <= 150]
    candidates: list[dict[str, Any]] = []

    def score_call(s: dict[str, Any]) -> float:
        ce = s["ce"]
        if ce["ltp"] <= 0 or ce["volume"] < 500:
            return -1e9
        # Prefer mild OTM/ATM for weekly directional; reward rising OI + volume
        moneyness = (s["strike"] - spot) / spot
        # Ideal slight OTM call ~0 to +0.6%
        money_score = 10 - abs(moneyness - 0.002) * 800
        oi_score = min(ce["oi"] / 50000, 8)
        vol_score = min(ce["volume"] / 20000, 6)
        chg_score = 3 if ce["chgOi"] > 0 else (-2 if ce["chgOi"] < 0 else 0)
        # Prefer below CE wall
        wall_score = 2 if s["strike"] <= resistance else -3
        return money_score + oi_score + vol_score + chg_score + wall_score

    def score_put(s: dict[str, Any]) -> float:
        pe = s["pe"]
        if pe["ltp"] <= 0 or pe["volume"] < 500:
            return -1e9
        moneyness = (spot - s["strike"]) / spot
        money_score = 10 - abs(moneyness - 0.002) * 800
        oi_score = min(pe["oi"] / 50000, 8)
        vol_score = min(pe["volume"] / 20000, 6)
        chg_score = 3 if pe["chgOi"] > 0 else (-2 if pe["chgOi"] < 0 else 0)
        wall_score = 2 if s["strike"] >= support else -3
        return money_score + oi_score + vol_score + chg_score + wall_score

    side = "CE" if bias == "Up" else "PE"
    scored = []
    for s in window:
        sc = score_call(s) if side == "CE" else score_put(s)
        if sc > -1e8:
            scored.append((sc, s))
    scored.sort(key=lambda x: -x[0])

    # Also add one hedge/counter spot of opposite side at OI wall for education
    top = [s for _, s in scored[:3]]

    for rank, s in enumerate(top, start=1):
        leg = s["ce"] if side == "CE" else s["pe"]
        entry = round(leg["ltp"], 2)
        if entry <= 0:
            continue
        # Strict premium risk: SL 35% of premium; targets at 1.5R and 2.5R
        risk = round(entry * 0.35, 2)
        stop = round(max(0.05, entry - risk), 2)
        t1 = round(entry + risk * 1.5, 2)
        t2 = round(entry + risk * 2.5, 2)
        index_invalid = support - 20 if side == "CE" else resistance + 20
        why = _explain_spot(
            side=side,
            strike=s["strike"],
            spot=spot,
            atm=atm,
            max_pain=max_pain,
            support=support,
            resistance=resistance,
            leg=leg,
            pcr_strike=s.get("pcrOi"),
            pcr_oi=chain.get("pcrOi"),
            breakout=breakout,
            probability=probability,
            vix=vix,
            rank=rank,
        )
        candidates.append(
            {
                "rank": rank,
                "side": side,
                "instrument": f"NIFTY {int(s['strike'])} {side}",
                "strike": s["strike"],
                "expiry": chain.get("expiryLabel"),
                "entry": entry,
                "stopLoss": stop,
                "target1": t1,
                "target2": t2,
                "riskReward": "1 : 1.5 (T1) · 1 : 2.5 (T2)",
                "indexInvalidation": round(index_invalid, 0),
                "oi": leg["oi"],
                "chgOi": leg["chgOi"],
                "volume": leg["volume"],
                "pcrStrike": s.get("pcrOi"),
                "why": why,
                "confidence": "high" if rank == 1 and probability >= 60 else "moderate",
            }
        )

    method = (
        "Current weekly expiry NSE chain. Spots ranked by: (1) ATM proximity for weekly delta, "
        "(2) open interest + rising OI, (3) traded volume liquidity, (4) alignment with breakout/trend bias, "
        "(5) respect for PE support / CE resistance OI walls, (6) distance vs max-pain. "
        "Premium stop-loss is a strict 35% of entry LTP; targets use 1.5R / 2.5R. Educational only."
    )

    return {
        "active": True,
        "expiry": chain.get("expiryLabel"),
        "isExpiryDay": chain.get("isExpiryDay", False),
        "method": method,
        "biasNote": bias_note,
        "structure": {
            "spot": spot,
            "atm": atm,
            "maxPain": max_pain,
            "supportOiWall": support,
            "resistanceOiWall": resistance,
            "pcrOi": chain.get("pcrOi"),
            "pcrVol": chain.get("pcrVol"),
            "vix": vix,
        },
        "spots": candidates,
    }


def _explain_spot(
    *,
    side: str,
    strike: float,
    spot: float,
    atm: float,
    max_pain: float | None,
    support: float,
    resistance: float,
    leg: dict[str, Any],
    pcr_strike: float | None,
    pcr_oi: float | None,
    breakout: str,
    probability: int,
    vix: float | None,
    rank: int,
) -> str:
    otm = strike >= spot if side == "CE" else strike <= spot
    loc = "ATM" if abs(strike - atm) < 1 else ("slight OTM" if otm else "slight ITM")
    bits = [
        f"Rank #{rank}: {side} {int(strike)} is {loc} vs spot {spot:.0f} (ATM {atm:.0f}).",
        f"Liquidity: OI {leg['oi']:,} · ΔOI {leg['chgOi']:+,} · volume {leg['volume']:,}.",
        f"Trend context: breakout={breakout}, model bias probability {probability}%.",
        f"OI walls: PE support {support:.0f} · CE resistance {resistance:.0f}.",
    ]
    if max_pain:
        bits.append(f"Max pain ≈ {max_pain:.0f} — weekly pinning gravity for expiry week.")
    if pcr_strike is not None:
        bits.append(f"Strike PCR (OI) {pcr_strike}.")
    if pcr_oi is not None:
        bits.append(f"Chain PCR (OI) {pcr_oi}.")
    if vix is not None:
        bits.append(f"India VIX {vix} — {'calm premium' if vix < 14 else 'elevated premium'} backdrop.")
    bits.append(
        "Stop is a strict 35% premium cut; exit also if index breaks the stated invalidation level."
    )
    return " ".join(bits)


def build_hero_zero(chain: dict[str, Any], direction: str, vix: float | None) -> dict[str, Any]:
    """
    Hero Zero: educational expiry-day section only.
    Looks for cheap OTM premiums with volume/OI interest near the money.
    """
    if not chain.get("isExpiryDay"):
        return {
            "active": False,
            "reason": f"Hero Zero unlocks only on weekly expiry day. Next/current expiry: {chain.get('expiryLabel') or 'n/a'}.",
            "spots": [],
        }

    strikes = chain.get("strikes") or []
    spot = chain.get("spot")
    atm = chain.get("atm")
    if not strikes or spot is None or atm is None:
        return {"active": False, "reason": "Chain data missing on expiry day.", "spots": []}

    side = "CE" if direction == "Up" else "PE"
    # Hero-zero style: OTM, LTP typically low (₹5–₹80), volume spike
    picks: list[tuple[float, dict]] = []
    for s in strikes:
        dist = abs(s["strike"] - spot)
        if dist < 20 or dist > 250:
            continue
        leg = s["ce"] if side == "CE" else s["pe"]
        ltp = leg["ltp"]
        if ltp < 5 or ltp > 80:
            continue
        if side == "CE" and s["strike"] < spot:
            continue
        if side == "PE" and s["strike"] > spot:
            continue
        if leg["volume"] < 1000 and leg["oi"] < 20000:
            continue
        score = (leg["volume"] / 1000) + (leg["oi"] / 25000) + (3 if leg["chgOi"] > 0 else 0) - abs(dist - 100) / 50
        # Prefer cheaper tickets slightly
        score += max(0, (40 - ltp) / 20)
        picks.append((score, s))

    picks.sort(key=lambda x: -x[0])
    spots = []
    for rank, (_, s) in enumerate(picks[:3], start=1):
        leg = s["ce"] if side == "CE" else s["pe"]
        entry = round(leg["ltp"], 2)
        # Expiry-day theta is brutal — stricter 50% SL; targets 2x / 3x entry
        stop = round(max(0.05, entry * 0.50), 2)
        t1 = round(entry * 2.0, 2)
        t2 = round(entry * 3.0, 2)
        spots.append(
            {
                "rank": rank,
                "side": side,
                "instrument": f"NIFTY {int(s['strike'])} {side}",
                "strike": s["strike"],
                "expiry": chain.get("expiryLabel"),
                "idealEntry": entry,
                "entryNote": "Enter only if LTP is at/near this print with rising volume; skip if premium already spiked.",
                "stopLoss": stop,
                "target1": t1,
                "target2": t2,
                "oi": leg["oi"],
                "volume": leg["volume"],
                "chgOi": leg["chgOi"],
                "why": (
                    f"Expiry-day Hero Zero candidate: cheap {side} {int(s['strike'])} "
                    f"(LTP ₹{entry}) with volume {leg['volume']:,} and OI {leg['oi']:,}. "
                    f"Aligned with {direction} bias vs spot {spot:.0f}. "
                    f"Max loss risk is high on expiry — 50% premium stop is mandatory in this framework. "
                    f"VIX {vix if vix is not None else 'n/a'}."
                ),
            }
        )

    return {
        "active": True,
        "expiry": chain.get("expiryLabel"),
        "warning": (
            "Hero Zero is an educational expiry-day framework only. Most OTM options expire worthless. "
            "Never size as if returns are probable. Strict stop-loss means accepting frequent full/partial losses."
        ),
        "method": (
            "Filters current weekly expiry OTM strikes with low premium (₹5–₹80), "
            "meaningful volume/OI, and alignment to the session bias. "
            "Ideal entry = latest LTP; stop = 50% of entry; targets = 2× / 3× entry."
        ),
        "spots": spots,
    }


def forecast_methodology(symbol: str, row: dict[str, Any], rating: dict[str, Any]) -> dict[str, Any]:
    """Transparent explanation of equity 7D forecast inputs and approach."""
    return {
        "title": "How this forecast is derived",
        "purpose": "Educational research model — not a prediction guarantee.",
        "datasets": [
            {
                "name": "NSE / BSE session prices (Yahoo .NS / .BO)",
                "use": "OHLCV history for 50/200 EMA, support/resistance, volume z-score, breakout structure, and 7D band.",
            },
            {
                "name": "FII / DII cash nets (public daily flow feed)",
                "use": "Institutional cash bias tilt in the short-term score.",
            },
            {
                "name": "Block / bulk deals (NSE public reports when available)",
                "use": "Large-print confirmation / caution for the named stock.",
            },
            {
                "name": "Fundamentals snapshot (Yahoo quote info)",
                "use": "P/E, P/B, ROE, leverage, dividend yield for KPI context — not for the 7D direction itself.",
            },
            {
                "name": "Company news headlines (Yahoo news when available)",
                "use": "Context for sudden moves; not an NLP trading signal.",
            },
        ],
        "approach": [
            "Compute 50 EMA and 200 EMA; map price vs support/resistance (20D/55D swings) into confirmed / forming / range-bound.",
            "Derive Up/Down Prob % from EMA stack, distance to S/R, breakout state, 5D/20D momentum, volume z-score, and FII net (about 45–92%).",
            "Publish a separate 50/200 EMA score (1–10): 1 = weak chance the Prob % holds, 10 ≈ near sure-shot conviction (~99% model confidence).",
            "Build expected min/max from realized volatility × √7 with directional skew.",
            "Explain drivers with EMA levels, S/R, volume, flows, and deals — not black-box labels.",
            f"3Y rating for {symbol}: {rating.get('method', 'return / drawdown / volatility blend')}.",
        ],
        "currentOutput": {
            "direction": row.get("forecast7d", {}).get("direction"),
            "probability": row.get("forecast7d", {}).get("probability"),
            "emaScore": row.get("forecast7d", {}).get("emaScore"),
            "band": f"₹{row.get('forecast7d', {}).get('min')} – ₹{row.get('forecast7d', {}).get('max')}",
            "breakout": row.get("breakout"),
            "probMethod": row.get("forecast7d", {}).get("probMethod"),
        },
        "limits": [
            "Free public data can be delayed or missing (especially option chains and BSE depth).",
            "Probabilities are model scores, not historical hit-rates for this exact name.",
            "Corporate actions, gaps, and overnight news can invalidate the band quickly.",
        ],
    }
