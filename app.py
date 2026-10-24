import os
import re
import math
import uuid
import logging
from datetime import datetime, timedelta

import requests
from flask import (
    Flask, render_template, request,
    redirect, url_for, session, flash, jsonify, g
)
from werkzeug.utils import secure_filename
from flask_wtf.csrf import CSRFProtect
from pymongo import MongoClient
from bson import ObjectId
from bson.errors import InvalidId
from dotenv import load_dotenv

# Load env variables BEFORE importing modules that depend on them (like Twilio and OCR)
load_dotenv()

import groq

from similarity import (
    TextSimilarity, ImageSimilarity, UnifiedScorer, OCRExtractor,
    ocr_match_score, structured_match_score
)
from notifications import notification_service
import transport_schema as T
from passenger_auth import init_passenger_auth, require_passenger
from match_notify import init_match_notify, ACTIVE_STATUSES as MATCH_ACTIVE_STATUSES
from claims import init_claims, CATEGORIES as ITEM_CATEGORIES, ATTRIBUTES as PRIVATE_ATTRIBUTES
from claims import encode_private_attributes, new_tag_id

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'tn-bus-lost-found-dev-key-2026')

# CSRF protection for all state-changing POST requests (forms + JSON fetches).
csrf = CSRFProtect(app)

# MongoDB
MONGO_URI = os.environ.get('MONGO_URI')
client = MongoClient(MONGO_URI)
db = client['tn_bus_lost_found']

lost_collection    = db['lost_reports']   # Passenger lost item reports
found_collection   = db['found_reports']  # Depot found item reports
matches_collection = db['matches']        # Persisted AI-match links
depots_collection  = db['depots']         # Depot credentials

# Transport master collections (seeded by seed_transport.py, see transport_schema)
stops_collection   = db[T.STOPS]
buses_collection   = db[T.BUSES]
routes_collection  = db[T.ROUTES]
trips_collection   = db[T.TRIPS]


def ensure_indexes():
    """
    Create indexes for the hot query paths (tracking-ID lookup, route/date
    matching scans, depot dashboard). Idempotent; any single failure (e.g. a
    pre-existing duplicate blocking a unique index) is logged, not fatal.
    """
    specs = [
        (lost_collection,    [("tracking_id", 1)], {"unique": True}),
        (lost_collection,    [("route_id", 1), ("date", 1), ("status", 1)], {}),
        (found_collection,   [("depot_phone", 1)], {}),
        (found_collection,   [("route_id", 1), ("date", 1)], {}),
        (matches_collection, [("depot_phone", 1), ("status", 1)], {}),
        (matches_collection, [("request_id", 1)], {}),
        (matches_collection, [("found_id", 1), ("request_id", 1)], {"unique": True}),
    ]
    for coll, keys, opts in specs:
        try:
            coll.create_index(keys, **opts)
        except Exception as e:
            logger.warning(f"Index creation skipped for {coll.name} {keys}: {e}")


ensure_indexes()

# Feature 1: OTP-based passenger authentication (/auth/* API, /passenger pages,
# and the require_passenger guard used by passenger-facing features).
init_passenger_auth(app, db)

# Feature 2: automatic match notification. The matcher below calls
# match_notifier.record_match(); sending runs on a background worker. Staff
# re-runs reuse the two existing matching directions.
match_notifier = init_match_notify(
    app, db,
    rematch_lost=lambda lost: compute_matches_for_lost(lost),
    rematch_found=lambda found: compute_and_save_matches(
        found, depots_collection.find_one({"phone": found.get("depot_phone")}) or {}),
).service

# Feature 3: secure claims and QR pickup tokens (also makes found-item photos staff-only).
claims_state = init_claims(app, db)
claims_service = claims_state.service

# AI Models
logger.info("Initialising AI models...")
text_sim  = TextSimilarity()
image_sim = ImageSimilarity(db=db)
ocr_ext   = OCRExtractor()
logger.info(f"CLIP available: {image_sim.available}")

# File Upload
UPLOAD_FOLDER = os.path.join('static', 'uploads')
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024   # 5 MB
os.makedirs(UPLOAD_FOLDER, exist_ok=True)


# SECTION 2 — Static Route & Depot Data
# Stops include lat/lon for the Leaflet map.
ROUTES = [
    {
        "id": "ch-co",
        "name": "Chennai ↔ Coimbatore",
        "color": "#1F6FB2",
        "stops": [
            {"name": "Chennai",      "lat": 13.0827, "lon": 80.2707},
            {"name": "Chengalpattu", "lat": 12.6919, "lon": 79.9765},
            {"name": "Villupuram",   "lat": 11.9390, "lon": 79.4938},
            {"name": "Salem",        "lat": 11.6643, "lon": 78.1460},
            {"name": "Erode",        "lat": 11.3410, "lon": 77.7172},
            {"name": "Coimbatore",   "lat": 11.0168, "lon": 76.9558},
        ]
    },
    {
        "id": "ch-md",
        "name": "Chennai ↔ Madurai",
        "color": "#C62828",
        "stops": [
            {"name": "Chennai",      "lat": 13.0827, "lon": 80.2707},
            {"name": "Chengalpattu", "lat": 12.6919, "lon": 79.9765},
            {"name": "Villupuram",   "lat": 11.9390, "lon": 79.4938},
            {"name": "Trichy",       "lat": 10.7905, "lon": 78.7047},
            {"name": "Dindigul",     "lat": 10.3624, "lon": 77.9695},
            {"name": "Madurai",      "lat":  9.9252, "lon": 78.1198},
        ]
    },
    {
        "id": "md-tn",
        "name": "Madurai ↔ Tirunelveli",
        "color": "#2E7D32",
        "stops": [
            {"name": "Madurai",      "lat":  9.9252, "lon": 78.1198},
            {"name": "Virudhunagar", "lat":  9.5851, "lon": 77.9624},
            {"name": "Kovilpatti",   "lat":  9.1710, "lon": 77.8652},
            {"name": "Tirunelveli",  "lat":  8.7139, "lon": 77.7567},
        ]
    },
]

