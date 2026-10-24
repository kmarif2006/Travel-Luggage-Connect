"""
Feature 2 HTTP surface.

Passenger (Feature 1 login via require_passenger; object-level authorization —
anything not owned by the session's (pnr, phone) is a 404, indistinguishable from
a missing id):
    GET   /reports                      my reports
    PATCH /reports/<trackingId>         update description → re-runs the matcher
    POST  /reports/<trackingId>/cancel  cancel a report (active matches end)
    GET   /reports/<trackingId>/matches privacy-safe matches for a report
    GET   /matches/<matchId>            privacy-safe match view
    GET   /notifications                in-app notifications
    pages: /passenger/reports/<id>, /passenger/matches/<id>, /passenger/notifications

Staff (existing depot login; every query is scoped to the staff member's depot):
    GET  /staff/matches?status=staff_review
    POST /staff/matches/<id>/approve | /reject
    GET  /staff/notifications?status=failed
    POST /staff/notifications/<id>/retry
    POST /staff/matcher/rerun           {"itemId"?}
    page: /staff/review
"""
import functools

from flask import (Blueprint, current_app, g, jsonify, make_response, redirect, render_template,
                   request, session, url_for)
from flask_wtf.csrf import CSRFError

from passenger_auth import require_passenger
from passenger_auth.errors import AuthError
from passenger_auth.sessions import COOKIE_NAME, SessionError
from passenger_auth.validation import read_json_object

from .service import ALL_STATUSES, PENDING_REVIEW, Conflict, NotFound

bp = Blueprint("match_notify", __name__)

STATUS_ALIASES = {"staff_review": PENDING_REVIEW}
NOTIFICATION_STATUSES = ("pending", "sent", "failed", "delivered", "cancelled")


def _svc():
    return current_app.extensions["match_notify"].service


def _json(payload, status=200):
    resp = make_response(jsonify(payload), status)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@bp.errorhandler(CSRFError)
def _handle_csrf(err):
    resp = _json(AuthError(403, "csrf_failed",
                           "Security token missing or invalid. Reload the page and try again.").to_dict(), 403)
    return resp


def _not_found():
    return AuthError(404, "not_found", "Not found.")


def _call(fn, *args):
    try:
        return fn(*args)
    except NotFound:
        raise _not_found()
    except Conflict as e:
        raise AuthError(409, "conflict", str(e))


# ── staff auth ───────────────────────────────────────────────────────────────
def _has_passenger_session() -> bool:
    try:
        current_app.extensions["passenger_auth"].sessions.load(request.cookies.get(COOKIE_NAME))
        return True
    except SessionError:
        return False


