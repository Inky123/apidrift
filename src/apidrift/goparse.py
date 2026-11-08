"""Pull Stripe calls and their request fields out of Go source that uses stripe-go.

A file is read only when it imports stripe-go (any major version, aliased or not). Three call styles
resolve through :class:`gomap.GoIndex`:

* ``paymentintent.New(params)``: the receiver is the local name of an imported stripe-go sub-package
  that no variable shadows at the call;
* ``sc.PaymentIntents.New(params)`` / ``c.client.Customers.Get(id, nil)`` (``client.API``): the last two
  names are a client field and one of its methods; a call whose first argument is a ``context`` or that
  has more than two arguments is not stripe-go's signature and is skipped;
* ``sc.V1PaymentIntents.Create(ctx, params)`` (``stripe.Client``).

The params are always the last argument (:mod:`govars` reads them). A webhook handler that imports
stripe-go only for ``stripe.Event`` has no such call and yields nothing.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass

from .golex import ID, STR, GoSource
from .gomap import API, PACKAGE, SERVICE, GoIndex, snake_guess
from .govars import COMPARE, PARAM_METHODS, Decl, Entry, Params, Reader, Vars, assign_op, chain_after
from .sdkmap import Resolved

IMPORTS_STRIPE = re.compile(r"github\.com/stripe/stripe-go")
STRIPE_IMPORT = re.compile(r"github\.com/stripe/stripe-go(?:/v\d+)?(?:/(.+))?")
CONTEXT_ARG = re.compile(r"(?:\w*[cC]tx|\w*[cC]ontext|context\.\w+\(\)|.*\.Context\(\))")
QUIET_PACKAGES = {"form"}  # stripe-go sub-packages whose calls are not worth listing
GENERIC = {"req", "request", "r", "w", "ctx", "opts", "options", "cfg", "config", "c", "e", "event"}  # not a response


@dataclass
class GoCall:
    """One resolved stripe-go call and what its params argument sends."""

    line: int
    call: str
    resolved: Resolved
    pos: int  # token of the call's ``(``
    params: Params
    file: "GoFile"


@dataclass
class GoFound:
    line: int
    call: str
    resolved: Resolved
    fields: set[str]
    complete: bool
    hidden: frozenset[str]


class GoFile:
    """One Go file: tokens, stripe-go imports and variable declarations."""

    def __init__(self, text: str, go: GoIndex) -> None:
        self.src, self.go = GoSource(text), go
        self.roots: set[str] = set()  # local names of the stripe-go root package
        self.pkgs: dict[str, str] = {}  # local name -> sub-package path (``checkout/session``)
        self._imports()
        self.vars = Vars(self.src, self.roots)
        self.param_uses: set[int] = set()  # variable tokens passed as the params of a resolved call

    def _imports(self) -> None:
        src = self.src
        for i in src.ids("import"):
            if src.parent[i] != -1 or i + 1 >= src.n:
                continue
            if src.tok[i + 1] == "(" and src.match[i + 1] >= 0:
                lo, hi = i + 2, src.match[i + 1]
            else:
                lo, hi = i + 1, src.stmt_end(i + 1)
            for j in range(lo, hi):
                if src.kind[j] != STR:
                    continue
                m = STRIPE_IMPORT.fullmatch(src.tok[j][1:-1])
                if not m:
                    continue
                alias = src.tok[j - 1] if j > lo and src.kind[j - 1] != ";" else ""
                sub = m.group(1) or ""
                if alias in (".", "_"):
                    continue
                local = alias or (sub.rsplit("/", 1)[-1] if sub else "stripe")
                if sub:
                    self.pkgs[local] = sub
                else:
                    self.roots.add(local)

    def chain(self, paren: int) -> tuple[list[str], int]:
        """Names of the call chain that ends at ``paren`` and its first token (calls and indexes mid-chain pass)."""
        src = self.src
        parts, j = [src.tok[paren - 1]], paren - 1
        while j >= 2 and src.tok[j - 1] == ".":
            k = j - 2
            if src.tok[k] in (")", "]") and src.match[k] > 0 and src.kind[src.match[k] - 1] == ID:
                k = src.match[k] - 1
            elif src.kind[k] != ID:
                break
            parts.append(src.tok[k])
            j = k
        return parts[::-1], j

    def text_of(self, a: int, b: int) -> str:
        return self.src.text[self.src.start[a] : self.src.end(b - 1)] if b > a else ""

    # --- params -----------------------------------------------------------------------

    def read(self, paren: int, resolved: Resolved, index: int = -1) -> Params:
        """What the call's argument at ``index`` (the params: the last one) sends to ``resolved``."""
        src, out = self.src, Params()
        args = src.split(paren + 1, src.match[paren])
        if not args or index >= len(args):
            return out
        a, b = src.trim(*args[index])
        reader = Reader(src, self.go, (resolved.method, resolved.path), self.roots)
        if b - a == 1 and src.tok[a] == "nil":
            return out
        j = a + 1 if src.tok[a] == "&" else a
        found = reader.typed(j, b)
        if found:
            reader.literal(found[1], found[0], "", out)
        elif b - j == 1 and src.kind[j] == ID:
            self._variable(src.tok[j], j, reader, out)
        else:
            out.hidden.add("")  # a call, a struct field, a conversion: built somewhere else
        return out

    def _variable(self, name: str, use: int, reader: Reader, out: Params) -> None:
        src, vars_ = self.src, self.vars
        out.var = name
        decl = vars_.decl_for(name, use)
        if decl is None:
            out.hidden.add("")
            return
        events = [decl] + [
            d for d in vars_.decls(name)
            if not d.declares and decl.lo < d.tok < use and vars_.decl_for(name, d.tok) is decl
        ]
        fn = src.func_of(use)
        if fn >= 0 and any(fn < g < src.match[fn] for g in src.ids("goto")):
            out.hidden.add("")  # a jump: statement order no longer says what ran before the call
        dom = None
        for e in sorted(events, key=lambda e: e.tok, reverse=True):
            lo, hi = src.region(e.tok)
            if e.tok < use and lo < use < hi and src.func_of(e.tok) == fn:
                dom = e
                break
        if dom is None:  # a parameter, a closure's outer variable, or bound only inside branches
            out.hidden.add("")
            for e in events:
                self._event(e, reader, out)  # what the visible branches send is still worth reporting
            return
        struct = self._event(dom, reader, out)
        if struct is None:
            out.hidden.add("")
        out.struct = struct or ""
        for e in events:
            if dom.tok < e.tok < use:  # rebound in a branch between the binding and the call
                out.hidden.add("")
                self._event(e, reader, out)
        skip = {e.tok for e in events} | {use}
        after: list[int] = []
        mentions = src.ids(name)
        for t in mentions[bisect.bisect_right(mentions, dom.tok) : bisect.bisect_left(mentions, decl.hi)]:
            if t in skip or src.tok[t - 1] == "." or vars_.decl_for(name, t) is not decl:
                continue
            if t < use:
                self._use(t, use, struct or "", reader, out)
            else:
                after.append(t)
                self._read(t, reader, out)
        for loop in src.loops_around(use):
            if not (loop < dom.tok < src.match[loop]) and any(t < src.match[loop] and self._writes(t) for t in after):
                out.hidden.add("")  # written after the call, inside a loop that runs the call again

    def _event(self, e: Decl, reader: Reader, out: Params) -> str | None:
        """Read the value a binding gives the variable; its struct type, or None when it is not a visible struct."""
        if e.zero:
            return e.zero
        whole = reader.whole(*e.value) if e.value is not None else None
        if whole is None:
            return None
        if whole[1] is not None:
            reader.literal(whole[1], whole[0], "", out)
        return whole[0]

    def _use(self, t: int, use: int, struct: str, reader: Reader, out: Params) -> None:
        """Fold one mention of the params variable between its binding and the call into ``out``."""
        src = self.src
        sel, k = chain_after(src, t)
        if not sel:
            if t in self.param_uses:
                out.shared = True  # the params of another Stripe call: stripe-go does not change them
            else:
                out.hidden.add("")  # passed on, aliased, returned, dereferenced
            return
        if src.tok[k] == "(":
            method = src.tok[sel[0]] if len(sel) == 1 else ""
            if method not in PARAM_METHODS:
                out.hidden.add("")  # AddExtra, or a method this reader does not know
            elif PARAM_METHODS[method]:
                path = self.go.field(reader.op, "", PARAM_METHODS[method])
                out.fields.update({path} if path else ())  # AddMetadata(k, v) sends metadata
            return
        after = src.match[k] + 1 if src.tok[k] == "[" and src.match[k] >= 0 else k
        if src.starts_stmt(t) and assign_op(src, after):
            self._write(t, sel, k, after, use, struct, reader, out)
            return
        path = self._read(t, reader, out)
        if src.tok[k] in COMPARE or (src.tok[t - 1] == "(" and src.tok[t - 2] in ("append", "len")):
            return
        if path:  # the field's value is read into something else: what is under it may be changed there
            out.hidden |= {path + ".", path + "[]."}

    def _read(self, t: int, reader: Reader, out: Params) -> str | None:
        """Record a read of ``name.Field`` (not a write, not a method call); the field's path if it has one."""
        src = self.src
        sel, k = chain_after(src, t)
        after = src.match[k] + 1 if sel and src.tok[k] == "[" and src.match[k] >= 0 else k
        if not sel or src.tok[k] == "(" or (src.starts_stmt(t) and assign_op(src, after)):
            return None
        path = reader.name("", src.tok[sel[0]])[0]
        if path:
            out.reads.append((path, sel[0]))
        return path

    def _write(self, t: int, sel: list[int], k: int, op: int, use: int, struct: str, reader: Reader, out: Params) -> None:
        src = self.src
        prefix, exact, paths = "", True, []
        for s in sel:
            path, exact = reader.name(prefix, src.tok[s])
            if path is None:
                return  # params.Context = ctx: not a request field
            paths.append(path)
            prefix = path + "."
        out.fields.update(paths)
        end = src.stmt_end(op + 1)
        lo, hi = src.region(t)
        if src.func_of(t) != src.func_of(use):
            out.hidden.add("")  # written inside a closure: whether it ran before the call is not visible
        if op == k:
            reader.value(op + 1, end, paths[-1], out)
        out.entries.append(Entry(
            paths[-1], sel[-1], op + 1, end, -1, struct if len(sel) == 1 else "", exact, stmt=t,
            conditional=not (lo < use < hi),
        ))

    def _writes(self, t: int) -> bool:
        src = self.src
        sel, k = chain_after(src, t)
        if not sel:
            return t not in self.param_uses
        after = src.match[k] + 1 if src.tok[k] == "[" and src.match[k] >= 0 else k
        return src.tok[k] == "(" or assign_op(src, after)


