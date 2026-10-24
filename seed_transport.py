"""
seed_transport.py — TNSTC Transport Database seeder
====================================================
Builds a realistic, normalized transport network in MongoDB from the official
SETC CSV (data/SETCbustimings_1_0.csv), scaled up with synthetic Tamil Nadu
data to the target sizes:

    depots = 100    stops = 3000    routes = 500    buses = 500
    trips  (from real departure timings)   drivers / conductors (> 500)
    bus_schedules (one per trip)

Follows the same connection pattern as seed_db.py. Reseeding is idempotent AND
non-destructive: every document is upserted on its natural key and reuses the
surrogate id it already had, so existing lost/found reports that reference a
route_id / trip_id / bus_id keep resolving. The existing depot LOGIN accounts
are preserved (phone/name/password kept) so the current auth flow keeps working
— that collection is EXPANDED, not replaced.

Run:  python seed_transport.py
"""

import csv
import os
import re
import random
import hashlib
from datetime import datetime, timedelta

from pymongo import MongoClient, UpdateOne
from dotenv import load_dotenv

import transport_schema as T

load_dotenv()
random.seed(42)  # deterministic, reproducible seed data

CSV_PATH = os.path.join("data", "SETCbustimings_1_0.csv")

# Target sizes
N_DEPOTS = 100
N_STOPS  = 3000
N_ROUTES = 500
N_BUSES  = 500

# Tamil Nadu geographic bounding box (for deterministic fallback coordinates)
TN_LAT = (8.08, 13.50)
TN_LON = (76.23, 80.35)

# ── Curated real coordinates for the well-known cities in the CSV ─────────────
CITY_COORDS = {
    "CHENNAI": (13.0827, 80.2707), "COIMBATORE": (11.0168, 76.9558),
    "MADURAI": (9.9252, 78.1198), "TRICHY": (10.7905, 78.7047),
    "SALEM": (11.6643, 78.1460), "TIRUNELVELI": (8.7139, 77.7567),
    "TUTICORIN": (8.7642, 78.1348), "NAGERCOIL": (8.1780, 77.4285),
    "KANYAKUMARI": (8.0883, 77.5385), "VELLORE": (12.9165, 79.1325),
    "ERODE": (11.3410, 77.7172), "THANJAVUR": (10.7870, 79.1378),
    "DINDIGUL": (10.3624, 77.9695), "KARUR": (10.9601, 78.0766),
    "NAMAKKAL": (11.2189, 78.1677), "DHARMAPURI": (12.1211, 78.1583),
    "HOSUR": (12.7409, 77.8253), "BANGALORE": (12.9716, 77.5946),
    "TIRUPATHI": (13.6288, 79.4192), "PUDUKKOTTAI": (10.3833, 78.8001),
    "PUDUKOTTAI": (10.3833, 78.8001), "KARAIKUDI": (10.0736, 78.7734),
    "SIVAGANGAI": (9.8433, 78.4809), "RAMNAD": (9.3639, 78.8395),
    "VIRUDHUNAGAR": (9.5851, 77.9578), "SIVAKASI": (9.4533, 77.7970),
    "KOVILPATTI": (9.1710, 77.8652), "SANKARANKOIL": (9.1697, 77.5459),
    "RAMESWARAM": (9.2876, 79.3129), "POLLACHI": (10.6588, 77.0088),
    "PALANI": (10.4500, 77.5161), "KODAIKANAL": (10.2381, 77.4892),
    "OOTY": (11.4102, 76.6950), "UDUMALPET": (10.5883, 77.2450),
    "TIRUPPUR": (11.1085, 77.3411), "METTUPALAYAM": (11.2996, 76.9370),
    "VILLUPURAM": (11.9401, 79.4861), "PUDUCHERRY": (11.9416, 79.8083),
    "CHIDAMBARAM": (11.3994, 79.6913), "MAYILADUTHURAI": (11.1018, 79.6552),
    "MYLADUTHURAI": (11.1018, 79.6552), "NAGAPATTINAM": (10.7660, 79.8420),
    "VELANKANNI": (10.6810, 79.8500), "VEDARANYAM": (10.3746, 79.8500),
    "TIRUVARUR": (10.7726, 79.6368), "MANNARKUDI": (10.6667, 79.4500),
    "MANNACHANALLUR": (10.9200, 78.6500), "KUMBAKONAM": (10.9601, 79.3788),
    "PATTUKOTTAI": (10.4250, 79.3190), "ARANTHANGI": (10.1720, 79.0030),
    "DEVAKOTTAI": (9.9470, 78.8230), "PARAMAKUDI": (9.5480, 78.5900),
    "THENI": (10.0104, 77.4768), "CUMBAM": (9.7333, 77.2833),
    "BODI": (10.0100, 77.3500), "PERIYAKULAM": (10.1200, 77.5500),
    "USILAMPATTI": (9.9670, 77.7830), "TIRUMANGALAM": (9.8200, 77.9900),
    "SRIVILLIPUTHUR": (9.5120, 77.6330), "TIRUCHENDUR": (8.4959, 78.1197),
    "MARTHANDAM": (8.3010, 77.2230), "KOLLAM": (8.8932, 76.6141),
    "TRIVANDRUM": (8.5241, 76.9366), "ERNAKULAM": (9.9816, 76.2999),
    "TRISSUR": (10.5276, 76.2144), "GURUVAYUR": (10.5946, 76.0400),
    "KOZHICODE": (11.2588, 75.7804), "KOLLENCODE": (8.2500, 77.2200),
    "KOTTARAKARA": (9.0000, 76.7800), "PATHANAMTHITTA": (9.2648, 76.7870),
    "CHENGANACHERRY": (9.3160, 76.5220), "KUMILI": (9.6000, 77.1600),
    "GOBI": (11.4550, 77.4420), "DHARAPURAM": (10.7350, 77.5320),
    "MELUR": (10.0330, 78.3390), "NATHAM": (10.2270, 78.2260),
    "KALPAKKAM": (12.5200, 80.1750), "TAMBARAM": (12.9249, 80.1000),
    "TIRUVANMIYUR": (12.9830, 80.2590), "AVUDAIYAR KOIL": (10.0330, 78.7500),
    "AVUDAIARKOIL": (10.0330, 78.7500), "ANNAVASAL": (10.4000, 78.7000),
    "ALANKULAM": (8.8670, 77.4830), "ARANI": (12.6700, 79.2900),
    "ARIMALAM": (10.2500, 78.8830), "ARUPPUKOTTAI": (9.5090, 78.0970),
    "KAMUDHI": (9.4000, 78.3700), "SAYALKUDI": (9.1700, 78.4500),
    "KAZHUGUMALAI": (9.1500, 77.6000), "IDAYANKUDI": (8.4000, 77.9000),
    "UDANKUDI": (8.4900, 78.0400), "KULASEKARAPATTINAM": (8.3930, 78.0850),
    "KULASEKARAPATTI": (8.3930, 78.0850), "KULASEKARAM": (8.3000, 77.2500),
    "KARUNGAL": (8.2500, 77.2900), "SHENCOTTAH": (8.9760, 77.2510),
    "GANDARVAKOTTAI": (10.4500, 78.9000), "MANALMELKUDI": (10.0400, 79.2500),
    "KEERAMANGALAM": (10.2000, 78.9000), "PONNAMARAVATHY": (10.2800, 78.6300),
    "TIRUMAYAM": (10.2500, 78.7500), "ILUPPUR": (10.5100, 78.6300),
    "KULATHUR": (10.4000, 78.6000), "PULLAMBADI": (10.9000, 78.8500),
    "SRIRANGAM": (10.8620, 78.6900), "POOMBUHAR": (11.1400, 79.8500),
    "TIRUNALLAR": (10.9260, 79.7900), "TIRUTHURAIPOOND": (10.5300, 79.6400),
    "MEEMISAL": (10.0500, 79.2000), "TONDI": (9.7420, 79.0190),
    "PERIYAPATTINAM": (9.2700, 78.9200), "KEEZHAKARAI": (9.2340, 78.7860),
    "PERAIYUR": (9.7000, 77.7800), "VEDASANDUR": (10.3670, 77.9500),
    "PALLAPATTI": (10.4000, 78.0000), "SATTAN KULAM": (8.4500, 77.9200),
    "THYSAYANVILAI": (8.3300, 77.9000), "ERUVADI": (8.4700, 77.5300),
    "SIVAGANGAI": (9.8433, 78.4809), "GUDALORE": (11.5000, 76.5000),
    "KUMBAKONAM": (10.9601, 79.3788), "TIRUTHANGAL": (9.4930, 77.8390),
}

