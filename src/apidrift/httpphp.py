"""PHP raw-HTTP Stripe wrappers and their calls, for :mod:`httpwrap`.

``wp_remote_post(self::ENDPOINT . $api, ['method' => $method, 'body' => $request])`` inside
``public static function request($request, $api = 'charges', $method = 'POST')`` makes ``request`` a
wrapper: path parameter 1 appended to ``const ENDPOINT = 'https://api.stripe.com/v1/'``, method parameter
2 defaulting to POST, body parameter 0. ``WC_Stripe_API::request($r, 'payment_intents')`` is then
``POST /v1/payment_intents`` with the keys of ``$r`` (read by :func:`phpparse.collect`, as for an SDK call).
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass, replace
from typing import NamedTuple

from . import httpphpbody, httpphppath, httpurl, httpwrap, phpfilters, phpparse
from .httpphpbody import Writes, _writes
from .httpphppath import client_method, real_client
from .jsparse import Fields
from .phplex import STRING, lex

FUNC = re.compile(r"function(?<![\w$>:\\]function)\s*(?:&\s*)?([A-Za-z_]\w*)\s*\(")
CLASS = re.compile(r"(?:class(?<![\w$>:\\]class)|trait(?<![\w$>:\\]trait))\s+([A-Za-z_]\w*)[^{;]*?\{")
CONST = re.compile(r"const(?<![\w$>:\\]const)\s+([A-Za-z_]\w*)\s*=")
DEFINE = re.compile(r"define(?<![\w$>:\\]define)\s*\(")
HTTP = re.compile(
    r"(wp_(?<![\w$>:\\]wp_)(?:safe_)?remote_(?:get|post|request)|curl_init(?<![\w$>:\\]curl_init)"
    r"|file_get_contents(?<![\w$>:\\]file_get_contents))\s*\("
    r"|->\s*(request|get|post|delete)\s*\("
)
STATIC_CALL = re.compile(r"(?<![\w$\\>])(\\?[A-Za-z_][\w\\]*+)\s*::\s*([A-Za-z_]\w*+)\s*\(")
THIS_CALL = re.compile(r"\$this\s*->\s*([A-Za-z_]\w*)\s*\(")
PLAIN_CALL = re.compile(r"(?<![\w$>:\\])([A-Za-z_]\w*+)\s*\(")
NAMESPACE = re.compile(r"namespace(?<![\w$>:\\]namespace)\s+([\w\\]+)\s*[;{]")
USE_STMT = re.compile(r"use(?<![\w$>:\\]use)\s+(?!function\b|const\b)([\w\\][\w\\\s,]*?)\s*;")
USE_PART = re.compile(r"\s*\\?([\w\\]+)(?:\s+as\s+(\w+))?\s*")
EXTENDS = re.compile(r"extends(?<!\wextends)\s+(\\?[\w\\]+)")
CALLED = re.compile(r"(?<!\w)[^\W_A-Za-z]*+([A-Za-z_]\w*+)\s*(?:\(|::)")  # linear: one try per word (a long word is no cost)
PARAM = re.compile(r"^\s*(?:[\w\\?|]+\s+)?(&\s*)?(\.\.\.)?\s*\$([A-Za-z_]\w*)\s*(?:=\s*(.*?))?\s*$", re.S)
ENCODERS = re.compile(r"^(?:http_build_query|json_encode|wp_json_encode)\s*\(\s*\$([A-Za-z_]\w*)\s*\)$")
SELF = {"self", "static"}
NOT_CALLS = {"function", "fn", "if", "elseif", "while", "for", "foreach", "switch", "array", "list", "isset", "unset",
             "empty", "return", "echo", "print", "new", "catch", "match", "use", "and", "or", "not"}


@dataclass(frozen=True)
class Func:
    name: str
    owner: str
    paren: int
    open: int
    close: int
    params: tuple[str, ...]
    defaults: tuple[str | None, ...]
    is_method: bool


class File:
    def __init__(self, text: str) -> None:
        self.text = text
        self.src = src = lex(text)
        self.code = src.code
        ns = NAMESPACE.search(self.code)
        self.namespace = ns.group(1).strip("\\") if ns else ""
        self.uses: dict[str, str] = {}  # lowercased alias -> fully qualified name
        self.classes: list[tuple[str, int, int]] = []
        self.extends: dict[str, str] = {}  # class -> the fully qualified class it extends
        for m in CLASS.finditer(self.code):
            brace = m.end() - 1
            if brace in src.pairs:
                self.classes.append((m.group(1), brace, src.pairs[brace]))
        for m in USE_STMT.finditer(self.code):
            if self.class_at(m.start()):
                continue  # a trait use inside a class body
            for part in m.group(1).split(","):
                got = USE_PART.fullmatch(part)
                if got:
                    self.uses[(got.group(2) or got.group(1).rsplit("\\", 1)[-1]).lower()] = got.group(1)
        for m in CLASS.finditer(self.code):
            parent = EXTENDS.search(m.group(0))
            if parent:
                self.extends[m.group(1)] = self.resolve(parent.group(1), m.start())
        self.consts: dict[tuple[str, str], str] = {}
        for m in CONST.finditer(self.code):
            value = self.string_at(src.skip_space(m.end()))
            if value is not None:
                self.consts.setdefault((self.class_at(m.start()), m.group(1)), value)
        for m in DEFINE.finditer(self.code):
            args = src.args(m.end() - 1)
            if len(args) == 2:
                name, value = self.string_at(src.skip_space(args[0][0])), self.string_at(src.skip_space(args[1][0]))
                if name and value is not None:
                    self.consts.setdefault(("", name), value)
        self.funcs: list[Func] = []
        for m in FUNC.finditer(self.code):
            paren = m.end() - 1
            close = src.pairs.get(paren)
            if close is None:
                continue
            j = src.skip_scalar(src.skip_space(close + 1), "{;")
            if j >= src.n or self.text[j] != "{" or j not in src.pairs:
                continue
            params, defaults = [], []
            for a, b in src.args(paren):
                p = PARAM.match(text[a:b])
                params.append(p.group(3) if p and not p.group(1) and not p.group(2) else "")
                defaults.append(p.group(4) if p else None)
            owner = self.class_at(m.start())
            self.funcs.append(Func(m.group(1), owner, paren, j, src.pairs[j], tuple(params), tuple(defaults), bool(owner)))
        self._parsed: phpparse.Parsed | None = None
        self._calls: tuple[list[re.Match], list[re.Match], list[re.Match]] | None = None

    @property
    def parsed(self) -> phpparse.Parsed:
        if self._parsed is None:
            self._parsed = phpparse.parse(self.text, lambda chain: None)
        return self._parsed

    @property
    def call_matches(self) -> tuple[list[re.Match], list[re.Match], list[re.Match]]:
        """Every STATIC_CALL, THIS_CALL and PLAIN_CALL match of the code, in order, found once: :func:`_calls`
        walks them for each wrapper set it is asked about (the survey, the forwarding passes, the sites)."""
        if self._calls is None:
            self._calls = (list(STATIC_CALL.finditer(self.code)), list(THIS_CALL.finditer(self.code)),
                           list(PLAIN_CALL.finditer(self.code)))
        return self._calls

    def fq(self, short: str) -> str:
        """The fully qualified name of a class declared in this file."""
        return f"{self.namespace}\\{short}" if self.namespace and short else short

    def resolve(self, name: str, pos: int) -> str:
        """The fully qualified class a reference names here: a leading backslash, an imported alias,
        ``self`` / ``static``, or a name relative to the file's namespace."""
        if name.lower() in SELF:
            return self.fq(self.class_at(pos))
        if name.startswith("\\"):
            return name.strip("\\")
        first, _, rest = name.partition("\\")
        if first.lower() in self.uses:
            return self.uses[first.lower()] + ("\\" + rest if rest else "")
        return self.fq(name)

    def class_at(self, pos: int) -> str:
        inside = [(o, name) for name, o, c in self.classes if o < pos < c]
        return max(inside)[1] if inside else ""

    def func_at(self, pos: int) -> Func | None:
        inside = [f for f in self.funcs if f.open < pos < f.close]
        return max(inside, key=lambda f: f.open) if inside else None

    def line(self, pos: int) -> int:
        return self.src.line(pos)

    def string_at(self, i: int) -> str | None:
        """The value of the plain string literal that starts at ``i`` (interpolations become holes)."""
        span = self.src.span_at(i)
        if span is None or span[0] != i or span[2] != STRING or self.text[i] not in "'\"":
            return None
        raw = self.text[i + 1 : span[1] - 1]
        if self.text[i] == "'":
            return raw.replace("\\'", "'").replace("\\\\", "\\")
        from .httplit import PHP_VAR

        return PHP_VAR.sub(httpurl.HOLE, raw).replace('\\"', '"')

    def constant(self, a: int, b: int) -> str | None:
        """The string a constant reference (``self::X``, ``Cls::X``, ``X``) between ``a`` and ``b`` holds."""
        expr = re.sub(r"\s+", "", self.code[a:b])
        m = re.fullmatch(r"(?:(\\?[A-Za-z_][\w\\]*)::)?([A-Za-z_]\w*)", expr)
        if m is None:
            return None
        owner = m.group(1) or ""
        if owner.lower() in SELF:
            owner = self.class_at(a)
        owner = owner.rsplit("\\", 1)[-1]
        return self.consts.get((owner, m.group(2))) if (owner or m.group(1) is None) else None

    def pieces(self, a: int, b: int) -> list[tuple[str, str | None]]:
        """An expression split at its top-level ``.``: ("lit", text) / ("var", name) / ("expr", None)."""
        src, code, out = self.src, self.code, []
        i = src.skip_space(a)
        while i < b:
            value = self.string_at(i)
            if value is not None:
                out += self.interpolated(i) if self.text[i] == '"' else [("lit", value)]
                i = src.span_at(i)[1]
            else:
                j = i
                while j < b:
                    end = src.masked(j)
                    if end:
                        j = end
                    elif code[j] in "([{" and j in src.pairs:
                        j = src.pairs[j] + 1
                    elif code[j] == "." and code[j + 1 : j + 2] not in (".", "=") and not code[j - 1 : j].isdigit():
                        break
                    else:
                        j += 1
                end = src.back_space(j)
                expr = self.text[i:end]
                const = self.constant(i, end)
                if const is not None:
                    out.append(("lit", const))
                elif re.fullmatch(r"\$[A-Za-z_]\w*", expr):
                    out.append(("var", expr[1:]))
                else:
                    out.append(("expr", f"{i}:{end}"))
                i = j
            i = src.skip_space(i)
            if i < b:
                if code[i] != ".":
                    return out + [("expr", None)]
                i = src.skip_space(i + 1)
        return out

    def interpolated(self, i: int) -> list[tuple[str, str | None]]:
        """A double-quoted string split into its text and the variables interpolated in it."""
        from .httplit import PHP_VAR

        raw, out, last = self.text[i + 1 : self.src.span_at(i)[1] - 1], [], 0
        for m in PHP_VAR.finditer(raw):
            if m.start() > last:
                out.append(("lit", raw[last : m.start()].replace('\\"', '"')))
            name = re.fullmatch(r"\{?\$([A-Za-z_]\w*)\}?", m.group(0))
            out.append(("var", name.group(1)) if name else ("expr", None))
            last = m.end()
        if last < len(raw) or not out:
            out.append(("lit", raw[last:].replace('\\"', '"')))
        return out

    def array_items(self, a: int, b: int) -> dict[str, tuple[int, int]] | None:
        """Literal-keyed elements of the array literal (or variable bound to one) between ``a`` and ``b``."""
        i = self.src.skip_space(a)
        opener = self.src.array_open(i)
        if opener is None:
            m = re.fullmatch(r"\$([A-Za-z_]\w*)", self.text[i : self.src.back_space(b)])
            if m is None:
                return None
            reaching = self.parsed.bindings.reaching(m.group(1), i)
            if len(reaching) != 1 or reaching[0].literal is None:
                return None
            opener = reaching[0].literal
        out = {}
        for el in phpparse.elements(self.src, opener):
            key = phpparse.literal_key(self.src, el)
            if key is None:
                return None  # a spread or a computed key could be anything, ``method`` included
            out[key[0]] = (el.value, el.end)
        return out


