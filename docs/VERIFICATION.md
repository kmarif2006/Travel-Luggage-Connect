# Verification Summary — All Three Features

```bash
pip install -r requirements-dev.txt
python -m pytest tests -q
```

**465 tests, all passing.** The suite needs no database or ML models. It runs on mongomock with
a controllable clock and in-memory SMS/WhatsApp/email providers. The end-to-end tests drive the
real `app.py` with only the ML models stubbed.

| Area | Tests | Detail |
|---|---|---|
| Feature 1 — OTP passenger authentication | 165 | [VERIFICATION_F1.md](VERIFICATION_F1.md) |
| Feature 2 — Automatic match notification | 134 | [VERIFICATION_F2.md](VERIFICATION_F2.md) |
| Feature 3 — Secure claims & QR pickup | 150 | [VERIFICATION_F3.md](VERIFICATION_F3.md) |
| Pre-existing matcher scoring | 16 | `tests/test_scoring.py` (also runs standalone) |

## 1. The whole journey, end to end

`tests/test_claims_e2e.py::test_full_journey_all_three_features` runs the complete story through
the real Flask routes:

| Step | Feature | What is asserted |
|---|---|---|
| PNR + phone → OTP → session | 1 | OTP verified, HttpOnly session cookie |
| File a lost report | 1 → 2 | report owned by the verified PNR + phone |
| Depot logs the item with category, secret attributes, photo | 3 | attributes stored as hashes only, QR tag assigned, custody "logged" entry |
| Existing matcher pairs them | 2 | match auto-approved (same trip) |
| Worker sends the WhatsApp notification | 2 | privacy-safe message in the mock log |
| Passenger opens the match → "This is my item" | 2 → 3 | claim opened, photo still 403, uploads 404 for the passenger |
| Answers in Tamil / Tanglish / English with a typo | 3 | trust ≥ threshold → item `claim_locked` |
| Photo via a signed URL | 3 | image served only through the 120 s link |
| Pickup code + QR | 3 | code, SVG QR, depot shown |
| Staff verify QR + item tag, confirm ID, hand over | 3 | custody "handover" entry, item `handed_over`, claim `completed` |
| Afterwards | 1–3 | report `resolved` on the public status page, passenger notified (in-app + SMS), reused code refused |

## 2. Security properties and where they are proven

| Property | Feature 1 | Feature 2 | Feature 3 |
|---|---|---|---|
| Object-level authorization (IDOR → 404) | session bound to (pnr, phone) | `test_idor_*` | `test_idor_on_someone_elses_match_and_claim` |
| Unauthenticated / wrong role | `test_missing_session` | `test_staff_routes_*` | `test_unauthenticated_passenger_endpoints`, `test_staff_endpoints_need_staff` |
| Input validation & injection | `test_non_string_injection_is_rejected` | `test_template_is_injection_safe` | `test_invalid_answers_rejected`, `test_emoji_and_injection_answers_are_just_wrong` |
| Rate limits | `test_per_ip_rate_limit`, resend/PNR caps | per-IP OTP limits reused | `test_ip_rate_limit_on_claim_endpoints`, daily caps |
| Replay | `test_otp_reuse_after_success_fails` | dedupe / single send | `test_replayed_answer_submission_is_rejected`, `test_reused_token` |
| Tampering | `test_tampered_session_rejected` | — | `test_forged_or_tampered_qr` |
| Races (forced worst-case interleaving) | `test_parallel_verify_only_one_succeeds` | `test_job_running_twice_concurrently_sends_once` | `test_two_passengers_claiming_same_item_simultaneously`, `test_concurrent_pickup_scans_only_one_succeeds` |
| Secrets never stored raw | OTP only as salted HMAC | no contacts in jobs | answers only as keyed hashes, outcomes pass/fail |
| CSRF on state-changing routes | `test_csrf_enforced_on_auth_posts` | `test_staff_actions_require_csrf` | `test_staff_actions_require_csrf` |
| Atomicity under failure | — | — | `test_db_failure_mid_handover_rolls_back_everything` |

## 3. Do the tests catch real bugs?

Each feature's critical guards were deliberately removed one at a time, and the targeted tests had
to fail. About two dozen guard mutations were run across F1, F2 and F3; see the "Mutation checks"
section of each verification file.

Three race tests initially passed against broken code because their threads never actually
overlapped:
- Feature 1: parallel OTP verification;
- Feature 3: concurrent pickup scans and the strict follow-up question.

They were rebuilt with barriers that force the worst-case interleaving deterministically. Guards
that are deliberately layered (a second check behind the first) are documented as such.

## 4. Running-app checks

The configured MongoDB Atlas credentials were rejected (`bad auth`) throughout. The real `app.py`
was therefore run with `pymongo.MongoClient` swapped for in-memory mongomock, all seed scripts
loaded and mock providers on, and driven in a browser:
- **Feature 1:** OTP login, wrong OTP / lockout, logout.
- **Feature 2:** auto-notification, email fallback, the review queue → approval → worker send, and
  the failed-delivery table.
- **Feature 3:**
  - Priya claimed her bag from scratch with Tamil/Tanglish answers and got the token and QR;
  - the photo opened through the signed link, while `/static/uploads` returned 404 for the
    passenger;
  - at the staff desk: valid verify, wrong tag rejected with its reason, handover completed, and
    the reused code refused;
  - the tag page shows "logged" then "handover → Priya Raman · by Counter 2";
  - Karthik's borderline claim waits in the review queue.

The step-by-step script for a live demo is [DEMO.md](DEMO.md).

## 5. Not verified, and why

| Item | Why |
|---|---|
| Running against the real Atlas database | credentials in `.env` are rejected (`bad auth`); needs a new `MONGO_URI` |
| MongoDB multi-document transaction path (`with_transaction`) | needs a replica set. Atlas has one, but this machine has none (no local mongod; Docker daemon not running). The compensating-rollback path used on standalone servers is fully tested |
| Live Twilio SMS/WhatsApp, Meta WhatsApp Cloud API, SMTP | adapters tested with fake transports only; Meta also needs an approved template for business-initiated messages |
| Camera QR scanning in the browser | uses the browser's `BarcodeDetector`, which desktop Chrome on Windows lacks. USB/keyboard scanners and typed codes are the tested paths |
