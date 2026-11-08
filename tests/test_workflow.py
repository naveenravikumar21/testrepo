"""End-to-end monitoring over synthetic Google Flights responses and an in-memory GitHub."""
from fare_monitor import cli
from fare_monitor.db import Database
from fare_monitor.github_api import GitHubClient
from fare_monitor.notify import Dispatcher
from fare_monitor.runner import Monitor

from .conftest import FakeGitHub, FakeSession, Market, at, combined_handler, direct_6e, form_body, via_bom_ai

D4, D5 = "2027-01-04", "2027-01-05"


class Env:
    def __init__(self, config):
        self.config = config
        self.market = Market(serp={D4: [direct_6e(D4, 7800, 7900), via_bom_ai(D4, 8600)],
                                   D5: [direct_6e(D5, 7200, 7300, no="6E 9105"), via_bom_ai(D5, 8100)]})
        self.gh = FakeGitHub()
        self.session = FakeSession(combined_handler(self.market, self.gh))
        self.db = Database(config.db_path)

    def run(self, when, **kw):
        github = GitHubClient("t", "naveenravikumar21/testrepo", self.session)
        m = Monitor(self.config, self.db, self.session, github, Dispatcher(self.config, self.session, github=github),
                    now=at(when))
        return m.run(**kw)

    def calls(self):
        n, self.market.serp_calls = self.market.serp_calls, 0
        return n


def test_new_watch_first_check_and_alerts(config):
    env = Env(config)
    env.gh.add_issue(1, form_body())

    # Sunday 8 Nov, issue just created: acknowledged + checked immediately.
    r = env.run("2026-11-08T10:00", trigger="issues", force_ids={"1"})
    assert r.status == "ok" and r.watches_checked == 1
    # 2 date searches + 1 seller confirmation per date (first price for each date) = 4 searches
    assert env.calls() == 4 and r.api_calls == 4
    bodies = env.gh.bodies(1)
    assert len(bodies) == 2
    assert "Watch set up" in bodies[0] and "about **" in bodies[0]
    first = bodies[1]
    assert "First prices" in first and "₹7,900" in first and "₹7,300" in first        # seller-quoted prices
    assert "is **rising**" in first                                                      # Google price history
    assert "Fare under your book-now price" in first and "Tue 05 Jan 2027 — ₹7,300" in first
    assert "Nothing has been booked" in first
    assert 1 in env.gh.labels_added
    # The ack comment becomes the quietly-edited status comment.
    ack_id = env.gh.comments[0]["id"]
    assert env.gh.edits and env.gh.edits[-1][0] == ack_id and "Latest status" in env.gh.edits[-1][1]

    # Same day, scheduled run: already checked today -> nothing searched, nothing posted.
    r = env.run("2026-11-08T23:00")
    assert r.status == "idle" and env.calls() == 0 and len(env.gh.bodies(1)) == 2

    # Monday: prices unchanged -> 2 searches, no confirmations (under-target fare confirmed < 72h ago), no email.
    r = env.run("2026-11-09T07:05")
    assert env.calls() == 2 and len(env.gh.bodies(1)) == 2

    # Tuesday: Jan 4 drops to ₹7,100 search / ₹7,150 seller -> record low (-₹750, 9.5%) and under target.
    env.market.serp[D4][0].price, env.market.serp[D4][0].seller_price = 7100, 7150
    env.run("2026-11-10T07:05")
    assert env.calls() == 3
    alert = env.gh.bodies(1)[-1]
    assert "New lowest fare" in alert and "Mon 04 Jan 2027 — ₹7,150" in alert
    assert "Previous lowest confirmed:** ₹7,900" in alert and "₹750 (9.5% lower)" in alert
    assert "6E 9101" in alert and "06:10 JAI → 09:45 COK" in alert and "3h 35m" in alert and "Book:" in alert

    # Wednesday: same prices -> no repeat alert.
    env.run("2026-11-11T07:05")
    assert len(env.gh.bodies(1)) == 3

    # Thursday: a new cheaper flight appears on Jan 5.
    env.market.serp[D5].insert(0, direct_6e(D5, 6900, 6950, dep="18:00", arr="21:30", no="6E 9199"))
    env.run("2026-11-12T07:05")
    alert = env.gh.bodies(1)[-1]
    assert "6E 9199" in alert and "₹6,950" in alert
    env.db.close()


