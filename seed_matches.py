"""
seed_matches.py — demo data for Feature 2 (automatic match notification).

Requires the sample bookings (python seed_bookings.py). Idempotent: reports and
items are upserted by id and matches are deduplicated, so re-running changes
nothing and sends nothing twice. Uses fixed confidence scores (no ML models) and
the MOCK channels, then runs the notification worker once.

    python seed_matches.py

Demo depot login: phone 9000000101 / password demo123  → /staff/review

  TRK-DEMO0001  Arun   ↔ "Black backpack"     0.92  high   → auto-notified (WhatsApp)
  TRK-DEMO0002  Priya  ↔ "Brown leather bag"  0.88  high   → email-only passenger → email
  TRK-DEMO0003  Karthik↔ "Steel water bottle" 0.71  medium → waits in the staff review queue
  TRK-DEMO0004  Senthil↔ "Umbrella"           0.45  low    → rejected, never notified
  TRK-DEMO0005  Arun   ↔ "Grey suitcase"      0.95  notified 8 days ago → hold expired, released
  TRK-DEMO0006  Senthil┐
  TRK-DEMO0007  Ravi   ┴→ "Red shopping bag"  0.90 / 0.86  one item, two reports (conflict);
                                               Ravi has no WhatsApp/email → in-app only
"""
import os
from datetime import timedelta

from dotenv import load_dotenv
from pymongo import MongoClient

from match_notify import ensure_match_indexes
from match_notify.channels import EMAIL, WHATSAPP, MockChannel
from match_notify.config import MatchNotifyConfig
from match_notify.service import MatchNotifyService
from passenger_auth.phone import normalize_phone
from passenger_auth.timeutil import today_ist, utcnow

DEMO_DEPOT = {"phone": "9000000101", "name": "Chennai CMBT Depot (demo)", "password": "demo123",
              "stop": "Chennai", "city": "Chennai"}

# tracking id, (pnr, phone), passenger name, description
REPORTS = [
    ("TRK-DEMO0001", ("T26100101", "9876543210"), "Arun Kumar", "Black backpack with a laptop and charger"),
    ("TRK-DEMO0002", ("T26100102", "9444012345"), "Priya Raman", "Brown leather handbag, gold zip"),
    ("TRK-DEMO0003", ("T26100103", "9840011223"), "கார்த்திக் செல்வம்", "Steel water bottle with blue cap"),
    ("TRK-DEMO0004", ("T26100105", "9123456780"), "Senthil Kumar, Meena Senthil", "Folding umbrella"),
    ("TRK-DEMO0005", ("T26100101", "9876543210"), "Arun Kumar", "Grey trolley suitcase"),
    ("TRK-DEMO0006", ("T26100105", "9123456780"), "Senthil Kumar, Meena Senthil", "Red cloth shopping bag"),
    ("TRK-DEMO0007", ("T26100105", "8098765432"), "Ravi Senthil", "Red shopping bag with vegetables"),
]

# found id, public description, private notes
ITEMS = [
    ("demo-item-backpack", "Black backpack", "Black Wildcraft backpack, Dell laptop SN 7X2K9Q inside"),
    ("demo-item-handbag", "Brown leather bag", "Brown leather handbag, purse with Rs 2,300"),
    ("demo-item-bottle", "Steel water bottle", "Milton steel bottle, blue cap"),
    ("demo-item-umbrella", "Umbrella", "Black umbrella, broken spoke"),
    ("demo-item-suitcase", "Grey suitcase", "Grey VIP trolley, tag 'R. Kumar'"),
    ("demo-item-redbag", "Red shopping bag", "Red cloth bag, onions and tomatoes"),
]

# (tracking id, found id, confidence, days ago)
PAIRS = [
    ("TRK-DEMO0001", "demo-item-backpack", 0.92, 0),
    ("TRK-DEMO0002", "demo-item-handbag", 0.88, 0),
    ("TRK-DEMO0003", "demo-item-bottle", 0.71, 0),
    ("TRK-DEMO0004", "demo-item-umbrella", 0.45, 0),
    ("TRK-DEMO0005", "demo-item-suitcase", 0.95, 8),
    ("TRK-DEMO0006", "demo-item-redbag", 0.90, 0),
    ("TRK-DEMO0007", "demo-item-redbag", 0.86, 0),
]


def _score(final):
    return {"text": final, "image": 0.0, "route": 1.0, "ocr": 0.0, "structured": 0.0,
            "final": final, "is_match": True}


def seed_matches(db, now=None, config=None) -> dict:
    now = now or utcnow()
    config = config or MatchNotifyConfig()
    date = today_ist(now).isoformat()
    if not db.depots.find_one({"phone": DEMO_DEPOT["phone"]}):
        db.depots.insert_one(dict(DEMO_DEPOT))

    for tid, (pnr, phone), name, description in REPORTS:
        db.lost_reports.update_one({"tracking_id": tid}, {"$setOnInsert": {
            "request_id": f"demo-{tid.lower()}", "tracking_id": tid, "route_id": "demo-route",
            "route_name": "SETC 101 Chennai → Coimbatore", "date": date, "description": description,
            "name": name, "phone": normalize_phone(phone), "ownerPnr": pnr,
            "ownerPhone": normalize_phone(phone), "status": "pending", "created_at": now.isoformat(),
        }}, upsert=True)
    for fid, public, notes in ITEMS:
        db.found_reports.update_one({"found_id": fid}, {"$setOnInsert": {
            "found_id": fid, "id": fid[:8], "depot_phone": DEMO_DEPOT["phone"],
            "depot_name": DEMO_DEPOT["name"], "route_id": "demo-route",
            "route_name": "SETC 101 Chennai → Coimbatore", "date": date, "notes": notes,
            "public_description": public, "status": "open", "created_at": now.isoformat(),
        }}, upsert=True)

    for tid, fid, confidence, days_ago in PAIRS:
        at = now - timedelta(days=days_ago)
        channels = {WHATSAPP: MockChannel(WHATSAPP, db, clock=lambda: at),
                    EMAIL: MockChannel(EMAIL, db, clock=lambda: at)}
        svc = MatchNotifyService(db, config, channels, clock=lambda: at)
        svc.record_match(db.found_reports.find_one({"found_id": fid}),
                         db.lost_reports.find_one({"tracking_id": tid}), _score(confidence))
        svc.process_due_notifications("seed")

    channels = {WHATSAPP: MockChannel(WHATSAPP, db), EMAIL: MockChannel(EMAIL, db)}
    released = MatchNotifyService(db, config, channels, clock=lambda: now).release_expired_matches()
    return {"reports": len(REPORTS), "items": len(ITEMS), "pairs": len(PAIRS), "released": released}


def main():
    load_dotenv()
    uri = os.environ.get("MONGO_URI")
    if not uri:
        print("MONGO_URI not found in environment")
        return
    db = MongoClient(uri)["tn_bus_lost_found"]
    ensure_match_indexes(db)
    print(seed_matches(db, config=MatchNotifyConfig.from_env()))


if __name__ == "__main__":
    main()
