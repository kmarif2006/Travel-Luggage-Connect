"""
seed_claims.py — demo data for Feature 3 (secure claims and QR pickup tokens).

Run after seed_bookings.py and seed_matches.py. Idempotent: attributes and tags are
only added to demo items that lack them, and claims are created only once per
scenario. The private attributes and pickup codes are keyed with SIGNING_SECRET,
so it MUST be the same secret the app runs with (the script refuses to run
without one).

    python seed_claims.py

Scenarios (secret answers are listed in docs/DEMO.md):
  TRK-DEMO0001  Arun   · Black backpack   → claim approved, pickup code ready (demo the counter)
  TRK-DEMO0002  Priya  · Brown leather bag → notified, no claim yet (demo "This is my item" live)
  TRK-DEMO0006  Senthil┐ Red shopping bag → Senthil's claim holds the item (token issued);
  TRK-DEMO0007  Ravi   ┘                    Ravi's match shows "unavailable" (multi-claim conflict)
  TRK-DEMO0008  Karthik· Phone            → borderline answers → waiting in the staff review queue
  TRK-DEMO0009  Arun   · Laptop           → token issued 9 days ago → expired, item released
"""
import os
from datetime import timedelta

from dotenv import load_dotenv
from pymongo import MongoClient

from claims import ensure_claim_indexes
from claims.attributes import encode_private_attributes
from claims.config import ClaimsConfig
from claims.service import ClaimsService
from claims.tokens import new_tag_id
from match_notify import ensure_match_indexes
from match_notify.channels import EMAIL, WHATSAPP, MockChannel
from match_notify.config import MatchNotifyConfig
from match_notify.service import MatchNotifyService
from passenger_auth.phone import normalize_phone
from passenger_auth.ratelimit import RateLimiter
from passenger_auth.sessions import Passenger
from passenger_auth.timeutil import today_ist, utcnow
from seed_matches import DEMO_DEPOT

# found id → (category, public description, secret attributes)
SECRETS = {
    "demo-item-backpack": ("bag", "Black backpack", {
        "zipper_tag_colour": "yellow", "content_item": "steel tiffin box", "brand": "Wildcraft",
        "distinguishing_mark": "Chennai Super Kings sticker"}),
    "demo-item-handbag": ("bag", "Brown leather bag", {
        "zipper_tag_colour": "red", "content_item": "blue umbrella", "brand": "Lavie",
        "distinguishing_mark": "torn left strap"}),
    "demo-item-redbag": ("bag", "Red shopping bag", {
        "zipper_tag_colour": "green", "content_item": "onions", "distinguishing_mark": "Saravana Stores print"}),
    "demo-item-phone": ("phone", "Mobile phone", {
        "lock_screen": "Murugan temple", "case_colour": "black", "brand": "Redmi",
        "distinguishing_mark": "cracked top corner"}),
    "demo-item-laptop": ("laptop", "Laptop", {
        "lock_screen": "family photo at the beach", "case_colour": "grey", "brand": "Lenovo",
        "distinguishing_mark": "ISRO sticker"}),
}
PHOTO_COLOURS = {"demo-item-backpack": (30, 30, 30), "demo-item-handbag": (120, 72, 40),
                 "demo-item-redbag": (190, 30, 40), "demo-item-phone": (60, 60, 70), "demo-item-laptop": (150, 150, 150)}

NEW_REPORTS = [  # tracking id, (pnr, phone), name, description, found id, confidence, days ago
    ("TRK-DEMO0008", ("T26100103", "9840011223"), "கார்த்திக் செல்வம்", "Black Redmi phone", "demo-item-phone", 0.90, 0),
    ("TRK-DEMO0009", ("T26100101", "9876543210"), "Arun Kumar", "Grey Lenovo laptop", "demo-item-laptop", 0.93, 10),
]


def _write_photo(upload_dir, found_id):
    from PIL import Image, ImageDraw
    os.makedirs(upload_dir, exist_ok=True)
    path = os.path.join(upload_dir, f"{found_id}.png")
    if not os.path.exists(path):
        img = Image.new("RGB", (320, 240), PHOTO_COLOURS[found_id])
        ImageDraw.Draw(img).text((12, 12), f"DEMO PHOTO\n{SECRETS[found_id][1]}", fill=(255, 255, 255))
        img.save(path)
    return f"uploads/{found_id}.png"


def _services(db, secret, at, config):
    clock = lambda: at                                                  # noqa: E731
    channels = {WHATSAPP: MockChannel(WHATSAPP, db, clock=clock), EMAIL: MockChannel(EMAIL, db, clock=clock)}
    match_svc = MatchNotifyService(db, MatchNotifyConfig(), channels, clock)
    return match_svc, ClaimsService(db, config, secret, clock, RateLimiter(db, clock), match_svc)


def _passenger(lost, name):
    return Passenger(lost["ownerPnr"], lost["ownerPhone"], name, "seed", None)


def _answer(claims_svc, claim, truth, passenger, wrong=()):
    answers = {q: ("purple elephant" if q in wrong else truth[q]) for q in claim["pendingQuestions"]}
    return claims_svc.submit_answers(claim, answers, passenger)