def _default(text: str | None) -> str | None:
    if text is None:
        return None
    m = re.fullmatch(r"\s*(['\"])(.*)\1\s*", text, re.S)
    return m.group(2) if m else None


def _empty(text: str | None) -> bool:
    return text is not None and re.fullmatch(r"\s*(?:\[\s*\]|array\s*\(\s*\))\s*", text, re.I) is not None


def _direct(f: File, fn: Func, rel: str) -> httpwrap.Wrapper | None:
    """``fn`` as a wrapper whose own HTTP call requests a Stripe base joined with one of its parameters."""
    for m in HTTP.finditer(f.code, fn.open, fn.close):
        callee, verb = m.group(1), m.group(2)
        args = f.src.args(m.end() - 1)
        url_at = 1 if verb == "request" else 0
        if len(args) <= url_at:
            continue
        pieces = f.pieces(*args[url_at])
        if len(pieces) < 2 or pieces[-1][0] != "var" or any(k != "lit" for k, _ in pieces[:-1]):
            continue
        base = "".join(v for _, v in pieces[:-1])
        name = pieces[-1][1]
        if not httpurl.is_base(base) or name not in fn.params:
            continue
        method, method_param, body, body_seen = None, None, None, True
        if callee == "curl_init" or not real_client(f, m):
            continue  # curl options are set by curl_setopt elsewhere: not read
        method, method_var, options, keys = client_method(f, m, args)
        if method_var is not None and method_var in fn.params:
            method_param = fn.params.index(method_var)
        if options is None:
            body_seen = False  # the options are not a visible array: a body may be in them
        else:
            for key in keys:
                if key in options:
                    a, b = options[key]
                    expr = f.text[a:b].strip()
                    v = re.fullmatch(r"\$([A-Za-z_]\w*)", expr) or ENCODERS.fullmatch(expr)
                    if v and v.group(1) in fn.params and body is None:
                        body = fn.params.index(v.group(1))
                    else:
                        body_seen = False
        w = _writes(f, fn, fn.params[body], m.start(), name) if body is not None else Writes(False, set(), (), frozenset())
        return httpwrap.Wrapper(
            "php", rel, fn.name, f.fq(fn.owner), base, fn.params.index(name), method, method_param, body, body_seen,
            w.rewrites, frozenset(w.adds), fn.defaults, fn.is_method, f.line(fn.paren), maybe=w.maybe, filters=w.filters,
        )
    return None


