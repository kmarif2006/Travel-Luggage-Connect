"""
FULL end-to-end across all three features through the REAL app.py routes:

PNR + phone → OTP → lost report → depot logs item (category, secret attributes,
photo, QR tag) → existing matcher → WhatsApp notification (mock) → passenger
opens the match → "This is my item" → questions (Tamil/Tanglish answers) →
photo via signed URL → approval → pickup code + QR → staff verify + handover →
custody log entry → item handed_over → passenger notified.
"""
import io
import os

from conftest import ARUN, PNG_BYTES
from test_match_e2e import _login, _report_lost, _today, e2e, real_app  # noqa: F401  (fixtures)


def _log_item_with_secrets(depot):
    return depot.post("/depot/found", content_type="multipart/form-data", data={
        "route_id": "R100", "trip_id": "T1", "date": _today(),
        "notes": "black backpack with laptop", "public_description": "Black backpack",
        "category": "bag",
        "attr_zipper_tag_colour": "Yellow", "attr_content_item": "steel tiffin box",
        "attr_brand": "Wildcraft", "attr_distinguishing_mark": "Chennai Super Kings sticker",
        "attr_lock_screen": "should be dropped: not a bag attribute",
        "image": (io.BytesIO(PNG_BYTES), "backpack.png"),
    })


def test_full_journey_all_three_features(e2e):
    app_module, db, sms = e2e
    app = app_module.app
    uploaded = []
    try:
        # Feature 1: OTP login, then a lost report owned by the verified passenger.
        passenger = _login(app.test_client(), sms, ARUN)
        assert _report_lost(passenger, "black backpack with laptop", _today()).status_code == 302
        lost = db.lost_reports.find_one()

        # Depot logs the item with its secret ownership details and a photo.
        depot = app.test_client()
        depot.post("/depot/login", data={"phone": "9000000004", "password": "pass123"})
        assert _log_item_with_secrets(depot).status_code == 302
        item = db.found_reports.find_one()
        uploaded.append(os.path.join("static", item["image_path"]))
        assert item["category"] == "bag" and item["qrTagId"].startswith("TAG-")
        assert set(item["private_attributes"]) == {"zipper_tag_colour", "content_item", "brand",
                                                   "distinguishing_mark"}
        stored = str(item["private_attributes"]).lower()
        assert "yellow" not in stored and "wildcraft" not in stored and "tiffin" not in stored
        assert db.custody_log.find_one({"event": "logged", "itemId": item["found_id"]})
        assert depot.get(f"/staff/items/{item['found_id']}/tag").status_code == 200

        # Feature 2: matcher → auto-approved → worker delivers the WhatsApp message.
        match = db.matches.find_one()
        assert match["status"] == "auto_approved"
        app_module.match_notifier.process_due_notifications("e2e")
        assert db.message_log.count_documents({"channel": "whatsapp"}) == 1
        assert passenger.get(f"/matches/{match['_id']}").get_json()["match"]["claim"]["canStart"] is True
        assert b'id="start-claim-btn"' in passenger.get(f"/passenger/matches/{match['_id']}").data

        # The photo is not reachable directly by the passenger (only staff).
        static_url = "/" + uploaded[0].replace(os.sep, "/")
        assert passenger.get(static_url).status_code == 404
        staff_view = depot.get(static_url)
        assert staff_view.status_code == 200
        staff_view.close()

        # Feature 3: "This is my item" → questions → answers in Tamil / Tanglish / English with a typo.
        resp = passenger.post("/claims", json={"matchId": str(match["_id"])})
        assert resp.status_code == 201
        claim_id = resp.get_json()["claim"]["claimId"]
        assert passenger.get(f"/passenger/claims/{claim_id}").status_code == 200
        assert passenger.get(f"/claims/{claim_id}/photo").status_code == 403
        questions = passenger.get(f"/claims/{claim_id}/questions").get_json()["questions"]
        spoken = {"zipper_tag_colour": "மஞ்சள்", "content_item": "oru steel dabba", "brand": "wildcarft",
                  "distinguishing_mark": "CSK super kings sticker"}
        resp = passenger.post(f"/claims/{claim_id}/answers",
                              json={"answers": {q["id"]: spoken[q["id"]] for q in questions}})
        assert resp.get_json()["claim"]["status"] == "token_issued"
        assert db.found_reports.find_one()["status"] == "claim_locked"

        # Photo reveal through the short-lived signed URL.
        link = passenger.get(f"/claims/{claim_id}/photo").get_json()
        photo = passenger.get(link["url"])
        assert photo.status_code == 200 and photo.data == PNG_BYTES
        photo.close()

        # Pickup token and QR.
        token = passenger.get(f"/claims/{claim_id}/token").get_json()["token"]
        assert token["depot"] == "Salem Depot" and token["qrSvg"].lstrip().startswith("<svg")

        # Staff scan the QR and the item tag, check ID, hand over.
        assert depot.get("/staff/claims-desk").status_code == 200
        check = depot.post("/pickup/verify", json={"token": token["qrPayload"], "qrTagId": item["qrTagId"]})
        assert check.get_json()["valid"] is True
        done = depot.post("/pickup/complete", json={"token": token["qrPayload"], "qrTagId": item["qrTagId"],
                                                    "idChecked": True, "staffName": "Counter 2"})
        assert done.status_code == 200

        # Custody log, item state, report state, passenger notified.
        handover = db.custody_log.find_one({"event": "handover"})
        assert handover["claimId"] == claim_id and handover["depotId"] == "9000000004"
        assert handover["recipientName"] == "Arun Kumar" and handover["staffName"] == "Counter 2"
        assert db.found_reports.find_one()["status"] == "handed_over"
        assert db.claims.find_one({"claimId": claim_id})["status"] == "completed"
        assert app.test_client().get(f"/api/status/{lost['tracking_id']}").get_json()["status"] == "resolved"
        titles = [n["title"] for n in passenger.get("/notifications").get_json()["notifications"]]
        assert any("collected" in t.lower() for t in titles)
        assert db.sms_log.find_one({"purpose": "notification", "body": {"$regex": "handed over"}})
        assert depot.post("/pickup/complete", json={"token": token["code"], "qrTagId": item["qrTagId"],
                                                    "idChecked": True}).status_code == 409
    finally:
        for path in uploaded:
            if os.path.exists(path):
                os.remove(path)


