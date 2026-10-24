"""
Feature 3 core: claims, adaptive verification, item locking, pickup tokens, handover.

claims document (one per attempt to claim a matched item):
    claimId, matchId, reportId, itemId, pnr, claimerName, claimerPhone, depotId,
    status, questionSet, pendingQuestions, reserveQuestion, extraAsked, roundsUsed,
    outcomePerQuestion [{round, question, outcome: pass|fail}], trustScore,
    tokenHash, tokenNonce, tokenVersion, tokenExpiresAt, tokenUsedAt,
    staffId, idVerified, expiresAt, createdAt, updatedAt, history[]
Only question ids and pass/fail outcomes are stored — never a submitted answer.
Unique partial indexes: one OPEN claim per match (openForMatch) and one LOCKING
claim per item (lockActive) back the compare-and-set item lock.
"""
import hashlib
import hmac
import io
import logging
import os
import uuid
from datetime import timedelta

import segno
from bson import ObjectId
from bson.errors import InvalidId
from pymongo.errors import DuplicateKeyError

from match_notify.service import (AUTO_APPROVED, CLAIM_STARTED, NOTIFIED, RELEASED, RESOLVED,
                                  NotFound as MatchNotFound)
from match_notify.template import format_found_date, public_item_description
from passenger_auth.otp import derive_key
from passenger_auth.phone import mask_phone
from passenger_auth.timeutil import fmt_ist, from_db, iso_utc, seconds_until, to_db

from . import statemachine as sm
from .attributes import ATTRIBUTES, question_view, select_questions
from .scoring import BORDERLINE, VERIFIED, answer_passes, decide, trust_score
from .tokens import (TokenError, code_hash, derive_code, format_code, is_qr_payload, new_nonce,
                     normalize_code, normalize_tag, sign_payload, verify_payload)
from .txn import ConflictAbort, run_atomically

logger = logging.getLogger(__name__)

CLAIMS, CUSTODY, FOUND, MATCHES, LOST = "claims", "custody_log", "found_reports", "matches", "lost_reports"
NOTIFICATIONS = "notifications"
ITEM_OPEN = (None, "open")
CLAIM_LOCKED, HANDED_OVER = "claim_locked", "handed_over"


class NotFound(Exception):
    pass


class ClaimError(Exception):
    """A business-rule refusal: HTTP `status`, machine `code`, human `message`."""

    def __init__(self, status, code, message, **extra):
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra


PICKUP_REASONS = {
    "malformed": "The code or QR could not be read.",
    "bad_signature": "The QR code has been altered or is not a TNSTC pickup code.",
    "not_found": "No claim uses this code.",
    "revoked": "This code was replaced by a newer one and is no longer valid.",
    "expired": "This pickup code has expired.",
    "already_used": "This pickup code has already been used.",
    "not_issued": "This claim is not ready for pickup.",
    "wrong_depot": "This item is held at a different depot.",
    "item_mismatch": "The code does not belong to this item.",
    "item_released": "The item is no longer reserved for this claim.",
    "tag_mismatch": "The scanned item tag does not match the claimed item.",
    "tag_required": "Scan or enter the item's QR tag to confirm the handover.",
    "id_check_required": "Confirm that you checked the passenger's photo ID.",
    "report_closed": "The lost report was closed, so this handover cannot be recorded.",
    "match_changed": "The match was changed by another action. Reload and try again.",
}


