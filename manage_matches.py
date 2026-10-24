"""
manage_matches.py — operational commands for Feature 2.

    python manage_matches.py run-jobs          # send due notifications once
    python manage_matches.py release-expired   # release matches whose hold expired
    python manage_matches.py rerun --report TRK-XXXXXXXX
    python manage_matches.py rerun --item <found_id>
    python manage_matches.py rerun --all-open  # every open found item

All commands are idempotent and safe to run while the app is running: jobs are
leased atomically and matches/notifications are deduplicated, so a re-run never
sends a notification twice. `run-jobs` / `release-expired` are suitable for cron
when the in-app worker is disabled (NOTIFY_WORKER_ENABLED=0). `rerun` loads the
app (and its ML models) because it uses the real matcher.
"""
import argparse
import os
import sys

from dotenv import load_dotenv


def _service():
    from pymongo import MongoClient

    from match_notify.channels import build_channels
    from match_notify.config import MatchNotifyConfig
    from match_notify.service import MatchNotifyService
    from passenger_auth.timeutil import utcnow

    db = MongoClient(os.environ["MONGO_URI"])["tn_bus_lost_found"]
    config = MatchNotifyConfig.from_env()
    return MatchNotifyService(db, config, build_channels(config, db, os.environ), utcnow)


def main(argv=None):
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run-jobs")
    sub.add_parser("release-expired")
    rerun = sub.add_parser("rerun")
    target = rerun.add_mutually_exclusive_group(required=True)
    target.add_argument("--report")
    target.add_argument("--item")
    target.add_argument("--all-open", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "run-jobs":
        print(f"Processed {_service().process_due_notifications('cli')} notification job(s).")
        return 0
    if args.command == "release-expired":
        print(f"Released {_service().release_expired_matches()} expired match(es).")
        return 0

    os.environ["NOTIFY_WORKER_ENABLED"] = "0"   # the CLI must not start a second background worker
    import app as app_module
    if args.report:
        lost = app_module.lost_collection.find_one({"tracking_id": args.report.strip().upper()})
        if not lost:
            print("No such report.")
            return 1
        print(f"Qualifying matches: {app_module.compute_matches_for_lost(lost)}")
        return 0
    query = {"status": {"$in": [None, "open"]}}
    if args.item:
        query["found_id"] = args.item
    items = list(app_module.found_collection.find(query))
    if args.item and not items:
        print("No such open item.")
        return 1
    for found in items:
        depot = app_module.depots_collection.find_one({"phone": found.get("depot_phone")}) or {}
        app_module.compute_and_save_matches(found, depot)
    print(f"Re-scored {len(items)} item(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
