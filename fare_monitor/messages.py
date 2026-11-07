"""Human-readable alert, health and summary messages (plain text + Telegram/email-safe HTML)."""
from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .analysis import AlertCandidate
from .models import Verification, fmt_inr, fmt_minutes

DISCLAIMER = "Nothing has been booked or paid. Fares can change until payment is completed."
TITLES = {
    "record_low": "📉 New record-low fare",
    "attractive": "🔥 Particularly attractive fare",
    "new_availability": "🆕 Flight now bookable at a competitive fare",
    "baseline": "📌 Monitoring started: first verified fare",
}


@dataclass
class Message:
    subject: str
    text: str
    html: str        # Telegram-compatible subset: <b>, <i>, <a>, <code>; newlines preserved


def _e(s) -> str:
    return html.escape(str(s or ""), quote=True)


def local_time(iso_utc: str, tz: ZoneInfo) -> str:
    return datetime.fromisoformat(iso_utc).astimezone(tz).strftime("%d %b %Y %H:%M %Z")


def fmt_date(iso_date: str) -> str:
    return date.fromisoformat(iso_date).strftime("%a %d %b %Y")


def fmt_clock(iso_local: str) -> str:
    return datetime.fromisoformat(iso_local).strftime("%H:%M")


def arrival_label(obs: dict) -> str:
    dep, arr = obs["departure_local"][:10], obs["arrival_local"][:10]
    clock = fmt_clock(obs["arrival_local"])
    if arr != dep:
        days = (date.fromisoformat(arr) - date.fromisoformat(dep)).days
        clock += f" (+{days}d)"
    return clock


def option_lines(obs: dict, tz: ZoneInfo) -> list[tuple[str, str]]:
    """(label, value) pairs describing an observation. Values are plain text."""
    ver = Verification(obs["verification"])
    baggage = " · ".join(x for x in (
        f"cabin: {obs['cabin_baggage']}" if obs.get("cabin_baggage") else "",
        f"checked: {obs['checked_baggage']}" if obs.get("checked_baggage") else "") if x) or "not provided by source"
    return [
        ("Airline", f"{obs['airline']} — {obs['flights_label']}"),
        ("Travel date", fmt_date(obs["travel_date"])),
        ("Departs", f"{fmt_clock(obs['departure_local'])} JAI  →  Arrives {arrival_label(obs)} COK"),
        ("Duration", f"{fmt_minutes(obs['duration_minutes'])} · {obs['stops_label']}"),
        ("Layovers", obs["layovers_label"]),
        ("Baggage", baggage),
        ("Fare status", f"{ver.icon} {ver.label} — {obs.get('verification_note') or ''}"),
        ("Source", obs["source"] + (f" · seller: {obs['seller']}" if obs.get("seller") else "")),
        ("Verified at", local_time(obs["observed_at"], tz)),
    ]


