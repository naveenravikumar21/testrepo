# ✈️ Flight fare watch

Tracks one-way economy fares (1 adult, INR) for the routes and dates you choose. It emails you through
GitHub when a fare is worth booking. It runs free on GitHub Actions, with prices from Google Flights through
SerpApi, inside SerpApi's free plan of 250 searches a month.

* **You choose:** route, dates or a date range, how often to check, and your book-now price, using a form.
* **You get:** an email when a **confirmed** fare is under your price, beats the lowest seen so far, or a
  new cheaper flight appears. You also get a quietly-updated status comment and a dashboard page.
* **It never books or pays.** Prices are never invented or converted. Search-only prices are marked
  *indicative* and are confirmed with the seller before any alert.

## Using it

### Start watching a trip
1. In the repository, open **Issues → New issue → Fare watch → Get started**.
2. Fill in the form:
   - From and To: an airport code like JAI or COK, or a city name like Jaipur or Kochi.
   - Travel dates: for example `2027-01-04, 2027-01-05`, or `2 Jan 2027 to 6 Jan 2027`.
   - How often to check: every day, 3 times a week, 2 times a week or once a week. You can instead tick
     the exact days.
   - Book-now price.
   - Stops.
3. Press **Submit**. Within a few minutes you'll see two comments, and get emails for them:
   - **Watch set up**: what will be checked and how many searches it will use. This comment becomes the
     **Latest status** and is edited quietly after every check.
   - **First prices**: the cheapest fare for each date and Google's price trend (low / typical / high,
     rising or falling).

After that, checks run at about **7 AM IST** on the days you chose. You only get an email when one of
these happens:

| Alert | When |
|---|---|
| 🎯 Fare under your book-now price | A seller-confirmed fare is at or below your price (once per flight and price) |
| 📉 New lowest fare | It beats the lowest confirmed fare by at least ₹200 **and** 2% |
| 🆕 New cheaper flight | A flight not seen before (or not for 3+ days) is bookable within 5% of the lowest fare |
| ⚠️ / ✅ Problem / recovered | Checks failed twice in a row, or are working again |
| 🏁 Watch finished | The dates have passed; the issue is closed automatically |

Each alert includes:
- the fare and the previous lowest, with the difference and percentage
- airline and flight numbers
- departure and arrival times, journey time, stops and layovers
- baggage, where the seller gives it
- who quoted the price, and when it was checked
- a booking link

**Change a watch:** edit the issue description; it's checked again straight away. **Stop it:** close the
issue. You can have several watches at once.

### Emails
GitHub sends these to your account's notification email (**github.com → Settings → Notifications →
Default notification email**). The GitHub mobile app can also push them to your phone.

### Dashboard
There's a web page with the lowest fares per date, a trend chart, an airline comparison, every option with
booking links, and searches used this month.
* One-time setup: **Settings → Pages → Build and deployment → Source: GitHub Actions**.
* Address: `https://<your-username>.github.io/<repository>/`. For this repository that is
  https://naveenravikumar21.github.io/testrepo/.

### Staying inside the free 250 searches a month
Each date costs **1 search per check**. Confirming a seller's final price costs 1 more, and only happens
when an alert is possible.

| Watch | Searches a month (approx.) |
|---|---|
| 2 dates, every day | ~70 |
| 2 dates, 3 times a week | ~30 |
| 5-day range, 2 times a week | ~45 |

The **Watch set up** comment shows the estimate for all your watches together. If SerpApi says fewer than
20 searches are left, checks pause until the 1st of the month and you get a comment. Soonest travel dates
are checked first.

### Pausing everything
Set the repository variable `MONITOR_ENABLED` to `false`, under **Settings → Secrets and variables →
Actions → Variables**. Alternatively, disable the **Fare monitor** workflow in the Actions tab.

---

## Setup (already done for this repository)

1. Create a free SerpApi account (serpapi.com) and copy the API key.
2. Go to **Settings → Secrets and variables → Actions → New repository secret**, name it
   `SERPAPI_API_KEY` and paste the key.
3. Enable Pages (see Dashboard above). This is optional.

Optional repository variables:

| Variable | Meaning |
|---|---|
| `SERPAPI_MONTHLY_LIMIT` | Your plan's searches per month, if you upgrade (default 250) |
| `NOTIFY_CHANNELS` | `github` (default); add `email` (needs `SMTP_*` secrets) or `telegram` |
| `ALERT_MIN_STATUS` | `seller_quoted` (default) or `verified` |
| `DASHBOARD_URL` | Override the dashboard address used in comments |

Thresholds and filters are in `config/monitor.toml`, and every key there is commented. The filters are
maximum journey time, departure window, minimum and maximum layover, and the alert thresholds.

## How it works

The diagram and design details are in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

* `.github/workflows/monitor.yml` runs daily at 07:05 IST, and also when a Fare-watch issue is opened or
  edited. Only issues from the repository owner or collaborators count, so strangers can't spend your
  searches.
* `fare_monitor/watches.py` reads the issue form.
* `fare_monitor/runner.py` does the following each check:
  1. Decides which watches are due.
  2. Checks the search budget.
  3. Searches each route and date once, even if several watches want it.
  4. Confirms the seller price only when it could trigger an alert.
  5. Stores everything in SQLite.
  6. Posts alerts and status.
* The database is kept on the `fare-data` branch, and each run is also backed up as a 14-day artifact.

## Local development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest -q                      # synthetic API responses, no network
export SERPAPI_API_KEY=... TRAVEL_DATES=2027-01-04,2027-01-05
python -m fare_monitor run --dry-run     # local check, messages printed to the console
python -m fare_monitor report            # rebuild state/index.html
```
