# Feature 3 Verification — Secure Claims & QR Pickup Token

Each rule and edge case from the Feature 3 brief, mapped to the test(s) that prove it.

```bash
python -m pytest tests -q
```

Result at time of writing: **465 passed**. That is 150 Feature 3 tests, plus 134 for Feature 2,
165 for Feature 1 and 16 scorer tests (all regressions green).
`U` = `tests/test_claims_unit.py`, `A` = `tests/test_claims_api.py`, `E` = `tests/test_claims_e2e.py`.
`E` runs the **real `app.py`** routes. Only the ML models are stubbed, and MongoDB is mongomock.

## 1. Rules

| # | Rule | Test(s) |
|---|---|---|
| R1 | `claims` collection fields (claimId, matchId, reportId, itemId, pnr, claimer, status, questionSet, roundsUsed, outcomePerQuestion, trustScore, token fields, staffId, depotId, timestamps) | A `test_open_answer_approve_token_pickup`, A `test_claim_record_holds_only_ids_and_outcomes` |
| R2 | Private attributes on found items + staff entry at logging time | E `test_full_journey_all_three_features` (category + 4 attributes posted through `/depot/found`; an inapplicable one is dropped) |
| R3 | One active claim per item (unique partial index) | A `test_database_unique_index_backs_the_item_lock`, A `test_two_passengers_claiming_same_item_simultaneously` |
| R4 | One open claim per match (no duplicates) | A `test_duplicate_claim_by_same_passenger`, A `test_parallel_duplicate_claims_create_one` |
| R5 | State machine; illegal transitions rejected; every transition logged | U `test_illegal_transitions_rejected`, U `test_transition_cas_history_and_index_flags`, U `test_every_state_is_reachable_and_terminals_are_final`, A `test_staff_reject_and_illegal_transitions`, A `test_wrong_answers_then_retry_reveals_only_progress` (history) |
| R6 | Passenger routes require Feature 1 login + object-level authorization | A `test_idor_on_someone_elses_match_and_claim`, A `test_unauthenticated_passenger_endpoints` |
| R7 | Endpoints: POST /claims, GET /claims/:id, cancel, questions, answers, photo, token | A `test_open_answer_approve_token_pickup`, A `test_passenger_cancel_unlocks_item_for_others` |
| R8 | Staff endpoints: queue, approve/reject, pickup verify/complete, reissue | A `test_lockout_after_max_rounds_then_staff_id_check`, A `test_staff_approve_borderline_issues_token`, A `test_staff_reject_and_illegal_transitions`, A `test_lost_phone_reissue_revokes_old_token`, A `test_open_answer_approve_token_pickup` |
| R9 | 2–4 questions from private attributes, varied by category and randomised | U `test_questions_vary_by_category`, U `test_question_count_rules`, U `test_selection_is_randomised`, U `test_next_round_prefers_unasked_questions` |
| R10 | Never answerable from the Feature 2 notification text | U `test_attributes_visible_in_notification_text_are_never_asked`, A `test_questions_never_answerable_from_notification` |
| R11 | Never reveal correct answers or which answer was wrong | A `test_wrong_answers_then_retry_reveals_only_progress`, A `test_questions_never_answerable_from_notification` (questions are id + text only), A `test_open_answer_approve_token_pickup` (no trust score in passenger view) |
| R12 | Borderline score → one more (stricter) question → staff | A `test_borderline_adds_question_then_routes_to_staff`, A `test_borderline_follow_up_can_verify`, A `test_borderline_follow_up_is_strict`, U `test_strict_mode_disallows_typos` |
| R13 | Normalization (case, whitespace, NFC, punctuation) | U `test_normalize_case_whitespace_punctuation_emoji`, U `test_normalize_keeps_tamil_vowel_signs_and_nfc`, U `test_tokens_drop_stopwords_and_fold_phrases` |
| R14 | Fuzzy matching with tolerance | U `test_colour_typos_within_tolerance`, U `test_text_matching_partial_and_typos`, U `test_short_tokens_are_exact_only` |
| R15 | Synonym/colour maps in English, Tamil, Tanglish | U `test_colour_synonyms_english_tamil_tanglish`, U `test_object_synonyms_in_three_languages`, A `test_answers_in_tamil_tanglish_english_and_typos` |
| R16 | Per-question weights and trust score vs TRUST_THRESHOLD and review band | U `test_trust_score_is_weighted`, U `test_threshold_boundaries`, U `test_config_validation` |
| R17 | Expected answers stored only as keyed hashes; submitted answers never stored | U `test_stored_form_contains_no_raw_answer`, A `test_claim_record_holds_only_ids_and_outcomes`, A `test_emoji_and_injection_answers_are_just_wrong`, E (raw values absent from the item) |
| R18 | Max 3 rounds then locked → staff ID verification | A `test_lockout_after_max_rounds_then_staff_id_check` |
| R19 | Daily caps per passenger (claims, answer attempts) | A `test_daily_claim_cap`, A `test_daily_answer_cap` |
| R20 | Rate limits on all claim endpoints | A `test_ip_rate_limit_on_claim_endpoints` |
| R21 | Photo only via authorized, signed, short-lived URL bound to the claim, after threshold | A `test_signed_photo_url_expiry_and_binding`, A `test_skipping_to_photo_or_token_without_passing` |
| R22 | Photo never in any list, notification or match view; uploads not public | A `test_photo_never_in_lists_notifications_or_match_view`, A `test_found_item_photos_are_not_publicly_served`, E (passenger 404, staff 200) |
| R23 | Approval locks the item by atomic compare-and-set; never two approved | A `test_two_passengers_claiming_same_item_simultaneously`, U `test_unit_of_work_concurrent_cas_only_one_wins` |
| R24 | Other matches show "unavailable" without leaking | A `test_two_passengers_claiming_same_item_simultaneously` |
| R25 | Cancel / expiry unlocks the item | A `test_passenger_cancel_unlocks_item_for_others`, A `test_token_expiry_boundary_and_release`, A `test_claim_expires_with_match_hold_and_frees_item` |
| R26 | Code `XXXX-XXXX` from an alphabet without 0/O/1/I; CSPRNG-backed | U `test_code_alphabet_and_format`, U `test_code_derivation_is_keyed_and_nonce_dependent`, U `test_normalize_code` |
| R27 | HMAC-signed QR payload (claimId, itemId, qrTagId, pnr, exp) | U `test_signed_payload_roundtrip_and_tamper_detection`, A `test_forged_or_tampered_qr` |
| R28 | TTL 7 days, single use, only the hash stored, constant-time compare | A `test_token_expiry_boundary_and_release`, A `test_reused_token`, U `test_code_hash_is_keyed` |
| R29 | Passenger token screen: code, QR, download/print, countdown | A `test_open_answer_approve_token_pickup` (code, SVG, expiresInSeconds, `token.svg` attachment), E |
| R30 | Pickup verify: signature, expiry, status, item↔tag, depot, unused | A `test_forged_or_tampered_qr`, A `test_token_expiry_boundary_and_release`, A `test_token_for_a_different_item_or_tag`, A `test_staff_scanning_at_wrong_depot`, A `test_reused_token` |
| R31 | Staff confirm ID check before handover | A `test_pickup_requires_id_check` |
| R32 | Handover in ONE transaction: custody log, item handed_over, claim completed, token invalidated; nothing partial on failure | A `test_db_failure_mid_handover_rolls_back_everything` (×3 failure points), U `test_unit_of_work_rolls_back_every_write`, U `test_unit_of_work_cas_miss_aborts` |
| R33 | Passenger notified after approval and handover | A `test_open_answer_approve_token_pickup`, A `test_staff_approve_borderline_issues_token`, E |
| R34 | Reissue requires the ID-verified flag and revokes the old token | A `test_lost_phone_reissue_revokes_old_token` |
| R35 | Staff routes depot-scoped, staff-only, CSRF-protected | A `test_staff_are_scoped_to_their_depot`, A `test_staff_endpoints_need_staff`, A `test_staff_actions_require_csrf`, A `test_staff_filter_and_body_validation` |
| R36 | Seed: items with attributes + tags, multi-claim conflict, borderline case, expired token | A `test_seed_claims_scenarios_and_idempotency` |
| R37 | FULL end-to-end across all three features | E `test_full_journey_all_three_features` |
| R38 | Legacy depot "resolve" cannot bypass a claim lock | E `test_legacy_depot_resolve_cannot_bypass_a_claim_lock` |

