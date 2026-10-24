"""
Feature 2 core: match routing, notification queue, delivery, hold expiry, views.

matches (existing collection, extended; legacy fields found_id/request_id/score kept):
    reportId, itemId, confidence, status, createdAt, approvedAt, approvedBy,
    rejectedAt, rejectedBy, notifiedAt, holdExpiresAt, deliveryStatus, deliveredVia, ...
    status: pending_staff_review | auto_approved | notified | claim_started | expired |
            released | rejected   (+ legacy terminal states: resolved, cancelled)

notifications:
    matchId, reportId, itemId, depotPhone, channel (in_app | whatsapp | email),
    status (pending | sent | failed | delivered | cancelled), attempts, lastError,
    sentAt, dedupeKey (unique: reportId:itemId:channel), nextAttemptAt, leaseUntil, ...

Every state change is one conditional MongoDB update, so duplicate triggers, a
re-run matcher, two workers or a double-clicked staff button cannot double-notify.
The `notifications` collection is itself the job queue: a worker claims a due job
by atomically setting a lease, so each job is processed by one worker at a time.
"""
import logging
import re
import uuid
from datetime import datetime, timedelta

from bson import ObjectId
from bson.errors import InvalidId
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from passenger_auth.bookings import BOOKINGS
from passenger_auth.phone import try_normalize_phone
from passenger_auth.timeutil import fmt_ist, from_db, iso_utc, seconds_until, to_db

from .channels import EMAIL, WHATSAPP, ChannelError, mask_contact
from .routing import AUTO, REJECT, REVIEW, backoff_seconds, dedupe_key, route_confidence
from .template import clean_text, format_found_date, public_item_description, render_match_message

logger = logging.getLogger(__name__)

LOST, FOUND, MATCHES, DEPOTS, NOTIFICATIONS = "lost_reports", "found_reports", "matches", "depots", "notifications"
IN_APP = "in_app"
EXTERNAL_CHANNELS = (WHATSAPP, EMAIL)

PENDING_REVIEW, AUTO_APPROVED, NOTIFIED = "pending_staff_review", "auto_approved", "notified"
CLAIM_STARTED, EXPIRED, RELEASED, REJECTED = "claim_started", "expired", "released", "rejected"
RESOLVED, CANCELLED = "resolved", "cancelled"
ALL_STATUSES = (PENDING_REVIEW, AUTO_APPROVED, NOTIFIED, CLAIM_STARTED, EXPIRED, RELEASED, REJECTED,
                RESOLVED, CANCELLED)
ACTIVE_STATUSES = (PENDING_REVIEW, AUTO_APPROVED, NOTIFIED, CLAIM_STARTED)
SENDABLE_STATUSES = (AUTO_APPROVED, NOTIFIED)
OPEN_REPORT_STATUSES = ("pending", "matched")
AVAILABLE_ITEM_STATUSES = (None, "open")

_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


class NotFound(Exception):
    """Unknown id, or an object the caller is not allowed to see (indistinguishable)."""


class Conflict(Exception):
    """The object exists but is not in a state that allows the action."""


def _oid(value):
    try:
        return ObjectId(value) if isinstance(value, str) else None
    except (InvalidId, TypeError):
        return None


def report_is_open(lost) -> bool:
    return bool(lost) and lost.get("status", "pending") in OPEN_REPORT_STATUSES


def item_is_available(found) -> bool:
    return bool(found) and found.get("status") in AVAILABLE_ITEM_STATUSES


