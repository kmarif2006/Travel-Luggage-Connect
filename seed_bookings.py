"""
seed_bookings.py — mock TNSTC PNR bookings for passenger OTP login (Feature 1).

Idempotent: each passenger row is upserted on (pnr, passengerSeq), so re-running
refreshes the sample data without duplicating it. Travel dates are relative to
today (IST) so the sample PNRs stay inside the Lost & Found access window.

    python seed_bookings.py

Sample PNRs (all phones are fictitious):
    T26100101  Arun Kumar          +91 98765 43210  confirmed, 2 days ago
    T26100102  Priya Raman         +91 94440 12345  confirmed, yesterday
    T26100103  கார்த்திக் செல்வம்     +91 98400 11223  confirmed, today (Unicode name)
    T26100104  Lakshmi Narayanan   +91 90033 12345  CANCELLED  → never gets an OTP
    T26100105  Senthil Kumar       +91 91234 56780  confirmed, multi-passenger PNR:
               Meena Senthil       +91 91234 56780    (same phone as Senthil)
               Ravi Senthil        +91 80987 65432    (own phone)
    T26100106  Kavitha Mani        +91 70102 03040  completed, 120 days ago → outside window

Contact preferences (Feature 2): Arun has WhatsApp + email, Priya has email only,
Ravi Senthil has neither (in-app notifications only), everyone else WhatsApp only.
"""
import os
from datetime import timedelta

from dotenv import load_dotenv
from pymongo import MongoClient

from passenger_auth import ensure_auth_indexes
from passenger_auth.bookings import BOOKINGS
from passenger_auth.phone import normalize_phone
from passenger_auth.timeutil import to_db, today_ist, utcnow

# (pnr, seq, name, phone as typed by the booking system, status, route, days ago)
SAMPLE_BOOKINGS = [
    ("T26100101", 1, "Arun Kumar",          "9876543210",       "confirmed", "SETC 101 Chennai → Coimbatore",   2),
    ("T26100102", 1, "Priya Raman",         "+91 94440 12345",  "confirmed", "SETC 133 Chennai → Madurai",      1),
    ("T26100103", 1, "கார்த்திக் செல்வம்",  "098400-11223",     "confirmed", "SETC 180 Madurai → Tirunelveli",  0),
    ("T26100104", 1, "Lakshmi Narayanan",   "9003312345",       "cancelled", "SETC 101 Chennai → Coimbatore",   3),
    ("T26100105", 1, "Senthil Kumar",       "9123456780",       "confirmed", "SETC 122 Chennai → Trichy",       1),
    ("T26100105", 2, "Meena Senthil",       "91 91234 56780",   "confirmed", "SETC 122 Chennai → Trichy",       1),
    ("T26100105", 3, "Ravi Senthil",        "8098765432",       "confirmed", "SETC 122 Chennai → Trichy",       1),
    ("T26100106", 1, "Kavitha Mani",        "7010203040",       "completed", "SETC 101 Chennai → Coimbatore", 120),
]

# Contact preferences used by Feature 2 notifications: (pnr, seq) → (on WhatsApp?, email).
# Unlisted passengers: registered phone is on WhatsApp, no email.
CONTACTS = {
    ("T26100101", 1): (True, "arun.kumar@example.com"),
    ("T26100102", 1): (False, "priya.raman@example.com"),    # email only
    ("T26100105", 3): (False, None),                        # no WhatsApp, no email → in-app only
}

BOOKINGS_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["pnr", "passengerSeq", "passengerName", "registeredPhone", "status", "travelDate"],
        "properties": {
            "pnr":             {"bsonType": "string"},
            "passengerSeq":    {"bsonType": ["int", "long"]},
            "passengerName":   {"bsonType": "string"},
            "registeredPhone": {"bsonType": "string"},
            "status":          {"enum": ["confirmed", "completed", "cancelled"]},
            "route":           {"bsonType": "string"},
            "travelDate":      {"bsonType": "string"},
            "whatsapp":        {"bsonType": "bool"},
            "email":           {"bsonType": ["string", "null"]},
        },
    }
}


def seed_bookings(db, now=None) -> int:
    """Upsert the sample bookings into `db`. Returns the number of rows written."""
    now = now or utcnow()
    today = today_ist(now)
    for pnr, seq, name, phone, status, route, days_ago in SAMPLE_BOOKINGS:
        whatsapp, email = CONTACTS.get((pnr, seq), (True, None))
        db[BOOKINGS].update_one(
            {"pnr": pnr, "passengerSeq": seq},
            {"$set": {
                "passengerName":   name,
                "registeredPhone": normalize_phone(phone),
                "status":          status,
                "route":           route,
                "travelDate":      (today - timedelta(days=days_ago)).isoformat(),
                "whatsapp":        whatsapp,
                "email":           email,
                "updatedAt":       to_db(now),
            }, "$setOnInsert": {"createdAt": to_db(now)}},
            upsert=True,
        )
    return len(SAMPLE_BOOKINGS)


def apply_bookings_validator(db) -> None:
    """Lenient validator (moderate level) so the collection behaves like a table."""
    if BOOKINGS in db.list_collection_names():
        db.command("collMod", BOOKINGS, validator=BOOKINGS_VALIDATOR, validationLevel="moderate")
    else:
        db.create_collection(BOOKINGS, validator=BOOKINGS_VALIDATOR, validationLevel="moderate")


def main():
    load_dotenv()
    uri = os.environ.get("MONGO_URI")
    if not uri:
        print("MONGO_URI not found in environment")
        return
    db = MongoClient(uri)["tn_bus_lost_found"]
    apply_bookings_validator(db)
    ensure_auth_indexes(db)
    n = seed_bookings(db)
    print(f"Seeded {n} booking rows across {len({b[0] for b in SAMPLE_BOOKINGS})} PNRs.")


if __name__ == "__main__":
    main()
