"""Integration tests for Feature 2 endpoints, routing, delivery and every listed edge case."""
import threading
from datetime import timedelta

import pytest
from bson import ObjectId

from conftest import (ARUN, DEPOT_A, DEPOT_B, KARTHIK, MULTI_RAVI, MULTI_SHARED, PRIYA, F2, make_f2_config,
                      score_of)
from match_notify.channels import EMAIL, WHATSAPP
from passenger_auth.timeutil import from_db

TAMIL_DESC = "கருப்பு பை"


def _err(resp):
    return resp.get_json()["error"]


def _external(f2, **q):
    return f2.notifications(channel={"$in": [WHATSAPP, EMAIL]}, **q)


# ── Confidence routing at the thresholds ─────────────────────────────────────
@pytest.mark.parametrize("final,status,notifies", [
    (0.85, "auto_approved", True), (0.8499, "pending_staff_review", False),
    (0.60, "pending_staff_review", False), (0.5999, "rejected", False), (0.95, "auto_approved", True),
])
def test_confidence_exactly_at_each_threshold(f2, final, status, notifies):
    match, is_new = f2.match(f2.lost(ARUN), f2.found(), final)
    assert is_new and match["status"] == status
    assert bool(f2.notifications()) is notifies
    if status == "rejected":
        assert match["rejectedBy"] == "system"


def test_thresholds_are_configurable(db, clock, sms):
    f2 = F2(db, clock, sms, config=make_f2_config(auto_notify_min=0.95, review_min=0.40))
    match, _ = f2.match(f2.lost(ARUN), f2.found(), 0.90)
    assert match["status"] == "pending_staff_review"


# ── Auto-notify happy path, WhatsApp first ──────────────────────────────────
def test_auto_match_notifies_on_whatsapp_and_starts_hold(f2, clock):
    lost, found = f2.lost(ARUN), f2.found()
    match, _ = f2.match(lost, found, 0.92)
    in_app = f2.notifications(channel="in_app")
    assert len(in_app) == 1 and in_app[0]["status"] == "delivered"
    assert f2.notifications(channel=WHATSAPP)[0]["status"] == "pending"
    assert not f2.channels[WHATSAPP].sent                    # matcher never sends inline
    assert f2.run_jobs() == 1
    sent = f2.channels[WHATSAPP].sent
    assert len(sent) == 1 and sent[0]["to"] == "+919876543210"
    assert not f2.notifications(channel=EMAIL)               # email is only a fallback
    m = f2.db.matches.find_one({"_id": match["_id"]})
    assert m["status"] == "notified" and m["deliveryStatus"] == "delivered" and m["deliveredVia"] == WHATSAPP
    assert from_db(m["holdExpiresAt"]) - from_db(m["notifiedAt"]) == timedelta(days=7)
    assert f2.db.lost_reports.find_one({"_id": lost["_id"]})["status"] == "matched"


def test_message_is_privacy_safe_and_links_to_login_protected_report(f2):
    f2.match(f2.lost(ARUN), f2.found(notes="Dell laptop SN 7X2K9Q, Aadhaar 1234 5678 9012",
                                     public="Black backpack", image_path="uploads/secret.jpg"), 0.9)
    f2.run_jobs()
    msg = f2.channels[WHATSAPP].sent[0]
    for secret in ("7X2K9Q", "Aadhaar", "1234", "Dell", "uploads/secret.jpg", ".jpg"):
        assert secret not in msg["text"] and secret not in msg["html"]
    assert "https://lostfound.tnstc.example/passenger/reports/TRK-AAAA0001" in msg["text"]
    assert 'Item Description (Depot): "Black backpack"' in msg["text"]
    assert "Date Found: 21-Oct-2026 IST" in msg["text"]
    assert "Found At: Chennai CMBT Depot" in msg["text"]


def test_deep_link_requires_feature1_login(f2):
    f2.match(f2.lost(ARUN), f2.found(), 0.9)
    resp = f2.client.get("/passenger/reports/TRK-AAAA0001")
    assert resp.status_code == 302 and "/passenger/login?next=%2Fpassenger%2Freports%2FTRK-AAAA0001" \
        in resp.headers["Location"]