def _calls(f: File, wrappers: list[httpwrap.Wrapper], rel: str, registry) -> list[tuple[httpwrap.Wrapper, int, int, str]]:
    """(wrapper, call start, its ``(``, display) for every call of a wrapper in the file."""
    out = []
    by_owner: dict[tuple[str, str], list[httpwrap.Wrapper]] = {}
    for w in wrappers:
        by_owner.setdefault((w.owner.lower(), w.name.lower()), []).append(w)

    def one(key: tuple[str, str]) -> httpwrap.Wrapper | None:
        found = by_owner.get(key, [])
        here = [w for w in found if w.rel == rel]
        return (here or found)[0] if len(here) == 1 or len(found) == 1 else None

    static_calls, this_calls, plain_calls = f.call_matches
    for m in static_calls:
        if m.group(1).lower() == "parent":
            continue
        w = one((f.resolve(m.group(1), m.start()).lower(), m.group(2).lower()))  # namespaces and ``use`` resolved
        if w is not None:
            out.append((w, m.start(), m.end() - 1, re.sub(r"\s+", "", m.group(0)[:-1])))
    for m in this_calls:
        short = f.class_at(m.start())
        w = one((f.fq(short).lower(), m.group(1).lower()))
        if w is None and short in f.extends:  # an inherited method: only a parent this file names
            w = one((f.extends[short].lower(), m.group(1).lower()))
        if w is not None and w.is_method:
            out.append((w, m.start(), m.end() - 1, re.sub(r"\s+", "", m.group(0)[:-1])))
    for m in plain_calls:
        name = m.group(1)
        if name.lower() in NOT_CALLS or re.search(r"(?:function|new)\s*&?\s*$", f.code[max(0, m.start() - 12) : m.start()]):
            continue
        w = one(("", name.lower()))
        if w is not None and (w.rel == rel or (registry is not None and registry.unique("php", name))):
            out.append((w, m.start(), m.end() - 1, name))
    return out