class MatchNotifyService:
    def __init__(self, db, config, channels, clock, rematch_lost=None, rematch_found=None):
        self.db = db
        self.config = config
        self.channels = channels
        self.clock = clock
        self.lost = db[LOST]
        self.found = db[FOUND]
        self.matches = db[MATCHES]
        self.depots = db[DEPOTS]
        self.notifications = db[NOTIFICATIONS]
        self.rematch_lost = rematch_lost
        self.rematch_found = rematch_found
        self.on_enqueue = None

    # ── setup ────────────────────────────────────────────────────────────────
    def backfill_legacy_matches(self) -> int:
        """Give pre-Feature-2 match docs the new fields. Idempotent; never notifies."""
        n = 0
        for m in self.matches.find({"reportId": {"$exists": False}}):
            status = m.get("status")
            created = None
            try:
                created = datetime.fromisoformat(m.get("created_at", ""))
            except (TypeError, ValueError):
                pass
            self.matches.update_one({"_id": m["_id"], "reportId": {"$exists": False}}, {"$set": {
                "reportId": m.get("request_id"),
                "itemId": m.get("found_id"),
                "confidence": float((m.get("score") or {}).get("final", 0.0)),
                "status": PENDING_REVIEW if status in (None, "pending") else status,
                "createdAt": to_db(created) if created else to_db(self.clock()),
                "routedAs": "legacy",
            }})
            n += 1
        return n

    # ── matcher hook ─────────────────────────────────────────────────────────
    def record_match(self, found, lost, score):
        """
        Called by the existing matcher for every (found, lost) pair that clears its
        threshold, from BOTH directions. Returns (match_doc, is_new) or (None, False)
        when the report is closed or the item is no longer available.
        """
        if not report_is_open(lost) or not item_is_available(found):
            return None, False
        now = self.clock()
        confidence = round(float(score.get("final", 0.0)), 4)
        decision = route_confidence(confidence, self.config)
        status = {AUTO: AUTO_APPROVED, REVIEW: PENDING_REVIEW, REJECT: REJECTED}[decision]
        key = {"reportId": lost["request_id"], "itemId": found["found_id"]}
        on_insert = {
            "found_id": found["found_id"], "request_id": lost["request_id"],
            "depot_phone": found.get("depot_phone"), "depot_name": found.get("depot_name"),
            "score": score, "created_at": datetime.now().isoformat(),
            "confidence": confidence, "status": status, "routedAs": decision,
            "createdAt": to_db(now),
        }
        if decision == REJECT:
            on_insert.update(rejectedBy="system", rejectedAt=to_db(now))
        update = {"$setOnInsert": on_insert,
                  "$set": {"latestConfidence": confidence, "lastScoredAt": to_db(now)}}
        try:
            res = self.matches.update_one(key, update, upsert=True)
        except DuplicateKeyError:
            res = self.matches.update_one(key, update)
        match = self.matches.find_one(key)
        is_new = res.upserted_id is not None
        if is_new:
            logger.info("Match %s created: confidence=%.2f route=%s", match["_id"], confidence, decision)
            if decision == AUTO:
                self._approve(match, by="system", from_status=AUTO_APPROVED)
            return self.matches.find_one(key), True

        # Re-scored existing pair. Only a SYSTEM rejection (score was too low) may be
        # re-routed upward; staff rejections, releases, cancellations never resurrect.
        if match.get("status") == REJECTED and match.get("rejectedBy") == "system" and decision != REJECT:
            new_status = AUTO_APPROVED if decision == AUTO else PENDING_REVIEW
            rerouted = self.matches.find_one_and_update(
                {"_id": match["_id"], "status": REJECTED, "rejectedBy": "system"},
                {"$set": {"status": new_status, "confidence": confidence, "routedAs": decision},
                 "$unset": {"rejectedBy": "", "rejectedAt": ""}},
                return_document=ReturnDocument.AFTER)
            if rerouted and decision == AUTO:
                self._approve(rerouted, by="system", from_status=AUTO_APPROVED)
        return self.matches.find_one(key), False

    # ── approval and notification fan-out ────────────────────────────────────
    def _approve(self, match, by, from_status):
        """Move a match to auto_approved (once) and enqueue its notifications."""
        now = self.clock()
        approved = self.matches.find_one_and_update(
            {"_id": match["_id"], "status": from_status, "approvedAt": {"$exists": False}},
            {"$set": {"status": AUTO_APPROVED, "approvedAt": to_db(now), "approvedBy": by,
                      "deliveryStatus": "pending"}},
            return_document=ReturnDocument.AFTER)
        if not approved:
            return None
        self.lost.update_one({"request_id": approved["reportId"], "status": "pending"},
                             {"$set": {"status": "matched"}})
        lost = self.lost.find_one({"request_id": approved["reportId"]})
        if lost and lost.get("ownerPnr"):
            self._insert_notification(approved, IN_APP, status="delivered", sentAt=to_db(now))
        first = self._enqueue_channel(approved, WHATSAPP, lost)
        if first != "pending":
            if self._enqueue_channel(approved, EMAIL, lost) != "pending":
                self._finalize(approved["_id"])
        self._wake()
        return approved

    def _wake(self):
        if self.on_enqueue:
            self.on_enqueue()

    def _insert_notification(self, match, channel, **fields):
        now = self.clock()
        doc = {
            "matchId": str(match["_id"]), "reportId": match["reportId"], "itemId": match["itemId"],
            "depotPhone": match.get("depot_phone"), "channel": channel,
            "status": "pending", "attempts": 0, "maxAttempts": self.config.max_attempts,
            "lastError": None, "sentAt": None, "createdAt": to_db(now), "nextAttemptAt": to_db(now),
            "leaseUntil": None, "leaseOwner": None, "retryCount": 0,
            "dedupeKey": dedupe_key(match["reportId"], match["itemId"], channel),
        }
        doc.update(fields)
        try:
            self.notifications.insert_one(doc)
            return doc
        except DuplicateKeyError:
            return None

    def contacts_for(self, lost) -> dict:
        """Verified contact points for the report owner, read from their booking."""
        if not lost or not lost.get("ownerPnr") or not lost.get("ownerPhone"):
            return {}
        rows = [b for b in self.db[BOOKINGS].find({"pnr": lost["ownerPnr"]})
                if try_normalize_phone(b.get("registeredPhone")) == lost["ownerPhone"]]
        out = {}
        if rows and any(b.get("whatsapp", True) for b in rows):
            out[WHATSAPP] = lost["ownerPhone"]
        email = next((b.get("email") for b in rows
                      if isinstance(b.get("email"), str) and _EMAIL_RE.fullmatch(b["email"].strip())), None)
        if email:
            out[EMAIL] = email.strip()
        return out

    def _enqueue_channel(self, match, channel, lost):
        """Create the channel job. Returns 'pending', 'unavailable' or 'exists'."""
        contact = self.contacts_for(lost).get(channel)
        if not contact:
            reason = ("report is not linked to a verified passenger" if not (lost or {}).get("ownerPnr")
                      else f"passenger has no {'WhatsApp' if channel == WHATSAPP else 'email'} on file")
            created = self._insert_notification(match, channel, status="failed", lastError=reason,
                                                failedAt=to_db(self.clock()))
            return "unavailable" if created else "exists"
        created = self._insert_notification(match, channel, toMasked=mask_contact(channel, contact))
        return "pending" if created else "exists"

    def _finalize(self, match_id):
        """All external channels are settled: record the outcome and start the hold."""
        sent = self.notifications.find_one({"matchId": str(match_id), "channel": {"$in": EXTERNAL_CHANNELS},
                                            "status": "sent"})
        if sent:
            self.matches.update_one({"_id": match_id},
                                    {"$set": {"deliveryStatus": "delivered", "deliveredVia": sent["channel"]}})
        else:
            self.matches.update_one({"_id": match_id}, {"$set": {"deliveryStatus": "failed"}})
            logger.warning("Match %s: every notification channel failed; surfaced to staff.", match_id)
        self._mark_notified(match_id)

    def _mark_notified(self, match_id):
        now = self.clock()
        self.matches.update_one(
            {"_id": match_id, "status": AUTO_APPROVED},
            {"$set": {"status": NOTIFIED, "notifiedAt": to_db(now),
                      "holdExpiresAt": to_db(now + timedelta(days=self.config.hold_days))}})

    # ── worker: notification jobs ────────────────────────────────────────────
    def process_due_notifications(self, worker_id=None, limit=100) -> int:
        worker_id = worker_id or uuid.uuid4().hex
        handled = 0
        while handled < limit:
            now = self.clock()
            job = self.notifications.find_one_and_update(
                {"channel": {"$in": EXTERNAL_CHANNELS}, "status": "pending",
                 "nextAttemptAt": {"$lte": to_db(now)},
                 "$or": [{"leaseUntil": None}, {"leaseUntil": {"$lte": to_db(now)}}]},
                {"$set": {"leaseUntil": to_db(now + timedelta(seconds=self.config.lease_seconds)),
                          "leaseOwner": worker_id},
                 "$inc": {"attempts": 1}},
                sort=[("nextAttemptAt", 1)], return_document=ReturnDocument.AFTER)
            if job is None:
                break
            self._handle_job(job, worker_id)
            handled += 1
        return handled

    def _owned(self, job, worker_id):
        return {"_id": job["_id"], "status": "pending", "leaseOwner": worker_id}

    def _handle_job(self, job, worker_id):
        match = self.matches.find_one({"_id": _oid(job["matchId"])})
        lost = self.lost.find_one({"request_id": job["reportId"]})
        found = self.found.find_one({"found_id": job["itemId"]})
        if not match or match.get("status") not in SENDABLE_STATUSES \
                or not report_is_open(lost) or not item_is_available(found):
            self.notifications.update_one(self._owned(job, worker_id), {"$set": {
                "status": "cancelled", "lastError": "match no longer active", "leaseUntil": None}})
            return
        contact = self.contacts_for(lost).get(job["channel"])
        if not contact:
            return self._job_failed(job, worker_id, ChannelError("no contact on file", permanent=True))
        depot = self.depots.find_one({"phone": found.get("depot_phone")}) or {}
        message = render_match_message(
            passenger_name=lost.get("name"), report_tracking_id=lost.get("tracking_id"),
            confidence=match.get("confidence", 0.0),
            depot_name=depot.get("name") or found.get("depot_name"),
            public_description=found.get("public_description"), date_found=found.get("date"),
            hold_days=self.config.hold_days, report_url=self.report_url(lost),
            description_limit=self.config.description_max_chars)
        try:
            ref = self.channels[job["channel"]].send(contact, message)
        except ChannelError as e:
            return self._job_failed(job, worker_id, e)
        except Exception as e:   # an adapter bug must not kill the worker loop
            return self._job_failed(job, worker_id, ChannelError(f"unexpected {type(e).__name__}"))
        now = self.clock()
        done = self.notifications.update_one(self._owned(job, worker_id), {"$set": {
            "status": "sent", "sentAt": to_db(now), "providerRef": str(ref)[:120],
            "lastError": None, "leaseUntil": None}})
        if done.modified_count:
            logger.info("Notification %s sent via %s (attempt %d).", job["_id"], job["channel"], job["attempts"])
            self.matches.update_one({"_id": match["_id"]},
                                    {"$set": {"deliveryStatus": "delivered", "deliveredVia": job["channel"]}})
            self._mark_notified(match["_id"])

    def _job_failed(self, job, worker_id, err):
        now = self.clock()
        if not err.permanent and job["attempts"] < job.get("maxAttempts", self.config.max_attempts):
            delay = backoff_seconds(job["attempts"], self.config)
            self.notifications.update_one(self._owned(job, worker_id), {"$set": {
                "lastError": str(err)[:300], "leaseUntil": None,
                "nextAttemptAt": to_db(now + timedelta(seconds=delay))}})
            logger.info("Notification %s attempt %d failed (%s); retry in %ds.",
                        job["_id"], job["attempts"], err, delay)
            return
        res = self.notifications.update_one(self._owned(job, worker_id), {"$set": {
            "status": "failed", "lastError": str(err)[:300], "failedAt": to_db(now), "leaseUntil": None}})
        if not res.modified_count:
            return
        logger.warning("Notification %s via %s failed permanently: %s", job["_id"], job["channel"], err)
        match = self.matches.find_one({"_id": _oid(job["matchId"])})
        if job["channel"] == WHATSAPP and match:
            lost = self.lost.find_one({"request_id": job["reportId"]})
            if self._enqueue_channel(match, EMAIL, lost) == "pending":
                self._wake()
                return
        if match and not self.notifications.find_one(
                {"matchId": job["matchId"], "channel": {"$in": EXTERNAL_CHANNELS}, "status": "pending"}):
            self._finalize(match["_id"])

    # ── worker: hold expiry ──────────────────────────────────────────────────
    def release_expired_matches(self) -> int:
        now = to_db(self.clock())
        released = 0
        for m in self.matches.find({"status": NOTIFIED, "holdExpiresAt": {"$lte": now}}, {"_id": 1}):
            res = self.matches.update_one(
                {"_id": m["_id"], "status": NOTIFIED, "holdExpiresAt": {"$lte": now}},
                {"$set": {"status": RELEASED, "releasedAt": now, "releaseReason": "hold_expired"}})
            if res.modified_count:
                released += 1
                self._cancel_pending_notifications([str(m["_id"])], "match hold expired")
        if released:
            logger.info("Released %d match(es) whose hold expired.", released)
        return released

    def _cancel_pending_notifications(self, match_ids, reason):
        self.notifications.update_many(
            {"matchId": {"$in": list(match_ids)}, "status": "pending"},
            {"$set": {"status": "cancelled", "lastError": reason, "leaseUntil": None}})

    def _cancel_matches(self, query, reason):
        ids = [str(m["_id"]) for m in self.matches.find({**query, "status": {"$in": ACTIVE_STATUSES}}, {"_id": 1})]
        if ids:
            self.matches.update_many({"_id": {"$in": [ObjectId(i) for i in ids]},
                                      "status": {"$in": ACTIVE_STATUSES}},
                                     {"$set": {"status": CANCELLED, "cancelledReason": reason,
                                               "cancelledAt": to_db(self.clock())}})
            self._cancel_pending_notifications(ids, reason)
        return len(ids)

    # ── lifecycle events from the rest of the app ────────────────────────────
    def on_match_resolved(self, match_id):
        """Depot confirmed this match: the item is reserved, competing matches end."""
        match = self.matches.find_one({"_id": match_id})
        if not match:
            return
        self.found.update_one({"found_id": match["itemId"], "status": {"$in": list(AVAILABLE_ITEM_STATUSES)}},
                              {"$set": {"status": "resolved"}})
        self._cancel_matches({"itemId": match["itemId"], "_id": {"$ne": match_id}}, "item_unavailable")
        self._cancel_matches({"reportId": match["reportId"], "_id": {"$ne": match_id}}, "report_resolved")
        self._cancel_pending_notifications([str(match_id)], "match resolved by depot")

    def cancel_report(self, lost):
        res = self.lost.update_one({"_id": lost["_id"], "status": {"$in": OPEN_REPORT_STATUSES}},
                                   {"$set": {"status": "cancelled", "cancelledAt": to_db(self.clock())}})
        if not res.modified_count:
            raise Conflict("Only an open report can be cancelled.")
        self._cancel_matches({"reportId": lost["request_id"]}, "report_cancelled")

    def update_report_description(self, lost, description):
        if not report_is_open(lost):
            raise Conflict("Only an open report can be updated.")
        self.lost.update_one({"_id": lost["_id"]}, {"$set": {"description": description,
                                                             "updatedAt": to_db(self.clock())}})
        if self.rematch_lost:
            self.rematch_lost(self.lost.find_one({"_id": lost["_id"]}))

    # ── staff actions ────────────────────────────────────────────────────────
    def _staff_match(self, match_id, depot_phone):
        oid = _oid(match_id)
        match = self.matches.find_one({"_id": oid, "depot_phone": depot_phone}) if oid else None
        if not match:
            raise NotFound()
        return match

    def approve(self, match_id, depot_phone):
        match = self._staff_match(match_id, depot_phone)
        lost = self.lost.find_one({"request_id": match["reportId"]})
        found = self.found.find_one({"found_id": match["itemId"]})
        if match.get("status") != PENDING_REVIEW:
            raise Conflict(f"Match is {match.get('status')}, not awaiting review.")
        if not report_is_open(lost) or not item_is_available(found):
            raise Conflict("The report is closed or the item is no longer available.")
        approved = self._approve(match, by=f"staff:{depot_phone}", from_status=PENDING_REVIEW)
        if not approved:
            raise Conflict("Match was already handled.")
        return approved

    def reject(self, match_id, depot_phone):
        match = self._staff_match(match_id, depot_phone)
        res = self.matches.find_one_and_update(
            {"_id": match["_id"], "status": PENDING_REVIEW},
            {"$set": {"status": REJECTED, "rejectedBy": f"staff:{depot_phone}",
                      "rejectedAt": to_db(self.clock())}},
            return_document=ReturnDocument.AFTER)
        if not res:
            raise Conflict(f"Match is {match.get('status')}, not awaiting review.")
        return res

    def retry_blocker(self, job):
        """Why a staff retry of this job is not allowed, or None if it is."""
        if job["channel"] not in EXTERNAL_CHANNELS or job["status"] != "failed":
            return "Only a failed WhatsApp/email notification can be retried."
        if self.notifications.find_one({"matchId": job["matchId"], "channel": {"$in": EXTERNAL_CHANNELS},
                                        "status": {"$in": ["pending", "sent"]}}):
            return "The passenger was already reached, or another delivery is in progress."
        match = self.matches.find_one({"_id": _oid(job["matchId"])})
        if not match or match.get("status") not in SENDABLE_STATUSES:
            return "The match is no longer active."
        if not self.contacts_for(self.lost.find_one({"request_id": job["reportId"]})).get(job["channel"]):
            return "No contact on file for this channel."
        return None

    def retry_notification(self, notification_id, depot_phone):
        oid = _oid(notification_id)
        job = self.notifications.find_one({"_id": oid, "depotPhone": depot_phone}) if oid else None
        if not job:
            raise NotFound()
        blocker = self.retry_blocker(job)
        if blocker:
            raise Conflict(blocker)
        res = self.notifications.update_one(
            {"_id": oid, "status": "failed"},
            {"$set": {"status": "pending", "attempts": 0, "nextAttemptAt": to_db(self.clock()),
                      "leaseUntil": None, "leaseOwner": None},
             "$inc": {"retryCount": 1}})
        if not res.modified_count:
            raise Conflict("Notification was already retried.")
        self.matches.update_one({"_id": _oid(job["matchId"])}, {"$set": {"deliveryStatus": "pending"}})
        self._wake()
        return self.notifications.find_one({"_id": oid})

    def rerun_for_depot(self, depot_phone, item_id=None) -> int:
        """Re-run the matcher for this depot's open items. Safe to repeat (dedupe)."""
        query = {"depot_phone": depot_phone, "status": {"$in": list(AVAILABLE_ITEM_STATUSES)}}
        if item_id:
            query["found_id"] = item_id
        items = list(self.found.find(query))
        if item_id and not items:
            raise NotFound()
        for found in items:
            self.rematch_found(found)
        return len(items)

    # ── views ────────────────────────────────────────────────────────────────
    def report_url(self, lost) -> str:
        return f"{self.config.app_base_url}/passenger/reports/{lost.get('tracking_id', '')}"

    def owned_report(self, tracking_id, passenger):
        if not isinstance(tracking_id, str) or len(tracking_id) > 20:
            raise NotFound()
        lost = self.lost.find_one({"tracking_id": tracking_id.strip().upper(),
                                   "ownerPnr": passenger.pnr, "ownerPhone": passenger.phone})
        if not lost:
            raise NotFound()
        return lost

    def reports_for(self, passenger):
        return list(self.lost.find({"ownerPnr": passenger.pnr, "ownerPhone": passenger.phone})
                    .sort("created_at", -1))

    def visible_matches_for_report(self, lost):
        return list(self.matches.find({"reportId": lost["request_id"], "approvedAt": {"$exists": True}})
                    .sort("createdAt", -1))

    def match_for_passenger(self, match_id, passenger):
        oid = _oid(match_id)
        match = self.matches.find_one({"_id": oid, "approvedAt": {"$exists": True}}) if oid else None
        if not match:
            raise NotFound()
        lost = self.lost.find_one({"request_id": match["reportId"], "ownerPnr": passenger.pnr,
                                   "ownerPhone": passenger.phone})
        if not lost:
            raise NotFound()
        return match, lost

    @staticmethod
    def passenger_state(match) -> str:
        return {AUTO_APPROVED: "active", NOTIFIED: "active", CLAIM_STARTED: "claim_started",
                RELEASED: "expired", EXPIRED: "expired", RESOLVED: "confirmed"}.get(match.get("status"),
                                                                                    "unavailable")

    def match_view(self, match, lost) -> dict:
        """The ONLY match representation a passenger ever receives."""
        found = self.found.find_one({"found_id": match["itemId"]}) or {}
        depot = self.depots.find_one({"phone": found.get("depot_phone")}) or {}
        now = self.clock()
        hold = match.get("holdExpiresAt")
        state = self.passenger_state(match)
        if state == "active" and hold is not None and from_db(hold) <= now:
            state = "expired"
        # Item reserved or collected through someone else's claim (Feature 3): say only "unavailable".
        lock = found.get("claimLock") or {}
        if state in ("active", "claim_started") and (
                (found.get("status") == "claim_locked" and lock.get("matchId") != str(match["_id"]))
                or found.get("status") in ("handed_over", "resolved")):
            state = "unavailable"
        return {
            "matchId": str(match["_id"]),
            "reportId": lost.get("tracking_id"),
            "state": state,
            "confidencePercent": max(0, min(100, round(float(match.get("confidence", 0)) * 100))),
            "foundAt": clean_text(depot.get("name") or found.get("depot_name"), 80),
            "itemDescription": public_item_description(found.get("public_description"),
                                                       self.config.description_max_chars)
                               or "Details withheld - please verify at the depot",
            "dateFound": format_found_date(found.get("date")),
            "notifiedAt": iso_utc(match["notifiedAt"]) if match.get("notifiedAt") else None,
            "holdExpiresAt": iso_utc(hold) if hold else None,
            "holdExpiresAtIst": fmt_ist(hold) if hold else None,
            "holdSecondsRemaining": seconds_until(hold, now) if hold and state == "active" else None,
            "claim": {"canStart": state == "active"},
        }

    def notifications_for(self, passenger):
        report_ids = {r["request_id"]: r for r in self.reports_for(passenger)}
        docs = self.notifications.find({"reportId": {"$in": list(report_ids)}, "channel": IN_APP}) \
            .sort("createdAt", -1)
        out = []
        for n in docs:
            lost = report_ids[n["reportId"]]
            out.append({
                "id": str(n["_id"]), "matchId": n["matchId"], "reportId": lost.get("tracking_id"),
                "kind": n.get("kind", "match_found"),
                "title": n.get("title") or f"Possible match found for report {lost.get('tracking_id')}",
                "createdAt": iso_utc(n["createdAt"]), "createdAtIst": fmt_ist(n["createdAt"]),
                "link": n.get("link") or f"/passenger/matches/{n['matchId']}",
            })
        return out

    def staff_match_rows(self, depot_phone, status=None):
        query = {"depot_phone": depot_phone}
        if status:
            query["status"] = status
        rows = []
        for m in self.matches.find(query).sort("createdAt", -1).limit(200):
            lost = self.lost.find_one({"request_id": m.get("reportId")}) or {}
            found = self.found.find_one({"found_id": m.get("itemId")}) or {}
            rows.append({
                "matchId": str(m["_id"]), "status": m.get("status"),
                "confidence": m.get("confidence"), "routedAs": m.get("routedAs"),
                "deliveryStatus": m.get("deliveryStatus"),
                "createdAt": iso_utc(m["createdAt"]) if m.get("createdAt") else None,
                "report": {"trackingId": lost.get("tracking_id"), "status": lost.get("status"),
                           "description": lost.get("description"), "date": lost.get("date"),
                           "linkedToPassenger": bool(lost.get("ownerPnr"))},
                "item": {"itemId": m.get("itemId"), "status": found.get("status"),
                         "publicDescription": found.get("public_description"),
                         "notes": found.get("notes"), "date": found.get("date")},
            })
        return rows

    def staff_notification_rows(self, depot_phone, status=None):
        query = {"depotPhone": depot_phone, "channel": {"$in": EXTERNAL_CHANNELS}}
        if status:
            query["status"] = status
        rows = []
        for n in self.notifications.find(query).sort("createdAt", -1).limit(200):
            lost = self.lost.find_one({"request_id": n["reportId"]}) or {}
            rows.append({
                "id": str(n["_id"]), "matchId": n["matchId"], "reportId": lost.get("tracking_id"),
                "channel": n["channel"], "status": n["status"], "attempts": n.get("attempts", 0),
                "maxAttempts": n.get("maxAttempts"), "lastError": n.get("lastError"),
                "to": n.get("toMasked"), "retryCount": n.get("retryCount", 0),
                "retryable": n["status"] == "failed" and self.retry_blocker(n) is None,
                "sentAt": iso_utc(n["sentAt"]) if n.get("sentAt") else None,
                "nextAttemptAt": iso_utc(n["nextAttemptAt"]) if n.get("nextAttemptAt") else None,
                "createdAt": iso_utc(n["createdAt"]),
            })
        return rows