# Depot code → home city. Inferred from CSV (non-hub argmax) with overrides for
# the Chennai-family / ambiguous codes.
DEPOT_HOME_OVERRIDE = {
    "CB": "CHENNAI", "CC": "CHENNAI", "CA": "CHENNAI",
    "PDY": "PUDUCHERRY", "DGL": "DINDIGUL", "NGP": "NAGAPATTINAM",
}
HUB_CITIES = {"CHENNAI", "BANGALORE", "TIRUPATHI", "TAMBARAM", "PUDUCHERRY"}

# Real district for major depot cities (others default to the city name).
CITY_DISTRICT = {
    "CHENNAI": "Chennai", "COIMBATORE": "Coimbatore", "MADURAI": "Madurai",
    "TRICHY": "Tiruchirappalli", "SALEM": "Salem", "TIRUNELVELI": "Tirunelveli",
    "TUTICORIN": "Thoothukudi", "NAGERCOIL": "Kanniyakumari", "VELLORE": "Vellore",
    "ERODE": "Erode", "THANJAVUR": "Thanjavur", "DINDIGUL": "Dindigul",
    "KARUR": "Karur", "NAMAKKAL": "Namakkal", "DHARMAPURI": "Dharmapuri",
    "HOSUR": "Krishnagiri", "PUDUKKOTTAI": "Pudukkottai", "KARAIKUDI": "Sivagangai",
    "PUDUCHERRY": "Puducherry", "VILLUPURAM": "Viluppuram", "MARTHANDAM": "Kanniyakumari",
    "SHENCOTTAH": "Tenkasi", "NAGAPATTINAM": "Nagapattinam", "KUMBAKONAM": "Thanjavur",
}

