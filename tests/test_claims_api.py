"""Integration tests for Feature 3: every endpoint, edge case and security property."""
import json
import threading
from datetime import timedelta

import pytest

from claims.tokens import sign_payload
from conftest import (ARUN, BAG_TRUTH, DEPOT_B, MULTI_SHARED, PRIYA, F3, PNG_BYTES, make_claims_config)
from passenger_auth.timeutil import from_db


def _err(resp):
    return resp.get_json()["error"]


def _claim(f3, claim_id):
    return f3.db.claims.find_one({"claimId": claim_id})


def _item(f3, found_id="item-1"):
    return f3.db.found_reports.find_one({"found_id": found_id})


# ── Full happy path at the API level ─────────────────────────────────────────
def test_open_answer_approve_token_pickup(f3, sms):
    client = f3.login_passenger(ARUN)
    match = f3.notified_match(ARUN)
    claim_id = f3.open_claim(client, match)
    c = _claim(f3, claim_id)
    assert c["status"] == "questions_pending" and len(c["pendingQuestions"]) == 3
    assert f3.db.matches.find_one({"_id": match["_id"]})["status"] == "claim_started"
    assert client.get(f"/claims/{claim_id}/photo").status_code == 403            # not yet verified

    resp = f3.answer(client, claim_id)
    view = resp.get_json()["claim"]
    assert view["status"] == "token_issued" and view["photoAvailable"] and view["tokenAvailable"]
    assert "trustScore" not in view and "outcomePerQuestion" not in json.dumps(view)
    assert _item(f3)["status"] == "claim_locked"

    token = f3.token(client, claim_id)
    assert len(token["code"]) == 9 and token["qrSvg"].lstrip().startswith("<svg")
    assert token["qrPayload"].startswith("TNSTC1.") and token["expiresInSeconds"] == 7 * 86400
    assert client.get(f"/claims/{claim_id}/token.svg").headers["Content-Disposition"].startswith("attachment")

    staff = f3.staff_client()
    check = staff.post("/pickup/verify", json={"token": token["qrPayload"]}).get_json()
    assert check["valid"] and check["claim"]["claimerName"] == "Arun Kumar"
    done = staff.post("/pickup/complete", json={"token": token["code"], "qrTagId": _item(f3)["qrTagId"],
                                                "idChecked": True, "staffName": "R. Selvi"})
    assert done.status_code == 200 and done.get_json()["status"] == "completed"
    assert _item(f3)["status"] == "handed_over"
    log = f3.db.custody_log.find_one({"event": "handover"})
    assert log["claimId"] == claim_id and log["staffName"] == "R. Selvi" and log["depotId"] == "9000000101"
    assert f3.db.lost_reports.find_one()["status"] == "resolved"
    kinds = [n.get("kind") for n in f3.db.notifications.find({"channel": "in_app"})]
    assert "claim_approved" in kinds and "pickup_completed" in kinds
    assert any("handed over" in m["body"] for m in sms.sent if m["purpose"] == "notification")
    titles = [n["title"] for n in client.get("/notifications").get_json()["notifications"]]
    assert any("Item collected" in t for t in titles)


def test_claim_record_holds_only_ids_and_outcomes(f3):
    client = f3.login_passenger(ARUN)
    claim_id = f3.open_claim(client, f3.notified_match(ARUN))
    f3.answer(client, claim_id, overrides={q: "zzsecretanswerzz" for q in f3.pending(claim_id)})
    raw = json.dumps(_claim(f3, claim_id), default=str).lower()
    assert "zzsecretanswerzz" not in raw
    assert all(set(o) == {"round", "question", "outcome"} and o["outcome"] in ("pass", "fail")
               for o in _claim(f3, claim_id)["outcomePerQuestion"])


# ── Access control ───────────────────────────────────────────────────────────
def test_idor_on_someone_elses_match_and_claim(f3):
    a_client = f3.login_passenger(ARUN)
    match = f3.notified_match(ARUN)
    claim_id = f3.open_claim(a_client, match)
    b = f3.login_passenger(PRIYA, client=f3.app.test_client())
    assert b.post("/claims", json={"matchId": str(match["_id"])}).status_code == 404
    for method, url in [("get", f"/claims/{claim_id}"), ("post", f"/claims/{claim_id}/cancel"),
                        ("get", f"/claims/{claim_id}/questions"), ("get", f"/claims/{claim_id}/photo"),
                        ("get", f"/claims/{claim_id}/token"), ("get", f"/passenger/claims/{claim_id}")]:
        assert getattr(b, method)(url).status_code == 404, url
    assert b.post(f"/claims/{claim_id}/answers", json={"answers": {"brand": "x"}}).status_code == 404
    assert b.get("/claims").get_json()["claims"] == []
    assert _claim(f3, claim_id)["status"] == "questions_pending"


