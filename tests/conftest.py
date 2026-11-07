"""Test doubles. All fares below are SYNTHETIC TEST DATA used only to exercise the code paths;
they are never written to the production database and are not real prices."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from fare_monitor.config import load_config


class FakeResponse:
    def __init__(self, status: int, payload, headers=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {"content-type": "application/json"}
        self.text = json.dumps(payload) if not isinstance(payload, str) else payload

    def json(self):
        if isinstance(self._payload, str):
            raise ValueError("not json")
        return self._payload


class FakeSession:
    """Routes requests to a handler(method, url, params, json) -> FakeResponse and records calls."""

    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None, **kw):
        self.calls.append(("GET", url, params, None))
        return self.handler("GET", url, params or {}, None)

    def post(self, url, params=None, json=None, headers=None, timeout=None, **kw):
        self.calls.append(("POST", url, params, json))
        return self.handler("POST", url, params or {}, json)


@dataclass
class Itin:
    """A synthetic itinerary. legs: (flight_no, airline, from, to, dep 'YYYY-MM-DD HH:MM', arr)."""
    legs: list
    price: float                     # search price
    seller_price: float | None = None  # airline quote at booking options (None -> same as price)
    currency: str = "INR"

    @property
    def key(self):
        return "/".join(l[0].replace(" ", "") for l in self.legs)


@dataclass
class Market:
    serp: dict = field(default_factory=dict)     # date -> [Itin]
    duffel: dict = field(default_factory=dict)   # date -> [Itin]
    serp_fail: bool = False
    duffel_fail: bool = False
    duffel_live: bool = True
    duffel_reprice: dict = field(default_factory=dict)  # offer key -> new price / None (gone)


def _mins(a: str, b: str) -> int:
    fmt = "%Y-%m-%d %H:%M"
    return int((datetime.strptime(b, fmt) - datetime.strptime(a, fmt)).total_seconds() // 60)


def serp_handler(market: Market):
    def handler(method, url, params, body):
        if market.serp_fail:
            return FakeResponse(503, {"error": "Service temporarily unavailable"})
        date = params["outbound_date"]
        itins = market.serp.get(date, [])
        if "booking_token" in params:
            it = next((i for i in itins if f"{date}|{i.key}" == params["booking_token"]), None)
            if it is None:
                return FakeResponse(200, {"search_metadata": {"status": "Success"}, "booking_options": []})
            airline = it.legs[0][1]
            price = it.seller_price if it.seller_price is not None else it.price
            return FakeResponse(200, {
                "search_metadata": {"status": "Success"},
                "booking_options": [
                    {"together": {"book_with": "SomeOTA", "price": price - 50,
                                  "booking_request": {"url": "https://www.google.com/travel/clk/f",
                                                      "post_data": "u=ota"}}},
                    {"together": {"book_with": airline, "price": price,
                                  "baggage_prices": ["1 free carry-on", "1st checked bag free"],
                                  "booking_request": {"url": "https://www.google.com/travel/clk/f",
                                                      "post_data": "u=direct&x=1"}}},
                ]})
        if not itins:
            return FakeResponse(200, {"search_metadata": {"status": "Success"},
                                      "error": "Google Flights hasn't returned any results for this query."})
        flights = []
        for it in itins:
            legs = [{"flight_number": l[0], "airline": l[1],
                     "departure_airport": {"id": l[2], "time": l[4]},
                     "arrival_airport": {"id": l[3], "time": l[5]},
                     "duration": _mins(l[4], l[5])} for l in it.legs]
            layovers = [{"id": it.legs[i][3], "duration": _mins(it.legs[i][5], it.legs[i + 1][4])}
                        for i in range(len(it.legs) - 1)]
            flights.append({"flights": legs, "layovers": layovers,
                            "total_duration": _mins(it.legs[0][4], it.legs[-1][5]),
                            "price": it.price, "booking_token": f"{date}|{it.key}"})
        return FakeResponse(200, {
            "search_metadata": {"status": "Success", "google_flights_url": f"https://www.google.com/travel/flights?test={date}"},
            "best_flights": flights[:1], "other_flights": flights[1:],
            "price_insights": {"lowest_price": min(i.price for i in itins), "price_level": "typical",
                               "typical_price_range": [5000, 9000]}})
    return handler


def duffel_offer(date: str, it: Itin, price: float | None = None, expires: str | None = None):
    segs = []
    for l in it.legs:
        segs.append({
            "origin": {"iata_code": l[2]}, "destination": {"iata_code": l[3]},
            "departing_at": l[4].replace(" ", "T") + ":00", "arriving_at": l[5].replace(" ", "T") + ":00",
            "marketing_carrier": {"iata_code": l[0].split()[0], "name": l[1]},
            "marketing_carrier_flight_number": l[0].split()[1],
            "passengers": [{"baggages": [{"type": "checked", "quantity": 1}, {"type": "carry_on", "quantity": 1}]}],
        })
    total = _mins(it.legs[0][4], it.legs[-1][5])
    return {
        "id": f"off_{date}_{it.key.replace('/', '-')}", "live_mode": True,
        "total_amount": f"{price if price is not None else it.price:.2f}", "total_currency": it.currency,
        "expires_at": expires or "2099-01-01T00:00:00Z", "owner": {"name": it.legs[0][1]},
        "slices": [{"duration": f"PT{total // 60}H{total % 60}M", "segments": segs}],
    }


def duffel_handler(market: Market):
    def handler(method, url, params, body):
        if market.duffel_fail:
            return FakeResponse(500, {"errors": [{"code": "internal_server_error", "message": "boom"}]})
        if method == "POST" and url.endswith("/air/offer_requests"):
            date = body["data"]["slices"][0]["departure_date"]
            return FakeResponse(201, {"data": {"id": "orq_1", "live_mode": market.duffel_live,
                                               "offers": [duffel_offer(date, i) for i in market.duffel.get(date, [])]}})
        if method == "GET" and "/air/offers/" in url:
            oid = url.rsplit("/", 1)[1]
            _, date, key = oid.split("_", 2)
            it = next((i for i in market.duffel.get(date, []) if i.key.replace("/", "-") == key), None)
            if it is None or market.duffel_reprice.get(oid, "keep") is None:
                return FakeResponse(404, {"errors": [{"code": "not_found", "message": "gone"}]})
            new = market.duffel_reprice.get(oid, "keep")
            return FakeResponse(200, {"data": duffel_offer(date, it, None if new == "keep" else new)})
        return FakeResponse(404, {"errors": [{"code": "not_found"}]})
    return handler


def combined_handler(market: Market, sent: list):
    serp, duffel = serp_handler(market), duffel_handler(market)

    def handler(method, url, params, body):
        if "serpapi.com" in url:
            return serp(method, url, params, body)
        if "api.duffel.com" in url:
            return duffel(method, url, params, body)
        if "api.telegram.org" in url:
            sent.append(body)
            return FakeResponse(200, {"ok": True})
        raise AssertionError(f"unexpected URL {url}")
    return handler


@pytest.fixture
def config(tmp_path, monkeypatch):
    for k in ("DUFFEL_ACCESS_TOKEN", "SERPAPI_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "NOTIFY_CHANNELS",
              "PROVIDERS", "TARGET_FARE_INR", "ALERT_MIN_STATUS", "MONITOR_ENABLED", "TRAVEL_DATES"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SERPAPI_API_KEY", "test-key")
    monkeypatch.setenv("DUFFEL_ACCESS_TOKEN", "duffel_test_token")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setenv("FARE_STATE_DIR", str(tmp_path / "state"))
    return load_config(Path(__file__).resolve().parent.parent / "config" / "monitor.toml")


def at(s: str) -> datetime:
    """IST wall-clock string -> aware UTC datetime."""
    return (datetime.fromisoformat(s) - timedelta(hours=5, minutes=30)).replace(tzinfo=timezone.utc)


# Synthetic itineraries (TEST DATA ONLY)
def direct_6e(date, price, seller=None, dep="06:10", arr="09:45", no="6E 9101"):
    return Itin([(no, "IndiGo", "JAI", "COK", f"{date} {dep}", f"{date} {arr}")], price, seller)


def via_bom_ai(date, price, seller=None):
    return Itin([("AI 9612", "Air India", "JAI", "BOM", f"{date} 10:00", f"{date} 11:45"),
                 ("AI 9681", "Air India", "BOM", "COK", f"{date} 13:15", f"{date} 15:20")], price, seller)
