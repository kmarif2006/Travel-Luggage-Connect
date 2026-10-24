"""
Private attributes of found items and adaptive question selection.

Staff record attributes ONLY while logging the item. They are encoded straight
away (see normalize.py) and the raw values are discarded, so no screen, API or
database field can reveal them later. An attribute whose words also appear in
the item's public description (which is sent in the Feature 2 notification) is
flagged `leaked` and is never asked.
"""
import secrets

from .normalize import colour_tokens, encode_tokens, tokens

CATEGORIES = ("bag", "wallet", "phone", "laptop", "spectacles", "umbrella", "documents", "clothing", "other")
ALL = frozenset(CATEGORIES)

ATTRIBUTES = {
    "zipper_tag_colour": {"kind": "colour", "weight": 1.0, "label": "Zipper pull / tag colour",
                          "question": "What colour is the zipper pull or the tag on it?",
                          "categories": frozenset({"bag", "wallet", "clothing"})},
    "content_item": {"kind": "text", "weight": 1.5, "label": "One item inside",
                     "question": "Name one thing that was inside it.",
                     "categories": frozenset({"bag", "wallet", "documents", "other"})},
    "brand": {"kind": "text", "weight": 1.0, "label": "Brand / make",
              "question": "What brand or make is it?", "categories": ALL},
    "distinguishing_mark": {"kind": "text", "weight": 1.5, "label": "Sticker, scratch or mark",
                            "question": "Describe a sticker, scratch, name tag or other mark on it.",
                            "categories": ALL},
    "lock_screen": {"kind": "text", "weight": 1.5, "label": "Lock-screen wallpaper",
                    "question": "What is shown on the lock-screen wallpaper?",
                    "categories": frozenset({"phone", "laptop"})},
    "case_colour": {"kind": "colour", "weight": 1.0, "label": "Case / cover colour",
                    "question": "What colour is its case or cover?",
                    "categories": frozenset({"phone", "laptop", "spectacles", "umbrella"})},
}

MIN_QUESTIONS = 2
ROUND_QUESTIONS = 3
MAX_RAW_CHARS = 80
_rng = secrets.SystemRandom()


def applicable(category: str):
    return [k for k, a in ATTRIBUTES.items() if category in a["categories"]]


def encode_private_attributes(raw: dict, category: str, public_description: str, key: bytes,
                              fuzzy_min: int) -> dict:
    """Raw staff input → stored hashed form. Unknown keys and inapplicable attributes are dropped."""
    category = category if category in CATEGORIES else "other"
    public = set(tokens(public_description))
    encoded = {}
    for name in applicable(category):
        value = raw.get(name)
        if not isinstance(value, str) or not value.strip():
            continue
        value = value.strip()[:MAX_RAW_CHARS]
        spec = ATTRIBUTES[name]
        toks = (colour_tokens(value) or tokens(value)) if spec["kind"] == "colour" else tokens(value)
        if not toks:
            continue
        encoded[name] = {
            "kind": spec["kind"], "weight": spec["weight"],
            "entries": encode_tokens(key, toks, fuzzy_min),
            "leaked": bool(public & set(toks)),
        }
    return encoded


def askable(item) -> list:
    category = item.get("category") or "other"
    attrs = item.get("private_attributes") or {}
    return [k for k in applicable(category) if k in attrs and not attrs[k].get("leaked")]


def select_questions(item, previous=()):
    """
    Choose this round's questions (2-3) plus one reserve for a borderline score.
    Varies across rounds and claims (CSPRNG shuffle), preferring questions not
    asked in the previous round. Returns (round_ids, reserve_id_or_None), or
    ([], None) when the item has too few askable attributes.
    """
    pool = askable(item)
    if len(pool) < MIN_QUESTIONS:
        return [], None
    _rng.shuffle(pool)
    ordered = [q for q in pool if q not in previous] + [q for q in pool if q in previous]
    # 2 askable → 2 questions, no reserve; 3 → 2 + reserve; 4+ → 3 + reserve.
    count = ROUND_QUESTIONS if len(ordered) > ROUND_QUESTIONS else MIN_QUESTIONS
    reserve = ordered[count] if len(ordered) > count else None
    return ordered[:count], reserve


def question_view(qid: str) -> dict:
    return {"id": qid, "text": ATTRIBUTES[qid]["question"]}