@pytest.mark.parametrize("method,url", [
    ("get", "/claims"), ("post", "/claims"), ("get", "/claims/x"), ("post", "/claims/x/cancel"),
    ("get", "/claims/x/questions"), ("post", "/claims/x/answers"), ("get", "/claims/x/photo"),
    ("get", "/claims/x/photo/file"), ("get", "/claims/x/token"), ("get", "/claims/x/token.svg")])
def test_unauthenticated_passenger_endpoints(f3, method, url):
    resp = getattr(f3.client, method)(url, json={})
    assert resp.status_code == 401


STAFF = [("get", "/staff/claims"), ("post", "/staff/claims/x/approve"), ("post", "/staff/claims/x/reject"),
         ("post", "/staff/claims/x/reissue-token"), ("post", "/pickup/verify"), ("post", "/pickup/complete")]


@pytest.mark.parametrize("method,url", STAFF)
def test_staff_endpoints_need_staff(f3, method, url):
    assert getattr(f3.client, method)(url, json={}).status_code == 401
    passenger = f3.login_passenger(ARUN)
    assert getattr(passenger, method)(url, json={}).status_code == 403


def test_found_item_photos_are_not_publicly_served(f3):
    f3.app.static_folder = str(f3.upload_root)
    f3.item()
    assert f3.client.get("/static/uploads/item-1.png").status_code == 404
    assert f3.login_passenger(ARUN).get("/static/uploads/item-1.png").status_code == 404
    assert f3.staff_client().get("/static/uploads/item-1.png").status_code == 200


# ── Opening claims ───────────────────────────────────────────────────────────
def test_duplicate_claim_by_same_passenger(f3):
    client = f3.login_passenger(ARUN)
    match = f3.notified_match(ARUN)
    claim_id = f3.open_claim(client, match)
    again = client.post("/claims", json={"matchId": str(match["_id"])})
    assert again.status_code == 409 and _err(again)["claimId"] == claim_id
    assert f3.db.claims.count_documents({}) == 1


def test_parallel_duplicate_claims_create_one(f3):
    match = f3.notified_match(ARUN)
    clients = [f3.login_passenger(ARUN, client=f3.app.test_client())]
    f3.clock.advance(31)
    clients.append(f3.app.test_client())
    for c in clients[1:]:
        c.set_cookie("tnstc_pax", clients[0].get_cookie("tnstc_pax").value)
    barrier, codes = threading.Barrier(2), []

    def go(c):
        barrier.wait(timeout=5)
        codes.append(c.post("/claims", json={"matchId": str(match["_id"])}).status_code)

    threads = [threading.Thread(target=go, args=(c,)) for c in clients]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(codes) == [201, 409] and f3.db.claims.count_documents({}) == 1


def test_claim_after_hold_expired(f3):
    match = f3.notified_match(ARUN)
    f3.clock.advance(7 * 86400)
    client = f3.login_passenger(ARUN)
    resp = client.post("/claims", json={"matchId": str(match["_id"])})
    assert resp.status_code == 409 and _err(resp)["code"] == "match_not_claimable"


@pytest.mark.parametrize("status", ["claim_locked", "handed_over", "claimed"])
def test_item_already_claimed_or_handed_over(f3, status):
    client = f3.login_passenger(ARUN)
    match = f3.notified_match(ARUN)
    f3.db.found_reports.update_one({"found_id": "item-1"}, {"$set": {"status": status}})
    resp = client.post("/claims", json={"matchId": str(match["_id"])})
    assert resp.status_code == 409 and _err(resp)["code"] == "item_unavailable"


def test_daily_claim_cap(db, clock, sms, tmp_path):
    f3 = F3(db, clock, sms, tmp_path, claims_config=make_claims_config(max_claims_per_day=2))
    client = f3.login_passenger(ARUN)
    codes = []
    for i in range(3):
        match = f3.notified_match(ARUN, tracking=f"TRK-CAP0000{i}", found_id=f"item-{i}")
        codes.append(client.post("/claims", json={"matchId": str(match["_id"])}).status_code)
    assert codes == [201, 201, 429]


def test_ip_rate_limit_on_claim_endpoints(db, clock, sms, tmp_path):
    f3 = F3(db, clock, sms, tmp_path, claims_config=make_claims_config(ip_limit=3))
    client = f3.login_passenger(ARUN)
    codes = [client.get("/claims").status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]


def test_insufficient_attributes_go_straight_to_staff(f3):
    client = f3.login_passenger(ARUN)
    match = f3.notified_match(ARUN, truth={"brand": "Wildcraft"})
    claim_id = f3.open_claim(client, match)
    assert _claim(f3, claim_id)["status"] == "staff_review"
    assert client.get(f"/claims/{claim_id}/questions").status_code == 409


