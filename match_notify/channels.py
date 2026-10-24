"""
Outbound message channels behind one interface, selected by env.

  WhatsApp: WHATSAPP_PROVIDER = mock (default) | twilio | meta (WhatsApp Business Cloud API)
  Email:    EMAIL_PROVIDER    = mock (default) | smtp

A channel's `send(to, message)` either returns a provider reference or raises
ChannelError. `permanent=True` means retrying cannot help (bad number, rejected
address), so the job falls back to the next channel immediately; otherwise the
job retries with exponential backoff.
"""
import logging
import smtplib
from abc import ABC, abstractmethod
from datetime import timedelta
from email.message import EmailMessage

import requests

from passenger_auth.config import ConfigError
from passenger_auth.phone import mask_phone
from passenger_auth.timeutil import to_db, utcnow

logger = logging.getLogger(__name__)

MESSAGE_LOG = "message_log"
WHATSAPP = "whatsapp"
EMAIL = "email"


class ChannelError(Exception):
    def __init__(self, message: str, permanent: bool = False):
        super().__init__(message)
        self.permanent = permanent


def mask_email(address: str) -> str:
    if not address or "@" not in address:
        return "***"
    local, _, domain = address.partition("@")
    return f"{local[:1]}***@{domain}"


def mask_contact(channel: str, to: str) -> str:
    return mask_phone(to) if channel == WHATSAPP else mask_email(to)


class MessageChannel(ABC):
    channel = "abstract"
    name = "abstract"

    @abstractmethod
    def send(self, to: str, message) -> str:
        """Deliver `message` (a MatchMessage); return a provider reference."""


class MockChannel(MessageChannel):
    """Logs to the console and the `message_log` collection. `failure` simulates outages."""
    name = "mock"

    def __init__(self, channel: str, db, failure: str = "none", clock=utcnow):
        self.channel = channel
        self.log = db[MESSAGE_LOG]
        self.failure = failure
        self.clock = clock

    def send(self, to, message):
        if self.failure == "transient":
            raise ChannelError(f"mock {self.channel} provider unavailable (simulated outage)")
        if self.failure == "permanent":
            raise ChannelError(f"mock {self.channel} provider rejected the recipient", permanent=True)
        now = self.clock()
        result = self.log.insert_one({
            "channel": self.channel, "provider": "mock", "to": mask_contact(self.channel, to),
            "subject": message.subject, "body": message.text,
            "createdAt": to_db(now), "purgeAt": to_db(now + timedelta(days=30)),
        })
        logger.info("[MOCK %s] to=%s: %s", self.channel.upper(), mask_contact(self.channel, to),
                    message.subject)
        return f"mock-{result.inserted_id}"


class TwilioWhatsAppChannel(MessageChannel):
    channel, name = WHATSAPP, "twilio"

    def __init__(self, account_sid, auth_token, from_number, client=None):
        if client is None:
            from twilio.rest import Client
            client = Client(account_sid, auth_token)
        self.client = client
        self.from_number = from_number

    def send(self, to, message):
        try:
            msg = self.client.messages.create(body=f"*{message.subject}*\n\n{message.text}",
                                              from_=f"whatsapp:{self.from_number}", to=f"whatsapp:{to}")
        except Exception as e:
            status = getattr(e, "status", None)
            raise ChannelError(f"twilio error {type(e).__name__}",
                               permanent=status is not None and 400 <= int(status) < 500 and int(status) != 429)
        return getattr(msg, "sid", "twilio")


class MetaWhatsAppChannel(MessageChannel):
    """
    WhatsApp Business Cloud API. Free-form text is only delivered inside the 24-hour
    customer-service window; production use needs an approved template (README).
    """
    channel, name = WHATSAPP, "meta"

    def __init__(self, token, phone_number_id, api_version="v19.0", session=None, timeout=10):
        self.url = f"https://graph.facebook.com/{api_version}/{phone_number_id}/messages"
        self.token = token
        self.http = session or requests
        self.timeout = timeout

    def send(self, to, message):
        payload = {"messaging_product": "whatsapp", "to": to.lstrip("+"), "type": "text",
                   "text": {"preview_url": False, "body": f"*{message.subject}*\n\n{message.text}"}}
        try:
            resp = self.http.post(self.url, json=payload, timeout=self.timeout,
                                  headers={"Authorization": f"Bearer {self.token}"})
        except requests.RequestException as e:
            raise ChannelError(f"meta network error {type(e).__name__}")
        if resp.status_code >= 400:
            permanent = 400 <= resp.status_code < 500 and resp.status_code != 429
            raise ChannelError(f"meta HTTP {resp.status_code}", permanent=permanent)
        try:
            return resp.json()["messages"][0]["id"]
        except (ValueError, KeyError, IndexError, TypeError):
            return "meta"


