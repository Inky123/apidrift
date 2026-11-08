"""Stripe REST calls made with an HTTP client of the app's own: the URL and form pieces every reader shares.

A request URL is read as a template: literal text, plus ``{x}`` wherever an expression is joined in
(``'.../v1/tokens/' . $id``, ``"invoices/" + url.PathEscape(id)``, ``f"/v1/x/{id}"``). A placeholder
that fills a whole path segment matches the spec's parameter there (``/v1/tokens/{token}``); one glued to
literal text in the same segment (``'payment_intents' . $suffix``) could be anything, so nothing is
matched. Form keys (``line_items[0][price_data][currency]``) become the dotted field paths the SDK
readers produce (``line_items[].price_data.currency``).
"""

from __future__ import annotations

import re

HOLE = "{x}"
# A full Stripe API URL (``files.stripe.com`` serves file uploads under the same paths) or just its base.
URL = re.compile(r"^https://(?:api|files)\.stripe\.com(/v\d+(?:/[^?#\s]*)?)(?:\?([^#\s]*))?$")
BASE = re.compile(r"^https://api\.stripe\.com(?:/v\d+)?/?$")
METHODS = {"get", "post", "delete"}
WORD_METHOD = re.compile(r"^(?:http\.Method)?(get|post|delete)$", re.I)
FORM_KEY = re.compile(r"^([A-Za-z_]\w*)((?:\[[^\[\]]*\])*)$")
BRACKET = re.compile(r"\[([^\[\]]*)\]")
# ``'method' => 'GET'``, ``method: "post"``, ``method="DELETE"``, ``-X GET``, ``http.MethodGet``
EXPLICIT = re.compile(
    r"""(?:\bmethod['"]?\s*(?:=>|:|=)\s*['"]|-X\s*['"]?|--request\s+['"]?|CURLOPT_CUSTOMREQUEST\s*,\s*['"])"""
    r"""(get|post|delete)\b|\bhttp\.Method(Get|Post|Delete)\b""",
    re.I,
)
CALLEE_METHOD = re.compile(r"(?:^|[._])(get|post|delete)$", re.I)  # requests.get, wp_remote_post, conn.delete
DEFAULT_GET = {"fetch", "wp_remote_request", "wp_safe_remote_request", "file_get_contents", "urlopen", "Request"}
NEW_REQUEST = {"NewRequest", "NewRequestWithContext", "request", "request_async", "raw_request", "raw_request_async"}


def template(pieces: list[str | None]) -> str:
    """Literal pieces joined, every run of non-literal ones (None) one hole."""
    out: list[str] = []
    for p in pieces:
        if p is None and out and out[-1] == HOLE:
            continue
        out.append(HOLE if p is None else p)
    return "".join(out)


def split(url: str) -> tuple[str, str] | None:
    """(path, query) of a Stripe URL template, or None when it is not one or a hole shares a path segment
    with literal text (``/v1/payment_intents{x}``: an id, a suffix or another endpoint)."""
    m = URL.match(url)
    if m is None:
        return None
    path, query = m.group(1), m.group(2) or ""
    for segment in path.strip("/").split("/"):
        if HOLE in segment and segment != HOLE:
            return None
    return path.rstrip("/") or path, query


def is_base(value: str) -> bool:
    return bool(BASE.match(value))


def spec_path(index, url: str) -> tuple[str, str] | None:
    """(spec path, query) a URL template calls, or None. An ``index.match_path`` question only."""
    parts = split(url)
    if parts is None:
        return None
    path = parts[0].replace(HOLE, "{id}")
    found = index.match_path(path)
    return (found, parts[1]) if found else None


def method_word(word: str | None) -> str | None:
    """``GET`` / ``"post"`` / ``http.MethodDelete`` as a lowercase HTTP method, else None."""
    if not word:
        return None
    m = WORD_METHOD.match(word.strip().strip("'\""))
    return m.group(1).lower() if m else None


def pick(index, path: str, method: str | None, explicit: bool = True) -> str:
    """The method a site is reported with: the one the code names when the path has it; when the code names
    none, the path's only method; else ``?`` (an explicit method the path lacks means the path is unsure)."""
    available = index.methods_for(path)
    if method in available:
        return method
    if method is None or not explicit:
        return available[0] if len(available) == 1 else "?"
    return "?"


def form_paths(key: str) -> list[str] | None:
    """Field paths of one form-encoded key: ``line_items[0][price_data][currency]`` ->
    ``line_items``, ``line_items[].price_data``, ``line_items[].price_data.currency``. None: not a plain key."""
    m = FORM_KEY.match(key)
    if m is None:
        return None
    cur, out, listed = m.group(1), [m.group(1)], False
    for part in BRACKET.findall(m.group(2)):
        if part == "" or part.isdigit():
            if listed:
                cur += "[]"
            listed = True
            continue
        if not re.fullmatch(r"[A-Za-z_]\w*", part):
            return None
        cur = f"{cur}[].{part}" if listed else f"{cur}.{part}"
        listed = False
        out.append(cur)
    return out


def query_fields(query: str) -> tuple[set[str], bool]:
    """(field paths, all visible) of a literal query string such as ``limit=100&expand[]=data.x``."""
    paths: set[str] = set()
    visible = True
    for pair in filter(None, query.split("&")):
        key = pair.split("=", 1)[0]
        got = None if HOLE in key else form_paths(key)
        if got is None:
            visible = False
        else:
            paths.update(got)
    return paths, visible


def callee_method(callee: str) -> str | None:
    """The method an HTTP client function's name says: ``wp_remote_get``, ``requests.post``, ``http.Get``."""
    last = re.split(r"[.:>\\]+", callee)[-1]
    if last in DEFAULT_GET:
        return "get"
    if last == "PostForm":
        return "post"
    m = CALLEE_METHOD.search(last)
    return m.group(1).lower() if m else None


def explicit_method(args: str) -> str | None:
    """A method named outright in a call's argument text, when exactly one is."""
    named = {(m.group(1) or m.group(2)).lower() for m in EXPLICIT.finditer(args)}
    return named.pop() if len(named) == 1 else None
