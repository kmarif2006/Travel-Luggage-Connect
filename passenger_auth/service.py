"""
OTP request / verify business logic.

otp_sessions document:
    pnr, phone (registered E.164), otpHash, otpSalt, createdAt, expiresAt,
    attempts, maxAttempts, status, cooldownUntil, consumedAt, resendCount, ip, purgeAt
status: active → verified | expired | locked | superseded

Concurrency model — every state transition is ONE conditional MongoDB update,
so the rules hold even with parallel requests across processes:
  * an attempt is reserved ($inc attempts WHERE status=active AND attempts<max
    AND not expired) BEFORE the code is compared, so N parallel guesses can never
    exceed OTP_MAX_ATTEMPTS in total;
  * success is the transition active→verified, which only one request can win
    (single use: replay and parallel duplicates fail);
  * reaching the limit with a wrong code transitions active→locked.
"""
import logging
from dataclasses import dataclass
from datetime import timedelta

from pymongo import ReturnDocument

from .bookings import PnrFormatError, find_eligible_booking, normalize_pnr
from .errors import AuthError
from .otp import derive_key, generate_otp, hash_otp, is_valid_otp_format, new_salt, otp_matches
from .phone import PhoneFormatError, mask_phone, normalize_phone
from .timeutil import from_db, iso_utc, seconds_until, to_db, today_ist

logger = logging.getLogger(__name__)

OTP_SESSIONS = "otp_sessions"
GENERIC_REQUEST_MESSAGE = ("If the PNR and mobile number match an eligible booking, a 6-digit OTP "
                           "has been sent to the registered mobile number.")
INVALID_OTP_MESSAGE = "The OTP is incorrect or no longer valid. Please check it or request a new one."


@dataclass(frozen=True)
class OtpRequestResult:
    masked_phone: str
    expires_in: int
    resend_after: int


def parse_pnr(raw) -> str:
    try:
        return normalize_pnr(raw)
    except PnrFormatError:
        raise AuthError(400, "invalid_request", "Please enter a valid PNR (e.g. T26100101).",
                        fields={"pnr": "invalid_format"})


def parse_phone(raw) -> str:
    try:
        return normalize_phone(raw)
    except PhoneFormatError:
        raise AuthError(400, "invalid_request", "Please enter a valid 10-digit Indian mobile number.",
                        fields={"phone": "invalid_format"})


