"""Core data types."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import IntEnum


class Verification(IntEnum):
    """How much we trust a price. Higher is stronger. Stored as an integer in the database."""

    INDICATIVE = 1      # search-result price only (may exclude fees, may be cached)
    SELLER_QUOTED = 2   # final price quoted by a named seller (airline/OTA) moments ago
    VERIFIED = 3        # live bookable offer re-priced through a booking API

    @classmethod
    def parse(cls, value: str | int | "Verification") -> "Verification":
        if isinstance(value, Verification):
            return value
        if isinstance(value, int) or str(value).isdigit():
            return cls(int(value))
        return cls[str(value).strip().upper()]

    @property
    def label(self) -> str:
        return {
            Verification.INDICATIVE: "INDICATIVE",
            Verification.SELLER_QUOTED: "SELLER-QUOTED",
            Verification.VERIFIED: "VERIFIED",
        }[self]

    @property
    def icon(self) -> str:
        return {Verification.INDICATIVE: "⚪", Verification.SELLER_QUOTED: "🟡", Verification.VERIFIED: "✅"}[self]


@dataclass
class Layover:
    airport: str
    minutes: int
    overnight: bool = False


@dataclass
class FareOption:
    provider: str                 # e.g. "duffel", "serpapi_google_flights"
    source: str                   # human readable source, e.g. "Google Flights (via SerpApi)"
    travel_date: str              # ISO date of departure (local)
    airline: str                  # "IndiGo" or "IndiGo + Air India"
    flight_numbers: list[str]     # ["6E 123", "6E 456"]
    departure_local: str          # "2027-01-04T06:10"
    arrival_local: str            # "2027-01-04T12:40"
    duration_minutes: int
    stops: int
    layovers: list[Layover]
    price: float                  # total payable for all passengers, incl. taxes & mandatory charges
    currency: str
    verification: Verification
    verification_note: str
    observed_at: str              # UTC ISO timestamp when the price was obtained
    booking_url: str
    seller: str = ""
    cabin_baggage: str = ""
    checked_baggage: str = ""
    offer_ref: str = ""           # provider reference (Duffel offer id, Google booking token)
    expires_at: str | None = None
    booking_request: dict | None = None   # optional POST form for Google booking redirects
    extra: dict = field(default_factory=dict)

    @property
    def flight_key(self) -> str:
        return f"{self.travel_date}|{'/'.join(fn.replace(' ', '') for fn in self.flight_numbers)}"

    @property
    def flights_label(self) -> str:
        return " / ".join(self.flight_numbers)

    def layovers_json(self) -> str:
        return json.dumps([asdict(l) for l in self.layovers])

    def stops_label(self) -> str:
        return "Non-stop" if self.stops == 0 else f"{self.stops} stop{'s' if self.stops > 1 else ''}"

    def layovers_label(self) -> str:
        if not self.layovers:
            return "none (direct)"
        return ", ".join(
            f"{l.airport} {fmt_minutes(l.minutes)}{' (overnight)' if l.overnight else ''}" for l in self.layovers
        )


def fmt_minutes(minutes: int | None) -> str:
    if minutes is None:
        return "?"
    h, m = divmod(int(minutes), 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def fmt_inr(amount: float | None) -> str:
    if amount is None:
        return "—"
    whole = int(round(amount))
    s = str(abs(whole))
    # Indian digit grouping: 1,23,456
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups + [tail])
    return f"{'-' if whole < 0 else ''}₹{s}"
