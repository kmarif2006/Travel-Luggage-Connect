"""
Feature 1 — OTP-based passenger authentication (PNR + registered mobile).

Wire into the app with:

    from passenger_auth import init_passenger_auth
    init_passenger_auth(app, db)

Protect passenger routes (Features 2 and 3) with:

    from passenger_auth import require_passenger, current_passenger

    @app.route("/api/my-claims")
    @require_passenger
    def my_claims():
        p = current_passenger()        # p.pnr, p.phone, p.passenger_name
"""
import logging
import os
from dataclasses import dataclass

from flask import request

from .bookings import BOOKINGS
from .config import AuthConfig, ConfigError
from .ratelimit import COUNTERS, EVENTS, RateLimiter
from .routes import bp, current_passenger, require_passenger
from .service import OTP_SESSIONS, OtpService
from .sessions import COOKIE_NAME, PASSENGER_SESSIONS, Passenger, SessionError, SessionManager
from .sms import SMS_LOG, SmsProvider, build_sms_provider
from .timeutil import fmt_ist, utcnow

__all__ = ["init_passenger_auth", "require_passenger", "current_passenger", "Passenger",
           "AuthConfig", "ConfigError", "ensure_auth_indexes"]

logger = logging.getLogger(__name__)


@dataclass
class PassengerAuthState:
    db: object
    config: AuthConfig
    clock: object
    sms: SmsProvider
    sessions: SessionManager
    otp: OtpService


def ensure_auth_indexes(db) -> None:
    """Idempotent. TTL indexes on `purgeAt` give automatic cleanup of old records."""
    specs = [
        (BOOKINGS,           [("pnr", 1), ("passengerSeq", 1)], {"unique": True}),
        (OTP_SESSIONS,       [("pnr", 1), ("phone", 1), ("status", 1), ("createdAt", -1)], {}),
        (OTP_SESSIONS,       [("purgeAt", 1)], {"expireAfterSeconds": 0}),
        (PASSENGER_SESSIONS, [("sidHash", 1)], {"unique": True}),
        (PASSENGER_SESSIONS, [("purgeAt", 1)], {"expireAfterSeconds": 0}),
        (COUNTERS,           [("purgeAt", 1)], {"expireAfterSeconds": 0}),
        (EVENTS,             [("key", 1), ("at", 1)], {}),
        (EVENTS,             [("purgeAt", 1)], {"expireAfterSeconds": 0}),
        (SMS_LOG,            [("purgeAt", 1)], {"expireAfterSeconds": 0}),
    ]
    for name, keys, opts in specs:
        try:
            db[name].create_index(keys, **opts)
        except Exception as e:
            logger.warning(f"Index creation skipped for {name} {keys}: {e}")


def init_passenger_auth(app, db, config=None, sms_provider=None, clock=None, environ=None):
    env = os.environ if environ is None else environ
    config = config or AuthConfig.from_env(env)
    clock = clock or utcnow
    sms = sms_provider or build_sms_provider(config, db, env, clock=clock)
    limiter = RateLimiter(db, clock)
    state = PassengerAuthState(
        db=db, config=config, clock=clock, sms=sms,
        sessions=SessionManager(db, config, clock),
        otp=OtpService(db, config, sms, limiter, clock),
    )
    app.extensions["passenger_auth"] = state
    ensure_auth_indexes(db)
    app.register_blueprint(bp)
    app.add_template_filter(fmt_ist, "ist")

    @app.context_processor
    def _inject_passenger():
        # Lazily resolved so pages that never show it pay no DB lookup.
        def nav_passenger():
            token = request.cookies.get(COOKIE_NAME)
            if not token:
                return None
            try:
                return state.sessions.load(token)
            except SessionError:
                return None
        return {"nav_passenger": nav_passenger}

    logger.info("Passenger OTP auth enabled (SMS provider: %s).", sms.name)
    return state
