"""Unit tests for Feature 3: normalization, fuzzy/synonym scoring, questions, trust, tokens, state machine, txn."""
import json
import threading

import mongomock
import pytest

from claims import statemachine as sm
from claims.attributes import ATTRIBUTES, encode_private_attributes, select_questions
from claims.config import ClaimsConfig
from claims.normalize import encode_tokens, match_ratio, normalize_text, tokens
from claims.scoring import BORDERLINE, FAIL, VERIFIED, answer_passes, decide, trust_score
from claims.tokens import (ALPHABET, TokenError, code_hash, derive_code, format_code, new_nonce, new_tag_id,
                           normalize_code, normalize_tag, sign_payload, verify_payload)
from claims.txn import ConflictAbort, Tx, run_atomically, supports_transactions
from conftest import BAG_TRUTH
from passenger_auth.config import ConfigError

KEY = b"k" * 32
CFG = ClaimsConfig()


def _attr(kind, value, category="bag", public="Black backpack"):
    name = {"colour": "zipper_tag_colour", "text": "distinguishing_mark"}[kind]
    return encode_private_attributes({name: value}, category, public, KEY, CFG.fuzzy_min_token_length)[name]


def passes(kind, expected, answer, strict=False):
    return answer_passes(KEY, _attr(kind, expected), answer, CFG, strict)


# ── Normalization ────────────────────────────────────────────────────────────
def test_normalize_case_whitespace_punctuation_emoji():
    assert normalize_text("  Dragon-Ball   STICKER!!! 😀🐉 ") == "dragon ball sticker"


def test_normalize_keeps_tamil_vowel_signs_and_nfc():
    import unicodedata
    decomposed = unicodedata.normalize("NFD", "மஞ்சள்")
    assert normalize_text(decomposed) == "மஞ்சள்"


def test_tokens_drop_stopwords_and_fold_phrases():
    assert tokens("A steel lunch box with the lid") == ["steel", "tiffin", "lid"]
    assert tokens("oru manjal niram") == ["yellow"]


@pytest.mark.parametrize("answer", ["yellow", "Yellow!", "manjal", "MANJAL", "மஞ்சள்", "manjal niram", "it's yellow"])
def test_colour_synonyms_english_tamil_tanglish(answer):
    assert passes("colour", "yellow", answer)


@pytest.mark.parametrize("answer", ["yelow", "yelloww", "yeIlow"])
def test_colour_typos_within_tolerance(answer):
    assert passes("colour", "Yellow", answer)


def test_colour_wrong_or_shotgun_fails():
    assert not passes("colour", "yellow", "blue")
    assert not passes("colour", "yellow", "red blue green yellow black")


@pytest.mark.parametrize("answer", ["Chennai Super Kings sticker", "CSK sticker super kings", "super kings",
                                    "chennai superkings stiker", "sticker of chennai super kings"])
def test_text_matching_partial_and_typos(answer):
    assert passes("text", "Chennai Super Kings sticker", answer)


@pytest.mark.parametrize("answer", ["Mumbai Indians", "dragon", "", "😀😀😀", "<script>alert(1)</script>",
                                    "{'$ne': null}", "a b c d e f g h i j k l m n o p q chennai"])
def test_text_wrong_empty_emoji_injection_and_shotgun_fail(answer):
    assert not passes("text", "Chennai Super Kings sticker", answer)


def test_strict_mode_disallows_typos():
    assert passes("text", "Wildcraft", "wildcrafts")
    assert not passes("text", "Wildcraft", "wildcrafts", strict=True)
    assert passes("text", "Wildcraft", "wildcraft", strict=True)


def test_object_synonyms_in_three_languages():
    for answer in ("tiffin", "lunch box", "dabba", "steel tiffin"):
        assert passes("text", "steel tiffin box", answer), answer
    assert passes("text", "my spectacles", "கண்ணாடி")
    assert passes("text", "my spectacles", "kannadi")


def test_short_tokens_are_exact_only():
    assert not passes("text", "red cap", "rod cop")


def test_stored_form_contains_no_raw_answer():
    attrs = encode_private_attributes(BAG_TRUTH, "bag", "Black backpack", KEY, 5)
    dumped = json.dumps(attrs).lower()
    for raw in ("yellow", "tiffin", "wildcraft", "chennai", "sticker", "steel"):
        assert raw not in dumped


def test_match_ratio_uses_each_answer_token_once():
    entries = encode_tokens(KEY, ["red", "red2"], 5)
    assert match_ratio(KEY, entries, ["red"]) == 0.5


# ── Question selection ───────────────────────────────────────────────────────
def _item(category, truth, public="Black backpack"):
    return {"category": category,
            "private_attributes": encode_private_attributes(truth, category, public, KEY, 5)}


def test_questions_vary_by_category():
    phone = _item("phone", {"lock_screen": "Murugan temple", "case_colour": "blue", "brand": "Redmi",
                            "zipper_tag_colour": "red", "content_item": "sim pin"})
    chosen, reserve = select_questions(phone)
    assert set(chosen + [reserve]) <= {"lock_screen", "case_colour", "brand"}   # bag-only ones never asked
    assert len(chosen) == 2 and reserve is not None