# SECTION 3 — Helper Functions
def get_route_by_id(route_id: str):
    """Return the route dict for a given route ID, or None."""
    for route in ROUTES:
        if route["id"] == route_id:
            return route
    return None

def get_stop_names(route: dict) -> list:
    """Return a plain list of stop name strings from a route dict."""
    return [s["name"] for s in route["stops"]]

def allowed_file(filename: str) -> bool:
    """True if the file extension is in the allowed set."""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def generate_tracking_id() -> str:
    """Return a human-readable tracking ID like TRK-A1B2C3D4."""
    return f"TRK-{uuid.uuid4().hex[:8].upper()}"

def save_uploaded_image(file) -> str:
    """
    Save an uploaded image to the uploads folder.
    Returns relative path like 'uploads/abc.jpg', or None if invalid.
    """
    if file and file.filename and allowed_file(file.filename):
        filename = secure_filename(f"{uuid.uuid4().hex[:8]}_{file.filename}")
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)
        return f"uploads/{filename}"
    return None

def get_depots() -> dict:
    """Load depot info from MongoDB into a dict keyed by phone number."""
    return {d["phone"]: d for d in depots_collection.find()}


# ── Transport DB helpers (used by the /api/transport/* endpoints & matching) ──
def get_route(route_id: str):
    """Return a transport route doc by route_id, or None."""
    if not route_id:
        return None
    return routes_collection.find_one({"route_id": route_id})

def get_trip(trip_id: str):
    """Return a trip doc by trip_id, or None."""
    if not trip_id:
        return None
    return trips_collection.find_one({"trip_id": trip_id})

def get_bus(bus_id: str):
    """Return a bus doc by bus_id, or None."""
    if not bus_id:
        return None
    return buses_collection.find_one({"bus_id": bus_id})

def route_stops(route: dict) -> list:
    """Resolve a route's ordered stop_sequence (ids) into stop docs."""
    seq = route.get("stop_sequence", []) if route else []
    by_id = {s["stop_id"]: s for s in stops_collection.find({"stop_id": {"$in": seq}})}
    return [by_id[sid] for sid in seq if sid in by_id]

def _haversine_km(a, b) -> float:
    """Great-circle distance in km between (lat, lon) tuples a and b."""
    R = 6371.0
    la1, lo1, la2, lo2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    h = (math.sin((la2 - la1) / 2) ** 2
         + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(h))


def _parse_seat(value, capacity=None, upper=80):
    """
    Parse a seat number, bounded by the bus capacity when it is known (else a
    sane upper limit). Returns (seat_int, error_message) — error is None on OK.
    """
    try:
        seat = int(str(value).strip())
    except (TypeError, ValueError):
        return None, "Seat number must be a whole number."
    limit = capacity if capacity else upper
    if seat < 1 or seat > limit:
        return None, f"Seat number must be between 1 and {limit}."
    return seat, None


def _parse_travel_date(value):
    """
    Parse a YYYY-MM-DD date and reject anything past tomorrow. The forms cap the date
    client-side, and an unparseable date silently collapses the matching window,
    so it is validated here too. Returns (date_str, error_message).
    """
    try:
        d = datetime.strptime(str(value).strip(), "%Y-%m-%d")
    except (TypeError, ValueError):
        return None, "Please provide a valid date."
    # One day of slack: the browser caps the picker using the CLIENT's local date,
    # so a TN (IST) user just past midnight would otherwise be rejected by a
    # server running on UTC. Anything beyond tomorrow is still refused.
    if d.date() > (datetime.now() + timedelta(days=1)).date():
        return None, "The date cannot be in the future."
    return d.strftime("%Y-%m-%d"), None


def depot_serves_route(depot: dict, route: dict) -> bool:
    """
    True when a depot may log a found item against this route: either it
    operates the service, or the route actually calls at the depot's city (an
    item is handed in wherever the bus reaches).

    Both clauses are needed — an operating depot is not always itself a listed
    stop on its own route, and a few routes have no staffed depot among their
    stops at all. Uses the denormalized `stop_cities` when present, otherwise
    resolves the stop documents.
    """
    if not depot or not route:
        return False
    if route.get("operating_depot_code") and \
       route["operating_depot_code"] == depot.get("depot_code"):
        return True
    city = (depot.get("city") or depot.get("stop") or "").strip().lower()
    if not city:
        return False
    cities = route.get("stop_cities")
    if cities is None:
        cities = [(x.get("city") or "").strip().lower() for x in route_stops(route)]
    return city in cities


def build_seatmap(bus: dict) -> dict:
    """
    Build a seat-map descriptor for the visual picker from a bus's capacity and
    seat_layout. Seat numbers run 1..capacity so the front-end can render a grid.
    """
    layout = (bus or {}).get("seat_layout", {}) or {}
    capacity = int((bus or {}).get("capacity", 0) or 0)
    return {
        "capacity":  capacity,
        "pattern":   layout.get("pattern", "2+2"),
        "cols":      layout.get("cols", 4),
        "rows":      layout.get("rows", 0),
        "aisle_after": layout.get("aisle_after", 2),
        "back_row":  layout.get("back_row", 0),
        "seats":     list(range(1, capacity + 1)),
        "occupied":  [],   # no booking system — picker records where the passenger sat
    }

