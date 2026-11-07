# ✈️ Jaipur → Kochi fare monitor

An automated agent that watches **one-way, economy, 1-adult** fares from **Jaipur (JAI)** to
**Kochi (COK)** for **4 January 2027** and **5 January 2027**. It runs every hour on GitHub Actions and
stores every verified observation in SQLite. It sends a Telegram and/or email alert when a meaningful new
opportunity appears.

* Sources: authorised APIs only, no HTML scraping. **Duffel** provides live airline offers that are
  re-priced to `VERIFIED`. **Google Flights via SerpApi** provides seller-quoted final prices from airlines
  and OTAs such as MakeMyTrip, Cleartrip and ixigo.
* Prices are never invented, estimated or currency-converted. Search-only prices are labelled
  `INDICATIVE` and never trigger alerts by default.
* **It never books or pays.** Booking links only open the seller's site.

Architecture, data sources, verification rules and the alert logic are in
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

```
fare_monitor/
  providers/duffel.py           Duffel offer request + GET /air/offers re-pricing  -> VERIFIED
  providers/serpapi_google.py   Google Flights search + booking options           -> SELLER_QUOTED
  analysis.py                   validation, preferences, record-low / alert decisions
  runner.py                     one hourly cycle
  db.py                         SQLite schema + queries
  notify/                       telegram | email | console
  messages.py, report.py        alert text, daily summary, HTML dashboard, job summary
config/monitor.toml             all non-secret settings (thresholds, filters, providers, channels)
.github/workflows/monitor.yml   hourly scheduler, state persistence, auto-stop
.github/workflows/ci.yml        tests on every push
```

---

## Deployment (about 20 minutes)

### 1. Get fare-source credentials (at least one; both is best)

| Secret | Where | Notes |
|---|---|---|
| `SERPAPI_API_KEY` | <https://serpapi.com> → Dashboard → API key | Default settings use up to **6 searches/hour** (1 search + 2 booking checks per date), about **4,300/month**. Pick a plan that covers this, or set `booking_checks_per_date = 1` in `config/monitor.toml` (about 2,900/month). Searches use `no_cache=true`, so no stale cached prices are served. |
| `DUFFEL_ACCESS_TOKEN` | <https://app.duffel.com> → Developers → Access tokens | Must be a **live-mode** token: test mode only returns the fictional "Duffel Airways", and the monitor refuses it. Live mode requires completing Duffel's account verification. Check Duffel's current pricing for search-to-book ratio charges. Coverage of Indian domestic carriers depends on your Duffel account. If Duffel returns no JAI→COK offers, the dashboard shows the search as `empty`, and Google Flights still covers the route. |

### 2. Create the notification channel

**Telegram (recommended)**
1. In Telegram, open **@BotFather** → `/newbot` → copy the token (`TELEGRAM_BOT_TOKEN`).
2. Send any message to your new bot.
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `message.chat.id` (`TELEGRAM_CHAT_ID`).
   Several chat IDs can be comma-separated.