def test_questions_never_answerable_from_notification(f3):
    client = f3.login_passenger(ARUN)
    match = f3.notified_match(ARUN, public="Wildcraft backpack, yellow tag")
    claim_id = f3.open_claim(client, match)
    asked = _claim(f3, claim_id)["questionSet"] + [_claim(f3, claim_id)["reserveQuestion"]]
    assert "brand" not in asked and "zipper_tag_colour" not in asked
    for q in client.get(f"/claims/{claim_id}/questions").get_json()["questions"]:
        assert set(q) == {"id", "text"}


# ── Answering ────────────────────────────────────────────────────────────────
def test_wrong_answers_then_retry_reveals_only_progress(f3):
    client = f3.login_passenger(ARUN)
    claim_id = f3.open_claim(client, f3.notified_match(ARUN))
    first = f3.pending(claim_id)
    resp = f3.answer(client, claim_id, wrong=set(BAG_TRUTH))
    body = resp.get_json()["claim"]
    assert resp.status_code == 200 and body["status"] == "questions_pending"
    assert body["progress"] == {"round": 2, "maxRounds": 3, "roundsUsed": 1, "followUp": False}
    text = json.dumps(resp.get_json())
    for q in first:
        assert q not in text                                   # which answers failed is never said
    history = [(h["from"], h["to"]) for h in _claim(f3, claim_id)["history"]]
    assert ("questions_pending", "failed") in history and ("failed", "questions_pending") in history
    assert f3.answer(client, claim_id).get_json()["claim"]["status"] == "token_issued"


def test_lockout_after_max_rounds_then_staff_id_check(f3):
    client = f3.login_passenger(ARUN)
    claim_id = f3.open_claim(client, f3.notified_match(ARUN))
    for _ in range(3):
        last = f3.answer(client, claim_id, wrong=set(BAG_TRUTH))
    assert last.get_json()["claim"]["status"] == "locked"
    assert client.get(f"/claims/{claim_id}/questions").status_code == 409
    assert client.post(f"/claims/{claim_id}/answers", json={"answers": {"brand": "Wildcraft"}}).status_code == 409
    staff = f3.staff_client()
    assert [c["claimId"] for c in staff.get("/staff/claims?status=locked").get_json()["claims"]] == [claim_id]
    no_id = staff.post(f"/staff/claims/{claim_id}/approve", json={})
    assert no_id.status_code == 400 and _err(no_id)["code"] == "id_verification_required"
    ok = staff.post(f"/staff/claims/{claim_id}/approve", json={"idVerified": True})
    assert ok.status_code == 200 and ok.get_json()["status"] == "token_issued"
    assert _claim(f3, claim_id)["idVerified"] is True


def _borderline_setup(f3):
    """Round of content_item(1.5), distinguishing_mark(1.5), brand(1.0) with zipper colour in reserve."""
    client = f3.login_passenger(ARUN)
    claim_id = f3.open_claim(client, f3.notified_match(ARUN))
    f3.db.claims.update_one({"claimId": claim_id}, {"$set": {
        "questionSet": ["content_item", "distinguishing_mark", "brand"],
        "pendingQuestions": ["content_item", "distinguishing_mark", "brand"],
        "reserveQuestion": "zipper_tag_colour"}})
    return client, claim_id


def test_borderline_adds_question_then_routes_to_staff(f3):
    client, claim_id = _borderline_setup(f3)
    resp = f3.answer(client, claim_id, wrong={"distinguishing_mark"})        # 2.5/4 = 0.625
    claim = resp.get_json()["claim"]
    assert claim["status"] == "questions_pending" and claim["progress"]["followUp"] is True
    assert f3.pending(claim_id) == ["zipper_tag_colour"]
    resp = f3.answer(client, claim_id, wrong={"zipper_tag_colour"})          # 2.5/5 = 0.5 → still borderline
    assert resp.get_json()["claim"]["status"] == "staff_review"
    assert client.get(f"/claims/{claim_id}/photo").status_code == 403         # threshold not reached
    staff = f3.staff_client()
    assert [c["claimId"] for c in staff.get("/staff/claims?status=staff_review").get_json()["claims"]] == [claim_id]


def _follow_up_setup(f3, ident, tracking, found_id):
    """Round brand(1.0) + zipper colour(1.0) + contents(1.5), with the mark question (1.5) in reserve."""
    item = f3.item(found_id=found_id)
    client = f3.login_passenger(ident, client=f3.app.test_client())
    claim_id = f3.open_claim(client, f3.notified_match(ident, tracking=tracking, item=item))
    f3.db.claims.update_one({"claimId": claim_id}, {"$set": {
        "questionSet": ["brand", "zipper_tag_colour", "content_item"],
        "pendingQuestions": ["brand", "zipper_tag_colour", "content_item"],
        "reserveQuestion": "distinguishing_mark"}})
    f3.answer(client, claim_id, wrong={"brand"})                             # 2.5/3.5 = 0.71 → follow-up
    assert f3.pending(claim_id) == ["distinguishing_mark"]
    return client, claim_id


