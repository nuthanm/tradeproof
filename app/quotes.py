"""Live / session-close quotes for indices and equities."""

from __future__ import annotations



import logging

import re

from datetime import date, datetime

from typing import Any



import numpy as np

import requests

import yfinance as yf



from app.cache import cache

from app.session import cache_ttl_seconds, expected_last_trading_day, market_session, now_ist

from app.universe import INDEX_YF



log = logging.getLogger("tradeproof.quotes")



_BSE_SESSION = requests.Session()

_BSE_SESSION.headers.update(

    {

        "User-Agent": (

            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "

            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

        ),

        "Referer": "https://www.bseindia.com/",

        "Accept": "application/json,text/plain,*/*",

    }

)





def _finite(x: Any, default: float | None = None) -> float | None:

    try:

        v = float(x)

        return v if np.isfinite(v) else default

    except Exception:  # noqa: BLE001

        return default





def _parse_bse_ason(ason: str) -> date | None:

    """Parse BSE `Ason` like '27 Jul 26 | 16:00'."""

    if not ason:

        return None

    m = re.match(r"(\d{1,2})\s+([A-Za-z]{3})\s+(\d{2,4})", ason.strip())

    if not m:

        return None

    day, mon, yr = m.groups()

    yr = int(yr)

    if yr < 100:

        yr += 2000

    try:

        return datetime.strptime(f"{day} {mon} {yr}", "%d %b %Y").date()

    except ValueError:

        return None





def _daily_bars(ticker: str):

    df = yf.Ticker(ticker).history(period="10d", interval="1d", auto_adjust=True)

    if df is None or df.empty:

        return None

    df = df.dropna(subset=["Close"])

    return df if not df.empty else None





def _intraday_last(ticker: str) -> float | None:

    try:

        df = yf.Ticker(ticker).history(period="1d", interval="1m", auto_adjust=True)

        if df is None or df.empty:

            return None

        closes = df["Close"].dropna()

        if closes.empty:

            return None

        return float(closes.iloc[-1])

    except Exception as exc:  # noqa: BLE001

        log.debug("intraday %s failed: %s", ticker, exc)

        return None





def _fetch_nse_indices() -> dict[str, dict[str, Any]]:

    """NIFTY 50 and INDIA VIX from NSE public index feed (fresher than delayed Yahoo)."""

    try:

        from nselib.indices import index_data



        df = index_data.live_index_performances()

        out: dict[str, dict[str, Any]] = {}

        for key, symbol in (("nifty", "NIFTY 50"), ("vix", "INDIA VIX")):

            rows = df[df["indexSymbol"] == symbol]

            if rows.empty:

                continue

            r = rows.iloc[0]

            last = _finite(r.get("last"))

            if last is None:

                continue

            prev = _finite(r.get("previousDayVal")) or last

            pct = _finite(r.get("percentChange"))

            var = _finite(r.get("variation"))

            if var is not None and var != 0:

                chg = var

                if prev and abs(prev - last) < 0.01:

                    prev = last - var

            else:

                chg = last - prev if prev else 0

            if pct is None and prev:

                pct = chg / prev * 100

            out[key] = {

                "value": round(last, 2),

                "lastClose": round(last, 2),

                "prevClose": round(prev, 2),

                "change": round(chg, 2),

                "pct": round(pct or 0.0, 2),

                "source": "nse",

                "barDate": expected_last_trading_day().isoformat(),

            }

        return out

    except Exception as exc:  # noqa: BLE001

        log.warning("NSE live indices failed: %s", exc)

        return {}





def _fetch_bse_sensex() -> dict[str, Any] | None:

    """BSE SENSEX header API — includes session timestamp (`Ason`)."""

    try:

        r = _BSE_SESSION.get(

            "https://api.bseindia.com/BseIndiaAPI/api/getScripHeaderData/w?Debtflag=&scripcode=1",

            timeout=15,

        )

        r.raise_for_status()

        data = r.json()

        header = data.get("Header") or {}

        curr = data.get("CurrRate") or {}

        ltp = _finite(curr.get("LTP") or header.get("LTP"))

        prev = _finite(header.get("PrevClose"))

        if ltp is None:

            return None

        prev = prev or ltp

        chg = ltp - prev

        pct = (chg / prev * 100) if prev else 0.0

        ason = str(header.get("Ason") or "")

        bar_day = _parse_bse_ason(ason)

        return {

            "value": round(ltp, 2),

            "lastClose": round(ltp, 2),

            "prevClose": round(prev, 2),

            "change": round(chg, 2),

            "pct": round(pct, 2),

            "source": "bse",

            "asOfRaw": ason,

            "barDate": bar_day.isoformat() if bar_day else expected_last_trading_day().isoformat(),

        }

    except Exception as exc:  # noqa: BLE001

        log.warning("BSE Sensex quote failed: %s", exc)

        return None





def _bar_date_from_index(last_ts) -> date | None:

    if last_ts is None:

        return None

    if hasattr(last_ts, "date"):

        return last_ts.date()

    try:

        return datetime.fromisoformat(str(last_ts)[:10]).date()

    except ValueError:

        return None





