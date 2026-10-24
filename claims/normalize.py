"""
Answer normalization, synonym folding and privacy-preserving fuzzy matching.

Expected answers (the private attributes staff record) are never stored in
clear. Each significant token is folded to a canonical form (case, Unicode NFC,
punctuation, English/Tamil/Tanglish synonyms) and stored as a keyed HMAC, plus
the HMACs of its one-character-deletion variants for longer tokens. Comparing
deletion variants of both sides ("symmetric delete") accepts any single typo —
substitution, insertion, deletion or an adjacent transposition — while the
database only ever holds hashes. Submitted answers are hashed the same way and
discarded; only the pass/fail outcome is kept.
"""
import hashlib
import hmac
import re
import unicodedata

# canonical colour → variants (English spellings, Tamil script, Tanglish)
COLOURS = {
    "red": ["red", "sivappu", "sigappu", "chivappu", "sivapu", "sigapu", "சிவப்பு", "செவப்பு", "maroon"],
    "blue": ["blue", "neelam", "nilam", "neela", "neelamana", "நீலம்", "நீல", "navy"],
    "green": ["green", "pachai", "pacha", "pachchai", "பச்சை"],
    "yellow": ["yellow", "manjal", "manjhal", "manjai", "மஞ்சள்"],
    "black": ["black", "karuppu", "karupu", "karuppa", "kaarupu", "கருப்பு", "கறுப்பு"],
    "white": ["white", "vellai", "vella", "velai", "வெள்ளை"],
    "grey": ["grey", "gray", "saambal", "sambal", "chaambal", "சாம்பல்"],
    "brown": ["brown", "pazhuppu", "palupu", "pazhupu", "பழுப்பு", "kaapi", "coffee"],
    "orange": ["orange", "aaranju", "aranju", "kaavi", "ஆரஞ்சு", "காவி", "saffron"],
    "pink": ["pink", "ilanjivappu", "ilansivappu", "இளஞ்சிவப்பு", "rose"],
    "purple": ["purple", "violet", "oodha", "ootha", "udha", "ஊதா"],
    "silver": ["silver", "velli", "வெள்ளி"],
    "gold": ["gold", "golden", "thangam", "thanga", "தங்கம்", "தங்க"],
}

# canonical object word → variants (single tokens after phrase folding)
OBJECTS = {
    "phone": ["phone", "mobile", "cellphone", "smartphone", "cell", "selpon", "kaipesi", "கைபேசி", "போன்"],
    "wallet": ["wallet", "purse", "parsu", "பர்ஸ்", "pouch"],
    "spectacles": ["spectacles", "specs", "glasses", "kannadi", "கண்ணாடி", "eyeglasses"],
    "keys": ["keys", "key", "saavi", "chaavi", "சாவி", "keychain"],
    "tiffin": ["tiffin", "lunchbox", "dabba", "tiffinbox", "டிபன்"],
    "umbrella": ["umbrella", "kudai", "குடை"],
    "book": ["book", "books", "puthagam", "puthakam", "புத்தகம்", "notebook"],
    "bottle": ["bottle", "flask", "thannibottle", "பாட்டில்"],
    "charger": ["charger", "adapter", "charging"],
    "earphones": ["earphones", "earphone", "headphones", "headset", "earbuds", "airpods"],
    "money": ["money", "cash", "panam", "kaasu", "பணம்", "காசு", "rupees"],
    "towel": ["towel", "thundu", "துண்டு"],
    "clothes": ["clothes", "dress", "shirt", "saree", "sari", "thuni", "துணி", "udai", "உடை"],
    "medicine": ["medicine", "medicines", "tablets", "marundhu", "marunthu", "மருந்து"],
    "laptop": ["laptop", "notebookpc"],
    "photo": ["photo", "photograph", "pic", "picture", "padam", "படம்"],
    "sticker": ["sticker", "stickers", "label"],
    "scratch": ["scratch", "scratches", "scratched", "keeral", "கீறல்"],
}

PHRASES = {
    "lunch box": "tiffin", "tiffin box": "tiffin", "water bottle": "bottle", "thanni bottle": "bottle",
    "power bank": "powerbank", "ear phones": "earphones", "head phones": "earphones",
    "cell phone": "phone", "mobile phone": "phone", "note book": "book",
}

