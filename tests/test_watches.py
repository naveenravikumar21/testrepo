from datetime import date

from fare_monitor.watches import (is_trusted_author, is_watch_issue, parse_dates, parse_price, watch_from_issue)

from .conftest import form_body

TODAY = date(2026, 11, 8)


def issue(body, number=7, title="[Fare watch] JAI → COK", assoc="OWNER"):
    return {"number": number, "title": title, "body": body, "author_association": assoc,
            "user": {"login": "someone"}, "labels": [], "html_url": "https://x/7"}


def test_parse_date_formats():
    d, e = parse_dates("2027-01-04, 5 Jan 2027", TODAY)
    assert d == [date(2027, 1, 4), date(2027, 1, 5)] and not e
    d, _ = parse_dates("4th Jan\n05/01/2027", TODAY)       # no year -> next occurrence; Indian dd/mm/yyyy
    assert d == [date(2027, 1, 4), date(2027, 1, 5)]
    d, _ = parse_dates("2 Jan 2027 to 6 Jan 2027", TODAY)
    assert d == [date(2027, 1, i) for i in range(2, 7)]
    d, e = parse_dates("someday", TODAY)
    assert d == [] and "Couldn't read" in e[0]


def test_form_body_parsed(config):
    w = watch_from_issue(issue(form_body(target="₹7,500")), config, TODAY)
    assert not w.errors
    assert (w.origin, w.destination) == ("JAI", "COK")
    assert w.dates == [date(2027, 1, 4), date(2027, 1, 5)]
    assert w.days == list(range(7)) and w.frequency == "Every day"
    assert w.target == 7500 and w.max_stops == 2
    assert w.is_due(TODAY)


def test_frequency_and_ticked_days(config):
    w = watch_from_issue(issue(form_body(freq="2 times a week (Mon, Thu)")), config, TODAY)
    assert w.days == [0, 3] and not w.is_due(TODAY)        # 8 Nov 2026 is a Sunday
    w = watch_from_issue(issue(form_body(freq="Once a week (Mon)", days=("Tuesday", "Saturday"))), config, TODAY)
    assert w.days == [1, 5] and w.frequency == "2 times a week"


def test_cities_stops_and_empty_target(config):
    w = watch_from_issue(issue(form_body(frm="Delhi", to="Kochi (COK)", target="", stops="Non-stop only")),
                         config, TODAY)
    assert (w.origin, w.destination, w.target, w.max_stops) == ("DEL", "COK", None, 0)
    assert parse_price("Rs. 6,999") == 6999


def test_invalid_watches(config):
    assert "No travel dates" in watch_from_issue(issue(form_body(dates="")), config, TODAY).errors[0]
    assert watch_from_issue(issue(form_body(dates="2026-01-04")), config, TODAY).active_dates(TODAY) == []
    assert "too many" in watch_from_issue(issue(form_body(dates="1 Jan 2027 to 20 Jan 2027")), config,
                                          TODAY).errors[0]
    assert "airport" in watch_from_issue(issue(form_body(frm="Atlantis City")), config, TODAY).errors[0]


def test_estimates_fit_free_plan(config):
    daily = watch_from_issue(issue(form_body()), config, TODAY)
    weekly3 = watch_from_issue(issue(form_body(freq="3 times a week")), config, TODAY)
    assert 60 <= daily.estimate_searches_30d(TODAY) <= 80
    assert weekly3.estimate_searches_30d(TODAY) < daily.estimate_searches_30d(TODAY)


def test_only_trusted_authors(config):
    assert is_watch_issue(issue("x"), config)
    assert not is_watch_issue(issue("x", title="Bug report"), config)
    assert is_trusted_author(issue("x"), config)
    assert not is_trusted_author(issue("x", assoc="NONE"), config)


def test_airlines_field(config):
    from fare_monitor.watches import parse_airlines
    assert parse_airlines("IndiGo, Akasa Air") == (["6E", "QP"], [])
    assert parse_airlines("Any airline") == ([], [])
    assert parse_airlines("6E") == (["6E"], [])
    assert parse_airlines("Air India Express") == (["IX"], [])
    assert "Unknown airline" in parse_airlines("Pan Am")[1][0]
    body = form_body() + "\n### Airlines\n\nIndiGo\n"
    w = watch_from_issue(issue(body), config, TODAY)
    assert w.airlines == ["6E"] and "IndiGo only" in w.summary_lines()[-1]
    # "To" must not be confused with "How often to check" or "Stops"
    assert (w.origin, w.destination, w.max_stops) == ("JAI", "COK", 2)
