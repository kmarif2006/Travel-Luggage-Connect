"""API / integration tests for /auth/* and the passenger pages, one per rule and edge case."""
import threading
from datetime import timedelta

import pytest

from conftest import (ARUN, CANCELLED, KARTHIK, MULTI_RAVI, MULTI_SHARED, OLD_JOURNEY, PRIYA,
                      build_app, make_config, wrong_otp)
from passenger_auth.sessions import COOKIE_NAME
from passenger_auth.timeutil import from_db


def _err(resp):
    return resp.get_json()["error"]


def _otp_count(sms):
    return sum(1 for m in sms.sent if m["purpose"] == "otp")


# ── End-to-end happy path ────────────────────────────────────────────────────
def test_end_to_end_happy_path(client, flow, sms):
    page = client.get("/passenger/login")
    assert page.status_code == 200 and b"Passenger Login" in page.data

    resp = flow.request(ARUN["pnr"], ARUN["phone"])
    assert resp.status_code == 202
    body = resp.get_json()
    assert body["expiresInSeconds"] == 300 and body["resendAfterSeconds"] == 30
    assert body["maskedPhone"] == "+91 ******3210"
    assert sms.sent[-1]["to"] == "+919876543210"

    otp = sms.last_otp()
    resp = flow.verify(ARUN["pnr"], ARUN["phone"], otp)
    assert resp.status_code == 200
    data = resp.get_json()
    assert otp not in resp.get_data(as_text=True)          # the raw OTP is never returned
    assert data["passenger"] == {"pnr": "T26100101", "passengerName": "Arun Kumar",
                                 "maskedPhone": "+91 ******3210"}
    assert data["redirect"] == "/passenger"
    assert data["session"]["expiresInSeconds"] == 1800 and data["session"]["expiresAt"].endswith("Z")

    me = client.get("/auth/me")
    assert me.status_code == 200 and me.get_json()["passenger"]["pnr"] == "T26100101"
    assert me.get_json()["booking"]["status"] == "confirmed"

    home = client.get("/passenger")
    assert home.status_code == 200 and b"Arun Kumar" in home.data and b"IST" in home.data

    assert client.post("/auth/logout").status_code == 200
    assert client.get("/auth/me").status_code == 401


def test_session_cookie_flags(flow):
    resp = flow.login(ARUN)
    cookie = next(h for h in resp.headers.getlist("Set-Cookie") if h.startswith(COOKIE_NAME + "="))
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=Lax" in cookie
    assert "Max-Age=1800" in cookie


def test_responses_are_not_cacheable(flow):
    assert flow.request(ARUN["pnr"], ARUN["phone"]).headers["Cache-Control"] == "no-store"


# ── Request: validation and non-disclosure ───────────────────────────────────
@pytest.mark.parametrize("pnr", ["12345678", "T12", "T2610-0101", "TT26100101X", "T 26100101"])
def test_invalid_pnr_format_is_400(flow, sms, pnr):
    resp = flow.request(pnr, ARUN["phone"])
    assert resp.status_code == 400 and _err(resp)["fields"] == {"pnr": "invalid_format"}
    assert not sms.sent


def test_invalid_phone_format_is_400(flow):
    resp = flow.request(ARUN["pnr"], "12345")
    assert resp.status_code == 400 and _err(resp)["fields"] == {"phone": "invalid_format"}


def test_unknown_pnr_gets_generic_response_and_no_sms(flow, sms):
    known = flow.request(ARUN["pnr"], ARUN["phone"])
    unknown = flow.request("T99999999", ARUN["phone"])
    assert unknown.status_code == known.status_code == 202
    assert unknown.get_json() == known.get_json()          # byte-identical modulo nothing
    assert _otp_count(sms) == 1                             # only the real booking got an OTP


