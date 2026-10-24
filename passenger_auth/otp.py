"""
OTP primitives: CSPRNG generation, salted HMAC hashing, constant-time compare.

The raw OTP exists only in memory long enough to hash it and hand it to the SMS
provider. What is stored is HMAC-SHA256(key, salt ‖ pnr ‖ phone ‖ otp) where the
key is derived from SIGNING_SECRET and the salt is random per OTP — a database
dump alone is not enough to brute-force the 10^6 code space.
"""
import hashlib
import hmac
import re
import secrets

OTP_LENGTH = 6
_OTP_RE = re.compile(r"[0-9]{6}")
_SEP = "\x1f"   # unit separator: cannot appear in any validated field


def generate_otp() -> str:
    return f"{secrets.randbelow(10 ** OTP_LENGTH):0{OTP_LENGTH}d}"


def new_salt() -> str:
    return secrets.token_hex(16)


def derive_key(secret: str, purpose: str) -> bytes:
    """Purpose-separated subkey so the OTP hash key is never the cookie-signing key."""
    return hmac.new(secret.encode("utf-8"), purpose.encode("utf-8"), hashlib.sha256).digest()


def hash_otp(key: bytes, salt: str, pnr: str, phone: str, otp: str) -> str:
    msg = _SEP.join((salt, pnr, phone, otp)).encode("utf-8")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def otp_matches(key: bytes, salt: str, pnr: str, phone: str, candidate: str, expected_hash: str) -> bool:
    return hmac.compare_digest(hash_otp(key, salt, pnr, phone, candidate), expected_hash)


def is_valid_otp_format(value) -> bool:
    return isinstance(value, str) and _OTP_RE.fullmatch(value) is not None