def test_borderline_follow_up_can_verify(f3):
    client, claim_id = _follow_up_setup(f3, ARUN, "TRK-ARUN0001", "item-1")
    resp = f3.answer(client, claim_id, overrides={"distinguishing_mark": "Chennai Super Kings sticker"})
    assert resp.get_json()["claim"]["status"] == "token_issued"                # 4/5 = 0.8


def test_borderline_follow_up_is_strict(f3):
    # These typos pass in a normal round (fuzzy) but not in the stricter follow-up question.
    from claims.scoring import answer_passes
    sloppy = "Chenai Supr Kings stiker"
    client, claim_id = _follow_up_setup(f3, PRIYA, "TRK-PRIY0001", "item-2")
    mark = f3.db.found_reports.find_one({"found_id": "item-2"})["private_attributes"]["distinguishing_mark"]
    assert answer_passes(f3.csvc.answer_key, mark, sloppy, f3.claims_state.config, strict=False)
    resp = f3.answer(client, claim_id, overrides={"distinguishing_mark": sloppy})
    assert resp.get_json()["claim"]["status"] == "staff_review"               # 2.5/5 = 0.5


def test_answers_in_tamil_tanglish_english_and_typos(f3):
    client = f3.login_passenger(ARUN)
    claim_id = f3.open_claim(client, f3.notified_match(ARUN))
    variants = {"zipper_tag_colour": "மஞ்சள்", "content_item": "oru steel dabba",
                "brand": "wildcarft", "distinguishing_mark": "CSK super kings stiker"}
    resp = f3.answer(client, claim_id, overrides=variants)
    assert resp.get_json()["claim"]["status"] == "token_issued"


@pytest.mark.parametrize("value,status", [
    ("", 400), ("   ", 400), ("x" * 201, 400), ({"$ne": None}, 400), (["Wildcraft"], 400), (123, 400),
])
def test_invalid_answers_rejected(f3, value, status):
    client = f3.login_passenger(ARUN)
    claim_id = f3.open_claim(client, f3.notified_match(ARUN))
    answers = {q: BAG_TRUTH[q] for q in f3.pending(claim_id)}
    answers[next(iter(answers))] = value
    assert client.post(f"/claims/{claim_id}/answers", json={"answers": answers}).status_code == status
    assert _claim(f3, claim_id)["roundsUsed"] == 0


@pytest.mark.parametrize("payload", ["😀😀😀", "<script>alert(1)</script>", "{{7*7}}", "'; DROP TABLE claims; --",
                                     "${jndi:ldap://x}"])
def test_emoji_and_injection_answers_are_just_wrong(f3, payload):
    client = f3.login_passenger(ARUN)
    claim_id = f3.open_claim(client, f3.notified_match(ARUN))
    resp = f3.answer(client, claim_id, overrides={q: payload for q in f3.pending(claim_id)})
    assert resp.status_code == 200 and resp.get_json()["claim"]["progress"]["roundsUsed"] == 1
    assert payload.lower() not in json.dumps(_claim(f3, claim_id), default=str).lower()


def test_answer_set_must_match_questions(f3):
    client = f3.login_passenger(ARUN)
    claim_id = f3.open_claim(client, f3.notified_match(ARUN))
    assert client.post(f"/claims/{claim_id}/answers", json={"answers": {"brand": "x"}}).status_code == 400
    assert client.post(f"/claims/{claim_id}/answers", json={"answers": {"lock_screen": "x"}}).status_code == 400
    assert client.post(f"/claims/{claim_id}/answers", json={"nope": 1}).status_code == 400


def test_answering_after_approval(f3):
    client, claim_id = f3.approved_claim()
    resp = client.post(f"/claims/{claim_id}/answers", json={"answers": {"brand": "Wildcraft"}})
    assert resp.status_code == 409 and _err(resp)["code"] == "no_questions"


def test_replayed_answer_submission_is_rejected(f3):
    client = f3.login_passenger(ARUN)
    claim_id = f3.open_claim(client, f3.notified_match(ARUN))
    answers = {q: "wrong wrong" for q in f3.pending(claim_id)}
    stale = dict(_claim(f3, claim_id))
    assert client.post(f"/claims/{claim_id}/answers", json={"answers": answers}).status_code == 200
    with pytest.raises(Exception) as e:                       # the same round replayed from a stale copy
        f3.csvc.submit_answers(stale, dict(answers), type("P", (), {"pnr": "T26100101", "phone": "+919876543210"}))
    assert getattr(e.value, "code", "") == "stale_submission"


