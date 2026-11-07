"""One monitoring cycle: search -> validate -> verify -> store -> decide alerts -> notify -> report."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from .analysis import decide_alerts, row_view, validate, value_score
from .config import Config
from .db import Database
from .http import build_session
from .messages import alert_message, plain_message, summary_message
from .models import fmt_inr
from .notify import Dispatcher
from .providers import ProviderError, build_providers
from .report import build_summary, write_reports

log = logging.getLogger(__name__)


class ConfigError(Exception):
    pass


@dataclass
class RunResult:
    status: str                      # ok | partial | failed | skipped | window_closed
    run_id: int | None = None
    searches: int = 0
    failed_searches: int = 0
    observations: int = 0
    alerts_sent: int = 0
    errors: list[str] = field(default_factory=list)
    window_closed: bool = False


def active_dates(config: Config, now: datetime) -> list[date]:
    today = now.astimezone(config.trip.timezone).date()
    return [d for d in config.trip.dates if d >= today]


def _send(dispatcher: Dispatcher | None, message) -> list[str]:
    if dispatcher is None:
        return []
    return dispatcher.send(message)


def run_once(config: Config, db: Database, session=None, dispatcher: Dispatcher | None = None,
             now: datetime | None = None, providers=None) -> RunResult:
    now = now or datetime.now(timezone.utc)
    tz = config.trip.timezone
    session = session or build_session()

    if not config.monitor_enabled:
        log.info("MONITOR_ENABLED is false: monitoring paused, nothing searched")
        return RunResult(status="skipped")

    dates = active_dates(config, now)
    if not dates:
        log.info("All travel dates have passed: monitoring window closed")
        if db.kv_get("final_summary_sent") is None:
            summary = build_summary(db, config, now)
            msg = summary_message(summary, tz, "Monitoring has ended because the travel dates have passed.",
                                  subject="🏁 Final fare summary — monitoring stopped (travel dates passed)")
            if _send(dispatcher, msg) or dispatcher is None or not dispatcher.notifiers:
                db.kv_set("final_summary_sent", now.isoformat())
            write_reports(summary, config)
        return RunResult(status="window_closed", window_closed=True)

    providers = providers if providers is not None else build_providers(config, session)
    if not providers:
        raise ConfigError("No fare provider is configured. Set DUFFEL_ACCESS_TOKEN and/or SERPAPI_API_KEY "
                          "(see README) - the monitor never invents prices.")

    result = RunResult(status="ok", run_id=db.start_run(now))
    run_id = result.run_id
    for travel_date in dates:
        for provider in providers:
            result.searches += 1
            try:
                options, meta = provider.search(travel_date)
            except ProviderError as exc:
                result.failed_searches += 1
                result.errors.append(f"{provider.name} {travel_date}: {exc}")
                log.error("Search failed: %s %s: %s", provider.name, travel_date, exc)
                db.record_search(run_id, provider.name, travel_date.isoformat(), "error", error=str(exc)[:1000],
                                 now=now)
                continue
            except Exception as exc:  # parser bug or unexpected payload: never crash the whole run
                result.failed_searches += 1
                result.errors.append(f"{provider.name} {travel_date}: unexpected {type(exc).__name__}: {exc}")
                log.exception("Unexpected error in %s %s", provider.name, travel_date)
                db.record_search(run_id, provider.name, travel_date.isoformat(), "error",
                                 error=f"{type(exc).__name__}: {exc}"[:1000], now=now)
                continue

            valid, rejected = [], {}
            for opt in options:
                reason = validate(opt, travel_date, config, now)
                if reason:
                    rejected[reason] = rejected.get(reason, 0) + 1
                else:
                    valid.append(opt)
            if rejected:
                log.info("%s %s: rejected %d option(s): %s", provider.name, travel_date,
                         sum(rejected.values()), "; ".join(f"{k} x{v}" for k, v in list(rejected.items())[:6]))
            valid.sort(key=lambda o: (o.price, o.duration_minutes))

            # Verify the cheapest options: confirm the final payable fare before trusting it.
            verified = []
            for i, opt in enumerate(valid):
                if i < provider.verify_budget:
                    opt.extra["search_price"] = opt.price
                    checked = provider.verify(opt)
                    if checked is None:
                        log.info("%s %s %s: no longer available at verification", provider.name, travel_date,
                                 opt.flights_label)
                        continue
                    reason = validate(checked, travel_date, config, now)
                    if reason:
                        log.info("Verified option rejected (%s): %s", checked.flights_label, reason)
                        continue
                    opt = checked
                verified.append(opt)
            verified.sort(key=lambda o: (o.price, -int(o.verification)))

            keep = verified[: config.preferences.store_top_n]
            if verified:
                best_value = min(verified, key=lambda o: value_score(o.price, o.duration_minutes, o.stops, config))
                if best_value not in keep:
                    keep.append(best_value)
            status = "ok" if options else "empty"
            search_id = db.record_search(run_id, provider.name, travel_date.isoformat(), status,
                                         result_count=len(options), meta=meta, now=now)
            for opt in keep:
                db.insert_observation(run_id, search_id, opt,
                                      value_score(opt.price, opt.duration_minutes, opt.stops, config))
            db.update_search_kept(search_id, len(keep))
            db.commit()
            result.observations += len(keep)
            log.info("%s %s: %d result(s), %d valid, %d stored, cheapest %s", provider.name, travel_date,
                     len(options), len(valid), len(keep), fmt_inr(keep[0].price) if keep else "—")

    if result.failed_searches == result.searches:
        result.status = "failed"
    elif result.failed_searches:
        result.status = "partial"

    _health(db, config, dispatcher, result, now)

    if result.status != "failed":
        alerts, record_lows = decide_alerts(db, run_id, config, now)
        for rl in record_lows:
            db.add_record_low(rl["travel_date"], rl["observation_id"], rl["price"], rl["previous_low"],
                              int(config.verification.alert_min_status), now)
        overall = None
        for d in dates:
            row = db.run_best(run_id, d.isoformat(), int(config.verification.alert_min_status))
            if row and (overall is None or row["price"] < overall["price"]):
                overall = row_view(row)
        for cand in alerts:
            msg = alert_message(cand, overall, tz)
            delivered = _send(dispatcher, msg)
            if delivered:
                for key in cand.dedup_keys:
                    db.save_alert(key, cand.kind, cand.travel_date, cand.observation["flight_key"],
                                  cand.observation["id"], cand.observation["price"], cand.previous_low,
                                  ",".join(delivered), bool(delivered), msg.subject, msg.text, now)
                result.alerts_sent += 1
            else:
                log.error("Alert could not be delivered on any channel (not marked as sent): %s",
                          msg.subject)

    summary = build_summary(db, config, now)
    _daily_summary(db, config, dispatcher, summary, now)
    write_reports(summary, config)
    db.finish_run(run_id, result.status, "; ".join(result.errors)[:2000], now)
    return result


def _health(db, config, dispatcher, result: RunResult, now: datetime):
    """Operations alerts: N consecutive fully-failed runs, and recovery afterwards."""
    failures = int(db.kv_get("consecutive_failed_runs", 0))
    threshold = config.alerts.health_alert_after_failures
    if result.status == "failed":
        failures += 1
        db.kv_set("consecutive_failed_runs", failures)
        if failures == threshold:
            _send(dispatcher, plain_message(
                f"⚠️ Fare monitor: {failures} consecutive searches failed",
                ["No fare could be retrieved from any source in the last "
                 f"{failures} runs. No prices are reported while this lasts.", "", "Latest errors:",
                 *[f"• {e[:300]}" for e in result.errors[:6]],
                 "", "Check API keys/quotas and the GitHub Actions logs."]))
    else:
        if failures >= threshold:
            _send(dispatcher, plain_message("✅ Fare monitor recovered",
                                            [f"Searches are succeeding again after {failures} failed runs."]))
        db.kv_set("consecutive_failed_runs", 0)


def _daily_summary(db, config, dispatcher, summary, now: datetime, force: bool = False) -> bool:
    local = now.astimezone(config.trip.timezone)
    today = local.date().isoformat()
    if not force and (local.hour < config.alerts.daily_summary_hour or db.kv_get("daily_summary_date") == today):
        return False
    msg = summary_message(summary, config.trip.timezone, config.extra.get("dashboard_url", ""))
    delivered = _send(dispatcher, msg)
    if delivered or force:
        db.kv_set("daily_summary_date", today)
    return bool(delivered)
