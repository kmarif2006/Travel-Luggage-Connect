"""Unit tests: OTP primitives, phone/PNR normalization, expiry, attempts, config, SMS, rate limiter."""
import logging
import re
from collections import Counter
from datetime import timedelta

import pytest

from conftest import TEST_SECRET, make_config, wrong_otp
from passenger_auth.bookings import PnrFormatError, find_eligible_booking, normalize_pnr
from passenger_auth.config import AuthConfig, ConfigError
from passenger_auth.errors import AuthError
from passenger_auth.otp import (derive_key, generate_otp, hash_otp, is_valid_otp_format,
                                new_salt, otp_matches)
from passenger_auth.phone import PhoneFormatError, mask_phone, normalize_phone
from passenger_auth.ratelimit import RateLimiter
from passenger_auth.service import OtpService
from passenger_auth.sms import MockSmsProvider, TwilioSmsProvider, build_sms_provider
from passenger_auth.timeutil import IST, fmt_ist, from_db, iso_utc, seconds_until, to_db, today_ist

KEY = derive_key(TEST_SECRET, "tnstc-otp-hash-v1")


# ── OTP generation / hashing / comparison ────────────────────────────────────
def test_generate_otp_is_six_ascii_digits():
    for _ in range(500):
        otp = generate_otp()
        assert re.fullmatch(r"[0-9]{6}", otp)


def test_generate_otp_uses_full_range_including_leading_zeros():
    # 3000 draws from 10^6: duplicates are rare and leading zeros must be kept.
    draws = [generate_otp() for _ in range(3000)]
    assert len(set(draws)) > 2950
    assert any(d.startswith("0") for d in draws)
    first_digits = Counter(d[0] for d in draws)
    assert len(first_digits) == 10


def test_hash_is_salted_and_keyed():
    salt_a, salt_b = new_salt(), new_salt()
    h1 = hash_otp(KEY, salt_a, "T26100101", "+919876543210", "123456")
    assert h1 != hash_otp(KEY, salt_b, "T26100101", "+919876543210", "123456")
    assert h1 != hash_otp(derive_key("another-secret-0123456789abcdefghij", "x"), salt_a,
                          "T26100101", "+919876543210", "123456")
    assert "123456" not in h1 and len(h1) == 64


def test_hash_binds_pnr_and_phone():
    salt = new_salt()
    h = hash_otp(KEY, salt, "T26100101", "+919876543210", "123456")
    assert not otp_matches(KEY, salt, "T26100102", "+919876543210", "123456", h)
    assert not otp_matches(KEY, salt, "T26100101", "+919876543211", "123456", h)


def test_compare_accepts_only_exact_code():
    salt = new_salt()
    h = hash_otp(KEY, salt, "T26100101", "+919876543210", "042517")
    assert otp_matches(KEY, salt, "T26100101", "+919876543210", "042517", h)
    assert not otp_matches(KEY, salt, "T26100101", "+919876543210", "42517", h)
    assert not otp_matches(KEY, salt, "T26100101", "+919876543210", "042518", h)


def test_compare_uses_constant_time_primitive(monkeypatch):
    import passenger_auth.otp as otp_mod
    calls = []
    real = otp_mod.hmac.compare_digest
    monkeypatch.setattr(otp_mod.hmac, "compare_digest", lambda a, b: calls.append(1) or real(a, b))
    salt = new_salt()
    otp_matches(KEY, salt, "T26100101", "+919876543210", "111111",
                hash_otp(KEY, salt, "T26100101", "+919876543210", "111111"))
    assert calls == [1]


@pytest.mark.parametrize("value,ok", [
    ("123456", True), ("000000", True), ("12345", False), ("1234567", False),
    ("12a456", False), ("１２３４５６", False), ("١٢٣٤٥٦", False), (123456, False), (None, False),
])
def test_otp_format(value, ok):
    assert is_valid_otp_format(value) is ok


