"""Validation, ranking, seller-check gating and per-watch alert decisions."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from .config import Config
from .models import FareOption, Layover, fmt_minutes


# ---- validation ----------------------------------------------------------------------------------
def validate(opt: FareOption, travel_date: date, config: Config, now: datetime | None = None) -> str | None:
    """Return a rejection reason, or None if the option is a usable, trustworthy observation."""
    prefs, ver = config.preferences, config.verification
    now = now or datetime.now(timezone.utc)
    if opt.currency.upper() != config.defaults.currency:
        return f"currency {opt.currency} != {config.defaults.currency} (prices are never converted or estimated)"
    if opt.travel_date != travel_date.isoformat() or not opt.departure_local.startswith(travel_date.isoformat()):
        return f"departure {opt.departure_local} is not on {travel_date}"
    origin, dest = opt.extra.get("origin"), opt.extra.get("destination")
    if (origin and origin != opt.origin) or (dest and dest != opt.destination):
        return f"route {origin}-{dest} does not match {opt.origin}-{opt.destination}"
    if not (ver.min_plausible_fare <= opt.price <= ver.max_plausible_fare):
        return f"implausible fare {opt.price}"
    if opt.expires_at:
        try:
            if datetime.fromisoformat(opt.expires_at.replace("Z", "+00:00")) <= now:
                return "offer expired"
        except ValueError:
            pass
    if opt.stops > config.defaults.max_stops:
        return f"{opt.stops} stops > {config.defaults.max_stops}"
    if opt.duration_minutes <= 0 or opt.duration_minutes > prefs.max_duration_minutes:
        return f"duration {fmt_minutes(opt.duration_minutes)} outside limit"
    dep_time = datetime.fromisoformat(opt.departure_local).time()
    if not (prefs.earliest_departure <= dep_time <= prefs.latest_departure):
        return f"departure time {dep_time} outside preferred window"
    for lay in opt.layovers:
        if lay.minutes < prefs.min_layover_minutes or lay.minutes > prefs.max_layover_minutes:
            return f"layover {lay.airport} {fmt_minutes(lay.minutes)} outside {prefs.min_layover_minutes}-" \
                   f"{prefs.max_layover_minutes} min"
    return None


def value_score(price: float, duration_minutes: int, stops: int, config: Config) -> float:
    p = config.preferences
    return round(price + p.value_inr_per_hour * duration_minutes / 60 + p.value_inr_per_stop * stops, 2)


# ---- seller-check gating -------------------------------------------------------------------------
def needs_confirmation(opt: FareOption, prev_low_search_price: float | None, targets: list[float], db, config: Config,
                       now: datetime) -> str | None:
    """Should we spend a search confirming this option's final price? Returns the reason or None.

    Confirmation costs one SerpApi search, so it is only done when the result could change what you
    are told: the first price for a date, a possible new record low, or a fare at/below a book-now price
    that hasn't been confirmed in the last few days."""
    min_ver = int(config.verification.alert_min_status)
    if opt.verification >= config.verification.alert_min_status:
        return None
    if prev_low_search_price is None:
        return "first price for this date"
    if opt.price < prev_low_search_price - 0.5:
        return "possible new record low"
    if any(opt.price <= t for t in targets):
        since = now - timedelta(hours=config.verification.reverify_after_hours)
        if db.recent_confirmation(opt.flight_key, min_ver, since) is None:
            return "at or below a book-now price"
    return None


# ---- row helpers ---------------------------------------------------------------------------------
def row_view(row) -> dict:
    """Turn an observations row into a plain dict with parsed JSON fields."""
    d = dict(row)
    d["flight_numbers"] = json.loads(d["flight_numbers"])
    d["layovers"] = [Layover(**l) for l in json.loads(d["layovers_json"])]
    d["booking_request"] = json.loads(d["booking_request_json"]) if d.get("booking_request_json") else None
    d["extra"] = json.loads(d["extra_json"]) if d.get("extra_json") else {}
    d["flights_label"] = " / ".join(d["flight_numbers"])
    d["stops_label"] = "Non-stop" if d["stops"] == 0 else f"{d['stops']} stop{'s' if d['stops'] > 1 else ''}"
    d["layovers_label"] = ", ".join(
        f"{l.airport} {fmt_minutes(l.minutes)}{' (overnight)' if l.overnight else ''}" for l in d["layovers"]
    ) or "none (direct)"
    return d


