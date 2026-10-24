# Feature 1 Verification — OTP-Based Passenger Authentication

Each rule and edge case from the Feature 1 brief, mapped to the test(s) that prove it.
Run everything with:

```bash
pip install -r requirements-dev.txt
python -m pytest tests -q
```

Result at time of writing: **181 passed** (165 new auth tests + 16 existing scorer tests).
`U` = `tests/test_auth_unit.py`, `A` = `tests/test_auth_api.py`.

## 1. Rules

| # | Rule | Test(s) |
|---|---|---|
| R1 | Phone normalized (+91, spaces, dashes, leading 0) before comparing with the registered phone | U `test_phone_variants_normalize_to_e164`, U `test_phone_rejects_invalid`, A `test_phone_format_variants`, A `test_registered_phone_stored_in_other_format_still_matches` |
| R2 | OTP sent only to the registered number, never to a differing typed number | A `test_otp_only_goes_to_registered_number`, A `test_phone_mismatch_gets_generic_response_and_no_sms` |
| R3 | Unknown PNR and phone mismatch get the same generic response | A `test_unknown_pnr_gets_generic_response_and_no_sms` (bodies compared for equality), A `test_phone_mismatch_gets_generic_response_and_no_sms`, A `test_per_pnr_cap_applies_to_unknown_pnr_too` |
| R4 | 6 digits from a CSPRNG | U `test_generate_otp_is_six_ascii_digits`, U `test_generate_otp_uses_full_range_including_leading_zeros` |
| R5 | Valid 5 minutes | U `test_otp_expiry_boundary`, U `test_otp_record_never_stores_raw_code` (expiresAt − createdAt = 300 s), A `test_expired_otp`, A `test_end_to_end_happy_path` (`expiresInSeconds: 300`) |
| R6 | Stored only as a salted HMAC hash | U `test_hash_is_salted_and_keyed`, U `test_hash_binds_pnr_and_phone`, U `test_otp_record_never_stores_raw_code` |
| R7 | Constant-time comparison | U `test_compare_uses_constant_time_primitive`, U `test_compare_accepts_only_exact_code` |
| R8 | Raw OTP never logged or returned | U `test_raw_otp_never_logged`, U `test_mock_provider_redacts_otp_by_default`, U `test_twilio_adapter_sends_sms_without_logging_body`, A `test_end_to_end_happy_path` (OTP absent from response) |
| R9 | Dev-only flag (off by default) exposes the OTP in the mock SMS log | U `test_mock_provider_exposes_otp_only_with_dev_flag`, U `test_config_defaults_match_spec` |
| R10 | Max 3 wrong attempts, then lock | U `test_attempts_are_counted_and_lock_at_max`, A `test_wrong_otp_three_times_then_lock` |
| R11 | Lock cool-down 15 min by default, configurable | A `test_wrong_otp_three_times_then_lock` (900 s), A `test_lock_cooldown_is_configurable` (120 s), A `test_lock_expires_after_cooldown`, U `test_config_reads_env_overrides` |
| R12 | Correct OTP during a lock is rejected | A `test_correct_otp_while_locked_is_rejected`, A `test_new_otp_request_during_lock_is_refused` |
| R13 | A new OTP request supersedes the previous active one | A `test_new_otp_invalidates_old_one` |
| R14 | Resend cooldown 30 s | A `test_resend_cooldown`, U `test_cooldown_gate` |
| R15 | Max 5 requests per PNR per hour | A `test_resend_spam_capped_at_five_per_pnr_per_hour`, A `test_per_pnr_cap_applies_to_unknown_pnr_too` |
| R16 | Per-IP rate limits | A `test_per_ip_rate_limit`, A `test_per_ip_verify_rate_limit`, U `test_fixed_window_counter` |
| R17 | OTP is single use; replay after success fails | A `test_otp_reuse_after_success_fails`, A `test_parallel_verify_only_one_succeeds` |
| R18 | Signed, short-lived session bound to (pnr, phone) | A `test_session_cookie_flags`, A `test_session_ttl_configurable_and_server_side`, A `test_session_record_bound_to_pnr_and_phone`, A `test_forged_session_with_other_secret_rejected` |
| R19 | Cookie is httpOnly, Secure, SameSite | A `test_session_cookie_flags` |
| R20 | Session expiry and logout invalidation | A `test_expired_session`, A `test_logout_then_reuse_old_session`, A `test_logout_without_session_is_idempotent` |
| R21 | SMS through a provider interface; mock by default; real adapter selected by env | U `test_provider_selected_by_env`, U `test_mock_provider_redacts_otp_by_default`, U `test_twilio_adapter_sends_sms_without_logging_body` |
| R22 | All config in env vars, spec defaults, invalid values refused | U `test_config_defaults_match_spec`, U `test_config_reads_env_overrides`, U `test_config_rejects_bad_values`, U `test_missing_secret_falls_back_to_random_not_constant` |
| R23 | Every input schema-validated; consistent error shape | A `test_malformed_missing_oversized_payloads`, A `test_non_string_injection_is_rejected`, A `test_invalid_pnr_format_is_400`, A `test_invalid_phone_format_is_400` |
| R24 | Correct HTTP codes 400/401/403/423/429 (+413) | 400: A `test_malformed_missing_oversized_payloads` · 401: A `test_expired_otp`, A `test_missing_session` · 403: A `test_booking_cancelled_after_login_is_403`, A `test_csrf_enforced_on_auth_posts` · 413: A `test_malformed_missing_oversized_payloads` · 423: A `test_wrong_otp_three_times_then_lock` · 429: A `test_resend_cooldown`, A `test_per_ip_rate_limit` |
| R25 | Timestamps in UTC, displayed in IST | U `test_db_roundtrip_is_utc_millisecond_precision`, U `test_iso_and_ist_formatting`, A `test_end_to_end_happy_path` (`expiresAt` ends in `Z`, page shows `IST`) |
| R26 | TTL / cleanup of old records | U `test_ttl_cleanup_indexes_exist` |
| R27 | Reusable `requirePassenger` exposing (pnr, phone, passengerName) | A `test_end_to_end_happy_path` (`/auth/me`, `/passenger`), A `test_missing_session`, A `test_expired_session_redirects_page_to_login` |
| R28 | CSRF protection kept on the auth POSTs | A `test_csrf_enforced_on_auth_posts`, A `test_csrf_token_flow_works` |
| R29 | Seed: several PNRs/phones, one cancelled, one multi-passenger; idempotent | U `test_seed_is_idempotent_and_covers_policy_cases` |

