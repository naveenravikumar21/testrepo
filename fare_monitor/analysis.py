"""Validation, preference filtering, ranking and alert decisions."""
from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from .config import Config
from .models import FareOption, Layover, fmt_minutes


# ---- validation ----------------------------------------------------------------------------------
def validate(opt: FareOption, travel_date: date, config: Config, now: datetime | None = None) -> str | None:
    """Return a rejection reason, or None if the option is a usable, trustworthy observation."""
    trip, prefs, ver = config.trip, config.preferences, config.verification
    now = now or datetime.now(timezone.utc)
    if opt.currency.upper() != trip.currency:
        return f"currency {opt.currency} != {trip.currency} (prices are never converted or estimated)"
    if opt.travel_date != travel_date.isoformat() or not opt.departure_local.startswith(travel_date.isoformat()):
        return f"departure {opt.departure_local} is not on {travel_date}"
    origin, dest = opt.extra.get("origin"), opt.extra.get("destination")
    if (origin and origin != trip.origin) or (dest and dest != trip.destination):
        return f"route {origin}-{dest} does not match {trip.origin}-{trip.destination}"
    if not (ver.min_plausible_fare <= opt.price <= ver.max_plausible_fare):
        return f"implausible fare {opt.price}"
    if opt.expires_at:
        try:
            if datetime.fromisoformat(opt.expires_at.replace("Z", "+00:00")) <= now:
                return "offer expired"
        except ValueError:
            pass
    if opt.stops > prefs.max_stops:
        return f"{opt.stops} stops > {prefs.max_stops}"
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


def value_score(opt_price: float, duration_minutes: int, stops: int, config: Config) -> float:
    p = config.preferences
    return round(opt_price + p.value_inr_per_hour * duration_minutes / 60 + p.value_inr_per_stop * stops, 2)


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
class AlertCandidate:
    kind: str                      # baseline | record_low | attractive | new_availability
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


KIND_PRIORITY = {"record_low": 0, "attractive": 1, "new_availability": 2, "baseline": 3}
KEY_KIND = {"record_low": "record_low", "target": "attractive", "attractive": "attractive",
            "new": "new_availability", "baseline": "baseline"}


def decide_alerts(db, run_id: int, config: Config, now: datetime) -> tuple[list[AlertCandidate], list[dict]]:
    """Return (alerts to send, record lows to persist). Pure decision logic over the database."""
    min_ver = int(config.verification.alert_min_status)
    a = config.alerts
    candidates: dict[int, AlertCandidate] = {}
    record_lows: list[dict] = []

    def add(kind, obs, prev, reason, key):
        cand = candidates.get(obs["id"])
        if cand is None:
            cand = candidates[obs["id"]] = AlertCandidate(kind, obs["travel_date"], obs, prev)
        elif KIND_PRIORITY[kind] < KIND_PRIORITY[cand.kind]:
            cand.kind = kind
        cand.reasons.append(reason)
        cand.dedup_keys.append(key)

    for travel_date in (d.isoformat() for d in config.trip.dates):
        best_row = db.run_best(run_id, travel_date, min_ver)
        if best_row is None:
            continue
        best = row_view(best_row)
        prev_row = db.previous_low(travel_date, min_ver, run_id)
        prev_low = prev_row["price"] if prev_row else None
        price_key = f"{best['flight_key']}|{best['price']:.0f}"

        # 1. record lows
        if prev_low is None:
            record_lows.append({"travel_date": travel_date, "observation_id": best["id"], "price": best["price"],
                                "previous_low": None})
            if a.send_baseline:
                add("baseline", best, None, "First verified fare recorded for this date (baseline)",
                    f"baseline|{travel_date}")
        elif best["price"] < prev_low - 0.005:
            record_lows.append({"travel_date": travel_date, "observation_id": best["id"], "price": best["price"],
                                "previous_low": prev_low})
            drop = prev_low - best["price"]
            pct = 100 * drop / prev_low
            if drop >= a.min_drop_inr and pct >= a.min_drop_pct:
                add("record_low", best, prev_low, f"New record low (down {pct:.1f}%)",
                    f"record_low|{price_key}")

        # 2. particularly attractive fares
        if a.target_fare_inr and best["price"] <= a.target_fare_inr:
            add("attractive", best, prev_low, f"At or below your target fare of ₹{a.target_fare_inr:,.0f}",
                f"target|{price_key}")
        history = [r["price"] for r in db.hourly_lows(travel_date, min_ver, since=now - timedelta(days=7))
                   if r["run_id"] != run_id]
        if len(history) >= a.attractive_min_history:
            median = statistics.median(history)
            threshold = median * (1 - a.attractive_below_median_pct / 100)
            if best["price"] <= threshold:
                add("attractive", best, prev_low,
                    f"{100 * (median - best['price']) / median:.0f}% below the 7-day median low (₹{median:,.0f})",
                    f"attractive|{price_key}")

        # 3. previously unavailable flights that are now bookable at a competitive price
        if db.date_had_successful_search_before(travel_date, run_id):
            ceiling = best["price"] * (1 + a.competitive_pct / 100)
            seen_keys = set()
            for row in db.run_observations(run_id, travel_date, min_ver):
                obs = row_view(row)
                if obs["price"] > ceiling or obs["flight_key"] in seen_keys:
                    continue
                seen_keys.add(obs["flight_key"])
                last_seen = db.flight_last_seen_before(obs["flight_key"], run_id)
                if last_seen is None:
                    why = "Flight not seen in any earlier search"
                elif now - datetime.fromisoformat(last_seen) >= timedelta(hours=a.reappear_after_hours):
                    why = f"Flight back after being unavailable since {last_seen[:16].replace('T', ' ')} UTC"
                else:
                    continue
                add("new_availability", obs, prev_low,
                    f"{why}; bookable within {a.competitive_pct:g}% of the current lowest fare",
                    f"new|{obs['flight_key']}|{obs['price']:.0f}")

    alerts = []
    for cand in candidates.values():
        fresh = [(r, k) for r, k in zip(cand.reasons, cand.dedup_keys) if not db.alert_exists(k)]
        if fresh:
            cand.reasons = [r for r, _ in fresh]
            cand.dedup_keys = [k for _, k in fresh]
            kinds = [KEY_KIND[k.split("|", 1)[0]] for k in cand.dedup_keys]
            cand.kind = min(kinds, key=KIND_PRIORITY.__getitem__)
            alerts.append(cand)
    alerts.sort(key=lambda c: (KIND_PRIORITY[c.kind], c.observation["price"]))
    return alerts, record_lows