def luggage_could_be_at_depot(stops: list, src: str, dst: str, depot_stop: str) -> bool:
    """
    Check if a lost item could have reached the depot.

    When a passenger gets off at 'dst', their luggage stays on the bus
    and travels to later stops. So the depot must come AFTER 'dst'.

    Example: src=Chennai, dst=Salem → depot could be Erode or Coimbatore.
    """
    try:
        i = stops.index(src)
        j = stops.index(dst)
        k = stops.index(depot_stop)
        if i < j:
            return k >= j    # Forward journey: depot is at or past destination
        elif i > j:
            return k <= j    # Reverse journey: depot is at or before destination
        else:
            return False    # Same stop — invalid
    except ValueError:
        return False        # Stop not in this route


# SECTION 4 — Matching Engine
def _window_dates(date_str: str, days: int = 7, forward: bool = False) -> list:
    """
    Build a date window around an anchor date (inclusive), as YYYY-MM-DD strings.
    forward=False → [anchor-days … anchor] (a lost item logged up to `days` before
    a found report). forward=True → [anchor … anchor+days] (found reports that may
    appear up to `days` after a passenger's travel date).
    """
    dates = [date_str]
    try:
        d0 = datetime.strptime(date_str, "%Y-%m-%d")
        for i in range(1, days + 1):
            delta = timedelta(days=i)
            d = (d0 + delta) if forward else (d0 - delta)
            dates.append(d.strftime("%Y-%m-%d"))
    except Exception:
        pass
    return dates


def score_lost_found(found_report: dict, lost: dict, depot: dict):
    """
    Score a single (found, lost) pair — the shared core used by BOTH matching
    directions. Returns the UnifiedScorer breakdown dict, or None when the
    directional luggage filter rejects the pair (or the route is unknown).

    Signals: text (descriptions), image (CLIP), OCR (text read off the item,
    cached on the found doc), and structured (exact trip / bus / adjacent seat).
    """
    depot = depot or {}
    transport_route = get_route(found_report.get("route_id"))
    if transport_route:
        seq_ids = transport_route.get("stop_sequence", [])
        depot_city = depot.get("city") or depot.get("stop") or ""
        depot_stop_id = next(
            (s["stop_id"] for s in route_stops(transport_route)
             if s.get("city", "").strip().lower() == depot_city.strip().lower()),
            None
        )
        b, a = lost.get("boarding_stop_id"), lost.get("alighting_stop_id")
        if (b and a and depot_stop_id
                and b in seq_ids and a in seq_ids and depot_stop_id in seq_ids):
            if not luggage_could_be_at_depot(seq_ids, b, a, depot_stop_id):
                return None
    else:
        # Legacy fallback: hardcoded ROUTES + name-based directional logic.
        legacy_route = get_route_by_id(found_report.get("route_id", ""))
        if not legacy_route:
            return None
        names = get_stop_names(legacy_route)
        if not luggage_could_be_at_depot(
            names, lost.get("source", ""), lost.get("destination", ""), depot.get("stop", "")
        ):
            return None

    # Text similarity
    text_score = text_sim.similarity(
        lost.get("description", ""), found_report.get("notes", "")
    )

    # Image similarity
    image_score = 0.0
    found_img = os.path.join("static", found_report["image_path"]) \
        if found_report.get("image_path") else None
    if found_img and lost.get("image_path"):
        image_score = image_sim.similarity(
            found_img, os.path.join("static", lost["image_path"])
        )

    # OCR — reuse the text cached on the found doc; only extract live if a legacy
    # found report has no cached ocr_text.
    ocr_text = found_report.get("ocr_text")
    if ocr_text is None and found_img:
        ocr_text = ocr_ext.extract_text(found_img)
    ocr_score = ocr_match_score(
        lost.get("name", ""), lost.get("description", ""), ocr_text or ""
    )

    # Structured (travel-record) score — exact trip / bus / adjacent seat.
    structured_score = structured_match_score(
        found_report.get("trip_id"), lost.get("trip_id"),
        found_report.get("bus_id"),  lost.get("bus_id"),
        found_report.get("found_seat_area"), lost.get("seat_no"),
    )

    return UnifiedScorer.compute(
        text_score, image_score, route_score=1.0,
        ocr_score=ocr_score, structured_score=structured_score
    )


def _save_match(found_report: dict, lost: dict, score: dict) -> bool:
    """
    Persist a qualifying (found, lost) pair through Feature 2, which upserts the
    match (unique on reportId+itemId), routes it by confidence (auto-notify /
    staff review / reject) and queues privacy-safe notifications on the
    background worker. Duplicate triggers are no-ops. Returns True if a new match
    document was created.
    """
    _, is_new = match_notifier.record_match(found_report, lost, score)
    return is_new


def compute_and_save_matches(found_report: dict, depot: dict) -> int:
    """
    FOUND-triggered matching: score a new found report against pending lost
    reports on the SAME route within the date window. Returns qualifying count.
    """
    valid_dates = _window_dates(found_report.get("date", ""), forward=False)
    pending_lost = lost_collection.find({
        "route_id": found_report.get("route_id"),
        "date":     {"$in": valid_dates},
        "status":   {"$ne": "resolved"}
    })

    n = 0
    for lost in pending_lost:
        score = score_lost_found(found_report, lost, depot)
        if score and score["is_match"]:
            _save_match(found_report, lost, score)
            n += 1
    return n