def test_question_count_rules():
    assert select_questions(_item("bag", {"brand": "x1y"}))[0] == []                    # < 2 → none
    two, reserve = select_questions(_item("bag", {"brand": "Wildcraft", "content_item": "tiffin"}))
    assert len(two) == 2 and reserve is None
    four, reserve = select_questions(_item("bag", BAG_TRUTH))
    assert len(four) == 3 and reserve is not None and reserve not in four


def test_attributes_visible_in_notification_text_are_never_asked():
    item = _item("bag", BAG_TRUTH, public="Wildcraft backpack with yellow tag")
    for _ in range(30):
        chosen, reserve = select_questions(item)
        assert "brand" not in chosen + [reserve] and "zipper_tag_colour" not in chosen + [reserve]


def test_next_round_prefers_unasked_questions():
    item = _item("bag", BAG_TRUTH)
    first, _ = select_questions(item)
    second, _ = select_questions(item, previous=first)
    assert set(second) != set(first)


def test_selection_is_randomised():
    item = _item("bag", BAG_TRUTH)
    assert len({tuple(select_questions(item)[0]) for _ in range(40)}) > 1


# ── Trust score ──────────────────────────────────────────────────────────────
def test_trust_score_is_weighted():
    w = {q: ATTRIBUTES[q]["weight"] for q in ("brand", "content_item", "distinguishing_mark")}   # 1, 1.5, 1.5
    assert trust_score({"brand": True, "content_item": True, "distinguishing_mark": True}, w) == 1.0
    assert trust_score({"brand": False, "content_item": True, "distinguishing_mark": True}, w) == 0.75
    assert trust_score({"brand": True, "content_item": True, "distinguishing_mark": False}, w) == 0.625
    assert trust_score({"brand": True, "content_item": False, "distinguishing_mark": False}, w) == 0.25


@pytest.mark.parametrize("score,verdict", [(1.0, VERIFIED), (0.75, VERIFIED), (0.7499, BORDERLINE),
                                           (0.5, BORDERLINE), (0.4999, FAIL), (0.0, FAIL)])
def test_threshold_boundaries(score, verdict):
    assert decide(score, CFG) == verdict


def test_config_validation():
    with pytest.raises(ConfigError):
        ClaimsConfig(trust_threshold=0.5, trust_review_min=0.6)
    with pytest.raises(ConfigError):
        ClaimsConfig.from_env({"CLAIM_MAX_ROUNDS": "0"})
    cfg = ClaimsConfig.from_env({"TRUST_THRESHOLD": "0.8", "CLAIM_TOKEN_TTL_DAYS": "3"})
    assert cfg.trust_threshold == 0.8 and cfg.token_ttl_days == 3 and cfg.max_rounds == 3


# ── Tokens ───────────────────────────────────────────────────────────────────
def test_code_alphabet_and_format():
    assert not set("0O1I") & set(ALPHABET) and len(ALPHABET) == 32
    code = derive_code(KEY, "claim1", new_nonce())
    assert len(code) == 8 and set(code) <= set(ALPHABET)
    assert len(format_code(code)) == 9 and format_code(code)[4] == "-"


def test_code_derivation_is_keyed_and_nonce_dependent():
    nonce = new_nonce()
    assert derive_code(KEY, "c", nonce) == derive_code(KEY, "c", nonce)
    assert derive_code(KEY, "c", nonce) != derive_code(KEY, "c", new_nonce())
    assert derive_code(KEY, "c", nonce) != derive_code(b"x" * 32, "c", nonce)
    codes = {derive_code(KEY, "c", new_nonce()) for _ in range(500)}
    assert len(codes) == 500


@pytest.mark.parametrize("raw,ok", [("a7x2-k9p4", True), ("A7X2K9P4", True), (" A7X2 K9P4 ", True),
                                    ("A7X2-K9P0", False), ("A7X2-K9PO", False), ("A7X2-K9P", False),
                                    ("A7X2-K9P1", False), (None, False), ("😀😀😀😀-😀😀😀😀", False)])
def test_normalize_code(raw, ok):
    if ok:
        assert normalize_code(raw) == "A7X2K9P4"
    else:
        with pytest.raises(TokenError):
            normalize_code(raw)


def test_code_hash_is_keyed():
    assert code_hash(KEY, "A7X2K9P4") != code_hash(b"y" * 32, "A7X2K9P4")
    assert "A7X2K9P4" not in code_hash(KEY, "A7X2K9P4")