def test_daily_answer_cap(db, clock, sms, tmp_path):
    f3 = F3(db, clock, sms, tmp_path, claims_config=make_claims_config(max_answer_submissions_per_day=2,
                                                                         max_rounds=5))
    client = f3.login_passenger(ARUN)
    claim_id = f3.open_claim(client, f3.notified_match(ARUN))
    codes = [f3.answer(client, claim_id, wrong=set(BAG_TRUTH)).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


# ── Photo gating ─────────────────────────────────────────────────────────────
def test_skipping_to_photo_or_token_without_passing(f3):
    client = f3.login_passenger(ARUN)
    claim_id = f3.open_claim(client, f3.notified_match(ARUN))
    assert _err(client.get(f"/claims/{claim_id}/photo"))["code"] == "claim_not_verified"
    assert _err(client.get(f"/claims/{claim_id}/token"))["code"] == "token_not_available"
    exp = int((f3.clock() + timedelta(seconds=60)).timestamp())
    sig = f3.csvc._photo_sig(claim_id, exp)                     # even a correctly signed link
    assert client.get(f"/claims/{claim_id}/photo/file?exp={exp}&sig={sig}").status_code == 403


def test_signed_photo_url_expiry_and_binding(f3):
    client, claim_id = f3.approved_claim()
    link = client.get(f"/claims/{claim_id}/photo").get_json()
    assert link["expiresInSeconds"] == 120
    ok = client.get(link["url"])
    assert ok.status_code == 200 and ok.data == PNG_BYTES and "no-store" in ok.headers["Cache-Control"]
    f3.clock.advance(119)
    assert client.get(link["url"]).status_code == 200            # clock skew: last second still valid
    f3.clock.advance(1)
    assert _err(client.get(link["url"]))["code"] == "photo_link_expired"
    fresh = client.get(f"/claims/{claim_id}/photo").get_json()["url"]
    assert client.get(fresh.replace("sig=", "sig=0")).status_code == 403
    other_client, other_id = f3.approved_claim(PRIYA, tracking="TRK-PRIY0001", found_id="item-2")
    assert other_client.get(fresh.replace(claim_id, other_id)).status_code == 403   # bound to the claim


def test_photo_never_in_lists_notifications_or_match_view(f3):
    client, claim_id = f3.approved_claim()
    for url in ("/claims", f"/claims/{claim_id}", "/notifications", "/reports",
                f"/matches/{_claim(f3, claim_id)['matchId']}"):
        body = client.get(url).get_data(as_text=True)
        assert "uploads/" not in body and ".png" not in body, url


# ── Locking and concurrency ──────────────────────────────────────────────────
def test_two_passengers_claiming_same_item_simultaneously(f3, monkeypatch):
    item = f3.item()
    a = f3.login_passenger(ARUN, client=f3.app.test_client())
    b = f3.login_passenger(MULTI_SHARED, client=f3.app.test_client())
    ca = f3.open_claim(a, f3.notified_match(ARUN, tracking="TRK-ARUN0001", item=item))
    cb = f3.open_claim(b, f3.notified_match(MULTI_SHARED, tracking="TRK-SENT0001", item=item))
    import claims.service as svc_mod
    real = svc_mod.run_atomically
    barrier = threading.Barrier(2)

    def synced(db, work):
        try:
            barrier.wait(timeout=2)
        except threading.BrokenBarrierError:
            pass
        return real(db, work)

    monkeypatch.setattr(svc_mod, "run_atomically", synced)
    results = {}

    def go(name, client, cid):
        results[name] = f3.answer(client, cid).get_json()["claim"]["status"]

    threads = [threading.Thread(target=go, args=x) for x in (("a", a, ca), ("b", b, cb))]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results.values()) == ["cancelled", "token_issued"]
    winner = ca if results["a"] == "token_issued" else cb
    assert _item(f3)["claimLock"]["claimId"] == winner
    assert f3.db.claims.count_documents({"lockActive": True}) == 1
    loser_client = b if winner == ca else a
    loser_match = _claim(f3, cb if winner == ca else ca)["matchId"]
    assert loser_client.get(f"/matches/{loser_match}").get_json()["match"]["state"] == "unavailable"
    body = loser_client.get(f"/matches/{loser_match}").get_data(as_text=True)
    winner_report, winner_name = (("TRK-ARUN0001", "Arun") if winner == ca else ("TRK-SENT0001", "Senthil"))
    assert winner_report not in body and winner_name not in body and winner not in body