class SmtpEmailChannel(MessageChannel):
    channel, name = EMAIL, "smtp"

    def __init__(self, host, port, sender, username=None, password=None, starttls=True,
                 smtp_factory=smtplib.SMTP, timeout=10):
        self.host, self.port, self.sender = host, port, sender
        self.username, self.password, self.starttls = username, password, starttls
        self.smtp_factory, self.timeout = smtp_factory, timeout

    def send(self, to, message):
        mail = EmailMessage()
        mail["Subject"] = message.subject
        mail["From"] = self.sender
        mail["To"] = to
        mail.set_content(message.text, charset="utf-8")
        mail.add_alternative(f"<!doctype html><html><body>{message.html}</body></html>",
                             subtype="html", charset="utf-8")
        try:
            with self.smtp_factory(self.host, self.port, timeout=self.timeout) as smtp:
                if self.starttls:
                    smtp.starttls()
                if self.username:
                    smtp.login(self.username, self.password or "")
                smtp.send_message(mail)
        except smtplib.SMTPRecipientsRefused:
            raise ChannelError("smtp recipient refused", permanent=True)
        except (smtplib.SMTPException, OSError) as e:
            raise ChannelError(f"smtp error {type(e).__name__}")
        return mail.get("Message-ID") or "smtp"


def build_channels(config, db, environ, clock=utcnow) -> dict:
    """{'whatsapp': MessageChannel, 'email': MessageChannel} per env selection."""
    env = environ
    if config.whatsapp_provider == "twilio":
        sid, token = env.get("TWILIO_ACCOUNT_SID", ""), env.get("TWILIO_AUTH_TOKEN", "")
        sender = env.get("TWILIO_PHONE_NUMBER", "")
        if not (sid and token and sender) or "YOUR_" in sid:
            raise ConfigError("WHATSAPP_PROVIDER=twilio needs TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, "
                              "TWILIO_PHONE_NUMBER.")
        whatsapp = TwilioWhatsAppChannel(sid, token, sender)
    elif config.whatsapp_provider == "meta":
        token, number_id = env.get("WHATSAPP_META_TOKEN", ""), env.get("WHATSAPP_META_PHONE_NUMBER_ID", "")
        if not (token and number_id):
            raise ConfigError("WHATSAPP_PROVIDER=meta needs WHATSAPP_META_TOKEN and "
                              "WHATSAPP_META_PHONE_NUMBER_ID.")
        whatsapp = MetaWhatsAppChannel(token, number_id, env.get("WHATSAPP_META_API_VERSION") or "v19.0")
    else:
        whatsapp = MockChannel(WHATSAPP, db, config.mock_whatsapp_failure, clock)

    if config.email_provider == "smtp":
        host, sender = env.get("SMTP_HOST", ""), env.get("SMTP_FROM", "")
        if not (host and sender):
            raise ConfigError("EMAIL_PROVIDER=smtp needs SMTP_HOST and SMTP_FROM.")
        try:
            port = int(env.get("SMTP_PORT") or 587)
        except ValueError:
            raise ConfigError("SMTP_PORT must be an integer.")
        email = SmtpEmailChannel(host, port, sender, env.get("SMTP_USER") or None,
                                 env.get("SMTP_PASSWORD") or None,
                                 (env.get("SMTP_STARTTLS") or "1").strip() not in ("0", "false", "no"))
    else:
        email = MockChannel(EMAIL, db, config.mock_email_failure, clock)
    return {WHATSAPP: whatsapp, EMAIL: email}
