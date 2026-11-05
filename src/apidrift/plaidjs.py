"""Find plaid-node calls in JavaScript and TypeScript and read the request object each one sends.

A client is known by how it is made or typed: ``new PlaidApi(configuration)`` (also ``new plaid.PlaidApi``
and the pre-2021 ``new plaid.Client``), a ``: PlaidApi`` annotation, a function that returns one, or a
variable bound to the result of such a function. Those names are followed through the file.

A client that comes from another file is not resolved across files. Its calls are read when the receiver
is named after Plaid (``plaidClient.accountsGet``, ``this.plaid.itemGet``) or was imported from a module
so named (``import { client } from './lib/plaid'``), and only when the method is one of the SDK's
generated names (``transactionsSync``, ``itemPublicTokenExchange``), which other code does not use.

Every generated method takes the request object first. The optional second argument is HTTP options
and is not read. Built on the cursor, scopes and bindings of :mod:`jsparse` and :mod:`jsroots`.
"""

from __future__ import annotations

import re

from . import jsparse, jsroots
from .jsparse import Fields, Found, Mask, Source
from .jsroots import NAME, NO_ASSIGN_OP, TYPE_ANN
from .plaidmap import PlaidIndex

QUOTED = r"['\"](?P<spec>[^'\"\n]*)['\"]"
IMPORT = re.compile(
    rf"\bimport\s+(?:type\s+)?(?:(?P<default>{NAME})\s*,?\s*)?(?:\*\s*as\s+(?P<ns>{NAME})\s*|\{{(?P<named>[^}}]*)\}}\s*)?from\s*{QUOTED}"
)
REQUIRE = re.compile(
    rf"\b(?:const|let|var)\s+(?:(?P<default>{NAME})|\{{(?P<named>[^}}]*)\}})\s*=\s*(?:await\s+)?(?:require|import)\(\s*{QUOTED}\s*\)"
)
SDK_PACKAGE = "plaid"
CLIENT_CLASSES = ("PlaidApi", "Client")  # Client: the pre-2021 SDK
NOT_A_MODULE = re.compile(r"plaid-link|plaid-threads|^@plaid/", re.I)  # Link's browser and mobile SDKs: no API client
ENDS = r"\s*(?:;|\}|$)"
VARIABLE = re.compile(rf"({NAME})\s*(?:!|\bas\s+[\w.<>\[\]]+\s*)?$")  # a bare name, ``name!`` or ``name as T``
WRITE = re.compile(r"\s*(?:['\"]\s*\]\s*)?=(?![=>])\s*")  # what follows the key of ``request.key = `` / ``request['key'] = ``


class Clients:
    """Names in one file that hold a Plaid client (``known``) or plausibly do (``likely``)."""

    def __init__(self, src: Source, mask: Mask, scopes: jsroots.Scopes) -> None:
        text = src.text
        classes, namespaces, self.likely = {"PlaidApi"}, set(), set()
        for pattern in (IMPORT, REQUIRE):
            for m in pattern.finditer(text):
                if mask.covers(m.start()):
                    continue
                spec, named = m.group("spec"), _named(m.group("named") or "")
                whole = {m.group("default"), m.groupdict().get("ns")} - {None}
                if spec == SDK_PACKAGE:
                    namespaces |= whole
                    classes |= {local for name, local in named if name in CLIENT_CLASSES}
                elif "plaid" in spec.lower() and not NOT_A_MODULE.search(spec):
                    self.likely |= whole | {local for _, local in named}
        direct = "|".join(sorted(re.escape(name) for name in classes))
        spaced = "|".join(sorted(re.escape(name) for name in namespaces))
        made = rf"new\s+(?:{direct})\s*[(<]" if not spaced else (
            rf"new\s+(?:(?:{direct})|(?:{spaced})\s*\.\s*(?:{'|'.join(CLIENT_CLASSES)}))\s*[(<]"
        )
        self.made = re.compile(made)
        roots = [
            re.compile(rf"(?<![\w$.])(?:this\s*\.\s*)?({NAME}){TYPE_ANN}\s*{NO_ASSIGN_OP}\s*(?:await\s+)?{made}"),
            re.compile(rf"(?<![\w$])({NAME})\s*[?!]?\s*:\s*(?:{NAME}\s*\.\s*)?PlaidApi\b(?!\s*[.(])"),
        ]
        self.known = {m.group(1) for pattern in roots for m in pattern.finditer(text) if not mask.covers(m.start())}
        for _ in range(3):
            grown = self.known | self._factories(text, mask, scopes) | self._aliases(text, mask)
            if grown == self.known:
                break
            self.known = grown

    def _factories(self, text: str, mask: Mask, scopes: jsroots.Scopes) -> set[str]:
        """Functions that return a client: ``return new PlaidApi(...)``, ``return client;``, ``() => new PlaidApi(...)``."""
        names = "|".join(sorted((re.escape(n) for n in self.known), key=len, reverse=True))
        returned = rf"(?:await\s+)?(?:{self.made.pattern}" + (rf"|(?:this\s*\.\s*)?(?:{names}){ENDS})" if names else ")")
        returns = re.compile(rf"\breturn\s+{returned}", re.M)
        out = {name for open_, close, name in scopes.functions if name and returns.search(text, open_, close)}
        arrow = re.compile(
            rf"\b(?:const|let|var)\s+({NAME}){TYPE_ANN}\s*=\s*(?:async\s*)?(?:\([^()]*\)|{NAME})\s*(?::[^=\n]*?)?=>\s*{returned}",
            re.M,
        )
        return out | {m.group(1) for m in arrow.finditer(text) if not mask.covers(m.start())}

    def _aliases(self, text: str, mask: Mask) -> set[str]:
        """``const client = getClient()``, ``const api = this.client;``: bound to a known client or factory."""
        names = "|".join(sorted((re.escape(n) for n in self.known), key=len, reverse=True))
        if not names:
            return set()
        pattern = re.compile(
            rf"(?<![\w$.])(?:this\s*\.\s*)?({NAME}){TYPE_ANN}\s*{NO_ASSIGN_OP}\s*(?:await\s+)?(?:this\s*\.\s*)?(?:{names})\s*(?:\([^()]*\))?{ENDS}",
            re.M,
        )
        return {m.group(1) for m in pattern.finditer(text) if not mask.covers(m.start())}

    def holds(self, chain: list[str]) -> bool:
        """The receiver of ``chain`` is a client this file makes, types or gets from a factory."""
        receiver = chain[:-1]
        if receiver[0] in ("this", "self"):
            receiver = receiver[1:]
        return len(receiver) == 1 and receiver[0] in self.known

    def may_hold(self, chain: list[str]) -> bool:
        """The receiver is named after Plaid or came from a module that is."""
        return chain[0] in self.likely or any("plaid" in part.lower() for part in chain[:-1])