# ── Phone normalization ──────────────────────────────────────────────────────
@pytest.mark.parametrize("raw", [
    "9876543210", "+919876543210", "+91 98765 43210", "+91-98765-43210", "919876543210",
    "09876543210", "098765-43210", "0091 9876543210", "(+91) 98765 43210", " 98765.43210 ",
    "+91 0 98765 43210",
])
def test_phone_variants_normalize_to_e164(raw):
    assert normalize_phone(raw) == "+919876543210"


@pytest.mark.parametrize("raw", [
    "", "12345", "5876543210", "+1 9876543210", "98765432101", "98765 4321O",
    "９８７６５４３２１０", "௯௮௭௬௫௪௩௨௧௦", "9876543210😀", None, 9876543210, ["9876543210"],
])
def test_phone_rejects_invalid(raw):
    with pytest.raises(PhoneFormatError):
        normalize_phone(raw)


def test_mask_phone():
    assert mask_phone("+919876543210") == "+91 ******3210"


# ── PNR normalization ────────────────────────────────────────────────────────
def test_pnr_normalizes_case_and_whitespace():
    assert normalize_pnr("  t26100101 ") == "T26100101"


@pytest.mark.parametrize("raw", ["", "26100101", "T123", "TOOLONG123456789", "T2610-0101",
                                 "T26100101😀", "Ｔ26100101", "' OR 1=1 --", {"$ne": None}, None])
def test_pnr_rejects_invalid(raw):
    with pytest.raises(PnrFormatError):
        normalize_pnr(raw)


# ── Time helpers ─────────────────────────────────────────────────────────────
def test_db_roundtrip_is_utc_millisecond_precision(clock):
    now = clock().replace(microsecond=123456)
    stored = to_db(now)
    assert stored.tzinfo is None and stored.microsecond == 123000
    assert from_db(stored) == now.replace(microsecond=123000)


def test_iso_and_ist_formatting(clock):
    dt = clock().replace(hour=20, minute=0, second=0)
    assert iso_utc(to_db(dt)).endswith("Z")
    assert fmt_ist(to_db(dt)).endswith("IST")
    assert today_ist(dt) == dt.astimezone(IST).date()   # 20:00 UTC is already "tomorrow" in IST


def test_seconds_until_rounds_up_and_floors_at_zero(clock):
    now = clock()
    assert seconds_until(now + timedelta(seconds=1.2), now) == 2
    assert seconds_until(now - timedelta(seconds=5), now) == 0


# ── Service-level expiry and attempt counting ────────────────────────────────
def _service(db, clock, sms, **cfg):
    config = make_config(**cfg)
    return OtpService(db, config, sms, RateLimiter(db, clock), clock)


def test_otp_expiry_boundary(db, clock, sms):
    svc = _service(db, clock, sms)
    svc.request_otp("T26100101", "9876543210", "1.1.1.1")
    otp = sms.last_otp()
    clock.advance(299)
    assert svc.verify_otp("T26100101", "9876543210", otp, "1.1.1.1").pnr == "T26100101"

    clock.advance(31)
    svc.request_otp("T26100101", "9876543210", "1.1.1.1")
    otp = sms.last_otp()
    clock.advance(300)                       # exactly at expiresAt → expired (exclusive bound)
    with pytest.raises(AuthError) as e:
        svc.verify_otp("T26100101", "9876543210", otp, "1.1.1.1")
    assert e.value.code == "otp_expired"


