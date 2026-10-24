"""
Shared fixtures for the passenger OTP authentication tests.

The suite never touches the real database or app.py (which loads ML models):
it builds a minimal Flask app with the passenger_auth blueprint on top of
mongomock, a controllable clock and an in-memory SMS provider that captures the
raw OTP the way a passenger's phone would receive it.
"""
import functools
import os
import sys
import threading
from datetime import datetime, timedelta, timezone

import mongomock
import pytest
from flask import Flask
from flask_wtf.csrf import CSRFProtect

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from passenger_auth import init_passenger_auth                       # noqa: E402
from passenger_auth.config import AuthConfig                          # noqa: E402
from passenger_auth.sms import SmsProvider                            # noqa: E402
from seed_bookings import seed_bookings                               # noqa: E402

TEST_SECRET = "test-signing-secret-0123456789-abcdefghijklmnopqrstuvwxyz"

# Seeded sample identities (see seed_bookings.SAMPLE_BOOKINGS).
ARUN = {"pnr": "T26100101", "phone": "9876543210"}
PRIYA = {"pnr": "T26100102", "phone": "9444012345"}
KARTHIK = {"pnr": "T26100103", "phone": "9840011223"}
CANCELLED = {"pnr": "T26100104", "phone": "9003312345"}
MULTI_SHARED = {"pnr": "T26100105", "phone": "9123456780"}
MULTI_RAVI = {"pnr": "T26100105", "phone": "8098765432"}
OLD_JOURNEY = {"pnr": "T26100106", "phone": "7010203040"}


class FakeClock:
    """Starts at the real current time (mongomock's TTL purge uses the wall clock)."""

    def __init__(self):
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            return self.now

    def advance(self, seconds):
        with self._lock:
            self.now += timedelta(seconds=seconds)


class RecordingSmsProvider(SmsProvider):
    """Captures every message, i.e. what the registered handset would receive."""
    name = "recording"

    def __init__(self):
        self.sent = []
        self._lock = threading.Lock()

    def _deliver(self, to_phone, body, redacted, purpose):
        with self._lock:
            self.sent.append({"to": to_phone, "body": body, "purpose": purpose})

    def last_otp(self, to_phone=None):
        for msg in reversed(self.sent):
            if msg["purpose"] == "otp" and (to_phone is None or msg["to"] == to_phone):
                return msg["body"].split(" ", 1)[0]
        return None


_ATOMIC_METHODS = ("find_one_and_update", "update_one", "update_many", "insert_one",
                   "find_one", "count_documents", "delete_many")


@pytest.fixture
def atomic_mongomock():
    """
    mongomock is not thread-safe, while real MongoDB guarantees that each single
    operation is atomic. Serializing each collection operation reproduces exactly
    that guarantee (and no more), so concurrency tests are meaningful.
    """
    lock = threading.RLock()
    originals = {name: getattr(mongomock.collection.Collection, name) for name in _ATOMIC_METHODS}

    def wrap(fn):
        @functools.wraps(fn)
        def locked(*args, **kwargs):
            with lock:
                return fn(*args, **kwargs)
        return locked

    for name, fn in originals.items():
        setattr(mongomock.collection.Collection, name, wrap(fn))
    yield
    for name, fn in originals.items():
        setattr(mongomock.collection.Collection, name, fn)


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def db(clock, atomic_mongomock):
    database = mongomock.MongoClient()["tn_bus_lost_found_test"]
    seed_bookings(database, now=clock())
    return database


@pytest.fixture
def sms():
    return RecordingSmsProvider()


def make_config(**overrides):
    base = dict(signing_secret=TEST_SECRET, otp_ip_request_limit=1000, otp_ip_verify_limit=1000)
    base.update(overrides)
    return AuthConfig(**base)


def build_app(db, clock, sms, config=None, csrf=False):
    templates = os.path.join(ROOT, "templates")
    app = Flask(__name__, template_folder=templates)
    app.config.update(SECRET_KEY="flask-test-key", TESTING=True, WTF_CSRF_ENABLED=csrf)
    CSRFProtect(app)

    # Endpoints that base.html links to; the real ones live in app.py.
    for rule, endpoint in (("/", "index"), ("/status", "status_page"),
                           ("/depot-login", "depot_login_page"), ("/depot/logout", "depot_logout")):
        app.add_url_rule(rule, endpoint, lambda: "stub")

    init_passenger_auth(app, db, config=config or make_config(), sms_provider=sms, clock=clock)
    return app


@pytest.fixture
def app(db, clock, sms):
    return build_app(db, clock, sms)


@pytest.fixture
def client(app):
    return app.test_client()


class AuthFlow:
    """Small helper so tests read as the passenger journey."""

    def __init__(self, client, sms, clock):
        self.client, self.sms, self.clock = client, sms, clock

    def request(self, pnr, phone, **kw):
        return self.client.post("/auth/otp/request", json={"pnr": pnr, "phone": phone}, **kw)

    def verify(self, pnr, phone, otp, **kw):
        return self.client.post("/auth/otp/verify", json={"pnr": pnr, "phone": phone, "otp": otp}, **kw)

    def login(self, ident):
        assert self.request(ident["pnr"], ident["phone"]).status_code == 202
        otp = self.sms.last_otp()
        resp = self.verify(ident["pnr"], ident["phone"], otp)
        assert resp.status_code == 200, resp.get_json()
        return resp


