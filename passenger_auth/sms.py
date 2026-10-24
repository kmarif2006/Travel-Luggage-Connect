"""
SMS delivery behind a provider interface, selected by SMS_PROVIDER.

  mock   (default) — logs to the console and the `sms_log` collection. The OTP
                     is redacted unless SMS_MOCK_EXPOSE_OTP=1 (development only).
  twilio           — real SMS through Twilio's REST API, sent off the request
                     path so response time does not reveal whether a PNR exists.

Features 2/3 reuse `send_message` for notifications.
"""
import logging
import threading
from abc import ABC, abstractmethod
from datetime import timedelta

from .config import ConfigError
from .phone import mask_phone
from .timeutil import to_db, utcnow

logger = logging.getLogger(__name__)

SMS_LOG = "sms_log"


def otp_message(otp: str, ttl_seconds: int) -> str:
    minutes = max(1, ttl_seconds // 60)
    return (f"{otp} is your TNSTC Lost & Found verification code. "
            f"It is valid for {minutes} minutes. Do not share it with anyone.")


class SmsProvider(ABC):
    name = "abstract"

    def send_otp(self, to_phone: str, otp: str, ttl_seconds: int) -> None:
        self._deliver(to_phone, otp_message(otp, ttl_seconds),
                      redacted=otp_message("******", ttl_seconds), purpose="otp")

    def send_message(self, to_phone: str, body: str) -> None:
        self._deliver(to_phone, body, redacted=body, purpose="notification")

    @abstractmethod
    def _deliver(self, to_phone: str, body: str, redacted: str, purpose: str) -> None:
        """Send `body`; `redacted` is the variant that is safe to log or persist."""


class MockSmsProvider(SmsProvider):
    name = "mock"

    def __init__(self, db, expose_otp: bool = False, retention_hours: int = 24, clock=utcnow):
        self.collection = db[SMS_LOG]
        self.expose_otp = expose_otp
        self.retention = timedelta(hours=retention_hours)
        self.clock = clock

    def _deliver(self, to_phone, body, redacted, purpose):
        shown = body if (purpose != "otp" or self.expose_otp) else redacted
        now = self.clock()
        logger.info("[MOCK SMS] to=%s purpose=%s: %s", mask_phone(to_phone), purpose, shown)
        self.collection.insert_one({
            "provider":  self.name,
            "to":        mask_phone(to_phone),
            "purpose":   purpose,
            "body":      shown,
            "createdAt": to_db(now),
            "purgeAt":   to_db(now + self.retention),
        })


class TwilioSmsProvider(SmsProvider):
    """Real-provider adapter. Uses the existing `twilio` dependency."""
    name = "twilio"

    def __init__(self, account_sid: str, auth_token: str, from_number: str,
                 client=None, async_send: bool = True):
        if client is None:
            from twilio.rest import Client
            client = Client(account_sid, auth_token)
        self.client = client
        self.from_number = from_number
        self.async_send = async_send

    def _send_now(self, to_phone, body, purpose):
        try:
            msg = self.client.messages.create(body=body, from_=self.from_number, to=to_phone)
            logger.info("Twilio SMS (%s) queued to %s, sid=%s", purpose, mask_phone(to_phone),
                        getattr(msg, "sid", "?"))
        except Exception as e:   # delivery is best-effort; never surface provider errors
            logger.error("Twilio SMS (%s) to %s failed: %s", purpose, mask_phone(to_phone),
                         type(e).__name__)

    def _deliver(self, to_phone, body, redacted, purpose):
        if self.async_send:
            threading.Thread(target=self._send_now, args=(to_phone, body, purpose),
                             daemon=True).start()
        else:
            self._send_now(to_phone, body, purpose)


def build_sms_provider(config, db, environ, clock=utcnow) -> SmsProvider:
    if config.sms_provider == "twilio":
        sid = (environ.get("TWILIO_ACCOUNT_SID") or "").strip()
        token = (environ.get("TWILIO_AUTH_TOKEN") or "").strip()
        sender = (environ.get("TWILIO_SMS_FROM") or environ.get("TWILIO_PHONE_NUMBER") or "").strip()
        if not (sid and token and sender) or "YOUR_" in sid:
            raise ConfigError("SMS_PROVIDER=twilio needs TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN "
                              "and TWILIO_SMS_FROM (or TWILIO_PHONE_NUMBER).")
        return TwilioSmsProvider(sid, token, sender)
    if config.sms_mock_expose_otp:
        logger.warning("SMS_MOCK_EXPOSE_OTP is ON: raw OTPs are written to the mock SMS log. "
                       "Development use only.")
    return MockSmsProvider(db, expose_otp=config.sms_mock_expose_otp,
                           retention_hours=config.record_retention_hours, clock=clock)
