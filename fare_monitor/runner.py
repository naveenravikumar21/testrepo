"""One monitoring cycle over all fare watches:
load watches -> decide which are due -> check the search budget -> search each route/date once ->
confirm only prices that could matter -> store -> alert per watch -> update status -> report."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from . import messages as M
from .analysis import decide_watch_alerts, needs_confirmation, row_view, validate, value_score
from .config import Config
from .db import Database
from .http import build_session
from .models import fmt_inr
from .providers import ProviderError, build_providers
from .report import build_summary, watch_infos, write_reports
from .watches import (WEEKDAYS, Watch, is_trusted_author, is_watch_issue, local_watch, watch_from_issue,
                      watch_from_row)

log = logging.getLogger(__name__)


class ConfigError(Exception):
    pass


@dataclass
class RunResult:
    status: str                      # ok | partial | failed | idle | skipped
    run_id: int | None = None
    watches_checked: int = 0
    searches: int = 0
    failed_searches: int = 0
    observations: int = 0
    alerts_sent: int = 0
    budget_skipped: int = 0
    api_calls: int = 0
    errors: list[str] = field(default_factory=list)


def next_check_label(watch: Watch, today: date) -> str:
    for i in range(1, 8):
        day = today + timedelta(days=i)
        if day.weekday() in watch.days and watch.active_dates(day):
            return f"{WEEKDAYS[day.weekday()][:3]} {day.strftime('%d %b')}, about 7 AM IST"
    return "none (travel dates have passed)"


class Monitor:
    def __init__(self, config: Config, db: Database, session=None, github=None, dispatcher=None, providers=None,
                 now: datetime | None = None):
        self.config = config
        self.db = db
        self.session = session or build_session()
        self.github = github
        self.dispatcher = dispatcher
        self.now = now or datetime.now(timezone.utc)
        self.today = self.now.astimezone(config.tz).date()
        self.month = self.now.astimezone(config.tz).strftime("%Y-%m")
        self._providers = providers
        self._quota = None

    # ---- helpers ---------------------------------------------------------------------------------
    def send(self, message, watch=None) -> list[str]:
        if self.dispatcher is None:
            return []
        return self.dispatcher.send(message, watch)

    def load_watches(self) -> list[Watch]:
        if self.github is None:
            w = local_watch(self.config)
            return [w] if w else []
        watches = []
        for issue in self.github.open_issues():
            if not is_watch_issue(issue, self.config):
                continue
            if not is_trusted_author(issue, self.config):
                log.info("Ignoring watch issue #%s by %s (not a repository owner/collaborator)",
                         issue["number"], (issue.get("user") or {}).get("login"))
                continue
            watches.append(watch_from_issue(issue, self.config, self.today))
        open_ids = {w.id for w in watches}
        for row in self.db.watches(active_only=True):
            if row["id"] not in open_ids and row["id"] != "local":
                log.info("Watch #%s was closed; it will no longer be checked", row["id"])
                self.db.set_watch_field(row["id"], "active", 0)
        return watches

    def budget_available(self, providers) -> tuple[int | None, str]:
        """Searches we may still spend this month on metered providers, and a human-readable usage line."""
        metered = [p for p in providers if p.counts_against_budget]
        if not metered:
            return None, ""
        b = self.config.budget
        p = metered[0]
        own_used = self.db.usage(self.month, p.name)
        quota = p.remaining_quota()
        self._quota = quota if quota and quota.get("left") is not None else None
        if self._quota:
            available = int(quota["left"]) - b.safety_margin
        else:
            available = b.monthly_search_limit - b.safety_margin - own_used
        return max(0, available), self.usage_line(providers)

    def usage_line(self, providers) -> str:
        metered = [p for p in providers if p.counts_against_budget]
        if not metered:
            return ""
        p, b = metered[0], self.config.budget
        q = self._quota
        if q:
            used = (q["used"] if q.get("used") is not None else self.db.usage(self.month, p.name)) + p.calls
            return (f"Searches used this month: **{used}** · about {max(0, int(q['left']) - p.calls)} left "
                    f"(from SerpApi).")
        return (f"Searches used this month: **{self.db.usage(self.month, p.name)} of {b.monthly_search_limit}** "
                "(counted by the monitor).")

    # ---- main ------------------------------------------------------------------------------------
    def run(self, trigger: str = "schedule", force_ids: set[str] | None = None, check_all: bool = False) -> RunResult:
        cfg, db = self.config, self.db
        force_ids = set(force_ids or ())
        if not cfg.monitor_enabled:
            log.info("MONITOR_ENABLED is false: monitoring paused")
            return RunResult(status="skipped")

        run_id = db.start_run(self.now, trigger)
        result = RunResult(status="ok", run_id=run_id)
        if self.github is not None:
            self.github.ensure_label(cfg.github.watch_label)

        due: list[Watch] = []
        fresh_watches: set[str] = set()
        for w in self.load_watches():
            if w.errors:
                key = f"watch_error|{w.id}|{w.body_hash}"
                if db.kv_get(key) is None and self.send(M.watch_errors(w), w):
                    db.kv_set(key, self.now.isoformat())
                continue
            existing = db.get_watch(w.id)
            if not w.active_dates(self.today):
                if existing is None:                 # a brand-new watch with only past dates
                    w.errors = ["All travel dates are in the past."]
                    key = f"watch_error|{w.id}|{w.body_hash}"
                    if db.kv_get(key) is None and self.send(M.watch_errors(w), w):
                        db.kv_set(key, self.now.isoformat())
                elif existing["active"]:
                    db.upsert_watch(w, self.now)
                    self.send(M.watch_finished(w, watch_infos(db, cfg, w, None), cfg.tz), w)
                    if self.github is not None and w.issue_number:
                        self.github.close_issue(w.issue_number)
                    db.set_watch_field(w.id, "active", 0)
                continue
            new = existing is None
            changed = not new and existing["body_hash"] != w.body_hash
            db.upsert_watch(w, self.now)
            if new or changed:
                fresh_watches.add(w.id)
                if self.github is not None and w.issue_number:
                    self.github.add_label(w.issue_number, cfg.github.watch_label)
            last_checked = None if (new or changed) else existing["last_checked"]
            if (w.id in fresh_watches or w.id in force_ids or check_all
                    or (w.is_due(self.today) and last_checked != self.today.isoformat())):
                due.append(w)

        # Acknowledge new/edited watches (this comment becomes the quietly-updated status comment).
        providers = self._providers if self._providers is not None else build_providers(cfg, self.session)
        total_estimate = sum(self._estimate(r) for r in db.watches(active_only=True))
        for w in due:
            if w.id in fresh_watches:
                updated = db.kv_get(f"first_results|{w.id}") is not None
                msg = M.watch_ack(w, w.estimate_searches_30d(self.today), total_estimate,
                                  cfg.budget.monthly_search_limit, updated)
                if self.github is not None and w.issue_number:
                    cid = self.github.comment(w.issue_number, msg.body)
                    if not updated:
                        db.set_watch_field(w.id, "status_comment_id", cid)
                else:
                    self.send(msg, w)

        if not due:
            log.info("No watch is due today")
            db.finish_run(run_id, "idle", "", self.now)
            write_reports(build_summary(db, cfg, self.now), cfg)
            result.status = "idle"
            return result
        if not providers:
            raise ConfigError("No fare source is configured. Add the SERPAPI_API_KEY secret (see README); "
                              "the monitor never invents prices.")

        # ---- plan searches: each route/date once, even if several watches want it ----------------
        plan: dict[tuple, dict] = {}
        for w in due:
            for d in w.active_dates(self.today):
                p = plan.setdefault((w.origin, w.destination, d), {"max_stops": 0, "targets": [], "stops": set()})
                p["max_stops"] = max(p["max_stops"], w.max_stops)
                p["stops"].add(w.max_stops)
                if w.target:
                    p["targets"].append(w.target)
        keys = sorted(plan, key=lambda k: (k[2], k[0], k[1]))      # soonest travel date first

        available, usage_line = self.budget_available(providers)
        if available is not None and available < len(keys):
            skipped = keys[available:]
            keys = keys[:available]
            result.budget_skipped = len(skipped)
            log.warning("Search budget: only %d of %d route/date searches fit this month", len(keys), len(plan))
            if not keys:
                for w in due:
                    key = f"budget|{w.id}|{self.month}"
                    if not db.alert_exists(key) and self.send(M.budget_paused(
                            db.usage(self.month, providers[0].name), cfg.budget.monthly_search_limit), w):
                        db.save_alert(key, w.id, "budget", None, None, None, None, None, "", "budget", self.now)
        spare = None if available is None else available - len(keys)

        min_ver = cfg.verification.alert_min_status
        searched: dict[tuple, bool] = {}
        for key in keys:
            origin, dest, d = key
            info = plan[key]
            ok_any = False
            for provider in providers:
                result.searches += 1
                try:
                    options, meta = provider.search(origin, dest, d, info["max_stops"])
                except ProviderError as exc:
                    self._search_failed(result, provider, key, str(exc))
                    continue
                except Exception as exc:  # unexpected payload or bug: never crash the whole run
                    log.exception("Unexpected error in %s %s", provider.name, key)
                    self._search_failed(result, provider, key, f"{type(exc).__name__}: {exc}")
                    continue
                ok_any = True
                valid = []
                rejected: dict[str, int] = {}
                for opt in options:
                    reason = validate(opt, d, cfg, self.now)
                    if reason:
                        rejected[reason] = rejected.get(reason, 0) + 1
                    else:
                        valid.append(opt)
                if rejected:
                    log.info("%s %s: rejected %s", provider.name, key,
                             "; ".join(f"{k} x{v}" for k, v in list(rejected.items())[:6]))
                valid.sort(key=lambda o: (o.price, o.duration_minutes))

                # Confirm the cheapest option for each stops-limit a watch uses, only if it could matter.
                confirmed = 0
                for max_stops in sorted(info["stops"]):
                    if confirmed >= provider.verify_budget:
                        break
                    if provider.counts_against_budget and spare is not None and spare <= 0:
                        log.info("No spare searches left for price confirmation")
                        break
                    cands = [o for o in valid if o.stops <= max_stops]
                    for cand in cands[:2]:
                        prev = db.previous_low(origin, dest, d.isoformat(), int(min_ver), max_stops, run_id)
                        # Compare search price with search price: seller quotes are often a little higher
                        # than the search price, so comparing against the quote would re-confirm every time.
                        ref = None if prev is None else (prev["search_price"] or prev["price"])
                        why = needs_confirmation(cand, ref, info["targets"], db, cfg, self.now)
                        if not why:
                            break
                        log.info("Confirming %s %s %s (%s)", cand.flights_label, d, fmt_inr(cand.price), why)
                        cand.extra["search_price"] = cand.price
                        checked = provider.verify(cand)
                        confirmed += 1
                        if provider.counts_against_budget and spare is not None:
                            spare -= 1
                        if checked is None:
                            valid.remove(cand)
                            continue                 # gone: try the next cheapest
                        if validate(checked, d, cfg, self.now):
                            valid.remove(cand)
                            continue
                        break
                valid.sort(key=lambda o: (o.price, -int(o.verification)))

                keep = valid[: cfg.preferences.store_top_n]
                keep += [o for o in valid[cfg.preferences.store_top_n:] if o.verification >= min_ver]
                if valid:
                    bv = min(valid, key=lambda o: value_score(o.price, o.duration_minutes, o.stops, cfg))
                    if bv not in keep:
                        keep.append(bv)
                sid = db.record_search(run_id, provider.name, origin, dest, d.isoformat(),
                                       "ok" if options else "empty", self.now, result_count=len(options), meta=meta)
                for opt in keep:
                    db.insert_observation(run_id, sid, opt, value_score(opt.price, opt.duration_minutes, opt.stops, cfg))
                db.update_search_kept(sid, len(keep))
                db.commit()
                result.observations += len(keep)
                log.info("%s %s→%s %s: %d results, %d valid, %d stored, cheapest %s", provider.name, origin, dest,
                         d, len(options), len(valid), len(keep), fmt_inr(keep[0].price) if keep else "—")
            searched[key] = ok_any

        for p in providers:
            db.add_usage(self.month, p.name, p.calls)
            result.api_calls += p.calls
        usage_line = self.usage_line(providers)

        if result.searches and result.failed_searches == result.searches:
            result.status = "failed"
        elif result.failed_searches or result.budget_skipped:
            result.status = "partial"
        self._health(result, due)

        # ---- record lows (per route/date, any watch) --------------------------------------------
        for key, ok in searched.items():
            if not ok:
                continue
            origin, dest, d = key
            rows = db.run_observations(run_id, origin, dest, d.isoformat(), int(min_ver), cfg.defaults.max_stops)
            if not rows:
                continue
            prev = db.previous_low(origin, dest, d.isoformat(), int(min_ver), cfg.defaults.max_stops, run_id)
            if prev is None or rows[0]["price"] < prev["price"] - 0.005:
                db.add_record_low(origin, dest, d.isoformat(), rows[0]["id"], rows[0]["price"],
                                  prev["price"] if prev else None, self.now)

        # ---- per-watch alerts and status ----------------------------------------------------------
        for w in due:
            dates = [d for d in w.active_dates(self.today) if searched.get((w.origin, w.destination, d))]
            if not dates and not any((w.origin, w.destination, d) in searched for d in w.active_dates(self.today)):
                continue  # nothing searched for this watch (budget)
            result.watches_checked += 1
            infos = watch_infos(db, cfg, w, run_id, errored={d for (o, de, d), ok in searched.items()
                                                             if not ok and o == w.origin and de == w.destination})
            items = decide_watch_alerts(db, run_id, w, dates, cfg, self.now)
            first = db.kv_get(f"first_results|{w.id}") is None
            msg = None
            if first:
                msg = M.first_results(w, infos, cfg.tz)
                if items:
                    alert = M.alert_message(w, items, self._overall(infos), cfg.tz)
                    msg = M.Message(alert.subject, msg.body + "\n\n---\n\n" + alert.body)
            elif items:
                msg = M.alert_message(w, items, self._overall(infos), cfg.tz)
            if msg is not None:
                delivered = self.send(msg, w)
                if delivered:
                    if first:
                        db.kv_set(f"first_results|{w.id}", self.now.isoformat())
                    for it in items:
                        for k in it.dedup_keys:
                            db.save_alert(k, w.id, it.kind, it.travel_date, it.observation["flight_key"],
                                          it.observation["id"], it.observation["price"], it.previous_low,
                                          ",".join(delivered), msg.subject, self.now)
                    result.alerts_sent += len(items)
                else:
                    log.error("Message for watch %s could not be delivered (not marked as sent)", w.id)
            self._update_status(w, infos, usage_line)
            db.set_watch_field(w.id, "last_checked", self.today.isoformat())

        db.finish_run(run_id, result.status, "; ".join(result.errors)[:2000], self.now)
        write_reports(build_summary(db, cfg, self.now, usage_line=usage_line), cfg)
        return result

    # ---- internals -------------------------------------------------------------------------------
    def _estimate(self, row) -> int:
        return watch_from_row(row).estimate_searches_30d(self.today)

    def _search_failed(self, result: RunResult, provider, key, error: str):
        origin, dest, d = key
        result.failed_searches += 1
        result.errors.append(f"{provider.name} {origin}-{dest} {d}: {error}")
        log.error("Search failed: %s %s-%s %s: %s", provider.name, origin, dest, d, error)
        self.db.record_search(result.run_id, provider.name, origin, dest, d.isoformat(), "error", self.now,
                              error=error[:1000])

    @staticmethod
    def _overall(infos: list[dict]) -> dict | None:
        confirmed = [i["confirmed"] for i in infos if i.get("confirmed")]
        return min(confirmed, key=lambda o: o["price"]) if confirmed else None

    def _health(self, result: RunResult, due: list[Watch]):
        failures = int(self.db.kv_get("consecutive_failed_runs", 0))
        threshold = self.config.alerts.health_alert_after_failures
        if result.status == "failed":
            failures += 1
            self.db.kv_set("consecutive_failed_runs", failures)
            if failures == threshold:
                for w in due:
                    self.send(M.health_problem(result.errors, failures), w)
        elif result.searches:
            if failures >= threshold:
                for w in due:
                    self.send(M.health_recovered(failures), w)
            self.db.kv_set("consecutive_failed_runs", 0)

    def _update_status(self, w: Watch, infos: list[dict], usage_line: str):
        if self.github is None or not w.issue_number:
            return
        body = M.status_comment(w, infos, usage_line, next_check_label(w, self.today), self.config.tz,
                                self.now.isoformat(), self.config.dashboard_url)
        body += "\n\n<details><summary>Watch settings</summary>\n\n" + "\n".join(
            f"- {l}" for l in w.summary_lines()) + "\n\n</details>"
        row = self.db.get_watch(w.id)
        try:
            if row and row["status_comment_id"]:
                self.github.update_comment(row["status_comment_id"], body)
            else:
                self.db.set_watch_field(w.id, "status_comment_id", self.github.comment(w.issue_number, body))
        except Exception as exc:
            log.warning("Could not update the status comment for #%s: %s", w.id, exc)