# Synthetic TN town pool for extra depots/stops (not necessarily in the CSV).
TN_TOWN_POOL = [
    "Arakkonam", "Ambur", "Vaniyambadi", "Krishnagiri", "Tiruvannamalai",
    "Cuddalore", "Neyveli", "Panruti", "Kallakurichi", "Ariyalur",
    "Perambalur", "Musiri", "Thuraiyur", "Manapparai", "Lalgudi",
    "Rasipuram", "Tiruchengode", "Sankagiri", "Attur", "Mettur",
    "Omalur", "Bhavani", "Sathyamangalam", "Perundurai", "Kangeyam",
    "Palladam", "Valparai", "Sulur", "Avinashi", "Anthiyur",
    "Theni Allinagaram", "Andipatti", "Chinnamanur", "Uthamapalayam",
    "Melur", "Vadipatti", "Sholavandan", "Nilakottai", "Oddanchatram",
    "Batlagundu", "Ottanchatram", "Kariapatti", "Rajapalayam", "Sattur",
    "Watrap", "Srivaikuntam", "Kayalpattinam", "Ettayapuram", "Vilathikulam",
    "Nanguneri", "Ambasamudram", "Tenkasi", "Kadayanallur", "Puliyangudi",
    "Sengottai", "Valliyoor", "Cheranmahadevi", "Palayamkottai", "Melapalayam",
    "Colachel", "Thuckalay", "Boothapandi", "Vilavancode", "Padmanabhapuram",
    "Gingee", "Tindivanam", "Marakkanam", "Vandavasi", "Chetpet",
    "Polur", "Arni", "Cheyyar", "Kanchipuram", "Sriperumbudur",
    "Maraimalai Nagar", "Chengalpattu", "Madurantakam", "Uthiramerur", "Walajabad",
    "Ranipet", "Arcot", "Gudiyatham", "Pernambut", "Tirupathur",
    "Harur", "Palacode", "Pennagaram", "Karimangalam", "Nagercoil South",
    "Manamadurai", "Ilayangudi", "Tirupattur RMD", "Kalaiyarkoil", "Singampunari",
]

LOCALITY_ROOTS = [
    "Anna", "Gandhi", "Nehru", "Bharathi", "Kamaraj", "Periyar", "Sivan",
    "Murugan", "Amman", "Vinayagar", "Meenakshi", "Kaveri", "Vaigai",
    "Thamirabarani", "Kurinji", "Mullai", "Marutham", "Neithal", "Palar",
    "Ponni", "Senthil", "Velan", "Arasu", "Thendral", "Malligai",
]
LOCALITY_SUFFIX = ["Nagar", "Pudur", "Kulam", "Puram", "Patti", "Kudi",
                   "Palayam", "Colony", "Bus Stand", "Junction", "Thoppu", "Medu"]

TAMIL_FIRST = [
    "Arun", "Bala", "Chandran", "Dinesh", "Ezhil", "Ganesan", "Hari",
    "Iniyan", "Jagan", "Karthik", "Lokesh", "Manikandan", "Naveen", "Prabu",
    "Raja", "Saravanan", "Tamil", "Vijay", "Anand", "Murugan", "Selvam",
    "Kumar", "Senthil", "Ravi", "Suresh", "Ramesh", "Mohan", "Gopal",
    "Velan", "Arivu", "Kannan", "Pandian", "Sundar", "Vetri", "Mani",
]
TAMIL_LAST = [
    "Kumar", "Raj", "Murthy", "Krishnan", "Nathan", "Perumal", "Samy",
    "Pillai", "Nadar", "Gounder", "Thevar", "Chettiar", "Mudaliar", "Iyer",
    "Subramanian", "Rajan", "Moorthy", "Durai", "Palani", "Sekar",
]


def canon(city: str) -> str:
    """Normalize a raw CSV city to a canonical uppercase name for lookups."""
    c = re.sub(r"\(.*?\)", "", city)          # drop "(Via ...)", "(OOTY)" etc.
    c = re.split(r"\s+via\s+", c, flags=re.I)[0]
    c = re.split(r"[-/]", c)[0]               # "TRICHY-BHEL" -> "TRICHY"
    c = re.sub(r"\s+", " ", c).strip().upper()
    return c