def test_attempts_are_counted_and_lock_at_max(db, clock, sms):
    svc = _service(db, clock, sms)
    svc.request_otp("T26100101", "9876543210", "1.1.1.1")
    bad = wrong_otp(sms.last_otp())
    remaining = []
    for _ in range(2):
        with pytest.raises(AuthError) as e:
            svc.verify_otp("T26100101", "9876543210", bad, "1.1.1.1")
        remaining.append(e.value.extra["attemptsRemaining"])
    assert remaining == [2, 1]
    with pytest.raises(AuthError) as e:
        svc.verify_otp("T26100101", "9876543210", bad, "1.1.1.1")
    assert e.value.status == 423
    doc = db.otp_sessions.find_one({"pnr": "T26100101"})
    assert doc["attempts"] == 3 and doc["status"] == "locked"
    assert from_db(doc["cooldownUntil"]) == clock() + timedelta(minutes=15)


def test_otp_record_never_stores_raw_code(db, clock, sms):
    svc = _service(db, clock, sms)
    svc.request_otp("T26100101", "9876543210", "1.1.1.1")
    otp = sms.last_otp()
    doc = db.otp_sessions.find_one({"pnr": "T26100101"})
    assert otp not in str(doc)
    expected = {"pnr", "phone", "otpHash", "createdAt", "expiresAt", "attempts", "status",
                "cooldownUntil", "consumedAt", "resendCount", "ip"}
    assert expected <= set(doc)
    assert from_db(doc["expiresAt"]) - from_db(doc["createdAt"]) == timedelta(seconds=300)


def test_raw_otp_never_logged(db, clock, sms, caplog):
    caplog.set_level(logging.DEBUG)
    svc = _service(db, clock, sms)
    svc.request_otp("T26100101", "9876543210", "1.1.1.1")
    otp = sms.last_otp()
    svc.verify_otp("T26100101", "9876543210", otp, "1.1.1.1")
    assert otp not in caplog.text
    assert "9876543210" not in caplog.text          # phones are masked in logs


# ── Booking policy ───────────────────────────────────────────────────────────
def test_booking_policy(db, clock):
    today = today_ist(clock())
    assert find_eligible_booking(db, "T26100101", "+919876543210", today, 90)
    assert find_eligible_booking(db, "T26100104", "+919003312345", today, 90) is None   # cancelled
    assert find_eligible_booking(db, "T26100106", "+917010203040", today, 90) is None   # 120 days ago
    assert find_eligible_booking(db, "T26100106", "+917010203040", today, 150)          # window is config


# ── Config ───────────────────────────────────────────────────────────────────
def test_config_defaults_match_spec():
    cfg = AuthConfig.from_env({"SIGNING_SECRET": TEST_SECRET})
    assert (cfg.otp_ttl_seconds, cfg.otp_max_attempts, cfg.otp_cooldown_minutes,
            cfg.otp_resend_seconds, cfg.otp_max_requests_per_pnr_per_hour) == (300, 3, 15, 30, 5)
    assert cfg.sms_provider == "mock" and cfg.sms_mock_expose_otp is False
    assert cfg.cookie_secure is True and cfg.cookie_samesite == "Lax"


def test_config_reads_env_overrides():
    cfg = AuthConfig.from_env({"SIGNING_SECRET": TEST_SECRET, "OTP_COOLDOWN_MINUTES": "5",
                               "SESSION_TTL": "600", "SMS_MOCK_EXPOSE_OTP": "1"})
    assert cfg.otp_cooldown_seconds == 300 and cfg.session_ttl_seconds == 600
    assert cfg.sms_mock_expose_otp is True


@pytest.mark.parametrize("env", [
    {"SIGNING_SECRET": "short"},
    {"SIGNING_SECRET": TEST_SECRET, "OTP_MAX_ATTEMPTS": "0"},
    {"SIGNING_SECRET": TEST_SECRET, "OTP_TTL_SECONDS": "abc"},
    {"SIGNING_SECRET": TEST_SECRET, "SMS_PROVIDER": "carrier-pigeon"},
    {"SIGNING_SECRET": TEST_SECRET, "PASSENGER_COOKIE_SAMESITE": "None", "PASSENGER_COOKIE_SECURE": "0"},
    {"SIGNING_SECRET": TEST_SECRET, "SMS_MOCK_EXPOSE_OTP": "maybe"},
])
def test_config_rejects_bad_values(env):
    with pytest.raises(ConfigError):
        AuthConfig.from_env(env)


