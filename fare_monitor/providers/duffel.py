"""Duffel Flights API (https://duffel.com/docs/api) - authorised NDC/GDS aggregator.

Offers returned by Duffel are real, bookable airline offers. The cheapest ones are re-fetched with
GET /air/offers/{id} immediately after the search; an offer that is re-priced successfully and has not
expired is marked VERIFIED. Duffel test-mode tokens return synthetic airlines ("Duffel Airways"), so
test-mode results are refused and never stored.

Nothing in this module creates orders or payments.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timezone

from ..models import FareOption, Layover, Verification
from .base import Provider, ProviderError, airline_site

log = logging.getLogger(__name__)

API = "https://api.duffel.com"
ISO_DURATION = re.compile(r"P(?:(?P<d>\d+)D)?(?:T(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+)S)?)?$")


def parse_iso_duration(value: str | None) -> int | None:
    if not value:
        return None
    m = ISO_DURATION.match(value)
    if not m:
        return None
    d, h, mi = (int(m.group(k) or 0) for k in ("d", "h", "m"))
    return d * 1440 + h * 60 + mi


def _minutes_between(a: str, b: str) -> int:
    return int((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() // 60)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DuffelProvider(Provider):
    name = "duffel"
    display_name = "Duffel (airline offers)"

    @classmethod
    def is_configured(cls, config) -> bool:
        return bool(config.secrets.duffel_access_token)

    @property
    def verify_budget(self) -> int:
        return int(self.settings.get("verify_top_n", 1))

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.config.secrets.duffel_access_token}",
            "Duffel-Version": "v2",
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "Content-Type": "application/json",
        }

    def _check(self, resp, what: str) -> dict:
        try:
            payload = resp.json()
        except ValueError:
            raise ProviderError(f"Duffel {what}: HTTP {resp.status_code}, non-JSON response")
        if resp.status_code >= 400:
            errors = payload.get("errors") or []
            msg = "; ".join(f"{e.get('code')}: {e.get('message')}" for e in errors) or resp.text[:200]
            raise ProviderError(f"Duffel {what}: HTTP {resp.status_code} {msg}")
        return payload

    def search(self, origin: str, destination: str, travel_date: date, max_stops: int = 2, airlines=()):
        d = self.config.defaults
        body = {
            "data": {
                "slices": [{
                    "origin": origin,
                    "destination": destination,
                    "departure_date": travel_date.isoformat(),
                }],
                "passengers": [{"type": "adult"} for _ in range(d.adults)],
                "cabin_class": d.cabin,
                "max_connections": min(max_stops, int(self.settings.get("max_connections", 2))),
            }
        }
        params = {"return_offers": "true", "supplier_timeout": int(self.settings.get("supplier_timeout_ms", 20000))}
        self.calls += 1
        try:
            resp = self.session.post(f"{API}/air/offer_requests", params=params, json=body,
                                     headers=self._headers(), timeout=60)
        except Exception as exc:  # network failure after retries
            raise ProviderError(f"Duffel search request failed: {exc}") from exc
        data = self._check(resp, "offer request").get("data") or {}
        if data.get("live_mode") is False:
            raise ProviderError("Duffel token is in TEST mode: results are synthetic and were discarded. "
                                "Use a live-mode access token.")
        observed_at = _utcnow().isoformat(timespec="seconds")
        options = []
        for offer in data.get("offers") or []:
            opt = self._parse_offer(offer, origin, destination, travel_date, observed_at, Verification.INDICATIVE,
                                    "Offer returned by Duffel search; not yet re-priced")
            if opt:
                options.append(opt)
        meta = {"offer_request_id": data.get("id"), "offer_count": len(data.get("offers") or [])}
        return options, meta

    def verify(self, option: FareOption) -> FareOption | None:
        self.calls += 1
        try:
            resp = self.session.get(f"{API}/air/offers/{option.offer_ref}", headers=self._headers(), timeout=45)
        except Exception as exc:
            log.warning("Duffel re-price failed for %s: %s", option.offer_ref, exc)
            return option  # stays INDICATIVE
        if resp.status_code in (404, 410, 422):
            log.info("Duffel offer %s no longer available (HTTP %s)", option.offer_ref, resp.status_code)
            return None
        try:
            offer = self._check(resp, "get offer").get("data") or {}
        except ProviderError as exc:
            log.warning("%s", exc)
            return option
        if offer.get("live_mode") is False:
            return None
        expires = offer.get("expires_at")
        if expires and datetime.fromisoformat(expires.replace("Z", "+00:00")) <= _utcnow():
            return None
        observed_at = _utcnow().isoformat(timespec="seconds")
        note = "Live offer re-priced via Duffel GET /air/offers"
        new_price = float(offer.get("total_amount") or 0)
        if abs(new_price - option.price) >= 0.01:
            note += f" (search price {option.price:.2f} -> {new_price:.2f})"
        return self._parse_offer(offer, option.origin, option.destination, date.fromisoformat(option.travel_date),
                                 observed_at,
                                 Verification.VERIFIED, note)

    def _parse_offer(self, offer: dict, origin: str, destination: str, travel_date: date, observed_at: str,
                     verification: Verification, note: str) -> FareOption | None:
        try:
            slices = offer.get("slices") or []
            if len(slices) != 1:
                return None
            sl = slices[0]
            segments = sl.get("segments") or []
            if not segments:
                return None
            first, last = segments[0], segments[-1]
            flight_numbers, airlines, layovers = [], [], []
            for i, seg in enumerate(segments):
                carrier = seg.get("marketing_carrier") or {}
                code = carrier.get("iata_code") or ""
                flight_numbers.append(f"{code} {seg.get('marketing_carrier_flight_number', '')}".strip())
                name = carrier.get("name") or code
                if name not in airlines:
                    airlines.append(name)
                if i > 0:
                    prev = segments[i - 1]
                    layovers.append(Layover(
                        airport=(seg.get("origin") or {}).get("iata_code", "?"),
                        minutes=_minutes_between(prev["arriving_at"], seg["departing_at"]),
                        overnight=prev["arriving_at"][:10] != seg["departing_at"][:10],
                    ))
            duration = parse_iso_duration(sl.get("duration"))
            if duration is None:
                duration = _minutes_between(first["departing_at"], last["arriving_at"])
            cabin_bags, checked_bags = self._baggage(first)
            owner = (offer.get("owner") or {})
            carrier_code = ((first.get("marketing_carrier") or {}).get("iata_code") or "")
            return FareOption(
                provider=self.name,
                source=self.display_name,
                origin=origin,
                destination=destination,
                travel_date=travel_date.isoformat(),
                airline=" + ".join(airlines),
                flight_numbers=flight_numbers,
                departure_local=first["departing_at"][:16],
                arrival_local=last["arriving_at"][:16],
                duration_minutes=duration,
                stops=len(segments) - 1,
                layovers=layovers,
                price=float(offer["total_amount"]),
                currency=offer.get("total_currency", ""),
                verification=verification,
                verification_note=note,
                observed_at=observed_at,
                booking_url=airline_site(carrier_code, origin, destination, travel_date),
                seller=owner.get("name", "") + " (Duffel offer)",
                cabin_baggage=cabin_bags,
                checked_baggage=checked_bags,
                offer_ref=offer.get("id", ""),
                expires_at=offer.get("expires_at"),
                extra={"base_amount": offer.get("base_amount"), "tax_amount": offer.get("tax_amount")},
            )
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("Skipping malformed Duffel offer %s: %s", offer.get("id"), exc)
            return None

    @staticmethod
    def _baggage(segment: dict) -> tuple[str, str]:
        carry, checked = 0, 0
        known = False
        for pax in segment.get("passengers") or []:
            for bag in pax.get("baggages") or []:
                known = True
                if bag.get("type") == "carry_on":
                    carry += int(bag.get("quantity") or 0)
                elif bag.get("type") == "checked":
                    checked += int(bag.get("quantity") or 0)
            break  # first passenger is representative (1 adult)
        if not known:
            return "", ""
        return f"{carry} cabin bag(s)", f"{checked} checked bag(s)"
