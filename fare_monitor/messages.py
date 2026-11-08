"""Messages posted on watch issues (GitHub Markdown) and sent by email/console."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from .models import Verification, fmt_inr, fmt_minutes

DISCLAIMER = "Nothing has been booked or paid. Fares can change until payment is completed."


@dataclass
class Message:
    subject: str
    body: str                 # GitHub-flavoured Markdown (also readable as plain text)


def local_time(iso_utc: str | None, tz: ZoneInfo) -> str:
    if not iso_utc:
        return "—"
    return datetime.fromisoformat(iso_utc).astimezone(tz).strftime("%d %b %Y %H:%M IST")


def fmt_date(iso_date: str) -> str:
    return date.fromisoformat(iso_date).strftime("%a %d %b %Y")


def fmt_clock(iso_local: str) -> str:
    return datetime.fromisoformat(iso_local).strftime("%H:%M")


def arrival_label(obs: dict) -> str:
    dep, arr = obs["departure_local"][:10], obs["arrival_local"][:10]
    clock = fmt_clock(obs["arrival_local"])
    if arr != dep:
        clock += f" (+{(date.fromisoformat(arr) - date.fromisoformat(dep)).days}d)"
    return clock


def status_badge(level: int) -> str:
    v = Verification(level)
    return f"{v.icon} {v.label}"


def _book_link(obs: dict) -> str:
    seller = obs.get("seller") or "booking site"
    return f"[{seller} via Google Flights]({obs['booking_url']})" if "google" in obs["booking_url"] \
        else f"[{seller}]({obs['booking_url']})"


def flight_line(obs: dict) -> str:
    return (f"{obs['airline']} **{obs['flights_label']}** · {fmt_clock(obs['departure_local'])} "
            f"{obs['origin']} → {arrival_label(obs)} {obs['destination']} · {fmt_minutes(obs['duration_minutes'])} · "
            f"{obs['stops_label']}")


def price_history_summary(insights: dict | None) -> str:
    """One line from Google's price insights (indicative context, not a confirmed price)."""
    if not insights:
        return ""
    parts = []
    level = insights.get("price_level")
    rng = insights.get("typical_price_range") or []
    if level:
        parts.append(f"Google rates today's prices **{level}**"
                     + (f" (typical {fmt_inr(rng[0])}–{fmt_inr(rng[1])})" if len(rng) == 2 else ""))
    hist = [p for p in insights.get("price_history") or [] if isinstance(p, (list, tuple)) and len(p) == 2]
    if len(hist) >= 2:
        first, last = hist[0], hist[-1]
        prices = [p[1] for p in hist]
        d0 = datetime.fromtimestamp(first[0], timezone.utc).strftime("%d %b")
        trend = "rising" if last[1] > first[1] * 1.03 else "falling" if last[1] < first[1] * 0.97 else "flat"
        span = max(1, round((last[0] - first[0]) / 86400))
        parts.append(f"over the last {span} days its price history is **{trend}**: {fmt_inr(first[1])} on {d0} → "
                     f"{fmt_inr(last[1])} now (low {fmt_inr(min(prices))}, high {fmt_inr(max(prices))})")
    return "; ".join(parts) + "." if parts else ""


# ---- watch lifecycle -----------------------------------------------------------------------------
def watch_ack(watch, estimate: int, total_estimate: int, limit: int, updated: bool) -> Message:
    lines = [f"### {'✏️ Watch updated' if updated else '✅ Watch set up'}", ""]
    lines += [f"- {l}" for l in watch.summary_lines()]
    lines += ["", f"Estimated use: about **{estimate} searches** in the next 30 days "
                  f"(all watches together: ~{total_estimate} of the {limit} free searches a month)."]
    if total_estimate > limit:
        lines += ["", f"⚠️ That is more than the free plan allows. Checks will pause automatically when the monthly "
                      f"limit is close. Pick fewer dates or check less often to avoid gaps."]
    lines += ["", "The first check is running now. I'll comment here only when something is worth your attention; "
                  "the **Latest status** comment is updated quietly after every check.",
              "", "_To stop this watch, close the issue. To change it, edit the issue description._"]
    return Message(f"Fare watch {'updated' if updated else 'set up'}: {watch.route}", "\n".join(lines))


def watch_errors(watch) -> Message:
    lines = ["### ⚠️ I couldn't set up this watch", ""] + [f"- {e}" for e in watch.errors]
    lines += ["", "Edit the issue description to fix it; I'll check again automatically."]
    return Message(f"Fare watch needs a fix: {watch.title}", "\n".join(lines))


def first_results(watch, infos: list[dict], tz: ZoneInfo) -> Message:
    lines = [f"### ✈️ First prices · {watch.route}", "",
             "| Date | Cheapest found | Flight | Price status |", "|---|---|---|---|"]
    notes = []
    for info in infos:
        c = info.get("cheapest")
        if c:
            lines.append(f"| {fmt_date(info['date'])} | **{fmt_inr(c['price'])}** | {flight_line(c)} | "
                         f"{status_badge(c['verification'])} |")
        else:
            lines.append(f"| {fmt_date(info['date'])} | — | no flights found{' (search failed)' if info.get('error') else ''} | |")
        h = price_history_summary(info.get("price_insights"))
        if h:
            notes.append(f"- **{fmt_date(info['date'])}:** {h}")
    if notes:
        lines += ["", "**Price trend (Google Flights, indicative):**", *notes]
    if watch.target:
        hits = [i for i in infos if i.get("confirmed") and i["confirmed"]["price"] <= watch.target]
        lines += ["", (f"🎯 Already at or below your book-now price of {fmt_inr(watch.target)} on "
                       + ", ".join(fmt_date(i["date"]) for i in hits) + ". See the alert below.") if hits else
                  f"🎯 Nothing at or below your book-now price of {fmt_inr(watch.target)} yet."]
    lines += ["", "_🟡 SELLER-QUOTED = final price quoted by the named airline/agent. ⚪ INDICATIVE = search price only; "
                  "it is confirmed with the seller before any alert._", "", f"_{DISCLAIMER}_"]
    return Message(f"First prices for {watch.route}", "\n".join(lines))


