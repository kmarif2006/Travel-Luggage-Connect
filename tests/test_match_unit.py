"""Unit tests for Feature 2: routing, template/sanitization, backoff, dedupe, config, channels."""
import smtplib
from email import message_from_bytes, policy

import pytest
import requests

from conftest import make_f2_config
from match_notify.channels import (EMAIL, WHATSAPP, ChannelError, MetaWhatsAppChannel, MockChannel,
                                   SmtpEmailChannel, TwilioWhatsAppChannel, build_channels, mask_email)
from match_notify.config import MatchNotifyConfig
from match_notify.routing import AUTO, REJECT, REVIEW, backoff_seconds, dedupe_key, route_confidence
from match_notify.template import (clean_text, format_found_date, public_item_description,
                                   render_match_message)
from passenger_auth.config import ConfigError

TAMIL = "கருப்பு பை மற்றும் குடை"   # "black bag and umbrella"


def _message(**kw):
    base = dict(passenger_name="Arun Kumar", report_tracking_id="TRK-AB12CD34", confidence=0.876,
                depot_name="Chennai CMBT Depot", public_description="Black backpack",
                date_found="2026-10-21", hold_days=7,
                report_url="https://lostfound.tnstc.example/passenger/reports/TRK-AB12CD34")
    base.update(kw)
    return render_match_message(**base)


# ── Confidence routing (inclusive thresholds) ────────────────────────────────
@pytest.mark.parametrize("confidence,expected", [
    (1.0, AUTO), (0.85, AUTO), (0.8499, REVIEW), (0.70, REVIEW), (0.60, REVIEW),
    (0.5999, REJECT), (0.30, REJECT), (0.0, REJECT),
])
def test_route_confidence_default_thresholds(confidence, expected):
    assert route_confidence(confidence, MatchNotifyConfig()) == expected


def test_route_confidence_uses_configured_thresholds():
    cfg = MatchNotifyConfig(auto_notify_min=0.9, review_min=0.5)
    assert [route_confidence(c, cfg) for c in (0.9, 0.89, 0.5, 0.49)] == [AUTO, REVIEW, REVIEW, REJECT]


# ── Dedupe key and backoff ───────────────────────────────────────────────────
def test_dedupe_key_is_report_item_channel():
    assert dedupe_key("r1", "i1", "whatsapp") == "r1:i1:whatsapp"
    assert len({dedupe_key("r1", "i1", c) for c in ("whatsapp", "email", "in_app")}) == 3
    assert dedupe_key("r1", "i2", "email") != dedupe_key("r2", "i1", "email")


def test_backoff_is_exponential_and_capped():
    cfg = MatchNotifyConfig(backoff_base_seconds=30, backoff_max_seconds=100)
    assert [backoff_seconds(n, cfg) for n in (1, 2, 3, 4)] == [30, 60, 100, 100]
    assert [backoff_seconds(n, MatchNotifyConfig()) for n in (1, 2, 3)] == [30, 60, 120]


# ── Template: content, privacy, sanitization ─────────────────────────────────
def test_template_matches_spec_wording():
    m = _message()
    assert m.subject == "[MATCH FOUND] Your lost item may have been located - Report TRK-AB12CD34"
    for line in ("Dear Arun Kumar,", "Good news! We have found a potential match for your lost item.",
                 "Match Confidence: 88%", "Found At: Chennai CMBT Depot",
                 'Item Description (Depot): "Black backpack"', "Date Found: 21-Oct-2026 IST",
                 "This match will be held for 7 days. After that, it will be released.",
                 "Regards,", "TNSTC Luggage Recovery System",
                 "https://lostfound.tnstc.example/passenger/reports/TRK-AB12CD34"):
        assert line in m.text


def test_template_redacts_identifiers_from_description():
    m = _message(public_description="Black bag SN 7X2K9Q, phone 98765 43210, owner a.b@x.com, Rs 2,300")
    for leaked in ("7X2K9Q", "98765", "43210", "a.b@x.com", "2,300"):
        assert leaked not in m.text and leaked not in m.html
    assert "[redacted]" in m.text