**Email (optional or additional)**: any SMTP server. For Gmail, use an
[app password](https://myaccount.google.com/apppasswords) with host `smtp.gmail.com`, port 587.
Secrets: `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_FROM`, `EMAIL_TO`
(comma-separated). Set the repository variable `SMTP_USE_SSL=true` for port 465.

### 3. Add GitHub secrets and variables

Repository → **Settings → Secrets and variables → Actions**.

*Secrets*: the keys above (`SERPAPI_API_KEY`, `DUFFEL_ACCESS_TOKEN`, `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_CHAT_ID`, and the SMTP/email ones if used).

*Variables* (all optional):

| Variable | Default | Meaning |
|---|---|---|
| `NOTIFY_CHANNELS` | `telegram` | `telegram`, `email`, `console`, comma-separated, e.g. `telegram,email` |
| `TARGET_FARE_INR` | off | Alert whenever a verified fare is at or below this amount |
| `ALERT_MIN_STATUS` | `seller_quoted` | `verified` = only Duffel re-priced offers can alert. `indicative` is not recommended |
| `PROVIDERS` | `duffel,serpapi_google_flights` | Restrict sources |
| `DAILY_SUMMARY_HOUR` | `8` | Hour (IST) after which the daily summary is sent |
| `DASHBOARD_URL` | — | Put your GitHub Pages URL here so the summary links to it |
| `MONITOR_ENABLED` | `true` | Set to `false` to pause (manual stop) |

### 4. Merge to the default branch and test

GitHub only runs scheduled workflows from the **default branch**. Merge this branch into it, or make this
branch the default. Then go to **Actions → "Fare monitor (JAI → COK)" → Run workflow**:

1. `command = test-notify`: you should receive a test message on every configured channel.
2. `command = run` with **dry run** ticked: a full search with alerts printed in the log only. State is
   not saved.
3. `command = run`: the first real cycle. You get one **baseline** message per date, and the `fare-data`
   branch appears with `fares.sqlite3`, `index.html`, `summary.md` and `summary.json`.

From then on it runs every hour at :17 (GitHub may delay scheduled runs by a few minutes under load).

### 5. Dashboard

* Every run's **job summary** (Actions → run) shows the current lowest fares and source status.
* `index.html` on the `fare-data` branch is the full dashboard. It shows:
  * lowest fare for 4 Jan, for 5 Jan, and overall
  * all-time lows
  * the hourly price trend
  * an airline comparison
  * every option from the latest search, with baggage and booking links
  * source and verification status
  * alert history

  To publish it, open **Settings → Pages → Deploy from a branch → `fare-data` / root**. Pages on private
  repositories needs a paid GitHub plan. On a public repository the dashboard and database are public:
  they hold route, dates and fares, no personal data.
* Or download the `fare-state-<run id>` artifact from any run and open `index.html` locally.

---

## Operating it

| Task | How |
|---|---|
| Pause / stop | Set the variable `MONITOR_ENABLED=false`, or Actions → workflow → **Disable workflow** |
| Automatic stop | After 5 Jan 2027 (IST) the next run sends a final summary and disables the workflow itself |
| Change thresholds/filters | Edit `config/monitor.toml` (all keys are commented) and push |
| Send the summary now | Run workflow with `command = send-summary` |
| Failures | A run where every search fails ends red, so GitHub emails you. After 3 such runs in a row you also get an operations alert on Telegram/email, then a "recovered" message when searches succeed again. Partial failures (one source down) are listed on the dashboard |
| Inspect data | Download `fares.sqlite3` from the `fare-data` branch: `sqlite3 fares.sqlite3 "select travel_date, airline, flight_numbers, price, verification, observed_at from observations order by price limit 20"` |

### What the alerts contain

New fare, previous lowest, difference and percentage drop, airline and flight numbers, travel date,
departure and arrival times, total duration, stops and layovers (airport and length), baggage where the
source provides it, verification status and source/seller, booking link and verification timestamp. The
cheapest fare across both dates is added at the end.

---

## Local development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest -q                                  # 18 tests, synthetic API responses, no network
export SERPAPI_API_KEY=... TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...
python -m fare_monitor check-config                  # which sources/channels are configured
python -m fare_monitor test-notify                   # send a test message
python -m fare_monitor run --dry-run                 # real search, alerts printed to console
python -m fare_monitor run                           # real cycle; state in ./state/
python -m fare_monitor report                        # rebuild ./state/index.html
```

Commands exit with status 0 for ok or partial runs, 1 when every search failed, and 2 for a
configuration error.

## Safety notes

* No ordering or payment endpoint is called anywhere in the code (Duffel is used only for
  `offer_requests` and `offers`).
* Indicative prices are shown with a grey `INDICATIVE` badge. They are never presented as bookable and
  never alert under the default settings.
* Fares can change between verification and payment. Every message says so.