def test_phone_mismatch_gets_generic_response_and_no_sms(flow, sms):
    resp = flow.request(ARUN["pnr"], "9000000099")
    assert resp.status_code == 202
    assert resp.get_json()["message"].startswith("If the PNR and mobile number match")
    assert not sms.sent                                     # never sent to the typed number
    assert flow.verify(ARUN["pnr"], "9000000099", "123456").status_code == 401


def test_otp_only_goes_to_registered_number(flow, sms):
    flow.request(ARUN["pnr"], "+91 98765-43210")
    assert [m["to"] for m in sms.sent] == ["+919876543210"]


def test_cancelled_booking_gets_no_otp(flow, sms):
    resp = flow.request(CANCELLED["pnr"], CANCELLED["phone"])
    assert resp.status_code == 202 and not sms.sent
    assert flow.verify(CANCELLED["pnr"], CANCELLED["phone"], "123456").status_code == 401


def test_past_journey_inside_window_is_allowed(flow):
    flow.login(ARUN)                                        # travelled 2 days ago


def test_past_journey_outside_window_gets_no_otp(flow, sms):
    resp = flow.request(OLD_JOURNEY["pnr"], OLD_JOURNEY["phone"])
    assert resp.status_code == 202 and not sms.sent


# ── Payload validation / injection ───────────────────────────────────────────
@pytest.mark.parametrize("kwargs,status", [
    ({"data": "not json", "content_type": "application/json"}, 400),
    ({"data": "{\"pnr\": ", "content_type": "application/json"}, 400),
    ({"data": "pnr=T26100101&phone=9876543210", "content_type": "application/x-www-form-urlencoded"}, 400),
    ({"json": ["T26100101", "9876543210"]}, 400),
    ({"json": "T26100101"}, 400),
    ({"json": {}}, 400),
    ({"json": {"pnr": "T26100101"}}, 400),
    ({"json": {"pnr": "T26100101", "phone": ""}}, 400),
    ({"json": {"pnr": "T26100101", "phone": "9876543210", "admin": True}}, 400),
    ({"json": {"pnr": "T" * 40, "phone": "9876543210"}}, 400),
    ({"json": {"pnr": "T26100101", "phone": "9" * 5000}}, 413),
])
def test_malformed_missing_oversized_payloads(client, sms, kwargs, status):
    resp = client.post("/auth/otp/request", **kwargs)
    assert resp.status_code == status
    assert resp.get_json()["ok"] is False and "code" in _err(resp)
    assert not sms.sent


@pytest.mark.parametrize("payload", [
    {"pnr": {"$ne": None}, "phone": "9876543210"},
    {"pnr": "T26100101", "phone": {"$gt": ""}},
    {"pnr": ["T26100101"], "phone": "9876543210"},
    {"pnr": "T26100101", "phone": 9876543210},
    {"pnr": None, "phone": "9876543210"},
    {"pnr": True, "phone": "9876543210"},
])
def test_non_string_injection_is_rejected(client, sms, payload):
    resp = client.post("/auth/otp/request", json=payload)
    assert resp.status_code == 400 and _err(resp)["code"] == "invalid_request"
    assert set(_err(resp)["fields"].values()) == {"must_be_string"}
    assert not sms.sent


@pytest.mark.parametrize("payload", [
    {"pnr": "' OR '1'='1", "phone": "9876543210"},
    {"pnr": "T26100101'; DROP TABLE bookings;--", "phone": "9876543210"},
    {"pnr": "T26100101", "phone": "9876543210' OR 1=1"},
    {"pnr": "{\"$ne\": null}", "phone": "9876543210"},
])
def test_sql_and_operator_strings_are_rejected(client, sms, payload):
    assert client.post("/auth/otp/request", json=payload).status_code == 400
    assert not sms.sent


def test_injection_in_verify_otp_field(flow, client):
    flow.request(ARUN["pnr"], ARUN["phone"])
    for otp in ({"$ne": None}, ["123456"], 123456, "12345' OR '1'='1"):
        resp = client.post("/auth/otp/verify", json={**ARUN, "otp": otp})
        assert resp.status_code == 400