@functools.lru_cache(maxsize=8)
def _recent(text: str) -> File:
    return File(text)


def _file(text: str, cache: dict | None) -> File:
    """The :class:`File` of ``text``, built once: the survey and the first pass's same-file sites read a
    file back to back (the LRU), and the registry keeps the survey's Files (``cache``, text -> File) for
    :func:`sites`, which reads the same files again once every wrapper is known. A File is read-only."""
    if cache is None:
        return _recent(text)
    f = cache.get(text)
    if f is None:
        f = cache[text] = _recent(text)
    return f


def survey(text: str, rel: str, cache: dict | None = None) -> tuple[list[httpwrap.Wrapper], set[str], dict[str, bool]]:
    called = httpwrap.called_names(text, CALLED) if re.search(r"::|\(", text) else {}
    for m in EXTENDS.finditer(text):  # a subclass calls its parent's methods through ``$this->``
        called.setdefault(m.group(1).rsplit("\\", 1)[-1], True)
    for m in USE_STMT.finditer(text):  # ``use Stripe\API as S; S::request(...)``
        for part in m.group(1).split(","):
            got = USE_PART.fullmatch(part)
            if got:
                called.setdefault(got.group(1).rsplit("\\", 1)[-1], True)
    defined = set(FUNC.findall(text))  # methods counted too: a plain function's name is unique only if no method has it
    if "stripe.com" not in text:
        return [], defined, called
    return wrappers_in(_file(text, cache), rel, []), defined, called


