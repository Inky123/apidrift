"""The names a Python file binds to the Stripe SDK by importing it: ``import stripe as s`` (``s`` -> ``stripe``),
``import stripe.error`` (``stripe`` -> ``stripe``), ``from stripe import Charge`` (``Charge`` -> ``stripe.Charge``).
Kept apart from :mod:`pyparse` so the parser stays small."""

from __future__ import annotations

import ast


def import_aliases(tree: ast.AST) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "stripe" or alias.name.startswith("stripe."):
                    aliases[alias.asname or alias.name.split(".")[0]] = alias.name if alias.asname else "stripe"
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == "stripe":
            for alias in node.names:
                if alias.name != "*":
                    aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return aliases