@pytest.mark.parametrize("payload", [
    {"pnr": "T26100101😀", "phone": "9876543210"},
    {"pnr": "Ｔ26100101", "phone": "9876543210"},
    {"pnr": "T26100101", "phone": "９８７６５４３２１０"},
    {"pnr": "T26100101", "phone": "௯௮௭௬௫௪௩௨௧௦"},
    {"pnr": "த26100101", "phone": "9876543210"},
])
def test_unicode_and_emoji_in_fields_are_rejected(client, sms, payload):
    assert client.post("/auth/otp/request", json=payload).status_code == 400
    assert not sms.sent


def test_unicode_emoji_otp_rejected(flow):
    flow.request(ARUN["pnr"], ARUN["phone"])
    assert flow.verify(ARUN["pnr"], ARUN["phone"], "12345😀").status_code == 400
    assert flow.verify(ARUN["pnr"], ARUN["phone"], "١٢٣٤٥٦").status_code == 400


def test_unicode_passenger_name_round_trips(flow, client):
    flow.login(KARTHIK)
    assert client.get("/auth/me").get_json()["passenger"]["passengerName"] == "கார்த்திக் செல்வம்"
    assert "கார்த்திக் செல்வம்" in client.get("/passenger").get_data(as_text=True)


# ── Verify: expiry, attempts, lock ───────────────────────────────────────────
def test_expired_otp(flow, sms, clock):
    flow.request(ARUN["pnr"], ARUN["phone"])
    otp = sms.last_otp()
    clock.advance(301)
    resp = flow.verify(ARUN["pnr"], ARUN["phone"], otp)
    assert resp.status_code == 401 and _err(resp)["code"] == "otp_expired"


def test_clock_skew_near_otp_expiry(flow, sms, clock):
    # The server clock is authoritative: one second before expiresAt still works.
    flow.request(ARUN["pnr"], ARUN["phone"])
    clock.advance(299)
    assert flow.verify(ARUN["pnr"], ARUN["phone"], sms.last_otp()).status_code == 200


def test_otp_exactly_at_expiry_is_rejected(flow, sms, clock):
    flow.request(ARUN["pnr"], ARUN["phone"])
    clock.advance(300)
    assert _err(flow.verify(ARUN["pnr"], ARUN["phone"], sms.last_otp()))["code"] == "otp_expired"


def test_wrong_otp_three_times_then_lock(flow, sms, db):
    flow.request(ARUN["pnr"], ARUN["phone"])
    bad = wrong_otp(sms.last_otp())
    r1 = flow.verify(ARUN["pnr"], ARUN["phone"], bad)
    r2 = flow.verify(ARUN["pnr"], ARUN["phone"], bad)
    assert (r1.status_code, _err(r1)["attemptsRemaining"]) == (401, 2)
    assert (r2.status_code, _err(r2)["attemptsRemaining"]) == (401, 1)
    r3 = flow.verify(ARUN["pnr"], ARUN["phone"], bad)
    assert r3.status_code == 423 and _err(r3)["code"] == "otp_locked"
    assert _err(r3)["retryAfterSeconds"] == 900 and r3.headers["Retry-After"] == "900"
    assert _err(r3)["lockedUntil"].endswith("Z")
    assert db.otp_sessions.find_one({"pnr": ARUN["pnr"]})["status"] == "locked"


def test_correct_otp_while_locked_is_rejected(flow, sms):
    flow.request(ARUN["pnr"], ARUN["phone"])
    good = sms.last_otp()
    for _ in range(3):
        flow.verify(ARUN["pnr"], ARUN["phone"], wrong_otp(good))
    resp = flow.verify(ARUN["pnr"], ARUN["phone"], good)
    assert resp.status_code == 423


