"""
Flask blueprint: the /auth API, the passenger pages, and `require_passenger`.

    POST /auth/otp/request   {pnr, phone}       → 202 generic (never reveals PNR existence)
    POST /auth/otp/verify    {pnr, phone, otp}  → 200 + session cookie
    POST /auth/logout                           → 200, session revoked, cookie cleared
    GET  /auth/me                               → 200 current passenger (requires session)
    GET  /passenger/login                       → login + OTP screens
    GET  /passenger                             → passenger Lost & Found home (requires session)
"""
import functools
from urllib.parse import urlencode

from flask import (Blueprint, current_app, g, jsonify, make_response, redirect,
                   render_template, request, url_for)
from flask_wtf.csrf import CSRFError

from .bookings import find_eligible_booking
from .errors import AuthError
from .phone import mask_phone
from .service import GENERIC_REQUEST_MESSAGE
from .sessions import COOKIE_NAME, SessionError
from .timeutil import fmt_ist, iso_utc, seconds_until, today_ist
from .validation import read_json_object

bp = Blueprint("passenger_auth", __name__)

FIELD_LIMITS = {"pnr": 32, "phone": 32, "otp": 16}
SESSION_MESSAGES = {
    "missing": ("session_missing", "Please log in with your PNR and mobile number."),
    "invalid": ("session_invalid", "Your session is not valid. Please log in again."),
    "expired": ("session_expired", "Your session has expired. Please log in again."),
    "revoked": ("session_expired", "You have been logged out. Please log in again."),
}


def _state():
    return current_app.extensions["passenger_auth"]


def _client_ip() -> str:
    # remote_addr only; deployments behind a proxy must configure ProxyFix (docs/DECISIONS.md).
    return request.remote_addr or "unknown"


def _json(payload, status=200):
    resp = make_response(jsonify(payload), status)
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _safe_next(target) -> str:
    """Only same-site absolute paths are allowed as post-login redirects."""
    if isinstance(target, str) and target.startswith("/") and not target.startswith("//") \
            and "\\" not in target and len(target) <= 200:
        return target
    return url_for("passenger_auth.passenger_home")


def _passenger_json(passenger, booking=None) -> dict:
    now = _state().clock()
    body = {
        "pnr": passenger.pnr,
        "passengerName": passenger.passenger_name,
        "maskedPhone": mask_phone(passenger.phone),
    }
    out = {"ok": True, "passenger": body,
           "session": {"expiresAt": iso_utc(passenger.expires_at),
                       "expiresInSeconds": seconds_until(passenger.expires_at, now)}}
    if booking is not None:
        out["booking"] = {"route": booking.route, "travelDate": booking.travel_date,
                          "status": booking.status}
    return out


@bp.app_errorhandler(AuthError)
def _handle_auth_error(err):
    resp = _json(err.to_dict(), err.status)
    if err.retry_after is not None:
        resp.headers["Retry-After"] = str(err.retry_after)
    return resp


@bp.errorhandler(CSRFError)
def _handle_csrf(err):
    if request.path.startswith("/auth/"):
        return _handle_auth_error(AuthError(403, "csrf_failed",
                                            "Security token missing or invalid. Reload the page and try again."))
    return err


# ── require_passenger ────────────────────────────────────────────────────────
def authenticate_request():
    """
    Resolve the current request's passenger or raise.

    Raises SessionError for a missing/invalid/expired/revoked session, and
    AuthError(403) when the session is valid but its booking is no longer
    eligible (e.g. cancelled after login) — that session is revoked.
    """
    state = _state()
    passenger = state.sessions.load(request.cookies.get(COOKIE_NAME))
    booking = find_eligible_booking(state.db, passenger.pnr, passenger.phone,
                                    today_ist(state.clock()), state.config.booking_access_window_days)
    if booking is None:
        state.sessions.revoke(passenger.session_id)
        raise AuthError(403, "booking_not_eligible",
                        "This booking is no longer eligible for Lost & Found access.")
    g.passenger = passenger
    g.passenger_booking = booking
    return passenger


