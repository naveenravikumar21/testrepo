"""Configuration: non-secret settings from config/monitor.toml, secrets and overrides from env."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from datetime import date, time
from pathlib import Path
from zoneinfo import ZoneInfo

from .models import Verification

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "monitor.toml"


def _env(name: str, default=None):
    value = os.environ.get(name)
    return default if value is None or value.strip() == "" else value.strip()


def _csv(value) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [v.strip() for v in str(value).split(",") if v.strip()]


def _hhmm(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


@dataclass
class Trip:
    origin: str
    destination: str
    dates: list[date]
    adults: int
    cabin: str
    currency: str
    timezone: ZoneInfo


@dataclass
class Preferences:
    max_stops: int
    max_duration_minutes: int
    earliest_departure: time
    latest_departure: time
    min_layover_minutes: int
    max_layover_minutes: int
    value_inr_per_hour: float
    value_inr_per_stop: float
    store_top_n: int


@dataclass
class VerificationSettings:
    alert_min_status: Verification
    stale_after_minutes: int
    min_plausible_fare: float
    max_plausible_fare: float


@dataclass
class AlertSettings:
    min_drop_inr: float
    min_drop_pct: float
    target_fare_inr: float
    attractive_below_median_pct: float
    attractive_min_history: int
    competitive_pct: float
    reappear_after_hours: int
    send_baseline: bool
    daily_summary_hour: int
    health_alert_after_failures: int


@dataclass
class Secrets:
    duffel_access_token: str | None
    serpapi_api_key: str | None
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    smtp_host: str | None
    smtp_port: int
    smtp_user: str | None
    smtp_password: str | None
    smtp_use_ssl: bool
    email_from: str | None
    email_to: list[str]


@dataclass
class Config:
    trip: Trip
    preferences: Preferences
    verification: VerificationSettings
    alerts: AlertSettings
    providers_enabled: list[str]
    provider_settings: dict
    notify_channels: list[str]
    secrets: Secrets
    db_path: Path
    report_dir: Path
    monitor_enabled: bool = True
    extra: dict = field(default_factory=dict)


def load_config(path: str | Path | None = None) -> Config:
    path = Path(path or _env("FARE_MONITOR_CONFIG", DEFAULT_CONFIG_PATH))
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)

    t = raw["trip"]
    dates_raw = _csv(_env("TRAVEL_DATES", t["dates"]))
    trip = Trip(
        origin=t["origin"].upper(),
        destination=t["destination"].upper(),
        dates=sorted(date.fromisoformat(d) for d in dates_raw),
        adults=int(t.get("adults", 1)),
        cabin=t.get("cabin", "economy"),
        currency=t.get("currency", "INR").upper(),
        timezone=ZoneInfo(t.get("timezone", "Asia/Kolkata")),
    )

    p = raw.get("preferences", {})
    prefs = Preferences(
        max_stops=int(_env("PREF_MAX_STOPS", p.get("max_stops", 2))),
        max_duration_minutes=int(float(_env("PREF_MAX_DURATION_HOURS", p.get("max_duration_hours", 18))) * 60),
        earliest_departure=_hhmm(p.get("earliest_departure", "00:00")),
        latest_departure=_hhmm(p.get("latest_departure", "23:59")),
        min_layover_minutes=int(p.get("min_layover_minutes", 45)),
        max_layover_minutes=int(p.get("max_layover_minutes", 600)),
        value_inr_per_hour=float(p.get("value_inr_per_hour", 300)),
        value_inr_per_stop=float(p.get("value_inr_per_stop", 750)),
        store_top_n=int(p.get("store_top_n", 20)),
    )

    v = raw.get("verification", {})
    verification = VerificationSettings(
        alert_min_status=Verification.parse(_env("ALERT_MIN_STATUS", v.get("alert_min_status", "seller_quoted"))),
        stale_after_minutes=int(v.get("stale_after_minutes", 120)),
        min_plausible_fare=float(v.get("min_plausible_fare", 1000)),
        max_plausible_fare=float(v.get("max_plausible_fare", 150000)),
    )

    a = raw.get("alerts", {})
    alerts = AlertSettings(
        min_drop_inr=float(a.get("min_drop_inr", 200)),
        min_drop_pct=float(a.get("min_drop_pct", 2.0)),
        target_fare_inr=float(_env("TARGET_FARE_INR", a.get("target_fare_inr", 0))),
        attractive_below_median_pct=float(a.get("attractive_below_median_pct", 15.0)),
        attractive_min_history=int(a.get("attractive_min_history", 24)),
        competitive_pct=float(a.get("competitive_pct", 5.0)),
        reappear_after_hours=int(a.get("reappear_after_hours", 6)),
        send_baseline=bool(a.get("send_baseline", True)),
        daily_summary_hour=int(_env("DAILY_SUMMARY_HOUR", a.get("daily_summary_hour", 8))),
        health_alert_after_failures=int(a.get("health_alert_after_failures", 3)),
    )

    providers_raw = raw.get("providers", {})
    providers_enabled = _csv(_env("PROVIDERS", providers_raw.get("enabled", [])))
    provider_settings = {k: v for k, v in providers_raw.items() if isinstance(v, dict)}

    n = raw.get("notify", {})
    channels = _csv(_env("NOTIFY_CHANNELS", n.get("channels", ["console"])))

    secrets = Secrets(
        duffel_access_token=_env("DUFFEL_ACCESS_TOKEN"),
        serpapi_api_key=_env("SERPAPI_API_KEY"),
        telegram_bot_token=_env("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_env("TELEGRAM_CHAT_ID"),
        smtp_host=_env("SMTP_HOST"),
        smtp_port=int(_env("SMTP_PORT", 587)),
        smtp_user=_env("SMTP_USER"),
        smtp_password=_env("SMTP_PASSWORD"),
        smtp_use_ssl=_env("SMTP_USE_SSL", "false").lower() in ("1", "true", "yes"),
        email_from=_env("EMAIL_FROM"),
        email_to=_csv(_env("EMAIL_TO", "")),
    )

    state_dir = Path(_env("FARE_STATE_DIR", "state"))
    return Config(
        trip=trip,
        preferences=prefs,
        verification=verification,
        alerts=alerts,
        providers_enabled=providers_enabled,
        provider_settings=provider_settings,
        notify_channels=channels,
        secrets=secrets,
        db_path=Path(_env("FARE_DB_PATH", state_dir / "fares.sqlite3")),
        report_dir=Path(_env("FARE_REPORT_DIR", state_dir)),
        monitor_enabled=_env("MONITOR_ENABLED", "true").lower() not in ("0", "false", "no", "off"),
        extra={"dashboard_url": _env("DASHBOARD_URL", "")},
    )
