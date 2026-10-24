"""
Feature 3 — secure claims and QR pickup tokens.

    from claims import init_claims
    init_claims(app, db)        # after init_passenger_auth and init_match_notify

Also guards /static/uploads/* (found-item photos) so only depot staff can fetch
them directly; passengers see a photo only through a claim's signed URL.
"""
import logging
import os
from dataclasses import dataclass

from flask import abort, request, session

from passenger_auth.ratelimit import RateLimiter
from passenger_auth.timeutil import utcnow

from .attributes import ATTRIBUTES, CATEGORIES, encode_private_attributes
from .config import ClaimsConfig
from .routes import bp
from .service import CLAIMS, CUSTODY, FOUND, ClaimsService
from .tokens import new_tag_id

__all__ = ["init_claims", "ClaimsConfig", "ensure_claim_indexes", "ATTRIBUTES", "CATEGORIES",
           "encode_private_attributes", "new_tag_id"]

logger = logging.getLogger(__name__)


@dataclass
class ClaimsState:
    config: ClaimsConfig
    service: ClaimsService
    limiter: RateLimiter


def ensure_claim_indexes(db) -> None:
    specs = [
        (CLAIMS, [("claimId", 1)], {"unique": True}),
        (CLAIMS, [("matchId", 1)], {"unique": True, "partialFilterExpression": {"openForMatch": True}}),
        (CLAIMS, [("itemId", 1)], {"unique": True, "partialFilterExpression": {"lockActive": True}}),
        (CLAIMS, [("tokenHash", 1)], {}),
        (CLAIMS, [("pnr", 1), ("claimerPhone", 1)], {}),
        (CLAIMS, [("depotId", 1), ("status", 1)], {}),
        (CLAIMS, [("status", 1), ("expiresAt", 1)], {}),
        (CUSTODY, [("itemId", 1), ("at", 1)], {}),
        (FOUND, [("qrTagId", 1)], {"unique": True, "partialFilterExpression": {"qrTagId": {"$exists": True}}}),
    ]
    for name, keys, opts in specs:
        try:
            db[name].create_index(keys, **opts)
        except Exception as e:
            logger.warning(f"Index creation skipped for {name} {keys}: {e}")


def init_claims(app, db, config=None, clock=None, environ=None, upload_root=None):
    env = os.environ if environ is None else environ
    config = config or ClaimsConfig.from_env(env)
    auth = app.extensions["passenger_auth"]
    notify = app.extensions["match_notify"]
    clock = clock or utcnow
    limiter = RateLimiter(db, clock)
    service = ClaimsService(db, config, auth.config.signing_secret, clock, limiter, notify.service,
                            sms=auth.sms, upload_root=upload_root or os.path.join(app.root_path, "static"))
    ensure_claim_indexes(db)
    app.extensions["claims"] = ClaimsState(config=config, service=service, limiter=limiter)
    app.register_blueprint(bp)
    notify.worker.add_task(service.expire_due_claims)

    @app.context_processor
    def _claim_catalogue():
        return {"claim_catalogue": {"categories": CATEGORIES,
                                    "attributes": {k: {"label": v["label"], "categories": sorted(v["categories"])}
                                                   for k, v in ATTRIBUTES.items()}}}

    @app.before_request
    def _guard_uploaded_photos():
        # Found-item photos are private: staff only. Passengers use the signed claim URL.
        if request.path.startswith("/static/uploads/"):
            phone = session.get("depot_phone")
            if not (isinstance(phone, str) and db["depots"].find_one({"phone": phone})):
                abort(404)

    logger.info("Claims enabled (trust threshold %.2f, review band from %.2f).",
                config.trust_threshold, config.trust_review_min)
    return app.extensions["claims"]