def compute_matches_for_lost(lost_report: dict) -> int:
    """
    LOST-triggered matching (symmetric counterpart): score a new lost report
    against already-logged found reports on the SAME route within the date
    window. This closes the gap where a passenger reporting AFTER the depot had
    already logged the item would otherwise never be matched.
    """
    valid_dates = _window_dates(lost_report.get("date", ""), forward=True)
    candidate_found = found_collection.find({
        "route_id": lost_report.get("route_id"),
        "date":     {"$in": valid_dates},
    })

    n = 0
    depot_cache = {}
    for found in candidate_found:
        phone = found.get("depot_phone")
        if phone not in depot_cache:
            depot_cache[phone] = depots_collection.find_one({"phone": phone}) or {}
        score = score_lost_found(found, lost_report, depot_cache[phone])
        if score and score["is_match"]:
            _save_match(found, lost_report, score)
            n += 1
    return n


def get_matches_for_depot(depot_phone: str) -> list:
    """
    Load all non-resolved matches for this depot from MongoDB.
    Enriches each match with the full lost_report and found_report data.
    Groups them by found_id for display.
    """
    # Fetch only pending matches belonging to this depot
    raw_matches = list(matches_collection.find({
        "depot_phone": depot_phone,
        "status":      {"$in": list(MATCH_ACTIVE_STATUSES)}
    }).sort("created_at", -1))

    if not raw_matches:
        return []

    # Collect all unique found_ids and request_ids for batch fetching
    found_ids   = list({m["found_id"]   for m in raw_matches})
    request_ids = list({m["request_id"] for m in raw_matches})

    # Batch fetch from DB (much faster than one query per match)
    found_map = {
        f["found_id"]: f
        for f in found_collection.find({"found_id": {"$in": found_ids}})
    }
    lost_map = {
        l["request_id"]: l
        for l in lost_collection.find({"request_id": {"$in": request_ids}})
    }

    # Group matches by found_id
    groups = {}
    for match in raw_matches:
        fid  = match["found_id"]
        rid  = match["request_id"]
        lost = lost_map.get(rid)
        if not lost:
            continue   # Lost report deleted — skip

        # Attach score and match_id to the lost report dict for the template
        enriched_lost = dict(lost)
        enriched_lost["score"]    = match["score"]
        enriched_lost["match_id"] = str(match["_id"])
        enriched_lost["match_status"] = match.get("status")

        if fid not in groups:
            groups[fid] = {
                "found_report": found_map.get(fid, {}),
                "matches":      []
            }
        groups[fid]["matches"].append(enriched_lost)

    # Sort each group's matches by score descending
    result = list(groups.values())
    for g in result:
        g["matches"].sort(key=lambda x: x["score"]["final"], reverse=True)

    return result



# SECTION 5 — Passenger Routes
@app.route('/')
def index():
    """Passenger homepage: report form + Leaflet map."""
    last_tracking_id = session.pop('last_tracking_id', None)
    return render_template('index.html', last_tracking_id=last_tracking_id)


@app.route('/lost', methods=['POST'])
@require_passenger(page=True)
def submit_lost():
    """
    Accept a lost luggage report from an OTP-verified passenger (Feature 1).
    The report is owned by the session's (pnr, phone); name and phone come from
    the verified session, never from the form.
    """
    passenger = g.passenger
    route_id          = request.form.get('route_id')
    trip_id           = request.form.get('trip_id')
    seat_no           = request.form.get('seat_no')
    boarding_stop_id  = request.form.get('boarding_stop_id')
    alighting_stop_id = request.form.get('alighting_stop_id')
    date              = request.form.get('date')
    description       = request.form.get('description')
    phone             = passenger.phone
    name              = passenger.passenger_name
    image_file        = request.files.get('image')

    if not all([route_id, trip_id, seat_no, boarding_stop_id,
                alighting_stop_id, date, description, phone, name]):
        flash('Please fill in all required fields.', 'error')
        return redirect(url_for('index'))

    route = get_route(route_id)   # transport route
    if not route:
        flash('Invalid route selected.', 'error')
        return redirect(url_for('index'))

    seq = route.get("stop_sequence", [])
    if boarding_stop_id not in seq or alighting_stop_id not in seq:
        flash('Invalid boarding or alighting stop for selected route.', 'error')
        return redirect(url_for('index'))

    if boarding_stop_id == alighting_stop_id:
        # The directional luggage filter can never satisfy src == dst, so such a
        # report would sit unmatchable forever. Reject it up front.
        flash('Boarding and alighting stops must be different.', 'error')
        return redirect(url_for('index'))

    date, err = _parse_travel_date(date)
    if err:
        flash(err, 'error')
        return redirect(url_for('index'))

    # The trip must exist AND actually run on the selected route — trip_id is
    # client-supplied and drives the exact-trip match signal.
    trip = get_trip(trip_id)
    if not trip or trip.get("route_id") != route_id:
        flash('The selected bus/service does not run on that route.', 'error')
        return redirect(url_for('index'))

    bus            = get_bus(trip.get("bus_id"))
    bus_id         = trip.get("bus_id")
    departure_time = trip.get("departure_time")

    seat_no, err = _parse_seat(seat_no, (bus or {}).get("capacity"))
    if err:
        flash(err, 'error')
        return redirect(url_for('index'))

    stops_map = {s["stop_id"]: s["name"] for s in route_stops(route)}

    tracking_id = generate_tracking_id()
    request_id  = uuid.uuid4().hex   # Internal unique key for DB relations

    image_path = None
    if image_file and image_file.filename:
        image_path = save_uploaded_image(image_file)

    report = {
        "request_id":        request_id,    # Unique internal ID (used in matches)
        "tracking_id":       tracking_id,   # Human-readable ID shown to passenger
        "route_id":          route_id,
        "route_no":          route.get("route_no"),
        "route_name":        f"{route.get('origin_name')} → {route.get('dest_name')}",
        "trip_id":           trip_id,
        "bus_id":            bus_id,
        "bus_registration":  (bus or {}).get("registration_no"),
        "departure_time":    departure_time,
        "seat_no":           seat_no,
        "boarding_stop_id":  boarding_stop_id,
        "alighting_stop_id": alighting_stop_id,
        "date":              date,
        # Legacy display fields (keep status page & templates working unchanged)
        "source":            stops_map.get(boarding_stop_id, ""),
        "destination":       stops_map.get(alighting_stop_id, ""),
        "description":       description,
        "phone":             phone,
        "name":              name,
        "ownerPnr":          passenger.pnr,     # object-level authorization (Feature 2)
        "ownerPhone":        passenger.phone,
        "image_path":        image_path,
        "status":            "pending",     # pending | matched | resolved | cancelled
        "matched_depot":     None,
        "matched_at":        None,
        "created_at":        datetime.now().isoformat()
    }

    lost_collection.insert_one(report)

    # Symmetric matching: check against found items already logged on this route.
    # The count is not shown: review-band matches must stay invisible until approved.
    compute_matches_for_lost(report)

    msg = (f'Report submitted! Your Tracking ID is {tracking_id} — '
           f'save it to check your claim status. We will notify you if a depot finds a likely match.')
    flash(msg, 'success')
    session['last_tracking_id'] = tracking_id
    return redirect(url_for('index'))


