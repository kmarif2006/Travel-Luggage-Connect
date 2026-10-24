"""
Privacy-safe match notification message.

Only these facts ever leave the system: passenger's own name, report tracking ID,
match confidence, depot name, the depot's short PUBLIC description of the item
and the date it was found, plus a login-protected link. Photos, private depot
notes, contents, serial numbers and other matched reports are never included.

Untrusted text (item description, names) is never evaluated as a template: it
is inserted as a value, normalised, stripped of markup and control/bidi
characters, redacted for identifiers, truncated, and HTML-escaped for the HTML
email part. Tamil and other Unicode text passes through intact (NFC).
"""
import html
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
REDACTED = "[redacted]"

_TAG = re.compile(r"<[^>]*>|<[^>]*$")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
_LONG_NUMBER = re.compile(r"(?<!\w)\+?\d[\d\s\-./]{2,}\d(?!\w)")
_MONEY = re.compile(r"(?:\brs\.?|\binr\b|₹)\s*\d[\d,.]*|\b\d{1,3}(?:,\d{2,3})+\b", re.IGNORECASE)
_SERIAL = re.compile(r"\b(?=[A-Za-z0-9\-]*\d)(?=[A-Za-z0-9\-]*[A-Za-z])[A-Za-z0-9][A-Za-z0-9\-]{5,}\b")
_SPACES = re.compile(r"\s+")
_KEEP_FORMAT_CHARS = {"‌", "‍"}   # ZWNJ/ZWJ are legitimate in Indic scripts


def _strip_controls(text: str) -> str:
    out = []
    for ch in text:
        cat = unicodedata.category(ch)
        if cat == "Cc" or (cat == "Cf" and ch not in _KEEP_FORMAT_CHARS):
            out.append(" " if ch in "\t\n\r" else "")
        else:
            out.append(ch)
    return "".join(out)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    if space >= limit - 20:
        cut = cut[:space]
    return cut.rstrip(" ,.;:-") + "…"


def clean_text(value, limit: int) -> str:
    """Normalise untrusted text for display (no redaction)."""
    if not isinstance(value, str):
        return ""
    text = unicodedata.normalize("NFC", value)
    text = _strip_controls(text)
    text = _TAG.sub(" ", text)
    text = _SPACES.sub(" ", text).strip()
    return _truncate(text, limit)


def public_item_description(value, limit: int) -> str:
    """Clean + redact identifiers (emails, money amounts, long numbers, serial-like tokens)."""
    if not isinstance(value, str):
        return ""
    text = unicodedata.normalize("NFC", value)
    text = _strip_controls(text)
    text = _TAG.sub(" ", text)
    text = _EMAIL.sub(REDACTED, text)
    text = _MONEY.sub(REDACTED, text)
    text = _LONG_NUMBER.sub(REDACTED, text)
    text = _SERIAL.sub(REDACTED, text)
    text = _SPACES.sub(" ", text).strip()
    return _truncate(text, limit)


def format_found_date(value) -> str:
    """'2026-10-24' (an IST calendar date) → '24-Oct-2026'. Locale-independent."""
    if isinstance(value, datetime):
        d = value.date()
    elif isinstance(value, date):
        d = value
    else:
        try:
            d = datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
        except (TypeError, ValueError):
            return "—"
    return f"{d.day:02d}-{MONTHS[d.month - 1]}-{d.year}"


@dataclass(frozen=True)
class MatchMessage:
    subject: str
    text: str
    html: str


def render_match_message(*, passenger_name, report_tracking_id, confidence, depot_name,
                         public_description, date_found, hold_days, report_url,
                         description_limit=80) -> MatchMessage:
    name = clean_text(passenger_name, 60) or "Passenger"
    report = clean_text(report_tracking_id, 20)
    depot = clean_text(depot_name, 80) or "a TNSTC depot"
    description = public_item_description(public_description, description_limit) \
        or "Details withheld - please verify at the depot"
    found = format_found_date(date_found)
    percent = max(0, min(100, round(float(confidence) * 100)))

    subject = f"[MATCH FOUND] Your lost item may have been located - Report {report}"
    lines = [
        f"Dear {name},",
        "",
        "Good news! We have found a potential match for your lost item.",
        "",
        f"Match Confidence: {percent}%",
        f"Found At: {depot}",
        f'Item Description (Depot): "{description}"',
        f"Date Found: {found} IST",
        "",
        "Login to your report page to view the match and initiate your claim:",
        report_url,
        "",
        f"This match will be held for {hold_days} days. After that, it will be released.",
        "",
        "Regards,",
        "TNSTC Luggage Recovery System",
    ]
    text = "\n".join(lines)

    e = html.escape
    body_html = (
        f"<p>Dear {e(name)},</p>"
        "<p>Good news! We have found a potential match for your lost item.</p>"
        "<ul>"
        f"<li>Match Confidence: {percent}%</li>"
        f"<li>Found At: {e(depot)}</li>"
        f"<li>Item Description (Depot): &quot;{e(description)}&quot;</li>"
        f"<li>Date Found: {e(found)} IST</li>"
        "</ul>"
        f'<p><a href="{e(report_url, quote=True)}">Login to your report page</a> to view the match '
        "and initiate your claim.</p>"
        f"<p>This match will be held for {hold_days} days. After that, it will be released.</p>"
        "<p>Regards,<br>TNSTC Luggage Recovery System</p>"
    )
    return MatchMessage(subject=subject, text=text, html=body_html)