STOPWORDS = {
    "a", "an", "the", "of", "with", "and", "or", "my", "it", "its", "is", "was", "in", "on", "inside", "at",
    "some", "there", "this", "that", "has", "had", "have", "very", "light", "dark", "colour", "color",
    "coloured", "colored", "shade", "niram", "நிறம்", "நிற", "oru", "ஒரு", "matrum", "மற்றும்", "um",
    "irundhadhu", "irunthathu", "இருந்தது", "ulla", "ulle", "உள்ளே", "irukku", "இருக்கு",
}

_VARIANT_TO_CANON = {}
for _canon, _variants in list(COLOURS.items()) + list(OBJECTS.items()):
    for _v in _variants:
        _VARIANT_TO_CANON[unicodedata.normalize("NFC", _v).casefold()] = _canon
COLOUR_SET = frozenset(COLOURS)

_SPACES = re.compile(r"\s+")


def normalize_text(value) -> str:
    """NFC, casefold, punctuation/symbols/emoji → space, collapse whitespace. Keeps Tamil marks."""
    if not isinstance(value, str):
        return ""
    text = unicodedata.normalize("NFC", value).casefold()
    out = []
    for ch in text:
        cat = unicodedata.category(ch)
        if cat[0] in ("L", "M", "N"):
            out.append(ch)
        elif ch in "‌‍":
            continue
        else:
            out.append(" ")
    text = _SPACES.sub(" ", "".join(out)).strip()
    for phrase, canon in PHRASES.items():
        text = re.sub(rf"(?<!\w){re.escape(phrase)}(?!\w)", canon, text)
    return text


def tokens(value) -> list:
    """Significant canonical tokens of an answer or attribute (order kept, duplicates removed)."""
    seen, out = set(), []
    for tok in normalize_text(value).split():
        if tok in STOPWORDS:
            continue
        canon = _VARIANT_TO_CANON.get(tok, tok)
        if canon not in seen:
            seen.add(canon)
            out.append(canon)
    return out


def colour_tokens(value) -> list:
    return [t for t in tokens(value) if t in COLOUR_SET]


def deletions(tok: str) -> set:
    return {tok[:i] + tok[i + 1:] for i in range(len(tok))}


def token_hash(key: bytes, tok: str) -> str:
    return hmac.new(key, ("tok:" + tok).encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def encode_tokens(key: bytes, toks, fuzzy_min: int) -> list:
    """[{h: hash, d: [deletion-variant hashes]}] — the only stored form of an expected answer."""
    entries = []
    for tok in toks:
        entry = {"h": token_hash(key, tok)}
        if len(tok) >= fuzzy_min:
            entry["d"] = sorted(token_hash(key, v) for v in deletions(tok))
        entries.append(entry)
    return entries


def _token_matches(key, answer_tok, expected, fuzzy, fuzzy_min):
    h = token_hash(key, answer_tok)
    if hmac.compare_digest(h, expected["h"]):
        return True
    if not fuzzy or "d" not in expected or len(answer_tok) < fuzzy_min - 1:
        return False
    d = set(expected["d"])
    if h in d:                                            # answer is expected minus one char
        return True
    for v in deletions(answer_tok):                       # answer has one extra/changed char
        hv = token_hash(key, v)
        if hv == expected["h"] or hv in d:
            return True
    return False


def match_ratio(key, expected_entries, answer_toks, fuzzy=True, fuzzy_min=5) -> float:
    """Share of expected tokens matched by distinct answer tokens (0..1)."""
    if not expected_entries or not answer_toks:
        return 0.0
    # Anti-shotgun: listing many words to hit something scores nothing.
    if len(answer_toks) > max(2 * len(expected_entries), len(expected_entries) + 3):
        return 0.0
    used, matched = set(), 0
    for exp in expected_entries:
        for i, tok in enumerate(answer_toks):
            if i not in used and _token_matches(key, tok, exp, fuzzy, fuzzy_min):
                used.add(i)
                matched += 1
                break
    return matched / len(expected_entries)
