"""Command line entry point: python -m fare_monitor <command>."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone

from .config import load_config
from .db import Database
from .http import build_session
from .messages import plain_message
from .notify import Dispatcher
from .providers import REGISTRY
from .report import build_summary, to_markdown, write_reports
from .runner import ConfigError, _daily_summary, run_once


def _github_output(**values):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            for k, v in values.items():
                fh.write(f"{k}={v}\n")


def _github_summary(markdown: str):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(markdown)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="fare_monitor", description="JAI -> COK flight fare monitor")
    parser.add_argument("--config", help="path to monitor.toml")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command")
    run_p = sub.add_parser("run", help="run one monitoring cycle (default)")
    run_p.add_argument("--dry-run", action="store_true", help="print notifications to the console only")
    sub.add_parser("report", help="regenerate the dashboard and summary files")
    sum_p = sub.add_parser("summary", help="print (and optionally send) the daily summary")
    sum_p.add_argument("--send", action="store_true")
    sub.add_parser("test-notify", help="send a test message on every configured channel")
    sub.add_parser("check-config", help="show which providers/channels are configured (no secret values)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("fare_monitor")
    config = load_config(args.config)
    command = args.command or "run"
    session = build_session()

    if command == "check-config":
        info = {
            "route": f"{config.trip.origin}->{config.trip.destination}",
            "dates": [d.isoformat() for d in config.trip.dates],
            "monitor_enabled": config.monitor_enabled,
            "alert_min_status": config.verification.alert_min_status.name.lower(),
            "providers": {n: ("configured" if REGISTRY[n].is_configured(config) else "MISSING CREDENTIALS")
                          if n in REGISTRY else "UNKNOWN" for n in config.providers_enabled},
            "notify_channels": Dispatcher(config, session).names,
            "db_path": str(config.db_path),
        }
        print(json.dumps(info, indent=2))
        return 0

    if command == "test-notify":
        dispatcher = Dispatcher(config, session)
        if not dispatcher.notifiers:
            log.error("No notification channel is configured")
            return 2
        now = datetime.now(timezone.utc).astimezone(config.trip.timezone).strftime("%d %b %Y %H:%M %Z")
        delivered = dispatcher.send(plain_message("✅ Fare monitor test notification", [
            f"Your JAI → COK fare monitor can reach you on this channel ({now}).",
            "This is only a test; it contains no fare information."]))
        print(f"Delivered via: {', '.join(delivered) or 'nothing'}")
        return 0 if len(delivered) == len(dispatcher.notifiers) else 1

    db = Database(config.db_path)
    try:
        if command == "report":
            paths = write_reports(build_summary(db, config), config)
            print("\n".join(str(p) for p in paths.values()))
            return 0
        if command == "summary":
            summary = build_summary(db, config)
            print(to_markdown(summary, config))
            if args.send:
                _daily_summary(db, config, Dispatcher(config, session), summary, datetime.now(timezone.utc),
                               force=True)
            return 0

        dispatcher = Dispatcher(config, session, ["console"] if getattr(args, "dry_run", False) else None)
        if not dispatcher.notifiers:
            log.warning("No notification channel is configured: fares are still recorded, and alerts are "
                        "are not delivered until a channel works")
            print("::warning::No notification channel configured (check TELEGRAM_*/SMTP_* secrets)")
        try:
            result = run_once(config, db, session=session, dispatcher=dispatcher)
        except ConfigError as exc:
            log.error("%s", exc)
            _github_summary(f"## ❌ Fare monitor not configured\n\n{exc}\n")
            return 2
        log.info("Run finished: status=%s searches=%d failed=%d stored=%d alerts=%d", result.status,
                 result.searches, result.failed_searches, result.observations, result.alerts_sent)
        _github_output(status=result.status, window_closed=str(result.window_closed).lower())
        if result.status not in ("skipped",):
            _github_summary(to_markdown(build_summary(db, config), config))
        if result.status == "failed":
            for e in result.errors:
                print(f"::error::{e}"[:1000])
            return 1
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