def test_legacy_depot_resolve_cannot_bypass_a_claim_lock(e2e):
    app_module, db, sms = e2e
    app = app_module.app
    passenger = _login(app.test_client(), sms, ARUN)
    _report_lost(passenger, "black backpack with laptop", _today())
    depot = app.test_client()
    depot.post("/depot/login", data={"phone": "9000000004", "password": "pass123"})
    resp = depot.post("/depot/found", data={"route_id": "R100", "trip_id": "T1", "date": _today(),
                                            "notes": "black backpack with laptop", "public_description": "Backpack",
                                            "category": "bag", "attr_brand": "Wildcraft",
                                            "attr_content_item": "tiffin"})
    assert resp.status_code == 302
    match = db.matches.find_one()
    app_module.match_notifier.process_due_notifications("e2e")
    claim_id = passenger.post("/claims", json={"matchId": str(match["_id"])}).get_json()["claim"]["claimId"]
    qs = passenger.get(f"/claims/{claim_id}/questions").get_json()["questions"]
    truth = {"brand": "Wildcraft", "content_item": "tiffin"}
    passenger.post(f"/claims/{claim_id}/answers", json={"answers": {q["id"]: truth[q["id"]] for q in qs}})
    assert db.found_reports.find_one()["status"] == "claim_locked"
    blocked = depot.post("/api/match/resolve", json={"match_id": str(match["_id"])})
    assert blocked.status_code == 409 and db.found_reports.find_one()["status"] == "claim_locked"