def calls(text: str, go: GoIndex) -> tuple[list[GoCall], list[tuple[int, str]], GoFile | None]:
    """Resolved stripe-go calls with their params read, the (line, call) pairs that matched no single
    endpoint, and the parsed file (None when the text does not import stripe-go)."""
    if not IMPORTS_STRIPE.search(text):
        return [], [], None
    f = GoFile(text, go)
    src = f.src
    if not f.roots and not f.pkgs:
        return [], [], f
    pending: list[tuple[int, int, str, Resolved]] = []
    unresolved: list[tuple[int, str]] = []
    for paren in range(2, src.n):
        if src.tok[paren] != "(" or src.kind[paren - 1] != ID or src.tok[paren - 2] != "." or src.match[paren] < 0:
            continue
        verb = src.tok[paren - 1]
        if not verb[0].isupper():
            continue
        parts, first = f.chain(paren)
        call = ".".join(parts)
        if len(parts) == 2 and parts[0] in f.pkgs and src.tok[first - 1] != ".":
            sub = f.pkgs[parts[0]]
            if f.vars.shadowed(parts[0], first) or sub.rsplit("/", 1)[-1] in QUIET_PACKAGES:
                continue
            resolved = go.resolve(PACKAGE, sub, verb)
        elif len(parts) < 3 or not parts[-2][0].isupper():
            continue  # client fields are exported; a two-name chain has no client
        elif go.owns(SERVICE, parts[-2]):
            resolved = go.resolve(SERVICE, parts[-2], verb)
        elif len(parts) >= 3 and go.owns(API, parts[-2]):
            args = src.split(paren + 1, src.match[paren])
            if len(args) > 2 or (args and CONTEXT_ARG.fullmatch(f.text_of(*src.trim(*args[0])))):
                continue  # not a client.API signature: some other object with a field of this name
            resolved = go.resolve(API, parts[-2], verb)
            if resolved is None and not go.known(API, parts[-2], verb):
                continue
        else:
            continue
        if resolved is None:
            unresolved.append((src.line(first), call))
        else:
            pending.append((first, paren, call, resolved))
    for _, paren, _, _ in pending:
        args = src.split(paren + 1, src.match[paren])
        if args:
            a, b = src.trim(*args[-1])
            j = a + 1 if src.tok[a] == "&" else a
            if b - j == 1 and src.kind[j] == ID:
                f.param_uses.add(j)
    found = [GoCall(src.line(first), call, resolved, paren, f.read(paren, resolved), f) for first, paren, call, resolved in pending]
    return found, unresolved, f


