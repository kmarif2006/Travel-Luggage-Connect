"""Pure decision helpers: confidence routing, dedupe keys and retry backoff."""

AUTO = "auto"
REVIEW = "review"
REJECT = "reject"


def route_confidence(confidence: float, config) -> str:
    """
    >= MATCH_AUTO_NOTIFY_MIN → auto-notify
    >= MATCH_REVIEW_MIN      → staff review (nothing is sent before approval)
    otherwise                → reject, never notified
    Bounds are inclusive at each threshold.
    """
    if confidence >= config.auto_notify_min:
        return AUTO
    if confidence >= config.review_min:
        return REVIEW
    return REJECT


def dedupe_key(report_id: str, item_id: str, channel: str) -> str:
    """One notification per (report, item, channel), enforced by a unique index."""
    return f"{report_id}:{item_id}:{channel}"


def backoff_seconds(attempt: int, config) -> int:
    """Delay before retry number `attempt` (1-based): base · 2^(attempt-1), capped."""
    return min(config.backoff_max_seconds, config.backoff_base_seconds * (2 ** max(0, attempt - 1)))