def require_staff(view=None, *, page=False):
    """
    Depot staff only (the existing depot login: session['depot_phone']).
    Not logged in as staff → 401 (403 if the caller is a logged-in passenger);
    pages redirect to the depot login instead.
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            phone = session.get("depot_phone")
            depot = current_app.extensions["match_notify"].service.depots.find_one({"phone": phone}) \
                if isinstance(phone, str) else None
            if not depot:
                if page:
                    return redirect(url_for("depot_login_page"))
                if _has_passenger_session():
                    raise AuthError(403, "staff_only", "This action is restricted to depot staff.")
                raise AuthError(401, "staff_login_required", "Depot staff login required.")
            g.staff_depot = depot
            return fn(*args, **kwargs)
        return wrapper
    return decorator(view) if view is not None else decorator


# ── passenger API ────────────────────────────────────────────────────────────
def _report_json(lost):
    return {"trackingId": lost.get("tracking_id"), "status": lost.get("status", "pending"),
            "routeName": lost.get("route_name"), "date": lost.get("date"),
            "description": lost.get("description")}


@bp.route("/reports", methods=["GET"])
@require_passenger
def my_reports():
    return _json({"ok": True, "reports": [_report_json(r) for r in _svc().reports_for(g.passenger)]})


@bp.route("/reports/<tracking_id>/matches", methods=["GET"])
@require_passenger
def report_matches(tracking_id):
    svc = _svc()
    lost = _call(svc.owned_report, tracking_id, g.passenger)
    views = [svc.match_view(m, lost) for m in svc.visible_matches_for_report(lost)]
    return _json({"ok": True, "report": _report_json(lost), "matches": views})


@bp.route("/reports/<tracking_id>", methods=["PATCH"])
@require_passenger
def update_report(tracking_id):
    svc = _svc()
    lost = _call(svc.owned_report, tracking_id, g.passenger)
    body = read_json_object(request, {"description": 1000})
    _call(svc.update_report_description, lost, body["description"].strip())
    return _json({"ok": True, "report": _report_json(svc.owned_report(tracking_id, g.passenger))})


@bp.route("/reports/<tracking_id>/cancel", methods=["POST"])
@require_passenger
def cancel_report(tracking_id):
    svc = _svc()
    lost = _call(svc.owned_report, tracking_id, g.passenger)
    _call(svc.cancel_report, lost)
    return _json({"ok": True, "report": _report_json(svc.owned_report(tracking_id, g.passenger))})


@bp.route("/matches/<match_id>", methods=["GET"])
@require_passenger
def match_detail(match_id):
    svc = _svc()
    match, lost = _call(svc.match_for_passenger, match_id, g.passenger)
    return _json({"ok": True, "match": svc.match_view(match, lost)})


@bp.route("/notifications", methods=["GET"])
@require_passenger
def notifications():
    return _json({"ok": True, "notifications": _svc().notifications_for(g.passenger)})


# ── passenger pages ──────────────────────────────────────────────────────────
@bp.route("/passenger/reports/<tracking_id>", methods=["GET"])
@require_passenger(page=True)
def report_page(tracking_id):
    svc = _svc()
    try:
        lost = svc.owned_report(tracking_id, g.passenger)
    except NotFound:
        return render_template("passenger_not_found.html"), 404
    views = [svc.match_view(m, lost) for m in svc.visible_matches_for_report(lost)]
    return render_template("passenger_report.html", report=lost, matches=views)


@bp.route("/passenger/matches/<match_id>", methods=["GET"])
@require_passenger(page=True)
def match_page(match_id):
    svc = _svc()
    try:
        match, lost = svc.match_for_passenger(match_id, g.passenger)
    except NotFound:
        return render_template("passenger_not_found.html"), 404
    return render_template("passenger_match.html", match=svc.match_view(match, lost))


@bp.route("/passenger/notifications", methods=["GET"])
@require_passenger(page=True)
def notifications_page():
    return render_template("passenger_notifications.html",
                           notifications=_svc().notifications_for(g.passenger))


# ── staff API ────────────────────────────────────────────────────────────────
def _status_filter(allowed, aliases=None):
    raw = request.args.get("status")
    if raw is None or raw == "":
        return None
    value = (aliases or {}).get(raw, raw)
    if value not in allowed:
        raise AuthError(400, "invalid_request", "Unknown status filter.", fields={"status": "invalid"})
    return value


@bp.route("/staff/matches", methods=["GET"])
@require_staff
def staff_matches():
    status = _status_filter(ALL_STATUSES, STATUS_ALIASES)
    return _json({"ok": True, "matches": _svc().staff_match_rows(g.staff_depot["phone"], status)})


@bp.route("/staff/matches/<match_id>/approve", methods=["POST"])
@require_staff
def staff_approve(match_id):
    match = _call(_svc().approve, match_id, g.staff_depot["phone"])
    return _json({"ok": True, "matchId": str(match["_id"]), "status": match["status"]})


@bp.route("/staff/matches/<match_id>/reject", methods=["POST"])
@require_staff
def staff_reject(match_id):
    match = _call(_svc().reject, match_id, g.staff_depot["phone"])
    return _json({"ok": True, "matchId": str(match["_id"]), "status": match["status"]})


@bp.route("/staff/notifications", methods=["GET"])
@require_staff
def staff_notifications():
    status = _status_filter(NOTIFICATION_STATUSES)
    return _json({"ok": True,
                  "notifications": _svc().staff_notification_rows(g.staff_depot["phone"], status)})


@bp.route("/staff/notifications/<notification_id>/retry", methods=["POST"])
@require_staff
def staff_retry(notification_id):
    job = _call(_svc().retry_notification, notification_id, g.staff_depot["phone"])
    return _json({"ok": True, "id": str(job["_id"]), "status": job["status"]})


@bp.route("/staff/matcher/rerun", methods=["POST"])
@require_staff
def staff_rerun():
    data = request.get_json(silent=True)
    item_id = None
    if data not in (None, {}):
        if not isinstance(data, dict) or set(data) - {"itemId"} or \
                not isinstance(data.get("itemId"), str) or len(data["itemId"]) > 64:
            raise AuthError(400, "invalid_request", "Body must be {} or {\"itemId\": \"...\"}.")
        item_id = data["itemId"]
    n = _call(_svc().rerun_for_depot, g.staff_depot["phone"], item_id)
    return _json({"ok": True, "itemsRescored": n})


@bp.route("/staff/review", methods=["GET"])
@require_staff(page=True)
def staff_review_page():
    return render_template("staff_review.html", depot=g.staff_depot)
