"""
Time helpers. Everything is computed and stored in UTC; IST is display-only.

MongoDB stores BSON dates as UTC with millisecond precision, and pymongo hands
them back as naive datetimes. `to_db` / `from_db` make that boundary explicit so
comparisons never mix naive and aware values.
"""
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30), "IST")


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_db(dt: datetime) -> datetime:
    """Aware datetime → naive UTC truncated to milliseconds (BSON precision)."""
    dt = dt.astimezone(UTC)
    return dt.replace(tzinfo=None, microsecond=(dt.microsecond // 1000) * 1000)


def from_db(dt):
    """Naive-UTC datetime read from MongoDB → aware UTC datetime (None passes through)."""
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def iso_utc(dt) -> str:
    """ISO-8601 UTC string with a trailing 'Z', for API responses."""
    return from_db(dt).isoformat(timespec="seconds").replace("+00:00", "Z")


def fmt_ist(dt) -> str:
    """Human-readable IST timestamp for templates, e.g. '24 Oct 2026, 03:15 PM IST'."""
    if dt is None:
        return ""
    return from_db(dt).astimezone(IST).strftime("%d %b %Y, %I:%M %p IST")


def today_ist(now: datetime):
    """The calendar date in India at instant `now` (journey dates are IST dates)."""
    return now.astimezone(IST).date()


def seconds_until(later, now: datetime) -> int:
    """Whole seconds from `now` until `later`, rounded up, never negative."""
    delta = (from_db(later) - now).total_seconds()
    return max(0, int(-(-delta // 1)))