def wrappers_in(f: File, rel: str, known: list[httpwrap.Wrapper]) -> list[httpwrap.Wrapper]:
    """Wrappers defined in one file: direct ones, then (twice) functions forwarding their own parameter
    to the path of a wrapper known here or elsewhere."""
    found = [w for fn in f.funcs if (w := _direct(f, fn, rel)) is not None]
    for _ in range(2):
        names = {(w.owner, w.name) for w in found if w.rel == rel}
        forwards: dict[Func, list[httpwrap.Wrapper | None]] = {}
        for w, start, paren, _ in _calls(f, found + known, rel, None):
            fn = f.func_at(start)
            if fn is not None and (f.fq(fn.owner), fn.name) not in names:
                forwards.setdefault(fn, []).append(_forward(f, fn, w, paren, rel))
        for fn, got in forwards.items():
            # every wrapper call in it must forward the same way: ``retrieve($path)`` on one branch and
            # ``request($params, $path, $method)`` on another make the method depend on the arguments
            shapes = {None if g is None else (g.base, g.path, g.method, g.method_param, g.body) for g in got}
            if len(shapes) == 1 and got[0] is not None:
                # a key one forwarding call sends and another does not is sent only on some paths
                adds = frozenset.intersection(*(g.adds for g in got))
                some = {k for g in got for k in g.adds} - adds
                maybe = tuple(dict.fromkeys([m for g in got for m in g.maybe] + [(k, None) for k in sorted(some)]))
                first = got[0]
                found.append(httpwrap.Wrapper(
                    first.language, first.rel, first.name, first.owner, first.base, first.path, first.method,
                    first.method_param, first.body, all(g.body_seen for g in got), any(g.rewrites for g in got),
                    adds, first.defaults, first.is_method, first.line, maybe=maybe,
                    filters=frozenset().union(*(g.filters for g in got)),
                ))
    return found


