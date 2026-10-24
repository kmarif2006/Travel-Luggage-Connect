"""Single error type for the auth API, rendered as one consistent JSON shape."""


class AuthError(Exception):
    """
    Rendered as:
        {"ok": false, "error": {"code": ..., "message": ..., **extra}}
    with `status` as the HTTP code and a Retry-After header when retry_after is set.
    """

    def __init__(self, status: int, code: str, message: str, retry_after=None, **extra):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after
        self.extra = extra

    def to_dict(self) -> dict:
        body = {"code": self.code, "message": self.message}
        if self.retry_after is not None:
            body["retryAfterSeconds"] = self.retry_after
        body.update(self.extra)
        return {"ok": False, "error": body}
