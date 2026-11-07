"""Provider registry."""
from __future__ import annotations

import logging

from .base import Provider, ProviderError
from .duffel import DuffelProvider
from .serpapi_google import SerpApiGoogleFlightsProvider

log = logging.getLogger(__name__)

REGISTRY: dict[str, type[Provider]] = {
    DuffelProvider.name: DuffelProvider,
    SerpApiGoogleFlightsProvider.name: SerpApiGoogleFlightsProvider,
}


def build_providers(config, session) -> list[Provider]:
    providers = []
    for name in config.providers_enabled:
        cls = REGISTRY.get(name)
        if cls is None:
            log.error("Unknown provider %r (known: %s)", name, ", ".join(REGISTRY))
            continue
        if not cls.is_configured(config):
            log.warning("Provider %s is enabled but has no credentials; skipping", name)
            continue
        providers.append(cls(config, session))
    return providers


__all__ = ["Provider", "ProviderError", "REGISTRY", "build_providers"]