## 2. Edge cases from the brief

| Edge case | Expected behaviour | Test(s) |
|---|---|---|
| Invalid PNR format | 400 `invalid_request`, `fields.pnr = invalid_format`, no SMS | A `test_invalid_pnr_format_is_400`, U `test_pnr_rejects_invalid` |
| PNR not found | generic 202, no SMS; verify → 401 | A `test_unknown_pnr_gets_generic_response_and_no_sms` |
| Phone mismatch | generic 202, nothing sent to either number; verify → 401 | A `test_phone_mismatch_gets_generic_response_and_no_sms` |
| Cancelled booking | generic 202, no OTP (policy F1-07) | A `test_cancelled_booking_gets_no_otp`, U `test_booking_policy` |
| Past-journey booking | allowed within 90 days, refused beyond (policy F1-07) | A `test_past_journey_inside_window_is_allowed`, A `test_past_journey_outside_window_gets_no_otp`, U `test_booking_policy` |
| Booking cancelled after login | 403 `booking_not_eligible`, session revoked | A `test_booking_cancelled_after_login_is_403` |
| Expired OTP | 401 `otp_expired` | A `test_expired_otp`, A `test_otp_exactly_at_expiry_is_rejected` |
| Wrong OTP ×3 then lock | 401 (2 left), 401 (1 left), 423 + Retry-After 900 | A `test_wrong_otp_three_times_then_lock` |
| Correct OTP while locked | 423 | A `test_correct_otp_while_locked_is_rejected` |
| OTP reuse after success | 401 `otp_invalid` | A `test_otp_reuse_after_success_fails` |
| Parallel verify requests (race) | exactly one 200 and one session; parallel wrong guesses cannot exceed 3 attempts | A `test_parallel_verify_only_one_succeeds`, A `test_parallel_wrong_guesses_never_exceed_max_attempts` |
| Resend spam | 429 `resend_cooldown` inside 30 s; 429 `pnr_request_limit` after 5/hour | A `test_resend_cooldown`, A `test_resend_spam_capped_at_five_per_pnr_per_hour` |
| New OTP invalidates old one | old code 401, old record `superseded`, new code 200 | A `test_new_otp_invalidates_old_one` |
| One PNR, multiple passengers/phones | shared phone → joint session; own phone → own session; codes and locks independent | A `test_multi_passenger_shared_phone`, A `test_multi_passenger_own_phone`, A `test_multi_passenger_otps_are_independent`, A `test_lock_is_per_booking_phone` |
| Phone format variants | all normalize to the same E.164 | A `test_phone_format_variants`, U `test_phone_variants_normalize_to_e164` |
| Malformed / missing / oversized payloads | 400 (non-JSON, bad JSON, array, empty, missing, unknown, too long), 413 (> 2 KB) | A `test_malformed_missing_oversized_payloads` |
| Non-string / array / object injection | 400 `must_be_string` | A `test_non_string_injection_is_rejected`, A `test_injection_in_verify_otp_field` |
| NoSQL / SQL injection strings | 400 format error | A `test_sql_and_operator_strings_are_rejected` |
| Missing session on protected route | API 401 `session_missing`; page → login redirect | A `test_missing_session` |
| Expired session | API 401 `session_expired`; page → login with "session expired" banner | A `test_expired_session`, A `test_expired_session_redirects_page_to_login` |
| Tampered session | 401 `session_invalid` (bad signature, garbage, oversize, foreign secret, rebound record) | A `test_tampered_session_rejected`, A `test_forged_session_with_other_secret_rejected`, A `test_session_record_bound_to_pnr_and_phone` |
| Logout then reuse of old session | 401 (server record revoked) | A `test_logout_then_reuse_old_session` |
| Clock skew near expiry | server clock authoritative: OTP valid at +299 s, expired at +300 s; session valid at TTL−1 s, expired at TTL; UI counts down from relative seconds | A `test_clock_skew_near_otp_expiry`, A `test_otp_exactly_at_expiry_is_rejected`, A `test_expired_session`, U `test_otp_expiry_boundary`, U `test_seconds_until_rounds_up_and_floors_at_zero` |
| Unicode / emoji in fields | 400 (emoji, full-width, Tamil/Arabic-Indic digits); Unicode passenger names round-trip intact | A `test_unicode_and_emoji_in_fields_are_rejected`, A `test_unicode_emoji_otp_rejected`, A `test_unicode_passenger_name_round_trips`, U `test_otp_format` |
| Open redirect via `?next=` | only same-site paths accepted | A `test_post_login_redirect_is_same_site_only` |

