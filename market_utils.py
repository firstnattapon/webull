from __future__ import annotations

from datetime import datetime, time

import pytz


NEW_YORK_TZ = pytz.timezone("America/New_York")
MARKET_OPEN = time(hour=9, minute=30)
MARKET_CLOSE = time(hour=16, minute=0)


def to_new_york_time(now: datetime | None = None) -> datetime:
    """Return a timezone-aware New York datetime."""
    if now is None:
        now = datetime.now(tz=pytz.utc)
    elif now.tzinfo is None:
        now = pytz.utc.localize(now)
    return now.astimezone(NEW_YORK_TZ)


def is_us_market_open(now: datetime | None = None) -> bool:
    """Check regular US stock market hours: Mon-Fri, 09:30-16:00 ET."""
    now_et = to_new_york_time(now)
    if now_et.weekday() >= 5:
        return False

    current_time = now_et.time()
    return MARKET_OPEN <= current_time < MARKET_CLOSE
