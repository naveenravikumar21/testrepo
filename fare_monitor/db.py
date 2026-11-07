"""SQLite persistence. Every successful observation is stored with its source and timestamp."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import FareOption

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',     -- running | ok | partial | failed | skipped
    notes TEXT
);
CREATE TABLE IF NOT EXISTS searches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    provider TEXT NOT NULL,
    travel_date TEXT NOT NULL,
    started_at TEXT NOT NULL,
    status TEXT NOT NULL,                       -- ok | empty | error
    error TEXT,
    result_count INTEGER NOT NULL DEFAULT 0,
    kept_count INTEGER NOT NULL DEFAULT 0,
    meta_json TEXT
);
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    search_id INTEGER NOT NULL REFERENCES searches(id),
    provider TEXT NOT NULL,
    source TEXT NOT NULL,
    travel_date TEXT NOT NULL,
    flight_key TEXT NOT NULL,
    airline TEXT NOT NULL,
    flight_numbers TEXT NOT NULL,
    departure_local TEXT NOT NULL,
    arrival_local TEXT NOT NULL,
    duration_minutes INTEGER NOT NULL,
    stops INTEGER NOT NULL,
    layovers_json TEXT NOT NULL,
    price REAL NOT NULL,
    currency TEXT NOT NULL,
    verification INTEGER NOT NULL,              -- 1 indicative, 2 seller_quoted, 3 verified
    verification_note TEXT,
    seller TEXT,
    cabin_baggage TEXT,
    checked_baggage TEXT,
    booking_url TEXT,
    booking_request_json TEXT,
    offer_ref TEXT,
    expires_at TEXT,
    value_score REAL,
    search_price REAL,
    extra_json TEXT,
    observed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_obs_date_ver ON observations(travel_date, verification, price);
CREATE INDEX IF NOT EXISTS idx_obs_run ON observations(run_id);
CREATE INDEX IF NOT EXISTS idx_obs_flight ON observations(flight_key, observed_at);
CREATE TABLE IF NOT EXISTS record_lows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    travel_date TEXT NOT NULL,
    observation_id INTEGER NOT NULL REFERENCES observations(id),
    price REAL NOT NULL,
    previous_low REAL,
    min_verification INTEGER NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedup_key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    travel_date TEXT,
    flight_key TEXT,
    observation_id INTEGER,
    price REAL,
    previous_low REAL,
    channels TEXT,
    delivered INTEGER NOT NULL DEFAULT 0,
    sent_at TEXT NOT NULL,
    subject TEXT,
    body TEXT
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.commit()
        self.conn.close()

    # ---- runs & searches -------------------------------------------------------------------------
    def start_run(self, now: datetime | None = None) -> int:
        cur = self.conn.execute("INSERT INTO runs(started_at) VALUES (?)", (iso(now or utcnow()),))
        self.conn.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, status: str, notes: str = "", now: datetime | None = None):
        self.conn.execute("UPDATE runs SET finished_at=?, status=?, notes=? WHERE id=?",
                          (iso(now or utcnow()), status, notes, run_id))
        self.conn.commit()

    def record_search(self, run_id, provider, travel_date, status, error=None, result_count=0,
                      kept_count=0, meta=None, now: datetime | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO searches(run_id, provider, travel_date, started_at, status, error, result_count, "
            "kept_count, meta_json) VALUES (?,?,?,?,?,?,?,?,?)",
            (run_id, provider, travel_date, iso(now or utcnow()), status, error, result_count, kept_count,
             json.dumps(meta, default=str) if meta else None))
        self.conn.commit()
        return cur.lastrowid

    def update_search_kept(self, search_id: int, kept: int):
        self.conn.execute("UPDATE searches SET kept_count=? WHERE id=?", (kept, search_id))

    # ---- observations ----------------------------------------------------------------------------
    def insert_observation(self, run_id: int, search_id: int, opt: FareOption, value_score: float | None) -> int:
        cur = self.conn.execute(
            """INSERT INTO observations(run_id, search_id, provider, source, travel_date, flight_key, airline,
               flight_numbers, departure_local, arrival_local, duration_minutes, stops, layovers_json, price,
               currency, verification, verification_note, seller, cabin_baggage, checked_baggage, booking_url,
               booking_request_json, offer_ref, expires_at, value_score, search_price, extra_json, observed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, search_id, opt.provider, opt.source, opt.travel_date, opt.flight_key, opt.airline,
             json.dumps(opt.flight_numbers), opt.departure_local, opt.arrival_local, opt.duration_minutes,
             opt.stops, opt.layovers_json(), opt.price, opt.currency, int(opt.verification),
             opt.verification_note, opt.seller, opt.cabin_baggage, opt.checked_baggage, opt.booking_url,
             json.dumps(opt.booking_request) if opt.booking_request else None, opt.offer_ref[:512],
             opt.expires_at, value_score, opt.extra.get("search_price"),
             json.dumps({k: v for k, v in opt.extra.items() if k != "search_price"}, default=str),
             opt.observed_at))
        return cur.lastrowid

    def commit(self):
        self.conn.commit()

    def previous_low(self, travel_date: str, min_verification: int, before_run_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            """SELECT * FROM observations WHERE travel_date=? AND verification>=? AND run_id<?
               ORDER BY price ASC, observed_at ASC LIMIT 1""",
            (travel_date, min_verification, before_run_id)).fetchone()

    def run_best(self, run_id: int, travel_date: str, min_verification: int) -> sqlite3.Row | None:
        return self.conn.execute(
            """SELECT * FROM observations WHERE run_id=? AND travel_date=? AND verification>=?
               ORDER BY price ASC, verification DESC, duration_minutes ASC LIMIT 1""",
            (run_id, travel_date, min_verification)).fetchone()

    def run_observations(self, run_id: int, travel_date: str | None = None, min_verification: int = 1):
        sql = "SELECT * FROM observations WHERE run_id=? AND verification>=?"
        args: list = [run_id, min_verification]
        if travel_date:
            sql += " AND travel_date=?"
            args.append(travel_date)
        return self.conn.execute(sql + " ORDER BY price ASC, verification DESC", args).fetchall()

    def flight_last_seen_before(self, flight_key: str, before_run_id: int) -> str | None:
        row = self.conn.execute(
            "SELECT MAX(r.started_at) AS t FROM observations o JOIN runs r ON r.id=o.run_id "
            "WHERE o.flight_key=? AND o.run_id<?", (flight_key, before_run_id)).fetchone()
        return row["t"] if row else None

    def date_had_successful_search_before(self, travel_date: str, before_run_id: int) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM searches WHERE travel_date=? AND status IN ('ok','empty') AND run_id<? LIMIT 1",
            (travel_date, before_run_id)).fetchone()
        return row is not None

    def hourly_lows(self, travel_date: str, min_verification: int, since: datetime | None = None):
        """Lowest eligible fare per run for a date: the historical trend."""
        sql = """SELECT r.id AS run_id, r.started_at, MIN(o.price) AS price
                 FROM observations o JOIN runs r ON r.id=o.run_id
                 WHERE o.travel_date=? AND o.verification>=?"""
        args: list = [travel_date, min_verification]
        if since:
            sql += " AND r.started_at>=?"
            args.append(iso(since))
        return self.conn.execute(sql + " GROUP BY r.id ORDER BY r.started_at", args).fetchall()

    def add_record_low(self, travel_date, observation_id, price, previous_low, min_verification, now=None):
        self.conn.execute(
            "INSERT INTO record_lows(travel_date, observation_id, price, previous_low, min_verification, recorded_at)"
            " VALUES (?,?,?,?,?,?)", (travel_date, observation_id, price, previous_low, min_verification,
                                      iso(now or utcnow())))
        self.conn.commit()

    def record_lows(self, travel_date: str | None = None):
        sql = ("SELECT rl.*, o.airline, o.flight_numbers, o.provider, o.verification FROM record_lows rl "
               "JOIN observations o ON o.id=rl.observation_id")
        args = []
        if travel_date:
            sql += " WHERE rl.travel_date=?"
            args.append(travel_date)
        return self.conn.execute(sql + " ORDER BY rl.recorded_at", args).fetchall()

    # ---- alerts ----------------------------------------------------------------------------------
    def alert_exists(self, dedup_key: str) -> bool:
        return self.conn.execute("SELECT 1 FROM alerts WHERE dedup_key=?", (dedup_key,)).fetchone() is not None

    def save_alert(self, dedup_key, kind, travel_date, flight_key, observation_id, price, previous_low,
                   channels, delivered, subject, body, now=None):
        self.conn.execute(
            """INSERT OR IGNORE INTO alerts(dedup_key, kind, travel_date, flight_key, observation_id, price,
               previous_low, channels, delivered, sent_at, subject, body) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (dedup_key, kind, travel_date, flight_key, observation_id, price, previous_low, channels,
             int(delivered), iso(now or utcnow()), subject, body))
        self.conn.commit()

    def recent_alerts(self, limit: int = 20):
        return self.conn.execute("SELECT * FROM alerts ORDER BY sent_at DESC, id DESC LIMIT ?", (limit,)).fetchall()

    # ---- status for reports ----------------------------------------------------------------------
    def last_run(self, statuses=("ok", "partial")) -> sqlite3.Row | None:
        q = ",".join("?" * len(statuses))
        return self.conn.execute(f"SELECT * FROM runs WHERE status IN ({q}) ORDER BY id DESC LIMIT 1",
                                 statuses).fetchone()

    def recent_runs(self, limit: int = 50):
        return self.conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def provider_status(self):
        """Last search and last successful search for each (provider, date)."""
        return self.conn.execute(
            """SELECT s.provider, s.travel_date, s.status, s.error, s.started_at, s.result_count, s.kept_count,
                      (SELECT MAX(started_at) FROM searches s2 WHERE s2.provider=s.provider
                         AND s2.travel_date=s.travel_date AND s2.status IN ('ok','empty')) AS last_success
               FROM searches s
               WHERE s.id IN (SELECT MAX(id) FROM searches GROUP BY provider, travel_date)
               ORDER BY s.provider, s.travel_date""").fetchall()

    def latest_search_meta(self, provider: str, travel_date: str):
        row = self.conn.execute(
            "SELECT meta_json, started_at FROM searches WHERE provider=? AND travel_date=? AND status='ok' "
            "ORDER BY id DESC LIMIT 1", (provider, travel_date)).fetchone()
        return (json.loads(row["meta_json"]) if row and row["meta_json"] else None), (row["started_at"] if row else None)

    def latest_observations(self, travel_date: str, stale_after_minutes: int, now: datetime | None = None):
        """Observations from the most recent run that searched this date successfully, if still fresh."""
        now = now or utcnow()
        row = self.conn.execute(
            "SELECT MAX(run_id) AS run_id FROM searches WHERE travel_date=? AND status IN ('ok','empty')",
            (travel_date,)).fetchone()
        if not row or row["run_id"] is None:
            return [], None, True
        rows = self.conn.execute(
            "SELECT * FROM observations WHERE run_id=? AND travel_date=? ORDER BY price ASC, verification DESC",
            (row["run_id"], travel_date)).fetchall()
        run = self.conn.execute("SELECT * FROM runs WHERE id=?", (row["run_id"],)).fetchone()
        started = datetime.fromisoformat(run["started_at"])
        stale = now - started > timedelta(minutes=stale_after_minutes)
        return rows, run, stale

    def all_time_low(self, travel_date: str, min_verification: int):
        return self.conn.execute(
            "SELECT * FROM observations WHERE travel_date=? AND verification>=? ORDER BY price ASC, observed_at ASC "
            "LIMIT 1", (travel_date, min_verification)).fetchone()

    def airline_comparison(self, min_verification: int, since: datetime | None = None):
        sql = """SELECT airline, travel_date, MIN(price) AS min_price, COUNT(*) AS n, MAX(observed_at) AS last_seen
                 FROM observations WHERE verification>=?"""
        args: list = [min_verification]
        if since:
            sql += " AND observed_at>=?"
            args.append(iso(since))
        return self.conn.execute(sql + " GROUP BY airline, travel_date ORDER BY min_price", args).fetchall()

    # ---- key/value state -------------------------------------------------------------------------
    def kv_get(self, key: str, default=None):
        row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def kv_set(self, key: str, value):
        self.conn.execute("INSERT INTO kv(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                          (key, str(value)))
        self.conn.commit()