@pytest.fixture
def flow(client, sms, clock):
    return AuthFlow(client, sms, clock)


def wrong_otp(correct: str) -> str:
    return f"{(int(correct) + 1) % 1_000_000:06d}"


# ── Feature 2 (match notification) fixtures ──────────────────────────────────
from match_notify import init_match_notify                              # noqa: E402
from match_notify.channels import EMAIL, WHATSAPP, ChannelError, MessageChannel  # noqa: E402
from match_notify.config import MatchNotifyConfig                       # noqa: E402
from passenger_auth.phone import normalize_phone                        # noqa: E402

DEPOT_A = {"phone": "9000000101", "name": "Chennai CMBT Depot", "password": "x", "city": "Chennai"}
DEPOT_B = {"phone": "9000000202", "name": "Madurai Mattuthavani Depot", "password": "x", "city": "Madurai"}


class RecordingChannel(MessageChannel):
    """Records deliveries; `plan` scripts outcomes per call: 'ok' | 'transient' | 'permanent'."""

    def __init__(self, channel):
        self.channel = channel
        self.name = "recording"
        self.sent = []
        self.calls = 0
        self.plan = []
        self.default = "ok"
        self._lock = threading.Lock()

    def send(self, to, message):
        with self._lock:
            self.calls += 1
            outcome = self.plan.pop(0) if self.plan else self.default
        if outcome == "transient":
            raise ChannelError("simulated provider outage")
        if outcome == "permanent":
            raise ChannelError("simulated invalid recipient", permanent=True)
        with self._lock:
            self.sent.append({"to": to, "subject": message.subject, "text": message.text,
                              "html": message.html})
        return f"ref-{len(self.sent)}"


def make_f2_config(**overrides):
    base = dict(worker_enabled=False, app_base_url="https://lostfound.tnstc.example")
    base.update(overrides)
    return MatchNotifyConfig(**base)


def score_of(final):
    return {"text": final, "image": 0.0, "route": 1.0, "ocr": 0.0, "structured": 0.0,
            "final": final, "is_match": final >= 0.30}


class FakeMatcher:
    """Stands in for app.py's matcher in the staff re-run / report-update paths."""

    def __init__(self, db):
        self.db = db
        self.scores = {}
        self.service = None

    def rematch_lost(self, lost):
        for found in self.db.found_reports.find():
            final = self.scores.get((lost["request_id"], found["found_id"]))
            if final is not None:
                self.service.record_match(found, lost, score_of(final))

    def rematch_found(self, found):
        for lost in self.db.lost_reports.find({"status": {"$ne": "resolved"}}):
            final = self.scores.get((lost["request_id"], found["found_id"]))
            if final is not None:
                self.service.record_match(found, lost, score_of(final))


class F2:
    """Everything a Feature 2 test needs, with readable helpers."""

    def __init__(self, db, clock, sms, config=None, csrf=False):
        self.db, self.clock, self.sms = db, clock, sms
        self.channels = {WHATSAPP: RecordingChannel(WHATSAPP), EMAIL: RecordingChannel(EMAIL)}
        self.matcher = FakeMatcher(db)
        self.app = build_app(db, clock, sms, csrf=csrf)
        self.app.add_url_rule("/depot", "depot_dashboard", lambda: "stub")
        self.state = init_match_notify(self.app, db, config=config or make_f2_config(),
                                       channels=self.channels, clock=clock,
                                       rematch_lost=self.matcher.rematch_lost,
                                       rematch_found=self.matcher.rematch_found)
        self.svc = self.state.service
        self.matcher.service = self.svc
        for depot in (DEPOT_A, DEPOT_B):
            db.depots.insert_one(dict(depot))
        self.client = self.app.test_client()

    # data
    def lost(self, ident, tracking="TRK-AAAA0001", description="Black backpack", owned=True, **extra):
        doc = {"request_id": f"req-{tracking}", "tracking_id": tracking, "route_id": "r1",
               "route_name": "Chennai → Coimbatore", "date": "2026-10-20", "description": description,
               "name": "Arun Kumar", "phone": normalize_phone(ident["phone"]), "status": "pending",
               "created_at": "2026-10-20T10:00:00"}
        if owned:
            doc.update(ownerPnr=ident["pnr"], ownerPhone=normalize_phone(ident["phone"]))
        doc.update(extra)
        self.db.lost_reports.insert_one(doc)
        return self.db.lost_reports.find_one({"tracking_id": tracking})

    def found(self, found_id="item-1", depot=DEPOT_A, public="Black backpack",
              notes="Black backpack, Dell laptop SN 7X2K9Q", date="2026-10-21", **extra):
        doc = {"found_id": found_id, "depot_phone": depot["phone"], "depot_name": depot["name"],
               "route_id": "r1", "date": date, "notes": notes, "public_description": public,
               "status": "open", "created_at": "2026-10-21T09:00:00"}
        doc.update(extra)
        self.db.found_reports.insert_one(doc)
        return self.db.found_reports.find_one({"found_id": found_id})

    def match(self, lost, found, final):
        return self.svc.record_match(found, lost, score_of(final))

    def run_jobs(self):
        return self.svc.process_due_notifications("test-worker")

    def notifications(self, **query):
        return list(self.db.notifications.find(query))

    # actors
    def login_passenger(self, ident, client=None):
        client = client or self.client
        flow = AuthFlow(client, self.sms, self.clock)
        flow.login(ident)
        return client

    def staff_client(self, depot=DEPOT_A):
        client = self.app.test_client()
        with client.session_transaction() as s:
            s["depot_phone"] = depot["phone"]
            s["depot_name"] = depot["name"]
        return client