# ── Review band: nothing before approval ─────────────────────────────────────
def test_review_band_never_notifies_or_shows_before_approval(f2):
    lost = f2.lost(ARUN)
    match, _ = f2.match(lost, f2.found(), 0.70)
    assert f2.run_jobs() == 0
    assert not f2.notifications() and not f2.channels[WHATSAPP].sent
    assert f2.db.lost_reports.find_one({"_id": lost["_id"]})["status"] == "pending"   # status page shows nothing
    client = f2.login_passenger(ARUN)
    assert client.get(f"/matches/{match['_id']}").status_code == 404
    assert client.get("/reports/TRK-AAAA0001/matches").get_json()["matches"] == []
    assert client.get("/notifications").get_json()["notifications"] == []


def test_staff_approve_then_notifies(f2):
    match, _ = f2.match(f2.lost(ARUN), f2.found(), 0.70)
    staff = f2.staff_client()
    queue = staff.get("/staff/matches?status=staff_review").get_json()["matches"]
    assert [m["matchId"] for m in queue] == [str(match["_id"])]
    assert queue[0]["item"]["notes"]                       # staff see private notes; passengers never do
    resp = staff.post(f"/staff/matches/{match['_id']}/approve")
    assert resp.status_code == 200 and resp.get_json()["status"] == "auto_approved"
    f2.run_jobs()
    m = f2.db.matches.find_one({"_id": match["_id"]})
    assert m["status"] == "notified" and m["approvedBy"] == "staff:9000000101"
    assert len(f2.channels[WHATSAPP].sent) == 1
    assert staff.post(f"/staff/matches/{match['_id']}/approve").status_code == 409   # double click


def test_staff_reject_sends_nothing(f2):
    match, _ = f2.match(f2.lost(ARUN), f2.found(), 0.70)
    staff = f2.staff_client()
    assert staff.post(f"/staff/matches/{match['_id']}/reject").get_json()["status"] == "rejected"
    f2.run_jobs()
    assert not f2.notifications()
    assert staff.post(f"/staff/matches/{match['_id']}/approve").status_code == 409


def test_reject_then_rerun_does_not_resurrect(f2):
    lost, found = f2.lost(ARUN), f2.found()
    match, _ = f2.match(lost, found, 0.70)
    staff = f2.staff_client()
    staff.post(f"/staff/matches/{match['_id']}/reject")
    f2.matcher.scores[(lost["request_id"], found["found_id"])] = 0.99     # even a higher score
    assert staff.post("/staff/matcher/rerun", json={}).status_code == 200
    m = f2.db.matches.find_one({"_id": match["_id"]})
    assert m["status"] == "rejected" and f2.db.matches.count_documents({}) == 1
    assert not f2.notifications()


def test_system_rejected_low_score_can_be_rerouted_by_better_evidence(f2):
    lost, found = f2.lost(ARUN), f2.found()
    match, _ = f2.match(lost, found, 0.40)
    assert match["status"] == "rejected"
    rerouted, is_new = f2.match(lost, found, 0.75)
    assert not is_new and rerouted["status"] == "pending_staff_review"


# ── Dedupe / re-runs / concurrency ───────────────────────────────────────────
def test_duplicate_triggers_are_deduplicated(f2):
    lost, found = f2.lost(ARUN), f2.found()
    results = [f2.match(lost, found, 0.92) for _ in range(5)]
    assert [is_new for _, is_new in results] == [True, False, False, False, False]
    assert f2.db.matches.count_documents({}) == 1
    assert len(f2.notifications()) == 2                     # in_app + whatsapp, once each
    f2.run_jobs()
    f2.run_jobs()
    assert len(f2.channels[WHATSAPP].sent) == 1


def test_matcher_rerun_on_same_pair_is_idempotent(f2):
    lost, found = f2.lost(ARUN), f2.found()
    f2.matcher.scores[(lost["request_id"], found["found_id"])] = 0.9
    staff = f2.staff_client()
    for _ in range(3):
        assert staff.post("/staff/matcher/rerun", json={"itemId": "item-1"}).get_json()["itemsRescored"] == 1
        f2.run_jobs()
    assert f2.db.matches.count_documents({}) == 1 and len(f2.channels[WHATSAPP].sent) == 1