# ---- alert decisions -----------------------------------------------------------------------------
@dataclass
class AlertItem:
    kind: str                      # record_low | under_target | new_flight
    travel_date: str
    observation: dict
    previous_low: float | None
    reasons: list[str] = field(default_factory=list)
    dedup_keys: list[str] = field(default_factory=list)

    @property
    def drop(self) -> float | None:
        if self.previous_low is None:
            return None
        return round(self.previous_low - self.observation["price"], 2)

    @property
    def drop_pct(self) -> float | None:
        if not self.previous_low:
            return None
        return round(100.0 * (self.previous_low - self.observation["price"]) / self.previous_low, 2)


KIND_PRIORITY = {"record_low": 0, "under_target": 1, "new_flight": 2}
KEY_KIND = {"low": "record_low", "target": "under_target", "new": "new_flight"}


def decide_watch_alerts(db, run_id: int, watch, dates: list[date], config: Config, now: datetime) -> list[AlertItem]:
    """Alerts for one watch from the observations stored in this run (only confirmed prices count)."""
    min_ver = int(config.verification.alert_min_status)
    a = config.alerts
    items: dict[int, AlertItem] = {}

    def add(kind, obs, prev, reason, key):
        item = items.setdefault(obs["id"], AlertItem(kind, obs["travel_date"], obs, prev))
        item.reasons.append(reason)
        item.dedup_keys.append(key)

    for d in dates:
        ds = d.isoformat()
        rows = db.run_observations(run_id, watch.origin, watch.destination, ds, min_ver, watch.max_stops)
        if not rows:
            continue
        best = row_view(rows[0])
        prev_row = db.previous_low(watch.origin, watch.destination, ds, min_ver, watch.max_stops, run_id)
        prev_low = prev_row["price"] if prev_row else None
        price_key = f"{watch.id}|{best['flight_key']}|{best['price']:.0f}"

        if prev_low is not None and best["price"] < prev_low - 0.005:
            drop = prev_low - best["price"]
            if drop >= a.min_drop_inr and 100 * drop / prev_low >= a.min_drop_pct:
                add("record_low", best, prev_low, f"New lowest confirmed fare (down {100 * drop / prev_low:.1f}%)",
                    f"low|{price_key}")
        if watch.target and best["price"] <= watch.target:
            add("under_target", best, prev_low, f"At or below your book-now price of ₹{watch.target:,.0f}",
                f"target|{price_key}")

        if db.route_had_search_before(watch.origin, watch.destination, ds, run_id):
            ceiling = best["price"] * (1 + a.competitive_pct / 100)
            if watch.target:
                ceiling = min(ceiling, watch.target)
            seen = set()
            for row in rows:
                obs = row_view(row)
                if obs["price"] > ceiling or obs["flight_key"] in seen:
                    continue
                seen.add(obs["flight_key"])
                last_seen = db.flight_last_seen_before(obs["flight_key"], run_id)
                if last_seen is None:
                    why = "A flight not seen in earlier checks"
                elif now - datetime.fromisoformat(last_seen) >= timedelta(days=a.reappear_after_days):
                    why = f"A flight that was unavailable since {last_seen[:10]}"
                else:
                    continue
                add("new_flight", obs, prev_low, f"{why} is now bookable at a competitive price",
                    f"new|{watch.id}|{obs['flight_key']}|{obs['price']:.0f}")

    out = []
    for item in items.values():
        fresh = [(r, k) for r, k in zip(item.reasons, item.dedup_keys) if not db.alert_exists(k)]
        if not fresh:
            continue
        item.reasons = [r for r, _ in fresh]
        item.dedup_keys = [k for _, k in fresh]
        item.kind = min((KEY_KIND[k.split("|", 1)[0]] for k in item.dedup_keys), key=KIND_PRIORITY.__getitem__)
        out.append(item)
    out.sort(key=lambda i: (KIND_PRIORITY[i.kind], i.observation["price"]))
    return out
