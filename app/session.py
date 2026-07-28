"""NSE cash-market session clock (IST)."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any

try:
    from zoneinfo import ZoneInfo

    IST = ZoneInfo("Asia/Kolkata")
except Exception:  # noqa: BLE001
    IST = timezone(timedelta(hours=5, minutes=30))

# Regular NSE equity + index cash session
PREOPEN_START = time(9, 0)
OPEN_TIME = time(9, 15)
CLOSE_TIME = time(15, 30)
# Post-close freeze starts immediately after close until next open


def now_ist() -> datetime:
    return datetime.now(IST)


def _is_weekend(d: date) -> bool:
    return d.weekday() >= 5  # Sat/Sun


def market_session(at: datetime | None = None) -> dict[str, Any]:
    """
    Return NSE cash session state.
    Holiday calendar is not fully modeled on free data — weekends are closed;
    weekdays follow 09:15–15:30 IST.
    """
    dt = at.astimezone(IST) if at else now_ist()
    d = dt.date()
    t = dt.time()

    if _is_weekend(d):
        # Freeze Friday (or last weekday) close until Monday open
        return {
            "state": "closed",
            "isOpen": False,
            "isPreopen": False,
            "label": "Session closed | weekend — showing last session close until Monday 09:15 IST",
            "nextOpenHint": "Monday 09:15 IST" if d.weekday() == 5 else "Tomorrow 09:15 IST" if d.weekday() == 6 else "Next weekday 09:15 IST",
            "clock": dt.strftime("%d %b %Y | %H:%M:%S IST"),
            "refreshMsOpen": 1000,
            "refreshMsClosed": 60000,
            "note": (
                "During the open session TradeProof polls free public quotes as fast as sources allow (~1s). "
                "True exchange millisecond ticks require a paid market-data feed and are not available on zero-cost data."
            ),
        }

    if PREOPEN_START <= t < OPEN_TIME:
        return {
            "state": "preopen",
            "isOpen": False,
            "isPreopen": True,
            "label": "Pre-open | last close held until 09:15 IST",
            "nextOpenHint": "Today 09:15 IST",
            "clock": dt.strftime("%d %b %Y | %H:%M:%S IST"),
            "refreshMsOpen": 1000,
            "refreshMsClosed": 15000,
            "note": (
                "Pre-open: displaying previous session close. Live updates begin at cash-market open (09:15 IST)."
            ),
        }

    if OPEN_TIME <= t <= CLOSE_TIME:
        return {
            "state": "open",
            "isOpen": True,
            "isPreopen": False,
            "label": "Market open | live quotes (free feed, delayed)",
            "nextOpenHint": None,
            "clock": dt.strftime("%d %b %Y | %H:%M:%S IST"),
            "refreshMsOpen": 1000,
            "refreshMsClosed": 60000,
            "note": (
                "Live mode: UI refreshes about every second while the session is open. "
                "Underlying free feeds (Yahoo/NSE public) are delayed and cannot provide true millisecond exchange prints."
            ),
        }

    # After close same day, or before pre-open
    return {
        "state": "closed",
        "isOpen": False,
        "isPreopen": False,
        "label": "Session closed | showing last close until next open (09:15 IST)",
        "nextOpenHint": "Next weekday 09:15 IST",
        "clock": dt.strftime("%d %b %Y | %H:%M:%S IST"),
        "refreshMsOpen": 1000,
        "refreshMsClosed": 60000,
        "note": (
            "After 15:30 IST the displayed value is the session close and stays frozen until the next trading open."
        ),
    }


def expected_last_trading_day(at: datetime | None = None) -> date:
    """
    Calendar date of the cash session close we should display when the market is not open.
    Weekends roll back to Friday; weekday pre-open uses the prior session.
    NSE holidays are not modeled on free data.
    """
    dt = at.astimezone(IST) if at else now_ist()
    d = dt.date()
    t = dt.time()

    if _is_weekend(d):
        while _is_weekend(d):
            d -= timedelta(days=1)
        return d

    sess = market_session(dt)
    if sess["isOpen"]:
        d = dt.date() - timedelta(days=1)
        while _is_weekend(d):
            d -= timedelta(days=1)
        return d

    if t < OPEN_TIME:
        d = dt.date() - timedelta(days=1)
        while _is_weekend(d):
            d -= timedelta(days=1)
        return d

    return dt.date()


def cache_ttl_seconds() -> float:
    sess = market_session()
    return 1.0 if sess["isOpen"] else 45.0
