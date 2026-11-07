from datetime import date

import pytest

from fare_monitor.analysis import validate
from fare_monitor.models import Verification, fmt_inr
from fare_monitor.providers.base import ProviderError
from fare_monitor.providers.duffel import DuffelProvider, parse_iso_duration
from fare_monitor.providers.serpapi_google import SerpApiGoogleFlightsProvider

from .conftest import FakeResponse, FakeSession, Itin, Market, direct_6e, duffel_handler, serp_handler, via_bom_ai

D4 = date(2027, 1, 4)


def test_formatting():
    assert fmt_inr(123456) == "₹1,23,456"
    assert fmt_inr(5843.0) == "₹5,843"
    assert fmt_inr(999) == "₹999"
    assert parse_iso_duration("PT5H20M") == 320
    assert parse_iso_duration("P1DT2H") == 1560


def test_serpapi_search_and_seller_quote(config):
    m = Market(serp={"2027-01-04": [direct_6e("2027-01-04", 6200, seller=6350), via_bom_ai("2027-01-04", 7100)]})
    session = FakeSession(serp_handler(m))
    p = SerpApiGoogleFlightsProvider(config, session)
    options, meta = p.search(D4)
    assert [o.price for o in options] == [6200, 7100]
    o = options[0]
    assert o.verification == Verification.INDICATIVE
    assert o.flight_numbers == ["6E 9101"] and o.stops == 0
    assert o.departure_local == "2027-01-04T06:10" and o.duration_minutes == 215
    params = session.calls[0][2]
    assert params["type"] == 2 and params["currency"] == "INR" and params["no_cache"] == "true"

    two = options[1]
    assert two.stops == 1 and two.layovers[0].airport == "BOM" and two.layovers[0].minutes == 90

    checked = p.verify(o)
    # airline-direct quote preferred over the cheaper OTA quote; final price differs from search price
    assert checked.verification == Verification.SELLER_QUOTED
    assert checked.price == 6350 and checked.seller == "IndiGo"
    assert checked.booking_request["post_data"] == "u=direct&x=1"
    assert "checked" in checked.checked_baggage


def test_serpapi_no_results_is_empty_not_error(config):
    p = SerpApiGoogleFlightsProvider(config, FakeSession(serp_handler(Market())))
    options, _ = p.search(D4)
    assert options == []


def test_serpapi_error_raises(config):
    p = SerpApiGoogleFlightsProvider(config, FakeSession(serp_handler(Market(serp_fail=True))))
    with pytest.raises(ProviderError):
        p.search(D4)


def test_duffel_search_verify(config):
    m = Market(duffel={"2027-01-04": [via_bom_ai("2027-01-04", 6900)]})
    p = DuffelProvider(config, FakeSession(duffel_handler(m)))
    options, meta = p.search(D4)
    assert len(options) == 1 and options[0].verification == Verification.INDICATIVE
    o = options[0]
    assert o.flight_numbers == ["AI 9612", "AI 9681"] and o.layovers[0].minutes == 90
    assert o.checked_baggage == "1 checked bag(s)"
    m.duffel_reprice[o.offer_ref] = 7050
    v = p.verify(o)
    assert v.verification == Verification.VERIFIED and v.price == 7050
    assert "6900.00 -> 7050.00" in v.verification_note
    m.duffel_reprice[o.offer_ref] = None
    assert p.verify(o) is None


def test_duffel_test_mode_refused(config):
    m = Market(duffel={"2027-01-04": [via_bom_ai("2027-01-04", 6900)]}, duffel_live=False)
    with pytest.raises(ProviderError, match="TEST mode"):
        DuffelProvider(config, FakeSession(duffel_handler(m))).search(D4)


def test_duffel_http_error(config):
    p = DuffelProvider(config, FakeSession(lambda *a: FakeResponse(401, {"errors": [{"code": "unauthorized",
                                                                                    "message": "bad token"}]})))
    with pytest.raises(ProviderError, match="unauthorized"):
        p.search(D4)


def test_validation_rules(config):
    m = Market(duffel={"2027-01-04": [
        direct_6e("2027-01-04", 6000),
        Itin([("6E 1", "IndiGo", "JAI", "COK", "2027-01-04 06:00", "2027-01-04 09:00")], 80, currency="USD"),
        Itin([("6E 2", "IndiGo", "JAI", "COK", "2027-01-04 06:00", "2027-01-04 09:00")], 300),
        Itin([("6E 3", "IndiGo", "JAI", "BOM", "2027-01-04 06:00", "2027-01-04 07:40"),
              ("6E 4", "IndiGo", "BOM", "COK", "2027-01-04 08:00", "2027-01-04 10:00")], 5000),
    ]})
    options, _ = DuffelProvider(config, FakeSession(duffel_handler(m))).search(D4)
    reasons = [validate(o, D4, config) for o in options]
    assert reasons[0] is None
    assert "currency" in reasons[1]
    assert "implausible" in reasons[2]
    assert "layover" in reasons[3]           # 20-minute connection is rejected
    assert "not on" in validate(options[0], date(2027, 1, 5), config)
