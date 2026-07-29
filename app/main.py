"""TradeProof FastAPI — live NSE/BSE research board."""
from __future__ import annotations

import logging
import math
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import engine, fetchers
from app.cache import cache

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tradeproof")

ROOT = Path(__file__).resolve().parent.parent
# Prefer public/ (Vercel CDN + local); fall back to prototype/ for older layouts
STATIC = ROOT / "public" if (ROOT / "public" / "index.html").exists() else ROOT / "prototype"
IS_VERCEL = bool(os.environ.get("VERCEL"))

app = FastAPI(
    title="TradeProof",
    version="0.1.0",
    description="Live institutional-flow forecasting for Indian equities",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _json_safe(obj: Any) -> Any:
    """Replace NaN/Inf so FastAPI JSON responses never fail."""
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    try:
        import numpy as np

        if isinstance(obj, np.generic):
            v = obj.item()
            if isinstance(v, float):
                return v if math.isfinite(v) else None
            return v
    except Exception:  # noqa: BLE001
        pass
    return obj


@app.get("/api/health")
def health():
    return {"ok": True, "service": "tradeproof", "vercel": IS_VERCEL}


@app.get("/api/meta")
def meta():
    pulse = engine.market_pulse()
    return {
        "asOf": pulse["asOf"],
        "session": pulse["meta"]["session"],
        "universe": pulse["meta"]["universe"],
        "disclaimer": pulse["meta"]["disclaimer"],
    }


@app.get("/api/pulse")
def pulse():
    from app.session import cache_ttl_seconds

    ttl = min(30.0, max(2.0, cache_ttl_seconds() * 2))
    return cache.get_or_set(f"api:pulse:v2:{int(ttl)}", ttl, engine.market_pulse)


@app.get("/api/quotes")
def quotes(symbols: str = Query("", description="Comma-separated equity symbols for dual NSE/BSE live quotes")):
    """Lightweight session-aware quotes. Short TTL while market is open; frozen close after hours."""
    from app.quotes import live_quotes_bundle
    from app.session import cache_ttl_seconds

    syms = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    ttl = cache_ttl_seconds()
    key = f"api:quotes:{','.join(syms)}:{int(ttl)}"
    return cache.get_or_set(key, ttl, lambda: live_quotes_bundle(syms))


@app.get("/api/quotes/stream")
async def quotes_stream(symbols: str = Query("")):
    """SSE stream of quotes. On Vercel, prefer /api/quotes polling (serverless duration limits)."""
    import asyncio
    import json

    from fastapi.responses import StreamingResponse

    from app.quotes import live_quotes_bundle
    from app.session import market_session

    syms = [s.strip().upper() for s in symbols.split(",") if s.strip()]

    # Serverless: emit one payload then close — client should use polling
    if IS_VERCEL:

        async def one_shot():
            try:
                payload = live_quotes_bundle(syms)
                payload["streamMode"] = "oneshot"
                payload["hint"] = "Use /api/quotes polling on Vercel"
                yield f"data: {json.dumps(payload)}\n\n"
            except Exception as exc:  # noqa: BLE001
                yield f"data: {json.dumps({'error': str(exc)})}\n\n"

        return StreamingResponse(
            one_shot(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def event_gen():
        while True:
            try:
                payload = live_quotes_bundle(syms)
                yield f"data: {json.dumps(payload)}\n\n"
                sess = payload.get("session") or market_session()
                wait = 1.0 if sess.get("isOpen") else 30.0
            except Exception as exc:  # noqa: BLE001
                yield f"data: {json.dumps({'error': str(exc)})}\n\n"
                wait = 5.0
            await asyncio.sleep(wait)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/signals")
def signals(limit: int = Query(55, ge=10, le=80)):
    def _load():
        # Cap work on serverless cold starts
        lim = min(limit, 35) if IS_VERCEL else limit
        rows = engine.scan_equities(max_symbols=lim)
        return _json_safe({"asOf": engine.market_pulse()["asOf"], "count": len(rows), "stocks": rows})

    return cache.get_or_set(f"api:signals:v4:{limit}:{int(IS_VERCEL)}", 600, _load)


@app.get("/api/stock/{symbol}")
def stock(symbol: str):
    symbol = symbol.upper()
    try:
        return cache.get_or_set(f"api:stock:{symbol}", 600, lambda: engine.stock_detail(symbol))
    except Exception as exc:  # noqa: BLE001
        log.exception("stock detail failed")
        raise HTTPException(status_code=404, detail=f"Could not load {symbol}: {exc}") from exc


@app.get("/api/deals")
def deals(
    from_date: str | None = Query(None, alias="from", description="YYYY-MM-DD inclusive start"),
    to_date: str | None = Query(None, alias="to", description="YYYY-MM-DD inclusive end"),
):
    raw = cache.get_or_set("api:deals:1M:v4", 900, lambda: fetchers.fetch_deals(period="1M"))
    filtered = fetchers.filter_deals_by_range(raw, from_date=from_date, to_date=to_date)
    return {
        "deals": filtered,
        "from": from_date,
        "to": to_date,
        "windowDays": 30,
        "count": len(filtered),
    }


@app.get("/api/options/nifty")
def options_nifty():
    return cache.get_or_set("api:options:nifty", 300, engine.nifty_options_reversal)


@app.get("/api/pennies")
def pennies():
    # Bust stale empty caches after candidate/universe refresh
    return cache.get_or_set("api:pennies:v2", 600, lambda: {"pennies": engine.scan_pennies()})


@app.get("/api/bundle")
def bundle(limit: int = Query(30, ge=10, le=60)):
    """Single payload shaped for the frontend."""

    def _load():
        lim = min(limit, 25) if IS_VERCEL else limit
        pulse = engine.market_pulse()
        stocks = engine.scan_equities(max_symbols=lim)
        deals = fetchers.fetch_deals()
        options = engine.nifty_options_reversal()
        pennies = engine.scan_pennies()
        return {
            "meta": {
                "asOf": pulse["asOf"],
                "session": pulse["meta"]["session"],
                "universe": pulse["meta"]["universe"],
                "disclaimer": pulse["meta"]["disclaimer"],
            },
            "indices": pulse["indices"],
            "flows": pulse["flows"],
            "stocks": stocks,
            "deals": deals,
            "optionsReversal": options,
            "pennies": pennies,
        }

    return cache.get_or_set(f"api:bundle:{limit}:{int(IS_VERCEL)}", 900, _load)


# Local/uvicorn: serve static UI from public/ (or prototype/)
# On Vercel, files under public/ are served by the CDN automatically.
if STATIC.exists() and not IS_VERCEL:
    css_dir = STATIC / "css"
    js_dir = STATIC / "js"
    if css_dir.exists():
        app.mount("/css", StaticFiles(directory=css_dir), name="css")
    if js_dir.exists():
        app.mount("/js", StaticFiles(directory=js_dir), name="js")


@app.get("/")
def home():
    index = STATIC / "index.html"
    if index.exists():
        return FileResponse(index)
    return JSONResponse({"service": "tradeproof", "docs": "/docs", "health": "/api/health"})


@app.get("/{page}.html")
def html_page(page: str):
    path = STATIC / f"{page}.html"
    if not path.exists():
        raise HTTPException(status_code=404)
    return FileResponse(path)
