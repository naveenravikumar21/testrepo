"""End-to-end monitoring workflow over synthetic provider responses."""
from fare_monitor.db import Database
from fare_monitor.notify import Dispatcher
from fare_monitor.runner import run_once

from .conftest import FakeSession, Market, at, combined_handler, direct_6e, via_bom_ai

D4, D5 = "2027-01-04", "2027-01-05"


def setup(config):
    market = Market(
        serp={D4: [direct_6e(D4, 6200, 6300), via_bom_ai(D4, 7400)],
              D5: [direct_6e(D5, 5900, 5950, no="6E 9105"), via_bom_ai(D5, 7000)]},
        duffel={D4: [via_bom_ai(D4, 6900)], D5: [via_bom_ai(D5, 6800)]},
    )
    sent = []
    session = FakeSession(combined_handler(market, sent))
    db = Database(config.db_path)
    dispatcher = Dispatcher(config, session)
    assert dispatcher.names == ["telegram"]
    return market, sent, session, db, dispatcher


def texts(sent):
    return [m["text"] for m in sent]


def test_full_workflow(config):
    market, sent, session, db, dispatcher = setup(config)

    # Run 1 (07:00 IST, before the daily-summary hour): baseline alert per date, no summary yet.
    r = run_once(config, db, session, dispatcher, now=at("2026-11-07T07:00"))
    assert r.status == "ok" and r.searches == 4 and r.failed_searches == 0
    msgs = texts(sent)
    assert len(msgs) == 2 and all("first verified fare" in m for m in msgs)
    jan4 = next(m for m in msgs if "Mon 04 Jan 2027" in m)
    assert "New fare:</b> ₹6,300" in jan4      # the seller-quoted price, not the ₹6,200 search price
    assert "₹6,200" not in jan4
    assert "Cheapest across both dates right now: ₹5,950" in jan4
    assert "Nothing has been booked" in msgs[0]

    # Run 2: unchanged prices -> no duplicate alerts; after 08:00 IST the daily summary goes out once.
    sent.clear()
    run_once(config, db, session, dispatcher, now=at("2026-11-07T09:00"))
    assert len(sent) == 1 and "Daily fare summary" in sent[0]["text"]
    sent.clear()
    run_once(config, db, session, dispatcher, now=at("2026-11-07T10:00"))
    assert sent == []

    # Run 4: Jan 5 fare drops meaningfully -> record-low alert with previous low, difference, percentage.
    market.serp[D5][0].seller_price = 5450
    run_once(config, db, session, dispatcher, now=at("2026-11-07T11:00"))
    assert len(sent) == 1
    t = sent[0]["text"]
    assert "New record-low fare" in t
    assert "₹5,450" in t and "Previous lowest:</b> ₹5,950" in t and "₹500 (8.4% lower)" in t
    assert "6E 9105" in t and "06:10 JAI" in t and "3h 35m" in t and "Book:" in t and "Verified at" in t

    # Run 5: a ₹50 drop is below the "meaningful" threshold -> stored as record low, but no alert.
    sent.clear()
    market.serp[D5][0].seller_price = 5400
    run_once(config, db, session, dispatcher, now=at("2026-11-07T12:00"))
    assert sent == []
    lows = db.record_lows(D5)
    assert [round(r["price"]) for r in lows] == [5950, 5450, 5400]

    # Run 6: a flight never seen before becomes bookable within 5% of the current low -> alert.
    market.serp[D5].append(direct_6e(D5, 5600, 5600, dep="18:00", arr="21:30", no="6E 9199"))
    market.serp[D5].sort(key=lambda i: i.price)
    run_once(config, db, session, dispatcher, now=at("2026-11-07T13:00"))
    assert len(sent) == 1 and "now bookable" in sent[0]["text"] and "6E 9199" in sent[0]["text"]

    # Run 7: same state -> nothing new.
    sent.clear()
    run_once(config, db, session, dispatcher, now=at("2026-11-07T14:00"))
    assert sent == []

    # Duffel re-pricing: verified fare that beats everything on Jan 4 -> record low marked VERIFIED.
    market.duffel[D4][0].price = 5200
    run_once(config, db, session, dispatcher, now=at("2026-11-07T15:00"))
    assert len(sent) == 1 and "VERIFIED" in sent[0]["text"] and "AI 9612 / AI 9681" in sent[0]["text"]
    assert "BOM 1h 30m" in sent[0]["text"]
    db.close()


