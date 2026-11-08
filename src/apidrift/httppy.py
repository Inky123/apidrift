"""Python raw-HTTP Stripe calls, for :mod:`httpwrap`.

Two shapes: stripe-python's own ``client.raw_request("post", "/v1/fx_quotes", to_currency="usd")`` (and
``raw_request_async``) on a ``StripeClient`` or the ``stripe`` module, whose keyword arguments are the
request fields; and an app function whose URL is a Stripe base joined with one of its parameters
(``requests.post(f"{STRIPE_API}/{path}", data=params)``), called with a literal path.
"""

from __future__ import annotations

import ast
import re

from . import httpurl, httpwrap

RAW = {"raw_request", "raw_request_async"}
OPTIONS = {
    "stripe_version", "stripe_account", "stripe_context", "api_key", "idempotency_key", "api_mode", "headers",
    "max_network_retries", "usage", "base_address", "options",
}
HTTP_LIBS = {"requests", "httpx", "session", "client", "http", "aiohttp"}
VERBS = {"get", "post", "delete", "request"}
BODY_KW = ("data", "json", "params")


def _pieces(node: ast.AST, consts: dict[str, str]) -> list[str | None]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr):
        out: list[str | None] = []
        for v in node.values:
            if isinstance(v, ast.Constant):
                out.append(str(v.value))
            elif isinstance(v, ast.FormattedValue) and isinstance(v.value, ast.Name) and v.value.id in consts:
                out.append(consts[v.value.id])
            else:
                out.append(None)
        return out
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _pieces(node.left, consts) + _pieces(node.right, consts)
    if isinstance(node, ast.Name) and node.id in consts:
        return [consts[node.id]]
    return [None]


def _dict_fields(node: ast.AST, prefix: str, out: set[str], hidden: set[str]) -> None:
    if isinstance(node, ast.Dict):
        for k, v in zip(node.keys, node.values):
            if k is None or not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
                hidden.add(prefix + "." if prefix else "")
                continue
            path = f"{prefix}.{k.value}" if prefix else k.value
            out.add(path)
            _value_fields(v, path, out, hidden)


def _value_fields(v: ast.AST, path: str, out: set[str], hidden: set[str]) -> None:
    if isinstance(v, ast.Dict):
        _dict_fields(v, path, out, hidden)
    elif isinstance(v, (ast.List, ast.Tuple)):
        for e in v.elts:
            if isinstance(e, ast.Dict):
                _dict_fields(e, path + "[]", out, hidden)


def _consts(tree: ast.Module) -> dict[str, str]:
    out: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                out[node.targets[0].id] = node.value.value
    return out


def _clients(tree: ast.Module) -> tuple[set[str], set[str]]:
    """(names, ``self`` attributes) bound to a ``StripeClient(...)``, plus the names the stripe module has."""
    names, attrs = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.asname or a.name for a in node.names if a.name == "stripe"}
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            f = node.value.func
            called = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ""
            if called == "StripeClient":
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        names.add(t.id)
                    elif isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == "self":
                        attrs.add(t.attr)
    return names, attrs


def _raw_sites(tree, text, rel, index, reads, language, consts) -> list:
    names, attrs = _clients(tree)
    out = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in RAW):
            continue
        recv = node.func.value
        ok = (isinstance(recv, ast.Name) and recv.id in names) or (
            isinstance(recv, ast.Attribute) and isinstance(recv.value, ast.Name) and recv.value.id == "self"
            and recv.attr in attrs)
        if not ok or len(node.args) < 2 or any(isinstance(a, ast.Starred) for a in node.args):
            continue
        m = node.args[0]
        method = httpurl.method_word(m.value) if isinstance(m, ast.Constant) and isinstance(m.value, str) else None
        fields, hidden = set(), set()
        for kw in node.keywords:
            if kw.arg is None:
                hidden.add("")
            elif kw.arg not in OPTIONS:
                fields.add(kw.arg)
                _value_fields(kw.value, kw.arg, fields, hidden)
        if len(node.args) > 2:
            hidden.add("")  # params passed positionally: not read
        w = httpwrap.Wrapper("python", rel, node.func.attr, "", "https://api.stripe.com", 1, method)
        u = httpwrap.build(text, rel, index, w, node.lineno, ast.unparse(node.func), _pieces(node.args[1], consts),
                           method, fields, hidden, reads, language)
        if u is not None:
            out.append(u)
    return out