@pytest.mark.parametrize("payload", [
    "<script>alert(1)</script>Bag", "<img src=x onerror=alert(1)>Bag", "Bag</li><li>Injected",
    "{{7*7}} Bag", "{% raw %}{name}{% endraw %} Bag", "${7*7} Bag", "Bag‮evil", "Bag\x00\x07",
])
def test_template_is_injection_safe(payload):
    m = _message(public_description=payload, passenger_name=payload)
    assert "<script" not in m.html and "onerror" not in m.html and "<img" not in m.html
    assert "</li><li>Injected" not in m.html
    assert "49" not in m.text                                   # never evaluated as a template
    assert "‮" not in m.text and "\x00" not in m.text and "\x07" not in m.text
    assert "Dear " in m.text


def test_html_part_escapes_markup_characters():
    m = _message(public_description='Bag "quoted" & <b>bold</b>')
    assert "&amp;" in m.html and "<b>" not in m.html


def test_very_long_description_is_truncated():
    m = _message(public_description="very long bag description " * 200)
    line = next(l for l in m.text.splitlines() if l.startswith("Item Description"))
    assert len(line) < 130 and line.endswith('…"')


def test_tamil_text_round_trips_nfc():
    import unicodedata
    decomposed = unicodedata.normalize("NFD", TAMIL)
    m = _message(public_description=decomposed, passenger_name="கார்த்திக் செல்வம்")
    assert TAMIL in m.text and "கார்த்திக் செல்வம்" in m.text
    assert unicodedata.is_normalized("NFC", m.text)


def test_tamil_truncation_keeps_valid_text():
    text = public_item_description("கருப்பு " * 40, 30)
    assert text.endswith("…") and len(text) <= 31
    assert "�" not in text


def test_missing_public_description_is_withheld():
    assert 'Details withheld' in _message(public_description=None).text
    assert 'Details withheld' in _message(public_description="<b></b>").text


@pytest.mark.parametrize("value,expected", [
    ("2026-10-21", "21-Oct-2026"), ("2026-01-05", "05-Jan-2026"), ("garbage", "—"), (None, "—"),
])
def test_found_date_format(value, expected):
    assert format_found_date(value) == expected


def test_confidence_percent_is_clamped():
    assert "Match Confidence: 100%" in _message(confidence=1.7).text
    assert "Match Confidence: 0%" in _message(confidence=-1).text


def test_clean_text_rejects_non_strings():
    assert clean_text({"$ne": 1}, 10) == "" and clean_text(None, 10) == ""


# ── Config ───────────────────────────────────────────────────────────────────
def test_config_defaults():
    cfg = MatchNotifyConfig.from_env({})
    assert (cfg.auto_notify_min, cfg.review_min, cfg.hold_days, cfg.max_attempts) == (0.85, 0.60, 7, 4)
    assert (cfg.whatsapp_provider, cfg.email_provider) == ("mock", "mock")


@pytest.mark.parametrize("env", [
    {"MATCH_AUTO_NOTIFY_MIN": "0.5", "MATCH_REVIEW_MIN": "0.7"},
    {"MATCH_AUTO_NOTIFY_MIN": "1.5"}, {"NOTIFY_MAX_ATTEMPTS": "9"}, {"NOTIFY_MAX_ATTEMPTS": "0"},
    {"WHATSAPP_PROVIDER": "pigeon"}, {"APP_BASE_URL": "javascript:alert(1)"}, {"MATCH_HOLD_DAYS": "x"},
])
def test_config_rejects_bad_values(env):
    with pytest.raises(ConfigError):
        MatchNotifyConfig.from_env(env)


def test_real_providers_need_credentials(db):
    with pytest.raises(ConfigError):
        build_channels(MatchNotifyConfig(whatsapp_provider="meta"), db, {})
    with pytest.raises(ConfigError):
        build_channels(MatchNotifyConfig(email_provider="smtp"), db, {})
    channels = build_channels(MatchNotifyConfig(), db, {})
    assert channels[WHATSAPP].name == channels[EMAIL].name == "mock"


# ── Channels ─────────────────────────────────────────────────────────────────
def test_mock_channel_logs_masked_and_can_simulate_outages(db, clock):
    msg = _message()
    MockChannel(EMAIL, db, clock=clock).send("arun.kumar@example.com", msg)
    entry = db.message_log.find_one()
    assert entry["to"] == "a***@example.com" and entry["subject"] == msg.subject
    with pytest.raises(ChannelError) as e:
        MockChannel(WHATSAPP, db, failure="transient", clock=clock).send("+919876543210", msg)
    assert not e.value.permanent
    with pytest.raises(ChannelError) as e:
        MockChannel(WHATSAPP, db, failure="permanent", clock=clock).send("+919876543210", msg)
    assert e.value.permanent