def test_database_unique_index_backs_the_item_lock(f3):
    from pymongo.errors import DuplicateKeyError
    f3.db.claims.insert_one({"claimId": "x1", "itemId": "item-9", "lockActive": True})
    with pytest.raises(DuplicateKeyError):
        f3.db.claims.insert_one({"claimId": "x2", "itemId": "item-9", "lockActive": True})


def test_passenger_cancel_unlocks_item_for_others(f3):
    item = f3.item()
    a, ca = f3.approved_claim(item=item)
    b = f3.login_passenger(PRIYA, client=f3.app.test_client())
    match_b = f3.notified_match(PRIYA, tracking="TRK-PRIY0001", item=item)
    assert b.post("/claims", json={"matchId": str(match_b["_id"])}).status_code == 409   # locked
    resp = a.post(f"/claims/{ca}/cancel")
    assert resp.status_code == 200 and resp.get_json()["claim"]["status"] == "cancelled"
    assert _item(f3)["status"] == "open" and "claimLock" not in _item(f3)
    assert f3.db.matches.find_one({"_id": f3.db.matches.find_one({"reportId": "req-TRK-AAAA0001"})["_id"]})["status"] == "notified"
    assert f3.open_claim(b, match_b)
    assert a.post(f"/claims/{ca}/cancel").status_code == 409                               # already cancelled


# ── Tokens and pickup ────────────────────────────────────────────────────────
def _pickup(f3, token, tag=None, staff=None, **extra):
    staff = staff or f3.staff_client()
    body = {"token": token, "qrTagId": tag if tag is not None else _item(f3)["qrTagId"], "idChecked": True}
    body.update(extra)
    return staff.post("/pickup/complete", json=body)


def _verify(f3, token, staff=None, **extra):
    staff = staff or f3.staff_client()
    return staff.post("/pickup/verify", json={"token": token, **extra}).get_json()


def test_token_expiry_boundary_and_release(f3):
    client, claim_id = f3.approved_claim()
    token = f3.token(client, claim_id)
    f3.clock.advance(7 * 86400 - 1)
    assert _verify(f3, token["code"])["valid"] is True               # clock skew: 1 s before expiry
    f3.clock.advance(1)
    result = _verify(f3, token["code"])
    assert result == {"ok": True, "valid": False, "reason": "expired", "message": result["message"]}
    assert _claim(f3, claim_id)["status"] == "expired" and _item(f3)["status"] == "open"
    assert _pickup(f3, token["code"]).status_code == 409


def test_reused_token(f3):
    client, claim_id = f3.approved_claim()
    token = f3.token(client, claim_id)
    assert _pickup(f3, token["code"]).status_code == 200
    again = _pickup(f3, token["qrPayload"])
    assert again.status_code == 409 and _err(again)["code"] == "already_used"
    assert _verify(f3, token["code"])["reason"] == "already_used"


def test_forged_or_tampered_qr(f3):
    client, claim_id = f3.approved_claim()
    payload = f3.token(client, claim_id)["qrPayload"]
    head, body, sig = payload.split(".")
    assert _verify(f3, f"{head}.{body}.{sig[::-1]}")["reason"] == "bad_signature"
    assert _verify(f3, f"{head}.{body[:-4]}AAAA.{sig}")["reason"] in ("bad_signature", "malformed")
    forged = sign_payload(b"attacker-key-000000000000000000000", {"v": 1, "cid": claim_id, "iid": "item-1",
                                                                  "tag": "TAG-X", "pnr": "T", "exp": 9999999999,
                                                                  "code": "A7X2K9P4"})
    assert _verify(f3, forged)["reason"] == "bad_signature"
    assert _verify(f3, "TNSTC1.only-two")["reason"] == "malformed"
    assert _verify(f3, "ZZZZ-ZZZZ")["reason"] == "not_found"


def test_token_for_a_different_item_or_tag(f3):
    client, claim_id = f3.approved_claim()
    token = f3.token(client, claim_id)
    other = f3.item(found_id="item-2")
    wrong_tag = _pickup(f3, token["code"], tag=other["qrTagId"])
    assert wrong_tag.status_code == 409 and _err(wrong_tag)["code"] == "tag_mismatch"
    assert _verify(f3, token["code"], qrTagId=other["qrTagId"])["reason"] == "tag_mismatch"
    c = _claim(f3, claim_id)
    code = f3.csvc._code(c)
    retargeted = sign_payload(f3.csvc.token_key, {"v": 1, "cid": claim_id, "iid": "item-2", "tag": other["qrTagId"],
                                                  "pnr": c["pnr"], "exp": int(from_db(c["tokenExpiresAt"]).timestamp()),
                                                  "code": code})
    assert _verify(f3, retargeted)["reason"] == "item_mismatch"
    missing_tag = f3.staff_client().post("/pickup/complete", json={"token": token["code"], "idChecked": True})
    assert _err(missing_tag)["code"] == "tag_required"
    assert _item(f3)["status"] == "claim_locked"


