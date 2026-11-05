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
        spec_url="https://raw.githubusercontent.com/stripe/openapi/master/openapi/spec3.sdk.json",
        changelog_url="https://docs.stripe.com/changelog",
    ),
    # One YAML file per API version; 2020-09-14 is the only version Plaid publishes. ``info.version`` is
    # ``2020-09-14_<release>`` and the changelog is keyed by that release number.
    "plaid": Provider(
        name="plaid",
        spec_url="https://raw.githubusercontent.com/plaid/plaid-openapi/master/2020-09-14.yml",
        changelog_url="https://github.com/plaid/plaid-openapi/blob/master/CHANGELOG.md",
    ),
}


def get_provider(name: str) -> Provider:
    try:
        return PROVIDERS[name]
    except KeyError:
        known = ", ".join(sorted(PROVIDERS))
        raise SystemExit(f"unknown provider {name!r}; known providers: {known}") from None