def test_new_otp_request_during_lock_is_refused(flow, sms):
    flow.request(ARUN["pnr"], ARUN["phone"])
    for _ in range(3):
        flow.verify(ARUN["pnr"], ARUN["phone"], wrong_otp(sms.last_otp()))
    before = _otp_count(sms)
    resp = flow.request(ARUN["pnr"], ARUN["phone"])
    assert resp.status_code == 423 and _otp_count(sms) == before


def test_lock_expires_after_cooldown(flow, sms, clock):
    flow.request(ARUN["pnr"], ARUN["phone"])
    for _ in range(3):
        flow.verify(ARUN["pnr"], ARUN["phone"], wrong_otp(sms.last_otp()))
    clock.advance(15 * 60 - 1)
    assert flow.request(ARUN["pnr"], ARUN["phone"]).status_code == 423
    clock.advance(1)
    flow.login(ARUN)


def test_lock_cooldown_is_configurable(db, clock, sms):
    client = build_app(db, clock, sms, make_config(otp_cooldown_minutes=2)).test_client()
    client.post("/auth/otp/request", json=ARUN)
    bad = wrong_otp(sms.last_otp())
    for _ in range(3):
        resp = client.post("/auth/otp/verify", json={**ARUN, "otp": bad})
    assert resp.status_code == 423 and _err(resp)["retryAfterSeconds"] == 120


def test_lock_is_per_booking_phone(flow, sms):
    flow.request(MULTI_SHARED["pnr"], MULTI_SHARED["phone"])
    for _ in range(3):
        flow.verify(MULTI_SHARED["pnr"], MULTI_SHARED["phone"], wrong_otp(sms.last_otp()))
    flow.login(MULTI_RAVI)                    # another passenger on the same PNR is unaffected


# ── Single use, supersession, races ──────────────────────────────────────────
def test_otp_reuse_after_success_fails(flow, sms, client):
    flow.request(ARUN["pnr"], ARUN["phone"])
    otp = sms.last_otp()
    assert flow.verify(ARUN["pnr"], ARUN["phone"], otp).status_code == 200
    client.post("/auth/logout")
    replay = flow.verify(ARUN["pnr"], ARUN["phone"], otp)
    assert replay.status_code == 401 and _err(replay)["code"] == "otp_invalid"


def test_new_otp_invalidates_old_one(flow, sms, clock, db):
    flow.request(ARUN["pnr"], ARUN["phone"])
    old = sms.last_otp()
    clock.advance(30)
    flow.request(ARUN["pnr"], ARUN["phone"])
    new = sms.last_otp()
    if old == new:
        pytest.skip("CSPRNG produced the same code twice (p = 1e-6)")
    assert flow.verify(ARUN["pnr"], ARUN["phone"], old).status_code == 401
    statuses = sorted(d["status"] for d in db.otp_sessions.find({"pnr": ARUN["pnr"]}))
    assert statuses == ["active", "superseded"]
    assert db.otp_sessions.find_one({"status": "active"})["resendCount"] == 1
    assert flow.verify(ARUN["pnr"], ARUN["phone"], new).status_code == 200


