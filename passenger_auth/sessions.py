"""
Passenger sessions: a signed cookie that points at a server-side record.

Cookie `tnstc_pax` (httpOnly, Secure, SameSite) carries an itsdangerous-signed
payload {sid, pnr, ph, exp}. The server keeps `passenger_sessions` keyed by
SHA-256(sid), so a session can be revoked (logout) even though the cookie is
still validly signed, and a stolen database row cannot be turned into a cookie.
A session is accepted only if the signature, the cookie expiry, the server
record, its expiry/revocation and the (pnr, phone) binding all agree.
"""
import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from itsdangerous import BadSignature, URLSafeSerializer

from .timeutil import from_db, to_db

PASSENGER_SESSIONS = "passenger_sessions"
COOKIE_NAME = "tnstc_pax"


class SessionError(Exception):
    """reason: 'missing' | 'invalid' | 'expired' | 'revoked'."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Passenger:
    """The authenticated identity exposed to handlers via require_passenger."""
    pnr: str
    phone: str
    passenger_name: str
    session_id: str
    expires_at: datetime


def _sid_hash(sid: str) -> str:
    return hashlib.sha256(sid.encode("ascii")).hexdigest()


class SessionManager:
    def __init__(self, db, config, clock):
        self.collection = db[PASSENGER_SESSIONS]
        self.config = config
        self.clock = clock
        self.serializer = URLSafeSerializer(config.signing_secret, salt="tnstc-passenger-session")

    def issue(self, booking, ip: str, user_agent: str):
        """Create a session for a verified booking. Returns (cookie_token, Passenger)."""
        now = self.clock()
        expires = now + timedelta(seconds=self.config.session_ttl_seconds)
        sid = secrets.token_urlsafe(32)
        self.collection.insert_one({
            "sidHash":       _sid_hash(sid),
            "pnr":           booking.pnr,
            "phone":         booking.phone,
            "passengerName": booking.passenger_name,
            "createdAt":     to_db(now),
            "expiresAt":     to_db(expires),
            "revokedAt":     None,
            "ip":            ip,
            "userAgent":     (user_agent or "")[:200],
            "purgeAt":       to_db(now + timedelta(seconds=self.config.record_retention_seconds)),
        })
        token = self.serializer.dumps({"sid": sid, "pnr": booking.pnr, "ph": booking.phone,
                                       "exp": int(expires.timestamp())})
        return token, Passenger(booking.pnr, booking.phone, booking.passenger_name,
                                _sid_hash(sid), from_db(to_db(expires)))

    def _decode(self, token):
        if not token:
            raise SessionError("missing")
        if len(token) > 1024:
            raise SessionError("invalid")
        try:
            payload = self.serializer.loads(token)
        except BadSignature:
            raise SessionError("invalid")
        if not (isinstance(payload, dict) and isinstance(payload.get("sid"), str)
                and isinstance(payload.get("pnr"), str) and isinstance(payload.get("ph"), str)
                and isinstance(payload.get("exp"), int)):
            raise SessionError("invalid")
        return payload

    def load(self, token) -> Passenger:
        payload = self._decode(token)
        now = self.clock()
        if payload["exp"] <= now.timestamp():
            raise SessionError("expired")
        doc = self.collection.find_one({"sidHash": _sid_hash(payload["sid"])})
        if not doc or doc.get("pnr") != payload["pnr"] or doc.get("phone") != payload["ph"]:
            raise SessionError("invalid")
        if doc.get("revokedAt") is not None:
            raise SessionError("revoked")
        if from_db(doc["expiresAt"]) <= now:
            raise SessionError("expired")
        return Passenger(doc["pnr"], doc["phone"], doc.get("passengerName", ""),
                         doc["sidHash"], from_db(doc["expiresAt"]))

    def revoke(self, session_id: str) -> None:
        self.collection.update_one({"sidHash": session_id, "revokedAt": None},
                                   {"$set": {"revokedAt": to_db(self.clock())}})

    def revoke_token(self, token) -> None:
        """Best-effort revoke from a raw cookie (logout must work even if expired)."""
        try:
            payload = self._decode(token)
        except SessionError:
            return
        self.revoke(_sid_hash(payload["sid"]))

    def set_cookie(self, response, token: str) -> None:
        response.set_cookie(COOKIE_NAME, token, max_age=self.config.session_ttl_seconds,
                            httponly=True, secure=self.config.cookie_secure,
                            samesite=self.config.cookie_samesite, path="/")

    def clear_cookie(self, response) -> None:
        response.delete_cookie(COOKIE_NAME, path="/", httponly=True,
                               secure=self.config.cookie_secure,
                               samesite=self.config.cookie_samesite)