def test_parallel_duplicate_triggers_create_one_match(f2):
    lost, found = f2.lost(ARUN), f2.found()
    barrier = threading.Barrier(8)

    def trigger():
        barrier.wait(timeout=10)
        f2.match(lost, found, 0.92)

    threads = [threading.Thread(target=trigger) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert f2.db.matches.count_documents({}) == 1
    assert len(f2.notifications(channel=WHATSAPP)) == 1


def test_job_running_twice_concurrently_sends_once(f2, monkeypatch):
    for i in range(5):
        f2.match(f2.lost(ARUN, tracking=f"TRK-AAAA000{i}"), f2.found(found_id=f"item-{i}"), 0.9)
    import mongomock
    real = mongomock.collection.Collection.find_one_and_update
    gate = threading.Barrier(2)

    def claim(self, flt, update, *a, **kw):
        if "leaseOwner" in update.get("$set", {}):
            try:
                gate.wait(timeout=0.5)        # both workers race for the same job
            except threading.BrokenBarrierError:
                pass
        return real(self, flt, update, *a, **kw)

    monkeypatch.setattr(mongomock.collection.Collection, "find_one_and_update", claim)
    counts = []
    workers = [threading.Thread(target=lambda w=w: counts.append(f2.svc.process_due_notifications(w)))
               for w in ("w1", "w2")]
    [t.start() for t in workers]
    [t.join() for t in workers]
    assert sum(counts) == 5 and len(f2.channels[WHATSAPP].sent) == 5
    assert sorted(m["to"] for m in f2.channels[WHATSAPP].sent) == ["+919876543210"] * 5


def test_expired_lease_is_reclaimed_but_live_lease_is_not(f2, clock):
    f2.match(f2.lost(ARUN), f2.found(), 0.9)
    f2.db.notifications.update_one({"channel": WHATSAPP}, {"$set": {
        "leaseUntil": f2.db.notifications.find_one({"channel": WHATSAPP})["nextAttemptAt"] + timedelta(seconds=120),
        "leaseOwner": "crashed-worker"}})
    assert f2.run_jobs() == 0                              # someone else holds the lease
    clock.advance(121)
    assert f2.run_jobs() == 1 and len(f2.channels[WHATSAPP].sent) == 1


# ── Multiple reports, item/report state ──────────────────────────────────────
def test_same_item_matched_to_multiple_reports_notifies_each_without_leaking(f2):
    found = f2.found()
    a = f2.lost(MULTI_SHARED, tracking="TRK-SHARED01")
    b = f2.lost(PRIYA, tracking="TRK-PRIYA001")
    f2.match(a, found, 0.9)
    f2.match(b, found, 0.88)
    f2.run_jobs()
    assert {m["to"] for m in f2.channels[WHATSAPP].sent} == {"+919123456780"}
    assert len(f2.channels[EMAIL].sent) == 1                 # Priya is email-only
    for msg in f2.channels[WHATSAPP].sent + f2.channels[EMAIL].sent:
        assert "also" not in msg["text"].lower()
    client = f2.login_passenger(PRIYA)
    body = client.get("/reports/TRK-PRIYA001/matches").get_data(as_text=True)
    assert "TRK-SHARED01" not in body and "Senthil" not in body and "also" not in body.lower()


def test_item_already_claimed_or_handed_over_is_not_notified(f2):
    for i, status in enumerate(("claimed", "handed_over", "resolved")):
        lost = f2.lost(ARUN, tracking=f"TRK-HAND000{i}")
        match, _ = f2.match(lost, f2.found(found_id=f"item-{status}", status=status), 0.95)
        assert match is None
    assert f2.db.matches.count_documents({}) == 0 and not f2.notifications()


def test_item_handed_over_after_queueing_cancels_pending_send(f2):
    f2.match(f2.lost(ARUN), f2.found(), 0.95)
    f2.db.found_reports.update_one({"found_id": "item-1"}, {"$set": {"status": "handed_over"}})
    f2.run_jobs()
    assert not f2.channels[WHATSAPP].sent
    assert f2.notifications(channel=WHATSAPP)[0]["status"] == "cancelled"


def test_report_cancelled_or_resolved_is_not_matched(f2):
    found = f2.found()
    for tracking, status in (("TRK-CANC0001", "cancelled"), ("TRK-RESO0001", "resolved")):
        match, _ = f2.match(f2.lost(ARUN, tracking=tracking, status=status), found, 0.95)
        assert match is None
    assert not f2.notifications()


def test_passenger_cancels_report_ends_matches_and_pending_sends(f2):
    f2.match(f2.lost(ARUN), f2.found(), 0.95)
    client = f2.login_passenger(ARUN)
    resp = client.post("/reports/TRK-AAAA0001/cancel")
    assert resp.status_code == 200 and resp.get_json()["report"]["status"] == "cancelled"
    f2.run_jobs()
    assert not f2.channels[WHATSAPP].sent
    assert f2.db.matches.find_one()["status"] == "cancelled"
    assert client.post("/reports/TRK-AAAA0001/cancel").status_code == 409


def test_depot_resolve_ends_competing_matches(f2):
    found = f2.found()
    a, b = f2.lost(ARUN, tracking="TRK-ARUN0001"), f2.lost(PRIYA, tracking="TRK-PRIY0001")
    ma, _ = f2.match(a, found, 0.95)
    mb, _ = f2.match(b, found, 0.90)
    f2.db.matches.update_one({"_id": ma["_id"]}, {"$set": {"status": "resolved"}})
    f2.svc.on_match_resolved(ma["_id"])
    f2.run_jobs()
    assert f2.db.matches.find_one({"_id": mb["_id"]})["status"] == "cancelled"
    assert f2.db.found_reports.find_one({"found_id": "item-1"})["status"] == "resolved"
    assert f2.channels[EMAIL].sent == [] and f2.channels[WHATSAPP].sent == []


# ── Delivery: fallback, failure, retry ───────────────────────────────────────
def test_email_only_passenger_gets_email(f2):
    f2.match(f2.lost(PRIYA), f2.found(), 0.9)
    wa = f2.notifications(channel=WHATSAPP)[0]
    assert wa["status"] == "failed" and "no WhatsApp" in wa["lastError"]
    f2.run_jobs()
    assert [m["to"] for m in f2.channels[EMAIL].sent] == ["priya.raman@example.com"]
    assert f2.db.matches.find_one()["deliveredVia"] == EMAIL


def test_passenger_with_no_whatsapp_or_email_gets_in_app_only_and_staff_sees_it(f2):
    match, _ = f2.match(f2.lost(MULTI_RAVI), f2.found(), 0.9)
    m = f2.db.matches.find_one({"_id": match["_id"]})
    assert m["status"] == "notified" and m["deliveryStatus"] == "failed"
    assert {n["channel"]: n["status"] for n in f2.notifications()} == \
        {"in_app": "delivered", WHATSAPP: "failed", EMAIL: "failed"}
    failed = f2.staff_client().get("/staff/notifications?status=failed").get_json()["notifications"]
    assert {n["channel"] for n in failed} == {WHATSAPP, EMAIL}
    assert not any(n["retryable"] for n in failed)          # no contact on file: retry cannot help
    client = f2.login_passenger(MULTI_RAVI)
    assert len(client.get("/notifications").get_json()["notifications"]) == 1


def test_whatsapp_failure_falls_back_to_email(f2):
    f2.channels[WHATSAPP].default = "permanent"
    f2.match(f2.lost(ARUN), f2.found(), 0.9)
    f2.run_jobs()
    assert f2.notifications(channel=WHATSAPP)[0]["status"] == "failed"
    assert [m["to"] for m in f2.channels[EMAIL].sent] == ["arun.kumar@example.com"]
    assert f2.db.matches.find_one()["deliveredVia"] == EMAIL


def test_provider_outage_retries_with_exponential_backoff(f2, clock):
    f2.channels[WHATSAPP].plan = ["transient", "transient", "ok"]
    f2.match(f2.lost(ARUN), f2.found(), 0.9)
    start = clock()
    assert f2.run_jobs() == 1
    job = f2.notifications(channel=WHATSAPP)[0]
    assert job["status"] == "pending" and job["attempts"] == 1
    assert from_db(job["nextAttemptAt"]) == start + timedelta(seconds=30)
    clock.advance(29)
    assert f2.run_jobs() == 0                               # not due yet
    clock.advance(1)
    f2.run_jobs()
    job = f2.notifications(channel=WHATSAPP)[0]
    assert job["attempts"] == 2 and from_db(job["nextAttemptAt"]) == clock() + timedelta(seconds=60)
    clock.advance(60)
    f2.run_jobs()
    job = f2.notifications(channel=WHATSAPP)[0]
    assert job["status"] == "sent" and job["attempts"] == 3 and f2.channels[WHATSAPP].calls == 3
    assert not f2.notifications(channel=EMAIL)


def test_both_channels_fail_marks_failed_and_surfaces_to_staff(f2, clock):
    f2.channels[WHATSAPP].default = "transient"
    f2.channels[EMAIL].default = "transient"
    match, _ = f2.match(f2.lost(ARUN), f2.found(), 0.9)
    for _ in range(12):
        f2.run_jobs()
        clock.advance(1800)
    jobs = {n["channel"]: n for n in _external(f2)}
    assert jobs[WHATSAPP]["status"] == jobs[EMAIL]["status"] == "failed"
    assert jobs[WHATSAPP]["attempts"] == jobs[EMAIL]["attempts"] == 4      # NOTIFY_MAX_ATTEMPTS
    m = f2.db.matches.find_one({"_id": match["_id"]})
    assert m["deliveryStatus"] == "failed" and m["status"] == "notified"
    rows = f2.staff_client().get("/staff/notifications?status=failed").get_json()["notifications"]
    assert len(rows) == 2 and all(r["lastError"] for r in rows) and all(r["retryable"] for r in rows)


def test_staff_retry_after_total_failure(f2, clock):
    f2.channels[WHATSAPP].default = "permanent"
    f2.channels[EMAIL].default = "permanent"
    match, _ = f2.match(f2.lost(ARUN), f2.found(), 0.9)
    f2.run_jobs()
    f2.channels[WHATSAPP].default = "ok"
    wa = f2.notifications(channel=WHATSAPP)[0]
    staff = f2.staff_client()
    resp = staff.post(f"/staff/notifications/{wa['_id']}/retry")
    assert resp.status_code == 200 and resp.get_json()["status"] == "pending"
    f2.run_jobs()
    assert len(f2.channels[WHATSAPP].sent) == 1
    assert f2.db.matches.find_one({"_id": match["_id"]})["deliveryStatus"] == "delivered"
    assert staff.post(f"/staff/notifications/{wa['_id']}/retry").status_code == 409   # now sent


def test_retry_after_partial_success_does_not_double_send(f2):
    f2.channels[WHATSAPP].default = "permanent"
    f2.match(f2.lost(ARUN), f2.found(), 0.9)
    f2.run_jobs()
    assert len(f2.channels[EMAIL].sent) == 1                # reached by email
    f2.channels[WHATSAPP].default = "ok"
    staff = f2.staff_client()
    wa = f2.notifications(channel=WHATSAPP)[0]
    email = f2.notifications(channel=EMAIL)[0]
    assert staff.post(f"/staff/notifications/{wa['_id']}/retry").status_code == 409
    assert staff.post(f"/staff/notifications/{email['_id']}/retry").status_code == 409
    f2.run_jobs()
    assert len(f2.channels[WHATSAPP].sent) == 0 and len(f2.channels[EMAIL].sent) == 1


def test_retry_is_scoped_to_staff_depot(f2):
    f2.channels[WHATSAPP].default = "permanent"
    f2.channels[EMAIL].default = "permanent"
    f2.match(f2.lost(ARUN), f2.found(depot=DEPOT_A), 0.9)
    f2.run_jobs()
    wa = f2.notifications(channel=WHATSAPP)[0]
    assert f2.staff_client(DEPOT_B).post(f"/staff/notifications/{wa['_id']}/retry").status_code == 404
    assert f2.staff_client(DEPOT_B).get("/staff/notifications").get_json()["notifications"] == []


# ── Hold expiry ──────────────────────────────────────────────────────────────
def test_hold_expiry_boundary_and_release(f2, clock):
    match, _ = f2.match(f2.lost(ARUN), f2.found(), 0.9)
    f2.run_jobs()
    clock.advance(7 * 86400 - 1)
    client = f2.login_passenger(ARUN)                       # sessions last 30 min, so log in now
    assert f2.svc.release_expired_matches() == 0
    view = client.get(f"/matches/{match['_id']}").get_json()["match"]
    assert view["state"] == "active" and view["holdSecondsRemaining"] == 1
    clock.advance(1)
    view = client.get(f"/matches/{match['_id']}").get_json()["match"]
    assert view["state"] == "expired"                       # shown expired even before the job runs
    assert f2.svc.release_expired_matches() == 1
    m = f2.db.matches.find_one({"_id": match["_id"]})
    assert m["status"] == "released" and m["releaseReason"] == "hold_expired"
    assert client.get(f"/matches/{match['_id']}").get_json()["match"]["state"] == "expired"
    page = client.get(f"/passenger/matches/{match['_id']}").get_data(as_text=True)
    assert "Hold expired" in page and "released" in page


def test_release_job_running_twice_concurrently_is_idempotent(f2, clock):
    for i in range(4):
        f2.match(f2.lost(ARUN, tracking=f"TRK-HOLD000{i}"), f2.found(found_id=f"item-{i}"), 0.9)
    f2.run_jobs()
    clock.advance(8 * 86400)
    counts = []
    threads = [threading.Thread(target=lambda: counts.append(f2.svc.release_expired_matches())) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum(counts) == 4 and f2.db.matches.count_documents({"status": "released"}) == 4


def test_released_match_is_not_resurrected_by_rerun(f2, clock):
    lost, found = f2.lost(ARUN), f2.found()
    f2.match(lost, found, 0.9)
    f2.run_jobs()
    clock.advance(8 * 86400)
    f2.svc.release_expired_matches()
    f2.match(lost, found, 0.99)
    f2.run_jobs()
    assert f2.db.matches.find_one()["status"] == "released" and len(f2.channels[WHATSAPP].sent) == 1


# ── Passenger endpoints, privacy and IST ─────────────────────────────────────
def test_match_view_is_privacy_safe_with_ist_and_countdown(f2):
    match, _ = f2.match(f2.lost(ARUN), f2.found(image_path="uploads/a.jpg",
                                                notes="Lenovo SN ABC12345", public="Black backpack"), 0.876)
    f2.run_jobs()
    client = f2.login_passenger(ARUN)
    view = client.get(f"/matches/{match['_id']}").get_json()["match"]
    assert set(view) == {"matchId", "reportId", "state", "confidencePercent", "foundAt", "itemDescription",
                         "dateFound", "notifiedAt", "holdExpiresAt", "holdExpiresAtIst",
                         "holdSecondsRemaining", "claim"}
    assert view["confidencePercent"] == 88 and view["dateFound"] == "21-Oct-2026"
    assert view["holdExpiresAt"].endswith("Z") and view["holdExpiresAtIst"].endswith("IST")
    assert view["holdSecondsRemaining"] == 7 * 86400 and view["claim"] == {"canStart": True}
    body = client.get(f"/matches/{match['_id']}").get_data(as_text=True)
    assert "uploads" not in body and "Lenovo" not in body and "ABC12345" not in body
    page = client.get(f"/passenger/matches/{match['_id']}").get_data(as_text=True)
    assert 'id="start-claim-btn"' in page and "hold-countdown" in page and "uploads/a.jpg" not in page


def test_report_matches_and_notifications_endpoints(f2):
    f2.match(f2.lost(ARUN), f2.found(), 0.9)
    client = f2.login_passenger(ARUN)
    reports = client.get("/reports").get_json()["reports"]
    assert [r["trackingId"] for r in reports] == ["TRK-AAAA0001"]
    data = client.get("/reports/TRK-AAAA0001/matches").get_json()
    assert data["report"]["trackingId"] == "TRK-AAAA0001" and len(data["matches"]) == 1
    notes = client.get("/notifications").get_json()["notifications"]
    assert len(notes) == 1 and notes[0]["reportId"] == "TRK-AAAA0001" and notes[0]["createdAtIst"].endswith("IST")
    assert client.get("/passenger/notifications").status_code == 200
    assert b"TRK-AAAA0001" in client.get("/passenger/reports/TRK-AAAA0001").data


def test_html_injection_in_description_is_escaped_on_pages(f2):
    payload = '<script>alert("x")</script><img src=x onerror=alert(1)>{{7*7}}'
    match, _ = f2.match(f2.lost(ARUN, description=payload), f2.found(public=payload), 0.9)
    client = f2.login_passenger(ARUN)
    for url in (f"/passenger/matches/{match['_id']}", "/passenger/reports/TRK-AAAA0001"):
        page = client.get(url).get_data(as_text=True)
        assert "<script>alert" not in page and "<img src=x" not in page
        assert "{{7*7}}" in page                            # shown literally, never evaluated
    view = client.get(f"/matches/{match['_id']}").get_json()["match"]
    assert "<script" not in view["itemDescription"] and "onerror" not in view["itemDescription"]


def test_very_long_and_tamil_descriptions(f2):
    match, _ = f2.match(f2.lost(KARTHIK), f2.found(public=TAMIL_DESC + " " + "x" * 5000), 0.9)
    f2.run_jobs()
    msg = f2.channels[WHATSAPP].sent[0]["text"]
    assert TAMIL_DESC in msg and len(msg) < 1200
    client = f2.login_passenger(KARTHIK)
    view = client.get(f"/matches/{match['_id']}").get_json()["match"]
    assert view["itemDescription"].startswith(TAMIL_DESC) and len(view["itemDescription"]) <= 81


def test_update_report_reruns_matcher(f2):
    lost, found = f2.lost(ARUN), f2.found()
    client = f2.login_passenger(ARUN)
    f2.matcher.scores[(lost["request_id"], found["found_id"])] = 0.9
    resp = client.patch("/reports/TRK-AAAA0001", json={"description": "Black Wildcraft backpack"})
    assert resp.status_code == 200 and resp.get_json()["report"]["description"] == "Black Wildcraft backpack"
    assert f2.db.matches.count_documents({}) == 1
    for bad in ({"description": {"$ne": 1}}, {"description": "x" * 2000}, {"oops": "x"}):
        assert client.patch("/reports/TRK-AAAA0001", json=bad).status_code in (400, 413)


# ── Authorization: IDOR, unauthenticated, staff-only ─────────────────────────
def test_idor_passenger_cannot_see_another_passengers_data(f2):
    match, _ = f2.match(f2.lost(ARUN), f2.found(), 0.9)
    f2.lost(PRIYA, tracking="TRK-PRIYA001")
    mallory = f2.login_passenger(PRIYA, client=f2.app.test_client())
    assert mallory.get(f"/matches/{match['_id']}").status_code == 404
    assert mallory.get("/reports/TRK-AAAA0001/matches").status_code == 404
    assert mallory.patch("/reports/TRK-AAAA0001", json={"description": "x"}).status_code == 404
    assert mallory.post("/reports/TRK-AAAA0001/cancel").status_code == 404
    assert mallory.get(f"/passenger/matches/{match['_id']}").status_code == 404
    assert mallory.get("/passenger/reports/TRK-AAAA0001").status_code == 404
    assert mallory.get("/notifications").get_json()["notifications"] == []
    assert [r["trackingId"] for r in mallory.get("/reports").get_json()["reports"]] == ["TRK-PRIYA001"]
    assert f2.db.lost_reports.find_one({"tracking_id": "TRK-AAAA0001"})["status"] != "cancelled"


def test_idor_same_pnr_other_passenger_phone(f2):
    match, _ = f2.match(f2.lost(MULTI_SHARED), f2.found(), 0.9)
    ravi = f2.login_passenger(MULTI_RAVI)                    # same PNR, different registered phone
    assert ravi.get(f"/matches/{match['_id']}").status_code == 404


@pytest.mark.parametrize("bad_id", ["not-an-id", str(ObjectId()), "1" * 300, "{\"$ne\":null}"])
def test_unknown_or_malformed_ids_are_404(f2, bad_id):
    client = f2.login_passenger(ARUN)
    assert client.get(f"/matches/{bad_id}").status_code == 404
    assert client.get(f"/reports/{bad_id}/matches").status_code == 404


@pytest.mark.parametrize("method,url", [
    ("get", "/reports"), ("get", "/reports/TRK-AAAA0001/matches"), ("get", "/matches/abc"),
    ("get", "/notifications"), ("post", "/reports/TRK-AAAA0001/cancel"), ("patch", "/reports/TRK-AAAA0001"),
])
def test_unauthenticated_passenger_api_is_401(f2, method, url):
    resp = getattr(f2.client, method)(url, json={})
    assert resp.status_code == 401 and _err(resp)["code"] == "session_missing"


@pytest.mark.parametrize("url", ["/passenger/notifications", "/passenger/matches/abc",
                                 "/passenger/reports/TRK-AAAA0001"])
def test_unauthenticated_passenger_pages_redirect(f2, url):
    resp = f2.client.get(url)
    assert resp.status_code == 302 and "/passenger/login" in resp.headers["Location"]


STAFF_ROUTES = [("get", "/staff/matches"), ("post", "/staff/matches/abc/approve"),
                ("post", "/staff/matches/abc/reject"), ("get", "/staff/notifications"),
                ("post", "/staff/notifications/abc/retry"), ("post", "/staff/matcher/rerun")]


@pytest.mark.parametrize("method,url", STAFF_ROUTES)
def test_staff_routes_unauthenticated_401(f2, method, url):
    resp = getattr(f2.client, method)(url, json={})
    assert resp.status_code == 401 and _err(resp)["code"] == "staff_login_required"


@pytest.mark.parametrize("method,url", STAFF_ROUTES)
def test_staff_routes_hit_by_passenger_403(f2, method, url):
    client = f2.login_passenger(ARUN)
    resp = getattr(client, method)(url, json={})
    assert resp.status_code == 403 and _err(resp)["code"] == "staff_only"


def test_staff_page_redirects_non_staff(f2):
    assert f2.client.get("/staff/review").status_code == 302
    page = f2.staff_client().get("/staff/review")
    assert page.status_code == 200 and b"Review queue" in page.data


def test_staff_cannot_act_on_other_depots_matches(f2):
    match, _ = f2.match(f2.lost(ARUN), f2.found(depot=DEPOT_A), 0.70)
    other = f2.staff_client(DEPOT_B)
    assert other.post(f"/staff/matches/{match['_id']}/approve").status_code == 404
    assert other.post(f"/staff/matches/{match['_id']}/reject").status_code == 404
    assert other.get("/staff/matches").get_json()["matches"] == []
    assert other.post("/staff/matcher/rerun", json={"itemId": "item-1"}).status_code == 404


def test_staff_filters_validated(f2):
    staff = f2.staff_client()
    assert staff.get("/staff/matches?status=bogus").status_code == 400
    assert staff.get("/staff/notifications?status=bogus").status_code == 400
    assert staff.post("/staff/matcher/rerun", json={"itemId": {"$ne": None}}).status_code == 400
    assert staff.get("/staff/matches?status=pending_staff_review").status_code == 200


def test_staff_actions_require_csrf(db, clock, sms):
    f2 = F2(db, clock, sms, csrf=True)
    match, _ = f2.match(f2.lost(ARUN), f2.found(), 0.70)
    resp = f2.staff_client().post(f"/staff/matches/{match['_id']}/approve")
    assert resp.status_code == 403 and _err(resp)["code"] == "csrf_failed"
    assert f2.db.matches.find_one()["status"] == "pending_staff_review"


# ── Legacy data migration ────────────────────────────────────────────────────
def test_legacy_matches_are_backfilled_without_notifying(db, clock, sms):
    db.matches.insert_one({"found_id": "f-old", "request_id": "r-old", "depot_phone": DEPOT_A["phone"],
                           "score": score_of(0.95), "status": "pending", "created_at": "2026-08-01T10:00:00"})
    f2 = F2(db, clock, sms)
    m = db.matches.find_one({"found_id": "f-old"})
    assert (m["reportId"], m["itemId"], m["status"], m["confidence"]) == ("r-old", "f-old",
                                                                         "pending_staff_review", 0.95)
    assert not f2.notifications()


# ── Seed data ────────────────────────────────────────────────────────────────
def test_seed_matches_covers_scenarios_and_is_idempotent(db, clock, sms):
    from match_notify import ensure_match_indexes
    from seed_matches import seed_matches
    ensure_match_indexes(db)
    first = seed_matches(db, now=clock())
    status = {db.lost_reports.find_one({"request_id": m["reportId"]})["tracking_id"]: m["status"]
              for m in db.matches.find()}
    assert status == {"TRK-DEMO0001": "notified", "TRK-DEMO0002": "notified",
                      "TRK-DEMO0003": "pending_staff_review", "TRK-DEMO0004": "rejected",
                      "TRK-DEMO0005": "released", "TRK-DEMO0006": "notified", "TRK-DEMO0007": "notified"}
    assert first["released"] == 1
    assert db.matches.count_documents({"itemId": "demo-item-redbag"}) == 2          # multi-report conflict
    channels = {(n["reportId"], n["channel"]): n["status"] for n in db.notifications.find()}
    assert channels[("demo-trk-demo0002", "email")] == "sent"                      # email-only passenger
    assert channels[("demo-trk-demo0007", "whatsapp")] == "failed"                 # no channel at all
    messages = db.message_log.count_documents({})
    seed_matches(db, now=clock())
    assert db.matches.count_documents({}) == 7 and db.message_log.count_documents({}) == messages
