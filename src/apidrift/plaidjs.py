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
from dataclasses import dataclass

from . import jsparse, jsroots
from .jsparse import Fields, Mask, Source
from .jsroots import NAME, NO_ASSIGN_OP, TYPE_ANN
from .plaidlegacy import NODE_METHODS
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
CAST = re.compile(r"\s+(?:as|satisfies)\s+[\w.<>\[\]]+\s*$")
WRITE = re.compile(r"\s*(?:['\"]\s*\]\s*)?=(?![=>])\s*")  # what follows the key of ``request.key = `` / ``request['key'] = ``


@dataclass
class Found(jsparse.Found):
    values: frozenset[tuple[str, str | None]] = frozenset()  # see scanner.Usage.values


class Clients:
    """Names in one file that hold a Plaid client (``known``) or plausibly do (``likely``)."""

    def __init__(self, src: Source, mask: Mask, scopes: jsroots.Scopes) -> None:
        text = src.text
        classes, namespaces, self.likely = set(), set(), set()
        theirs = set(re.findall(rf"\bclass\s+({NAME})", text))  # classes this project defines or imports from its own modules
        self.sdk: set[str] = set()  # names imported from the SDK (its enums among them) and names bound to all of it
        for pattern in (IMPORT, REQUIRE):
            for m in pattern.finditer(text):
                if mask.covers(m.start()):
                    continue
                spec, named = m.group("spec"), _named(m.group("named") or "")
                whole = {m.group("default"), m.groupdict().get("ns")} - {None}
                if spec == SDK_PACKAGE:
                    namespaces |= whole
                    self.sdk |= whole | {local for _, local in named}
                    classes |= {local for name, local in named if name in CLIENT_CLASSES}
                else:
                    theirs |= {local for _, local in named}
                    if "plaid" in spec.lower() and not NOT_A_MODULE.search(spec):
                        self.likely |= whole | {local for _, local in named}
        if "PlaidApi" not in theirs:  # a project's own class of that name (a wrapper) is not the SDK's client
            classes.add("PlaidApi")
        direct = "|".join(sorted(re.escape(name) for name in classes)) or "(?!)"
        spaced = "|".join(sorted(re.escape(name) for name in namespaces)) or "(?!)"
        typed = rf"(?:(?:{direct})|(?:{spaced})\s*\.\s*(?:{'|'.join(CLIENT_CLASSES)}))"
        made = rf"new\s+{typed}\s*[(<]"
        self.made = re.compile(made)
        roots = [
            re.compile(rf"(?<![\w$.])(?:this\s*\.\s*)?({NAME}){TYPE_ANN}\s*{NO_ASSIGN_OP}\s*(?:await\s+)?{made}"),
            re.compile(rf"(?<![\w$])({NAME})\s*[?!]?\s*:\s*{typed}(?![\w$])(?!\s*[.(])"),
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

    def holds(self, chain: list[str], at: int, bindings: jsroots.Bindings) -> bool:
        """The receiver of ``chain`` (called at ``at``) is a client this file makes, types or gets from a factory.
        A variable counts where the assignment in force binds it to one: another function's ``api`` does not."""
        receiver = chain[:-1]
        if receiver[0] in ("this", "self"):
            return len(receiver) == 2 and receiver[1] in self.known
        if len(receiver) != 1 or receiver[0] not in self.known:
            return False
        bound = bindings.at(receiver[0], at)
        if bound is None or bound.value < 0:
            return True  # a parameter, an import or a function: known by its type or by what it returns
        names = "|".join(sorted((re.escape(n) for n in self.known), key=len, reverse=True))
        value = re.compile(rf"(?:async\s+)?(?:await\s+)?(?:{self.made.pattern}|(?:this\s*\.\s*)?(?:{names})\b|\(|function\b)")
        return value.match(bindings.src.text, bound.value) is not None

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
        holds = clients.holds(chain, m.start(1), bindings)
        if not (holds or clients.may_hold(chain)):
            continue
        resolved = index.method(chain[-1]) if "_" not in chain[-1] else None
        line = src.text.count("\n", 0, m.start()) + 1
        if resolved is None and holds and chain[-1] in NODE_METHODS:  # the pre-2021 client: positional arguments
            resolved = index.endpoint(NODE_METHODS[chain[-1]], chain[-1])
            if resolved is not None:
                unseen = Fields()
                unseen.hide("")
                found.append(Found(line, jsroots.display(m.group(1)), resolved, unseen))
                continue
        if resolved is None:
            if holds:
                other.append((line, jsroots.display(m.group(1))))
            continue
        spans = src.args(m.end() - 1)
        fields = request_fields(src, spans[0], bindings) if spans else Fields()
        if not holds and spans and _not_a_request(src, spans[0], fields, index, (resolved.method, resolved.path)):
            continue  # a project's own function with the SDK method's name
        found.append(Found(line, jsroots.display(m.group(1)), resolved, fields, plain_values(src, fields, clients.sdk)))
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


def plain_values(src: Source, fields: Fields, sdk: set[str]) -> frozenset[tuple[str, str | None]]:
    """(field, value) for each value written next to a literal key that is not itself an object: a string,
    number or boolean literal as written, ``~Member`` for a member of an enum imported from the SDK
    (``Products.Auth``), None for anything else. A list gives one entry per element."""
    text, out = src.text, set()
    for path, spans in fields.spans.items():
        if any(other.startswith((path + ".", path + "[].")) for other in fields.paths):
            continue
        for span in spans:
            i = src.skip_space(span.key_end)
            if span.kind == "shorthand" or text[i:i + 1] != ":":
                out.add((path, None))
                continue
            a = src.skip_space(i + 1)
            if text[a:a + 1] != "[":
                out.add((path, _plain(text[a:span.entry_end], sdk)))
                continue
            i, close = a + 1, src.skip_balanced(a) - 1
            while i < close:
                j = src.skip_scalar(i)
                if text[i:j].strip():
                    out.add((path, _plain(text[i:j], sdk)))
                i = j + 1
    return frozenset(out)


def _plain(source: str, sdk: set[str]) -> str | None:
    value = CAST.sub("", source.strip())
    if len(value) >= 2 and value[0] in "'\"`" and value[-1] == value[0] and value[0] not in value[1:-1] and "${" not in value:
        return value[1:-1]
    if re.fullmatch(r"-?\d+(?:\.\d+)?|true|false", value):
        return value
    member = re.fullmatch(rf"((?:{NAME}\s*\.\s*)+)({NAME})", value)
    if member and re.match(NAME, member.group(1)).group(0) in sdk:
        return "~" + member.group(2)
    return None


def _not_a_request(src: Source, span: tuple[int, int], fields: Fields, index: PlaidIndex, op: tuple[str, str]) -> bool:
    """The first argument cannot be the SDK's request object: a string, number or template, or an object with a
    key the operation has never had (``{ userId }``). Asked only of receivers known by name alone, where a
    wrapper around the client can share the SDK's method names."""
    first = src.text[src.skip_space(span[0]):span[1]][:1]
    if first in "'\"`" or first.isdigit():
        return True
    top = {path for path in fields.paths if "." not in path and "[" not in path}
    return any(not index.knows(op, name) for name in top)