def test_staff_scanning_at_wrong_depot(f3):
    client, claim_id = f3.approved_claim()
    token = f3.token(client, claim_id)
    other = f3.staff_client(DEPOT_B)
    assert _verify(f3, token["code"], staff=other)["reason"] == "wrong_depot"
    assert _err(_pickup(f3, token["code"], staff=other))["code"] == "wrong_depot"


def test_pickup_requires_id_check(f3):
    client, claim_id = f3.approved_claim()
    resp = _pickup(f3, f3.token(client, claim_id)["code"], idChecked=False)
    assert resp.status_code == 400 and _err(resp)["code"] == "id_check_required"


def test_lost_phone_reissue_revokes_old_token(f3):
    client, claim_id = f3.approved_claim()
    old = f3.token(client, claim_id)
    staff = f3.staff_client()
    assert staff.post(f"/staff/claims/{claim_id}/reissue-token", json={}).status_code == 400
    resp = staff.post(f"/staff/claims/{claim_id}/reissue-token", json={"idVerified": True})
    assert resp.status_code == 200 and resp.get_json()["tokenVersion"] == 2
    new = f3.token(client, claim_id)
    assert new["code"] != old["code"]
    assert _verify(f3, old["code"])["reason"] == "not_found"
    assert _verify(f3, old["qrPayload"])["reason"] in ("revoked", "item_mismatch")
    assert _pickup(f3, new["qrPayload"]).status_code == 200
    assert any(h["reason"] and "reissued" in h["reason"] for h in _claim(f3, claim_id)["history"])


def test_token_for_claim_whose_item_was_released(f3):
    client, claim_id = f3.approved_claim()
    token = f3.token(client, claim_id)
    f3.db.found_reports.update_one({"found_id": "item-1"}, {"$set": {"status": "open"}, "$unset": {"claimLock": ""}})
    assert _verify(f3, token["code"])["reason"] == "item_released"
    assert _err(_pickup(f3, token["code"]))["code"] == "item_released"


def test_concurrent_pickup_scans_only_one_succeeds(f3, monkeypatch):
    client, claim_id = f3.approved_claim()
    token = f3.token(client, claim_id)
    tag = _item(f3)["qrTagId"]
    staffs = [f3.staff_client() for _ in range(5)]
    barrier, codes = threading.Barrier(5), []
    # Worst case: every scan passes the read-only checks before any of them commits.
    import claims.service as svc_mod
    real, commit_gate = svc_mod.run_atomically, threading.Barrier(5)

    def gated(db, work):
        try:
            commit_gate.wait(timeout=2)
        except threading.BrokenBarrierError:
            pass
        return real(db, work)

    monkeypatch.setattr(svc_mod, "run_atomically", gated)

    def go(staff):
        barrier.wait(timeout=5)
        codes.append(staff.post("/pickup/complete", json={"token": token["code"], "qrTagId": tag,
                                                          "idChecked": True}).status_code)

    threads = [threading.Thread(target=go, args=(s,)) for s in staffs]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert codes.count(200) == 1 and codes.count(409) == 4
    assert f3.db.custody_log.count_documents({"event": "handover"}) == 1


@pytest.mark.parametrize("fail_at", ["custody_log", "matches", "lost_reports"])
def test_db_failure_mid_handover_rolls_back_everything(f3, monkeypatch, fail_at):
    client, claim_id = f3.approved_claim()
    token = f3.token(client, claim_id)
    before = {"claim": _claim(f3, claim_id), "item": _item(f3), "match": f3.db.matches.find_one(),
              "report": f3.db.lost_reports.find_one()}
    import claims.txn as txn
    real_insert, real_cas = txn.Tx.insert, txn.Tx.cas_update

    def boom_insert(self, collection, doc):
        if collection == fail_at:
            raise RuntimeError("simulated DB outage")
        return real_insert(self, collection, doc)

    def boom_cas(self, collection, query, update, reason):
        if collection == fail_at:
            raise RuntimeError("simulated DB outage")
        return real_cas(self, collection, query, update, reason)

    monkeypatch.setattr(txn.Tx, "insert", boom_insert)
    monkeypatch.setattr(txn.Tx, "cas_update", boom_cas)
    with pytest.raises(RuntimeError):
        _pickup(f3, token["code"])
    assert _claim(f3, claim_id) == before["claim"]
    assert _item(f3) == before["item"]
    assert f3.db.matches.find_one() == before["match"]
    assert f3.db.lost_reports.find_one() == before["report"]
    assert f3.db.custody_log.count_documents({"event": "handover"}) == 0
    monkeypatch.setattr(txn.Tx, "insert", real_insert)
    monkeypatch.setattr(txn.Tx, "cas_update", real_cas)
    assert _pickup(f3, token["code"]).status_code == 200                # nothing partial blocked a retry