def _merge_series(daily, live_value: float | None, live_bar: date | None) -> tuple[list[float], list[str]]:

    series = [round(float(x), 2) for x in daily["Close"].tolist()[-15:]]

    dates_raw = list(daily.index[-15:])

    dates = [d.strftime("%d %b") if hasattr(d, "strftime") else str(d)[:10] for d in dates_raw]

    if live_value is None or not series:

        return series, dates



    yahoo_bar = _bar_date_from_index(dates_raw[-1]) if dates_raw else None

    if live_bar and yahoo_bar and live_bar > yahoo_bar:

        series.append(round(live_value, 2))

        dates.append(live_bar.strftime("%d %b"))

    elif live_bar and yahoo_bar and live_bar == yahoo_bar:

        series[-1] = round(live_value, 2)

    elif live_bar and yahoo_bar is None:

        series.append(round(live_value, 2))

        dates.append(live_bar.strftime("%d %b"))

    return series, dates





def _staleness_label(*, bar_day: date | None, expected: date, source: str, yahoo_stale: bool) -> tuple[str, bool]:

    if bar_day is None:

        return f"Last close · source {source.upper()} · date unverified", True

    bar_fmt = bar_day.strftime("%d %b %Y")

    if bar_day < expected or yahoo_stale:

        exp_fmt = expected.strftime("%d %b %Y")

        return (

            f"Feed delayed · last available close {bar_fmt} ({source.upper()}) · expected session {exp_fmt}",

            True,

        )

    return f"Session close · {bar_fmt} · held until next open 09:15 IST", False





def _quote_payload(

    *,

    name: str,

    exchange: str,

    ticker: str,

    series_daily=None,

    live_overlay: dict[str, Any] | None = None,

) -> dict[str, Any]:

    sess = market_session()

    expected = expected_last_trading_day()

    daily = series_daily if series_daily is not None else _daily_bars(ticker)

    overlay = live_overlay or {}



    if daily is None or daily.empty:

        if overlay.get("value") is not None:

            bar_raw = overlay.get("barDate")

            bar_day = date.fromisoformat(bar_raw) if isinstance(bar_raw, str) else expected

            label, stale = _staleness_label(

                bar_day=bar_day,

                expected=expected,

                source=str(overlay.get("source") or exchange.lower()),

                yahoo_stale=False,

            )

            return {

                "name": name,

                "exchange": exchange,

                "value": overlay["value"],

                "lastClose": overlay.get("lastClose", overlay["value"]),

                "prevClose": overlay.get("prevClose", overlay["value"]),

                "change": overlay.get("change", 0),

                "pct": overlay.get("pct", 0),

                "quoteMode": "session_close",

                "sessionState": sess["state"],

                "asOf": overlay.get("asOfRaw") or f"{bar_day.strftime('%d %b %Y')} | 15:30 IST close",

                "label": label,

                "dataSource": overlay.get("source") or exchange.lower(),

                "barDate": bar_day.isoformat(),

                "expectedSession": expected.isoformat(),

                "isStale": stale,

                "series": [overlay["value"]],

                "seriesDates": [bar_day.strftime("%d %b")],

            }

        return {

            "name": name,

            "exchange": exchange,

            "value": None,

            "lastClose": None,

            "prevClose": None,

            "change": None,

            "pct": None,

            "quoteMode": "unavailable",

            "sessionState": sess["state"],

            "asOf": None,

            "label": "Quote unavailable — no live feed",

            "dataSource": None,

            "isStale": True,

        }



    yahoo_bar = _bar_date_from_index(daily.index[-1])

    yahoo_stale = yahoo_bar is not None and yahoo_bar < expected



    last_close = float(daily["Close"].iloc[-1])

    prev_close = float(daily["Close"].iloc[-2]) if len(daily) > 1 else last_close

    close_day = yahoo_bar.strftime("%d %b %Y") if yahoo_bar else str(daily.index[-1])[:10]



    live_bar: date | None = None

    data_source = "yahoo"

    if overlay.get("value") is not None:

        last_close = float(overlay["value"])

        if overlay.get("prevClose") is not None:

            prev_close = float(overlay["prevClose"])

        bar_raw = overlay.get("barDate")

        live_bar = date.fromisoformat(bar_raw) if isinstance(bar_raw, str) else expected

        data_source = str(overlay.get("source") or data_source)

        close_day = live_bar.strftime("%d %b %Y")



    series, series_dates = _merge_series(daily, last_close if overlay else None, live_bar or yahoo_bar)



    if sess["isOpen"]:

        live = _intraday_last(ticker)

        value = live if live is not None else last_close

        today = now_ist().date()

        if yahoo_bar == today and len(daily) > 1 and overlay.get("value") is None:

            prev_close = float(daily["Close"].iloc[-2])

        elif overlay.get("prevClose") is not None:

            prev_close = float(overlay["prevClose"])

        else:

            prev_close = last_close if live is None else prev_close

        chg = value - prev_close

        pct = (chg / prev_close * 100) if prev_close else 0.0

        label = "Live | free feed (delayed) | updates while session is open"

        if overlay.get("value") is not None and yahoo_stale:

            label = f"Live blend · {data_source.upper()} spot · Yahoo history delayed to {yahoo_bar.strftime('%d %b') if yahoo_bar else 'n/a'}"

        return {

            "name": name,

            "exchange": exchange,

            "value": round(value, 2),

            "lastClose": round(last_close, 2),

            "prevClose": round(prev_close, 2),

            "change": round(chg, 2),

            "pct": round(pct, 2),

            "quoteMode": "live",

            "sessionState": "open",

            "asOf": now_ist().strftime("%d %b %Y | %H:%M:%S IST"),

            "label": label,

            "dataSource": data_source,

            "barDate": (live_bar or yahoo_bar).isoformat() if (live_bar or yahoo_bar) else None,

            "expectedSession": expected.isoformat(),

            "isStale": yahoo_stale and overlay.get("value") is None,

            "series": series,

            "seriesDates": series_dates,

        }



    chg = last_close - prev_close

    pct = (chg / prev_close * 100) if prev_close else 0.0

    display_bar = live_bar or yahoo_bar or expected

    label, stale = _staleness_label(

        bar_day=display_bar,

        expected=expected,

        source=data_source,

        yahoo_stale=yahoo_stale and overlay.get("value") is None,

    )

    as_of = overlay.get("asOfRaw") or f"{display_bar.strftime('%d %b %Y')} | 15:30 IST close"



    return {

        "name": name,

        "exchange": exchange,

        "value": round(last_close, 2),

        "lastClose": round(last_close, 2),

        "prevClose": round(prev_close, 2),

        "change": round(chg, 2),

        "pct": round(pct, 2),

        "quoteMode": "session_close",

        "sessionState": sess["state"],

        "asOf": as_of,

        "label": label,

        "dataSource": data_source,

        "barDate": display_bar.isoformat(),

        "expectedSession": expected.isoformat(),

        "isStale": stale,

        "series": series,

        "seriesDates": series_dates,

    }