@app.route('/status')
def status_page():
    """Passenger self-service status check page."""
    return render_template('status.html')


# SECTION 6 — Depot Staff Routes
@app.route('/depot-login')
def depot_login_page():
    return render_template('depot_login.html', depots=get_depots())


@app.route('/depot/login', methods=['POST'])
def depot_login():
    phone    = request.form.get('phone')
    password = request.form.get('password')
    depots   = get_depots()

    if phone in depots and depots[phone]["password"] == password:
        session['depot_phone'] = phone
        session['depot_name']  = depots[phone]["name"]
        flash(f'Welcome, {depots[phone]["name"]}!', 'success')
        return redirect(url_for('depot_dashboard'))

    flash('Invalid phone number or password.', 'error')
    return redirect(url_for('depot_login_page'))


@app.route('/depot/logout')
def depot_logout():
    session.pop('depot_phone', None)
    session.pop('depot_name',  None)
    flash('Logged out successfully.', 'success')
    return redirect(url_for('depot_login_page'))


@app.route('/depot')
def depot_dashboard():
    """Depot dashboard: register found items + view AI matches from DB."""
    if 'depot_phone' not in session:
        flash('Please login to access depot dashboard.', 'error')
        return redirect(url_for('depot_login_page'))

    depot_phone = session['depot_phone']
    depots      = get_depots()
    depot       = depots.get(depot_phone)

    if not depot:
        session.clear()
        flash('Invalid depot session.', 'error')
        return redirect(url_for('depot_login_page'))

    depot_found = list(found_collection.find(
        {"depot_phone": depot_phone}
    ).sort("created_at", -1))

    # Load matches from DB (not recomputed — already populated at submit_found time)
    all_matches = get_matches_for_depot(depot_phone)

    return render_template(
        'depot.html',
        depot=depot,
        depot_phone=depot_phone,
        found_reports=depot_found,
        all_matches=all_matches,
        clip_available=image_sim.available
    )


