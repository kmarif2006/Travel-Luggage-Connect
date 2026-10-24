"""
End-to-end: the REAL app.py routes and matcher, with only the ML models stubbed.

lost report (POST /lost, Feature 1 session) → found item (POST /depot/found) →
existing matcher (compute_and_save_matches) → Feature 2 routing → worker sends
the mock WhatsApp → passenger logs in → views the privacy-safe match.

app.py connects to MongoDB and loads SBERT/CLIP at import, so the import is done
once per module with pymongo swapped for mongomock, the similarity models
replaced by deterministic fakes, and every outbound provider forced to mock.
"""
import os
import sys
from datetime import datetime

import mongomock
import pymongo
import pytest

from conftest import ARUN, PRIYA, RecordingSmsProvider
from passenger_auth.timeutil import IST

ENV = {
    "MONGO_URI": "mongodb://localhost:27017", "SECRET_KEY": "e2e-secret",
    "SIGNING_SECRET": "e2e-signing-secret-0123456789-abcdefghijklmnop",
    "SMS_PROVIDER": "mock", "WHATSAPP_PROVIDER": "mock", "EMAIL_PROVIDER": "mock",
    "MOCK_WHATSAPP_FAILURE": "none", "MOCK_EMAIL_FAILURE": "none",
    "NOTIFY_WORKER_ENABLED": "0", "APP_BASE_URL": "https://lostfound.tnstc.example",
    "TWILIO_ACCOUNT_SID": "", "TWILIO_AUTH_TOKEN": "", "TWILIO_PHONE_NUMBER": "",
    "OCR_API_KEY": "", "GROQ_API_KEY": "", "FLASK_DEBUG": "0",
}


class FakeTextSimilarity:
    """Jaccard word overlap: deterministic and model-free."""

    def similarity(self, a, b):
        wa, wb = set((a or "").lower().split()), set((b or "").lower().split())
        return len(wa & wb) / len(wa | wb) if wa and wb else 0.0


class FakeImageSimilarity:
    available = False

    def __init__(self, db=None):
        pass

    def similarity(self, a, b):
        return 0.0

    def embed(self, path):
        return None


class FakeOCR:
    def extract_text(self, path):
        return ""


@pytest.fixture(scope="module")
def real_app():
    saved_env = {k: os.environ.get(k) for k in ENV}
    os.environ.update(ENV)
    import similarity
    saved = (pymongo.MongoClient, similarity.TextSimilarity, similarity.ImageSimilarity, similarity.OCRExtractor)
    pymongo.MongoClient = mongomock.MongoClient
    similarity.TextSimilarity = lambda *a, **k: FakeTextSimilarity()
    similarity.ImageSimilarity = FakeImageSimilarity
    similarity.OCRExtractor = FakeOCR
    sys.modules.pop("app", None)
    try:
        import app as app_module
    finally:
        pymongo.MongoClient, similarity.TextSimilarity, similarity.ImageSimilarity, similarity.OCRExtractor = saved
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    yield app_module
    sys.modules.pop("app", None)
    for k, v in saved_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.fixture