## 3. Concurrency test validity

mongomock is not thread-safe, so `tests/conftest.py` serializes each single collection call.
That reproduces MongoDB's per-operation atomicity and nothing stronger. The two race tests
hold every request at a barrier right before **and** right after the atomic attempt
reservation, so all reservations happen before any comparison or commit. This is the
worst-case interleaving, forced deterministically. Both were checked against deliberately
broken code:

| Mutation | Result |
|---|---|
| Remove `status: "active"` from the success transition (single-use guard) | `test_parallel_verify_only_one_succeeds` fails: 3 sessions instead of 1 |
| Remove `attempts < max` from the attempt reservation | `test_parallel_wrong_guesses_never_exceed_max_attempts` fails: 12 attempts instead of 3 |

Each race test also passed 15/15 repeated runs on the correct code.

## 4. Manual end-to-end check in the running app

The configured Atlas credentials were rejected (`bad auth`) at the time of verification. The
real `app.py` was therefore run with `pymongo.MongoClient` replaced by an in-memory mongomock,
the sample bookings seeded and `SMS_MOCK_EXPOSE_OTP=1`. It was driven in a browser:

- `/passenger` while logged out → redirected to `/passenger/login?next=/passenger`.
- PNR `t26100101` with phone `+91 98765-43210` → OTP screen with masked phone, 5:00 countdown and 30 s resend timer. The mock SMS log showed the code.
- Wrong code → "2 attempt(s) remaining". Correct code → redirected to `/passenger`, showing name, PNR, route, travel date and session expiry in IST. The nav shows the passenger name. `document.cookie` cannot see `tnstc_pax` (HttpOnly).
- Log out → `/passenger/login?reason=logged_out`, after which `/auth/me` returns 401.
- PNR `T26100102`: three wrong codes → lockout panel "Try again in 15:00" with verify and resend disabled. The correct code and a new OTP request then both returned 423 with `Retry-After`.
- POST without CSRF token → 403 `csrf_failed`. Unknown PNR → the same generic 202. `{"$ne": null}` → 400.
- Existing pages `/`, `/status`, `/depot-login`, `/api/routes` → 200. No JavaScript console errors; the only console entries were the deliberate 4xx responses.
