"""
transport_schema.py — TNSTC Transport Database schema
=====================================================
Single source of truth for the 8 normalized transport collections that back
the lost-and-found system. MongoDB is schemaless, so this module documents the
relational (ER) model and applies lenient JSON-Schema *validators* so each
collection behaves like a table (required keys + types) while still allowing
extra fields for forward compatibility with official TNSTC APIs.

Design rules (see plan):
  * Normalized — parents are referenced by string id (`*_id`), never embedded
    and duplicated. Mirrors the existing `request_id` / `found_id` convention.
  * Backward compatible — validators require only the identifying/relational
    fields, so the existing depot login flow (which writes phone/name/password/
    stop/routes) keeps working and old docs stay valid.
  * API-ready — every master collection carries a nullable `external_ref`
    (official TNSTC id slot) and a `source` flag ("setc_csv" | "synthetic").

ER model (→ = references by id):

    depots ─┬─< stops        (stop.depot_id → depots, nearest depot)
            ├─< buses        (bus.depot_id → depots, owning depot)
            ├─< routes       (route.operating_depot_id → depots)
            ├─< drivers      (driver.depot_id → depots)
            └─< conductors   (conductor.depot_id → depots)

    routes ─┬─< trips        (trip.route_id → routes)
            └── stop_sequence[] → stops    (ordered; powers luggage logic)

    buses  ──< trips         (trip.bus_id → buses)

    trips  ──1─1── bus_schedules   (schedule.trip_id → trips)
                     schedule.bus_id       → buses
                     schedule.driver_id    → drivers
                     schedule.conductor_id → conductors
"""

# ── Collection name constants (import these; never hard-code strings) ─────────
DEPOTS        = "depots"          # EXPANDED existing collection (100-depot master)
STOPS         = "stops"
BUSES         = "buses"
ROUTES        = "routes"
TRIPS         = "trips"
DRIVERS       = "drivers"
CONDUCTORS    = "conductors"
BUS_SCHEDULES = "bus_schedules"

# Collections created fresh by the transport seeder (validators applied on create).
TRANSPORT_COLLECTIONS = [STOPS, BUSES, ROUTES, TRIPS, DRIVERS, CONDUCTORS, BUS_SCHEDULES]


def _obj(required, props):
    """Build a lenient $jsonSchema object validator (extra fields allowed)."""
    return {
        "$jsonSchema": {
            "bsonType": "object",
            "required": required,
            "properties": props,
        }
    }


_STR   = {"bsonType": "string"}
_NUM   = {"bsonType": ["double", "int", "long"]}
_INT   = {"bsonType": ["int", "long"]}
_ARR   = {"bsonType": "array"}
_STRN  = {"bsonType": ["string", "null"]}   # nullable string (e.g. external_ref)

# ── Validators (only identifying + relational fields are required) ────────────
VALIDATORS = {
    # depots: require ONLY the fields the legacy auth flow already relied on,
    # so existing 5 depot docs remain valid. New master fields are optional.
    DEPOTS: _obj(
        ["phone", "name"],
        {
            "depot_id":   _STR,
            "depot_code": _STR,
            "name":       _STR,
            "phone":      _STR,
            "city":       _STR,
            "district":   _STR,
            "lat":        _NUM,
            "lon":        _NUM,
        },
    ),
    STOPS: _obj(
        ["stop_id", "name"],
        {
            "stop_id":  _STR,
            "name":     _STR,
            "city":     _STR,
            "district": _STR,
            "lat":      _NUM,
            "lon":      _NUM,
            "depot_id": _STRN,
            "is_major": {"bsonType": "bool"},
        },
    ),
    BUSES: _obj(
        ["bus_id", "registration_no", "depot_id", "capacity"],
        {
            "bus_id":          _STR,
            "registration_no": _STR,
            "depot_id":        _STR,
            "type":            _STR,
            "capacity":        _INT,
            "seat_layout":     {"bsonType": "object"},
        },
    ),
    ROUTES: _obj(
        ["route_id", "route_no", "origin_stop_id", "dest_stop_id"],
        {
            "route_id":          _STR,
            "route_no":          _STR,
            "origin_stop_id":    _STR,
            "dest_stop_id":      _STR,
            "distance_km":       _INT,
            "type":              _STR,
            "operating_depot_id": _STRN,
            "stop_sequence":     _ARR,
            "stop_cities":       _ARR,
        },
    ),
    TRIPS: _obj(
        ["trip_id", "route_id", "bus_id", "departure_time"],
        {
            "trip_id":        _STR,
            "route_id":       _STR,
            "bus_id":         _STR,
            "departure_time": _STR,
            "arrival_time":   _STR,
            "duration_min":   _INT,
        },
    ),
    DRIVERS: _obj(
        ["driver_id", "name", "depot_id"],
        {
            "driver_id":   _STR,
            "name":        _STR,
            "employee_no": _STR,
            "depot_id":    _STR,
        },
    ),
    CONDUCTORS: _obj(
        ["conductor_id", "name", "depot_id"],
        {
            "conductor_id": _STR,
            "name":         _STR,
            "employee_no":  _STR,
            "depot_id":     _STR,
        },
    ),
    BUS_SCHEDULES: _obj(
        ["schedule_id", "trip_id", "bus_id"],
        {
            "schedule_id":  _STR,
            "trip_id":      _STR,
            "bus_id":       _STR,
            "driver_id":    _STRN,
            "conductor_id": _STRN,
            "status":       _STR,
        },
    ),
}


def apply_validators(db):
    """
    Apply/refresh JSON-Schema validators on every transport collection.
    Uses collMod when the collection already exists, else creates it with the
    validator. validationLevel="moderate" so pre-existing docs are never
    rejected retroactively (protects the legacy depot docs).
    """
    existing = set(db.list_collection_names())
    for name, validator in VALIDATORS.items():
        if name in existing:
            db.command("collMod", name, validator=validator, validationLevel="moderate")
        else:
            db.create_collection(name, validator=validator, validationLevel="moderate")
