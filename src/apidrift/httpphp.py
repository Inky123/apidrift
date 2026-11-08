"""PHP raw-HTTP Stripe wrappers and their calls, for :mod:`httpwrap`.

``wp_remote_post(self::ENDPOINT . $api, ['method' => $method, 'body' => $request])`` inside
``public static function request($request, $api = 'charges', $method = 'POST')`` makes ``request`` a
wrapper: path parameter 1 appended to ``const ENDPOINT = 'https://api.stripe.com/v1/'``, method parameter
2 defaulting to POST, body parameter 0. ``WC_Stripe_API::request($r, 'payment_intents')`` is then
``POST /v1/payment_intents`` with the keys of ``$r`` (read by :func:`phpparse.collect`, as for an SDK call).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import httpphppath, httpurl, httpwrap, phpparse
from .jsparse import Fields
from .phplex import STRING, Lex

FUNC = re.compile(r"(?<![\w$>:\\])function\s*(?:&\s*)?([A-Za-z_]\w*)\s*\(")
CLASS = re.compile(r"(?<![\w$>:\\])(?:class|trait)\s+([A-Za-z_]\w*)[^{;]*?\{")
CONST = re.compile(r"(?<![\w$>:\\])const\s+([A-Za-z_]\w*)\s*=")
DEFINE = re.compile(r"(?<![\w$>:\\])define\s*\(")
HTTP = re.compile(
    r"(?<![\w$>:\\])(wp_(?:safe_)?remote_(?:get|post|request)|curl_init|file_get_contents)\s*\("
    r"|->\s*(request|get|post|delete)\s*\("
)
STATIC_CALL = re.compile(r"(?<![\w$\\>])(\\?[A-Za-z_][\w\\]*)\s*::\s*([A-Za-z_]\w*)\s*\(")
THIS_CALL = re.compile(r"\$this\s*->\s*([A-Za-z_]\w*)\s*\(")
PLAIN_CALL = re.compile(r"(?<![\w$>:\\])([A-Za-z_]\w*)\s*\(")
CALLED = re.compile(r"([A-Za-z_]\w*)\s*(?:\(|::)")
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
        self.src = src = Lex(text)
        self.code = src.code
        self.classes: list[tuple[str, int, int]] = []
        for m in CLASS.finditer(self.code):
            brace = m.end() - 1
            if brace in src.pairs:
                self.classes.append((m.group(1), brace, src.pairs[brace]))
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

    @property
    def parsed(self) -> phpparse.Parsed:
        if self._parsed is None:
            self._parsed = phpparse.parse(self.text, lambda chain: None)
        return self._parsed

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
            if key:
                out[key[0]] = (el.value, el.end)
        return out


def _default(text: str | None) -> str | None:
    if text is None:
        return None
    m = re.fullmatch(r"\s*(['\"])(.*)\1\s*", text, re.S)
    return m.group(2) if m else None


def _empty(text: str | None) -> bool:
    return text is not None and re.fullmatch(r"\s*(?:\[\s*\]|array\s*\(\s*\))\s*", text, re.I) is not None


def _writes(f: File, fn: Func, name: str, before: int) -> tuple[bool, set[str]]:
    """(rebound or written with a key that is not a literal, literal keys written) for ``$name`` in ``fn``."""
    body = f.code[fn.open : before]
    rebound = re.search(rf"\${name}\s*(?:=(?![=>])|\.=|\+=|\?\?=)|&\s*\${name}\b", body) is not None
    adds = set(re.findall(rf"\${name}\s*\[\s*'([A-Za-z_]\w*)'\s*\]\s*=(?![=>])", f.text[fn.open : before]))
    other = re.findall(rf"\${name}\s*\[[^\]]*\]\s*(?:\[[^\]]*\]\s*)*=(?![=>])", body)
    return rebound or len(other) > len(adds), adds


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
        if verb == "request":
            got = f.pieces(*args[0])
            method = httpurl.method_word(got[0][1]) if len(got) == 1 and got[0][0] == "lit" else None
            if len(got) == 1 and got[0][0] == "var" and got[0][1] in fn.params:
                method_param = fn.params.index(got[0][1])
            options = f.array_items(*args[2]) if len(args) > 2 else {}
            keys = ("form_params", "json", "body", "multipart", "query")
        elif verb:
            method, options, keys = verb, (f.array_items(*args[1]) if len(args) > 1 else {}), ("form_params", "json", "body", "query")
        elif callee == "curl_init":
            continue  # options set by curl_setopt elsewhere: not read
        else:
            options = f.array_items(*args[1]) if len(args) > 1 else {}
            method = "get" if callee.endswith(("_get", "file_get_contents")) else "post" if callee.endswith("_post") else None
            keys = ("body",)
            if options is not None and "method" in options:
                got = f.pieces(*options["method"])
                method = httpurl.method_word(got[0][1]) if len(got) == 1 and got[0][0] == "lit" else None
                if len(got) == 1 and got[0][0] == "var" and got[0][1] in fn.params:
                    method_param = fn.params.index(got[0][1])
            elif callee.endswith("_request"):
                method = "get"
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
        rewrites, adds = _writes(f, fn, fn.params[body], m.start()) if body is not None else (False, set())
        return httpwrap.Wrapper(
            "php", rel, fn.name, fn.owner, base, fn.params.index(name), method, method_param, body, body_seen,
            rewrites, frozenset(adds), fn.defaults, fn.is_method, f.line(fn.paren),
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

    for m in STATIC_CALL.finditer(f.code):
        owner = m.group(1).lstrip("\\").rsplit("\\", 1)[-1]
        if owner.lower() in SELF:
            owner = f.class_at(m.start())
        w = one((owner.lower(), m.group(2).lower()))
        if w is not None:
            out.append((w, m.start(), m.end() - 1, re.sub(r"\s+", "", m.group(0)[:-1])))
    for m in THIS_CALL.finditer(f.code):
        w = one((f.class_at(m.start()).lower(), m.group(1).lower()))
        if w is not None and w.is_method:
            out.append((w, m.start(), m.end() - 1, re.sub(r"\s+", "", m.group(0)[:-1])))
    for m in PLAIN_CALL.finditer(f.code):
        name = m.group(1)
        if name.lower() in NOT_CALLS or re.search(r"(?:function|new)\s*&?\s*$", f.code[max(0, m.start() - 12) : m.start()]):
            continue
        w = one(("", name.lower()))
        if w is not None and (w.rel == rel or (registry is not None and registry.unique("php", name))):
            out.append((w, m.start(), m.end() - 1, name))
    return out


def survey(text: str, rel: str) -> tuple[list[httpwrap.Wrapper], set[str], set[str]]:
    called = set(CALLED.findall(text)) if re.search(r"::|\(", text) else set()
    defined = set(FUNC.findall(text))  # methods counted too: a plain function's name is unique only if no method has it
    if "stripe.com" not in text:
        return [], defined, called
    return wrappers_in(File(text), rel, []), defined, called


def wrappers_in(f: File, rel: str, known: list[httpwrap.Wrapper]) -> list[httpwrap.Wrapper]:
    """Wrappers defined in one file: direct ones, then (twice) functions forwarding their own parameter
    to the path of a wrapper known here or elsewhere."""
    found = [w for fn in f.funcs if (w := _direct(f, fn, rel)) is not None]
    for _ in range(2):
        names = {(w.owner, w.name) for w in found}
        forwards: dict[Func, list[httpwrap.Wrapper | None]] = {}
        for w, start, paren, _ in _calls(f, found + known, rel, None):
            fn = f.func_at(start)
            if fn is not None and (fn.owner, fn.name) not in names:
                forwards.setdefault(fn, []).append(_forward(f, fn, w, paren, rel))
        for fn, got in forwards.items():
            # every wrapper call in it must forward the same way: ``retrieve($path)`` on one branch and
            # ``request($params, $path, $method)`` on another make the method depend on the arguments
            shapes = {None if g is None else (g.base, g.path, g.method, g.method_param, g.body) for g in got}
            if len(shapes) == 1 and got[0] is not None:
                adds = frozenset().union(*(g.adds for g in got))
                first = got[0]
                found.append(httpwrap.Wrapper(
                    first.language, first.rel, first.name, first.owner, first.base, first.path, first.method,
                    first.method_param, first.body, all(g.body_seen for g in got), any(g.rewrites for g in got),
                    adds, first.defaults, first.is_method, first.line,
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
    if w.body is not None:
        got = f.pieces(*args[w.body]) if w.body < len(args) else []
        if len(got) == 1 and got[0][0] == "var" and got[0][1] in fn.params:
            body = fn.params.index(got[0][1])
            more, keys = _writes(f, fn, got[0][1], fn.close)
            rewrites, adds = rewrites or more, adds | keys
        elif got or not _empty(w.defaults[w.body] if w.body < len(w.defaults) else None):
            body_seen = False
    return httpwrap.Wrapper(
        "php", rel, fn.name, fn.owner, w.base, fn.params.index(pieces[0][1]), method, method_param, body, body_seen,
        rewrites, frozenset(adds), fn.defaults, fn.is_method, f.line(fn.paren),
    )


def sites(text: str, rel: str, index, reads: frozenset[str], language: str, registry) -> list:
    known = [w for w in (registry.wrappers if registry is not None else []) if w.language == "php"]
    if not known and "stripe.com" not in text:
        return []
    f = File(text)
    here = wrappers_in(f, rel, known) if "stripe.com" in text or known else []
    wrappers = here + [w for w in known if w.rel != rel]
    out = httpphppath.constant_sites(f, text, rel, index, reads, language) if f.consts else []
    for w, start, paren, display in _calls(f, wrappers, rel, registry):
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
        fields = Fields()
        if w.body is not None:
            if w.body < len(args):
                call = phpparse.Call(f.line(start), display, None, paren, args, w.body)
                from .phphelpers import PhpHelpers, Via

                phpparse.collect(f.parsed, call, fields, Via(PhpHelpers(f.parsed)))
            elif not _empty(w.defaults[w.body] if w.body < len(w.defaults) else None):
                fields.hide("")
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
