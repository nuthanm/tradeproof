# TradeProof

Educational research board for Indian equities: **breakouts**, **7-day forecasts**, **Nifty options**, **FII/DII + deals**, and **penny stocks (< ₹25)**.

Not investment advice. Free public market feeds can be delayed or incomplete.

## Live site

- **Production:** https://tradeproof-two.vercel.app
- **GitHub:** https://github.com/nuthanm/tradeproof

Deployed on Vercel (FastAPI + static UI in `public/`).

## Run locally

```bash
python -m venv .venv
# Windows
.\.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
uvicorn app.main:app --reload --host 127.0.0.1 --port 8080
```

Open **http://127.0.0.1:8080/**

First signal/bundle scan can take a while (Yahoo + fundamentals). Results are cached in memory.

## Vercel

This project uses Vercel’s [FastAPI](https://vercel.com/docs/frameworks/backend/fastapi) preset:

- API: `app/main.py` (`app` instance)
- Static UI: `public/**` (CDN)
- Function config: `vercel.json` (`maxDuration: 60`)

```bash
npm i -g vercel
vercel login
vercel          # preview
vercel --prod   # production
```

Or connect [github.com/nuthanm/tradeproof](https://github.com/nuthanm/tradeproof) in the Vercel dashboard (Import Project).

**Notes**

- Hobby plans may cap function duration below 60s — Pro unlocks longer runs for heavy `/api/signals` scans.
- On Vercel, quote updates use **HTTP polling** (`/api/quotes`); SSE is one-shot only.
- In-memory cache resets on cold starts.

## API

| Endpoint | Description |
|----------|-------------|
| `GET /api/health` | Health check |
| `GET /api/pulse` | Indices + FII/DII bias |
| `GET /api/quotes` | Session-aware index/equity quotes |
| `GET /api/signals?limit=45` | Breakout scan |
| `GET /api/stock/{SYMBOL}` | Detail + KPIs + 7D range |
| `GET /api/deals` | Block/bulk deals |
| `GET /api/options/nifty` | Nifty reversal + option spots |
| `GET /api/pennies` | Under ₹25 screen |
| `GET /api/bundle` | Full UI payload |

## Data sources (free)

- **Prices / history:** Yahoo Finance (`yfinance`) + NSE/BSE public index feeds
- **FII/DII:** Mr. Chartist JSON (fallback: `nselib`)
- **Deals / option chain:** `nselib` / NSE public APIs when reachable

## Layout

```
app/           FastAPI backend
public/        Static UI (served on Vercel CDN; also used locally)
prototype/     Same UI copy kept for local parity
vercel.json    Vercel function settings
requirements.txt
```