def _named(body: str) -> list[tuple[str, str]]:
    """(exported name, local name) of each entry in ``{ PlaidApi, Configuration as Config, Client: C }``."""
    out: list[tuple[str, str]] = []
    for part in body.split(","):
        names = re.findall(NAME, re.sub(r"^\s*type\s+", "", part.split("=")[0]))
        names = [n for n in names if n != "as"]
        if names:
            out.append((names[0], names[-1]))
    return out


def extract(text: str, index: PlaidIndex) -> tuple[list[Found], list[tuple[int, str]]]:
    """plaid-node calls in a source, plus (line, call) pairs for other methods called on a known client:
    the pre-2021 SDK's own names, or a method newer than the specs the index was built from."""
    src = Source(text)
    mask = Mask(src.text, ruby=False)
    scopes = jsroots.Scopes(src, mask)
    clients = Clients(src, mask, scopes)
    bindings = jsroots.Bindings(src, mask, scopes)
    found: list[Found] = []
    other: list[tuple[int, str]] = []
    at = 0
    while (m := jsparse.JS_CALL.search(src.text, at)) is not None:
        at = m.end()
        if mask.covers(m.start()):
            at = mask.end(m.start())
            continue
        chain, _ = jsroots.chain_parts(src, m.start(1), m.end(1))
        if len(chain) < 2:
            continue
        holds = clients.holds(chain)
        if not (holds or clients.may_hold(chain)):
            continue
        resolved = index.method(chain[-1]) if "_" not in chain[-1] else None
        line = src.text.count("\n", 0, m.start()) + 1
        if resolved is None:
            if holds:
                other.append((line, jsroots.display(m.group(1))))
            continue
        spans = src.args(m.end() - 1)
        fields = request_fields(src, spans[0], bindings) if spans else Fields()
        found.append(Found(line, jsroots.display(m.group(1)), resolved, fields))
    return found, other


def request_fields(src: Source, span: tuple[int, int], bindings: jsroots.Bindings) -> Fields:
    """The fields of a request argument: an object literal, or a variable bound to one in scope (with the keys
    written into it before the call). Anything else may send fields that are not visible here."""
    out = Fields()
    text, (a, b) = src.text, span
    i = src.skip_space(a)
    if i >= b:
        return out
    if text[i] == "{":
        src.object_fields(i + 1, "", out)
        return out
    m = VARIABLE.match(text[i:b].rstrip())
    reaching = bindings.reaching(m.group(1), i) if m else []
    if not reaching:
        out.hide("")
    for binding in reaching:
        if binding.literal is None:
            out.hide("")
        else:
            _variable(src, bindings, binding, i, out)
    return out


def _variable(src: Source, bindings: jsroots.Bindings, binding: jsroots.Binding, at: int, out: Fields) -> None:
    """The literal a request variable was bound to, plus what is written into it before the call. Requests
    are commonly built up key by key (``request.cursor = cursor``), so a write with a visible key adds that
    key and the object assigned to it; only a computed key, a merge of something unseen, or handing the
    variable to other code leaves the request open."""
    text = src.text
    src.object_fields(binding.literal + 1, "", out)
    mutations = bindings.mutations(binding.name, binding.literal, at)
    for mutation in mutations:
        if mutation.literal is not None:  # Object.assign(request, { ... })
            src.object_fields(mutation.literal + 1, "", out)
        elif mutation.key is None:  # request[name] = value, Object.assign(request, other)
            out.hide("")
        else:
            out.add(mutation.key)
            write = WRITE.match(text, mutation.key_end)
            if write is None:  # request.user.name = value: deeper than the key
                out.hide(mutation.key + ".")
            else:
                src.value(write.end(), mutation.key, out)
    scopes, here = bindings.scopes, bindings.scopes.innermost(at)
    if scopes.innermost(binding.pos) != here:  # bound in an outer scope: another function may write into it first
        writes = jsroots._mutation_re(binding.name).finditer(text)
        if any(not bindings.mask.covers(w.start()) and scopes.innermost(w.start()) != here for w in writes):
            out.hide("")
    whole = re.compile(rf"(?<![\w$.]){re.escape(binding.name)}(?![\w$])")
    for use in whole.finditer(text, binding.literal, at):
        if bindings.mask.covers(use.start()) or any(w.start <= use.start() < w.end for w in mutations):
            continue
        if text[src.skip_space(use.end()):][:1] not in (".", "[", "=", ":"):
            out.hide("")  # passed to a function, spread into another object or returned before the call
            break
