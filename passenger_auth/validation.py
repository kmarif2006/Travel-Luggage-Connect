"""
Strict JSON body validation for the auth endpoints.

Every field must be present, must be a JSON string (never an object/array —
that is how NoSQL operator injection like {"$ne": null} arrives), must be no
longer than its limit, and unknown keys are rejected.
"""
from .errors import AuthError

MAX_BODY_BYTES = 2048


def read_json_object(request, fields: dict) -> dict:
    """`fields` maps field name → max length. Returns {name: str}."""
    if request.content_length is not None and request.content_length > MAX_BODY_BYTES:
        raise AuthError(413, "payload_too_large", "Request body is too large.")
    raw = request.get_data(cache=True)
    if len(raw) > MAX_BODY_BYTES:
        raise AuthError(413, "payload_too_large", "Request body is too large.")
    if not request.is_json:
        raise AuthError(400, "invalid_request", "Request body must be JSON (Content-Type: application/json).")
    data = request.get_json(silent=True)
    if data is None:
        raise AuthError(400, "invalid_request", "Request body is not valid JSON.")
    if not isinstance(data, dict):
        raise AuthError(400, "invalid_request", "Request body must be a JSON object.")

    problems = {}
    for key in data:
        if key not in fields:
            problems[str(key)[:40]] = "unknown_field"
    for name, max_len in fields.items():
        if name not in data:
            problems[name] = "required"
        elif not isinstance(data[name], str):
            problems[name] = "must_be_string"
        elif not data[name].strip():
            problems[name] = "required"
        elif len(data[name]) > max_len:
            problems[name] = "too_long"
    if problems:
        raise AuthError(400, "invalid_request", "Some fields are missing or invalid.", fields=problems)
    return {name: data[name] for name in fields}