## 2. Edge cases from the brief

| Edge case | Expected behaviour | Test(s) |
|---|---|---|
| Claim on someone else's match (IDOR) | 404 on every claim endpoint; nothing changes | A `test_idor_on_someone_elses_match_and_claim` |
| Unauthenticated access | 401 (passenger API) / 401 or 403 (staff) | A `test_unauthenticated_passenger_endpoints`, A `test_staff_endpoints_need_staff` |
| Two passengers claim the same item simultaneously | exactly one `token_issued`, the other `cancelled`; one lock | A `test_two_passengers_claiming_same_item_simultaneously` |
| Same passenger opens duplicate claims | 409 `claim_exists` with the existing id; parallel → one | A `test_duplicate_claim_by_same_passenger`, A `test_parallel_duplicate_claims_create_one` |
| Claim after the match hold expired | 409 `match_not_claimable` | A `test_claim_after_hold_expired` |
| Item already claimed or handed over | 409 `item_unavailable` | A `test_item_already_claimed_or_handed_over` |
| Wrong answers then retry | new round, progress only | A `test_wrong_answers_then_retry_reveals_only_progress` |
| Lockout after max rounds | `locked`; staff approval needs the ID flag | A `test_lockout_after_max_rounds_then_staff_id_check` |
| Borderline score adds a question then routes to staff | follow-up question → `staff_review` | A `test_borderline_adds_question_then_routes_to_staff`, A `test_borderline_follow_up_is_strict` |
| Tamil / Tanglish / English variants, typos within tolerance | verified | A `test_answers_in_tamil_tanglish_english_and_typos`, U synonym/typo tests |
| Empty, very long, emoji and injection answers | empty/long/non-string → 400; emoji/injection → simply wrong, never stored | A `test_invalid_answers_rejected`, A `test_emoji_and_injection_answers_are_just_wrong`, U `test_text_wrong_empty_emoji_injection_and_shotgun_fail` |
| Answering after approval or after lock | 409 `no_questions` | A `test_answering_after_approval`, A `test_lockout_after_max_rounds_then_staff_id_check` |
| Skipping straight to /photo or /token | 403 (even with a correctly signed photo link) | A `test_skipping_to_photo_or_token_without_passing` |
| Expired signed photo URL | 403 `photo_link_expired` at exactly TTL | A `test_signed_photo_url_expiry_and_binding` |
| Expired token | `expired`; claim expired; item unlocked | A `test_token_expiry_boundary_and_release` |
| Reused token | `already_used` (code and QR) | A `test_reused_token` |
| Forged or tampered QR payload | `bad_signature` / `malformed` | A `test_forged_or_tampered_qr`, U `test_signed_payload_roundtrip_and_tamper_detection` |
| Token valid but for a different item or QR tag | `tag_mismatch` / `item_mismatch` / `tag_required` | A `test_token_for_a_different_item_or_tag` |
| Staff scanning at the wrong depot | `wrong_depot` (verify and complete) | A `test_staff_scanning_at_wrong_depot` |
| Lost phone → staff reissue revokes old token | old code `not_found`, old QR `revoked`; new code works; ID flag required | A `test_lost_phone_reissue_revokes_old_token` |
| Passenger cancels → item unlocked | item `open`, match back to `notified`, another passenger can claim | A `test_passenger_cancel_unlocks_item_for_others` |
| Illegal state transitions | 409 / `IllegalTransition` | U `test_illegal_transitions_rejected`, A `test_staff_reject_and_illegal_transitions` |
| Replay attacks | stale answer submission 409; used token rejected; expired photo link rejected | A `test_replayed_answer_submission_is_rejected`, A `test_reused_token`, A `test_signed_photo_url_expiry_and_binding` |
| Clock skew at expiry | token valid at TTL−1 s, expired at TTL; photo link valid at TTL−1 s, expired at TTL | A `test_token_expiry_boundary_and_release`, A `test_signed_photo_url_expiry_and_binding` |
| DB failure mid-handover | every document identical to before; no custody entry; retry succeeds | A `test_db_failure_mid_handover_rolls_back_everything` |
| Concurrent pickup scans | exactly one 200, four 409, one custody entry | A `test_concurrent_pickup_scans_only_one_succeeds` |
| Token for a claim whose item was meanwhile released | `item_released` | A `test_token_for_claim_whose_item_was_released` |

