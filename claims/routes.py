"""
Feature 3 HTTP surface.

Passenger (Feature 1 session; a claim is visible only to the (pnr, phone) that
opened it, and only for a match on that passenger's own report — else 404):
    GET  /claims[?matchId=]          my claims
    POST /claims {matchId}           "This is my item"
    GET  /claims/<id>                status and progress
    POST /claims/<id>/cancel
    GET  /claims/<id>/questions      current questions (ids + text only)
    POST /claims/<id>/answers        {answers: {questionId: text}} → overall progress only
    GET  /claims/<id>/photo          signed short-lived URL, only after verification
    GET  /claims/<id>/photo/file     the image, via that URL
    GET  /claims/<id>/token          code + QR, only after approval
    GET  /claims/<id>/token.svg      QR download
    page /passenger/claims/<id>

Staff (depot login, scoped to the depot holding the item):
    GET  /staff/claims?status=staff_review
    POST /staff/claims/<id>/approve {idVerified?} | /reject
    POST /staff/claims/<id>/reissue-token {idVerified: true}
    POST /pickup/verify {token, qrTagId?}
    POST /pickup/complete {token, qrTagId, idChecked: true, staffName?}
    GET  /staff/items/<foundId>/tag  printable QR tag
    page /staff/claims-desk

Every endpoint here is rate limited per client IP (CLAIM_IP_LIMIT per window).
"""
import functools

from flask import (Blueprint, Response, current_app, g, jsonify, make_response, render_template,
                   request, send_file)
from flask_wtf.csrf import CSRFError

from match_notify.routes import require_staff
from passenger_auth import require_passenger
from passenger_auth.errors import AuthError

from .service import ClaimError, NotFound
from .statemachine import TRANSITIONS
from .tokens import TokenError, normalize_tag

bp = Blueprint("claims", __name__)
CLAIM_STATUSES = tuple(TRANSITIONS)


def _state():
    return current_app.extensions["claims"]


def _svc():
    return _state().service


def _json(payload, status=200):
    resp = make_response(jsonify(payload), status)
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _ip():
    return request.remote_addr or "unknown"


