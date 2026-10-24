"""
Claim state machine. Every change goes through `transition`, a compare-and-set
on the current status, so illegal or concurrent transitions are rejected and
every accepted one is appended to the claim's `history` and logged.

    opened → questions_pending → verified | failed | locked | staff_review
    failed → questions_pending (next round)
    verified | staff_review | locked → approved → token_issued → completed
    token_issued → token_issued (staff reissue)
    any non-terminal → cancelled | expired
"""
import logging

from pymongo import ReturnDocument

from passenger_auth.timeutil import to_db

logger = logging.getLogger(__name__)

OPENED, QUESTIONS_PENDING, VERIFIED, FAILED = "opened", "questions_pending", "verified", "failed"
LOCKED, STAFF_REVIEW, APPROVED, TOKEN_ISSUED = "locked", "staff_review", "approved", "token_issued"
COMPLETED, EXPIRED, CANCELLED = "completed", "expired", "cancelled"

TRANSITIONS = {
    OPENED: {QUESTIONS_PENDING, STAFF_REVIEW, CANCELLED, EXPIRED},
    QUESTIONS_PENDING: {VERIFIED, FAILED, LOCKED, STAFF_REVIEW, CANCELLED, EXPIRED},
    FAILED: {QUESTIONS_PENDING, CANCELLED, EXPIRED},
    VERIFIED: {APPROVED, CANCELLED, EXPIRED},
    STAFF_REVIEW: {APPROVED, CANCELLED, EXPIRED},
    LOCKED: {APPROVED, CANCELLED, EXPIRED},
    APPROVED: {TOKEN_ISSUED, CANCELLED},
    TOKEN_ISSUED: {TOKEN_ISSUED, COMPLETED, CANCELLED, EXPIRED},
    COMPLETED: set(),
    EXPIRED: set(),
    CANCELLED: set(),
}
TERMINAL = frozenset({COMPLETED, EXPIRED, CANCELLED})
OPEN_STATES = frozenset(TRANSITIONS) - TERMINAL
LOCK_STATES = frozenset({APPROVED, TOKEN_ISSUED})
PHOTO_STATES = frozenset({VERIFIED, APPROVED, TOKEN_ISSUED})


class IllegalTransition(Exception):
    def __init__(self, current, target):
        super().__init__(f"Cannot move a claim from {current} to {target}.")
        self.current, self.target = current, target


def allowed(current: str, target: str) -> bool:
    return target in TRANSITIONS.get(current, set())


def transition_update(claim, target, now, by, reason=None, set_fields=None, unset_fields=None,
                      extra_filter=None):
    """(query, update) for a CAS move of `claim` to `target`; raises if the move is illegal."""
    current = claim["status"]
    if not allowed(current, target):
        raise IllegalTransition(current, target)
    fields = dict(set_fields or {})
    fields.update(status=target, updatedAt=to_db(now))
    unset = dict(unset_fields or {})
    # Flags backing the unique partial indexes: one open claim per match, one lock per item.
    for flag, states in (("openForMatch", OPEN_STATES), ("lockActive", LOCK_STATES)):
        if target in states:
            fields[flag] = True
        else:
            unset[flag] = ""
    update = {"$set": fields,
              "$push": {"history": {"from": current, "to": target, "at": to_db(now), "by": by,
                                    "reason": reason}}}
    if unset:
        update["$unset"] = unset
    query = {"_id": claim["_id"], "status": current, **(extra_filter or {})}
    return query, update


def transition(collection, claim, target, now, by, reason=None, set_fields=None, unset_fields=None,
               extra_filter=None):
    """CAS claim from its current status to `target`. Returns the updated doc or raises."""
    query, update = transition_update(claim, target, now, by, reason, set_fields, unset_fields, extra_filter)
    doc = collection.find_one_and_update(query, update, return_document=ReturnDocument.AFTER)
    if doc is None:
        latest = collection.find_one({"_id": claim["_id"]}) or {}
        raise IllegalTransition(latest.get("status", claim["status"]), target)
    logger.info("Claim %s: %s -> %s by %s%s", claim["_id"], claim["status"], target, by,
                f" ({reason})" if reason else "")
    return doc
