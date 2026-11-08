"""SQLite persistence. Every successful observation is stored with its source and timestamp."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import FareOption

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    trigger TEXT,
    status TEXT NOT NULL DEFAULT 'running',     -- running | ok | partial | failed | idle | skipped
    notes TEXT
);
CREATE TABLE IF NOT EXISTS searches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    provider TEXT NOT NULL,
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    travel_date TEXT NOT NULL,
    started_at TEXT NOT NULL,
    status TEXT NOT NULL,                       -- ok | empty | error
    error TEXT,
    result_count INTEGER NOT NULL DEFAULT 0,
    kept_count INTEGER NOT NULL DEFAULT 0,
    meta_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_search_route ON searches(origin, destination, travel_date);
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    search_id INTEGER NOT NULL REFERENCES searches(id),
    provider TEXT NOT NULL,
    source TEXT NOT NULL,
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
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
CREATE INDEX IF NOT EXISTS idx_obs_route ON observations(origin, destination, travel_date, verification, price);
CREATE INDEX IF NOT EXISTS idx_obs_run ON observations(run_id);
CREATE INDEX IF NOT EXISTS idx_obs_flight ON observations(flight_key, observed_at);
CREATE TABLE IF NOT EXISTS record_lows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    travel_date TEXT NOT NULL,
    observation_id INTEGER NOT NULL REFERENCES observations(id),
    price REAL NOT NULL,
    previous_low REAL,
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedup_key TEXT NOT NULL UNIQUE,
    watch_id TEXT,
    kind TEXT NOT NULL,
    travel_date TEXT,
    flight_key TEXT,
    observation_id INTEGER,
    price REAL,
    previous_low REAL,
    channels TEXT,
    sent_at TEXT NOT NULL,
    subject TEXT
);
CREATE TABLE IF NOT EXISTS watches (
    id TEXT PRIMARY KEY,
    title TEXT, url TEXT, origin TEXT, destination TEXT, dates_json TEXT, days_json TEXT,
    frequency TEXT, target REAL, max_stops INTEGER, airlines_json TEXT, active INTEGER NOT NULL DEFAULT 1,
    body_hash TEXT, created_at TEXT, updated_at TEXT, last_checked TEXT, status_comment_id INTEGER
);
CREATE TABLE IF NOT EXISTS api_usage (
    month TEXT NOT NULL, provider TEXT NOT NULL, calls INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (month, provider)
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""


def carrier_clause(carriers) -> tuple[str, list]:
    """SQL that keeps only itineraries where EVERY leg is flown by one of `carriers` (IATA codes)."""
    if not carriers:
        return "", []
    q = ",".join("?" * len(carriers))
    return (f" AND NOT EXISTS (SELECT 1 FROM json_each(flight_numbers) "
            f"WHERE substr(json_each.value, 1, instr(json_each.value, ' ') - 1) NOT IN ({q}))", list(carriers))


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
        self._migrate()
        self.conn.commit()

    def _migrate(self):
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(watches)")}
        if "airlines_json" not in cols:
            self.conn.execute("ALTER TABLE watches ADD COLUMN airlines_json TEXT")

    def close(self):
        self.conn.commit()
        self.conn.close()

    def commit(self):
        self.conn.commit()

    # ---- runs & searches -------------------------------------------------------------------------
    def start_run(self, now: datetime, trigger: str = "") -> int:
        cur = self.conn.execute("INSERT INTO runs(started_at, trigger) VALUES (?,?)", (iso(now), trigger))
        self.conn.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, status: str, notes: str, now: datetime):
        self.conn.execute("UPDATE runs SET finished_at=?, status=?, notes=? WHERE id=?",
                          (iso(now), status, notes, run_id))
        self.conn.commit()

    def record_search(self, run_id, provider, origin, destination, travel_date, status, now, error=None,
                      result_count=0, meta=None) -> int:
        cur = self.conn.execute(
            "INSERT INTO searches(run_id, provider, origin, destination, travel_date, started_at, status, error, "
            "result_count, meta_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (run_id, provider, origin, destination, travel_date, iso(now), status, error, result_count,
             json.dumps(meta, default=str) if meta else None))
        self.conn.commit()
        return cur.lastrowid

    def update_search_kept(self, search_id: int, kept: int):
        self.conn.execute("UPDATE searches SET kept_count=? WHERE id=?", (kept, search_id))

    # ---- observations ----------------------------------------------------------------------------
    def insert_observation(self, run_id: int, search_id: int, opt: FareOption, value_score: float | None) -> int:
        cur = self.conn.execute(
            """INSERT INTO observations(run_id, search_id, provider, source, origin, destination, travel_date,
               flight_key, airline, flight_numbers, departure_local, arrival_local, duration_minutes, stops,
               layovers_json, price, currency, verification, verification_note, seller, cabin_baggage,
               checked_baggage, booking_url, booking_request_json, offer_ref, expires_at, value_score,
               search_price, extra_json, observed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, search_id, opt.provider, opt.source, opt.origin, opt.destination, opt.travel_date,
             opt.flight_key, opt.airline, json.dumps(opt.flight_numbers), opt.departure_local, opt.arrival_local,
             opt.duration_minutes, opt.stops, opt.layovers_json(), opt.price, opt.currency, int(opt.verification),
             opt.verification_note, opt.seller, opt.cabin_baggage, opt.checked_baggage, opt.booking_url,
             json.dumps(opt.booking_request) if opt.booking_request else None, opt.offer_ref[:512],
             opt.expires_at, value_score, opt.extra.get("search_price"),
             json.dumps({k: v for k, v in opt.extra.items() if k != "search_price"}, default=str),
             opt.observed_at))
        return cur.lastrowid

    def previous_low(self, origin, destination, travel_date, min_ver, max_stops, before_run_id, carriers=None):
        cc, ca = carrier_clause(carriers)
        return self.conn.execute(
            f"""SELECT * FROM observations WHERE origin=? AND destination=? AND travel_date=? AND verification>=?
               AND stops<=? AND run_id<?{cc} ORDER BY price ASC, observed_at ASC LIMIT 1""",
            (origin, destination, travel_date, min_ver, max_stops, before_run_id, *ca)).fetchone()

    def run_observations(self, run_id, origin, destination, travel_date, min_ver=1, max_stops=9, carriers=None):
        cc, ca = carrier_clause(carriers)
        return self.conn.execute(
            f"""SELECT * FROM observations WHERE run_id=? AND origin=? AND destination=? AND travel_date=?
               AND verification>=? AND stops<=?{cc} ORDER BY price ASC, verification DESC, duration_minutes ASC""",
            (run_id, origin, destination, travel_date, min_ver, max_stops, *ca)).fetchall()

    def recent_confirmation(self, flight_key: str, min_ver: int, since: datetime):
        return self.conn.execute(
            """SELECT * FROM observations WHERE flight_key=? AND verification>=? AND observed_at>=?
               ORDER BY observed_at DESC LIMIT 1""", (flight_key, min_ver, iso(since))).fetchone()

    def flight_last_seen_before(self, flight_key: str, before_run_id: int) -> str | None:
        row = self.conn.execute(
            "SELECT MAX(r.started_at) AS t FROM observations o JOIN runs r ON r.id=o.run_id "
            "WHERE o.flight_key=? AND o.run_id<?", (flight_key, before_run_id)).fetchone()
        return row["t"] if row else None

    def route_had_search_before(self, origin, destination, travel_date, before_run_id) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM searches WHERE origin=? AND destination=? AND travel_date=? AND status IN ('ok','empty')"
            " AND run_id<? LIMIT 1", (origin, destination, travel_date, before_run_id)).fetchone() is not None

    def lows_per_run(self, origin, destination, travel_date, min_ver=1, max_stops=9, carriers=None):
        """Lowest fare per check for a route/date: the historical trend."""
        cc, ca = carrier_clause(carriers)
        return self.conn.execute(
            f"""SELECT r.id AS run_id, r.started_at, MIN(o.price) AS price, MAX(o.verification) AS verification
               FROM observations o JOIN runs r ON r.id=o.run_id
               WHERE o.origin=? AND o.destination=? AND o.travel_date=? AND o.verification>=? AND o.stops<=?{cc}
               GROUP BY r.id ORDER BY r.started_at""",
            (origin, destination, travel_date, min_ver, max_stops, *ca)).fetchall()

    def add_record_low(self, origin, destination, travel_date, observation_id, price, previous_low, now):
        self.conn.execute(
            "INSERT INTO record_lows(origin, destination, travel_date, observation_id, price, previous_low, "
            "recorded_at) VALUES (?,?,?,?,?,?,?)",
            (origin, destination, travel_date, observation_id, price, previous_low, iso(now)))
        self.conn.commit()

    def all_time_low(self, origin, destination, travel_date, min_ver, max_stops=9, carriers=None):
        cc, ca = carrier_clause(carriers)
        return self.conn.execute(
            f"""SELECT * FROM observations WHERE origin=? AND destination=? AND travel_date=? AND verification>=?
               AND stops<=?{cc} ORDER BY price ASC, observed_at ASC LIMIT 1""",
            (origin, destination, travel_date, min_ver, max_stops, *ca)).fetchone()

    def latest_route_search(self, origin, destination, travel_date):
        """Most recent successful search for a route/date and its observations."""
        s = self.conn.execute(
            """SELECT * FROM searches WHERE origin=? AND destination=? AND travel_date=? AND status IN ('ok','empty')
               ORDER BY id DESC LIMIT 1""", (origin, destination, travel_date)).fetchone()
        if not s:
            return None, []
        rows = self.conn.execute(
            "SELECT * FROM observations WHERE run_id=? AND origin=? AND destination=? AND travel_date=? "
            "ORDER BY price ASC, verification DESC", (s["run_id"], origin, destination, travel_date)).fetchall()
        return s, rows

    def airline_comparison(self, origin, destination, dates: list[str], min_ver: int):
        q = ",".join("?" * len(dates))
        return self.conn.execute(
            f"""SELECT airline, travel_date, MIN(price) AS min_price, COUNT(*) AS n FROM observations
                WHERE origin=? AND destination=? AND travel_date IN ({q}) AND verification>=?
                GROUP BY airline, travel_date ORDER BY min_price""",
            (origin, destination, *dates, min_ver)).fetchall()

    # ---- alerts ----------------------------------------------------------------------------------
    def alert_exists(self, dedup_key: str) -> bool:
        return self.conn.execute("SELECT 1 FROM alerts WHERE dedup_key=?", (dedup_key,)).fetchone() is not None

    def save_alert(self, dedup_key, watch_id, kind, travel_date, flight_key, observation_id, price, previous_low,
                   channels, subject, now):
        self.conn.execute(
            """INSERT OR IGNORE INTO alerts(dedup_key, watch_id, kind, travel_date, flight_key, observation_id,
               price, previous_low, channels, sent_at, subject) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (dedup_key, watch_id, kind, travel_date, flight_key, observation_id, price, previous_low, channels,
             iso(now), subject))
        self.conn.commit()

    def recent_alerts(self, limit: int = 20, watch_id: str | None = None):
        if watch_id:
            return self.conn.execute("SELECT * FROM alerts WHERE watch_id=? ORDER BY sent_at DESC, id DESC LIMIT ?",
                                     (watch_id, limit)).fetchall()
        return self.conn.execute("SELECT * FROM alerts ORDER BY sent_at DESC, id DESC LIMIT ?", (limit,)).fetchall()

    # ---- watches ---------------------------------------------------------------------------------
    def get_watch(self, watch_id: str):
        return self.conn.execute("SELECT * FROM watches WHERE id=?", (watch_id,)).fetchone()

    def upsert_watch(self, w, now: datetime):
        existing = self.get_watch(w.id)
        vals = (w.title, w.url, w.origin, w.destination, json.dumps([d.isoformat() for d in w.dates]),
                json.dumps(w.days), w.frequency, w.target, w.max_stops, json.dumps(w.airlines), w.body_hash, iso(now))
        if existing:
            self.conn.execute(
                """UPDATE watches SET title=?, url=?, origin=?, destination=?, dates_json=?, days_json=?, frequency=?,
                   target=?, max_stops=?, airlines_json=?, body_hash=?, updated_at=?, active=1 WHERE id=?""",
                (*vals, w.id))
        else:
            self.conn.execute(
                """INSERT INTO watches(title, url, origin, destination, dates_json, days_json, frequency, target,
                   max_stops, airlines_json, body_hash, updated_at, created_at, id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (*vals, iso(now), w.id))
        self.conn.commit()

    def set_watch_field(self, watch_id: str, field: str, value):
        assert field in ("active", "last_checked", "status_comment_id")
        self.conn.execute(f"UPDATE watches SET {field}=? WHERE id=?", (value, watch_id))
        self.conn.commit()

    def watches(self, active_only: bool = False):
        sql = "SELECT * FROM watches" + (" WHERE active=1" if active_only else "") + " ORDER BY CAST(id AS INTEGER)"
        return self.conn.execute(sql).fetchall()

    # ---- search budget ---------------------------------------------------------------------------
    def add_usage(self, month: str, provider: str, calls: int):
        if calls:
            self.conn.execute(
                "INSERT INTO api_usage(month, provider, calls) VALUES (?,?,?) "
                "ON CONFLICT(month, provider) DO UPDATE SET calls=calls+excluded.calls", (month, provider, calls))
            self.conn.commit()

    def usage(self, month: str, provider: str) -> int:
        row = self.conn.execute("SELECT calls FROM api_usage WHERE month=? AND provider=?",
                                (month, provider)).fetchone()
        return row["calls"] if row else 0

    # ---- status ----------------------------------------------------------------------------------
    def last_run(self, statuses=("ok", "partial")):
        q = ",".join("?" * len(statuses))
        return self.conn.execute(f"SELECT * FROM runs WHERE status IN ({q}) ORDER BY id DESC LIMIT 1",
                                 statuses).fetchone()

    def provider_status(self):
        return self.conn.execute(
            """SELECT s.provider, s.origin, s.destination, s.travel_date, s.status, s.error, s.started_at,
                      s.result_count, s.kept_count,
                      (SELECT MAX(started_at) FROM searches s2 WHERE s2.provider=s.provider AND s2.origin=s.origin
                         AND s2.destination=s.destination AND s2.travel_date=s.travel_date
                         AND s2.status IN ('ok','empty')) AS last_success
               FROM searches s
               WHERE s.id IN (SELECT MAX(id) FROM searches GROUP BY provider, origin, destination, travel_date)
               ORDER BY s.travel_date, s.provider""").fetchall()

    def kv_get(self, key: str, default=None):
        row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def kv_set(self, key: str, value):
        self.conn.execute("INSERT INTO kv(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                          (key, str(value)))
        self.conn.commit()
