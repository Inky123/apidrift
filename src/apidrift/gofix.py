"""Documented migrations in Go source that uses stripe-go.

Go request fields are typed struct fields, so an edit is right only if the struct that stripe-go ships for
the new API version really has the new shape. ``GO_RULES`` therefore lists, per migration, the exact struct
types and field names, each checked against the stripe-go source: v86.0.0 pins Stripe API
2026-05-27.dahlia and v87.0.0 pins 2026-09-30.endive (``api_version.go``); the fields below are the ones
that differ between those two tags (``tests/fixtures/stripe_go_structs.json`` keeps the extract).

- ``PaymentMethodTypes []*string`` is gone and ``AllowedPaymentMethodTypes []*string`` exists on the
  payment intent, setup intent and checkout session params: the identifier is renamed, the value stays.
- ``PaymentMethodUpdateParams.Payto`` is gone; ``PaymentMethodParams.Payto`` stays because the same struct
  creates payment methods. The entry is deleted, only when its value is a literal that names no variable.
- ``Countries []*string`` became ``Country *string`` on the Financial Connections filters: a slice literal
  becomes its first element.
- ``SubscriptionResumeParams.BillingCycleAnchor`` went from ``*string`` to
  ``*SubscriptionResumeBillingCycleAnchorParams{Type}``, and the invoice preview's from ``*int64`` plus the
  ``BillingCycleAnchorNow`` / ``BillingCycleAnchorUnchanged`` flags to
  ``*InvoiceCreatePreviewSubscriptionDetailsBillingCycleAnchorParams{Type, Timestamp}``.
- Subscription updates are NOT rewritten: ``SubscriptionParams`` / ``SubscriptionUpdateParams`` still take
  ``BillingCycleAnchorNow`` in v87.0.0, so there is no new Go shape to write (``sdk_no_rewrite``).

Anything outside these exact shapes is left for a person. A Go file cannot be compiled here, so every
edited file must still tokenise with balanced brackets, and no use of an old field name may remain.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import goparse
from .fixvalue import EMPTY, LEAVE, UNSUPPORTED, Edit
from .golex import ID, LIT, GoSource
from .govars import Entry
from .migrations import Migration

STRIPE_GO = "stripe-go v87"  # the first major whose structs have the shapes written here
NO_REWRITE = "sdk_no_rewrite"
PURE = {"nil", "true", "false", "string", "int", "int64", "int32", "float64", "bool", "byte", "map", "any"}
INITIALISMS = {
    "id": "ID", "ids": "IDs", "url": "URL", "uri": "URI", "ip": "IP", "api": "API", "tos": "TOS", "pdf": "PDF",
    "vat": "VAT", "sdk": "SDK", "ui": "UI", "tls": "TLS", "pin": "PIN", "dob": "DOB", "cvc": "CVC", "ssn": "SSN",
    "iban": "IBAN", "mcc": "MCC", "us": "US", "ach": "ACH", "sepa": "SEPA", "bacs": "BACS", "becs": "BECS",
    "acss": "ACSS", "avs": "AVS", "atm": "ATM", "pii": "PII",
}


def go_name(field: str) -> str:
    """stripe-go's spelling of a request field: each word capitalised, known initialisms upper-cased.
    Observed on stripe-go v87.0.0's ``form`` tags; brand and country words there have their own spellings,
    which is why a fix takes its names from ``GO_RULES`` and uses this only as a cross-check."""
    return "".join(INITIALISMS.get(part, part.capitalize()) for part in field.split("_"))


@dataclass(frozen=True)
class GoRule:
    old: str  # the struct field today
    new: str  # the struct field in stripe-go v87 ("" when it is removed)
    structs: frozenset[str]  # params struct types that carry the field for this endpoint
    wrapper: str = ""  # wrap_type: the struct the value becomes
    flags: tuple[tuple[str, str], ...] = ()  # wrap_type: (flag field, the type it stood for), e.g. BillingCycleAnchorNow


def _rename(*structs: str) -> GoRule:
    return GoRule("PaymentMethodTypes", "AllowedPaymentMethodTypes", frozenset(structs))


GO_RULES: dict[tuple[str, str], GoRule] = {
    ("/v1/payment_intents", "payment_method_types"): _rename("PaymentIntentParams", "PaymentIntentCreateParams"),
    ("/v1/payment_intents/{intent}", "payment_method_types"): _rename("PaymentIntentParams", "PaymentIntentUpdateParams"),
    ("/v1/payment_intents/{intent}/confirm", "payment_method_types"): _rename("PaymentIntentConfirmParams"),
    ("/v1/setup_intents", "payment_method_types"): _rename("SetupIntentParams", "SetupIntentCreateParams"),
    ("/v1/setup_intents/{intent}", "payment_method_types"): _rename("SetupIntentParams", "SetupIntentUpdateParams"),
    ("/v1/checkout/sessions", "payment_method_types"): _rename("CheckoutSessionParams", "CheckoutSessionCreateParams"),
    ("/v1/payment_methods/{payment_method}", "payto"): GoRule(
        "Payto", "", frozenset({"PaymentMethodParams", "PaymentMethodUpdateParams"})
    ),
    ("/v1/financial_connections/sessions", "filters.countries"): GoRule(
        "Countries", "Country",
        frozenset({"FinancialConnectionsSessionFiltersParams", "FinancialConnectionsSessionCreateFiltersParams"}),
    ),
    ("/v1/subscriptions/{subscription}/resume", "billing_cycle_anchor"): GoRule(
        "BillingCycleAnchor", "BillingCycleAnchor", frozenset({"SubscriptionResumeParams"}),
        wrapper="SubscriptionResumeBillingCycleAnchorParams",
    ),
    ("/v1/invoices/create_preview", "subscription_details.billing_cycle_anchor"): GoRule(
        "BillingCycleAnchor", "BillingCycleAnchor", frozenset({"InvoiceCreatePreviewSubscriptionDetailsParams"}),
        wrapper="InvoiceCreatePreviewSubscriptionDetailsBillingCycleAnchorParams",
        flags=(("BillingCycleAnchorNow", "now"), ("BillingCycleAnchorUnchanged", "unchanged")),
    ),
}


class GoLocator:
    """Edit sites for the usages of one Go file, parsing it once (the Go half of ``fixedit.Locator``)."""

    def __init__(self, text: str, index) -> None:
        self.text = text
        self.calls, _, self.file = goparse.calls(text, index.go) if index.go is not None else ([], [], None)

    def sites(self, usage, m: Migration):
        from .fixedit import Sites  # fixedit dispatches here

        matches = [
            c for c in self.calls
            if (c.line, c.call, c.resolved.path, c.resolved.method) == (usage.line, usage.call, usage.path, usage.method)
        ]
        if not matches:
            return Sites(found_call=False)
        c, out = matches[0], Sites()
        if any(m.target.startswith(prefix) for prefix in c.params.hidden):
            out.incomplete = True
            return out
        rule = GO_RULES.get((m.path, m.target))
        for entry in c.params.entries:
            if entry.path == m.target:
                self._pad = []
                out.add(self._edit(entry, m, rule) if rule is not None else NO_REWRITE)
                if out.edits and out.why is None and not out.unsupported:
                    out.edits += self._pad  # sibling lines re-padded so the block stays column-aligned
        if rule is not None and m.action == "rename" and c.params.struct in rule.structs and out.edits:
            src = self.file.src  # the same variable's field read elsewhere (``p.F = append(p.F, x)``): same struct
            out.edits += [
                Edit(src.start[t], src.start[t] + len(rule.old), rule.new, m.target)
                for path, t in c.params.reads if path == m.target and src.tok[t] == rule.old
            ]
        return out

    def _edit(self, e: Entry, m: Migration, rule: GoRule):
        src, key = self.file.src, self.file.src.tok[e.key]
        flag = dict(rule.flags).get(key)
        if e.struct not in rule.structs or (key != rule.old and flag is None):
            return NO_REWRITE  # another struct type, or a field stripe-go encodes its own way
        a, b = src.trim(e.a, e.b)
        key_at, value_end = src.start[e.key], src.end(b - 1)
        value = self.text[src.start[a] : value_end]
        root = src.tok[a] if src.tok[a] in self.file.roots else (src.tok[a + 1] if b - a > 1 else "")
        if m.action == "rename":
            pad = self._realign(e, rule.new, m)
            if pad is not None and pad != self.text[src.end(e.key + 1) : src.start[a]]:
                self._pad.append(Edit(src.end(e.key + 1), src.start[a], pad, m.target))
            return Edit(key_at, key_at + len(key), rule.new, m.target)
        if m.action == "delete":
            return self._delete(e, a, b, m)
        if m.action == "rename_first":
            first = self._first(a, b)
            pad = self._realign(e, rule.new, m) or " "
            return Edit(key_at, value_end, f"{rule.new}:{pad}{first}", m.target) if first else UNSUPPORTED
        wrapped = f"&{root}.{rule.wrapper}"
        if value.startswith(wrapped + "{"):
            return LEAVE
        inner = self._helper_call(a, b)
        if inner is None or root not in self.file.roots:
            return UNSUPPORTED
        helper = src.tok[a + 2]
        if flag is not None:  # BillingCycleAnchorNow: stripe.Bool(true)
            if helper != "Bool" or inner != "true":
                return UNSUPPORTED
            pad = self._realign(e, rule.old, m) or " "
            return Edit(key_at, value_end, f'{rule.old}:{pad}{wrapped}{{Type: {root}.String("{flag}")}}', m.target)
        if helper == "String" and not m.timestamps:
            if not (m.wrap_any or inner.strip('"') in m.wrap_values):
                return UNSUPPORTED
            return Edit(src.start[a], value_end, f"{wrapped}{{Type: {value}}}", m.target)
        if helper == "Int64" and m.timestamps:
            text = f'{wrapped}{{Type: {root}.String("timestamp"), Timestamp: {value}}}'
            return Edit(src.start[a], value_end, text, m.target, timestamp=True)
        return UNSUPPORTED

    def _realign(self, e: Entry, new_key: str, m: Migration) -> str | None:
        """Padding after ``new_key:`` that keeps a column-aligned block of one-line entries aligned (the layout
        gofmt gives a struct literal), queueing the padding edits its sibling lines need. None when the entry
        is not in such a block, so the text between key and value is left exactly as written."""
        src, text = self.file.src, self.text
        if e.lit < 0:
            return None
        rows = []  # (key token, pad start, pad end, line) of the one-line ``Key: value`` entries
        for s in src.split(e.lit + 1, src.match[e.lit]):
            a, b = src.trim(*s)
            keyed = b - a >= 3 and src.kind[a] == ID and src.tok[a + 1] == ":"
            one_line = keyed and src.line(a) == src.line(b - 1) and not text[text.rfind("\n", 0, src.start[a]) + 1 : src.start[a]].strip()
            rows.append((a, src.end(a + 1), src.start[a + 2], src.line(a)) if one_line else None)
        at = next((k for k, r in enumerate(rows) if r is not None and r[0] == e.key), None)
        if at is None:
            return None
        lines = text.split("\n")
        lo = hi = at
        while lo > 0 and rows[lo - 1] is not None and self._adjacent(lines, rows[lo - 1][3], rows[lo][3]):
            lo -= 1
        while hi + 1 < len(rows) and rows[hi + 1] is not None and self._adjacent(lines, rows[hi][3], rows[hi + 1][3]):
            hi += 1
        block = rows[lo : hi + 1]
        width = max(len(src.tok[r[0]]) for r in block)
        if width > 40 or len(new_key) > 40 or any(text[r[1] : r[2]] != " " * (width - len(src.tok[r[0]]) + 1) for r in block):
            return None  # not laid out the way gofmt aligns it: leave the spacing alone
        new_width = max(len(new_key) if r[0] == e.key else len(src.tok[r[0]]) for r in block)
        for r in block:
            if r[0] != e.key and new_width != width:
                self._pad.append(Edit(r[1], r[2], " " * (new_width - len(src.tok[r[0]]) + 1), m.target))
        return " " * (new_width - len(new_key) + 1)

    @staticmethod
    def _adjacent(lines: list[str], a: int, b: int) -> bool:
        """Lines ``a`` and ``b`` (1-based) are neighbours, or only comment lines sit between them."""
        return all(lines[k - 1].strip().startswith("//") for k in range(a + 1, b))

    def _helper_call(self, a: int, b: int) -> str | None:
        """The argument text when value ``[a, b)`` is exactly ``stripe.Helper(arg)``."""
        src = self.file.src
        if b - a >= 5 and src.tok[a] in self.file.roots and src.tok[a + 1] == "." and src.tok[a + 3] == "(":
            if src.match[a + 3] == b - 1:
                return self.text[src.end(a + 3) : src.start[b - 1]].strip()
        return None

    def _first(self, a: int, b: int) -> str | None:
        """``stripe.StringSlice([]string{X, ...})`` -> ``stripe.String(X)``; ``[]*string{E, ...}`` -> ``E``."""
        src = self.file.src
        toks = src.tok
        if self._helper_call(a, b) is not None and toks[a + 2] == "StringSlice":
            if toks[a + 4 : a + 8] == ["[", "]", "string", "{"] and src.match[a + 7] == b - 2:
                parts = src.split(a + 8, b - 2)
                if parts:
                    x, y = src.trim(*parts[0])
                    return f"{toks[a]}.String({self.text[src.start[x] : src.end(y - 1)]})"
        elif toks[a : a + 5] == ["[", "]", "*", "string", "{"] and src.match[a + 4] == b - 1:
            parts = src.split(a + 5, b - 1)
            if parts:
                x, y = src.trim(*parts[0])
                return self.text[src.start[x] : src.end(y - 1)]
        return None

    def _delete(self, e: Entry, a: int, b: int, m: Migration):
        """Remove the entry (or the whole ``p.Field = value`` statement) when nothing else can depend on it."""
        src, text = self.file.src, self.text
        for j in range(a, b):
            if src.kind[j] == ID and src.tok[j] not in PURE and src.tok[j] not in self.file.roots:
                if src.tok[j - 1] != "." and src.tok[j + 1] != ":":
                    return UNSUPPORTED  # a variable or call: removing its only use may stop the file compiling
        first = e.stmt if e.lit < 0 else e.key
        start, end = src.start[first], src.end(b - 1)
        comma = b if b < src.n and src.tok[b] == "," else None
        if e.lit >= 0:
            keyed = [s for s in src.split(e.lit + 1, src.match[e.lit]) if src.trim(*s)[0] != first]
            if not keyed:
                return EMPTY
            if comma is not None:
                end = src.end(comma)
        line_start = text.rfind("\n", 0, start) + 1
        line_end = text.find("\n", end)
        line_end = len(text) if line_end < 0 else line_end + 1
        tail = text[end:line_end].strip()
        if not text[line_start:start].strip() and (not tail or tail.startswith("//")):
            return Edit(line_start, line_end, "", m.target)  # the entry fills its lines
        if e.lit < 0:
            return UNSUPPORTED
        if comma is not None:
            while end < len(text) and text[end] in " \t":
                end += 1
            return Edit(start, end, "", m.target)
        prev = first - 1  # the last entry of a one-line literal: take the comma before it
        if src.tok[prev] != ",":
            return UNSUPPORTED
        return Edit(src.start[prev], end, "", m.target)


def balanced(before: str, after: str) -> bool:
    """Both texts tokenise with every bracket closed, and the edit left top-level structure in place."""
    a, b = GoSource(before), GoSource(after)
    return _closed(a) and _closed(b) and _top_level(a) == _top_level(b)


def _closed(src: GoSource) -> bool:
    brackets = [i for i in range(src.n) if src.kind[i] == "op" and src.tok[i] in "()[]{}"]
    return all(src.match[i] >= 0 for i in brackets)


def _top_level(src: GoSource) -> list[str]:
    """Names of the file's top-level declarations, in order (an edit never adds or removes one)."""
    return [
        src.tok[i + 1] for i in range(src.n - 1)
        if src.parent[i] == -1 and src.kind[i] == ID and src.tok[i] in ("func", "type", "var", "const", "package")
    ]