def e2e(real_app):
    db = real_app.db
    for name in db.list_collection_names():
        db[name].delete_many({})
    from seed_bookings import seed_bookings
    seed_bookings(db)
    # Transport master: one route Chennai → Villupuram → Salem, one trip/bus.
    db.stops.insert_many([
        {"stop_id": "S1", "name": "Chennai CMBT", "city": "Chennai", "lat": 13.07, "lon": 80.2},
        {"stop_id": "S2", "name": "Villupuram", "city": "Villupuram", "lat": 11.9, "lon": 79.5},
        {"stop_id": "S3", "name": "Salem", "city": "Salem", "lat": 11.6, "lon": 78.1},
    ])
    db.routes.insert_one({"route_id": "R100", "route_no": "100", "origin_name": "Chennai", "dest_name": "Salem",
                          "origin_stop_id": "S1", "dest_stop_id": "S3", "stop_sequence": ["S1", "S2", "S3"],
                          "stop_cities": ["chennai", "villupuram", "salem"], "operating_depot_code": "SLM"})
    db.buses.insert_one({"bus_id": "B1", "registration_no": "TN 30 N 1234", "depot_id": "D1", "capacity": 40})
    db.trips.insert_one({"trip_id": "T1", "route_id": "R100", "bus_id": "B1", "departure_time": "21:00"})
    db.depots.insert_one({"phone": "9000000004", "name": "Salem Depot", "password": "pass123",
                          "stop": "Salem", "city": "Salem", "depot_code": "SLM"})
    sms = RecordingSmsProvider()
    otp_service = real_app.app.extensions["passenger_auth"].otp
    original_sms = otp_service.sms
    otp_service.sms = sms
    yield real_app, db, sms
    otp_service.sms = original_sms


def _login(client, sms, ident):
    assert client.post("/auth/otp/request", json=ident).status_code == 202
    resp = client.post("/auth/otp/verify", json={**ident, "otp": sms.last_otp()})
    assert resp.status_code == 200
    return client


def _report_lost(client, description, date):
    return client.post("/lost", data={
        "route_id": "R100", "trip_id": "T1", "seat_no": "12", "boarding_stop_id": "S1",
        "alighting_stop_id": "S2", "date": date, "description": description,
    })


def _log_found(depot_client, notes, public, date, trip="T1"):
    return depot_client.post("/depot/found", data={
        "route_id": "R100", "trip_id": trip, "date": date, "notes": notes, "public_description": public,
    })


def _today():
    return datetime.now(IST).date().isoformat()


def test_end_to_end_lost_then_found_then_notified_then_viewed(e2e):
    app_module, db, sms = e2e
    flask_app = app_module.app

    # 1. Passenger verifies with OTP (Feature 1) and files a lost report.
    passenger = _login(flask_app.test_client(), sms, ARUN)
    resp = _report_lost(passenger, "black backpack with laptop", _today())
    assert resp.status_code == 302
    lost = db.lost_reports.find_one()
    assert lost["ownerPnr"] == "T26100101" and lost["ownerPhone"] == "+919876543210"
    assert lost["name"] == "Arun Kumar"                       # from the verified session, not the form

    # 2. Depot staff log the found item; the existing matcher runs synchronously.
    depot = flask_app.test_client()
    assert depot.post("/depot/login", data={"phone": "9000000004", "password": "pass123"}).status_code == 302
    assert _log_found(depot, "black backpack with laptop", "Black backpack", _today()).status_code == 302
    match = db.matches.find_one()
    assert match["reportId"] == lost["request_id"] and match["confidence"] == 1.0   # same trip = proof
    assert match["status"] == "auto_approved"
    assert db.message_log.count_documents({}) == 0             # the request path never sends

    # 3. Background worker delivers the mock WhatsApp.
    assert app_module.match_notifier.process_due_notifications("e2e") == 1
    sent = db.message_log.find_one({"channel": "whatsapp"})
    assert sent["to"] == "+91 ******3210" and "[MATCH FOUND]" in sent["subject"]
    assert lost["tracking_id"] in sent["subject"]
    assert "laptop" not in sent["body"]                       # private notes never leave
    assert db.matches.find_one()["status"] == "notified"

    # 4. Passenger follows the deep link, which requires the Feature 1 session.
    link = f"/passenger/reports/{lost['tracking_id']}"
    assert f"https://lostfound.tnstc.example{link}" in sent["body"]
    assert flask_app.test_client().get(link).status_code == 302
    page = passenger.get(link)
    assert page.status_code == 200 and b"Black backpack" in page.data
    view = passenger.get(f"/matches/{match['_id']}").get_json()["match"]
    assert view["state"] == "active" and view["foundAt"] == "Salem Depot" and view["confidencePercent"] == 100
    assert passenger.get(f"/passenger/matches/{match['_id']}").status_code == 200
    assert len(passenger.get("/notifications").get_json()["notifications"]) == 1

    # 5. Depot dashboard still lists the match with its new status.
    assert b"notified" in depot.get("/depot").data


