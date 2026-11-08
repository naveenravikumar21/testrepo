"""Google Flights results through SerpApi's Google Flights API (https://serpapi.com/google-flights-api).

Two steps, no HTML scraping on our side:
1. Search (engine=google_flights, type=2 one-way). The prices here are INDICATIVE.
2. For the cheapest itineraries, request the booking options with their booking_token. Google then
   lists named sellers (the airline itself and/or OTAs such as MakeMyTrip, Cleartrip, ixigo) with the
   price each one quotes, including taxes. That price is recorded as SELLER_QUOTED. The airline's own
   quote is preferred over OTA quotes, because OTAs can add convenience fees at checkout.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from ..models import FareOption, Layover, Verification
from .base import Provider, ProviderError, google_flights_search_url

log = logging.getLogger(__name__)

API = "https://serpapi.com/search.json"
NO_RESULTS_MARKERS = ("hasn't returned any results", "no results")
STOPS_PARAM = {0: 1, 1: 2, 2: 3}  # SerpApi: 1 nonstop, 2 <=1 stop, 3 <=2 stops, 0 any


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iso_local(value: str) -> str:
    # "2027-01-04 6:10" / "2027-01-04 06:10" -> "2027-01-04T06:10"
    day, _, clock = value.strip().partition(" ")
    hh, _, mm = clock.partition(":")
    return f"{day}T{int(hh):02d}:{int(mm):02d}"


class SerpApiGoogleFlightsProvider(Provider):
    name = "serpapi_google_flights"
    display_name = "Google Flights (via SerpApi)"
    counts_against_budget = True

    @classmethod
    def is_configured(cls, config) -> bool:
        return bool(config.secrets.serpapi_api_key)

    @property
    def verify_budget(self) -> int:
        return int(self.settings.get("booking_checks_per_date", 1))

    def remaining_quota(self) -> dict | None:
        """SerpApi Account API (free, does not use a search)."""
        try:
            resp = self.session.get("https://serpapi.com/account.json",
                                    params={"api_key": self.config.secrets.serpapi_api_key}, timeout=30)
            data = resp.json()
        except Exception as exc:
            log.warning("SerpApi account lookup failed: %s", exc)
            return None
        if resp.status_code >= 400 or "error" in data:
            log.warning("SerpApi account lookup failed: %s", data.get("error") or resp.status_code)
            return None
        used = data.get("this_month_usage")
        left = data.get("total_searches_left", data.get("plan_searches_left"))
        if used is None and left is None:
            return None
        return {"used": used, "left": left, "per_month": data.get("searches_per_month"),
                "plan": data.get("plan_name")}

    def _base_params(self, origin: str, destination: str, travel_date: date, max_stops: int) -> dict:
        d = self.config.defaults
        params = {
            "engine": "google_flights",
            "departure_id": origin,
            "arrival_id": destination,
            "outbound_date": travel_date.isoformat(),
            "type": 2,                    # one-way
            "travel_class": 1,            # economy
            "adults": d.adults,
            "currency": d.currency,
            "gl": "in",
            "hl": "en",
            "no_cache": "true",           # always a fresh search: no stale cached prices
            "api_key": self.config.secrets.serpapi_api_key,
        }
        stops = STOPS_PARAM.get(max_stops)
        if stops:
            params["stops"] = stops
        if self.settings.get("deep_search", True):
            params["deep_search"] = "true"
        return params

    def _get(self, params: dict, what: str) -> dict:
        self.calls += 1
        try:
            resp = self.session.get(API, params=params, timeout=90)
        except Exception as exc:
            raise ProviderError(f"SerpApi {what} request failed: {exc}") from exc
        try:
            payload = resp.json()
        except ValueError:
            raise ProviderError(f"SerpApi {what}: HTTP {resp.status_code}, non-JSON response")
        error = payload.get("error")
        if error and any(m in error.lower() for m in NO_RESULTS_MARKERS):
            return {"_no_results": True, **payload}
        if resp.status_code >= 400 or error:
            raise ProviderError(f"SerpApi {what}: HTTP {resp.status_code} {error or ''}".strip())
        status = (payload.get("search_metadata") or {}).get("status")
        if status and status != "Success":
            raise ProviderError(f"SerpApi {what}: search status {status}")
        return payload

    def search(self, origin: str, destination: str, travel_date: date, max_stops: int = 2):
        payload = self._get(self._base_params(origin, destination, travel_date, max_stops), "search")
        meta = {"price_insights": payload.get("price_insights"),
                "google_flights_url": (payload.get("search_metadata") or {}).get("google_flights_url")}
        if payload.get("_no_results"):
            return [], meta
        search_url = meta["google_flights_url"] or google_flights_search_url(origin, destination, travel_date)
        observed_at = _utcnow_iso()
        options = []
        for group in ("best_flights", "other_flights"):
            for item in payload.get(group) or []:
                opt = self._parse_itinerary(item, origin, destination, travel_date, observed_at, search_url)
                if opt:
                    options.append(opt)
        return options, meta

    def _parse_itinerary(self, item: dict, origin: str, destination: str, travel_date: date, observed_at: str,
                         search_url: str):
        legs = item.get("flights") or []
        price = item.get("price")
        if not legs or not isinstance(price, (int, float)):
            return None  # "price unavailable" itineraries are ignored, never guessed
        try:
            airlines = []
            for leg in legs:
                if leg.get("airline") and leg["airline"] not in airlines:
                    airlines.append(leg["airline"])
            layovers = [Layover(airport=l.get("id") or l.get("name", "?"), minutes=int(l.get("duration") or 0),
                                overnight=bool(l.get("overnight"))) for l in item.get("layovers") or []]
            extensions = [e for leg in legs for e in (leg.get("extensions") or [])]
            cabin = next((e for e in extensions if "carry-on" in e.lower() or "cabin" in e.lower()), "")
            checked = next((e for e in extensions if "checked" in e.lower()), "")
            return FareOption(
                provider=self.name,
                source=self.display_name,
                origin=origin,
                destination=destination,
                travel_date=travel_date.isoformat(),
                airline=" + ".join(airlines),
                flight_numbers=[leg.get("flight_number", "?") for leg in legs],
                departure_local=_iso_local(legs[0]["departure_airport"]["time"]),
                arrival_local=_iso_local(legs[-1]["arrival_airport"]["time"]),
                duration_minutes=int(item.get("total_duration") or sum(int(l.get("duration") or 0) for l in legs)),
                stops=len(legs) - 1,
                layovers=layovers,
                price=float(price),
                currency=self.config.defaults.currency,
                verification=Verification.INDICATIVE,
                verification_note="Google Flights search price (not yet confirmed by a seller)",
                observed_at=observed_at,
                booking_url=search_url,
                seller="",
                cabin_baggage=cabin,
                checked_baggage=checked,
                offer_ref=item.get("booking_token", ""),
                extra={"origin": legs[0]["departure_airport"].get("id"),
                       "destination": legs[-1]["arrival_airport"].get("id")},
            )
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("Skipping malformed Google Flights itinerary: %s", exc)
            return None

    def verify(self, option: FareOption) -> FareOption | None:
        if not option.offer_ref:
            return option
        params = self._base_params(option.origin, option.destination, date.fromisoformat(option.travel_date),
                                   self.config.defaults.max_stops)
        params["booking_token"] = option.offer_ref
        try:
            payload = self._get(params, "booking options")
        except ProviderError as exc:
            log.warning("%s", exc)
            return option  # stays INDICATIVE
        quotes = []
        for raw in payload.get("booking_options") or []:
            together = raw.get("together")
            if raw.get("separate_tickets") or not together:
                continue  # separate tickets are not one bookable fare
            price = together.get("price")
            if not isinstance(price, (int, float)):
                continue
            quotes.append(together)
        if not quotes:
            option.verification_note = "Google Flights search price; no seller quote was available"
            return option
        airline_names = {a.strip().lower() for a in option.airline.split("+")}
        direct = [q for q in quotes if (q.get("book_with") or "").strip().lower() in airline_names
                  or q.get("airline") is True]
        best = min(direct or quotes, key=lambda q: q["price"])
        seller = best.get("book_with") or "unknown seller"
        option.price = float(best["price"])
        option.seller = seller
        option.verification = Verification.SELLER_QUOTED
        option.observed_at = _utcnow_iso()
        note = f"Final price quoted by {seller} via Google Flights booking options"
        if not direct:
            note += " (OTA quote: payment/convenience fees may be added at checkout)"
        option.verification_note = note
        baggage = best.get("baggage_prices") or (payload.get("baggage_prices") or {}).get("together") or []
        if baggage:
            option.cabin_baggage = option.cabin_baggage or "; ".join(
                b for b in baggage if "carry" in b.lower() or "cabin" in b.lower())
            option.checked_baggage = "; ".join(b for b in baggage if "checked" in b.lower()) or option.checked_baggage
        req = best.get("booking_request")
        if isinstance(req, dict) and req.get("url"):
            option.booking_request = {"url": req["url"], "post_data": req.get("post_data", "")}
        option.extra["all_quotes"] = sorted(
            [{"seller": q.get("book_with"), "price": q["price"]} for q in quotes], key=lambda q: q["price"])[:8]
        return option