def test_frequency_budget_and_closing(config, monkeypatch):
    env = Env(config)
    env.gh.add_issue(2, form_body(freq="2 times a week (Mon, Thu)"))
    env.run("2026-11-08T10:00", trigger="issues", force_ids={"2"})          # created on Sunday: checked now
    env.calls()
    assert env.run("2026-11-10T07:05").status == "idle" and env.calls() == 0   # Tuesday: not a check day
    env.run("2026-11-12T07:05")                                                # Thursday: check day
    assert env.calls() == 3      # 2 searches + re-confirming the under-target Jan 5 fare (last confirmed >72h ago)

    # SerpApi reports only 21 searches left (safety margin 20) -> only the soonest date is searched.
    env.market.account = {"this_month_usage": 229, "total_searches_left": 21}
    r = env.run("2026-11-16T07:05")
    assert r.budget_skipped == 1 and env.calls() == 1
    env.market.account = {"this_month_usage": 230, "total_searches_left": 20}
    r = env.run("2026-11-19T07:05")
    assert env.calls() == 0 and "Checks paused" in env.gh.bodies(2)[-1]

    # Closing the issue stops the watch.
    env.market.account = None
    env.gh.issues[2]["state"] = "closed"
    assert env.run("2026-11-23T07:05").status == "idle" and env.calls() == 0
    env.db.close()


def test_failures_untrusted_invalid_and_finished(config):
    env = Env(config)
    env.gh.add_issue(3, form_body(dates="2027-01-04"))
    env.gh.add_issue(4, form_body(), assoc="NONE", login="stranger")             # ignored
    env.gh.add_issue(5, form_body(dates="next full moon"))                       # invalid -> one error comment
    env.run("2026-11-08T10:00")
    assert env.gh.bodies(4) == []
    assert len(env.gh.bodies(5)) == 1 and "couldn't set up" in env.gh.bodies(5)[0]
    env.run("2026-11-09T07:05")
    assert len(env.gh.bodies(5)) == 1                                            # not repeated

    env.market.serp_fail = True
    assert env.run("2026-11-10T07:05").status == "failed"
    assert not any("failing" in b for b in env.gh.bodies(3))
    env.run("2026-11-11T07:05")
    assert "Price checks are failing (2 in a row)" in env.gh.bodies(3)[-1]
    env.market.serp_fail = False
    env.run("2026-11-12T07:05")
    assert "working again" in env.gh.bodies(3)[-1]

    # After the travel date: final summary comment and the issue is closed.
    env.run("2027-01-05T07:05")
    assert "Watch finished" in env.gh.bodies(3)[-1] and 3 in env.gh.closed
    env.db.close()


def test_dashboard_and_cli(config, monkeypatch, capsys):
    env = Env(config)
    env.gh.add_issue(1, form_body())
    env.run("2026-11-08T10:00", force_ids={"1"})
    html = (config.report_dir / "index.html").read_text()
    assert "JAI → COK" in html and "₹7,300" in html and "SELLER-QUOTED" in html and "rising" in html
    assert 'name="u" value="direct"' in html and "Airline comparison" in html
    env.db.close()

    monkeypatch.delenv("SERPAPI_API_KEY")
    monkeypatch.delenv("GITHUB_TOKEN")
    monkeypatch.setenv("TRAVEL_DATES", "2027-01-04")
    assert cli.main(["run", "--dry-run"]) == 2                    # no fare source -> configuration error
    assert cli.main(["check-config"]) == 0
    assert "test-key" not in capsys.readouterr().out