def _forward(f: File, fn: Func, w: httpwrap.Wrapper, paren: int, rel: str) -> httpwrap.Wrapper | None:
    args = f.src.args(paren)
    if w.path >= len(args):
        return None
    pieces = f.pieces(*args[w.path])
    if len(pieces) != 1 or pieces[0][0] != "var" or pieces[0][1] not in fn.params:
        return None
    method, method_param = w.method, None
    if w.method_param is not None:
        if w.method_param < len(args):
            got = f.pieces(*args[w.method_param])
            if len(got) == 1 and got[0][0] == "lit":
                method = httpurl.method_word(got[0][1])
            elif len(got) == 1 and got[0][0] == "var" and got[0][1] in fn.params:
                method_param = fn.params.index(got[0][1])
        else:
            method = httpurl.method_word(_default(w.defaults[w.method_param] if w.method_param < len(w.defaults) else None))
    body, body_seen, rewrites, adds = None, w.body_seen, w.rewrites, set(w.adds)
    maybe, filters = tuple(w.maybe), w.filters
    if w.body is not None:
        got = f.pieces(*args[w.body]) if w.body < len(args) else []
        if len(got) == 1 and got[0][0] == "var" and got[0][1] in fn.params:
            body = fn.params.index(got[0][1])
            more = _writes(f, fn, got[0][1], paren, pieces[0][1])  # what this forwarding call has seen so far
            rewrites, adds, maybe, filters = rewrites or more.rewrites, adds | more.adds, maybe + more.maybe, filters | more.filters
        elif got or not _empty(w.defaults[w.body] if w.body < len(w.defaults) else None):
            body_seen = False
    return httpwrap.Wrapper(
        "php", rel, fn.name, f.fq(fn.owner), w.base, fn.params.index(pieces[0][1]), method, method_param, body, body_seen,
        rewrites, frozenset(adds), fn.defaults, fn.is_method, f.line(fn.paren), maybe=maybe, filters=filters,
    )


def sites(text: str, rel: str, index, reads: frozenset[str], language: str, registry) -> list:
    known = [w for w in (registry.wrappers if registry is not None else []) if w.language == "php"]
    if not known and "stripe.com" not in text:
        return []
    f = _file(text, registry.files if registry is not None else None)
    here = wrappers_in(f, rel, known) if "stripe.com" in text or known else []
    wrappers = here + [w for w in known if w.rel != rel]
    out = httpphppath.constant_sites(f, text, rel, index, reads, language) if f.consts else []
    hooks = registry.hooks if registry is not None else httpwrap.production_hooks(rel, text)
    for w, start, paren, display in _calls(f, wrappers, rel, registry):
        if any(not phpfilters.pass_through(h, hooks) for h in w.filters):  # hooked, by name, prefix or any
            w = replace(w, rewrites=True)
        args = f.src.args(paren)
        if any(phpparse_named(f, a) or f.text.startswith("...", f.src.skip_space(a)) for a, _ in args):
            continue
        if w.path < len(args):
            pieces = f.pieces(*args[w.path])
        else:
            default = _default(w.defaults[w.path] if w.path < len(w.defaults) else None)
            pieces = [("lit", default)] if default is not None else []
        templates = httpphppath.expand(f, pieces, start) if pieces else []
        method = w.method
        if w.method_param is not None:
            if w.method_param < len(args):
                got = f.pieces(*args[w.method_param])
                method = httpurl.method_word(got[0][1]) if len(got) == 1 and got[0][0] == "lit" else None
            else:
                method = httpurl.method_word(_default(w.defaults[w.method_param] if w.method_param < len(w.defaults) else None))
        fields = httpphpbody.call_fields(f, w, args, start, paren, display, len(templates) > 1)
        paths = set()
        for template in templates:  # one call site per endpoint the path can name
            u = httpwrap.build(text, rel, index, w, f.line(start), display, template, method, fields.paths,
                               fields.hidden, reads, language)
            if u is not None and u.path not in paths:
                paths.add(u.path)
                out.append(u)
    return out


def phpparse_named(f: File, a: int) -> bool:
    """A PHP 8 named argument: ``name: value``."""
    return re.match(r"\s*[A-Za-z_]\w*\s*:(?!:)", f.text[a : a + 80]) is not None