class ClaimsService:
    def __init__(self, db, config, signing_secret, clock, limiter, match_service, sms=None,
                 upload_root=os.path.join("static")):
        self.db = db
        self.config = config
        self.clock = clock
        self.limiter = limiter
        self.match_service = match_service
        self.sms = sms
        self.upload_root = upload_root
        self.claims = db[CLAIMS]
        self.custody = db[CUSTODY]
        self.found = db[FOUND]
        self.matches = db[MATCHES]
        self.lost = db[LOST]
        self.answer_key = derive_key(signing_secret, "tnstc-claim-answers-v1")
        self.token_key = derive_key(signing_secret, "tnstc-pickup-token-v1")
        self.photo_key = derive_key(signing_secret, "tnstc-photo-url-v1")

    # ── lookups ──────────────────────────────────────────────────────────────
    def _item(self, claim_or_id):
        item_id = claim_or_id if isinstance(claim_or_id, str) else claim_or_id["itemId"]
        return self.found.find_one({"found_id": item_id}) or {}

    def claim_for_passenger(self, claim_id, passenger):
        if not isinstance(claim_id, str) or len(claim_id) > 64:
            raise NotFound()
        claim = self.claims.find_one({"claimId": claim_id, "pnr": passenger.pnr,
                                      "claimerPhone": passenger.phone})
        if not claim:
            raise NotFound()
        return self._expire_if_due(claim)

    def claims_for_passenger(self, passenger, match_id=None):
        query = {"pnr": passenger.pnr, "claimerPhone": passenger.phone}
        if isinstance(match_id, str):
            query["matchId"] = match_id
        return [self._expire_if_due(c) for c in self.claims.find(query).sort("createdAt", -1)]

    def _limit(self, scope, ident, limit, window):
        count, retry = self.limiter.recent_events(scope, ident, window)
        if count >= limit:
            raise ClaimError(429, "daily_limit", "Daily limit reached. Please try again later.",
                             retryAfterSeconds=retry)
        self.limiter.record_event(scope, ident, window)

    # ── open ─────────────────────────────────────────────────────────────────
    def open_claim(self, passenger, match_id):
        try:
            match, lost = self.match_service.match_for_passenger(match_id, passenger)
        except MatchNotFound:
            raise NotFound()
        now = self.clock()
        existing = self.claims.find_one({"matchId": str(match["_id"]), "openForMatch": True})
        if existing:
            raise ClaimError(409, "claim_exists", "You already have a claim in progress for this match.",
                             claimId=existing["claimId"])
        hold = match.get("holdExpiresAt")
        if match.get("status") not in (AUTO_APPROVED, NOTIFIED) or (hold is not None and from_db(hold) <= now):
            raise ClaimError(409, "match_not_claimable", "This match can no longer be claimed.")
        item = self._item(match["itemId"])
        if item.get("status") not in ITEM_OPEN:
            raise ClaimError(409, "item_unavailable", "This item is no longer available.")
        self._limit("claim-open", f"{passenger.pnr}:{passenger.phone}", self.config.max_claims_per_day, 86400)
        deadline = from_db(hold) if hold is not None else now + timedelta(days=self.match_service.config.hold_days)
        claim = {
            "claimId": uuid.uuid4().hex, "matchId": str(match["_id"]), "reportId": match["reportId"],
            "itemId": match["itemId"], "pnr": passenger.pnr, "claimerName": passenger.passenger_name,
            "claimerPhone": passenger.phone, "depotId": item.get("depot_phone"),
            "status": sm.OPENED, "openForMatch": True, "questionSet": [], "pendingQuestions": [],
            "reserveQuestion": None, "extraAsked": False, "roundsUsed": 0, "answerSeq": 0,
            "outcomePerQuestion": [], "trustScore": None, "tokenHash": None, "tokenExpiresAt": None,
            "tokenUsedAt": None, "tokenVersion": 0, "staffId": None, "idVerified": False,
            "expiresAt": to_db(deadline), "createdAt": to_db(now), "updatedAt": to_db(now),
            "history": [{"from": None, "to": sm.OPENED, "at": to_db(now), "by": "passenger", "reason": None}],
        }
        try:
            self.claims.insert_one(claim)
        except DuplicateKeyError:
            existing = self.claims.find_one({"matchId": str(match["_id"]), "openForMatch": True}) or {}
            raise ClaimError(409, "claim_exists", "You already have a claim in progress for this match.",
                             claimId=existing.get("claimId"))
        self.matches.update_one({"_id": match["_id"], "status": {"$in": [AUTO_APPROVED, NOTIFIED]}},
                                {"$set": {"status": CLAIM_STARTED}})
        logger.info("Claim %s opened for match %s.", claim["claimId"], claim["matchId"])
        return self._start_round(claim, item, previous=())

    def _start_round(self, claim, item, previous):
        chosen, reserve = select_questions(item, previous)
        now = self.clock()
        if not chosen:
            return sm.transition(self.claims, claim, sm.STAFF_REVIEW, now, "system", "insufficient_attributes")
        return sm.transition(self.claims, claim, sm.QUESTIONS_PENDING, now, "system",
                             f"round {claim['roundsUsed'] + 1}",
                             set_fields={"questionSet": chosen, "pendingQuestions": chosen,
                                         "reserveQuestion": reserve, "extraAsked": False})

    # ── questions and answers ────────────────────────────────────────────────
    def progress(self, claim) -> dict:
        return {"round": min(claim["roundsUsed"] + 1, self.config.max_rounds),
                "maxRounds": self.config.max_rounds, "roundsUsed": claim["roundsUsed"],
                "followUp": bool(claim.get("extraAsked"))}

    def questions(self, claim):
        if claim["status"] != sm.QUESTIONS_PENDING:
            raise ClaimError(409, "no_questions", "There are no questions to answer for this claim right now.")
        return [question_view(q) for q in claim["pendingQuestions"]]

    def submit_answers(self, claim, answers: dict, passenger):
        if claim["status"] != sm.QUESTIONS_PENDING:
            raise ClaimError(409, "no_questions", "There are no questions to answer for this claim right now.")
        pending = claim["pendingQuestions"]
        if set(answers) != set(pending):
            raise ClaimError(400, "invalid_request", "Answer every question shown, and only those.",
                             fields={q: "required" for q in pending if q not in answers})
        self._limit("claim-answers", f"{passenger.pnr}:{passenger.phone}",
                    self.config.max_answer_submissions_per_day, 86400)
        item = self._item(claim)
        attrs = item.get("private_attributes") or {}
        round_no = claim["roundsUsed"] + 1
        strict = bool(claim.get("extraAsked"))
        new_outcomes = []
        for qid in pending:
            ok = qid in attrs and answer_passes(self.answer_key, attrs[qid], answers[qid], self.config, strict)
            new_outcomes.append({"round": round_no, "question": qid, "outcome": "pass" if ok else "fail"})
        answers.clear()                                  # raw answers are not kept anywhere
        round_outcomes = {o["question"]: o["outcome"] == "pass"
                          for o in claim["outcomePerQuestion"] + new_outcomes if o["round"] == round_no}
        score = trust_score(round_outcomes, {q: ATTRIBUTES[q]["weight"] for q in round_outcomes})
        verdict = decide(score, self.config)
        now = self.clock()
        common = {"outcomePerQuestion": claim["outcomePerQuestion"] + new_outcomes, "trustScore": score,
                  "answerSeq": claim["answerSeq"] + 1}
        guard = {"answerSeq": claim["answerSeq"]}          # a replayed/parallel submission loses the CAS

        if verdict == BORDERLINE and not strict and claim.get("reserveQuestion"):
            extra = claim["reserveQuestion"]
            res = self.claims.find_one_and_update(
                {"_id": claim["_id"], "status": sm.QUESTIONS_PENDING, **guard},
                {"$set": {**common, "pendingQuestions": [extra], "questionSet": claim["questionSet"] + [extra],
                          "reserveQuestion": None, "extraAsked": True, "updatedAt": to_db(now)},
                 "$push": {"history": {"from": sm.QUESTIONS_PENDING, "to": sm.QUESTIONS_PENDING,
                                       "at": to_db(now), "by": "system", "reason": "borderline: follow-up question"}}})
            if res is None:
                raise ClaimError(409, "stale_submission", "These answers were already submitted.")
            return self.claims.find_one({"_id": claim["_id"]})

        common["roundsUsed"] = round_no
        common["pendingQuestions"] = []
        try:
            if verdict == VERIFIED:
                claim = sm.transition(self.claims, claim, sm.VERIFIED, now, "system", f"trust {score}",
                                      set_fields=common, extra_filter=guard)
                return self._auto_approve(claim)
            if verdict == BORDERLINE:
                return sm.transition(self.claims, claim, sm.STAFF_REVIEW, now, "system",
                                     f"borderline trust {score}", set_fields=common, extra_filter=guard)
            if round_no >= self.config.max_rounds:
                return sm.transition(self.claims, claim, sm.LOCKED, now, "system",
                                     "max rounds reached: staff ID verification required",
                                     set_fields=common, extra_filter=guard)
            failed = sm.transition(self.claims, claim, sm.FAILED, now, "system", f"trust {score}",
                                   set_fields=common, extra_filter=guard)
        except sm.IllegalTransition:
            raise ClaimError(409, "stale_submission", "These answers were already submitted.")
        return self._start_round(failed, item, previous=claim["questionSet"])

    # ── approval, locking, token ─────────────────────────────────────────────
    def _auto_approve(self, claim):
        try:
            return self._approve(claim, by="system")
        except ClaimError as e:
            if e.code != "item_unavailable":
                raise
            return self.cancel(claim, by="system", reason="item_claimed_by_another")

    def _approve(self, claim, by, id_verified=False, staff_id=None):
        """Lock the item (compare-and-set) and issue the pickup token, all or nothing."""
        now = self.clock()
        nonce = new_nonce()
        code = derive_code(self.token_key, claim["claimId"], nonce)
        expires = now + timedelta(days=self.config.token_ttl_days)

        def work(tx):
            tx.cas_update(FOUND, {"found_id": claim["itemId"], "status": {"$in": list(ITEM_OPEN)}},
                          {"$set": {"status": CLAIM_LOCKED,
                                    "claimLock": {"claimId": claim["claimId"], "matchId": claim["matchId"],
                                                  "at": to_db(now)}}}, "item_unavailable")
            q, u = sm.transition_update(claim, sm.APPROVED, now, by, "approved",
                                        set_fields={"idVerified": id_verified or claim.get("idVerified", False),
                                                    "staffId": staff_id, "approvedAt": to_db(now)})
            approved = tx.cas_update(CLAIMS, q, u, "stale_claim")
            q, u = sm.transition_update(approved, sm.TOKEN_ISSUED, now, "system", "token issued",
                                        set_fields=self._token_fields(code, nonce, expires, 1))
            return tx.cas_update(CLAIMS, q, u, "stale_claim")

        try:
            issued = run_atomically(self.db, work)
        except ConflictAbort as e:
            if e.reason == "item_unavailable":
                raise ClaimError(409, "item_unavailable", "This item is reserved for another claim.")
            raise ClaimError(409, "illegal_state", "The claim changed while it was being approved.")
        except DuplicateKeyError:
            raise ClaimError(409, "item_unavailable", "This item is reserved for another claim.")
        logger.info("Claim %s approved by %s; item %s locked.", claim["claimId"], by, claim["itemId"])
        self._notify(issued, "claim_approved",
                     f"Claim approved - pickup code ready for report {self._tracking(issued)}",
                     f"TNSTC Lost & Found: your claim for report {self._tracking(issued)} is approved. "
                     f"Log in to view your pickup code and QR. Valid until {fmt_ist(to_db(expires))}.")
        return issued

    def _token_fields(self, code, nonce, expires, version):
        return {"tokenHash": code_hash(self.token_key, code), "tokenNonce": nonce,
                "tokenExpiresAt": to_db(expires), "tokenUsedAt": None, "tokenVersion": version}

    def cancel(self, claim, by, reason):
        """Cancel from any open state; releases the item lock and returns the match to its hold."""
        now = self.clock()

        def work(tx):
            q, u = sm.transition_update(claim, sm.CANCELLED, now, by, reason,
                                        set_fields={"cancelReason": reason}, unset_fields={"tokenNonce": ""})
            done = tx.cas_update(CLAIMS, q, u, "stale_claim")
            self._release_item_and_match(tx, claim, now, final=False)
            return done

        return self._run_terminal(claim, work)

    def _release_item_and_match(self, tx, claim, now, final):
        if claim["status"] in sm.LOCK_STATES:
            tx.cas_update(FOUND, {"found_id": claim["itemId"], "status": CLAIM_LOCKED,
                                  "claimLock.claimId": claim["claimId"]},
                          {"$set": {"status": "open"}, "$unset": {"claimLock": ""}}, "lock_missing")
        match = self.matches.find_one({"_id": self._match_oid(claim)}) or {}
        if match.get("status") == CLAIM_STARTED:
            hold = match.get("holdExpiresAt")
            back = NOTIFIED if (not final and (hold is None or from_db(hold) > now)) else RELEASED
            fields = {"status": back}
            if back == RELEASED:
                fields.update(releasedAt=to_db(now), releaseReason="claim_ended")
            tx.cas_update(MATCHES, {"_id": match["_id"], "status": CLAIM_STARTED}, {"$set": fields}, "match_changed")

    def _run_terminal(self, claim, work):
        try:
            return run_atomically(self.db, work)
        except sm.IllegalTransition as e:
            raise ClaimError(409, "illegal_state", str(e))
        except ConflictAbort:
            latest = self.claims.find_one({"_id": claim["_id"]}) or claim
            raise ClaimError(409, "illegal_state", f"The claim is {latest.get('status')}.")

    @staticmethod
    def _match_oid(claim):
        try:
            return ObjectId(claim["matchId"])
        except (InvalidId, TypeError):
            return None

    def _expire_if_due(self, claim):
        now = self.clock()
        status = claim["status"]
        due = None
        if status in sm.OPEN_STATES - sm.LOCK_STATES and claim.get("expiresAt") is not None:
            due = from_db(claim["expiresAt"])
        elif status == sm.TOKEN_ISSUED and claim.get("tokenExpiresAt") is not None:
            due = from_db(claim["tokenExpiresAt"])
        if due is None or due > now:
            return claim

        def work(tx):
            q, u = sm.transition_update(claim, sm.EXPIRED, now, "system", "deadline passed",
                                        unset_fields={"tokenNonce": ""})
            done = tx.cas_update(CLAIMS, q, u, "stale_claim")
            self._release_item_and_match(tx, claim, now, final=True)
            return done

        try:
            return run_atomically(self.db, work)
        except (ConflictAbort, sm.IllegalTransition):
            return self.claims.find_one({"_id": claim["_id"]}) or claim

    def expire_due_claims(self) -> int:
        now = to_db(self.clock())
        n = 0
        for claim in self.claims.find({"$or": [
                {"status": {"$in": list(sm.OPEN_STATES - sm.LOCK_STATES)}, "expiresAt": {"$lte": now}},
                {"status": sm.TOKEN_ISSUED, "tokenExpiresAt": {"$lte": now}}]}):
            if self._expire_if_due(claim)["status"] == sm.EXPIRED:
                n += 1
        if n:
            logger.info("Expired %d claim(s).", n)
        return n

    # ── photo (signed, short-lived, bound to the claim) ──────────────────────
    def _photo_sig(self, claim_id, exp):
        return hmac.new(self.photo_key, f"photo|{claim_id}|{exp}".encode(), hashlib.sha256).hexdigest()

    def photo_link(self, claim):
        if claim["status"] not in sm.PHOTO_STATES:
            raise ClaimError(403, "claim_not_verified", "The photo is shown only after your answers are verified.")
        item = self._item(claim)
        if not item.get("image_path"):
            raise ClaimError(404, "no_photo", "The depot did not upload a photo for this item.")
        exp = int((self.clock() + timedelta(seconds=self.config.photo_url_ttl_seconds)).timestamp())
        return {"url": f"/claims/{claim['claimId']}/photo/file?exp={exp}&sig={self._photo_sig(claim['claimId'], exp)}",
                "expiresInSeconds": self.config.photo_url_ttl_seconds}

    def photo_path(self, claim, exp, sig):
        try:
            exp = int(exp)
        except (TypeError, ValueError):
            raise ClaimError(403, "bad_photo_link", "This photo link is invalid.")
        if not isinstance(sig, str) or not hmac.compare_digest(self._photo_sig(claim["claimId"], exp), sig):
            raise ClaimError(403, "bad_photo_link", "This photo link is invalid.")
        if exp <= self.clock().timestamp():
            raise ClaimError(403, "photo_link_expired", "This photo link has expired. Reload the page.")
        if claim["status"] not in sm.PHOTO_STATES:
            raise ClaimError(403, "claim_not_verified", "The photo is no longer available for this claim.")
        rel = (self._item(claim).get("image_path") or "").replace("\\", "/")
        root = os.path.abspath(self.upload_root)
        path = os.path.abspath(os.path.join(root, rel))
        if not rel or not path.startswith(root + os.sep) or not os.path.isfile(path):
            raise ClaimError(404, "no_photo", "The photo is not available.")
        return path

    # ── token view ───────────────────────────────────────────────────────────
    def _code(self, claim):
        return derive_code(self.token_key, claim["claimId"], claim["tokenNonce"])

    def qr_payload(self, claim):
        item = self._item(claim)
        return sign_payload(self.token_key, {
            "v": 1, "cid": claim["claimId"], "iid": claim["itemId"], "tag": item.get("qrTagId"),
            "pnr": claim["pnr"], "exp": int(from_db(claim["tokenExpiresAt"]).timestamp()),
            "code": self._code(claim)})

    @staticmethod
    def qr_svg(data: str) -> str:
        buf = io.BytesIO()
        segno.make(data, error="m").save(buf, kind="svg", scale=5, border=2, xmldecl=False, svgns=True)
        return buf.getvalue().decode("utf-8")

    def token_view(self, claim):
        if claim["status"] != sm.TOKEN_ISSUED:
            raise ClaimError(403, "token_not_available", "A pickup code is issued only after the claim is approved.")
        payload = self.qr_payload(claim)
        item = self._item(claim)
        depot = self.db["depots"].find_one({"phone": claim["depotId"]}) or {}
        return {"code": format_code(self._code(claim)), "qrPayload": payload, "qrSvg": self.qr_svg(payload),
                "expiresAt": iso_utc(claim["tokenExpiresAt"]), "expiresAtIst": fmt_ist(claim["tokenExpiresAt"]),
                "expiresInSeconds": seconds_until(claim["tokenExpiresAt"], self.clock()),
                "depot": depot.get("name") or item.get("depot_name"),
                "itemDescription": public_item_description(item.get("public_description"), 80),
                "reportId": self._tracking(claim)}

    def _tracking(self, claim):
        return (self.lost.find_one({"request_id": claim["reportId"]}) or {}).get("tracking_id")

    def claim_view(self, claim) -> dict:
        item = self._item(claim)
        status = claim["status"]
        return {
            "claimId": claim["claimId"], "matchId": claim["matchId"], "reportId": self._tracking(claim),
            "status": status, "progress": self.progress(claim),
            "photoAvailable": status in sm.PHOTO_STATES and bool(item.get("image_path")),
            "tokenAvailable": status == sm.TOKEN_ISSUED,
            "tokenExpiresAt": iso_utc(claim["tokenExpiresAt"]) if claim.get("tokenExpiresAt") else None,
            "depot": item.get("depot_name"),
            "itemDescription": public_item_description(item.get("public_description"), 80),
            "dateFound": format_found_date(item.get("date")),
            "createdAt": iso_utc(claim["createdAt"]),
        }

    # ── staff ────────────────────────────────────────────────────────────────
    def staff_claim(self, claim_id, depot_phone):
        claim = self.claims.find_one({"claimId": claim_id, "depotId": depot_phone}) \
            if isinstance(claim_id, str) else None
        if not claim:
            raise NotFound()
        return self._expire_if_due(claim)

    def staff_rows(self, depot_phone, status=None):
        query = {"depotId": depot_phone}
        if status:
            query["status"] = status
        rows = []
        for c in self.claims.find(query).sort("updatedAt", -1).limit(200):
            item = self._item(c)
            rows.append({
                "claimId": c["claimId"], "status": c["status"], "reportId": self._tracking(c),
                "claimerName": c["claimerName"], "claimerPhone": mask_phone(c["claimerPhone"]), "pnr": c["pnr"],
                "trustScore": c.get("trustScore"), "roundsUsed": c["roundsUsed"],
                "outcomes": c["outcomePerQuestion"], "idVerified": c.get("idVerified", False),
                "item": {"itemId": c["itemId"], "qrTagId": item.get("qrTagId"),
                         "publicDescription": item.get("public_description"), "status": item.get("status")},
                "updatedAt": iso_utc(c["updatedAt"]),
            })
        return rows

    def staff_approve(self, claim_id, depot_phone, id_verified):
        claim = self.staff_claim(claim_id, depot_phone)
        if claim["status"] not in (sm.STAFF_REVIEW, sm.LOCKED):
            raise ClaimError(409, "illegal_state", f"The claim is {claim['status']}, not awaiting staff review.")
        if claim["status"] == sm.LOCKED and not id_verified:
            raise ClaimError(400, "id_verification_required",
                             "This claim was locked after failed answers: verify the passenger's photo ID first.")
        return self._approve(claim, by=f"staff:{depot_phone}", id_verified=id_verified, staff_id=depot_phone)

    def staff_reject(self, claim_id, depot_phone):
        claim = self.staff_claim(claim_id, depot_phone)
        if claim["status"] not in (sm.STAFF_REVIEW, sm.LOCKED):
            raise ClaimError(409, "illegal_state", f"The claim is {claim['status']}, not awaiting staff review.")
        return self.cancel(claim, by=f"staff:{depot_phone}", reason="rejected_by_staff")

    def reissue_token(self, claim_id, depot_phone, id_verified):
        if id_verified is not True:
            raise ClaimError(400, "id_verification_required",
                             "Verify the passenger's photo ID before reissuing a pickup code.")
        claim = self.staff_claim(claim_id, depot_phone)
        now = self.clock()
        nonce = new_nonce()
        code = derive_code(self.token_key, claim["claimId"], nonce)
        try:
            issued = sm.transition(self.claims, claim, sm.TOKEN_ISSUED, now, f"staff:{depot_phone}",
                                   "reissued after ID check (previous code revoked)",
                                   set_fields={**self._token_fields(code, nonce,
                                                                    now + timedelta(days=self.config.token_ttl_days),
                                                                    claim.get("tokenVersion", 0) + 1),
                                               "idVerified": True, "staffId": depot_phone})
        except sm.IllegalTransition as e:
            raise ClaimError(409, "illegal_state", str(e))
        self._notify(issued, f"token_reissued_v{issued['tokenVersion']}",
                     f"New pickup code issued for report {self._tracking(issued)}",
                     f"TNSTC Lost & Found: a new pickup code was issued for report {self._tracking(issued)} "
                     f"after an ID check at the depot. Your previous code no longer works.")
        return issued

    # ── pickup ───────────────────────────────────────────────────────────────
    def _resolve_token(self, token):
        """(claim, code) for a typed code or a scanned QR payload; raises TokenError(reason)."""
        if is_qr_payload(token):
            payload = verify_payload(self.token_key, token)
            code = normalize_code(payload.get("code"))
            claim = self.claims.find_one({"claimId": payload.get("cid")})
            if not claim:
                raise TokenError("not_found")
            if payload.get("iid") != claim["itemId"] or payload.get("pnr") != claim["pnr"] \
                    or payload.get("tag") != self._item(claim).get("qrTagId"):
                raise TokenError("item_mismatch")
            if claim.get("tokenExpiresAt") is not None \
                    and payload["exp"] != int(from_db(claim["tokenExpiresAt"]).timestamp()):
                raise TokenError("revoked")
        else:
            code = normalize_code(token)
            claim = self.claims.find_one({"tokenHash": code_hash(self.token_key, code)})
            if not claim:
                raise TokenError("not_found")
        if not claim.get("tokenHash") or not hmac.compare_digest(claim["tokenHash"], code_hash(self.token_key, code)):
            raise TokenError("revoked")
        return claim, code

    def _check_pickup(self, token, depot_phone, tag=None, require_tag=False):
        claim, code = self._resolve_token(token)
        claim = self._expire_if_due(claim)
        if claim.get("tokenUsedAt") is not None or claim["status"] == sm.COMPLETED:
            raise TokenError("already_used")
        if claim["status"] == sm.EXPIRED:
            raise TokenError("expired")
        if claim["status"] != sm.TOKEN_ISSUED:
            raise TokenError("not_issued")
        if from_db(claim["tokenExpiresAt"]) <= self.clock():
            raise TokenError("expired")
        if claim["depotId"] != depot_phone:
            raise TokenError("wrong_depot")
        item = self._item(claim)
        if item.get("status") != CLAIM_LOCKED or (item.get("claimLock") or {}).get("claimId") != claim["claimId"]:
            raise TokenError("item_released")
        if tag is not None or require_tag:
            if tag is None:
                raise TokenError("tag_required")
            if normalize_tag(tag) != item.get("qrTagId"):
                raise TokenError("tag_mismatch")
        return claim, item, code

    def pickup_summary(self, claim, item):
        return {"claimId": claim["claimId"], "reportId": self._tracking(claim), "claimerName": claim["claimerName"],
                "claimerPhone": mask_phone(claim["claimerPhone"]), "pnr": claim["pnr"],
                "itemDescription": item.get("public_description"), "qrTagId": item.get("qrTagId"),
                "tokenExpiresAt": iso_utc(claim["tokenExpiresAt"]), "idVerified": claim.get("idVerified", False)}

    def verify_pickup(self, token, depot_phone, tag=None) -> dict:
        try:
            claim, item, _ = self._check_pickup(token, depot_phone, tag)
        except TokenError as e:
            return {"valid": False, "reason": e.reason, "message": PICKUP_REASONS.get(e.reason, e.reason)}
        return {"valid": True, "reason": "ok", "message": "Valid pickup code. Check photo ID, then confirm.",
                "claim": self.pickup_summary(claim, item)}

    def complete_pickup(self, token, depot_phone, tag, id_checked, staff_name=None):
        if id_checked is not True:
            raise ClaimError(400, "id_check_required", PICKUP_REASONS["id_check_required"])
        try:
            claim, item, code = self._check_pickup(token, depot_phone, tag, require_tag=True)
        except TokenError as e:
            raise ClaimError(409, e.reason, PICKUP_REASONS.get(e.reason, e.reason))
        now = self.clock()
        depot = self.db["depots"].find_one({"phone": depot_phone}) or {}

        def work(tx):
            q, u = sm.transition_update(claim, sm.COMPLETED, now, f"staff:{depot_phone}", "handed over",
                                        set_fields={"tokenUsedAt": to_db(now), "completedAt": to_db(now),
                                                    "staffId": depot_phone, "idVerified": True,
                                                    "handoverStaffName": staff_name},
                                        unset_fields={"tokenNonce": ""},
                                        extra_filter={"tokenHash": code_hash(self.token_key, code),
                                                      "tokenUsedAt": None})
            done = tx.cas_update(CLAIMS, q, u, "already_used")
            tx.cas_update(FOUND, {"found_id": claim["itemId"], "status": CLAIM_LOCKED,
                                  "claimLock.claimId": claim["claimId"]},
                          {"$set": {"status": HANDED_OVER, "handedOverAt": to_db(now),
                                    "handedOverClaimId": claim["claimId"]}, "$unset": {"claimLock": ""}},
                          "item_released")
            tx.insert(CUSTODY, {
                "event": "handover", "itemId": claim["itemId"], "qrTagId": item.get("qrTagId"),
                "claimId": claim["claimId"], "matchId": claim["matchId"], "reportId": claim["reportId"],
                "depotId": depot_phone, "depotName": depot.get("name"), "staffId": depot_phone,
                "staffName": staff_name, "idChecked": True, "recipientName": claim["claimerName"],
                "recipientPhone": mask_phone(claim["claimerPhone"]), "pnr": claim["pnr"], "at": to_db(now)})
            tx.cas_update(MATCHES, {"_id": self._match_oid(claim), "status": CLAIM_STARTED},
                          {"$set": {"status": RESOLVED, "resolution": "handed_over", "resolvedAt": to_db(now)}},
                          "match_changed")
            tx.cas_update(LOST, {"request_id": claim["reportId"], "status": {"$in": ["pending", "matched"]}},
                          {"$set": {"status": "resolved", "matched_depot": depot.get("name"),
                                    "matched_at": now.isoformat()}}, "report_closed")
            return done

        try:
            done = run_atomically(self.db, work)
        except ConflictAbort as e:
            reason = e.reason if e.reason in PICKUP_REASONS else "not_issued"
            raise ClaimError(409, reason, PICKUP_REASONS.get(reason, "The handover could not be completed."))
        except sm.IllegalTransition:
            raise ClaimError(409, "already_used", PICKUP_REASONS["already_used"])
        logger.info("Claim %s completed: item %s handed over at %s.", claim["claimId"], claim["itemId"], depot_phone)
        self._close_competitors(done)
        self._notify(done, "pickup_completed", f"Item collected for report {self._tracking(done)}",
                     f"TNSTC Lost & Found: the item for report {self._tracking(done)} was handed over at "
                     f"{depot.get('name') or 'the depot'} on {fmt_ist(to_db(now))}. "
                     f"If this was not you, contact the depot immediately.")
        return done, item

    def _close_competitors(self, winner):
        """After a handover, every other claim and match on the item ends (no details leak)."""
        for other in self.claims.find({"itemId": winner["itemId"], "claimId": {"$ne": winner["claimId"]},
                                       "status": {"$in": list(sm.OPEN_STATES)}}):
            try:
                self.cancel(other, by="system", reason="item_handed_over")
            except ClaimError:
                logger.warning("Could not cancel competing claim %s.", other["claimId"])
        self.match_service._cancel_matches({"itemId": winner["itemId"], "_id": {"$ne": self._match_oid(winner)}},
                                           "item_unavailable")

    # ── custody log and passenger notices ────────────────────────────────────
    def record_item_logged(self, found, depot_phone):
        self.custody.insert_one({"event": "logged", "itemId": found["found_id"], "qrTagId": found.get("qrTagId"),
                                 "depotId": depot_phone, "depotName": found.get("depot_name"),
                                 "staffId": depot_phone, "at": to_db(self.clock())})

    def custody_entries(self, item_id):
        return list(self.custody.find({"itemId": item_id}).sort("at", 1))

    def _notify(self, claim, kind, title, sms_text):
        now = self.clock()
        try:
            self.db[NOTIFICATIONS].insert_one({
                "matchId": claim["matchId"], "reportId": claim["reportId"], "itemId": claim["itemId"],
                "channel": "in_app", "kind": kind, "title": title,
                "link": f"/passenger/claims/{claim['claimId']}", "status": "delivered", "attempts": 0,
                "lastError": None, "sentAt": to_db(now), "createdAt": to_db(now),
                "dedupeKey": f"{claim['reportId']}:{claim['itemId']}:{kind}:{claim['claimId']}:in_app",
            })
        except DuplicateKeyError:
            return
        if self.sms is not None:
            try:
                self.sms.send_message(claim["claimerPhone"], sms_text)
            except Exception:
                logger.exception("Claim %s: passenger SMS (%s) failed; in-app notice stands.", claim["claimId"], kind)