def coord_for(city: str):
    """Real coordinate if known, else a deterministic point inside the TN box."""
    c = canon(city)
    if c in CITY_COORDS:
        return CITY_COORDS[c]
    h = int(hashlib.md5(c.encode()).hexdigest(), 16)
    lat = TN_LAT[0] + (h % 1000) / 1000 * (TN_LAT[1] - TN_LAT[0])
    lon = TN_LON[0] + ((h // 1000) % 1000) / 1000 * (TN_LON[1] - TN_LON[0])
    return (round(lat, 4), round(lon, 4))


def jitter(lat, lon, km=6.0):
    """Small deterministic-ish offset (~km) for a nearby minor stop."""
    d = km / 111.0
    return (round(lat + random.uniform(-d, d), 4), round(lon + random.uniform(-d, d), 4))


def route_type_from_suffix(route_no: str) -> str:
    m = re.search(r"([A-Z]+)$", route_no.strip().upper())
    suf = m.group(1) if m else "UD"
    return {
        "UD": "ULTRA DELUXE", "AC": "A/C", "TU": "ULTRA", "EU": "EXPRESS ULTRA",
        "AU": "A/C ULTRA", "BU": "ULTRA", "CU": "ULTRA", "KU": "ULTRA",
        "GU": "ULTRA", "CL": "DELUXE", "UK": "ULTRA", "VU": "ULTRA",
    }.get(suf, "ULTRA")


def reg_no(depot_code: str, idx: int) -> str:
    """TN-style registration, e.g. TN-72-N-1234."""
    # md5, NOT hash(): Python randomises string hashing per process, which would
    # change every bus's registration on each run and defeat the stable-id reseed.
    rto = 30 + (int(hashlib.md5(depot_code.encode()).hexdigest(), 16) % 60)   # 30..89
    letter = "ABCDEFGHJKLMNPRSTUVWXYZ"[idx % 22]
    return f"TN-{rto:02d}-{letter}-{1000 + (idx % 9000)}"


def parse_times(raw: str):
    """Split a CSV departure-timing cell into a clean list of HH.MM strings."""
    parts = re.split(r"[,\n]", raw.replace("\r", ""))
    out = []
    for p in parts:
        p = p.strip()
        if re.match(r"^\d{1,2}[.:]\d{0,2}$", p):
            hh, _, mm = p.replace(":", ".").partition(".")
            out.append(f"{int(hh):02d}.{(mm or '0').ljust(2, '0')[:2]}")
    return out


def est_arrival(dep: str, minutes: int) -> str:
    try:
        hh, mm = dep.split(".")
        base = int(hh) * 60 + int(mm)
        end = (base + minutes) % (24 * 60)
        return f"{end // 60:02d}.{end % 60:02d}"
    except Exception:
        return dep


def uid() -> str:
    return hashlib.md5(f"{random.random()}{random.random()}".encode()).hexdigest()


def load_id_map(db, coll: str, id_field: str, key_fields: list) -> dict:
    """
    Map an existing collection's NATURAL key -> its stored surrogate id.
    Used so a reseed reuses ids instead of minting new ones (see `stable`).
    """
    proj = {id_field: 1}
    for k in key_fields:
        proj[k] = 1
    out = {}
    for d in db[coll].find({}, proj):
        if d.get(id_field) is not None:
            out[tuple(d.get(k) for k in key_fields)] = d[id_field]
    return out


def stable(id_map: dict, *key) -> str:
    """
    Return the id already stored for this natural key, else a fresh one.

    This is what makes a reseed non-destructive: live lost/found reports hold
    route_id / trip_id / bus_id references, so regenerating those ids would
    orphan them. Reusing the existing id keeps every reference valid.
    """
    return id_map.get(tuple(key)) or uid()


# ══════════════════════════════════════════════════════════════════════════
def main():
    uri = os.environ.get("MONGO_URI")
    if not uri:
        print("MONGO_URI not found in environment")
        return
    client = MongoClient(uri)
    db = client["tn_bus_lost_found"]

    print("Applying schema validators...")
    T.apply_validators(db)

    # Existing natural-key -> id maps, so this run reuses ids (idempotent reseed).
    id_depot = load_id_map(db, T.DEPOTS, "depot_id", ["depot_code"])
    id_stop  = load_id_map(db, T.STOPS,  "stop_id",  ["name"])
    id_bus   = load_id_map(db, T.BUSES,  "bus_id",   ["registration_no"])
    id_route = load_id_map(db, T.ROUTES, "route_id", ["route_no", "origin_name", "dest_name"])
    id_trip  = load_id_map(db, T.TRIPS,  "trip_id",  ["route_id", "departure_time"])
    id_drv   = load_id_map(db, T.DRIVERS, "driver_id", ["employee_no"])
    id_con   = load_id_map(db, T.CONDUCTORS, "conductor_id", ["employee_no"])
    id_sched = load_id_map(db, T.BUS_SCHEDULES, "schedule_id", ["trip_id"])

    rows = list(csv.DictReader(open(CSV_PATH, encoding="utf-8-sig")))
    print(f"Loaded {len(rows)} SETC service rows from CSV")

    # ── Depot home cities (inference + overrides) ────────────────────────────
    from collections import Counter, defaultdict
    depot_city_counts = defaultdict(Counter)
    for r in rows:
        d = r["Depot"].strip()
        for city in (canon(r["From"]), canon(r["To"])):
            depot_city_counts[d][city] += 1

    real_codes = sorted(depot_city_counts.keys())
    depot_home = {}
    for code in real_codes:
        if code in DEPOT_HOME_OVERRIDE:
            depot_home[code] = DEPOT_HOME_OVERRIDE[code]
        else:
            non_hub = [c for c, _ in depot_city_counts[code].most_common()
                       if c not in HUB_CITIES]
            depot_home[code] = non_hub[0] if non_hub else \
                depot_city_counts[code].most_common(1)[0][0]

    # ── Preserve existing depot login accounts ───────────────────────────────
    existing = {d["phone"]: d for d in db[T.DEPOTS].find()}
    # Map an existing depot (by its 'stop'/'name' city) to a real code so we can
    # reuse its phone/password and keep current logins valid.
    def existing_for_city(city_upper):
        for phone, d in existing.items():
            if canon(d.get("stop", "")) == city_upper or canon(d.get("name", "")) == city_upper:
                return d
        return None

    # ── Build 100 depots: 22 real codes + synthetic fill ─────────────────────
    depots = []
    depot_by_code = {}
    phone_seq = 9000001000
    used_phones = set(existing.keys())

    def next_phone():
        nonlocal phone_seq
        phone_seq += 1
        while str(phone_seq) in used_phones:
            phone_seq += 1
        used_phones.add(str(phone_seq))
        return str(phone_seq)

    for code in real_codes:
        home = depot_home[code]
        lat, lon = coord_for(home)
        prior = existing_for_city(home)
        phone = prior["phone"] if prior else next_phone()
        password = prior.get("password", "pass123") if prior else "pass123"
        name = prior.get("name") if prior else f"{home.title()} Depot"
        doc = {
            "depot_id": stable(id_depot, code), "depot_code": code, "name": name,
            "phone": phone, "password": password,
            "city": home.title(), "district": CITY_DISTRICT.get(home, home.title()),
            "lat": lat, "lon": lon, "stop": home.title(),
            "routes": prior.get("routes", []) if prior else [],
            "is_staffed": True, "source": "setc_csv", "external_ref": None,
        }
        depots.append(doc)
        depot_by_code[code] = doc

    # Synthetic depots to reach 100
    town_i = 0
    while len(depots) < N_DEPOTS:
        town = TN_TOWN_POOL[town_i % len(TN_TOWN_POOL)]
        town_i += 1
        lat, lon = coord_for(town)
        code = f"SY{len(depots):03d}"
        doc = {
            "depot_id": stable(id_depot, code), "depot_code": code, "name": f"{town} Depot",
            "phone": next_phone(), "password": "pass123",
            "city": town, "district": town, "lat": lat, "lon": lon, "stop": town,
            "routes": [], "is_staffed": False, "source": "synthetic",
            "external_ref": None,
        }
        depots.append(doc)
        depot_by_code[code] = doc

    # ── Stops: 137 real-city majors + synthetic minors to reach 3000 ─────────
    major_cities = sorted({canon(r[k]) for r in rows for k in ("From", "To")})
    # include depot home cities & synthetic towns as majors too
    for d in depots:
        major_cities.append(canon(d["city"]))
    major_cities = sorted(set(major_cities))

    stops = []
    stop_by_city = {}   # canonical major city -> stop doc
    for city in major_cities:
        lat, lon = coord_for(city)
        doc = {
            "stop_id": stable(id_stop, city.title()), "name": city.title(), "city": city.title(),
            "district": CITY_DISTRICT.get(city, city.title()),
            "lat": lat, "lon": lon, "depot_id": None, "is_major": True,
            "source": "setc_csv" if city in {canon(r[k]) for r in rows for k in ("From", "To")} else "synthetic",
            "external_ref": None,
        }
        stops.append(doc)
        stop_by_city[city] = doc

    # nearest depot for each major stop (simple squared-distance)
    def nearest_depot(lat, lon):
        best, bestd = None, 1e18
        for d in depots:
            dd = (d["lat"] - lat) ** 2 + (d["lon"] - lon) ** 2
            if dd < bestd:
                best, bestd = d, dd
        return best["depot_id"]

    for s in stops:
        s["depot_id"] = nearest_depot(s["lat"], s["lon"])

    # synthetic minor stops around major-city anchors until 3000
    anchor_cycle = [c for c in major_cities]
    ai = 0
    minor_idx = 0
    while len(stops) < N_STOPS:
        anchor = anchor_cycle[ai % len(anchor_cycle)]
        ai += 1
        a = stop_by_city[anchor]
        root = LOCALITY_ROOTS[minor_idx % len(LOCALITY_ROOTS)]
        suf = LOCALITY_SUFFIX[(minor_idx // len(LOCALITY_ROOTS)) % len(LOCALITY_SUFFIX)]
        minor_idx += 1
        lat, lon = jitter(a["lat"], a["lon"])
        minor_name = f"{root} {suf} ({anchor.title()})"
        stops.append({
            "stop_id": stable(id_stop, minor_name),
            "name": minor_name,
            "city": anchor.title(), "district": a["district"],
            "lat": lat, "lon": lon, "depot_id": a["depot_id"],
            "is_major": False, "source": "synthetic", "external_ref": None,
        })

    # pool of minor stops grouped by anchor city (for route intermediates)
    minors_by_city = defaultdict(list)
    for s in stops:
        if not s["is_major"]:
            minors_by_city[canon(s["city"])].append(s)

    # ── Routes: dedupe CSV services, then synthetic fill to 500 ──────────────
    def intermediates(o_stop, d_stop, k=4):
        """
        Pick up to k major stops that lie ALONG the origin→dest corridor.

        Each candidate is projected onto the origin→dest line: `t` is how far
        along the line it falls (0 at origin, 1 at dest) and `perp` is its
        perpendicular distance from the line. We keep only cities that are
        genuinely between the endpoints (0.05 < t < 0.95) and near the corridor
        (perp within ~0.6°, ≈65 km), then spread the picks evenly along `t` so
        the sequence is ordered and doesn't clump near one end. Projection —
        rather than raw distance-to-origin — is what prevents long routes from
        collecting a cluster of stops beside the origin and then jumping to the
        destination (which made the drawn road path appear to stop halfway).
        """
        olat, olon = o_stop["lat"], o_stop["lon"]
        dlat, dlon = d_stop["lat"], d_stop["lon"]
        vlat, vlon = dlat - olat, dlon - olon
        vlen2 = vlat * vlat + vlon * vlon
        if vlen2 == 0:
            return []
        cands = []
        for city, s in stop_by_city.items():
            if s["stop_id"] in (o_stop["stop_id"], d_stop["stop_id"]):
                continue
            t = ((s["lat"] - olat) * vlat + (s["lon"] - olon) * vlon) / vlen2
            if t <= 0.05 or t >= 0.95:
                continue   # not between origin and destination
            proj_lat, proj_lon = olat + t * vlat, olon + t * vlon
            perp = ((s["lat"] - proj_lat) ** 2 + (s["lon"] - proj_lon) ** 2) ** 0.5
            if perp > 0.6:
                continue   # too far off the corridor
            cands.append((t, s))
        cands.sort(key=lambda x: x[0])   # order ALONG the route
        if len(cands) <= k:
            return [s for _, s in cands]
        # spread picks evenly along the corridor instead of taking the first k
        step = len(cands) / k
        return [cands[int(i * step)][1] for i in range(k)]

    def build_sequence(o_city, d_city):
        o = stop_by_city[o_city]
        d = stop_by_city[d_city]
        seq = [o] + intermediates(o, d) + [d]
        # sprinkle one minor stop near a random intermediate for texture
        if len(seq) > 2 and minors_by_city.get(canon(seq[1]["city"])):
            seq.insert(2, random.choice(minors_by_city[canon(seq[1]["city"])]))
        # dedupe preserving order
        seen, ordered = set(), []
        for s in seq:
            if s["stop_id"] not in seen:
                seen.add(s["stop_id"]); ordered.append(s)
        return [s["stop_id"] for s in ordered]

    routes = []
    seen_routes = set()
    route_services = {}   # route_id -> No.of Service (frequency)
    for r in rows:
        rn = r["Route No."].strip().upper()
        o_city, d_city = canon(r["From"]), canon(r["To"])
        key = (rn, o_city, d_city)
        if key in seen_routes or o_city == d_city:
            continue
        if o_city not in stop_by_city or d_city not in stop_by_city:
            continue
        seen_routes.add(key)
        try:
            dist = int(r["Route Length"])
        except Exception:
            dist = 300
        try:
            svc = max(1, int(r["No.of Service"]))
        except Exception:
            svc = 1
        code = r["Depot"].strip()
        doc = {
            "route_id": stable(id_route, rn, o_city.title(), d_city.title()), "route_no": rn,
            "origin_stop_id": stop_by_city[o_city]["stop_id"],
            "dest_stop_id": stop_by_city[d_city]["stop_id"],
            "origin_name": o_city.title(), "dest_name": d_city.title(),
            "via": (r["To"] if "Via" in r["To"] or "via" in r["To"] else "").strip(),
            "distance_km": dist, "type": route_type_from_suffix(rn),
            "operating_depot_id": depot_by_code.get(code, depots[0])["depot_id"],
            "operating_depot_code": code,
            "stop_sequence": build_sequence(o_city, d_city),
            "departure_raw": r["Departure Timings"], "num_services": svc,
            "source": "setc_csv", "external_ref": None,
        }
        routes.append(doc)
        route_services[doc["route_id"]] = svc
        if len(routes) >= N_ROUTES:
            break

    # synthetic routes to reach 500 (random major city pairs)
    majors_list = [c for c in stop_by_city]
    syn_i = 0
    while len(routes) < N_ROUTES:
        o_city = majors_list[syn_i % len(majors_list)]
        d_city = majors_list[(syn_i * 7 + 3) % len(majors_list)]
        syn_i += 1
        if o_city == d_city:
            continue
        o, d = stop_by_city[o_city], stop_by_city[d_city]
        dist = int(((o["lat"] - d["lat"]) ** 2 + (o["lon"] - d["lon"]) ** 2) ** 0.5 * 111) + 40
        rn = f"{700 + len(routes)}SY"
        code = random.choice(real_codes)
        doc = {
            "route_id": stable(id_route, rn, o_city.title(), d_city.title()), "route_no": rn,
            "origin_stop_id": o["stop_id"], "dest_stop_id": d["stop_id"],
            "origin_name": o_city.title(), "dest_name": d_city.title(), "via": "",
            "distance_km": dist, "type": "ULTRA",
            "operating_depot_id": depot_by_code[code]["depot_id"],
            "operating_depot_code": code,
            "stop_sequence": build_sequence(o_city, d_city),
            "departure_raw": "", "num_services": 1,
            "source": "synthetic", "external_ref": None,
        }
        routes.append(doc)
        route_services[doc["route_id"]] = 1

    # Denormalized lowercase stop cities per route. Lets depot_serves_route()
    # and the depot_only route filter answer "does this route call at the depot's
    # city?" with one indexable query instead of resolving every stop document.
    city_by_stop_id = {s["stop_id"]: (s.get("city") or "").strip().lower()
                       for s in stops}
    for rt in routes:
        rt["stop_cities"] = sorted(
            {city_by_stop_id.get(sid, "") for sid in rt["stop_sequence"]} - {""}
        )

    # ── Buses: distribute 500 across depots weighted by routes operated ──────
    routes_by_depot = defaultdict(list)
    for rt in routes:
        routes_by_depot[rt["operating_depot_id"]].append(rt)

    operating_depots = [d for d in depots if routes_by_depot[d["depot_id"]]]
    # base 1 bus each, then distribute remainder by route-count weight
    bus_alloc = {d["depot_id"]: 1 for d in operating_depots}
    remaining = N_BUSES - len(operating_depots)
    total_routes = sum(len(routes_by_depot[d["depot_id"]]) for d in operating_depots)
    for d in operating_depots:
        share = int(remaining * len(routes_by_depot[d["depot_id"]]) / max(1, total_routes))
        bus_alloc[d["depot_id"]] += share
    # fix rounding drift to hit exactly 500
    drift = N_BUSES - sum(bus_alloc.values())
    for d in operating_depots[:abs(drift)]:
        bus_alloc[d["depot_id"]] += 1 if drift > 0 else -1

    buses = []
    buses_by_depot = defaultdict(list)
    for d in operating_depots:
        for i in range(bus_alloc[d["depot_id"]]):
            reg = reg_no(d["depot_code"], len(buses))
            doc = {
                "bus_id": stable(id_bus, reg), "registration_no": reg,
                "depot_id": d["depot_id"], "depot_code": d["depot_code"],
                "type": "ULTRA", "capacity": 0, "seat_layout": {},
                "model": random.choice(["Ashok Leyland Viking", "TATA LPO 1618",
                                        "Ashok Leyland Cheetah", "Volvo B9R"]),
                "year": random.randint(2016, 2024),
                "_routes": [], "_max_dist": 0, "_trips": 0,
                "source": "synthetic", "external_ref": None,
            }
            buses.append(doc)
            buses_by_depot[d["depot_id"]].append(doc)

    # ── Trips: expand real departure timings; assign buses round-robin ───────
    trips = []
    rr = defaultdict(int)   # depot_id -> round-robin cursor
    for rt in routes:
        times = parse_times(rt["departure_raw"]) or ["21.00"]
        dep_id = rt["operating_depot_id"]
        fleet = buses_by_depot.get(dep_id) or buses
        duration = int(rt["distance_km"] / 45 * 60)   # ~45 km/h
        seen_times = set()
        for tstr in times:
            if tstr in seen_times:
                continue          # same route cannot depart twice at one time
            seen_times.add(tstr)
            bus = fleet[rr[dep_id] % len(fleet)]
            rr[dep_id] += 1
            trip = {
                "trip_id": stable(id_trip, rt["route_id"], tstr), "route_id": rt["route_id"], "bus_id": bus["bus_id"],
                "route_no": rt["route_no"], "departure_time": tstr,
                "arrival_time": est_arrival(tstr, duration), "duration_min": duration,
                "days_of_week": "DAILY", "source": rt["source"], "external_ref": None,
            }
            trips.append(trip)
            bus["_routes"].append(rt["route_id"])
            bus["_max_dist"] = max(bus["_max_dist"], rt["distance_km"])
            bus["_trips"] += 1
            bus["type"] = rt["type"]   # last route's type; fine for demo

    # ── Intelligent seat capacity (longer routes & busier buses → more seats) ─
    def seat_layout(cap, pattern):
        if pattern == "2+1":
            cols = 3
            rows = -(-cap // cols)     # ceil
            return {"pattern": "2+1", "cols": cols, "rows": rows,
                    "aisle_after": 2, "back_row": 0, "capacity": cap}
        cols = 4
        back = 5 if cap >= 30 else 0
        rows = -(-(cap - back) // cols)
        return {"pattern": "2+2", "cols": cols, "rows": rows,
                "aisle_after": 2, "back_row": back, "capacity": cap}

    for b in buses:
        md = b["_max_dist"]
        busy = b["_trips"] >= 4
        premium = b["type"] in ("A/C", "A/C ULTRA")
        if md > 500:
            if premium:
                cap, pattern = 30, "2+1"          # long-haul A/C sleeper-style
            else:
                cap, pattern = 54, "2+2"          # long-haul seater
        elif md >= 300:
            cap, pattern = (54 if busy else 51), "2+2"
        elif md > 0:
            cap, pattern = (54 if busy else 40), "2+2"
        else:
            cap, pattern = 40, "2+2"              # bus not assigned any route
        b["capacity"] = cap
        b["seat_layout"] = seat_layout(cap, pattern)
        for k in ("_routes", "_max_dist", "_trips"):
            b.pop(k, None)

    # ── Drivers & conductors (~1.3 per bus), per depot ───────────────────────
    def make_person(depot, kind, idx):
        name = f"{random.choice(TAMIL_FIRST)} {random.choice(TAMIL_LAST)}"
        base = {
            "name": name, "employee_no": f"TN{kind[0].upper()}{100000 + idx}",
            "depot_id": depot["depot_id"], "depot_code": depot["depot_code"],
            "phone": f"9{random.randint(100000000, 999999999)}",
            "source": "synthetic", "external_ref": None,
        }
        return base

    drivers, conductors = [], []
    di = ci = 0
    for d in operating_depots:
        n = max(1, int(len(buses_by_depot[d["depot_id"]]) * 1.3))
        for _ in range(n):
            drv = make_person(d, "driver", di); drv["driver_id"] = stable(id_drv, drv["employee_no"])
            drv["license_no"] = f"TN{random.randint(10, 99)}{random.randint(10**9, 10**10 - 1)}"
            drivers.append(drv); di += 1
            con = make_person(d, "conductor", ci); con["conductor_id"] = stable(id_con, con["employee_no"])
            conductors.append(con); ci += 1

    drivers_by_depot = defaultdict(list)
    conductors_by_depot = defaultdict(list)
    for d in drivers:
        drivers_by_depot[d["depot_id"]].append(d)
    for c in conductors:
        conductors_by_depot[c["depot_id"]].append(c)

    # ── Bus schedules: one per trip linking trip+bus+driver+conductor ────────
    bus_by_id = {b["bus_id"]: b for b in buses}
    schedules = []
    for tp in trips:
        dep_id = bus_by_id[tp["bus_id"]]["depot_id"]
        drv = random.choice(drivers_by_depot[dep_id]) if drivers_by_depot[dep_id] else None
        con = random.choice(conductors_by_depot[dep_id]) if conductors_by_depot[dep_id] else None
        schedules.append({
            "schedule_id": stable(id_sched, tp["trip_id"]), "trip_id": tp["trip_id"], "bus_id": tp["bus_id"],
            "driver_id": drv["driver_id"] if drv else None,
            "conductor_id": con["conductor_id"] if con else None,
            "effective_from": "2026-07-01", "status": "active",
            "source": "synthetic", "external_ref": None,
        })

    # ── Persist (idempotent reseed) ──────────────────────────────────────────
    # Upsert on the NATURAL key and reuse existing surrogate ids (see `stable`),
    # so re-running this seeder never orphans live lost/found reports that
    # reference route_id / trip_id / bus_id. $set (rather than a full replace)
    # also preserves runtime-added fields such as a route's cached road_geometry.
    def upsert_all(coll, docs, key_fields, label):
        # Guard against two generated docs sharing one natural key.
        uniq, seen = [], set()
        for d in docs:
            k = tuple(d.get(f) for f in key_fields)
            if k in seen:
                continue
            seen.add(k)
            uniq.append(d)

        # Collapse any pre-existing duplicates FIRST, so each natural key maps to
        # exactly one document (an upsert alone cannot remove a duplicate that
        # shares its key with the doc being updated).
        dup_ids, first_seen = [], set()
        for x in db[coll].find({}, {f: 1 for f in key_fields}):
            k = tuple(x.get(f) for f in key_fields)
            if k in first_seen:
                dup_ids.append(x["_id"])
            else:
                first_seen.add(k)
        if dup_ids:
            db[coll].delete_many({"_id": {"$in": dup_ids}})

        if uniq:
            db[coll].bulk_write(
                [UpdateOne({f: d.get(f) for f in key_fields}, {"$set": d}, upsert=True)
                 for d in uniq],
                ordered=False
            )

        # Converge the collection to what this run generated.
        stale = [x["_id"] for x in db[coll].find({}, {f: 1 for f in key_fields})
                 if tuple(x.get(f) for f in key_fields) not in seen]
        if stale:
            db[coll].delete_many({"_id": {"$in": stale}})

        dropped = len(docs) - len(uniq)
        extra = []
        if dropped:
            extra.append(f"dup keys skipped: {dropped}")
        if dup_ids:
            extra.append(f"existing dupes collapsed: {len(dup_ids)}")
        if stale:
            extra.append(f"stale removed: {len(stale)}")
        print(f"  {label:14} {len(uniq)}" + (f"   ({', '.join(extra)})" if extra else ""))

    print("Seeding collections:")
    upsert_all(T.DEPOTS, depots, ["depot_code"], "depots")
    upsert_all(T.STOPS, stops, ["name"], "stops")
    upsert_all(T.ROUTES, routes, ["route_no", "origin_name", "dest_name"], "routes")
    upsert_all(T.BUSES, buses, ["registration_no"], "buses")
    upsert_all(T.TRIPS, trips, ["route_id", "departure_time"], "trips")
    upsert_all(T.DRIVERS, drivers, ["employee_no"], "drivers")
    upsert_all(T.CONDUCTORS, conductors, ["employee_no"], "conductors")
    upsert_all(T.BUS_SCHEDULES, schedules, ["trip_id"], "bus_schedules")

    # helpful indexes for the API lookups
    db[T.ROUTES].create_index("route_id")
    db[T.ROUTES].create_index([("origin_name", 1), ("dest_name", 1)])
    db[T.TRIPS].create_index("route_id")
    db[T.TRIPS].create_index("trip_id")
    db[T.BUSES].create_index("bus_id")
    db[T.STOPS].create_index("stop_id")

    print("\nDepot home-city mapping (real codes):")
    for code in real_codes:
        print(f"  {code:5} -> {depot_by_code[code]['city']}")
    print("\nDone.")


if __name__ == "__main__":
    main()
