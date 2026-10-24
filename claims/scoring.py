"""Per-question outcomes and the weighted trust score."""
from .normalize import colour_tokens, match_ratio, tokens

VERIFIED, BORDERLINE, FAIL = "verified", "borderline", "fail"


def answer_passes(key, attribute: dict, answer: str, config, strict=False) -> bool:
    """
    True when the submitted answer matches the stored attribute.
    Colours: any of at most two named colours must equal the expected colour.
    Text: at least CLAIM_ANSWER_MATCH_MIN of the expected words (typo-tolerant).
    `strict` (the extra borderline question): no typo tolerance and 2/3 of the words.
    """
    if attribute["kind"] == "colour":
        named = colour_tokens(answer)
        if len(named) > 2:
            answer_toks = []                       # listing every colour is not an answer
        else:
            answer_toks = named or tokens(answer)  # an unusual colour word stored verbatim
        minimum = 1.0
    else:
        answer_toks = tokens(answer)
        minimum = max(config.answer_match_min, 0.67) if strict else config.answer_match_min
    ratio = match_ratio(key, attribute["entries"], answer_toks, fuzzy=not strict,
                        fuzzy_min=config.fuzzy_min_token_length)
    return ratio >= minimum - 1e-9


def trust_score(outcomes: dict, weights: dict) -> float:
    total = sum(weights[q] for q in outcomes)
    if not total:
        return 0.0
    return round(sum(weights[q] for q, ok in outcomes.items() if ok) / total, 4)


def decide(score: float, config) -> str:
    if score >= config.trust_threshold:
        return VERIFIED
    if score >= config.trust_review_min:
        return BORDERLINE
    return FAIL
