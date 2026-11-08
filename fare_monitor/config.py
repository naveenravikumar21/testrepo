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
class Defaults:
    origin: str
    destination: str
    adults: int
    cabin: str
    currency: str
    timezone: ZoneInfo
    target_fare_inr: float
    max_stops: int


@dataclass
class Preferences:
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
    reverify_after_hours: int
    min_plausible_fare: float
    max_plausible_fare: float
    stale_after_days: int


@dataclass
class AlertSettings:
    min_drop_inr: float
    min_drop_pct: float
    competitive_pct: float
    reappear_after_days: int
    health_alert_after_failures: int


@dataclass
class BudgetSettings:
    monthly_search_limit: int
    safety_margin: int
    max_dates_per_watch: int


@dataclass
class GitHubSettings:
    token: str | None
    repository: str | None
    watch_title_prefix: str
    watch_label: str
    allowed_author_associations: list[str]

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.repository)


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
    defaults: Defaults
    preferences: Preferences
    verification: VerificationSettings
    alerts: AlertSettings
    budget: BudgetSettings
    github: GitHubSettings
    providers_enabled: list[str]
    provider_settings: dict
    notify_channels: list[str]
    secrets: Secrets
    db_path: Path
    report_dir: Path
    local_dates: list[date] = field(default_factory=list)
    local_target: float = 0
    monitor_enabled: bool = True
    dashboard_url: str = ""

    @property
    def tz(self) -> ZoneInfo:
        return self.defaults.timezone


def load_config(path: str | Path | None = None) -> Config:
    path = Path(path or _env("FARE_MONITOR_CONFIG", DEFAULT_CONFIG_PATH))
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)

    d = raw.get("defaults", {})
    defaults = Defaults(
        origin=d.get("origin", "JAI").upper(),
        destination=d.get("destination", "COK").upper(),
        adults=int(d.get("adults", 1)),
        cabin=d.get("cabin", "economy"),
        currency=d.get("currency", "INR").upper(),
        timezone=ZoneInfo(d.get("timezone", "Asia/Kolkata")),
        target_fare_inr=float(d.get("target_fare_inr", 0)),
        max_stops=int(d.get("max_stops", 2)),
    )

    p = raw.get("preferences", {})
    prefs = Preferences(
        max_duration_minutes=int(float(p.get("max_duration_hours", 18)) * 60),
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
        reverify_after_hours=int(v.get("reverify_after_hours", 72)),
        min_plausible_fare=float(v.get("min_plausible_fare", 1000)),
        max_plausible_fare=float(v.get("max_plausible_fare", 150000)),
        stale_after_days=int(v.get("stale_after_days", 8)),
    )

    a = raw.get("alerts", {})
    alerts = AlertSettings(
        min_drop_inr=float(a.get("min_drop_inr", 200)),
        min_drop_pct=float(a.get("min_drop_pct", 2.0)),
        competitive_pct=float(a.get("competitive_pct", 5.0)),
        reappear_after_days=int(a.get("reappear_after_days", 3)),
        health_alert_after_failures=int(a.get("health_alert_after_failures", 2)),
    )

    b = raw.get("budget", {})
    budget = BudgetSettings(
        monthly_search_limit=int(_env("SERPAPI_MONTHLY_LIMIT", b.get("monthly_search_limit", 250))),
        safety_margin=int(b.get("safety_margin", 20)),
        max_dates_per_watch=int(b.get("max_dates_per_watch", 14)),
    )

    g = raw.get("github", {})
    github = GitHubSettings(
        token=_env("GITHUB_TOKEN"),
        repository=_env("GITHUB_REPOSITORY"),
        watch_title_prefix=g.get("watch_title_prefix", "[Fare watch]"),
        watch_label=g.get("watch_label", "fare-watch"),
        allowed_author_associations=[s.upper() for s in g.get("allowed_author_associations",
                                                                ["OWNER", "MEMBER", "COLLABORATOR"])],
    )

    providers_raw = raw.get("providers", {})
    providers_enabled = _csv(_env("PROVIDERS", providers_raw.get("enabled", [])))
    provider_settings = {k: v for k, v in providers_raw.items() if isinstance(v, dict)}

    n = raw.get("notify", {})
    channels = _csv(_env("NOTIFY_CHANNELS", n.get("channels", ["github"])))

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

    local = raw.get("local", {})
    local_dates = sorted(date.fromisoformat(x) for x in _csv(_env("TRAVEL_DATES", local.get("dates", []))))

    state_dir = Path(_env("FARE_STATE_DIR", "state"))
    return Config(
        defaults=defaults,
        preferences=prefs,
        verification=verification,
        alerts=alerts,
        budget=budget,
        github=github,
        providers_enabled=providers_enabled,
        provider_settings=provider_settings,
        notify_channels=channels,
        secrets=secrets,
        db_path=Path(_env("FARE_DB_PATH", state_dir / "fares.sqlite3")),
        report_dir=Path(_env("FARE_REPORT_DIR", state_dir)),
        local_dates=local_dates,
        local_target=float(_env("TARGET_FARE_INR", local.get("target_fare_inr", 0))),
        monitor_enabled=_env("MONITOR_ENABLED", "true").lower() not in ("0", "false", "no", "off"),
        dashboard_url=_env("DASHBOARD_URL", ""),
    )