def alert_message(watch, items, overall: dict | None, tz: ZoneInfo) -> Message:
    title = {"record_low": "📉 New lowest fare", "under_target": "🎯 Fare under your book-now price",
             "new_flight": "🆕 New cheaper flight available"}[items[0].kind]
    best = min(items, key=lambda i: i.observation["price"]).observation
    subject = f"{title.split(' ', 1)[1]}: {fmt_inr(best['price'])} {watch.route} {fmt_date(best['travel_date'])}"
    lines = [f"## {title} · {watch.route}", ""]
    for it in items:
        o = it.observation
        lines.append(f"### {fmt_date(o['travel_date'])} — {fmt_inr(o['price'])}")
        lines += [f"- **Why:** {'; '.join(it.reasons)}"]
        if it.previous_low is None:
            lines.append("- **Previous lowest confirmed:** none yet")
        else:
            diff = it.drop or 0
            change = (f"▼ {fmt_inr(diff)} ({it.drop_pct:.1f}% lower)" if diff > 0 else
                      f"▲ {fmt_inr(-diff)} above the lowest" if diff < 0 else "same as the lowest")
            lines.append(f"- **Previous lowest confirmed:** {fmt_inr(it.previous_low)} · **Difference:** {change}")
        baggage = " · ".join(x for x in (o.get("cabin_baggage"), o.get("checked_baggage")) if x) or "not provided"
        lines += [
            f"- **Flight:** {flight_line(o)}",
            f"- **Layovers:** {o['layovers_label']}",
            f"- **Baggage:** {baggage}",
            f"- **Price:** {fmt_inr(o['price'])} total for 1 adult incl. taxes · {status_badge(o['verification'])} — "
            f"{o.get('verification_note') or ''}",
            f"- **Checked at:** {local_time(o['observed_at'], tz)}",
            f"- **Book:** {_book_link(o)}",
            "",
        ]
    if overall:
        lines.append(f"Cheapest confirmed fare across this watch right now: **{fmt_inr(overall['price'])}** on "
                     f"{fmt_date(overall['travel_date'])} ({overall['airline']} {overall['flights_label']}).")
    lines += ["", f"_⚠️ {DISCLAIMER}_"]
    return Message(subject, "\n".join(lines))


def status_comment(watch, infos: list[dict], usage_line: str, next_check: str, tz: ZoneInfo, now_iso: str,
                   dashboard_url: str = "") -> str:
    lines = [f"### 📋 Latest status · {watch.route}", f"_Updated {local_time(now_iso, tz)} · next check: {next_check}_",
             "", "| Date | Cheapest now | Lowest confirmed ever | Google says |", "|---|---|---|---|"]
    for info in infos:
        c, low = info.get("cheapest"), info.get("all_time_low")
        cheap = (f"**{fmt_inr(c['price'])}** {status_badge(c['verification'])}<br>{c['airline']} {c['flights_label']}, "
                 f"{fmt_clock(c['departure_local'])}→{arrival_label(c)}, {c['stops_label']}") if c else "—"
        lowest = f"{fmt_inr(low['price'])} ({low['airline']}, {local_time(low['observed_at'], tz)[:6]})" if low else "—"
        pi = info.get("price_insights") or {}
        lines.append(f"| {fmt_date(info['date'])} | {cheap} | {lowest} | {pi.get('price_level') or '—'} |")
    lines += ["", usage_line]
    if dashboard_url:
        lines += ["", f"[Open the dashboard]({dashboard_url})"]
    lines += ["", "_This comment is edited after every check (no email). Alerts are posted as new comments._"]
    return "\n".join(lines)


def watch_finished(watch, infos: list[dict], tz: ZoneInfo) -> Message:
    lines = [f"### 🏁 Watch finished · {watch.route}", "", "All travel dates have passed, so this watch is closed.", ""]
    for info in infos:
        low = info.get("all_time_low")
        if low:
            lines.append(f"- {fmt_date(info['date'])}: lowest confirmed {fmt_inr(low['price'])} "
                         f"({low['airline']} {low['flights_label']}, {local_time(low['observed_at'], tz)})")
    return Message(f"Fare watch finished: {watch.route}", "\n".join(lines))


def health_problem(errors: list[str], failures: int) -> Message:
    lines = [f"### ⚠️ Price checks are failing ({failures} in a row)", "",
             "No prices could be fetched, so nothing is reported until this is fixed. Latest errors:", ""]
    lines += [f"- `{e[:300]}`" for e in errors[:5]]
    lines += ["", "Common causes: the `SERPAPI_API_KEY` secret is missing or wrong, or the monthly searches are "
                  "used up. Check the run logs under the Actions tab."]
    return Message("Fare monitor: price checks failing", "\n".join(lines))


def health_recovered(failures: int) -> Message:
    return Message("Fare monitor recovered", f"### ✅ Price checks are working again after {failures} failed checks.")


def budget_paused(used: int, limit: int) -> Message:
    return Message("Fare monitor: monthly search limit reached",
                   f"### ⏸️ Checks paused for the rest of the month\n\n{used} of {limit} free searches have been used. "
                   "Checks resume automatically on the 1st. To keep checking, reduce dates/frequency or upgrade "
                   "the SerpApi plan (then set the repository variable `SERPAPI_MONTHLY_LIMIT`).")