class OtpService:
    def __init__(self, db, config, sms, limiter, clock):
        self.db = db
        self.otps = db[OTP_SESSIONS]
        self.config = config
        self.sms = sms
        self.limiter = limiter
        self.clock = clock
        self.hash_key = derive_key(config.signing_secret, "tnstc-otp-hash-v1")

    # ── helpers ──────────────────────────────────────────────────────────────
    def _booking(self, pnr, phone):
        return find_eligible_booking(self.db, pnr, phone, today_ist(self.clock()),
                                     self.config.booking_access_window_days)

    def _active_lock(self, pnr, phone):
        return self.otps.find_one(
            {"pnr": pnr, "phone": phone, "status": "locked",
             "cooldownUntil": {"$gt": to_db(self.clock())}},
            sort=[("cooldownUntil", -1)])

    def _locked_error(self, lock_doc):
        until = lock_doc["cooldownUntil"]
        return AuthError(423, "otp_locked",
                         "Too many incorrect attempts. Verification is locked for this booking.",
                         retry_after=max(1, seconds_until(until, self.clock())),
                         lockedUntil=iso_utc(until))

    def _ip_limit(self, scope, ip, limit):
        decision = self.limiter.hit_window(scope, ip, limit, self.config.otp_ip_window_seconds)
        if not decision.allowed:
            raise AuthError(429, "rate_limited", "Too many requests from your network. Please try again later.",
                            retry_after=decision.retry_after)

    # ── POST /auth/otp/request ───────────────────────────────────────────────
    def request_otp(self, pnr_raw, phone_raw, ip) -> OtpRequestResult:
        pnr, phone = parse_pnr(pnr_raw), parse_phone(phone_raw)
        cfg = self.config
        self._ip_limit("ip-otp-request", ip, cfg.otp_ip_request_limit)

        # Per-PNR cap and resend cooldown apply whether or not the PNR exists,
        # so their responses cannot be used to probe for valid PNRs.
        count, retry = self.limiter.recent_events("pnr-otp-request", pnr, 3600)
        if count >= cfg.otp_max_requests_per_pnr_per_hour:
            raise AuthError(429, "pnr_request_limit",
                            "Too many OTP requests for this PNR. Please try again later.",
                            retry_after=retry)

        booking = self._booking(pnr, phone)
        if booking:
            lock = self._active_lock(pnr, phone)
            if lock:
                raise self._locked_error(lock)

        gate = self.limiter.acquire_cooldown("otp-resend", f"{pnr}:{phone}", cfg.otp_resend_seconds)
        if not gate.allowed:
            raise AuthError(429, "resend_cooldown",
                            "Please wait before requesting another OTP.", retry_after=gate.retry_after)
        self.limiter.record_event("pnr-otp-request", pnr, 3600)

        if booking:
            self._issue(booking, ip)
        else:
            logger.info("OTP request for pnr=%s phone=%s: no eligible booking match (generic response).",
                        pnr, mask_phone(phone))
        return OtpRequestResult(mask_phone(phone), cfg.otp_ttl_seconds, cfg.otp_resend_seconds)

    def _issue(self, booking, ip):
        now = self.clock()
        cfg = self.config
        key = {"pnr": booking.pnr, "phone": booking.phone}
        superseded = self.otps.update_many({**key, "status": "active"},
                                           {"$set": {"status": "superseded"}})
        resend_count = self.otps.count_documents(
            {**key, "createdAt": {"$gt": to_db(now - timedelta(hours=1))}})
        otp, salt = generate_otp(), new_salt()
        self.otps.insert_one({
            **key,
            "otpHash":       hash_otp(self.hash_key, salt, booking.pnr, booking.phone, otp),
            "otpSalt":       salt,
            "createdAt":     to_db(now),
            "expiresAt":     to_db(now + timedelta(seconds=cfg.otp_ttl_seconds)),
            "attempts":      0,
            "maxAttempts":   cfg.otp_max_attempts,
            "status":        "active",
            "cooldownUntil": None,
            "consumedAt":    None,
            "resendCount":   resend_count,
            "ip":            ip,
            "purgeAt":       to_db(now + timedelta(seconds=cfg.record_retention_seconds)),
        })
        self.sms.send_otp(booking.phone, otp, cfg.otp_ttl_seconds)
        logger.info("OTP issued for pnr=%s to %s (superseded=%d, resendCount=%d).",
                    booking.pnr, mask_phone(booking.phone), superseded.modified_count, resend_count)

    # ── POST /auth/otp/verify ────────────────────────────────────────────────
    def verify_otp(self, pnr_raw, phone_raw, otp_raw, ip):
        """Returns the BookingMatch on success; raises AuthError otherwise."""
        pnr, phone = parse_pnr(pnr_raw), parse_phone(phone_raw)
        otp = otp_raw.strip() if isinstance(otp_raw, str) else otp_raw
        if not is_valid_otp_format(otp):
            raise AuthError(400, "invalid_request", "The OTP must be exactly 6 digits.",
                            fields={"otp": "invalid_format"})
        cfg = self.config
        self._ip_limit("ip-otp-verify", ip, cfg.otp_ip_verify_limit)

        booking = self._booking(pnr, phone)
        if not booking:
            raise AuthError(401, "otp_invalid", INVALID_OTP_MESSAGE)

        lock = self._active_lock(pnr, phone)
        if lock:
            raise self._locked_error(lock)

        now = self.clock()
        current = self.otps.find_one({"pnr": pnr, "phone": phone, "status": "active"},
                                     sort=[("createdAt", -1)])
        if not current:
            raise AuthError(401, "otp_invalid", INVALID_OTP_MESSAGE)
        if from_db(current["expiresAt"]) <= now:
            self.otps.update_one({"_id": current["_id"], "status": "active"},
                                 {"$set": {"status": "expired"}})
            raise AuthError(401, "otp_expired", "This OTP has expired. Please request a new one.")

        # Reserve one attempt atomically before comparing (see module docstring).
        doc = self.otps.find_one_and_update(
            {"_id": current["_id"], "status": "active",
             "attempts": {"$lt": cfg.otp_max_attempts}, "expiresAt": {"$gt": to_db(now)}},
            {"$inc": {"attempts": 1}},
            return_document=ReturnDocument.AFTER)
        if doc is None:
            return self._explain_unavailable(current["_id"])

        if not otp_matches(self.hash_key, doc["otpSalt"], pnr, phone, otp, doc["otpHash"]):
            if doc["attempts"] >= cfg.otp_max_attempts:
                until = now + timedelta(seconds=cfg.otp_cooldown_seconds)
                self.otps.update_one(
                    {"_id": doc["_id"], "status": "active"},
                    {"$set": {"status": "locked", "cooldownUntil": to_db(until),
                              "purgeAt": to_db(until + timedelta(seconds=cfg.record_retention_seconds))}})
                logger.warning("OTP locked for pnr=%s phone=%s after %d failed attempts.",
                               pnr, mask_phone(phone), doc["attempts"])
                lock = self._active_lock(pnr, phone)
                raise self._locked_error(lock or {"cooldownUntil": to_db(until)})
            raise AuthError(401, "otp_invalid", INVALID_OTP_MESSAGE,
                            attemptsRemaining=cfg.otp_max_attempts - doc["attempts"])

        won = self.otps.find_one_and_update(
            {"_id": doc["_id"], "status": "active"},
            {"$set": {"status": "verified", "consumedAt": to_db(now)}})
        if won is None:
            # A parallel request with the same code consumed it first.
            raise AuthError(401, "otp_invalid", INVALID_OTP_MESSAGE)
        logger.info("OTP verified for pnr=%s phone=%s.", pnr, mask_phone(phone))
        return booking

    def _explain_unavailable(self, otp_id):
        """The attempt reservation failed: report the state that caused it."""
        doc = self.otps.find_one({"_id": otp_id}) or {}
        if doc.get("status") == "locked" and doc.get("cooldownUntil") is not None \
                and from_db(doc["cooldownUntil"]) > self.clock():
            raise self._locked_error(doc)
        if doc.get("status") in ("active", "expired") and doc.get("expiresAt") is not None \
                and from_db(doc["expiresAt"]) <= self.clock():
            raise AuthError(401, "otp_expired", "This OTP has expired. Please request a new one.")
        raise AuthError(401, "otp_invalid", INVALID_OTP_MESSAGE)