def oracle_tokens(before: str, after: str, planned: list, edits: list[Edit]) -> tuple[str | None, list[int]]:
    """Go's half of ``fixer._oracle_tokens``: the text outside the edits is unchanged, each edit removes
    exactly the field tokens it covers, and no use of a removed field is left anywhere in the file
    (it would no longer compile). A wrapped field keeps its name, so only its other uses count."""
    from .fixer import _layout  # fixer dispatches here

    identical, placed = _layout(before, after, edits)
    stray = not identical
    landed = {span[0] for span in placed.values()}  # where each edit's new text starts in ``after``
    leftovers: list[int] = []
    src_b, src_a = GoSource(before), GoSource(after)
    names: dict[str, bool] = {}  # Go field name -> it stays in the file (a wrapped field keeps its name)
    for _, m, _ in planned:
        rule = GO_RULES.get((m.path, m.target))
        if rule is None:
            return "leftover_tokens", []
        names[rule.old] = names.get(rule.old, True) and m.action == "wrap_type"
        names.update({flag: False for flag, _ in rule.flags})
    for name in sorted(names):
        found = [src_b.start[i] for i in src_b.ids(name)]
        remaining = src_a.ids(name)
        pattern = re.compile(rf"(?<!\w){re.escape(name)}(?!\w)")
        expected = sum(
            sum(1 for pos in found if e.start <= pos < e.end) - len(pattern.findall(e.text)) for e in set(edits)
        )
        if len(found) - len(remaining) != expected:
            stray = True
        if names[name]:
            ours = [  # a literal key, or the ``p.Field = `` whose value this plan rewrote
                i for i in remaining
                if (src_a.tok[i + 1] == ":" and src_a.brace.get(src_a.parent[i]) == LIT)
                or (src_a.tok[i + 1] == "=" and src_a.start[i + 2] in landed)
            ]
            stray = stray or len(ours) != len(remaining)  # read or written through another value
        elif remaining:
            stray = True
            leftovers += [src_a.line(i) for i in remaining]
    return ("leftover_tokens" if stray else None), sorted(set(leftovers))
