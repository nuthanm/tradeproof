"""NSE Nifty option-chain fetch + weekly spot / Hero Zero helpers."""
from __future__ import annotations

import logging
import time
from datetime import date, datetime
from threading import Lock
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

# Sticky spot lock: entry/SL/targets stay fixed for the 1h validity window.
# Spots are dropped when live LTP leaves the entry zone (unreachable / stopped).
_SPOT_LOCK: dict[str, Any] = {"spots": [], "lockedAt": 0.0, "validUntil": 0.0, "hourDirection": None, "expiry": None}
_SPOT_LOCK_MU = Lock()
# Validity ≈ next 1 hour (4 × 15m candles). Entry chase tolerance 10% above locked entry.
SPOT_VALID_SECONDS = 60 * 60
ENTRY_CHASE_PCT = 0.10
PREMIUM_SL_PCT = 0.35
T1_R = 1.5
T2_R = 2.5


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
    hour_forecast: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Educational weekly-expiry option spots driven by the 15m → 1h direction model.

    - Entry / SL / targets are locked for ~1 hour (do not tick with every LTP refresh).
    - A spot is removed when live premium leaves the entry zone (chase >10% or ≤ stop).
    - Direction: Up → CE, Down → PE, Sideways → ATM defined-risk or empty if illiquid.
    """
    hour = hour_forecast or {}
    hour_dir = hour.get("direction") or (
        "Uptrend" if direction == "Up" else "Downtrend" if direction == "Down" else "Sideways"
    )
    hour_prob = int(hour.get("probability") or probability or 55)

    strikes = chain.get("strikes") or []
    spot = chain.get("spot")
    atm = chain.get("atm")
    max_pain = chain.get("maxPain")
    expiry_label = chain.get("expiryLabel")

    if not strikes or spot is None or atm is None:
        return {
            "active": False,
            "expiry": expiry_label,
            "method": "NSE chain unavailable — cannot propose spots without live OI/LTP.",
            "spots": [],
            "structure": {},
            "hourForecast": hour,
        }

    pe_wall = max(strikes, key=lambda s: s["pe"]["oi"])
    ce_wall = max(strikes, key=lambda s: s["ce"]["oi"])
    support = pe_wall["strike"]
    resistance = ce_wall["strike"]

    if hour_dir == "Uptrend":
        bias_note = (
            f"1h Uptrend ({hour_prob}%) from last {hour.get('barsUsed', 15)} × 15m candles "
            "→ liquid Calls near ATM / slight OTM. Entry & exits locked for the 1h window."
        )
        side = "CE"
    elif hour_dir == "Downtrend":
        bias_note = (
            f"1h Downtrend ({hour_prob}%) from last {hour.get('barsUsed', 15)} × 15m candles "
            "→ liquid Puts near ATM / slight OTM. Entry & exits locked for the 1h window."
        )
        side = "PE"
    else:
        bias_note = (
            f"1h Sideways ({hour_prob}%) — prefer stand aside or tight defined-risk ATM only. "
            "Spots auto-drop when premium leaves the locked entry zone."
        )
        side = "CE"  # ATM CE shown as defined-risk example; scoring still applies liquidity filters

    method = (
        "Model: last 15 × 15-minute Nifty candles → next-1-hour Uptrend / Sideways / Downtrend "
        "(ATR-normalized slope, 5/9/20 EMA stack, candle bias, HH/LL, volume, RSI9). "
        "Strikes ranked by ATM proximity, OI + rising ΔOI, volume, and OI-wall alignment. "
        "Entry/SL/targets lock for ~60 minutes and are NOT refreshed on every tick. "
        f"Premium stop = {int(PREMIUM_SL_PCT*100)}% of locked entry; targets = {T1_R}R / {T2_R}R. "
        f"Spot removed if live LTP > entry×{1+ENTRY_CHASE_PCT:.0%} (chase) or ≤ stop (invalidated). "
        "Educational only — time decay (theta) accelerates into expiry."
    )

    now = time.time()
    live_map = {(s["strike"], "CE"): s["ce"] for s in strikes}
    live_map.update({(s["strike"], "PE"): s["pe"] for s in strikes})

    def _still_reachable(locked: dict[str, Any]) -> bool:
        key = (float(locked["strike"]), locked["side"])
        leg = live_map.get(key)
        if not leg or leg.get("ltp", 0) <= 0:
            return False
        ltp = float(leg["ltp"])
        entry = float(locked["entry"])
        stop = float(locked["stopLoss"])
        ceil = float(locked.get("entryCeil") or entry * (1 + ENTRY_CHASE_PCT))
        # Out of reach: chased too far, or already at/through stop
        if ltp > ceil:
            return False
        if ltp <= stop:
            return False
        return True

    def _mint_candidates(prefer_side: str, limit: int = 3) -> list[dict[str, Any]]:
        window = [s for s in strikes if abs(s["strike"] - atm) <= 150]
        scored: list[tuple[float, dict[str, Any]]] = []

        def score_call(s: dict[str, Any]) -> float:
            ce = s["ce"]
            if ce["ltp"] <= 0 or ce["volume"] < 500:
                return -1e9
            moneyness = (s["strike"] - spot) / spot
            money_score = 10 - abs(moneyness - 0.002) * 800
            oi_score = min(ce["oi"] / 50000, 8)
            vol_score = min(ce["volume"] / 20000, 6)
            chg_score = 3 if ce["chgOi"] > 0 else (-2 if ce["chgOi"] < 0 else 0)
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

        for s in window:
            sc = score_call(s) if prefer_side == "CE" else score_put(s)
            if sc > -1e8:
                scored.append((sc, s))
        scored.sort(key=lambda x: -x[0])

        # Sideways: only keep the single most liquid ATM-ish strike
        take = 1 if hour_dir == "Sideways" else limit
        out: list[dict[str, Any]] = []
        for rank, (_, s) in enumerate(scored[:take], start=1):
            leg = s["ce"] if prefer_side == "CE" else s["pe"]
            entry = round(float(leg["ltp"]), 2)
            if entry <= 0:
                continue
            risk = round(entry * PREMIUM_SL_PCT, 2)
            stop = round(max(0.05, entry - risk), 2)
            t1 = round(entry + risk * T1_R, 2)
            t2 = round(entry + risk * T2_R, 2)
            index_invalid = support - 20 if prefer_side == "CE" else resistance + 20
            entry_ceil = round(entry * (1 + ENTRY_CHASE_PCT), 2)
            why = _explain_spot(
                side=prefer_side,
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
                probability=hour_prob,
                vix=vix,
                rank=rank,
                hour_dir=hour_dir,
                hour=hour,
            )
            out.append(
                {
                    "rank": rank,
                    "side": prefer_side,
                    "instrument": f"NIFTY {int(s['strike'])} {prefer_side}",
                    "strike": s["strike"],
                    "expiry": expiry_label,
                    "entry": entry,
                    "entryCeil": entry_ceil,
                    "entryFloor": stop,
                    "currentLtp": entry,
                    "stopLoss": stop,
                    "target1": t1,
                    "target2": t2,
                    "riskReward": f"1 : {T1_R} (T1) · 1 : {T2_R} (T2)",
                    "indexInvalidation": round(index_invalid, 0),
                    "oi": leg["oi"],
                    "chgOi": leg["chgOi"],
                    "volume": leg["volume"],
                    "pcrStrike": s.get("pcrOi"),
                    "why": why,
                    "confidence": "high" if rank == 1 and hour_prob >= 60 else "moderate",
                    "hourDirection": hour_dir,
                    "locked": True,
                    "validFor": hour.get("horizon") or "Next 1 hour",
                }
            )
        return out

    with _SPOT_LOCK_MU:
        locked_spots: list[dict[str, Any]] = list(_SPOT_LOCK.get("spots") or [])
        valid_until = float(_SPOT_LOCK.get("validUntil") or 0)
        locked_dir = _SPOT_LOCK.get("hourDirection")
        locked_expiry = _SPOT_LOCK.get("expiry")

        # Drop unreachable or expired locks
        refreshed: list[dict[str, Any]] = []
        window_alive = now < valid_until and locked_expiry == expiry_label
        # If strong opposite flip, force refresh
        opposite = (
            (locked_dir == "Uptrend" and hour_dir == "Downtrend")
            or (locked_dir == "Downtrend" and hour_dir == "Uptrend")
        )

        if window_alive and not opposite and locked_spots:
            for sp in locked_spots:
                if not _still_reachable(sp):
                    continue
                key = (float(sp["strike"]), sp["side"])
                leg = live_map.get(key) or {}
                # Keep locked prices; only refresh live LTP / OI / volume for display
                updated = dict(sp)
                updated["currentLtp"] = round(float(leg.get("ltp") or sp["entry"]), 2)
                updated["oi"] = int(leg.get("oi") or sp.get("oi") or 0)
                updated["chgOi"] = int(leg.get("chgOi") or sp.get("chgOi") or 0)
                updated["volume"] = int(leg.get("volume") or sp.get("volume") or 0)
                updated["locked"] = True
                remaining = max(0, int(valid_until - now))
                updated["validSecondsRemaining"] = remaining
                updated["validFor"] = f"{remaining // 60}m remaining · locked entry"
                refreshed.append(updated)

        if opposite or not window_alive or not refreshed:
            # Mint fresh locked set
            mint_side = side
            if hour_dir == "Sideways":
                # Prefer the side with better liquidity near ATM
                atm_row = min(strikes, key=lambda s: abs(s["strike"] - atm))
                ce_liq = atm_row["ce"]["volume"] + atm_row["ce"]["oi"]
                pe_liq = atm_row["pe"]["volume"] + atm_row["pe"]["oi"]
                mint_side = "CE" if ce_liq >= pe_liq else "PE"
            refreshed = _mint_candidates(mint_side, limit=3)
            for i, sp in enumerate(refreshed, start=1):
                sp["rank"] = i
                sp["validSecondsRemaining"] = SPOT_VALID_SECONDS
            _SPOT_LOCK["spots"] = refreshed
            _SPOT_LOCK["lockedAt"] = now
            _SPOT_LOCK["validUntil"] = now + SPOT_VALID_SECONDS
            _SPOT_LOCK["hourDirection"] = hour_dir
            _SPOT_LOCK["expiry"] = expiry_label
        else:
            # Re-rank remaining locked spots
            for i, sp in enumerate(refreshed, start=1):
                sp["rank"] = i
            _SPOT_LOCK["spots"] = refreshed

        candidates = refreshed

    return {
        "active": bool(candidates),
        "expiry": expiry_label,
        "isExpiryDay": chain.get("isExpiryDay", False),
        "method": method,
        "biasNote": bias_note,
        "hourForecast": {
            "direction": hour_dir,
            "probability": hour_prob,
            "horizon": hour.get("horizon"),
            "drivers": hour.get("drivers") or [],
            "barsUsed": hour.get("barsUsed"),
            "expectedMovePct": hour.get("expectedMovePct"),
        },
        "lockMeta": {
            "lockedAt": datetime.fromtimestamp(_SPOT_LOCK["lockedAt"]).strftime("%H:%M:%S") if _SPOT_LOCK.get("lockedAt") else None,
            "validUntil": datetime.fromtimestamp(_SPOT_LOCK["validUntil"]).strftime("%H:%M:%S") if _SPOT_LOCK.get("validUntil") else None,
            "policy": "Entry/SL/targets fixed until window ends or LTP leaves entry zone",
        },
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
    hour_dir: str = "Sideways",
    hour: dict[str, Any] | None = None,
) -> str:
    hour = hour or {}
    otm = strike >= spot if side == "CE" else strike <= spot
    loc = "ATM" if abs(strike - atm) < 1 else ("slight OTM" if otm else "slight ITM")
    bits = [
        f"Rank #{rank}: {side} {int(strike)} is {loc} vs spot {spot:.0f} (ATM {atm:.0f}).",
        f"1h model: {hour_dir} at {probability}% from last {hour.get('barsUsed', 15)} × 15m candles.",
        f"Liquidity: OI {leg['oi']:,} · ΔOI {leg['chgOi']:+,} · volume {leg['volume']:,}.",
        f"Index structure: breakout={breakout}. OI walls PE {support:.0f} / CE {resistance:.0f}.",
    ]
    if hour.get("expectedMovePct"):
        bits.append(f"Expected ~1h index move ≈ {hour['expectedMovePct']}% (ATR-based).")
    if max_pain:
        bits.append(f"Max pain ≈ {max_pain:.0f} — weekly pinning gravity.")
    if pcr_strike is not None:
        bits.append(f"Strike PCR (OI) {pcr_strike}.")
    if pcr_oi is not None:
        bits.append(f"Chain PCR (OI) {pcr_oi}.")
    if vix is not None:
        bits.append(f"India VIX {vix} — {'calm' if vix < 14 else 'elevated'} premium / theta backdrop.")
    bits.append(
        f"Entry/SL/targets are locked for the 1h window. "
        f"Removed automatically if LTP leaves the entry zone (>{int(ENTRY_CHASE_PCT*100)}% chase or ≤ stop). "
        "Respect index invalidation and time decay."
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
    daily = (row.get("forecast7d") or {}).get("daily") or []
    return {
        "title": "How this forecast is derived",
        "purpose": "Educational research model — not a prediction guarantee.",
        "datasets": [
            {
                "name": "NSE / BSE session prices (Yahoo .NS / .BO)",
                "use": "OHLCV history for 50/200 EMA, support/resistance, volume z-score, breakout structure, and per-day 7-session bands.",
            },
            {
                "name": "FII / DII cash nets (public daily flow feed)",
                "use": "Institutional cash bias tilt in the short-term score.",
            },
            {
                "name": "Block / bulk deals (NSE public reports when available)",
                "use": "Large-print confirmation / caution for the named stock (last 7 days).",
            },
            {
                "name": "Fundamentals snapshot (Yahoo quote info)",
                "use": "P/E, P/B, ROE, leverage, dividend yield for KPI context — not for the 7D direction itself.",
            },
            {
                "name": "Company news headlines (Yahoo news when available)",
                "use": "Last 7 days only, newest first; context for sudden moves — not an NLP trading signal.",
            },
        ],
        "approach": [
            "Compute 50 EMA and 200 EMA; map price vs support/resistance (20D/55D swings) into confirmed / forming / range-bound.",
            "Derive Up/Down Prob % from EMA stack, distance to S/R, breakout state, 5D/20D momentum, volume z-score, and FII net (about 45–92%).",
            "Publish a separate 50/200 EMA score (1–10): 1 = weak chance the Prob % holds, 10 ≈ near sure-shot conviction (~99% model confidence).",
            "Build next-7-trading-day table: each session gets expected low/high, trend, probability, and a reason (vol expands with √day; conviction fades).",
            "Explain drivers with EMA levels, S/R, volume, flows, and deals — not black-box labels.",
            f"3Y rating for {symbol}: {rating.get('method', 'return / drawdown / volatility blend')}.",
        ],
        "currentOutput": {
            "direction": row.get("forecast7d", {}).get("direction"),
            "probability": row.get("forecast7d", {}).get("probability"),
            "emaScore": row.get("forecast7d", {}).get("emaScore"),
            "dailySessions": len(daily),
            "day1": f"{daily[0].get('date')} · {daily[0].get('trend')} {daily[0].get('probability')}% · ₹{daily[0].get('min')}–₹{daily[0].get('max')}" if daily else None,
            "breakout": row.get("breakout"),
            "probMethod": row.get("forecast7d", {}).get("probMethod"),
        },
        "limits": [
            "Free public data can be delayed or missing (especially option chains and BSE depth).",
            "Probabilities are model scores, not historical hit-rates for this exact name.",
            "Corporate actions, gaps, and overnight news can invalidate the band quickly.",
        ],
    }
