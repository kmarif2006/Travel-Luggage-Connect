"""
Environment-driven configuration for passenger OTP authentication.

Every knob lives in an env var (documented in .env.example). Values are parsed
and bounds-checked once at startup so a bad deployment fails loudly instead of
silently weakening the OTP rules.
"""
import logging
import os
import secrets
from dataclasses import dataclass

logger = logging.getLogger(__name__)


class ConfigError(ValueError):
    """Raised at startup when an auth setting is missing or out of range."""


def _int(environ, name, default, lo, hi):
    raw = environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}")
    if not lo <= value <= hi:
        raise ConfigError(f"{name} must be between {lo} and {hi}, got {value}")
    return value


def _bool(environ, name, default):
    raw = environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be a boolean (1/0/true/false), got {raw!r}")


@dataclass(frozen=True)
class AuthConfig:
    signing_secret: str
    signing_secret_ephemeral: bool = False
    otp_ttl_seconds: int = 300
    otp_max_attempts: int = 3
    otp_cooldown_minutes: int = 15
    otp_resend_seconds: int = 30
    otp_max_requests_per_pnr_per_hour: int = 5
    otp_ip_request_limit: int = 20
    otp_ip_verify_limit: int = 30
    otp_ip_window_seconds: int = 3600
    session_ttl_seconds: int = 1800
    sms_provider: str = "mock"
    sms_mock_expose_otp: bool = False
    cookie_secure: bool = True
    cookie_samesite: str = "Lax"
    booking_access_window_days: int = 90
    record_retention_hours: int = 24

    @property
    def otp_cooldown_seconds(self) -> int:
        return self.otp_cooldown_minutes * 60

    @property
    def record_retention_seconds(self) -> int:
        return self.record_retention_hours * 3600

    @classmethod
    def from_env(cls, environ=None) -> "AuthConfig":
        env = os.environ if environ is None else environ

        secret = (env.get("SIGNING_SECRET") or "").strip()
        ephemeral = False
        if not secret:
            # Fail safe rather than fall back to a guessable constant: a random
            # per-process secret means sessions/OTPs do not survive a restart,
            # which is acceptable in development and loudly logged.
            secret = secrets.token_urlsafe(48)
            ephemeral = True
            logger.warning("SIGNING_SECRET is not set; using an ephemeral random secret. "
                           "Passenger sessions and pending OTPs will not survive a restart.")
        elif len(secret) < 32:
            raise ConfigError("SIGNING_SECRET must be at least 32 characters long.")

        sms_provider = (env.get("SMS_PROVIDER") or "mock").strip().lower()
        if sms_provider not in ("mock", "twilio"):
            raise ConfigError(f"SMS_PROVIDER must be 'mock' or 'twilio', got {sms_provider!r}")

        samesite = (env.get("PASSENGER_COOKIE_SAMESITE") or "Lax").strip().capitalize()
        if samesite not in ("Lax", "Strict", "None"):
            raise ConfigError("PASSENGER_COOKIE_SAMESITE must be Lax, Strict or None.")
        secure = _bool(env, "PASSENGER_COOKIE_SECURE", True)
        if samesite == "None" and not secure:
            raise ConfigError("PASSENGER_COOKIE_SAMESITE=None requires PASSENGER_COOKIE_SECURE=1.")

        cfg = cls(
            signing_secret=secret,
            signing_secret_ephemeral=ephemeral,
            otp_ttl_seconds=_int(env, "OTP_TTL_SECONDS", 300, 60, 900),
            otp_max_attempts=_int(env, "OTP_MAX_ATTEMPTS", 3, 1, 10),
            otp_cooldown_minutes=_int(env, "OTP_COOLDOWN_MINUTES", 15, 1, 1440),
            otp_resend_seconds=_int(env, "OTP_RESEND_SECONDS", 30, 5, 600),
            otp_max_requests_per_pnr_per_hour=_int(env, "OTP_MAX_REQUESTS_PER_PNR_PER_HOUR", 5, 1, 50),
            otp_ip_request_limit=_int(env, "OTP_IP_REQUEST_LIMIT", 20, 1, 10000),
            otp_ip_verify_limit=_int(env, "OTP_IP_VERIFY_LIMIT", 30, 1, 10000),
            otp_ip_window_seconds=_int(env, "OTP_IP_WINDOW_SECONDS", 3600, 60, 86400),
            session_ttl_seconds=_int(env, "SESSION_TTL", 1800, 60, 86400),
            sms_provider=sms_provider,
            sms_mock_expose_otp=_bool(env, "SMS_MOCK_EXPOSE_OTP", False),
            cookie_secure=secure,
            cookie_samesite=samesite,
            booking_access_window_days=_int(env, "BOOKING_ACCESS_WINDOW_DAYS", 90, 1, 3650),
            record_retention_hours=_int(env, "OTP_RECORD_RETENTION_HOURS", 24, 2, 720),
        )
        # OTP / lock / session records are TTL-purged after the retention window,
        # so the window must outlive every state that is still being enforced.
        if cfg.record_retention_seconds <= max(cfg.otp_cooldown_seconds,
                                               cfg.session_ttl_seconds, 3600):
            raise ConfigError("OTP_RECORD_RETENTION_HOURS must exceed the lock cooldown, "
                              "the session TTL and the 1-hour per-PNR window.")
        return cfg