def test_indicative_prices_never_alert(config):
    market, sent, session, db, dispatcher = setup(config)
    market.duffel = {}
    run_once(config, db, session, dispatcher, now=at("2026-11-07T07:00"))
    sent.clear()
    # The search price collapses but the seller still quotes the old price: no alert, no record low.
    market.serp[D4][0].price = 3000
    run_once(config, db, session, dispatcher, now=at("2026-11-07T07:30"))
    assert sent == []
    assert [round(r["price"]) for r in db.record_lows(D4)] == [6300]
    db.close()


def test_failures_health_alert_and_recovery(config):
    market, sent, session, db, dispatcher = setup(config)
    run_once(config, db, session, dispatcher, now=at("2026-11-07T07:00"))
    sent.clear()
    market.serp_fail = market.duffel_fail = True
    statuses = [run_once(config, db, session, dispatcher, now=at(f"2026-11-07T0{h}:30")).status for h in (1, 2, 3, 4)]
    assert statuses == ["failed"] * 4
    assert len(sent) == 1 and "3 consecutive searches failed" in sent[0]["text"]
    sent.clear()
    # Partial failure: one provider down. Still works, stores the other provider's fares, recovery notice.
    market.serp_fail = False
    r = run_once(config, db, session, dispatcher, now=at("2026-11-07T05:30"))
    assert r.status == "partial"
    assert any("recovered" in m for m in texts(sent))
    db.close()


def test_window_closes_after_travel_dates(config):
    market, sent, session, db, dispatcher = setup(config)
    run_once(config, db, session, dispatcher, now=at("2026-11-07T07:00"))
    sent.clear()
    # On Jan 5 only Jan 5 is searched.
    session.calls.clear()
    run_once(config, db, session, dispatcher, now=at("2027-01-05T06:00"))
    searched = {c[2].get("outbound_date") for c in session.calls if "serpapi" in c[1]}
    assert searched == {D5}
    # After Jan 5: no searches, one final summary, then silence.
    session.calls.clear()
    sent.clear()
    r = run_once(config, db, session, dispatcher, now=at("2027-01-06T09:00"))
    assert r.window_closed and not [c for c in session.calls if "telegram" not in c[1]]
    assert len(sent) == 1 and "Final fare summary" in sent[0]["text"]
    sent.clear()
    run_once(config, db, session, dispatcher, now=at("2027-01-06T10:00"))
    assert sent == []
    db.close()


def test_dashboard_written(config):
    market, sent, session, db, dispatcher = setup(config)
    run_once(config, db, session, dispatcher, now=at("2026-11-07T07:00"))
    market.serp[D4][0].seller_price = 6100
    run_once(config, db, session, dispatcher, now=at("2026-11-07T08:00"))
    html = (config.report_dir / "index.html").read_text()
    md = (config.report_dir / "summary.md").read_text()
    assert "₹6,100" in html and "₹5,950" in html and "SELLER-QUOTED" in html
    assert "Airline comparison" in html and "IndiGo" in html and "Air India" in html
    assert 'name="u" value="direct"' in html      # Google booking redirect form
    assert "Overall cheapest:** ₹5,950" in md
    db.close()


def test_paused(config, monkeypatch):
    monkeypatch.setenv("MONITOR_ENABLED", "false")
    from fare_monitor.config import load_config
    cfg = load_config()
    market, sent, session, db, dispatcher = setup(cfg)
    assert run_once(cfg, db, session, dispatcher, now=at("2026-11-07T07:00")).status == "skipped"
    assert session.calls == []