def test_signed_payload_roundtrip_and_tamper_detection():
    payload = {"v": 1, "cid": "c1", "iid": "i1", "tag": "TAG-ABCDEFGH", "pnr": "T26100101", "exp": 2000000000,
               "code": "A7X2K9P4"}
    signed = sign_payload(KEY, payload)
    assert verify_payload(KEY, signed) == payload
    head, body, sig = signed.split(".")
    import base64
    forged = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    forged["iid"] = "i2"
    forged_body = base64.urlsafe_b64encode(json.dumps(forged).encode()).rstrip(b"=").decode()
    for bad, reason in [(f"{head}.{forged_body}.{sig}", "bad_signature"), (f"{head}.{body}.{sig[:-2]}AA", "bad_signature"),
                        ("TNSTC2." + body + "." + sig, "malformed"), ("garbage", "malformed"),
                        (sign_payload(b"z" * 32, payload), "bad_signature"), ("A" * 5000, "malformed")]:
        with pytest.raises(TokenError) as e:
            verify_payload(KEY, bad)
        assert e.value.reason == reason


def test_tag_ids():
    tag = new_tag_id()
    assert normalize_tag(tag.lower()) == tag and normalize_tag(f"TNSTC-TAG:{tag}") == tag
    with pytest.raises(TokenError):
        normalize_tag("TAG-0000")


# ── State machine ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("current,target", [
    ("opened", "token_issued"), ("questions_pending", "approved"), ("failed", "verified"),
    ("completed", "cancelled"), ("expired", "questions_pending"), ("cancelled", "approved"),
    ("approved", "completed"), ("staff_review", "token_issued"), ("locked", "questions_pending"),
])
def test_illegal_transitions_rejected(current, target):
    assert not sm.allowed(current, target)
    with pytest.raises(sm.IllegalTransition):
        sm.transition_update({"_id": 1, "status": current}, target, None, "t")


def test_transition_cas_history_and_index_flags(clock):
    coll = mongomock.MongoClient()["t"]["claims"]
    coll.insert_one({"_id": 1, "status": "opened", "openForMatch": True, "history": []})
    claim = coll.find_one({"_id": 1})
    doc = sm.transition(coll, claim, sm.QUESTIONS_PENDING, clock(), "system", "round 1")
    assert doc["status"] == "questions_pending" and doc["openForMatch"] is True and "lockActive" not in doc
    assert doc["history"][-1]["from"] == "opened" and doc["history"][-1]["to"] == "questions_pending"
    with pytest.raises(sm.IllegalTransition):                  # stale copy loses the compare-and-set
        sm.transition(coll, claim, sm.CANCELLED, clock(), "x")
    doc = sm.transition(coll, doc, sm.VERIFIED, clock(), "system")
    doc = sm.transition(coll, doc, sm.APPROVED, clock(), "staff")
    assert doc["lockActive"] is True
    doc = sm.transition(coll, doc, sm.TOKEN_ISSUED, clock(), "system")
    doc = sm.transition(coll, doc, sm.COMPLETED, clock(), "staff")
    assert "openForMatch" not in doc and "lockActive" not in doc and len(doc["history"]) == 5


def test_every_state_is_reachable_and_terminals_are_final():
    reachable, frontier = {"opened"}, ["opened"]
    while frontier:
        for nxt in sm.TRANSITIONS[frontier.pop()]:
            if nxt not in reachable:
                reachable.add(nxt)
                frontier.append(nxt)
    assert reachable == set(sm.TRANSITIONS)
    assert all(not sm.TRANSITIONS[t] for t in sm.TERMINAL)


# ── Unit of work ─────────────────────────────────────────────────────────────
def test_unit_of_work_rolls_back_every_write(atomic_mongomock):
    db = mongomock.MongoClient()["t"]
    db.a.insert_one({"_id": 1, "v": "before"})
    assert not supports_transactions(db)

    def work(tx: Tx):
        tx.cas_update("a", {"_id": 1, "v": "before"}, {"$set": {"v": "after"}}, "x")
        tx.insert("b", {"_id": 9, "log": True})
        raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError):
        run_atomically(db, work)
    assert db.a.find_one({"_id": 1})["v"] == "before" and db.b.count_documents({}) == 0


def test_unit_of_work_cas_miss_aborts(atomic_mongomock):
    db = mongomock.MongoClient()["t"]
    db.a.insert_one({"_id": 1, "v": "other"})

    def work(tx):
        tx.insert("b", {"_id": 1})
        tx.cas_update("a", {"_id": 1, "v": "before"}, {"$set": {"v": "after"}}, "conflict")

    with pytest.raises(ConflictAbort) as e:
        run_atomically(db, work)
    assert e.value.reason == "conflict" and db.b.count_documents({}) == 0


def test_unit_of_work_concurrent_cas_only_one_wins(atomic_mongomock):
    db = mongomock.MongoClient()["t"]
    db.item.insert_one({"_id": 1, "status": "open"})
    barrier, wins = threading.Barrier(6), []

    def worker():
        barrier.wait(timeout=5)
        try:
            run_atomically(db, lambda tx: tx.cas_update("item", {"_id": 1, "status": "open"},
                                                         {"$set": {"status": "claim_locked"}}, "taken"))
            wins.append(1)
        except ConflictAbort:
            pass

    threads = [threading.Thread(target=worker) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(wins) == 1