def alert_message(c: AlertCandidate, overall_best: dict | None, tz: ZoneInfo) -> Message:
    obs = c.observation
    title = TITLES[c.kind]
    subject = f"{title.split(' ', 1)[1]}: {fmt_inr(obs['price'])} JAI→COK {fmt_date(obs['travel_date'])} " \
              f"({obs['flights_label']})"
    if c.previous_low is None:
        prev_line = ("Previous lowest", "none (first verified observation for this date)")
        diff_line = ("Difference", "—")
    else:
        prev_line = ("Previous lowest", fmt_inr(c.previous_low))
        if c.drop is not None and c.drop > 0:
            diff_line = ("Difference", f"▼ {fmt_inr(c.drop)} ({c.drop_pct:.1f}% lower)")
        elif c.drop is not None and c.drop < 0:
            diff_line = ("Difference", f"▲ {fmt_inr(-c.drop)} ({-c.drop_pct:.1f}% above the record low)")
        else:
            diff_line = ("Difference", "equal to the record low")
    rows = [("New fare", f"{fmt_inr(obs['price'])} ({obs['currency']}, total for 1 adult incl. taxes)"),
            prev_line, diff_line] + option_lines(obs, tz)
    why = [f"• {r}" for r in c.reasons]
    book = obs["booking_url"]
    overall = ""
    if overall_best:
        overall = (f"Cheapest across both dates right now: {fmt_inr(overall_best['price'])} on "
                   f"{fmt_date(overall_best['travel_date'])} ({overall_best['airline']} "
                   f"{overall_best['flights_label']})")

    text = "\n".join([f"{title} — JAI → COK", *why, ""] + [f"{k}: {v}" for k, v in rows] +
                     [f"Book: {book}", "", overall, "", "⚠️ " + DISCLAIMER])
    html_body = "\n".join([f"<b>{_e(title)} — JAI → COK</b>", *(_e(w) for w in why), ""] +
                          [f"<b>{_e(k)}:</b> {_e(v)}" for k, v in rows] +
                          [f'<b>Book:</b> <a href="{_e(book)}">{_e(book[:80])}</a>', "",
                           _e(overall), "", "⚠️ <i>" + _e(DISCLAIMER) + "</i>"])
    return Message(subject, text, html_body)


def plain_message(subject: str, lines: list[str]) -> Message:
    return Message(subject, "\n".join([subject, ""] + lines),
                   "\n".join([f"<b>{_e(subject)}</b>", ""] + [_e(l) for l in lines]))


def summary_message(summary: dict, tz: ZoneInfo, dashboard_hint: str = "",
                    subject: str = "🗓️ Daily fare summary — JAI → COK (4/5 Jan 2027)") -> Message:
    """Daily summary built from report.build_summary()."""
    lines = []
    for d in summary["dates"]:
        cur = d["current_best"]
        low = d["all_time_low"]
        lines.append(f"{fmt_date(d['date'])}:")
        if cur:
            lines.append(f"  Current lowest: {fmt_inr(cur['price'])} — {cur['airline']} {cur['flights_label']}, "
                         f"{fmt_clock(cur['departure_local'])}→{arrival_label(cur)}, "
                         f"{fmt_minutes(cur['duration_minutes'])}, {cur['stops_label']} "
                         f"[{Verification(cur['verification']).label}]"
                         + (" (STALE)" if d["stale"] else ""))
        else:
            lines.append("  Current lowest: no eligible fare in the latest search")
        if low:
            lines.append(f"  Lowest ever observed: {fmt_inr(low['price'])} ({low['airline']} "
                         f"{low['flights_label']}, {local_time(low['observed_at'], tz)})")
        if d.get("best_value") and cur and d["best_value"]["id"] != cur["id"]:
            bv = d["best_value"]
            lines.append(f"  Best value (price vs time): {fmt_inr(bv['price'])} — {bv['airline']} "
                         f"{bv['flights_label']}, {fmt_minutes(bv['duration_minutes'])}, {bv['stops_label']}")
    ob = summary["overall_best"]
    lines += ["", f"Overall cheapest now: {fmt_inr(ob['price'])} on {fmt_date(ob['travel_date'])} "
                  f"({ob['airline']} {ob['flights_label']})" if ob else "Overall cheapest now: —"]
    if summary["airlines"]:
        lines += ["", "Airline comparison (lowest eligible fare, last 7 days):"]
        for a in summary["airlines"][:8]:
            lines.append(f"  {a['airline']}: " + ", ".join(
                f"{date.fromisoformat(k).strftime('%d %b')} {fmt_inr(v)}" for k, v in sorted(a["by_date"].items())))
    lines += ["", f"Last successful search: {summary['last_success_local'] or 'never'}"]
    for p in summary["providers"]:
        lines.append(f"  {p['provider']} {p['travel_date']}: {p['status']}"
                     + (f" — {p['error'][:120]}" if p.get("error") else ""))
    if dashboard_hint:
        lines += ["", dashboard_hint]
    lines += ["", DISCLAIMER]
    return plain_message(subject, lines)