@pytest.fixture
def f2(db, clock, sms):
    return F2(db, clock, sms)


# ── Feature 3 (claims) fixtures ──────────────────────────────────────────────
from claims import init_claims                                          # noqa: E402
from claims.attributes import encode_private_attributes                 # noqa: E402
from claims.config import ClaimsConfig                                  # noqa: E402
from claims.tokens import new_tag_id                                    # noqa: E402

# Ground truth a real owner knows; staff recorded these while logging the item.
BAG_TRUTH = {"zipper_tag_colour": "yellow", "content_item": "steel tiffin box",
             "brand": "Wildcraft", "distinguishing_mark": "Chennai Super Kings sticker"}
PNG_BYTES = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15"
             b"\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82")


def make_claims_config(**overrides):
    base = dict(ip_limit=10000, max_claims_per_day=50, max_answer_submissions_per_day=500)
    base.update(overrides)
    return ClaimsConfig(**base)


class F3(F2):
    def __init__(self, db, clock, sms, upload_root, claims_config=None, csrf=False):
        super().__init__(db, clock, sms, csrf=csrf)
        self.upload_root = upload_root
        self.claims_state = init_claims(self.app, db, config=claims_config or make_claims_config(),
                                        clock=clock, upload_root=str(upload_root))
        self.csvc = self.claims_state.service

    def item(self, found_id="item-1", depot=DEPOT_A, category="bag", truth=None, public="Black backpack",
             photo=True, **extra):
        truth = BAG_TRUTH if truth is None else truth
        image_path = None
        if photo:
            (self.upload_root / "uploads").mkdir(exist_ok=True)
            (self.upload_root / "uploads" / f"{found_id}.png").write_bytes(PNG_BYTES)
            image_path = f"uploads/{found_id}.png"
        attrs = encode_private_attributes(truth, category, public, self.csvc.answer_key,
                                          self.claims_state.config.fuzzy_min_token_length)
        return self.found(found_id=found_id, depot=depot, public=public, category=category,
                          private_attributes=attrs, qrTagId=new_tag_id(), image_path=image_path, **extra)

    def notified_match(self, ident, tracking="TRK-AAAA0001", found_id="item-1", item=None, **item_kw):
        item = item or self.db.found_reports.find_one({"found_id": found_id}) or self.item(found_id, **item_kw)
        match, _ = self.match(self.lost(ident, tracking=tracking), item, 0.92)
        self.run_jobs()
        return self.db.matches.find_one({"_id": match["_id"]})

    def open_claim(self, client, match):
        resp = client.post("/claims", json={"matchId": str(match["_id"])})
        assert resp.status_code == 201, resp.get_json()
        return resp.get_json()["claim"]["claimId"]

    def answer(self, client, claim_id, truth=None, wrong=(), overrides=None):
        """Answer the pending questions: correct from `truth` unless listed in `wrong`."""
        truth = BAG_TRUTH if truth is None else truth
        qs = client.get(f"/claims/{claim_id}/questions")
        assert qs.status_code == 200, qs.get_json()
        answers = {}
        for q in qs.get_json()["questions"]:
            qid = q["id"]
            if overrides and qid in overrides:
                answers[qid] = overrides[qid]
            else:
                answers[qid] = "purple elephant" if qid in wrong else truth[qid]
        return client.post(f"/claims/{claim_id}/answers", json={"answers": answers})

    def pending(self, claim_id):
        return self.db.claims.find_one({"claimId": claim_id})["pendingQuestions"]

    def approved_claim(self, ident=ARUN, **kw):
        client = self.login_passenger(ident, client=self.app.test_client())
        match = self.notified_match(ident, **kw)
        claim_id = self.open_claim(client, match)
        resp = self.answer(client, claim_id)
        assert resp.get_json()["claim"]["status"] == "token_issued", resp.get_json()
        return client, claim_id

    def token(self, client, claim_id):
        return client.get(f"/claims/{claim_id}/token").get_json()["token"]


@pytest.fixture
def f3(db, clock, sms, tmp_path):
    return F3(db, clock, sms, tmp_path)