def fetch_index_quotes(force: bool = False) -> dict[str, Any]:

    ttl = 0.5 if force else cache_ttl_seconds()



    def _load() -> dict[str, Any]:

        nse = _fetch_nse_indices()

        sensex_live = _fetch_bse_sensex()

        key_map = {"NIFTY": "nifty", "SENSEX": "sensex", "VIX": "vix"}

        name_map = {"NIFTY": "NIFTY 50", "SENSEX": "SENSEX", "VIX": "India VIX"}

        exch_map = {"NIFTY": "NSE", "SENSEX": "BSE", "VIX": "NSE"}

        overlay_map = {

            "NIFTY": nse.get("nifty"),

            "VIX": nse.get("vix"),

            "SENSEX": sensex_live,

        }

        out: dict[str, Any] = {}

        for key, ticker in INDEX_YF.items():

            try:

                out[key_map[key]] = _quote_payload(

                    name=name_map[key],

                    exchange=exch_map[key],

                    ticker=ticker,

                    live_overlay=overlay_map.get(key),

                )

            except Exception as exc:  # noqa: BLE001

                log.warning("index quote %s failed: %s", key, exc)

        return out



    return cache.get_or_set(f"quotes:indices:v2:{int(ttl)}", ttl, _load)





def fetch_equity_quote(symbol: str, exchange: str = "NSE") -> dict[str, Any]:

    symbol = symbol.upper()

    yf_sym = f"{symbol}.BO" if exchange.upper() == "BSE" else f"{symbol}.NS"

    ttl = cache_ttl_seconds()



    def _load() -> dict[str, Any]:

        q = _quote_payload(name=symbol, exchange=exchange.upper(), ticker=yf_sym)

        q["symbol"] = symbol

        q["price"] = q.get("value")

        return q



    return cache.get_or_set(f"quotes:eq:{exchange}:{symbol}:{int(ttl)}", ttl, _load)





def fetch_dual_live(symbol: str) -> dict[str, Any]:

    symbol = symbol.upper()

    ttl = cache_ttl_seconds()



    def _load() -> dict[str, Any]:

        return {

            "nse": fetch_equity_quote(symbol, "NSE"),

            "bse": fetch_equity_quote(symbol, "BSE"),

        }



    return cache.get_or_set(f"quotes:dual:{symbol}:{int(ttl)}", ttl, _load)





def live_quotes_bundle(symbols: list[str] | None = None) -> dict[str, Any]:

    sess = market_session()

    indices = fetch_index_quotes()

    equities: dict[str, Any] = {}

    for sym in (symbols or [])[:12]:

        sym = sym.upper().strip()

        if not sym:

            continue

        try:

            equities[sym] = fetch_dual_live(sym)

        except Exception as exc:  # noqa: BLE001

            log.warning("equity live %s failed: %s", sym, exc)

    return {

        "session": sess,

        "indices": indices,

        "equities": equities,

        "asOf": now_ist().strftime("%d %b %Y | %H:%M:%S IST"),

        "pollHintMs": sess["refreshMsOpen"] if sess["isOpen"] else sess["refreshMsClosed"],

    }