# ── Staff review and state rules ─────────────────────────────────────────────
def test_staff_reject_and_illegal_transitions(f3):
    client, claim_id = _borderline_setup(f3)
    f3.answer(client, claim_id, wrong={"distinguishing_mark"})
    f3.answer(client, claim_id, wrong={"zipper_tag_colour"})
    staff = f3.staff_client()
    assert staff.post(f"/staff/claims/{claim_id}/reissue-token", json={"idVerified": True}).status_code == 409
    resp = staff.post(f"/staff/claims/{claim_id}/reject")
    assert resp.status_code == 200 and resp.get_json()["status"] == "cancelled"
    assert staff.post(f"/staff/claims/{claim_id}/approve", json={}).status_code == 409
    assert staff.post(f"/staff/claims/{claim_id}/reject").status_code == 409
    assert client.post(f"/claims/{claim_id}/cancel").status_code == 409


def test_staff_approve_borderline_issues_token(f3, sms):
    client, claim_id = _borderline_setup(f3)
    f3.answer(client, claim_id, wrong={"distinguishing_mark"})
    f3.answer(client, claim_id, wrong={"zipper_tag_colour"})
    resp = f3.staff_client().post(f"/staff/claims/{claim_id}/approve", json={})
    assert resp.status_code == 200 and resp.get_json()["status"] == "token_issued"
    assert client.get(f"/claims/{claim_id}/token").status_code == 200
    assert any("approved" in m["body"] for m in sms.sent if m["purpose"] == "notification")


def test_staff_are_scoped_to_their_depot(f3):
    client, claim_id = _borderline_setup(f3)
    other = f3.staff_client(DEPOT_B)
    assert other.get("/staff/claims").get_json()["claims"] == []
    assert other.post(f"/staff/claims/{claim_id}/approve", json={}).status_code == 404
    assert other.post(f"/staff/claims/{claim_id}/reissue-token", json={"idVerified": True}).status_code == 404


def test_staff_filter_and_body_validation(f3):
    staff = f3.staff_client()
    assert staff.get("/staff/claims?status=bogus").status_code == 400
    assert staff.post("/pickup/verify", json={"token": {"$ne": None}}).status_code == 400
    assert staff.post("/pickup/verify", json={}).status_code == 400
    assert staff.post("/pickup/complete", json={"token": "x", "idChecked": "yes"}).status_code == 400


def test_claim_expires_with_match_hold_and_frees_item(f3):
    client = f3.login_passenger(ARUN)
    claim_id = f3.open_claim(client, f3.notified_match(ARUN))
    f3.clock.advance(7 * 86400)
    assert f3.csvc.expire_due_claims() == 1
    assert _claim(f3, claim_id)["status"] == "expired"
    assert f3.db.matches.find_one()["status"] == "released"


def test_staff_actions_require_csrf(db, clock, sms, tmp_path):
    f3 = F3(db, clock, sms, tmp_path, csrf=True)
    resp = f3.staff_client().post("/pickup/verify", json={"token": "ABCD-EFGH"})
    assert resp.status_code == 403 and _err(resp)["code"] == "csrf_failed"


# ── Seed data ────────────────────────────────────────────────────────────────
def test_seed_claims_scenarios_and_idempotency(db, clock, tmp_path):
    from claims import ensure_claim_indexes
    from match_notify import ensure_match_indexes
    from seed_claims import seed_claims
    from seed_matches import seed_matches
    from conftest import TEST_SECRET
    ensure_match_indexes(db)
    ensure_claim_indexes(db)
    seed_matches(db, now=clock())
    result = seed_claims(db, TEST_SECRET, now=clock(), upload_dir=str(tmp_path))
    assert result == {"ready_for_pickup": "token_issued", "conflict_winner": "token_issued",
                      "borderline_staff_review": "staff_review", "expired_token": "token_issued",
                      "expired_now": 1}
    status = {c["reportId"]: c["status"] for c in db.claims.find()}
    assert status["demo-trk-demo0009"] == "expired"
    assert db.found_reports.find_one({"found_id": "demo-item-laptop"})["status"] == "open"
    assert db.found_reports.find_one({"found_id": "demo-item-redbag"})["status"] == "claim_locked"
    from seed_claims import SECRETS
    seeded = list(db.found_reports.find({"found_id": {"$in": list(SECRETS)}}))
    assert len(seeded) == 5 and all(i.get("qrTagId") and i.get("private_attributes") and i.get("image_path")
                                    for i in seeded)
    again = seed_claims(db, TEST_SECRET, now=clock(), upload_dir=str(tmp_path))
    assert set(again.values()) == {"already seeded", 0} and db.claims.count_documents({}) == 4