## 3. Mutation checks

Each guard was removed in turn, the targeted tests were run, and the code was restored:

| Guard removed | Result |
|---|---|
| Owner filter on claim lookup | caught (IDOR test) |
| Answer replay guard (`answerSeq` CAS) | caught |
| "Photo only after verification" in the file route | caught |
| Photo-link expiry | caught |
| Depot check at pickup | caught |
| Item-tag check at pickup | caught |
| Compensating rollback | caught (5 tests) |
| Strict follow-up question | caught (after the test was rebuilt so the follow-up actually decides the outcome) |
| Max-rounds lockout | caught |
| Token revocation (hash compare) | caught |
| Expiry boundary in `_expire_if_due` | caught |
| **All** handover compare-and-sets together | caught (5 scans succeed instead of 1) |

**Layered guards (by design).** Removing only the expiry check inside `_check_pickup` is not
caught, because `_expire_if_due` enforces the same boundary first. Removing only the claim-status
CAS at handover is not caught either, because the item CAS (`claim_locked` by this claim) still
allows exactly one handover. Each layer is verified by removing it together with its backup.

The race tests force the worst-case interleaving with barriers. Without them, the first versions
of the concurrent-pickup and strict-follow-up tests passed against broken code. Both were
rebuilt, which is why this table exists.

## 4. Manual check in the running app

See `docs/VERIFICATION.md` §4. The real `app.py` was run on an in-memory database with all three
seed scripts and driven in the browser.