def extract(
    text: str, go: GoIndex, table=None, rel: str = ""
) -> tuple[list[GoFound], list[tuple[int, str]], frozenset[str]]:
    """Stripe calls in a Go source, the calls that matched no endpoint, and the response members the file reads.
    ``table`` (:mod:`crosstable`): wrappers followed across files, for the file at ``rel``."""
    found, unresolved, f = calls(text, go)
    out = [
        GoFound(c.line, c.call, c.resolved, set(c.params.fields), not c.params.hidden, frozenset(c.params.hidden))
        for c in found
    ]
    if table is not None:  # wrappers defined in other files (gocross builds on this module)
        from . import gocross
        out = gocross.extend(text, table, rel, out, found, f)
    return out, unresolved, member_reads(f) if f is not None else frozenset()


def member_reads(f: GoFile) -> frozenset[str]:
    """Exported members read off a value (``pi.PaymentMethodTypes``), as the response field they spell.
    Package-qualified names (``stripe.Customer``), method calls and writes into params are left out."""
    src, out = f.src, set()
    packages = f.roots | set(f.pkgs)
    for i in range(2, src.n - 1):
        if src.kind[i] != ID or src.tok[i - 1] != "." or not src.tok[i][0].isupper() or src.tok[i + 1] in ("(", "{"):
            continue
        receiver = src.tok[i - 2] if src.kind[i - 2] == ID else ""
        if receiver in packages or receiver in GENERIC or receiver.lower().endswith("params") or assign_op(src, i + 1):
            continue
        out.add(snake_guess(src.tok[i]))
    return frozenset(out)
