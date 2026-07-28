"""Market data fetchers — yfinance + NSE public endpoints + free FII/DII JSON."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
import requests
import yfinance as yf

from app.cache import cache
from app.universe import INDEX_YF, NIFTY_50, TOP_100_EXTRA

log = logging.getLogger("tradeproof.fetchers")

SESSION = requests.Session()
SESSION.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json,text/plain,*/*",
    }
)


def _yf_symbol(symbol: str, exchange: str = "NSE") -> str:
    return f"{symbol}.BO" if exchange.upper() == "BSE" else f"{symbol}.NS"


def fetch_indices() -> dict[str, Any]:
    """Session-aware index quotes (live while open; frozen last close after hours)."""
    try:
        from app.quotes import fetch_index_quotes

        return fetch_index_quotes()
    except Exception as exc:  # noqa: BLE001
        log.warning("fetch_indices via quotes failed, legacy fallback: %s", exc)

    def _load() -> dict[str, Any]:
        out: dict[str, Any] = {}
        key_map = {"NIFTY": "nifty", "SENSEX": "sensex", "VIX": "vix"}
        name_map = {"NIFTY": "NIFTY 50", "SENSEX": "SENSEX", "VIX": "India VIX"}
        exch_map = {"NIFTY": "NSE", "SENSEX": "BSE", "VIX": "NSE"}
        for key, ticker in INDEX_YF.items():
            try:
                hist = yf.Ticker(ticker).history(period="1mo")
                if hist.empty:
                    continue
                last = hist.iloc[-1]
                prev = hist.iloc[-2] if len(hist) > 1 else last
                close = float(last["Close"])
                prev_close = float(prev["Close"])
                chg = close - prev_close
                pct = (chg / prev_close * 100) if prev_close else 0.0
                series = [round(float(x), 2) for x in hist["Close"].tolist()[-15:]]
                series_dates = [d.strftime("%d %b") if hasattr(d, "strftime") else str(d)[:10] for d in hist.index[-15:]]
                out[key_map[key]] = {
                    "name": name_map[key],
                    "exchange": exch_map[key],
                    "value": round(close, 2),
                    "change": round(chg, 2),
                    "pct": round(pct, 2),
                    "series": series,
                    "seriesDates": series_dates,
                    "quoteMode": "session_close",
                }
            except Exception as exc2:  # noqa: BLE001
                log.warning("index %s failed: %s", key, exc2)
        return out

    return cache.get_or_set("indices:legacy", 300, _load)


def fetch_history(symbol: str, period: str = "6mo", exchange: str = "NSE") -> pd.DataFrame:
    def _load() -> pd.DataFrame:
        df = yf.Ticker(_yf_symbol(symbol, exchange)).history(period=period, auto_adjust=True)
        if df is None or df.empty:
            return pd.DataFrame()
        df = df.rename(columns=str.title)
        if "Close" in df.columns:
            df = df.dropna(subset=["Close"])
        if "Volume" in df.columns:
            df["Volume"] = df["Volume"].fillna(0)
        return df

    return cache.get_or_set(f"hist:{exchange}:{symbol}:{period}", 600, _load)


def fetch_dual_last(symbol: str) -> dict[str, Any]:
    """Latest NSE (.NS) and BSE (.BO) last prices."""

    def _one(ex: str) -> dict[str, Any] | None:
        try:
            df = fetch_history(symbol, period="10d", exchange=ex)
            if df.empty:
                return None
            closes = df["Close"].dropna()
            if closes.empty:
                return None
            # walk back to last finite close
            last = None
            prev = None
            for i in range(len(closes) - 1, -1, -1):
                v = float(closes.iloc[i])
                if np.isfinite(v):
                    if last is None:
                        last = v
                    elif prev is None:
                        prev = v
                        break
            if last is None:
                return None
            if prev is None:
                prev = last
            chg = last - prev
            pct = (chg / prev * 100) if prev else 0.0
            return {
                "exchange": ex,
                "price": round(last, 2),
                "change": round(chg, 2),
                "pct": round(pct, 2),
            }
        except Exception as exc:  # noqa: BLE001
            log.warning("dual last %s %s failed: %s", symbol, ex, exc)
            return None

    def _load() -> dict[str, Any]:
        return {"nse": _one("NSE"), "bse": _one("BSE")}

    return cache.get_or_set(f"dual:{symbol}:v2", 180, _load)


def fetch_news(symbol: str, limit: int = 6) -> list[dict[str, Any]]:
    def _load() -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        try:
            t = yf.Ticker(_yf_symbol(symbol, "NSE"))
            raw = t.news or []
            for n in raw[:limit]:
                # yfinance news shape varies by version
                content = n.get("content") if isinstance(n.get("content"), dict) else None
                title = n.get("title") or (content or {}).get("title")
                link = n.get("link") or (content or {}).get("clickThroughUrl", {})
                if isinstance(link, dict):
                    link = link.get("url")
                publisher = n.get("publisher") or (content or {}).get("provider", {}).get("displayName")
                ts = n.get("providerPublishTime") or n.get("pubDate")
                if not title:
                    continue
                items.append(
                    {
                        "title": title,
                        "publisher": publisher or "News",
                        "link": link or "",
                        "published": str(ts)[:19] if ts else "",
                    }
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("news %s failed: %s", symbol, exc)
        return items

    return cache.get_or_set(f"news:{symbol}", 900, _load)


def fetch_index_news(limit: int = 5) -> list[dict[str, Any]]:
    """Broad market headlines via Nifty proxy ticker news."""
    return fetch_news("RELIANCE", limit=limit)  # fallback breadth; also try ^NSEI



def fetch_quote_info(symbol: str) -> dict[str, Any]:
    def _load() -> dict[str, Any]:
        t = yf.Ticker(_yf_symbol(symbol, "NSE"))
        info = {}
        try:
            info = t.info or {}
        except Exception:  # noqa: BLE001
            info = {}
        fast = {}
        try:
            fast = t.fast_info or {}
        except Exception:  # noqa: BLE001
            fast = {}
        return {
            "longName": info.get("longName") or info.get("shortName") or symbol,
            "pe": info.get("trailingPE") or info.get("forwardPE"),
            "pb": info.get("priceToBook"),
            "roe": (info.get("returnOnEquity") or 0) * 100 if info.get("returnOnEquity") else None,
            "debtEquity": info.get("debtToEquity"),
            "dividendYield": (info.get("dividendYield") or 0) * 100 if info.get("dividendYield") else 0,
            "sector": info.get("sector"),
            "industry": info.get("industry"),
            "marketCap": info.get("marketCap"),
            "currentPrice": info.get("currentPrice")
            or info.get("regularMarketPrice")
            or getattr(fast, "last_price", None)
            or (fast.get("lastPrice") if isinstance(fast, dict) else None),
            "fiftyTwoWeekHigh": info.get("fiftyTwoWeekHigh"),
            "fiftyTwoWeekLow": info.get("fiftyTwoWeekLow"),
            "averageVolume": info.get("averageVolume") or info.get("averageVolume10days"),
        }

    return cache.get_or_set(f"info:{symbol}", 1800, _load)


def fetch_fii_dii() -> dict[str, Any]:
    """Prefer free Mr.Chartist JSON; fall back to nselib."""

    def _from_mrchartist() -> dict[str, Any] | None:
        try:
            r = SESSION.get("https://fii-diidata.mrchartist.com/api/data", timeout=20)
            r.raise_for_status()
            data = r.json()
            fii_net = float(data.get("fii_net") or data.get("fn") or 0)
            dii_net = float(data.get("dii_net") or data.get("dn") or 0)
            fii_buy = float(data.get("fii_buy") or data.get("fb") or abs(fii_net) + 1000)
            fii_sell = float(data.get("fii_sell") or data.get("fs") or abs(fii_buy - fii_net))
            dii_buy = float(data.get("dii_buy") or data.get("db") or abs(dii_net) + 1000)
            dii_sell = float(data.get("dii_sell") or data.get("ds") or abs(dii_buy - dii_net))
            return {
                "fii": {"buy": round(fii_buy, 2), "sell": round(fii_sell, 2), "net": round(fii_net, 2)},
                "dii": {"buy": round(dii_buy, 2), "sell": round(dii_sell, 2), "net": round(dii_net, 2)},
                "source": "mrchartist",
                "asOf": data.get("date") or data.get("d") or datetime.now().strftime("%d %b %Y"),
                "raw": data,
            }
        except Exception as exc:  # noqa: BLE001
            log.warning("mrchartist FII/DII failed: %s", exc)
            return None

    def _from_nselib() -> dict[str, Any] | None:
        try:
            from nselib import capital_market

            fn = getattr(capital_market, "fii_dii_trading_activity", None)
            if fn is None:
                from nselib.capital_market import capital_market_data as cmd

                fn = getattr(cmd, "fii_dii_trading_activity", None)
            if fn is None:
                return None
            df = fn() if callable(fn) else fn
            if isinstance(df, pd.DataFrame):
                rows = df.to_dict(orient="records")
            else:
                rows = df
            fii = next((r for r in rows if "FII" in str(r.get("category", r.get("Client Type", ""))).upper()), None)
            dii = next((r for r in rows if "DII" in str(r.get("category", r.get("Client Type", ""))).upper()), None)

            def pack(row: dict | None) -> dict[str, float]:
                if not row:
                    return {"buy": 0.0, "sell": 0.0, "net": 0.0}
                buy = float(row.get("buyValue") or row.get("Buy Value") or row.get("buy_value") or 0)
                sell = float(row.get("sellValue") or row.get("Sell Value") or row.get("sell_value") or 0)
                net = float(row.get("netValue") or row.get("Net Value") or row.get("net_value") or (buy - sell))
                return {"buy": round(buy, 2), "sell": round(sell, 2), "net": round(net, 2)}

            return {
                "fii": pack(fii),
                "dii": pack(dii),
                "source": "nselib",
                "asOf": datetime.now().strftime("%d %b %Y"),
            }
        except Exception as exc:  # noqa: BLE001
            log.warning("nselib FII/DII failed: %s", exc)
            return None

    def _load() -> dict[str, Any]:
        return _from_mrchartist() or _from_nselib() or {
            "fii": {"buy": 0, "sell": 0, "net": 0},
            "dii": {"buy": 0, "sell": 0, "net": 0},
            "source": "unavailable",
            "asOf": datetime.now().strftime("%d %b %Y"),
        }

    return cache.get_or_set("fii_dii", 900, _load)


def fetch_deals(period: str = "1W") -> list[dict[str, Any]]:
    def _nselib_deals() -> list[dict[str, Any]]:
        deals: list[dict[str, Any]] = []
        try:
            from nselib import capital_market

            for kind, fn_name in (("Block", "block_deals_data"), ("Bulk", "bulk_deal_data")):
                fn = getattr(capital_market, fn_name, None)
                if fn is None:
                    continue
                try:
                    df = fn(period=period)
                except TypeError:
                    df = fn()
                if df is None or (isinstance(df, pd.DataFrame) and df.empty):
                    continue
                if not isinstance(df, pd.DataFrame):
                    continue
                for _, row in df.head(80).iterrows():
                    symbol = str(row.get("Symbol") or row.get("symbol") or row.get("SECURITY") or "").strip()
                    if not symbol:
                        continue
                    qty = row.get("Quantity") or row.get("Traded Quantity") or row.get("qty") or 0
                    price = row.get("Trade Price") or row.get("Price") or row.get("WAP") or 0
                    try:
                        value_cr = (float(qty) * float(price)) / 1e7
                    except Exception:  # noqa: BLE001
                        value_cr = 0.0
                    side = str(row.get("Buy / Sell") or row.get("Client Type") or row.get("dealType") or "").title()
                    if "Sell" in side:
                        side = "Sell"
                    elif "Buy" in side:
                        side = "Buy"
                    else:
                        side = "Buy" if "buy" in side.lower() else "Sell"
                    deals.append(
                        {
                            "time": str(row.get("Date") or row.get("Trade Date") or "")[:16],
                            "type": kind,
                            "symbol": symbol.upper(),
                            "side": side,
                            "qty": str(qty),
                            "value": round(value_cr, 2),
                            "client": str(row.get("Client Name") or row.get("clientName") or "Institutional")[:80],
                            "note": f"{kind} deal from NSE report",
                        }
                    )
        except Exception as exc:  # noqa: BLE001
            log.warning("nselib deals failed: %s", exc)

        try:
            r = SESSION.get("https://fii-diidata.mrchartist.com/api/large-deals", timeout=20)
            if r.ok:
                payload = r.json()
                items = payload if isinstance(payload, list) else payload.get("deals") or payload.get("data") or []
                for item in items[:40]:
                    deals.append(
                        {
                            "time": str(item.get("time") or item.get("date") or "")[:16],
                            "type": str(item.get("type") or "Block").title(),
                            "symbol": str(item.get("symbol") or item.get("Symbol") or "").upper(),
                            "side": str(item.get("side") or item.get("buySell") or "Buy").title(),
                            "qty": str(item.get("qty") or item.get("quantity") or ""),
                            "value": float(item.get("value") or item.get("valueCr") or 0),
                            "client": str(item.get("client") or "Institutional")[:80],
                            "note": str(item.get("note") or "Large deal feed"),
                        }
                    )
        except Exception as exc:  # noqa: BLE001
            log.warning("large-deals feed failed: %s", exc)
        return deals

    return cache.get_or_set(f"deals:{period}", 900, _nselib_deals)


def fetch_option_volume_proxy(symbol: str = "NIFTY") -> dict[str, Any]:
    """Approximate call/put pressure via NSE option-chain when possible; else volume skew proxy."""

    def _load() -> dict[str, Any]:
        # Try NSE option chain for NIFTY
        if symbol.upper() in {"NIFTY", "NIFTY 50", "^NSEI"}:
            try:
                # warm cookies
                SESSION.get("https://www.nseindia.com", timeout=15)
                r = SESSION.get(
                    "https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY",
                    timeout=25,
                    headers={"Referer": "https://www.nseindia.com/option-chain"},
                )
                if r.ok:
                    records = r.json().get("records", {}).get("data", [])
                    call_vol = 0
                    put_vol = 0
                    for row in records:
                        ce = row.get("CE") or {}
                        pe = row.get("PE") or {}
                        call_vol += int(ce.get("totalTradedVolume") or 0)
                        put_vol += int(pe.get("totalTradedVolume") or 0)
                    if call_vol + put_vol > 0:
                        return {
                            "call": call_vol,
                            "put": put_vol,
                            "label": "NIFTY active options volume (NSE chain)",
                            "source": "nse_option_chain",
                        }
            except Exception as exc:  # noqa: BLE001
                log.warning("NSE option chain failed: %s", exc)

        # Equity proxy from volume z / candle body — filled by engine when needed
        return {"call": 0, "put": 0, "label": "Unavailable", "source": "none"}

    return cache.get_or_set(f"optvol:{symbol}", 300, _load)


def batch_histories(symbols: list[str], period: str = "6mo") -> dict[str, pd.DataFrame]:
    """Download multiple .NS tickers in one yfinance call where possible."""

    def _load() -> dict[str, pd.DataFrame]:
        tickers = " ".join(_yf_symbol(s) for s in symbols)
        data = yf.download(
            tickers,
            period=period,
            group_by="ticker",
            auto_adjust=True,
            threads=True,
            progress=False,
        )
        out: dict[str, pd.DataFrame] = {}
        if len(symbols) == 1:
            sym = symbols[0]
            if not data.empty:
                out[sym] = data.copy()
            return out
        for sym in symbols:
            try:
                df = data[_yf_symbol(sym)].dropna(how="all")
                if not df.empty:
                    out[sym] = df
            except Exception:  # noqa: BLE001
                continue
        return out

    key = f"batch:{period}:{','.join(sorted(symbols)[:30])}:{len(symbols)}"
    return cache.get_or_set(key, 600, _load)


def universe_for_scan(limit_top100: int = 40) -> list[dict[str, str]]:
    rows = [{"symbol": s, "universe": ["Nifty 50", "Top 100"]} for s in NIFTY_50]
    for s in TOP_100_EXTRA[: max(0, limit_top100 - 0)]:
        if s not in NIFTY_50:
            rows.append({"symbol": s, "universe": ["Top 100"]})
    return rows
