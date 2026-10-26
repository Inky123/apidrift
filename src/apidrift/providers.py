"""Registry of API providers whose OpenAPI specs we track."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Provider:
    name: str
    spec_url: str
    changelog_url: str


PROVIDERS: dict[str, Provider] = {
    "stripe": Provider(
        name="stripe",
        spec_url="https://raw.githubusercontent.com/stripe/openapi/master/openapi/spec3.json",
        changelog_url="https://docs.stripe.com/changelog",
    ),
}


def get_provider(name: str) -> Provider:
    try:
        return PROVIDERS[name]
    except KeyError:
        known = ", ".join(sorted(PROVIDERS))
        raise SystemExit(f"unknown provider {name!r}; known providers: {known}") from None