def _wrappers(tree: ast.Module, rel: str, consts: dict[str, str]) -> list[httpwrap.Wrapper]:
    out = []
    for cls, fn in _functions(tree):
        params = [a.arg for a in fn.args.args]
        defaults = [None] * (len(params) - len(fn.args.defaults)) + [
            d.value if isinstance(d, ast.Constant) and isinstance(d.value, str) else None for d in fn.args.defaults]
        for node in ast.walk(fn):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in VERBS):
                continue
            lib = node.func.value
            root = lib.id if isinstance(lib, ast.Name) else lib.attr if isinstance(lib, ast.Attribute) else ""
            if root not in HTTP_LIBS and not root.endswith(("session", "client")):
                continue
            url_at = 1 if node.func.attr == "request" else 0
            url = node.args[url_at] if len(node.args) > url_at else next(
                (k.value for k in node.keywords if k.arg == "url"), None)
            if url is None:
                continue
            pieces = _url_param(url, consts, params)
            if pieces is None:
                continue
            base, path = pieces
            method, method_param = (node.func.attr if node.func.attr != "request" else None), None
            if node.func.attr == "request" and node.args:
                a = node.args[0]
                if isinstance(a, ast.Name) and a.id in params:
                    method_param = params.index(a.id)
                elif isinstance(a, ast.Constant) and isinstance(a.value, str):
                    method = httpurl.method_word(a.value)
            body, seen = None, True
            for k in node.keywords:
                if k.arg in BODY_KW:
                    if isinstance(k.value, ast.Name) and k.value.id in params and body is None:
                        body = params.index(k.value.id)
                    else:
                        seen = False
                elif k.arg is None:
                    seen = False
            rewrites = body is not None and any(
                isinstance(n, (ast.Assign, ast.AugAssign)) and any(
                    isinstance(t, ast.Name) and t.id == params[body] for t in (n.targets if isinstance(n, ast.Assign) else [n.target]))
                for n in ast.walk(fn))
            out.append(httpwrap.Wrapper(
                "python", rel, fn.name, cls, base, params.index(path), method, method_param, body, seen, rewrites,
                frozenset(), tuple(defaults), bool(cls), fn.lineno,
            ))
            break
    return out


def _url_param(node, consts, params) -> tuple[str, str] | None:
    """(base, parameter) for a URL that is a Stripe base followed by exactly one parameter."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add) and isinstance(node.right, ast.Name):
        left = _pieces(node.left, consts)
        name = node.right.id
    elif isinstance(node, ast.JoinedStr) and node.values and isinstance(node.values[-1], ast.FormattedValue):
        last = node.values[-1].value
        if not isinstance(last, ast.Name):
            return None
        left = _pieces(ast.JoinedStr(values=node.values[:-1]), consts)
        name = last.id
    else:
        return None
    if None in left or name not in params:
        return None
    base = "".join(left)
    return (base, name) if httpurl.is_base(base) else None


def _functions(tree: ast.Module):
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield "", node
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    yield node.name, item


def _parse(text: str):
    try:
        return ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return None


def survey(text: str, rel: str):
    defined = set(re.findall(r"(?m)^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)", text))
    called = set(re.findall(r"([A-Za-z_]\w*)\s*\(", text)) if defined or "(" in text else set()
    if "stripe.com" not in text:
        return [], defined, called
    tree = _parse(text)
    return (_wrappers(tree, rel, _consts(tree)) if tree else []), defined, called


def sites(text: str, rel: str, index, reads: frozenset[str], language: str, registry) -> list:
    known = [w for w in (registry.wrappers if registry is not None else []) if w.language == "python" and w.rel != rel]
    if not known and "stripe.com" not in text and "raw_request" not in text:
        return []
    tree = _parse(text)
    if tree is None:
        return []
    consts = _consts(tree)
    out = _raw_sites(tree, text, rel, index, reads, language, consts) if "raw_request" in text else []
    here = _wrappers(tree, rel, consts)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        for w in here + known:
            if isinstance(f, ast.Name) and f.id == w.name and not w.is_method:
                if w.rel != rel and not (registry and registry.unique("python", w.name)):
                    continue
            elif isinstance(f, ast.Attribute) and f.attr == w.name and w.is_method:
                if not (isinstance(f.value, ast.Name) and f.value.id == "self" and w.rel == rel):
                    continue
            else:
                continue
            u = _call_site(node, w, text, rel, index, reads, language, consts)
            if u is not None:
                out.append(u)
    return out


def _call_site(node: ast.Call, w: httpwrap.Wrapper, text, rel, index, reads, language, consts):
    offset = 1 if w.is_method else 0  # ``self`` is not passed
    if any(isinstance(a, ast.Starred) for a in node.args) or any(k.arg is None for k in node.keywords):
        return None

    def arg(i: int):
        if i - offset < len(node.args) and i - offset >= 0:
            return node.args[i - offset]
        return None

    path = arg(w.path)
    if path is None:
        return None
    method = w.method
    if w.method_param is not None:
        m = arg(w.method_param)
        if m is None and w.method_param < len(w.defaults):
            method = httpurl.method_word(w.defaults[w.method_param])
        else:
            method = httpurl.method_word(m.value) if isinstance(m, ast.Constant) and isinstance(m.value, str) else None
    fields, hidden = set(), set()
    if w.body is not None:
        b = arg(w.body)
        if isinstance(b, ast.Dict):
            _dict_fields(b, "", fields, hidden)
        elif b is not None and not (isinstance(b, ast.Constant) and b.value is None):
            hidden.add("")
    return httpwrap.build(text, rel, index, w, node.lineno, w.name, _pieces(path, consts), method, fields, hidden,
                          reads, language)
