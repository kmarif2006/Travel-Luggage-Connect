"""
Feature 2 — automatic match notification.

    from match_notify import init_match_notify
    state = init_match_notify(app, db, rematch_lost=..., rematch_found=...)
    state.service.record_match(found, lost, score)   # called from the matcher

Requires init_passenger_auth(app, db) first (passenger routes reuse its session).
"""
import logging
import os
from dataclasses import dataclass

from passenger_auth.timeutil import utcnow

from .channels import MESSAGE_LOG, build_channels
from .config import MatchNotifyConfig
from .routes import bp, require_staff
from .service import (ACTIVE_STATUSES, FOUND, LOST, MATCHES, NOTIFICATIONS, MatchNotifyService)
from .worker import NotificationWorker

__all__ = ["init_match_notify", "MatchNotifyConfig", "require_staff", "ACTIVE_STATUSES",
           "ensure_match_indexes"]

logger = logging.getLogger(__name__)


@dataclass
class MatchNotifyState:
    config: MatchNotifyConfig
    service: MatchNotifyService
    worker: NotificationWorker


def ensure_match_indexes(db) -> None:
    specs = [
        (MATCHES, [("reportId", 1), ("itemId", 1)],
         {"unique": True, "partialFilterExpression": {"reportId": {"$exists": True}}}),
        (MATCHES, [("status", 1), ("holdExpiresAt", 1)], {}),
        (MATCHES, [("depot_phone", 1), ("status", 1)], {}),
        (NOTIFICATIONS, [("dedupeKey", 1)], {"unique": True}),
        (NOTIFICATIONS, [("status", 1), ("nextAttemptAt", 1)], {}),
        (NOTIFICATIONS, [("matchId", 1)], {}),
        (NOTIFICATIONS, [("reportId", 1), ("channel", 1)], {}),
        (NOTIFICATIONS, [("depotPhone", 1), ("status", 1)], {}),
        (LOST, [("ownerPnr", 1), ("ownerPhone", 1)], {}),
        (FOUND, [("found_id", 1)], {}),
        (MESSAGE_LOG, [("purgeAt", 1)], {"expireAfterSeconds": 0}),
    ]
    for name, keys, opts in specs:
        try:
            db[name].create_index(keys, **opts)
        except Exception as e:
            logger.warning(f"Index creation skipped for {name} {keys}: {e}")


def init_match_notify(app, db, config=None, channels=None, clock=None, environ=None,
                      rematch_lost=None, rematch_found=None, start_worker=None):
    env = os.environ if environ is None else environ
    config = config or MatchNotifyConfig.from_env(env)
    clock = clock or utcnow
    channels = channels or build_channels(config, db, env, clock=clock)
    service = MatchNotifyService(db, config, channels, clock, rematch_lost, rematch_found)
    migrated = service.backfill_legacy_matches()
    if migrated:
        logger.info("Backfilled %d legacy match document(s) for Feature 2.", migrated)
    ensure_match_indexes(db)
    worker = NotificationWorker(service, config.worker_interval_seconds)
    service.on_enqueue = worker.wake
    state = MatchNotifyState(config=config, service=service, worker=worker)
    app.extensions["match_notify"] = state
    app.register_blueprint(bp)

    should_start = config.worker_enabled if start_worker is None else start_worker
    if should_start:
        worker.start()
    logger.info("Match notification enabled (whatsapp=%s, email=%s, worker=%s).",
                channels["whatsapp"].name, channels["email"].name, "on" if should_start else "off")
    return state
