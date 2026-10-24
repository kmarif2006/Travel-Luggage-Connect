# Live Demo — TNSTC Lost & Found (Features 1, 2 and 3)

About 15 minutes, two browser windows: a normal window for the **passenger** and a private
window for **depot staff**. All messages go to the mock providers. OTPs, WhatsApp and email
messages appear in the app's console.

## 0. One-time setup

1. In `.env` set (see `.env.example`):
   ```
   SIGNING_SECRET=<python -c "import secrets; print(secrets.token_urlsafe(48))">
   SMS_MOCK_EXPOSE_OTP=1        # demo only: shows the OTP in the console
   SMS_PROVIDER=mock
   WHATSAPP_PROVIDER=mock
   EMAIL_PROVIDER=mock
   PASSENGER_COOKIE_SECURE=1    # fine on http://localhost / 127.0.0.1
   ```
2. Seed, in this order (each is idempotent):
   ```bash
   python seed_transport.py
   ```
   ```bash
   python seed_bookings.py
   ```
   ```bash
   python seed_matches.py
   ```
   ```bash
   python seed_claims.py
   ```
3. Start the app: `python app.py`, then open `http://127.0.0.1:5003`.

**Demo accounts**

| Who | Login | Notes |
|---|---|---|
| Priya (passenger) | PNR `T26100102`, mobile `94440 12345` | email-only passenger, has an unclaimed match |
| Arun (passenger) | PNR `T26100101`, mobile `98765 43210` | has a pickup code ready |
| Ravi (passenger) | PNR `T26100105`, mobile `80987 65432` | lost the multi-claim conflict |
| Depot staff | `/depot-login` → "Chennai CMBT Depot (demo)", password `demo123` | phone 9000000101 |

**Secret answers** (staff recorded these when logging the items; they are stored only as hashes):

| Item | Zipper/tag colour | One thing inside | Brand | Mark |
|---|---|---|---|---|
| Brown leather bag (Priya, TRK-DEMO0002) | red · *sivappu* · சிவப்பு | blue umbrella · *neela kudai* | Lavie | torn left strap |
| Black backpack (Arun, TRK-DEMO0001) | yellow · *manjal* · மஞ்சள் | steel tiffin box · *dabba* | Wildcraft | Chennai Super Kings sticker |

## 1. Feature 1 — OTP login (passenger window)

1. Click **Passenger Login**. Enter PNR `T26100102` and mobile `+91 94440-12345` (any format
   works). Click **Send OTP**.
   - Point out the 5:00 countdown, the 30 s resend timer, and that the message is the same
     whether or not the PNR exists.
2. Copy the 6-digit code from the console line `[MOCK SMS] … purpose=otp: 123456 …` and click
   **Verify & Continue**.
3. You land on **My Lost & Found**, which shows booking details, the session expiry in IST, and
   *My reports*.
4. *Optional:* log out, enter a wrong OTP three times, and show the 15-minute lockout.

## 2. Feature 2 — Match notification

1. In the console, show the mock email sent to Priya for TRK-DEMO0002 at seed time. She has no
   WhatsApp, so it fell back to email. The message contains only the depot, a short public
   description, the date and a login link. It has no photo, notes or serial numbers.
2. Passenger window: **Notifications**, then the notice, then the match page: 88 %, Chennai CMBT
   Depot (demo), "Brown leather bag", the date found and a 7-day hold countdown.
3. Staff window: log in at `/depot-login` (demo depot, `demo123`), then go to **Match Review**:
   - the review queue holds TRK-DEMO0003 at 71 %; click **Approve** and a WhatsApp is sent
     within seconds (console);
   - the delivery table lists failed channels for Ravi, who has no WhatsApp and no email.

## 3. Feature 3 — Secure claim and QR pickup

**Passenger window (Priya):**

1. On the TRK-DEMO0002 match page click **This is my item**.
2. Answer the questions in any mix of languages, e.g. `sivappu`, `neela kudai`, `lavie`,
   `torn strap`. Point out:
   - the progress bar;
   - that it never says which answer was wrong;
   - wrong answers start another round (max 3, then the claim is locked for an in-person ID check).
3. Status: **Approved – ready for pickup**. Click **Show photo**: the photo loads through a
   2-minute signed link. It was never shown before verification, and `/static/uploads/...`
   returns 404 for passengers.
4. The pickup code (e.g. `A7X2-K9P4`) and QR appear with a countdown, plus **Download QR** and
   **Print**.

**Staff window:**

5. **Dashboard**: in the *found items* list, click the **TAG-… · print tag** link for "Brown
   leather bag". This is the item's QR tag, with its custody log ("logged"). Copy the tag ID.
6. **Claims & Pickup**:
   1. Type Priya's code (or paste the QR text, or use a USB scanner) and click **Verify** →
      green **VALID** with her name, phone, PNR and report.
   2. Enter a wrong tag (e.g. the backpack's tag) → red **NOT VALID … tag does not match**.
   3. Enter the correct tag, tick **I checked the passenger's photo ID**, type a name, and click
      **Hand over item** → **HANDED OVER**.
   4. Click **Verify** again with the same code → **already been used**.
7. Reopen the tag page: the custody log now shows **handover → Priya Raman · by <name>**. Priya's
   notifications show "Item collected", and the SMS appears in the console.

**More Feature 3 scenarios from the seed:**

- **Lost phone / reissue.** Staff → Claims & Pickup → filter *Ready for pickup* → TRK-DEMO0001
  (Arun) → **Reissue code**. Arun's old code now verifies as invalid; his page shows the new code.
- **Borderline → staff.** Filter *Borderline answers*. Karthik's phone claim (TRK-DEMO0008)
  answered most questions, failed a follow-up, and waits for **Approve** or **Reject**.
- **Multi-claim conflict.** Log in as Ravi (`T26100105` / `80987 65432`) and open TRK-DEMO0007.
  The red bag shows **No longer available**, because Senthil's claim holds it. No other details
  are shown.
- **Expired token.** Arun's TRK-DEMO0009 laptop claim expired after 7 days. The laptop is back
  to `open`.

## 4. Proof that it is tested

```bash
python -m pytest tests -q
```
All three features are covered. [VERIFICATION.md](VERIFICATION.md) summarises them, and
[VERIFICATION_F1.md](VERIFICATION_F1.md), [VERIFICATION_F2.md](VERIFICATION_F2.md) and
[VERIFICATION_F3.md](VERIFICATION_F3.md) map every rule to its test.