@app.route('/depot/found', methods=['POST'])
def submit_found():
    """Accept a found luggage report from depot staff and run AI matching."""
    if 'depot_phone' not in session:
        flash('Please login to submit found reports.', 'error')
        return redirect(url_for('depot_login_page'))

    depot_phone = session['depot_phone']
    depots      = get_depots()
    depot       = depots.get(depot_phone)

    if not depot:
        flash('Invalid depot session.', 'error')
        return redirect(url_for('depot_login_page'))

    route_id        = request.form.get('route_id')
    trip_id         = request.form.get('trip_id') or None
    date            = request.form.get('date')
    notes           = request.form.get('notes')
    public_description = (request.form.get('public_description') or '').strip()
    category        = request.form.get('category') or 'other'
    found_seat_area = request.form.get('found_seat_area') or None

    if not all([route_id, date, notes, public_description]):
        flash('Please fill in all required fields.', 'error')
        return redirect(url_for('depot_dashboard'))

    route = get_route(route_id)   # transport route
    if not route:
        flash('Invalid route selected.', 'error')
        return redirect(url_for('depot_dashboard'))

    if not depot_serves_route(depot, route):
        flash('This depot does not serve the selected route.', 'error')
        return redirect(url_for('depot_dashboard'))

    date, err = _parse_travel_date(date)
    if err:
        flash(err, 'error')
        return redirect(url_for('depot_dashboard'))

    # Optional trip → carries the bus the item came off, for exact matching.
    # When supplied it must actually run on the selected route (it drives the
    # exact-trip signal). bus_id is derived from the trip only — never read from
    # the form, which would let a client forge the same-bus score boost.
    trip = None
    if trip_id:
        trip = get_trip(trip_id)
        if not trip or trip.get("route_id") != route_id:
            flash('The selected bus/service does not run on that route.', 'error')
            return redirect(url_for('depot_dashboard'))
    bus_id = trip.get("bus_id") if trip else None

    if found_seat_area is not None:
        capacity = (get_bus(bus_id) or {}).get("capacity") if bus_id else None
        found_seat_area, err = _parse_seat(found_seat_area, capacity)
        if err:
            flash(err, 'error')
            return redirect(url_for('depot_dashboard'))

    # Handle optional image upload
    image_path = None
    if 'image' in request.files:
        image_path = save_uploaded_image(request.files['image'])

    # Pre-compute CLIP embedding + OCR text now so BOTH matching directions are
    # fast later and never re-hit the OCR API for this image.
    ocr_text = ""
    if image_path:
        if image_sim.available:
            image_sim.embed(os.path.join("static", image_path))
        ocr_text = ocr_ext.extract_text(os.path.join("static", image_path))

    found_id = uuid.uuid4().hex   # Unique internal ID for this found report

    # Feature 3: private attributes are hashed right here; the raw answers are never stored.
    if category not in ITEM_CATEGORIES:
        category = 'other'
    private_attributes = encode_private_attributes(
        {name: request.form.get(f'attr_{name}') for name in PRIVATE_ATTRIBUTES},
        category, public_description, claims_service.answer_key,
        claims_state.config.fuzzy_min_token_length)

    report = {
        "found_id":        found_id,          # Unique internal ID (used in matches)
        "id":              found_id[:8],      # Short display ID
        "depot_phone":     depot_phone,
        "depot_name":      depot["name"],
        "route_id":        route_id,
        "route_no":        route.get("route_no"),
        "route_name":      f"{route.get('origin_name')} → {route.get('dest_name')}",
        "trip_id":         trip_id,
        "bus_id":          bus_id,
        "departure_time":  trip.get("departure_time") if trip else None,
        "found_seat_area": found_seat_area,
        "date":            date,
        "notes":           notes,             # private: staff only
        "public_description": public_description[:120],   # the only text passengers see
        "category":        category,
        "private_attributes": private_attributes,          # hashed claim-verification answers
        "qrTagId":         new_tag_id(),                   # printed and stuck on the item
        "image_path":      image_path,
        "ocr_text":        ocr_text,          # cached; reused by the matcher
        "status":          "open",
        "created_at":      datetime.now().isoformat()
    }

    found_collection.insert_one(report)
    claims_service.record_item_logged(report, depot_phone)

    # Run AI matching and SAVE results to matches collection
    n = compute_and_save_matches(report, depot)

    if n > 0:
        flash(f'Found report submitted! {n} potential match(es) found!', 'success')
    else:
        flash('Found report submitted. No matches yet.', 'success')

    return redirect(url_for('depot_dashboard'))



# SECTION 7 — API Endpoints

@app.route('/api/status/<tracking_id>')
def api_status(tracking_id: str):
    """
    Return JSON status of a lost report by tracking ID.
    Used by the passenger 'Check Status' page.
    """
    tracking_id = tracking_id.strip().upper()
    report = lost_collection.find_one({"tracking_id": tracking_id})
    if not report:
        return jsonify({"found": False, "message": "Tracking ID not found."}), 404

    return jsonify({
        "found":         True,
        "tracking_id":   report.get("tracking_id"),
        "status":        report.get("status", "pending"),
        "name":          report.get("name"),
        "route_name":    report.get("route_name"),
        "date":          report.get("date"),
        "description":   report.get("description"),
        "matched_depot": report.get("matched_depot"),
        "matched_at":    report.get("matched_at"),
        "created_at":    report.get("created_at")
    })


@app.route('/api/routes')
def api_routes():
    """Return all routes with stop coordinates for the Leaflet map."""
    return jsonify(ROUTES)


# ── Transport catalog API (backs the passenger route/bus/seat selector) ───────
@app.route('/api/transport/routes')
def api_transport_routes():
    """
    Search the 500-route SETC catalog for the passenger/depot selector.
    Query params: q (free text over route_no / origin / destination), limit.
    """
    q = (request.args.get('q') or '').strip()
    try:
        limit = min(max(int(request.args.get('limit', 30)), 1), 100)
    except (TypeError, ValueError):
        limit = 30

    query = {}
    if q:
        rx = {"$regex": re.escape(q), "$options": "i"}
        query = {"$or": [
            {"route_no": rx}, {"origin_name": rx},
            {"dest_name": rx}, {"via": rx},
        ]}

    # depot_only=1 narrows the catalog to routes the logged-in depot may log
    # found items against, so the picker cannot offer a route that submit_found
    # would then reject. Mirrors depot_serves_route().
    if request.args.get('depot_only') == '1' and session.get('depot_phone'):
        depot = depots_collection.find_one({"phone": session['depot_phone']}) or {}
        scope = {"$or": [
            {"operating_depot_code": depot.get("depot_code")},
            {"stop_cities": (depot.get("city") or depot.get("stop") or "").strip().lower()},
        ]}
        query = {"$and": [query, scope]} if query else scope

    cursor = routes_collection.find(
        query,
        {"_id": 0, "route_id": 1, "route_no": 1, "origin_name": 1,
         "dest_name": 1, "via": 1, "type": 1, "distance_km": 1}
    ).sort("route_no", 1).limit(limit)
    return jsonify(list(cursor))


