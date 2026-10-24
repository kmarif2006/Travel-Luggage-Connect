"""
Pickup tokens: a human code (XXXX-XXXX) and an HMAC-signed QR payload.

The code is derived as HMAC(key, claimId | nonce) mapped onto a 32-symbol
alphabet without the look-alikes 0/O and 1/I. The nonce is 16 CSPRNG bytes
stored on the claim; only HMAC(key, code) is stored as `tokenHash`. The server
can therefore re-display the code to its owner, but a database dump alone cannot
produce a valid code. Reissuing draws a new nonce, which revokes the old code
and every QR that embeds it.

QR payload: "TNSTC1.<base64url(json)>.<base64url(hmac)>" with
{v, cid (claimId), iid (itemId), tag (qrTagId), pnr, exp (epoch seconds), code}.
"""
import base64
import hashlib
import hmac
import json
import re
import secrets

ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"     # 32 symbols: no 0/O, no 1/I
CODE_LEN = 8
PREFIX = "TNSTC1"
_CODE_RE = re.compile(rf"[{ALPHABET}]{{{CODE_LEN}}}")
TAG_PREFIX = "TAG-"


class TokenError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def new_nonce() -> str:
    return secrets.token_hex(16)


def derive_code(key: bytes, claim_id: str, nonce: str) -> str:
    digest = hmac.new(key, f"code|{claim_id}|{nonce}".encode(), hashlib.sha256).digest()
    return "".join(ALPHABET[b % len(ALPHABET)] for b in digest[:CODE_LEN])   # 256 % 32 == 0: unbiased


def format_code(code: str) -> str:
    return f"{code[:4]}-{code[4:]}"


def normalize_code(value) -> str:
    if not isinstance(value, str):
        raise TokenError("malformed")
    code = re.sub(r"[\s\-]", "", value).upper()
    if not _CODE_RE.fullmatch(code):
        raise TokenError("malformed")
    return code


def code_hash(key: bytes, code: str) -> str:
    return hmac.new(key, f"pickup|{code}".encode(), hashlib.sha256).hexdigest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign_payload(key: bytes, payload: dict) -> str:
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    sig = _b64(hmac.new(key, f"{PREFIX}.{body}".encode(), hashlib.sha256).digest())
    return f"{PREFIX}.{body}.{sig}"


def verify_payload(key: bytes, value) -> dict:
    """Signature-checked payload. Raises TokenError('malformed' | 'bad_signature')."""
    if not isinstance(value, str) or len(value) > 1024:
        raise TokenError("malformed")
    parts = value.strip().split(".")
    if len(parts) != 3 or parts[0] != PREFIX:
        raise TokenError("malformed")
    try:
        expected = hmac.new(key, f"{PREFIX}.{parts[1]}".encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _unb64(parts[2])):
            raise TokenError("bad_signature")
        payload = json.loads(_unb64(parts[1]))
    except (ValueError, TypeError):
        raise TokenError("malformed")
    if not isinstance(payload, dict) or payload.get("v") != 1 or not isinstance(payload.get("exp"), int):
        raise TokenError("malformed")
    return payload


def is_qr_payload(value) -> bool:
    return isinstance(value, str) and value.strip().startswith(PREFIX + ".")


def new_tag_id() -> str:
    return TAG_PREFIX + "".join(secrets.choice(ALPHABET) for _ in range(CODE_LEN))


def normalize_tag(value) -> str:
    """Accepts 'TAG-XXXXXXXX' or a scanned tag QR 'TNSTC-TAG:TAG-XXXXXXXX'."""
    if not isinstance(value, str):
        raise TokenError("malformed")
    text = value.strip().upper()
    if text.startswith("TNSTC-TAG:"):
        text = text[len("TNSTC-TAG:"):]
    if not re.fullmatch(rf"TAG-[{ALPHABET}]{{{CODE_LEN}}}", text):
        raise TokenError("malformed")
    return text
