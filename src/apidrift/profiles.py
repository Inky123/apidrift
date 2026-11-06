"""How each vendor's calls are found in a checkout: the index built from its specs and the reader that uses it.

:mod:`providers` says where a vendor publishes its spec; a profile says how code that calls the vendor is
read. Stripe's SDKs are described by metadata in its spec (:mod:`sdkmap`) and read by :mod:`scanner`'s own
parsers. Plaid's SDKs are generated from the spec's operations (:mod:`plaidmap`, :mod:`plaidscan`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from . import plaidscan
from .plaidmap import PlaidIndex
from .scanner import Reader
from .sdkmap import SdkIndex


@dataclass(frozen=True)
class Profile:
    name: str
    build_index: Callable[..., Any]  # (*specs) -> the index the reader resolves calls against
    reader: Reader
    fixes: bool = False  # documented migrations exist, so ``apidrift fix`` can plan edits
    nested: bool = False  # the vendor wraps what it returns and sends in named schemas: place their changes on operations
    # Every question the reader (and the fixer) may ask the index, with its number of positional arguments:
    # ``app.inventory`` records them so a release can be checked from a repo's index without its code. A
    # dotted name asks an object the index holds (``go.resolve``). Each one must be a pure lookup.
    questions: dict[str, int] = field(default_factory=dict)


STRIPE_QUESTIONS = {
    "resolve": 1, "match_path": 1, "methods_for": 1,
    "go.resolve": 3, "go.known": 3, "go.owns": 2, "go.field": 3, "go.children": 2, "go.path_param": 2,
}
PLAID_QUESTIONS = {
    "method": 1, "legacy": 1, "endpoint": 2, "match_path": 1, "methods_for": 1, "knows": 2, "has_children": 2,
    "required": 4,
}

PROFILES: dict[str, Profile] = {
    "stripe": Profile("stripe", SdkIndex.build, Reader(), fixes=True, questions=STRIPE_QUESTIONS),
    "plaid": Profile("plaid", PlaidIndex.build, plaidscan.READER, nested=True, questions=PLAID_QUESTIONS),
}


def get_profile(name: str) -> Profile:
    try:
        return PROFILES[name]
    except KeyError:
        known = ", ".join(sorted(PROFILES))
        raise SystemExit(f"apidrift cannot read {name} call sites yet; it reads: {known}") from None