@app.route('/api/transport/routes/<route_id>')
def api_transport_route_detail(route_id):
    """Return a route with its ordered stops (names + coords) for the map."""
    route = get_route(route_id)
    if not route:
        return jsonify({"error": "Route not found"}), 404
    stops = [
        {"stop_id": s["stop_id"], "name": s["name"],
         "lat": s.get("lat"), "lon": s.get("lon"), "is_major": s.get("is_major", False)}
        for s in route_stops(route)
    ]
    return jsonify({
        "route_id":    route["route_id"],
        "route_no":    route.get("route_no"),
        "origin_name": route.get("origin_name"),
        "dest_name":   route.get("dest_name"),
        "via":         route.get("via", ""),
        "type":        route.get("type"),
        "distance_km": route.get("distance_km"),
        "stops":       stops,
    })


@app.route('/api/transport/routes/<route_id>/geometry')
def api_transport_route_geometry(route_id):
    """
    Road-following geometry for a route as [[lat, lon], ...].

    Routes ORIGIN → DESTINATION via the public OSRM server and lets OSRM follow
    the real road corridor. The synthetic intermediate stops are deliberately
    NOT used as routing waypoints — some sit off-corridor and made OSRM produce
    paths that ended partway to the destination. The response is validated
    (both endpoints must snap onto roads near the true origin/destination) before
    it is accepted and cached on the route document. If OSRM is unavailable or
    the response fails validation, a straight line through the stops is returned
    and NOT cached, so a later view retries.
    """
    route = get_route(route_id)
    if not route:
        return jsonify({"error": "Route not found"}), 404

    # Serve cached geometry when available.
    if route.get("road_geometry"):
        return jsonify({"coords": route["road_geometry"],
                        "source": route.get("geometry_source", "osrm")})

    coord_stops = [s for s in route_stops(route)
                   if s.get("lat") is not None and s.get("lon") is not None]
    straight = [[s["lat"], s["lon"]] for s in coord_stops]
    if len(coord_stops) < 2:
        return jsonify({"coords": straight, "source": "straight"})

    origin, dest = coord_stops[0], coord_stops[-1]
    coords, source = straight, "straight"
    try:
        path = f"{origin['lon']},{origin['lat']};{dest['lon']},{dest['lat']}"
        resp = requests.get(
            f"https://router.project-osrm.org/route/v1/driving/{path}",
            params={"overview": "full", "geometries": "geojson"},
            timeout=6,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("code") == "Ok" and data.get("routes"):
                geo = data["routes"][0]["geometry"]["coordinates"]   # [lon, lat]
                # Validate: every waypoint snapped onto a nearby road AND the
                # drawn path actually starts at the origin and ends at the dest.
                snaps_ok = all(w.get("distance", 0) <= 25000
                               for w in data.get("waypoints", []))
                ends_ok = bool(geo) and (
                    _haversine_km((geo[0][1], geo[0][0]), (origin["lat"], origin["lon"])) <= 25
                    and _haversine_km((geo[-1][1], geo[-1][0]), (dest["lat"], dest["lon"])) <= 25
                )
                if snaps_ok and ends_ok:
                    coords = [[lat, lon] for lon, lat in geo]
                    source = "osrm"
    except Exception as e:
        logger.warning(f"OSRM routing failed for route {route_id}: {e}")

    # Cache only a validated road route (failures retry on the next view).
    if source == "osrm":
        routes_collection.update_one(
            {"route_id": route_id},
            {"$set": {"road_geometry": coords, "geometry_source": "osrm"}}
        )
    return jsonify({"coords": coords, "source": source})


@app.route('/api/transport/routes/<route_id>/trips')
def api_transport_route_trips(route_id):
    """Return the trips (bus + departure timing) running on a route."""
    trips = list(trips_collection.find({"route_id": route_id}).sort("departure_time", 1))
    bus_ids = list({t["bus_id"] for t in trips})
    buses = {b["bus_id"]: b for b in buses_collection.find({"bus_id": {"$in": bus_ids}})}
    out = []
    for t in trips:
        bus = buses.get(t["bus_id"], {})
        out.append({
            "trip_id":         t["trip_id"],
            "bus_id":          t["bus_id"],
            "departure_time":  t.get("departure_time"),
            "arrival_time":    t.get("arrival_time"),
            "registration_no": bus.get("registration_no"),
            "type":            bus.get("type"),
            "capacity":        bus.get("capacity"),
        })
    return jsonify(out)


@app.route('/api/transport/depots/<phone>/routes')
def api_transport_depot_routes(phone):
    """Routes operated by a depot (with stop coords) — for the depot dashboard map."""
    depot = depots_collection.find_one({"phone": phone})
    if not depot:
        return jsonify([])
    routes = list(routes_collection.find(
        {"operating_depot_code": depot.get("depot_code")}
    ).limit(40))
    # Batch-fetch every stop referenced by these routes in one query.
    all_ids = {sid for r in routes for sid in r.get("stop_sequence", [])}
    stop_map = {s["stop_id"]: s for s in stops_collection.find({"stop_id": {"$in": list(all_ids)}})}
    out = []
    for r in routes:
        pts = [
            {"name": stop_map[sid]["name"], "lat": stop_map[sid].get("lat"),
             "lon": stop_map[sid].get("lon")}
            for sid in r.get("stop_sequence", []) if sid in stop_map
        ]
        out.append({
            "route_no":    r.get("route_no"),
            "origin_name": r.get("origin_name"),
            "dest_name":   r.get("dest_name"),
            "stops":       pts,
        })
    return jsonify(out)


@app.route('/api/transport/trips/<trip_id>/seatmap')
def api_transport_seatmap(trip_id):
    """Return the seat-map descriptor for the bus on a trip (visual picker)."""
    trip = get_trip(trip_id)
    if not trip:
        return jsonify({"error": "Trip not found"}), 404
    bus = get_bus(trip.get("bus_id"))
    if not bus:
        return jsonify({"error": "Bus not found"}), 404
    seatmap = build_seatmap(bus)
    seatmap["registration_no"] = bus.get("registration_no")
    seatmap["type"] = bus.get("type")
    return jsonify(seatmap)


@app.route('/api/translate', methods=['POST'])
def api_translate():
    """Translate incoming text to English using Groq."""
    data = request.get_json(silent=True) or {}
    if 'text' not in data:
        return jsonify({'error': 'No text provided'}), 400

    text = data['text']

    groq_api_key = os.environ.get("GROQ_API_KEY")
    if not groq_api_key:
        return jsonify({'error': 'Groq API key not configured.'}), 500

    try:
        groq_client = groq.Groq(api_key=groq_api_key)
        chat_completion = groq_client.chat.completions.create(
            messages=[
                {
                    "role": "system",
                    "content": "You are a professional translator for a lost luggage recovery system. Translate the user's text into clear, concise English. Only output the translated English text, nothing else."
                },
                {
                    "role": "user",
                    "content": text
                }
            ],
            model="llama3-8b-8192",
        )
        translated_text = chat_completion.choices[0].message.content.strip()
        return jsonify({'translated': translated_text})
    except Exception as e:
        logger.error(f"Translation error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/match/resolve', methods=['POST'])
def resolve_match():
    """
    Mark a specific match as resolved.
    Updates BOTH the matches collection AND the lost_reports collection.

    Expected JSON body:
      { "match_id": "<MongoDB ObjectId hex>" }

    Access control:
      The match must belong to the logged-in depot. The associated request_id
      is read from the match document (never trusted from the client), so a
      depot cannot resolve another depot's match or an arbitrary lost report.
    """
    if 'depot_phone' not in session:
        return jsonify({"success": False, "message": "Not authenticated"}), 401

    data        = request.get_json(silent=True) or {}
    match_id    = data.get('match_id')
    depot_phone = session['depot_phone']
    depot_name  = session.get('depot_name', '')

    # Validate the match id (request_id is derived server-side from the match,
    # never trusted from the client).
    if not isinstance(match_id, str) or not match_id.strip():
        return jsonify({"success": False, "message": "match_id is required."}), 400
    match_id = match_id.strip()
    try:
        oid = ObjectId(match_id)
    except (InvalidId, TypeError):
        return jsonify({"success": False, "message": "Invalid match id."}), 400

    # Access control: a depot may only resolve matches that belong to it.
    match = matches_collection.find_one({"_id": oid, "depot_phone": depot_phone})
    if not match:
        return jsonify({"success": False, "message": "Match not found."}), 404
    if match.get("status") == "resolved":
        return jsonify({"success": False, "message": "Match already resolved."}), 409
    if match.get("status") not in MATCH_ACTIVE_STATUSES:
        return jsonify({"success": False,
                        "message": f"Match is {match.get('status')} and can no longer be resolved."}), 409
    item = found_collection.find_one({"found_id": match["found_id"]}) or {}
    if item.get("status") not in (None, "open"):
        return jsonify({"success": False,
                        "message": "This item is reserved by a passenger claim; use the claims desk."}), 409

    request_id = match["request_id"]   # trusted: comes from the match document

    # Step 1: Mark this match as resolved
    matches_collection.update_one(
        {"_id": oid},
        {"$set": {
            "status":      "resolved",
            "resolved_at": datetime.now().isoformat(),
            "resolved_by": depot_name
        }}
    )

    # Step 2: Mark the lost report as resolved
    # This prevents it from matching other found reports in the future.
    lost_collection.update_one(
        {"request_id": request_id},
        {"$set": {
            "status":        "resolved",
            "matched_depot": depot_name,
            "matched_at":    datetime.now().isoformat()
        }}
    )

    # Step 3: The item is now reserved for this report — end every competing match
    # (other reports on this item, other items on this report) and their queued
    # notifications.
    match_notifier.on_match_resolved(oid)
    # Step 4: Send WhatsApp notification to the passenger
    lost_report = lost_collection.find_one({"request_id": request_id})
    if lost_report and lost_report.get("phone"):
        msg = f"TNSTC Alert: Good news! Your lost item ({lost_report.get('tracking_id')}) has been verified and found at {depot_name}. Please visit the depot to collect it."
        notification_service.send_whatsapp_async(lost_report["phone"], msg)

    return jsonify({
        "success": True,
        "message": "Match resolved successfully."
    })


@app.errorhandler(413)
def file_too_large(e):
    """Friendly response when an upload exceeds MAX_CONTENT_LENGTH (5 MB)."""
    flash('That file is too large. Please upload an image under 5 MB.', 'error')
    return redirect(request.referrer or url_for('index')), 413


if __name__ == '__main__':
    # Debug is OFF by default (the Werkzeug debugger is an RCE surface if exposed).
    # Enable locally with FLASK_DEBUG=1.
    debug = os.environ.get('FLASK_DEBUG') == '1'
    app.run(debug=debug, use_reloader=False, port=5003)
