"""
MongoDB-backed rate limiting, safe across multiple app processes.

Three primitives, each built on a single atomic MongoDB operation:
  * hit_window      — fixed-window counter ($inc upsert), used for per-IP limits.
  * acquire_cooldown — "at most once per N seconds" gate (insert-or-conditional-update),
                       used for the OTP resend cooldown.
  * recent_events / record_event — sliding one-hour log, used for the per-PNR cap.
"""
from dataclasses import dataclass
from datetime import timedelta

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from .timeutil import from_db, seconds_until, to_db

COUNTERS = "rate_limits"
EVENTS = "rate_limit_events"


@dataclass(frozen=True)
class Decision:
    allowed: bool
    retry_after: int = 0


class RateLimiter:
    def __init__(self, db, clock):
        self.counters = db[COUNTERS]
        self.events = db[EVENTS]
        self.clock = clock

    def hit_window(self, scope: str, ident: str, limit: int, window_seconds: int) -> Decision:
        now = self.clock()
        epoch = int(now.timestamp())
        bucket = epoch - epoch % window_seconds
        window_end = now + timedelta(seconds=bucket + window_seconds - epoch)
        key = f"{scope}:{ident}:{bucket}"
        update = {"$inc": {"count": 1},
                  "$setOnInsert": {"purgeAt": to_db(window_end + timedelta(seconds=window_seconds))}}
        try:
            doc = self.counters.find_one_and_update({"_id": key}, update, upsert=True,
                                                    return_document=ReturnDocument.AFTER)
        except DuplicateKeyError:
            # Two first-hits raced on the upsert; the loser simply retries the $inc.
            doc = self.counters.find_one_and_update({"_id": key}, update,
                                                    return_document=ReturnDocument.AFTER)
        if doc["count"] > limit:
            return Decision(False, seconds_until(window_end, now))
        return Decision(True)

    def acquire_cooldown(self, scope: str, ident: str, seconds: int) -> Decision:
        now = self.clock()
        key = f"{scope}:{ident}"
        until = now + timedelta(seconds=seconds)
        fields = {"until": to_db(until), "purgeAt": to_db(until + timedelta(hours=1))}
        try:
            self.counters.insert_one({"_id": key, **fields})
            return Decision(True)
        except DuplicateKeyError:
            pass
        res = self.counters.update_one({"_id": key, "until": {"$lte": to_db(now)}}, {"$set": fields})
        if res.modified_count == 1:
            return Decision(True)
        doc = self.counters.find_one({"_id": key}) or {}
        return Decision(False, max(1, seconds_until(doc.get("until", to_db(until)), now)))

    def recent_events(self, scope: str, ident: str, window_seconds: int):
        """(count, retry_after) for events of this key inside the sliding window."""
        now = self.clock()
        since = to_db(now - timedelta(seconds=window_seconds))
        query = {"key": f"{scope}:{ident}", "at": {"$gt": since}}
        count = self.events.count_documents(query)
        oldest = self.events.find_one(query, sort=[("at", 1)])
        retry = 0
        if oldest:
            retry = seconds_until(from_db(oldest["at"]) + timedelta(seconds=window_seconds), now)
        return count, max(1, retry)

    def record_event(self, scope: str, ident: str, window_seconds: int) -> None:
        now = self.clock()
        self.events.insert_one({"key": f"{scope}:{ident}", "at": to_db(now),
                                "purgeAt": to_db(now + timedelta(seconds=window_seconds * 2))})
