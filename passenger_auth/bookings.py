"""
Booking (PNR) lookup and the eligibility policy for Lost & Found access.

Collection `bookings` holds one document per passenger on a PNR:
    pnr, passengerSeq, passengerName, registeredPhone (E.164), status,
    route, routeId, travelDate ("YYYY-MM-DD", IST calendar date)

A multi-passenger PNR is several documents sharing `pnr`. A typed phone is
matched against every passenger's registeredPhone on that PNR; the session is
bound to the passengers registered with that phone.

Eligibility (see docs/DECISIONS.md):
  * status must be "confirmed" or "completed" — cancelled bookings never get an OTP;
  * the journey must be no older than BOOKING_ACCESS_WINDOW_DAYS (default 90).
    Past journeys inside the window ARE allowed — that is when items get lost.
"""
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .phone import try_normalize_phone

BOOKINGS = "bookings"
ELIGIBLE_STATUSES = frozenset({"confirmed", "completed"})
PNR_RE = re.compile(r"[A-Z]{1,3}[0-9]{6,10}")


class PnrFormatError(ValueError):
    pass


def normalize_pnr(raw) -> str:
    if not isinstance(raw, str):
        raise PnrFormatError("pnr must be a string")
    pnr = raw.strip().upper()
    if not PNR_RE.fullmatch(pnr):
        raise PnrFormatError("PNR must be 1-3 letters followed by 6-10 digits")
    return pnr


def _parse_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class BookingMatch:
    pnr: str
    phone: str
    passenger_name: str
    passenger_names: tuple
    route: str
    travel_date: str
    status: str


def is_eligible(doc: dict, today: date, window_days: int) -> bool:
    status = str(doc.get("status", "")).strip().lower()
    if status not in ELIGIBLE_STATUSES:
        return False
    travel = _parse_date(doc.get("travelDate"))
    return travel is not None and travel >= today - timedelta(days=window_days)


def find_eligible_booking(db, pnr: str, phone: str, today: date, window_days: int):
    """
    Return a BookingMatch when `phone` is the registered phone of at least one
    eligible passenger on `pnr`; otherwise None. Callers must NOT tell the
    client which of (unknown PNR / wrong phone / ineligible) happened.
    """
    docs = [d for d in db[BOOKINGS].find({"pnr": pnr}).sort("passengerSeq", 1)
            if try_normalize_phone(d.get("registeredPhone")) == phone
            and is_eligible(d, today, window_days)]
    if not docs:
        return None
    names = tuple(str(d.get("passengerName", "")).strip() for d in docs)
    lead = docs[0]
    return BookingMatch(
        pnr=pnr,
        phone=phone,
        passenger_name=", ".join(n for n in names if n),
        passenger_names=names,
        route=str(lead.get("route", "")),
        travel_date=str(lead.get("travelDate", "")),
        status=str(lead.get("status", "")).lower(),
    )
