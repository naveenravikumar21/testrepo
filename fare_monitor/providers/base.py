"""Provider interface. Every fare source implements search() and, optionally, verify()."""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from urllib.parse import quote

from ..config import Config
from ..models import FareOption

# Official airline websites, used as the booking link when a provider has no consumer deep link.
AIRLINE_SITES = {
    "6E": "https://www.goindigo.in/",
    "AI": "https://www.airindia.com/",
    "IX": "https://www.airindiaexpress.com/",
    "QP": "https://www.akasaair.com/",
    "SG": "https://www.spicejet.com/",
    "UK": "https://www.airindia.com/",
    "I5": "https://www.airindiaexpress.com/",
    "9I": "https://www.allianceair.in/",
}


class ProviderError(Exception):
    """A search failed. The runner records it and moves on; nothing from it is stored as a fare."""


class Provider(ABC):
    name: str = "base"
    display_name: str = "Base"
    counts_against_budget: bool = False   # True when each API call uses a metered search

    def __init__(self, config: Config, session):
        self.config = config
        self.session = session
        self.settings = config.provider_settings.get(self.name, {})
        self.calls = 0                    # API calls made by this instance (for the search budget)

    @classmethod
    def is_configured(cls, config: Config) -> bool:  # pragma: no cover - overridden
        return False

    @abstractmethod
    def search(self, origin: str, destination: str, travel_date: date, max_stops: int,
               airlines: tuple[str, ...] = ()) -> tuple[list[FareOption], dict]:
        """Return (options, metadata). Raise ProviderError on failure. `airlines` (IATA codes) is a hint;
        the runner always re-filters results so every leg is on one of those airlines."""

    def verify(self, option: FareOption) -> FareOption | None:
        """Re-check an option's final price. Return the updated option, or None if no longer available."""
        return option

    @property
    def verify_budget(self) -> int:
        return 0

    def remaining_quota(self) -> dict | None:
        """Provider-reported usage, e.g. {"used": 12, "left": 238}. None if unknown."""
        return None


def google_flights_search_url(origin: str, destination: str, travel_date: date) -> str:
    q = f"Flights from {origin} to {destination} on {travel_date.isoformat()} one way economy"
    return f"https://www.google.com/travel/flights?q={quote(q)}&curr=INR&hl=en&gl=in"


def airline_site(carrier_code: str, origin: str, destination: str, travel_date: date) -> str:
    return AIRLINE_SITES.get(carrier_code.upper(), google_flights_search_url(origin, destination, travel_date))