def require_passenger(view=None, *, page=False):
    """
    Decorator for routes that need an OTP-verified passenger.

        @require_passenger            → JSON 401/403 on failure (APIs)
        @require_passenger(page=True) → redirect to the login page (HTML views)

    On success `g.passenger` is a Passenger(pnr, phone, passenger_name,
    session_id, expires_at) and `g.passenger_booking` the matched booking.
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                authenticate_request()
            except SessionError as e:
                code, message = SESSION_MESSAGES[e.reason]
                if page:
                    # Only a GET can be replayed after login; a form POST lands on the home page.
                    query = {"next": request.full_path.rstrip("?")} if request.method == "GET" else {}
                    if e.reason in ("expired", "revoked", "invalid"):
                        query["reason"] = "expired"
                    login_url = url_for('passenger_auth.login_page')
                    resp = redirect(f"{login_url}?{urlencode(query)}" if query else login_url)
                    if e.reason != "missing":
                        _state().sessions.clear_cookie(resp)
                    return resp
                resp = _handle_auth_error(AuthError(401, code, message))
                if e.reason != "missing":
                    _state().sessions.clear_cookie(resp)
                return resp
            except AuthError as e:
                resp = (redirect(f"{url_for('passenger_auth.login_page')}?reason=ineligible")
                        if page else _handle_auth_error(e))
                _state().sessions.clear_cookie(resp)
                return resp
            return fn(*args, **kwargs)
        return wrapper
    return decorator(view) if view is not None else decorator


def current_passenger():
    """The authenticated Passenger inside a require_passenger view, else None."""
    return g.get("passenger")


# ── API ──────────────────────────────────────────────────────────────────────
@bp.route("/auth/otp/request", methods=["POST"])
def otp_request():
    body = read_json_object(request, {"pnr": FIELD_LIMITS["pnr"], "phone": FIELD_LIMITS["phone"]})
    result = _state().otp.request_otp(body["pnr"], body["phone"], _client_ip())
    return _json({"ok": True, "message": GENERIC_REQUEST_MESSAGE,
                  "maskedPhone": result.masked_phone,
                  "expiresInSeconds": result.expires_in,
                  "resendAfterSeconds": result.resend_after}, 202)


@bp.route("/auth/otp/verify", methods=["POST"])
def otp_verify():
    body = read_json_object(request, FIELD_LIMITS)
    state = _state()
    booking = state.otp.verify_otp(body["pnr"], body["phone"], body["otp"], _client_ip())
    token, passenger = state.sessions.issue(booking, _client_ip(), request.headers.get("User-Agent", ""))
    payload = _passenger_json(passenger, booking)
    payload["redirect"] = _safe_next(request.args.get("next"))
    resp = _json(payload, 200)
    state.sessions.set_cookie(resp, token)
    return resp


@bp.route("/auth/logout", methods=["POST"])
def logout():
    state = _state()
    state.sessions.revoke_token(request.cookies.get(COOKIE_NAME))
    resp = _json({"ok": True, "message": "Logged out."}, 200)
    state.sessions.clear_cookie(resp)
    return resp


@bp.route("/auth/me", methods=["GET"])
@require_passenger
def me():
    return _json(_passenger_json(g.passenger, g.passenger_booking))


# ── Pages ────────────────────────────────────────────────────────────────────
@bp.route("/passenger/login", methods=["GET"])
def login_page():
    state = _state()
    try:
        state.sessions.load(request.cookies.get(COOKIE_NAME))
        return redirect(_safe_next(request.args.get("next")))
    except SessionError:
        pass
    return render_template(
        "passenger_login.html",
        reason=request.args.get("reason", ""),
        next_url=_safe_next(request.args.get("next")),
        otp_ttl_seconds=state.config.otp_ttl_seconds,
        resend_seconds=state.config.otp_resend_seconds,
    )


@bp.route("/passenger", methods=["GET"])
@require_passenger(page=True)
def passenger_home():
    passenger = g.passenger
    return render_template(
        "passenger_home.html",
        passenger=passenger,
        booking=g.passenger_booking,
        masked_phone=mask_phone(passenger.phone),
        session_expires_iso=iso_utc(passenger.expires_at),
        session_expires_ist=fmt_ist(passenger.expires_at),
        session_expires_in=seconds_until(passenger.expires_at, _state().clock()),
    )
