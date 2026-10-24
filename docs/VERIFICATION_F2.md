# Feature 2 Verification — Automatic Match Notification

Each rule and edge case from the Feature 2 brief, mapped to the test(s) that prove it.

```bash
pip install -r requirements-dev.txt
python -m pytest tests -q
```

Result at time of writing: **315 passed**. That is 134 Feature 2 tests, all 165 Feature 1 tests
(regression) and the 16 existing scorer tests.
`U` = `tests/test_match_unit.py`, `A` = `tests/test_match_api.py`, `E` = `tests/test_match_e2e.py`.
The `E` tests drive the **real `app.py`** routes and matcher (`POST /lost`, `POST /depot/found`,
`compute_and_save_matches`, `/api/match/resolve`). Only the ML models are replaced by
deterministic stubs, and MongoDB by mongomock.

## 1. Rules

| # | Rule | Test(s) |
|---|---|---|
| R1 | `matches` has reportId, itemId, confidence, status, createdAt, notifiedAt, holdExpiresAt; unique (reportId, itemId) | A `test_auto_match_notifies_on_whatsapp_and_starts_hold`, A `test_duplicate_triggers_are_deduplicated`, A `test_parallel_duplicate_triggers_create_one_match` |
| R2 | `notifications` has matchId, reportId, itemId, channel, status, attempts, lastError, sentAt, unique dedupeKey = reportId+itemId+channel | U `test_dedupe_key_is_report_item_channel`, A `test_duplicate_triggers_are_deduplicated`, A `test_provider_outage_retries_with_exponential_backoff` |
| R3 | Trigger (a): new lost report matches existing items | E `test_found_first_then_late_lost_report_still_matches` |
| R4 | Trigger (a'): updated lost report re-runs the matcher | A `test_update_report_reruns_matcher` |
| R5 | Trigger (b): new found item matches older open reports | E `test_end_to_end_lost_then_found_then_notified_then_viewed` |
| R6 | Safe manual re-run for staff (endpoint + CLI) | A `test_matcher_rerun_on_same_pair_is_idempotent`, A `test_reject_then_rerun_does_not_resurrect`, A `test_staff_cannot_act_on_other_depots_matches` |
| R7 | ≥ MATCH_AUTO_NOTIFY_MIN → auto-notify | A `test_confidence_exactly_at_each_threshold`, U `test_route_confidence_default_thresholds` |
| R8 | Review band → staff_review; notify only after approval | A `test_review_band_never_notifies_or_shows_before_approval`, A `test_staff_approve_then_notifies`, E `test_review_band_match_waits_for_staff_approval` |
| R9 | Below review min → rejected, no notification | A `test_confidence_exactly_at_each_threshold` (0.5999), A `test_staff_reject_sends_nothing` |
| R10 | Thresholds are env-configurable | U `test_route_confidence_uses_configured_thresholds`, A `test_thresholds_are_configurable`, U `test_config_rejects_bad_values` |
| R11 | Channel interface; mock default; real adapters chosen by env | U `test_real_providers_need_credentials`, U `test_mock_channel_logs_masked_and_can_simulate_outages`, U `test_twilio_whatsapp_adapter`, U `test_meta_whatsapp_adapter_success`, U `test_smtp_adapter_sends_utf8_text_and_escaped_html` |
| R12 | Template wording (subject, body, 7-day hold, regards) | U `test_template_matches_spec_wording` |
| R13 | No photos, contents, serials or private attributes in messages | A `test_message_is_privacy_safe_and_links_to_login_protected_report`, U `test_template_redacts_identifiers_from_description`, E `test_end_to_end_…` (private notes absent) |
| R14 | Deep link to the report page that still requires Feature 1 login | A `test_deep_link_requires_feature1_login`, E `test_end_to_end_…` (step 4) |
| R15 | Descriptions sanitized/escaped (HTML/template injection) | U `test_template_is_injection_safe`, U `test_html_part_escapes_markup_characters`, A `test_html_injection_in_description_is_escaped_on_pages` |
| R16 | Tamil / UTF-8 handled correctly | U `test_tamil_text_round_trips_nfc`, U `test_tamil_truncation_keeps_valid_text`, U `test_smtp_adapter_sends_utf8_text_and_escaped_html`, A `test_very_long_and_tamil_descriptions` |
| R17 | WhatsApp first, email fallback | A `test_whatsapp_failure_falls_back_to_email`, A `test_email_only_passenger_gets_email` |
| R18 | Retries with exponential backoff, max attempts configurable (1–5) | U `test_backoff_is_exponential_and_capped`, A `test_provider_outage_retries_with_exponential_backoff`, A `test_both_channels_fail_marks_failed_and_surfaces_to_staff`, U `test_config_rejects_bad_values` |
| R19 | Final failure → failed, surfaced to staff | A `test_both_channels_fail_marks_failed_and_surfaces_to_staff`, A `test_passenger_with_no_whatsapp_or_email_gets_in_app_only_and_staff_sees_it` |
| R20 | Sending runs in a background job; the matcher is never blocked | A `test_auto_match_notifies_on_whatsapp_and_starts_hold` (nothing sent until the worker runs), E `test_end_to_end_…` (`message_log` empty after the depot request) |
| R21 | Jobs are idempotent | A `test_duplicate_triggers_are_deduplicated`, A `test_job_running_twice_concurrently_sends_once`, A `test_expired_lease_is_reclaimed_but_live_lease_is_not` |
| R22 | MATCH_HOLD_DAYS from notifiedAt; scheduled release → released; passenger sees expired | A `test_hold_expiry_boundary_and_release`, A `test_release_job_running_twice_concurrently_is_idempotent` |
| R23 | Passenger routes use requirePassenger + object-level authorization | A `test_idor_passenger_cannot_see_another_passengers_data`, A `test_idor_same_pnr_other_passenger_phone`, A `test_unauthenticated_passenger_api_is_401` |
| R24 | Privacy-safe match view (depot, short description, date found, confidence, hold countdown; no photo or private attributes) | A `test_match_view_is_privacy_safe_with_ist_and_countdown` (exact key set asserted) |
| R25 | Endpoints: GET /reports/:id/matches, GET /matches/:id, GET /notifications | A `test_report_matches_and_notifications_endpoints`, A `test_match_view_is_privacy_safe_with_ist_and_countdown` |
| R26 | Staff endpoints: list/approve/reject/notifications/retry | A `test_staff_approve_then_notifies`, A `test_staff_reject_sends_nothing`, A `test_both_channels_fail_marks_failed_and_surfaces_to_staff`, A `test_staff_retry_after_total_failure`, A `test_staff_filters_validated` |
| R27 | Staff auth (existing depot login, depot-scoped, CSRF) | A `test_staff_routes_unauthenticated_401`, A `test_staff_cannot_act_on_other_depots_matches`, A `test_retry_is_scoped_to_staff_depot`, A `test_staff_actions_require_csrf`, A `test_staff_page_redirects_non_staff` |
| R28 | One item → several reports: notify each, leak nothing | A `test_same_item_matched_to_multiple_reports_notifies_each_without_leaking` |
| R29 | UI: notifications list, match detail + countdown + "Start claim" entry point, expired state, staff queue + status table | A `test_report_matches_and_notifications_endpoints`, A `test_match_view_is_privacy_safe_with_ist_and_countdown`, A `test_hold_expiry_boundary_and_release`, A `test_staff_page_redirects_non_staff` |
| R30 | Seed: high/medium/low, expired, multi-report conflict, email-only passenger | A `test_seed_matches_covers_scenarios_and_is_idempotent` |
| R31 | Legacy match documents migrate without notifying | A `test_legacy_matches_are_backfilled_without_notifying` |
| R32 | Lost reports are owned (filing requires Feature 1 login) | E `test_lost_report_requires_passenger_login`, E `test_end_to_end_…` (step 1) |
| R33 | Found items need a separate public description | E `test_found_item_requires_public_description` |

## 2. Edge cases from the brief

| Edge case | Expected behaviour | Test(s) |
|---|---|---|
| Duplicate triggers / dedupe | one match and one notification per channel; sent once | A `test_duplicate_triggers_are_deduplicated`, A `test_parallel_duplicate_triggers_create_one_match` |
| Matcher re-run on same pair | no new match, no new send | A `test_matcher_rerun_on_same_pair_is_idempotent` |
| Same item matched to multiple reports | each notified; no "also matched" or other-report data | A `test_same_item_matched_to_multiple_reports_notifies_each_without_leaking` |
| Item already claimed or handed over | no match created; queued send cancelled if it happens later | A `test_item_already_claimed_or_handed_over_is_not_notified`, A `test_item_handed_over_after_queueing_cancels_pending_send`, E `test_depot_resolve_reserves_item_and_rejects_inactive` |
| Report cancelled or already resolved | no match; cancel ends matches and queued sends | A `test_report_cancelled_or_resolved_is_not_matched`, A `test_passenger_cancels_report_ends_matches_and_pending_sends`, A `test_depot_resolve_ends_competing_matches` |
| Confidence exactly at each threshold | 0.85 auto · 0.8499 review · 0.60 review · 0.5999 rejected | A `test_confidence_exactly_at_each_threshold`, U `test_route_confidence_default_thresholds` |
| Review band must NOT notify before approval | no notification docs, report not "matched", match 404 to the passenger | A `test_review_band_never_notifies_or_shows_before_approval`, E `test_review_band_match_waits_for_staff_approval` |
| Reject then re-run must not resurrect | stays rejected even at 0.99 | A `test_reject_then_rerun_does_not_resurrect`, A `test_released_match_is_not_resurrected_by_rerun` |
| Passenger with no WhatsApp/email | in-app only; both channels failed and visible to staff; not retryable | A `test_passenger_with_no_whatsapp_or_email_gets_in_app_only_and_staff_sees_it` |
| WhatsApp fails → email fallback | email sent, `deliveredVia: email` | A `test_whatsapp_failure_falls_back_to_email`, A `test_email_only_passenger_gets_email` |
| Both fail → failed + staff visibility | both failed after 4 attempts each; staff table lists them | A `test_both_channels_fail_marks_failed_and_surfaces_to_staff` |
| Provider outage and retry/backoff | retries at +30 s, +60 s; not before due; recovers | A `test_provider_outage_retries_with_exponential_backoff` |
| Retry after partial success must not double-send | 409 on both channels once email was sent | A `test_retry_after_partial_success_does_not_double_send`, A `test_staff_retry_after_total_failure` |
| Hold expiry boundary | active at 7d−1s, expired at exactly 7d, then released | A `test_hold_expiry_boundary_and_release` |
| Job running twice concurrently | each job sent exactly once; release counted once | A `test_job_running_twice_concurrently_sends_once`, A `test_release_job_running_twice_concurrently_is_idempotent`, A `test_expired_lease_is_reclaimed_but_live_lease_is_not` |
| HTML/template injection or very long text | escaped/stripped, `{{7*7}}` literal, truncated | U `test_template_is_injection_safe`, U `test_very_long_description_is_truncated`, A `test_html_injection_in_description_is_escaped_on_pages`, A `test_very_long_and_tamil_descriptions` |
| Tamil text | preserved (NFC) in WhatsApp, email (UTF-8 MIME) and pages | U `test_tamil_text_round_trips_nfc`, U `test_smtp_adapter_sends_utf8_text_and_escaped_html`, A `test_very_long_and_tamil_descriptions` |
| IST date display | `21-Oct-2026 IST`; hold `… IST`; API in UTC `Z` | U `test_found_date_format`, A `test_match_view_is_privacy_safe_with_ist_and_countdown`, A `test_report_matches_and_notifications_endpoints` |
| IDOR (A requests B's match/notification) | 404 everywhere; lists show only own data | A `test_idor_passenger_cannot_see_another_passengers_data`, A `test_idor_same_pnr_other_passenger_phone`, A `test_unknown_or_malformed_ids_are_404` |
| Unauthenticated access | API 401 `session_missing`; pages → login | A `test_unauthenticated_passenger_api_is_401`, A `test_unauthenticated_passenger_pages_redirect`, A `test_staff_routes_unauthenticated_401` |
| Staff-only routes hit by a passenger | 403 `staff_only` on all six staff routes | A `test_staff_routes_hit_by_passenger_403` |

## 3. Mutation checks (do the tests catch real regressions?)

Each guard below was removed from `match_notify/service.py`, the targeted tests were run, and
the code was restored:

| Guard removed | Caught by |
|---|---|
| Lease condition on job claim | `test_job_running_twice_concurrently_sends_once`, `test_expired_lease_is_reclaimed_but_live_lease_is_not` |
| `rejectedBy: system` in both the re-route `if` and its conditional update | `test_reject_then_rerun_does_not_resurrect` |
| "no other channel pending/sent" retry check | `test_retry_after_partial_success_does_not_double_send` |
| Owner filter in `match_for_passenger` | `test_idor_passenger_cannot_see_another_passengers_data`, `test_idor_same_pnr_other_passenger_phone` |
| Approving review-band matches automatically | 5 tests (review-band, thresholds) |
| Hold release `$lte` → `$lt` (off-by-one) | `test_hold_expiry_boundary_and_release` |

Removing only the `if` of the re-route guard is **not** caught. That is expected: the conditional
update's filter still requires `rejectedBy: system`, a deliberate second guard.

## 4. Manual check in the running app

Atlas credentials were still rejected (`bad auth`). The real `app.py` was therefore run against
in-memory mongomock with `seed_bookings.py` + `seed_matches.py` loaded and the background worker
on. It was driven in a browser:

- **Seed log:** high → mock WhatsApp; email-only Priya → mock email; 0.71 → review queue;
  0.45 → rejected; the 8-day-old match → released; the shared red bag notified both reports;
  Ravi (no channels) → "every notification channel failed; surfaced to staff".
- **Passenger (Arun):** OTP login → "My reports" lists TRK-DEMO0001/0005 → the report page (deep
  link) shows the match. The match page shows 92 %, the depot, "Black backpack" (the private note
  with the serial is absent), "24-Oct-2026 IST", "Until 31 Oct 2026, 03:24 PM IST", a live
  countdown and the disabled "Start claim" button. TRK-DEMO0005 shows **expired**. Notifications
  list his two only. Another passenger's report → 404. `/staff/matches` → 403.
- **Staff (demo depot):** `/staff/review` lists the 0.71 match with approve/reject. The delivery
  table lists Ravi's failed WhatsApp/email and Priya's failed WhatsApp. Retry is offered only
  where it could succeed.