def test_mask_email():
    assert mask_email("priya.raman@example.com") == "p***@example.com"


def test_twilio_whatsapp_adapter():
    class Messages:
        def __init__(self):
            self.calls = []

        def create(self, **kw):
            self.calls.append(kw)
            return type("M", (), {"sid": "SM1"})()

    client = type("C", (), {})()
    client.messages = Messages()
    ref = TwilioWhatsAppChannel("AC", "tok", "+14155238886", client=client).send("+919876543210", _message())
    call = client.messages.calls[0]
    assert ref == "SM1" and call["to"] == "whatsapp:+919876543210"
    assert call["from_"] == "whatsapp:+14155238886" and "[MATCH FOUND]" in call["body"]


class FakeResponse:
    def __init__(self, status, payload=None):
        self.status_code, self._payload = status, payload or {}

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, response=None, exc=None):
        self.response, self.exc, self.calls = response, exc, []

    def post(self, url, **kw):
        self.calls.append((url, kw))
        if self.exc:
            raise self.exc
        return self.response


def test_meta_whatsapp_adapter_success():
    http = FakeSession(FakeResponse(200, {"messages": [{"id": "wamid.1"}]}))
    ref = MetaWhatsAppChannel("tok", "12345", session=http).send("+919876543210", _message())
    url, kw = http.calls[0]
    assert ref == "wamid.1" and url.endswith("/12345/messages")
    assert kw["json"]["to"] == "919876543210" and kw["headers"]["Authorization"] == "Bearer tok"


@pytest.mark.parametrize("status,permanent", [(400, True), (404, True), (429, False), (500, False), (503, False)])
def test_meta_whatsapp_adapter_error_classification(status, permanent):
    with pytest.raises(ChannelError) as e:
        MetaWhatsAppChannel("tok", "1", session=FakeSession(FakeResponse(status))).send("+91987", _message())
    assert e.value.permanent is permanent


def test_meta_whatsapp_network_error_is_transient():
    with pytest.raises(ChannelError) as e:
        MetaWhatsAppChannel("tok", "1", session=FakeSession(exc=requests.ConnectionError())).send("+9", _message())
    assert not e.value.permanent


class FakeSMTP:
    instances = []
    refuse = False

    def __init__(self, host, port, timeout):
        self.host, self.port, self.sent, self.tls, self.auth = host, port, [], False, None
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        self.tls = True

    def login(self, user, password):
        self.auth = (user, password)

    def send_message(self, mail):
        if FakeSMTP.refuse:
            raise smtplib.SMTPRecipientsRefused({})
        self.sent.append(mail)


def test_smtp_adapter_sends_utf8_text_and_escaped_html():
    FakeSMTP.instances, FakeSMTP.refuse = [], False
    channel = SmtpEmailChannel("smtp.example", 587, "noreply@tnstc.example", "user", "pw",
                               smtp_factory=FakeSMTP)
    channel.send("priya@example.com", _message(public_description=f"<b>{TAMIL}</b>",
                                                passenger_name="கார்த்திக்"))
    smtp = FakeSMTP.instances[0]
    assert smtp.tls and smtp.auth == ("user", "pw")
    raw = smtp.sent[0].as_bytes()
    parsed = message_from_bytes(raw, policy=policy.default)
    text = parsed.get_body(("plain",)).get_content()
    html_part = parsed.get_body(("html",)).get_content()
    assert TAMIL in text and "கார்த்திக்" in text
    assert "<b>" not in html_part and TAMIL in html_part
    assert parsed["To"] == "priya@example.com"


def test_smtp_refused_recipient_is_permanent():
    FakeSMTP.refuse = True
    with pytest.raises(ChannelError) as e:
        SmtpEmailChannel("h", 25, "f@x.com", smtp_factory=FakeSMTP, starttls=False).send("bad@x.com", _message())
    FakeSMTP.refuse = False
    assert e.value.permanent


def test_f2_test_config_disables_background_worker():
    assert make_f2_config().worker_enabled is False