def seed_claims(db, signing_secret, now=None, upload_dir=os.path.join("static", "uploads"), config=None):
    now = now or utcnow()
    config = config or ClaimsConfig()
    _, svc_now = _services(db, signing_secret, now, config)
    date = today_ist(now).isoformat()

    for fid, (category, public, truth) in SECRETS.items():
        if not db.found_reports.find_one({"found_id": fid}):
            db.found_reports.insert_one({
                "found_id": fid, "id": fid[:8], "depot_phone": DEMO_DEPOT["phone"], "depot_name": DEMO_DEPOT["name"],
                "route_id": "demo-route", "route_name": "SETC 101 Chennai → Coimbatore", "date": date,
                "notes": f"{public} (demo)", "public_description": public, "status": "open",
                "created_at": now.isoformat()})
        db.found_reports.update_one({"found_id": fid, "private_attributes": {"$exists": False}}, {"$set": {
            "category": category, "qrTagId": new_tag_id(), "image_path": _write_photo(upload_dir, fid),
            "private_attributes": encode_private_attributes(truth, category, public, svc_now.answer_key,
                                                            config.fuzzy_min_token_length)}})
        if not db.custody_log.find_one({"itemId": fid, "event": "logged"}):
            svc_now.record_item_logged(db.found_reports.find_one({"found_id": fid}), DEMO_DEPOT["phone"])

    for tid, (pnr, phone), name, description, fid, confidence, days_ago in NEW_REPORTS:
        at = now - timedelta(days=days_ago)
        match_svc, _ = _services(db, signing_secret, at, config)
        db.lost_reports.update_one({"tracking_id": tid}, {"$setOnInsert": {
            "request_id": f"demo-{tid.lower()}", "tracking_id": tid, "route_id": "demo-route",
            "route_name": "SETC 101 Chennai → Coimbatore", "date": date, "description": description, "name": name,
            "phone": normalize_phone(phone), "ownerPnr": pnr, "ownerPhone": normalize_phone(phone),
            "status": "pending", "created_at": at.isoformat()}}, upsert=True)
        score = {"text": confidence, "image": 0.0, "route": 1.0, "ocr": 0.0, "structured": 0.0,
                 "final": confidence, "is_match": True}
        match_svc.record_match(db.found_reports.find_one({"found_id": fid}),
                               db.lost_reports.find_one({"tracking_id": tid}), score)
        match_svc.process_due_notifications("seed")

    def scenario(tid, fid, at, play):
        lost = db.lost_reports.find_one({"tracking_id": tid})
        match = db.matches.find_one({"reportId": lost["request_id"], "itemId": fid})
        if not match or db.claims.find_one({"matchId": str(match["_id"])}):
            return None
        _, svc = _services(db, signing_secret, at, config)
        passenger = _passenger(lost, lost["name"])
        claim = svc.open_claim(passenger, str(match["_id"]))
        return play(svc, claim, passenger)

    truth = lambda fid: SECRETS[fid][2]                                 # noqa: E731
    results = {
        "ready_for_pickup": scenario("TRK-DEMO0001", "demo-item-backpack", now,
                                     lambda s, c, p: _answer(s, c, truth("demo-item-backpack"), p)),
        "conflict_winner": scenario("TRK-DEMO0006", "demo-item-redbag", now,
                                    lambda s, c, p: _answer(s, c, truth("demo-item-redbag"), p)),
    }

    def borderline(svc, claim, passenger):
        db.claims.update_one({"_id": claim["_id"]}, {"$set": {
            "questionSet": ["lock_screen", "case_colour", "brand"],
            "pendingQuestions": ["lock_screen", "case_colour", "brand"], "reserveQuestion": "distinguishing_mark"}})
        claim = db.claims.find_one({"_id": claim["_id"]})
        claim = _answer(svc, claim, truth("demo-item-phone"), passenger, wrong={"case_colour"})  # 2.5/3.5 → follow-up
        return _answer(svc, claim, truth("demo-item-phone"), passenger, wrong={"distinguishing_mark"})  # 2.5/5 → staff

    results["borderline_staff_review"] = scenario("TRK-DEMO0008", "demo-item-phone", now, borderline)
    results["expired_token"] = scenario("TRK-DEMO0009", "demo-item-laptop", now - timedelta(days=9),
                                        lambda s, c, p: _answer(s, c, truth("demo-item-laptop"), p))
    expired = svc_now.expire_due_claims()
    return {k: (v or {}).get("status", "already seeded") for k, v in results.items()} | {"expired_now": expired}


def main():
    load_dotenv()
    uri, secret = os.environ.get("MONGO_URI"), (os.environ.get("SIGNING_SECRET") or "").strip()
    if not uri:
        print("MONGO_URI not found in environment")
        return
    if len(secret) < 32:
        print("Set SIGNING_SECRET (>= 32 chars) in .env first: claim answers and pickup codes are keyed with it.")
        return
    db = MongoClient(uri)["tn_bus_lost_found"]
    ensure_match_indexes(db)
    ensure_claim_indexes(db)
    print(seed_claims(db, secret, config=ClaimsConfig.from_env()))


if __name__ == "__main__":
    main()
