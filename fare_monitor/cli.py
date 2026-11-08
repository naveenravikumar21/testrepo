"""Command line entry point: python -m fare_monitor <command>."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from .config import load_config
from .db import Database
from .github_api import GitHubClient
from .http import build_session
from .messages import Message
from .notify import Dispatcher
from .providers import REGISTRY
from .report import build_summary, to_markdown, write_reports
from .runner import ConfigError, Monitor


def _github_summary(markdown: str):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(markdown)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="fare_monitor", description="Flight fare watch")
    parser.add_argument("--config", help="path to monitor.toml")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command")
    run_p = sub.add_parser("run", help="check every watch that is due (default)")
    run_p.add_argument("--issue", type=int, action="append", default=[], help="also check this watch now")
    run_p.add_argument("--check-now", action="store_true", help="check all active watches now")
    run_p.add_argument("--dry-run", action="store_true", help="print messages to the console instead of posting")
    run_p.add_argument("--trigger", default="manual")
    sub.add_parser("report", help="regenerate the dashboard")
    sub.add_parser("check-config", help="show what is configured (never prints secret values)")
    sub.add_parser("test-notify", help="send a test message on the email/telegram/console channels")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("fare_monitor")
    config = load_config(args.config)
    command = args.command or "run"
    session = build_session()
    github = GitHubClient(config.github.token, config.github.repository, session) if config.github.enabled else None

    if command == "check-config":
        print(json.dumps({
            "github_repository": config.github.repository or "(not set: local mode)",
            "monitor_enabled": config.monitor_enabled,
            "alert_min_status": config.verification.alert_min_status.name.lower(),
            "monthly_search_limit": config.budget.monthly_search_limit,
            "providers": {n: ("configured" if REGISTRY[n].is_configured(config) else "MISSING CREDENTIALS")
                          if n in REGISTRY else "UNKNOWN" for n in config.providers_enabled},
            "notify_channels": Dispatcher(config, session, github=github).names,
            "local_dates": [d.isoformat() for d in config.local_dates],
        }, indent=2))
        return 0

    if command == "test-notify":
        dispatcher = Dispatcher(config, session, [c for c in config.notify_channels if c != "github"] or ["console"])
        delivered = dispatcher.send(Message("✅ Fare monitor test notification",
                                            "This is a test message from your flight fare monitor."))
        print(f"Delivered via: {', '.join(delivered) or 'nothing'}")
        return 0 if delivered else 1

    db = Database(config.db_path)
    try:
        if command == "report":
            paths = write_reports(build_summary(db, config), config)
            print("\n".join(str(p) for p in paths.values()))
            return 0

        channels = ["console"] if args.dry_run else None
        dispatcher = Dispatcher(config, session, channels, github=None if args.dry_run else github)
        if not dispatcher.notifiers:
            print("::warning::No notification channel is available; prices are recorded but nobody is told.")
        monitor = Monitor(config, db, session=session, github=github, dispatcher=dispatcher)
        if args.dry_run and github is not None:
            # Read watches from GitHub but never write to it during a dry run.
            monitor.github = _ReadOnlyGitHub(github)
        try:
            result = monitor.run(trigger=args.trigger, force_ids={str(i) for i in args.issue},
                                 check_all=args.check_now)
        except ConfigError as exc:
            log.error("%s", exc)
            _github_summary(f"## ❌ Fare monitor not configured\n\n{exc}\n")
            return 2
        log.info("Run finished: status=%s watches=%d searches=%d failed=%d api_calls=%d stored=%d alerts=%d "
                 "budget_skipped=%d", result.status, result.watches_checked, result.searches,
                 result.failed_searches, result.api_calls, result.observations, result.alerts_sent,
                 result.budget_skipped)
        _github_summary(to_markdown(build_summary(db, config), config))
        if result.status == "failed":
            for e in result.errors:
                print(f"::error::{e}"[:1000])
            return 1
        return 0
    finally:
        db.close()


class _ReadOnlyGitHub:
    """Wraps the GitHub client for dry runs: reads pass through, writes are printed instead."""

    def __init__(self, inner):
        self.inner = inner
        self._next = 1

    def open_issues(self):
        return self.inner.open_issues()

    def get_issue(self, number):
        return self.inner.get_issue(number)

    def comment(self, number, body):
        print(f"--- [dry run] would comment on #{number} ---\n{body}\n")
        self._next += 1
        return -self._next

    def update_comment(self, comment_id, body):
        print(f"--- [dry run] would update status comment ---\n{body}\n")

    def close_issue(self, number):
        print(f"--- [dry run] would close #{number} ---")

    def ensure_label(self, *a, **k):
        pass

    def add_label(self, *a, **k):
        pass


if __name__ == "__main__":
    sys.exit(main())