def test_found_first_then_late_lost_report_still_matches(e2e):
    app_module, db, sms = e2e
    flask_app = app_module.app
    depot = flask_app.test_client()
    depot.post("/depot/login", data={"phone": "9000000004", "password": "pass123"})
    _log_found(depot, "blue steel water bottle", "Steel bottle", _today())
    passenger = _login(flask_app.test_client(), sms, PRIYA)
    _report_lost(passenger, "blue steel water bottle", _today())
    match = db.matches.find_one()
    assert match and match["status"] == "auto_approved"
    app_module.match_notifier.process_due_notifications("e2e")
    assert db.message_log.find_one({"channel": "email"})["to"] == "p***@example.com"   # Priya: email only


def test_review_band_match_waits_for_staff_approval(e2e):
    app_module, db, sms = e2e
    flask_app = app_module.app
    passenger = _login(flask_app.test_client(), sms, ARUN)
    _report_lost(passenger, "black leather wallet brown strap", _today())
    depot = flask_app.test_client()
    depot.post("/depot/login", data={"phone": "9000000004", "password": "pass123"})
    # No trip given → no structured boost; partial word overlap lands in the review band.
    resp = depot.post("/depot/found", data={"route_id": "R100", "date": _today(),
                                            "notes": "black leather wallet brown", "public_description": "Wallet"})
    assert resp.status_code == 302
    match = db.matches.find_one()
    assert 0.60 <= match["confidence"] < 0.85 and match["status"] == "pending_staff_review"
    app_module.match_notifier.process_due_notifications("e2e")
    assert db.message_log.count_documents({}) == 0
    assert passenger.get(f"/matches/{match['_id']}").status_code == 404
    queue = depot.get("/staff/matches?status=staff_review").get_json()["matches"]
    assert len(queue) == 1
    assert depot.post(f"/staff/matches/{match['_id']}/approve").status_code == 200
    app_module.match_notifier.process_due_notifications("e2e")
    assert db.message_log.count_documents({"channel": "whatsapp"}) == 1
    assert passenger.get(f"/matches/{match['_id']}").status_code == 200


def test_lost_report_requires_passenger_login(e2e):
    app_module, db, _ = e2e
    resp = app_module.app.test_client().post("/lost", data={"route_id": "R100"})
    assert resp.status_code == 302 and "/passenger/login" in resp.headers["Location"]
    assert db.lost_reports.count_documents({}) == 0


def test_found_item_requires_public_description(e2e):
    app_module, db, _ = e2e
    depot = app_module.app.test_client()
    depot.post("/depot/login", data={"phone": "9000000004", "password": "pass123"})
    resp = depot.post("/depot/found", data={"route_id": "R100", "date": _today(), "notes": "bag"})
    assert resp.status_code == 302 and db.found_reports.count_documents({}) == 0


def test_depot_resolve_reserves_item_and_rejects_inactive(e2e):
    app_module, db, sms = e2e
    flask_app = app_module.app
    passenger = _login(flask_app.test_client(), sms, ARUN)
    _report_lost(passenger, "red umbrella", _today())
    depot = flask_app.test_client()
    depot.post("/depot/login", data={"phone": "9000000004", "password": "pass123"})
    _log_found(depot, "red umbrella", "Umbrella", _today())
    match = db.matches.find_one()
    resp = depot.post("/api/match/resolve", json={"match_id": str(match["_id"])})
    assert resp.status_code == 200
    assert db.found_reports.find_one()["status"] == "resolved"
    assert depot.post("/api/match/resolve", json={"match_id": str(match["_id"])}).status_code == 409