def test_missing_secret_falls_back_to_random_not_constant():
    a, b = AuthConfig.from_env({}), AuthConfig.from_env({})
    assert a.signing_secret_ephemeral and a.signing_secret != b.signing_secret


# ── SMS providers ────────────────────────────────────────────────────────────
def test_mock_provider_redacts_otp_by_default(db, clock):
    MockSmsProvider(db, clock=clock).send_otp("+919876543210", "482913", 300)
    entry = db.sms_log.find_one()
    assert "482913" not in entry["body"] and "******" in entry["body"]
    assert entry["to"] == "+91 ******3210"


def test_mock_provider_exposes_otp_only_with_dev_flag(db, clock):
    MockSmsProvider(db, expose_otp=True, clock=clock).send_otp("+919876543210", "482913", 300)
    assert "482913" in db.sms_log.find_one()["body"]


def test_provider_selected_by_env(db):
    assert build_sms_provider(make_config(), db, {}).name == "mock"
    with pytest.raises(ConfigError):
        build_sms_provider(make_config(sms_provider="twilio"), db, {})


def test_twilio_adapter_sends_sms_without_logging_body(caplog):
    class FakeMessages:
        def __init__(self):
            self.calls = []

        def create(self, **kw):
            self.calls.append(kw)
            return type("Msg", (), {"sid": "SM123"})()

    fake = type("FakeClient", (), {})()
    fake.messages = FakeMessages()
    caplog.set_level(logging.INFO)
    TwilioSmsProvider("AC1", "tok", "+15550001111", client=fake, async_send=False) \
        .send_otp("+919876543210", "771100", 300)
    call = fake.messages.calls[0]
    assert call["to"] == "+919876543210" and call["from_"] == "+15550001111"
    assert "771100" in call["body"]
    assert "771100" not in caplog.text


# ── Rate limiter primitives ──────────────────────────────────────────────────
def test_fixed_window_counter(db, clock):
    rl = RateLimiter(db, clock)
    results = [rl.hit_window("t", "ip", 3, 60).allowed for _ in range(4)]
    assert results == [True, True, True, False]
    clock.advance(61)
    assert rl.hit_window("t", "ip", 3, 60).allowed


def test_cooldown_gate(db, clock):
    rl = RateLimiter(db, clock)
    assert rl.acquire_cooldown("c", "k", 30).allowed
    blocked = rl.acquire_cooldown("c", "k", 30)
    assert not blocked.allowed and blocked.retry_after == 30
    clock.advance(30)
    assert rl.acquire_cooldown("c", "k", 30).allowed


# ── Seed data and record cleanup ─────────────────────────────────────────────
def test_seed_is_idempotent_and_covers_policy_cases(db, clock):
    from seed_bookings import seed_bookings
    seed_bookings(db, now=clock())
    assert db.bookings.count_documents({}) == 8
    assert db.bookings.count_documents({"pnr": "T26100105"}) == 3
    assert db.bookings.count_documents({"status": "cancelled"}) == 1
    assert len(db.bookings.distinct("pnr")) == 6
    assert all(d["registeredPhone"].startswith("+91") for d in db.bookings.find())


def test_ttl_cleanup_indexes_exist(app, db):
    for name in ("otp_sessions", "passenger_sessions", "rate_limits", "rate_limit_events", "sms_log"):
        ttl = [ix for ix in db[name].index_information().values() if "expireAfterSeconds" in ix]
        assert ttl and ttl[0]["key"] == [("purgeAt", 1)] and ttl[0]["expireAfterSeconds"] == 0, name
    unique = db.bookings.index_information()
    assert any(ix.get("unique") and ix["key"] == [("pnr", 1), ("passengerSeq", 1)] for ix in unique.values())