def rate_limited(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        cfg = _state().config
        decision = _state().limiter.hit_window("claims-ip", _ip(), cfg.ip_limit, cfg.ip_window_seconds)
        if not decision.allowed:
            raise AuthError(429, "rate_limited", "Too many requests. Please slow down.",
                            retry_after=decision.retry_after)
        return fn(*args, **kwargs)
    return wrapper


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except NotFound:
        raise AuthError(404, "not_found", "Not found.")
    except ClaimError as e:
        raise AuthError(e.status, e.code, e.message, **e.extra)


@bp.errorhandler(CSRFError)
def _handle_csrf(err):
    return _json(AuthError(403, "csrf_failed",
                           "Security token missing or invalid. Reload the page and try again.").to_dict(), 403)


def _body(allowed: dict) -> dict:
    """JSON object with only `allowed` keys, each of the given type. Size-capped."""
    if request.content_length is not None and request.content_length > 8192:
        raise AuthError(413, "payload_too_large", "Request body is too large.")
    data = request.get_json(silent=True)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise AuthError(400, "invalid_request", "Request body must be a JSON object.")
    problems = {}
    for key, value in data.items():
        if key not in allowed:
            problems[str(key)[:40]] = "unknown_field"
        elif not isinstance(value, allowed[key]):
            problems[key] = "invalid_type"
    if problems:
        raise AuthError(400, "invalid_request", "Some fields are missing or invalid.", fields=problems)
    return data


def _passenger_claim(claim_id):
    return _call(_svc().claim_for_passenger, claim_id, g.passenger)


# ── passenger API ────────────────────────────────────────────────────────────
@bp.route("/claims", methods=["GET"])
@require_passenger
@rate_limited
def list_claims():
    svc = _svc()
    match_id = request.args.get("matchId")
    return _json({"ok": True, "claims": [svc.claim_view(c) for c in svc.claims_for_passenger(g.passenger, match_id)]})


@bp.route("/claims", methods=["POST"])
@require_passenger
@rate_limited
def open_claim():
    body = _body({"matchId": str})
    if not body.get("matchId") or len(body["matchId"]) > 64:
        raise AuthError(400, "invalid_request", "matchId is required.", fields={"matchId": "required"})
    svc = _svc()
    claim = _call(svc.open_claim, g.passenger, body["matchId"])
    return _json({"ok": True, "claim": svc.claim_view(claim)}, 201)


@bp.route("/claims/<claim_id>", methods=["GET"])
@require_passenger
@rate_limited
def get_claim(claim_id):
    return _json({"ok": True, "claim": _svc().claim_view(_passenger_claim(claim_id))})


@bp.route("/claims/<claim_id>/cancel", methods=["POST"])
@require_passenger
@rate_limited
def cancel_claim(claim_id):
    svc = _svc()
    claim = _passenger_claim(claim_id)
    done = _call(svc.cancel, claim, by="passenger", reason="cancelled_by_passenger")
    return _json({"ok": True, "claim": svc.claim_view(done)})


@bp.route("/claims/<claim_id>/questions", methods=["GET"])
@require_passenger
@rate_limited
def get_questions(claim_id):
    svc = _svc()
    claim = _passenger_claim(claim_id)
    questions = _call(svc.questions, claim)
    return _json({"ok": True, "questions": questions, "progress": svc.progress(claim)})


@bp.route("/claims/<claim_id>/answers", methods=["POST"])
@require_passenger
@rate_limited
def post_answers(claim_id):
    body = _body({"answers": dict})
    answers = body.get("answers")
    if not answers:
        raise AuthError(400, "invalid_request", "answers is required.", fields={"answers": "required"})
    limit = _state().config.max_answer_chars
    problems = {}
    for qid, text in answers.items():
        if not isinstance(qid, str) or len(qid) > 40:
            problems[str(qid)[:40]] = "unknown_field"
        elif not isinstance(text, str):
            problems[qid] = "must_be_string"
        elif not text.strip():
            problems[qid] = "required"
        elif len(text) > limit:
            problems[qid] = "too_long"
    if problems:
        raise AuthError(400, "invalid_request", "Some answers are missing or invalid.", fields=problems)
    svc = _svc()
    claim = _passenger_claim(claim_id)
    updated = _call(svc.submit_answers, claim, dict(answers), g.passenger)
    return _json({"ok": True, "claim": svc.claim_view(updated)})


@bp.route("/claims/<claim_id>/photo", methods=["GET"])
@require_passenger
@rate_limited
def photo_link(claim_id):
    return _json({"ok": True, **_call(_svc().photo_link, _passenger_claim(claim_id))})


@bp.route("/claims/<claim_id>/photo/file", methods=["GET"])
@require_passenger
@rate_limited
def photo_file(claim_id):
    claim = _passenger_claim(claim_id)
    path = _call(_svc().photo_path, claim, request.args.get("exp"), request.args.get("sig"))
    resp = send_file(path, max_age=0, conditional=False)
    resp.headers["Cache-Control"] = "no-store, private"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


@bp.route("/claims/<claim_id>/token", methods=["GET"])
@require_passenger
@rate_limited
def token(claim_id):
    return _json({"ok": True, "token": _call(_svc().token_view, _passenger_claim(claim_id))})


@bp.route("/claims/<claim_id>/token.svg", methods=["GET"])
@require_passenger
@rate_limited
def token_svg(claim_id):
    view = _call(_svc().token_view, _passenger_claim(claim_id))
    resp = Response(view["qrSvg"], mimetype="image/svg+xml")
    resp.headers["Content-Disposition"] = f'attachment; filename="tnstc-pickup-{view["reportId"]}.svg"'
    resp.headers["Cache-Control"] = "no-store, private"
    return resp


@bp.route("/passenger/claims/<claim_id>", methods=["GET"])
@require_passenger(page=True)
def claim_page(claim_id):
    try:
        claim = _svc().claim_for_passenger(claim_id, g.passenger)
    except NotFound:
        return render_template("passenger_not_found.html"), 404
    return render_template("passenger_claim.html", claim=_svc().claim_view(claim))


# ── staff API ────────────────────────────────────────────────────────────────
@bp.route("/staff/claims", methods=["GET"])
@require_staff
@rate_limited
def staff_claims():
    status = request.args.get("status") or None
    if status is not None and status not in CLAIM_STATUSES:
        raise AuthError(400, "invalid_request", "Unknown status filter.", fields={"status": "invalid"})
    return _json({"ok": True, "claims": _svc().staff_rows(g.staff_depot["phone"], status)})


@bp.route("/staff/claims/<claim_id>/approve", methods=["POST"])
@require_staff
@rate_limited
def staff_approve(claim_id):
    body = _body({"idVerified": bool})
    claim = _call(_svc().staff_approve, claim_id, g.staff_depot["phone"], body.get("idVerified", False))
    return _json({"ok": True, "claimId": claim["claimId"], "status": claim["status"]})


@bp.route("/staff/claims/<claim_id>/reject", methods=["POST"])
@require_staff
@rate_limited
def staff_reject(claim_id):
    claim = _call(_svc().staff_reject, claim_id, g.staff_depot["phone"])
    return _json({"ok": True, "claimId": claim["claimId"], "status": claim["status"]})


@bp.route("/staff/claims/<claim_id>/reissue-token", methods=["POST"])
@require_staff
@rate_limited
def staff_reissue(claim_id):
    body = _body({"idVerified": bool})
    claim = _call(_svc().reissue_token, claim_id, g.staff_depot["phone"], body.get("idVerified"))
    return _json({"ok": True, "claimId": claim["claimId"], "status": claim["status"],
                  "tokenVersion": claim["tokenVersion"]})


def _token_input(body):
    token = body.get("token")
    if not token or len(token) > 1024:
        raise AuthError(400, "invalid_request", "Scan the QR or type the pickup code.", fields={"token": "required"})
    return token


@bp.route("/pickup/verify", methods=["POST"])
@require_staff
@rate_limited
def pickup_verify():
    body = _body({"token": str, "qrTagId": str})
    result = _svc().verify_pickup(_token_input(body), g.staff_depot["phone"], body.get("qrTagId") or None)
    return _json({"ok": True, **result})


@bp.route("/pickup/complete", methods=["POST"])
@require_staff
@rate_limited
def pickup_complete():
    body = _body({"token": str, "qrTagId": str, "idChecked": bool, "staffName": str})
    staff_name = (body.get("staffName") or "").strip()[:60] or None
    claim, item = _call(_svc().complete_pickup, _token_input(body), g.staff_depot["phone"],
                        body.get("qrTagId") or None, body.get("idChecked"), staff_name)
    return _json({"ok": True, "claimId": claim["claimId"], "status": claim["status"],
                  "itemStatus": "handed_over", "qrTagId": item.get("qrTagId")})


@bp.route("/staff/items/<found_id>/tag", methods=["GET"])
@require_staff(page=True)
def item_tag(found_id):
    svc = _svc()
    item = svc.found.find_one({"found_id": found_id, "depot_phone": g.staff_depot["phone"]})
    if not item or not item.get("qrTagId"):
        return render_template("passenger_not_found.html"), 404
    try:
        tag = normalize_tag(item["qrTagId"])
    except TokenError:
        return render_template("passenger_not_found.html"), 404
    return render_template("staff_item_tag.html", item=item, tag=tag,
                           qr_svg=svc.qr_svg(f"TNSTC-TAG:{tag}"), custody=svc.custody_entries(found_id))


@bp.route("/staff/claims-desk", methods=["GET"])
@require_staff(page=True)
def claims_desk():
    return render_template("staff_claims.html", depot=g.staff_depot)
