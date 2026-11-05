"""What a stripe-go params value sends: the composite literal, and the variable that carries it to the call.

A params struct is read from ``&stripe.XParams{...}`` (nested ``&stripe.Y{...}`` and ``[]*stripe.Z{{...}}``
included). When the call gets a variable, the value in force is the nearest binding whose block also
holds the call; ``params.Field = v`` statements between that binding and the call add their field, and
anything that could change the value out of sight makes the call incomplete (``hidden`` gets ``""``):
a parameter, a binding in a branch, a use the walk cannot classify (passed on, aliased, an unknown
method), a closure boundary, a later write inside a loop around the call. Go has no spread, so a
literal that is visible is visible whole.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .golex import ASSIGN_OPS, BLOCK, ID, GoSource
from .gomap import GoIndex, Op, snake_guess

SDK_ONLY = {"Params", "ListParams", "Context", "UnsetFields", "StripeAccount", "IdempotencyKey", "Headers", "Extra"}
# params methods that set no request field of their own; the two ``Add`` helpers fill the field named here
PARAM_METHODS = {
    "AddExpand": "Expand", "AddMetadata": "Metadata", "SetIdempotencyKey": "", "SetStripeAccount": "",
    "AddUnsetField": "",
}
# ``form:"-"`` flags stripe-go encodes into a sibling field: ``BillingCycleAnchorNow`` sends
# billing_cycle_anchor=now, and ``CreatedRange`` is the range form of ``created``
SENTINELS = (
    "Now", "Unchanged", "MaxBilledUntil", "MaxPeriodEnd", "MinPeriodEnd", "CurrentPeriodEnd", "Minimum", "Inf", "Range",
)
RANGE = {"GreaterThan": "gt", "GreaterThanOrEqual": "gte", "LesserThan": "lt", "LesserThanOrEqual": "lte"}
COMPARE = {"==", "!="}


@dataclass(frozen=True)
class Entry:
    """One place a request field is written: a ``Key: value`` of a literal or a ``p.Key = value`` statement."""

    path: str
    key: int  # token of the Go field name
    a: int  # value tokens [a, b)
    b: int
    lit: int  # ``{`` of the literal holding it; -1 for an assignment statement
    struct: str  # struct type the field belongs to ("" when the source does not name it)
    exact: bool  # the Go name is the request field's own name, not a sentinel flag or a guess
    stmt: int = -1  # first token of an assignment statement
    conditional: bool = False


@dataclass
class Params:
    fields: set[str] = field(default_factory=set)
    hidden: set[str] = field(default_factory=set)
    entries: list[Entry] = field(default_factory=list)
    var: str | None = None  # the variable that carries the params to the call
    struct: str = ""  # the struct type that variable holds
    shared: bool = False  # that variable also feeds another resolved call
    reads: list[tuple[str, int]] = field(default_factory=list)  # (field path, token): the variable's field is read


@dataclass
class Decl:
    tok: int
    end: int  # the name is in scope after this token
    lo: int  # scope: token span (lo, hi)
    hi: int
    value: tuple[int, int] | None = None  # value tokens; None when the value is not written here
    zero: str | None = None  # ``var p stripe.T``: the struct type of the zero value
    declares: bool = True  # False for a plain ``p = ...``


class Vars:
    """Declarations and uses of plain variables in one file."""

    def __init__(self, src: GoSource, roots: set[str]) -> None:
        self.src, self.roots = src, roots
        self._decls: dict[str, list[Decl]] = {}
        self._params: dict[int, set[str]] = {}  # function body -> names its signature declares
        self._for: dict[tuple[str, int], Decl | None] = {}
        for body, func in src.funcs.items():
            self._params[body] = {
                src.tok[j] for j in range(func + 1, body)
                if src.kind[j] == ID and src.tok[j - 1] != "." and src.tok[j + 1] != "."
            }

    # --- declarations ---------------------------------------------------------------

    def decls(self, name: str) -> list[Decl]:
        found = self._decls.get(name)
        if found is None:
            found = self._decls[name] = sorted(self._find(name), key=lambda d: d.tok)
        return found

    def _find(self, name: str) -> list[Decl]:
        src, out = self.src, []
        for body, names in self._params.items():
            if name in names:
                out.append(Decl(body, body, body, src.match[body] if src.match[body] >= 0 else src.n))
        for i in src.ids(name):
            if i and src.tok[i - 1] == ".":
                continue
            lo, hi = src.region(i)
            if i and src.tok[i - 1] == "var":
                end = self._decl_end(i)
                eq = next((j for j in range(i + 1, end) if src.tok[j] == "=" and src.parent[j] == src.parent[i]), None)
                zero = None
                if eq is None and end - i == 4 and src.tok[i + 1] in self.roots and src.tok[i + 2] == ".":
                    zero = src.tok[i + 3]
                out.append(Decl(i, end, lo, hi, (eq + 1, end) if eq is not None else None, zero))
                continue
            first, last = self._targets(i)
            if first is None or not src.starts_stmt(first):
                continue
            op = src.tok[last + 1] if last + 1 < src.n else ""
            if op not in (":=", "="):
                continue
            end = self._decl_end(last + 2)
            single = first == last
            if op == ":=" and first and src.tok[first - 1] in ("if", "for", "switch"):
                block = self._header_block(last + 2)  # ``if x := f(); cond {``: scoped to the statement
                hi = self._chain_end(block) if block is not None else hi
            out.append(Decl(i, end, lo, hi, (last + 2, end) if single else None, declares=op == ":="))
        return out

    def _header_block(self, j: int) -> int | None:
        src = self.src
        while j < src.n and src.tok[j] not in (")", "]", "}"):
            if src.tok[j] == "{" and src.brace.get(j) == BLOCK and src.head.get(j) != "func":
                return j
            j = src._skip(j)
        return None

    def _targets(self, i: int) -> tuple[int | None, int]:
        """First and last token of the plain ``a, b, c`` target list holding identifier ``i``."""
        src = self.src
        first = i
        while first >= 2 and src.tok[first - 1] == "," and src.kind[first - 2] == ID:
            first -= 2
        if first and src.tok[first - 1] in (".", ","):
            return None, i
        last = i
        while last + 2 < src.n and src.tok[last + 1] == "," and src.kind[last + 2] == ID:
            last += 2
        return first, last

    def _decl_end(self, i: int) -> int:
        """Like ``stmt_end``, but a statement header also ends at its block (``for x := range xs {``)."""
        src, j = self.src, i
        while j < src.n:
            if src.kind[j] == ";" or src.tok[j] in (")", "]", "}"):
                return j
            if src.tok[j] == "{" and src.brace.get(j) == BLOCK and src.head.get(j) != "func":
                return j
            j = src._skip(j)
        return src.n

    def _chain_end(self, block: int) -> int:
        """End of an ``if``/``else if``/``else`` chain (or a loop body) that starts with ``block``."""
        src = self.src
        end = src.match[block]
        while 0 <= end < src.n - 1 and src.tok[end + 1] == "else":
            j = end + 2
            while j < src.n and not (src.tok[j] == "{" and src.brace.get(j) == BLOCK):
                j = src._skip(j)
            if j >= src.n or src.match[j] < 0:
                break
            end = src.match[j]
        return end if end >= 0 else src.n

    def decl_for(self, name: str, t: int) -> Decl | None:
        """The declaration of ``name`` that an identifier at token ``t`` refers to."""
        key = (name, t)
        if key not in self._for:
            self._for[key] = next(  # the nearest one before ``t`` whose scope holds it
                (d for d in reversed(self.decls(name))
                 if d.declares and d.tok <= t and (d.tok == t or (d.end <= t and d.lo < t < d.hi))),
                None,
            )
        return self._for[key]

    def shadowed(self, name: str, t: int) -> bool:
        """A local variable or parameter called ``name`` is in scope at ``t`` (so it is not the package)."""
        d = self.decl_for(name, t)
        return d is not None and d.tok != t


class Reader:
    """Reads params values for one operation into :class:`Params`."""

    def __init__(self, src: GoSource, go: GoIndex, op: Op, roots: set[str]) -> None:
        self.src, self.go, self.op, self.roots = src, go, op, roots

    def name(self, prefix: str, go_name: str) -> tuple[str | None, bool]:
        """(request field path, exact) for a struct field under ``prefix``; path None for SDK-only fields."""
        path = self.go.field(self.op, prefix, go_name)
        if path:
            return path, True
        if go_name in SDK_ONLY or (not prefix and self.go.path_param(self.op, go_name)):
            return None, False
        for suffix in SENTINELS:
            if go_name.endswith(suffix) and len(go_name) > len(suffix):
                base = self.go.field(self.op, prefix, go_name[: -len(suffix)])
                if base:
                    return base, False
        return prefix + RANGE.get(go_name, snake_guess(go_name)), False

    def typed(self, j: int, b: int) -> tuple[str, int] | None:
        """(struct type, ``{`` token) when tokens ``[j, b)`` are exactly ``stripe.T{...}``."""
        src = self.src
        if j + 3 < b and src.tok[j] in self.roots and src.tok[j + 1] == "." and src.kind[j + 2] == ID:
            if src.tok[j + 3] == "{" and src.match[j + 3] == b - 1 and (j == 0 or src.tok[j - 1] != "."):
                return src.tok[j + 2], j + 3
        return None

    def literal(self, open_: int, struct: str, prefix: str, out: Params) -> None:
        src = self.src
        for a, b in src.split(open_ + 1, src.match[open_]):
            a, b = src.trim(a, b)
            if b - a < 3 or src.kind[a] != ID or src.tok[a + 1] != ":":
                out.hidden.add(prefix)  # an unkeyed element: which field it fills is not written here
                continue
            path, exact = self.name(prefix, src.tok[a])
            if path is None:
                continue
            out.fields.add(path)
            out.entries.append(Entry(path, a, a + 2, b, open_, struct, exact))
            self.value(a + 2, b, path, out)

    def value(self, a: int, b: int, path: str, out: Params) -> None:
        """Nested fields of a value: a struct literal, or a slice literal of structs. Anything else is a leaf."""
        src = self.src
        j = a + 1 if src.tok[a] == "&" else a
        found = self.typed(j, b)
        if found:
            self.literal(found[1], found[0], path + ".", out)
            return
        if b - a > 3 and src.tok[a] == "[" and src.tok[a + 1] == "]":
            j = a + 3 if src.tok[a + 2] == "*" else a + 2
            found = self.typed(j, b)
            if found:
                self.elements(found[1] + 1, src.match[found[1]], found[0], path, out)
        elif b - a > 3 and src.tok[a] == "append" and src.tok[a + 1] == "(" and src.match[a + 1] == b - 1:
            parts = src.split(a + 2, b - 1)
            self.elements(parts[0][1] + 1, b - 1, "", path, out) if parts else None

    def elements(self, a: int, b: int, struct: str, path: str, out: Params) -> None:
        src = self.src
        for ea, eb in src.split(a, b):
            ea, eb = src.trim(ea, eb)
            k = ea + 1 if src.tok[ea] == "&" else ea
            found = self.typed(k, eb)
            if found:
                self.literal(found[1], found[0], path + "[].", out)
            elif struct and src.tok[k] == "{" and src.match[k] == eb - 1:
                self.literal(k, struct, path + "[].", out)
            elif self.go.children(self.op, path + "[]."):
                out.hidden.add(path + "[].")  # an element object built elsewhere

    def whole(self, a: int, b: int) -> tuple[str, int | None] | None:
        """(struct, ``{`` or None) when value ``[a, b)`` is a visible params struct: a literal or ``new(stripe.T)``."""
        src = self.src
        a, b = src.trim(a, b)
        if a >= b:
            return None
        found = self.typed(a + 1 if src.tok[a] == "&" else a, b)
        if found:
            return found
        if b - a == 6 and src.tok[a] == "new" and src.tok[a + 1] == "(" and src.tok[a + 2] in self.roots:
            if src.tok[a + 3] == "." and src.kind[a + 4] == ID and src.tok[a + 5] == ")":
                return src.tok[a + 4], None
        return None


def chain_after(src: GoSource, t: int) -> tuple[list[int], int]:
    """Selector tokens of ``name.A.B`` starting at identifier ``t``, and the token after the chain."""
    toks, k = [], t + 1
    while k + 1 < src.n and src.tok[k] == "." and src.kind[k + 1] == ID:
        toks.append(k + 1)
        k += 2
    return toks, k


def assign_op(src: GoSource, k: int) -> bool:
    return k < src.n and src.tok[k] in ASSIGN_OPS and src.tok[k] != ":="