def _parallel_verify(app, otps, monkeypatch):
    """
    Fire len(otps) concurrent verify requests in the worst-case interleaving,
    deterministically: all requests issue their atomic attempt reservation at
    the same moment (barrier before), and none of them goes on to compare or
    commit until every reservation has been answered (barrier after). This is
    what parallel requests on separate app processes can do in production.
    """
    import mongomock
    real_fau = mongomock.collection.Collection.find_one_and_update
    before, after = threading.Barrier(len(otps)), threading.Barrier(len(otps))

    def fau(self, flt, update, *args, **kwargs):
        if "attempts" not in update.get("$inc", {}):
            return real_fau(self, flt, update, *args, **kwargs)
        before.wait(timeout=10)
        result = real_fau(self, flt, update, *args, **kwargs)
        after.wait(timeout=10)
        return result

    monkeypatch.setattr(mongomock.collection.Collection, "find_one_and_update", fau)
    results = [None] * len(otps)

    def worker(i, otp):
        results[i] = app.test_client().post("/auth/otp/verify", json={**ARUN, "otp": otp}).status_code

    threads = [threading.Thread(target=worker, args=(i, o)) for i, o in enumerate(otps)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def test_parallel_verify_only_one_succeeds(app, flow, sms, db, monkeypatch):
    flow.request(ARUN["pnr"], ARUN["phone"])
    results = _parallel_verify(app, [sms.last_otp()] * 8, monkeypatch)
    assert results.count(200) == 1
    assert set(results) <= {200, 401}
    assert db.passenger_sessions.count_documents({}) == 1
    assert db.otp_sessions.find_one({"pnr": ARUN["pnr"]})["status"] == "verified"


def test_parallel_wrong_guesses_never_exceed_max_attempts(app, flow, sms, db, monkeypatch):
    flow.request(ARUN["pnr"], ARUN["phone"])
    good = sms.last_otp()
    guesses = [f"{(int(good) + k) % 1_000_000:06d}" for k in range(1, 13)]
    results = _parallel_verify(app, guesses, monkeypatch)
    assert 200 not in results and results.count(423) >= 1
    doc = db.otp_sessions.find_one({"pnr": ARUN["pnr"]})
    assert doc["attempts"] == 3 and doc["status"] == "locked"


# ── Resend spam and rate limits ──────────────────────────────────────────────
def test_resend_cooldown(flow, sms, clock):
    assert flow.request(ARUN["pnr"], ARUN["phone"]).status_code == 202
    clock.advance(10)
    spam = flow.request(ARUN["pnr"], ARUN["phone"])
    assert spam.status_code == 429 and _err(spam)["code"] == "resend_cooldown"
    assert _err(spam)["retryAfterSeconds"] == 20 and spam.headers["Retry-After"] == "20"
    assert _otp_count(sms) == 1
    clock.advance(20)
    assert flow.request(ARUN["pnr"], ARUN["phone"]).status_code == 202


def test_resend_spam_capped_at_five_per_pnr_per_hour(flow, sms, clock):
    codes = []
    for _ in range(7):
        codes.append(flow.request(ARUN["pnr"], ARUN["phone"]).status_code)
        clock.advance(31)
    assert codes == [202] * 5 + [429, 429]
    assert _otp_count(sms) == 5
    clock.advance(3600)
    assert flow.request(ARUN["pnr"], ARUN["phone"]).status_code == 202


def test_per_pnr_cap_applies_to_unknown_pnr_too(flow, clock):
    codes = []
    for _ in range(6):
        codes.append(flow.request("T99999999", ARUN["phone"]).status_code)
        clock.advance(31)
    assert codes == [202] * 5 + [429]


def test_per_ip_rate_limit(db, clock, sms):
    client = build_app(db, clock, sms, make_config(otp_ip_request_limit=3)).test_client()
    pnrs = ["T26100101", "T26100102", "T26100103", "T26100105"]
    codes = [client.post("/auth/otp/request", json={"pnr": p, "phone": "9876543210"}).status_code
             for p in pnrs]
    assert codes == [202, 202, 202, 429]
    other_ip = client.post("/auth/otp/request", json={"pnr": "T26100105", "phone": "9123456780"},
                           environ_base={"REMOTE_ADDR": "10.0.0.2"})
    assert other_ip.status_code == 202


def test_per_ip_verify_rate_limit(db, clock, sms):
    client = build_app(db, clock, sms, make_config(otp_ip_verify_limit=2)).test_client()
    codes = [client.post("/auth/otp/verify", json={"pnr": f"T9999999{i}", "phone": "9876543210",
                                                   "otp": "123456"}).status_code for i in range(3)]
    assert codes == [401, 401, 429]


# ── Multi-passenger PNR / phone variants ─────────────────────────────────────
def test_multi_passenger_shared_phone(flow, client, sms):
    flow.login(MULTI_SHARED)
    assert sms.sent[-1]["to"] == "+919123456780"
    me = client.get("/auth/me").get_json()
    assert me["passenger"]["passengerName"] == "Senthil Kumar, Meena Senthil"


def test_multi_passenger_own_phone(flow, client, sms):
    flow.login(MULTI_RAVI)
    assert sms.sent[-1]["to"] == "+918098765432"
    assert client.get("/auth/me").get_json()["passenger"]["passengerName"] == "Ravi Senthil"


def test_multi_passenger_otps_are_independent(flow, sms):
    flow.request(MULTI_SHARED["pnr"], MULTI_SHARED["phone"])
    shared_otp = sms.last_otp("+919123456780")
    flow.request(MULTI_RAVI["pnr"], MULTI_RAVI["phone"])
    if shared_otp == sms.last_otp("+918098765432"):
        pytest.skip("CSPRNG produced the same code twice (p = 1e-6)")
    # One passenger's code does not unlock the other passenger's identity.
    assert flow.verify(MULTI_RAVI["pnr"], MULTI_RAVI["phone"], shared_otp).status_code == 401
    assert flow.verify(MULTI_SHARED["pnr"], MULTI_SHARED["phone"], shared_otp).status_code == 200


@pytest.mark.parametrize("request_phone,verify_phone", [
    ("+91 98765 43210", "098765-43210"),
    ("919876543210", "9876543210"),
    ("0091-98765-43210", "+91 (98765) 43210"),
])
def test_phone_format_variants(flow, sms, request_phone, verify_phone):
    assert flow.request(ARUN["pnr"], request_phone).status_code == 202
    assert flow.verify(" t26100101 ", verify_phone, sms.last_otp()).status_code == 200


def test_registered_phone_stored_in_other_format_still_matches(db, flow, sms):
    db.bookings.update_one({"pnr": PRIYA["pnr"]}, {"$set": {"registeredPhone": "094440-12345"}})
    flow.login(PRIYA)


# ── Sessions on protected routes ─────────────────────────────────────────────
def test_missing_session(client):
    resp = client.get("/auth/me")
    assert resp.status_code == 401 and _err(resp)["code"] == "session_missing"
    page = client.get("/passenger")
    assert page.status_code == 302 and "/passenger/login" in page.headers["Location"]
    assert "reason=expired" not in page.headers["Location"]


def test_expired_session(flow, client, clock):
    flow.login(ARUN)
    clock.advance(1799)
    assert client.get("/auth/me").status_code == 200       # clock skew: last second still valid
    clock.advance(1)
    resp = client.get("/auth/me")
    assert resp.status_code == 401 and _err(resp)["code"] == "session_expired"


def test_expired_session_redirects_page_to_login(flow, client, clock):
    flow.login(ARUN)
    clock.advance(1800)
    page = client.get("/passenger")
    assert page.status_code == 302
    assert "/passenger/login" in page.headers["Location"] and "reason=expired" in page.headers["Location"]
    login = client.get(page.headers["Location"])
    assert b"Your session has expired" in login.data


@pytest.mark.parametrize("mutate", [
    lambda t: t[:-2] + ("AA" if not t.endswith("AA") else "BB"),   # signature
    lambda t: "x" + t,                                              # payload
    lambda t: "garbage",
    lambda t: "A" * 3000,
])
def test_tampered_session_rejected(flow, client, mutate):
    flow.login(ARUN)
    token = client.get_cookie(COOKIE_NAME).value
    client.set_cookie(COOKIE_NAME, mutate(token))
    resp = client.get("/auth/me")
    assert resp.status_code == 401 and _err(resp)["code"] == "session_invalid"


def test_forged_session_with_other_secret_rejected(flow, client):
    from itsdangerous import URLSafeSerializer
    flow.login(ARUN)
    forged = URLSafeSerializer("attacker-secret-0123456789-abcdefghij", salt="tnstc-passenger-session") \
        .dumps({"sid": "x", "pnr": "T26100102", "ph": "+919444012345", "exp": 9999999999})
    client.set_cookie(COOKIE_NAME, forged)
    assert client.get("/auth/me").status_code == 401


def test_session_record_bound_to_pnr_and_phone(flow, client, db):
    flow.login(ARUN)
    db.passenger_sessions.update_one({}, {"$set": {"pnr": "T26100102"}})
    assert client.get("/auth/me").status_code == 401


def test_logout_then_reuse_old_session(flow, client):
    flow.login(ARUN)
    old_token = client.get_cookie(COOKIE_NAME).value
    resp = client.post("/auth/logout")
    assert resp.status_code == 200
    assert any(h.startswith(COOKIE_NAME + "=;") for h in resp.headers.getlist("Set-Cookie"))
    client.set_cookie(COOKIE_NAME, old_token)             # attacker replays the captured cookie
    replay = client.get("/auth/me")
    assert replay.status_code == 401 and _err(replay)["code"] == "session_expired"


def test_logout_without_session_is_idempotent(client):
    assert client.post("/auth/logout").status_code == 200


def test_booking_cancelled_after_login_is_403(flow, client, db):
    flow.login(ARUN)
    db.bookings.update_one({"pnr": ARUN["pnr"]}, {"$set": {"status": "cancelled"}})
    resp = client.get("/auth/me")
    assert resp.status_code == 403 and _err(resp)["code"] == "booking_not_eligible"
    db.bookings.update_one({"pnr": ARUN["pnr"]}, {"$set": {"status": "confirmed"}})
    assert client.get("/auth/me").status_code == 401        # that session was revoked


def test_session_ttl_configurable_and_server_side(db, clock, sms):
    app = build_app(db, clock, sms, make_config(session_ttl_seconds=120))
    client = app.test_client()
    client.post("/auth/otp/request", json=ARUN)
    client.post("/auth/otp/verify", json={**ARUN, "otp": sms.last_otp()})
    doc = db.passenger_sessions.find_one()
    assert from_db(doc["expiresAt"]) - from_db(doc["createdAt"]) == timedelta(seconds=120)
    assert "sid" not in doc and len(doc["sidHash"]) == 64


# ── Redirects, CSRF, pages ───────────────────────────────────────────────────
@pytest.mark.parametrize("target,expected", [
    ("/status", "/status"), ("//evil.example", "/passenger"),
    ("https://evil.example", "/passenger"), ("/\\evil.example", "/passenger"),
])
def test_post_login_redirect_is_same_site_only(flow, sms, client, target, expected):
    flow.request(ARUN["pnr"], ARUN["phone"])
    resp = client.post(f"/auth/otp/verify?next={target}", json={**ARUN, "otp": sms.last_otp()})
    assert resp.get_json()["redirect"] == expected


def test_csrf_enforced_on_auth_posts(db, clock, sms):
    client = build_app(db, clock, sms, csrf=True).test_client()
    resp = client.post("/auth/otp/request", json=ARUN)
    assert resp.status_code == 403 and _err(resp)["code"] == "csrf_failed"
    assert not sms.sent


def test_csrf_token_flow_works(db, clock, sms):
    import re
    client = build_app(db, clock, sms, csrf=True).test_client()
    html = client.get("/passenger/login").get_data(as_text=True)
    token = re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)
    resp = client.post("/auth/otp/request", json=ARUN, headers={"X-CSRFToken": token})
    assert resp.status_code == 202


def test_login_page_redirects_when_already_logged_in(flow, client):
    flow.login(ARUN)
    resp = client.get("/passenger/login")
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/passenger")


def test_nav_shows_passenger_when_logged_in(flow, client):
    assert b"Passenger Login" in client.get("/passenger/login").data
    flow.login(ARUN)
    assert b"Arun Kumar" in client.get("/passenger").data
