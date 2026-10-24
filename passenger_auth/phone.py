"""
Indian mobile number normalization.

Passengers type numbers in many shapes ("+91 98765 43210", "098765-43210",
"919876543210", "9876543210"). All of them normalize to E.164 "+919876543210"
so they can be compared with the booking's registered phone. Only ASCII digits
are accepted — Unicode digits (Tamil, full-width, …) are rejected rather than
silently converted.
"""
import re

_SEPARATORS = re.compile(r"[\s\-().]", re.ASCII)
_MOBILE = re.compile(r"[6-9][0-9]{9}")


class PhoneFormatError(ValueError):
    """The value is not a recognisable Indian mobile number."""


def normalize_phone(raw) -> str:
    if not isinstance(raw, str):
        raise PhoneFormatError("phone must be a string")
    s = _SEPARATORS.sub("", raw.strip())
    if s.startswith("+"):
        if not s.startswith("+91"):
            raise PhoneFormatError("only Indian (+91) mobile numbers are supported")
        s = s[3:]
    elif s.startswith("0091"):
        s = s[4:]
    elif len(s) == 12 and s.startswith("91"):
        s = s[2:]
    if len(s) == 11 and s.startswith("0"):
        s = s[1:]
    if not _MOBILE.fullmatch(s):
        raise PhoneFormatError("not a valid 10-digit Indian mobile number")
    return "+91" + s


def try_normalize_phone(raw):
    """normalize_phone that returns None instead of raising (for stored data)."""
    try:
        return normalize_phone(raw)
    except PhoneFormatError:
        return None


def mask_phone(e164: str) -> str:
    """'+919876543210' → '+91 ******3210' (safe for logs and the UI)."""
    if not e164 or len(e164) < 4:
        return "******"
    return f"{e164[:3]} ******{e164[-4:]}"
