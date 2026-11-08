"""API-version pins near the fixed files: reported in the fix pull request, never edited.

A pin is an assignment whose name says it holds the Stripe API version (``apiVersion:``, ``api_version =``,
``'Stripe-Version' =>``, ``setApiVersion(``, and the client or per-request ``stripe_version=`` /
``stripe_version:`` of stripe-python and stripe-ruby). Its value is read as one expression up to the end of the
statement (a top-level ``,`` / ``;`` / closer / assignment, or a line break that does not continue it), and
every version-shaped string literal in that expression is a pin: ``Rails.env.test? ? "2023-10-16" :
"2023-10-16; x_beta=v1"`` pins 2023-10-16 in both branches, ``os.environ.get("V", "2020-08-27")`` pins its
default. A string after the statement ends (``apiVersion: v, created: '2024-01-01'``) is not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from . import scanner
from .jsparse import Mask, Source
from .scanner import LANGUAGES

MAX_PINS = 100
MAX_EXPRESSION = 400  # characters read after the name: a pin's value is short
PROJECT_MARKERS = scanner.PROJECT_FILES | {"requirements.txt", "Pipfile", "setup.cfg", "Gemfile.lock", "deno.json"}
PIN_HEAD = re.compile(
    r"""(?:\bapiVersion|\bapi_version|\bstripe_version|['"]?Stripe-Version['"]?)\s*(?:=>|[:=])"""
    r"""|['"](?:api|stripe)_version['"]\s*=>"""
    r"""|\bsetApiVersion\("""
)
VERSION = re.compile(r"(\d{4}-\d{2}-\d{2}(?:\.[A-Za-z]\w*)?)(?:;[^\n]*)?")  # the whole string; a beta suffix after ";"
# A line break continues the value only after an operator that cannot end a statement (or before one that
# cannot start a statement, in JS-like languages). Never on words (``if``, ``unless``, ``else``, ``or``): in Ruby
# and Python those start or modify a new statement. Ruby/Python ``?``/``:`` end method names and block heads.
TRAILING = {"any": ("||", "&&", "+", "-", "*", "\\"), "js": ("||", "&&", "??", "+", "-", "*", "?", ":", "\\")}
LEADING_JS = ("?", ":", "||", "&&", "??")
JS_LIKE = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts", ".vue", ".svelte", ".php"}


KINDS = ("server", "test", "browser")
JS_SUFFIXES = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts", ".vue", ".svelte"}
BROWSER_DIRS = {"client", "public", "static"}
LOAD_STRIPE = re.compile(r"(?<![\w$.])loadStripe\s*\(")  # @stripe/stripe-js: always the browser
NEW_STRIPE = re.compile(r"\bnew\s+Stripe\s*\(")
STRIPE_JS_URL = re.compile(r"js\.stripe\.com/")
# Any import of the server SDK: require/import()/import/from with 'stripe', 'stripe/...', 'npm:stripe@14',
# 'https://esm.sh/stripe@14?target=deno' (never '@stripe/stripe-js' or 'stripe-js').
SERVER_IMPORT = re.compile(
    r"""(?:\brequire\s*\(\s*|\bimport\s*\(\s*|\bfrom\s+|\bimport\s+)(['"`])"""
    r"""(?:npm:|https?://[^'"`\s]*/)?stripe(?:@[^'"`/?\s]*)?(?:[/?][^'"`\s]*)?\1"""
)


@dataclass(frozen=True)
class Pin:
    file: str
    line: int
    version: str
    kind: str = "server"  # one of KINDS: a test file, browser Stripe.js, or anything else (the server)


def test_path(rel: str) -> bool:
    """The scanner's test and fixture rules (directories like ``spec/``, ``__tests__/``, ``fixtures/``; names like
    ``*_spec.rb``, ``*.test.ts``): such a pin does not set the version the application's requests use."""
    *dirs, name = rel.split("/")
    return any(d in scanner.TEST_DIRS or d in scanner.DOC_DIRS for d in dirs) or bool(scanner.TEST_FILE.match(name))


def pin_kind(rel: str, text: str, pos: int) -> str:
    return Kinds(rel, text).of(pos)


class Kinds:
    """Which kind each pin in one file is, read once per file. "browser" needs positive evidence and a file that
    never imports the server SDK in any form: the pin sits in ``loadStripe(...)``'s arguments (outside strings
    and comments), or the file loads ``js.stripe.com``, or it lives under a client/, public/ or static/
    directory without writing ``new Stripe(``. Everything else that is not a test file is the server's."""

    def __init__(self, rel: str, text: str) -> None:
        self.test = test_path(rel)
        self.js = Path(rel).suffix.lower() in JS_SUFFIXES
        self.calls: list[tuple[int, int]] = []
        self.whole = False
        if self.test or not self.js or SERVER_IMPORT.search(text):
            self.js = False  # a server file: no browser evidence counts
            return
        mask, src = Mask(text, ruby=False), None
        for m in LOAD_STRIPE.finditer(text):
            if not mask.covers(m.start()):
                src = src or Source(text)
                self.calls.append((m.end() - 1, src.skip_balanced(m.end() - 1)))
        code_new = any(not mask.covers(m.start()) for m in NEW_STRIPE.finditer(text))
        dirs = set(rel.split("/")[:-1])
        self.whole = not code_new and (STRIPE_JS_URL.search(text) is not None or bool(dirs & BROWSER_DIRS))

    def of(self, pos: int) -> str:
        if self.test:
            return "test"
        if self.js and (self.whole or any(a < pos < b for a, b in self.calls)):
            return "browser"
        return "server"


def pin_versions(text: str, js: bool = True) -> list[tuple[int, str]]:
    """(offset, version) of every pinned version string in ``text``, in order, once per statement. ``js``: a
    language where a line break never ends a statement on its own (JS/TS, PHP); False for Ruby and Python."""
    out: list[tuple[int, str]] = []
    for m in PIN_HEAD.finditer(text):
        line = text[text.rfind("\n", 0, m.start()) + 1 : m.start()].lstrip()
        if line.startswith(("//", "#", "/*")) or (line.startswith("*") and not line.startswith("*/")):
            continue  # a commented-out line: not a pin
        seen: set[str] = set()
        for pos, version in _strings(text, m.end(), js):
            if version not in seen:
                seen.add(version)
                out.append((pos, version))
    return out


def _strings(text: str, i: int, js: bool = True) -> list[tuple[int, str]]:
    """Version strings in the expression starting at ``i``."""
    n = len(text)
    while i < n and text[i] in " \t":
        i += 1
    if i < n and text[i] in "=~>":  # ``api_version == x``, ``=~``: a comparison, not an assignment
        return []
    stop, depth, found, code_end = min(n, i + MAX_EXPRESSION), 0, [], i  # code_end: past the last code character
    while i < stop:
        c = text[i]
        if not c.isspace() and c != "#" and not text.startswith(("//", "/*"), i):
            code_end = i + 1
        if c in "'\"`":
            end = _string_end(text, i)
            if end < 0:
                return found
            m = VERSION.fullmatch(text, i + 1, end)
            if m:
                found.append((i, m.group(1)))
            i = code_end = end + 1
        elif c in "([{":
            depth += 1
            i += 1
        elif c in ")]}":
            if depth == 0:
                break
            depth -= 1
            i += 1
        elif c in ",;" and depth == 0:
            break
        elif c == "=" and depth == 0 and text[i - 1] not in "=!<>" and text[i + 1 : i + 2] not in ("=", ">", "~"):
            break  # a second assignment: another statement
        elif text.startswith("/*", i):
            close = text.find("*/", i + 2)
            i = n if close < 0 else close + 2
        elif c == "#" or text.startswith("//", i):
            nl = text.find("\n", i)
            i = n if nl < 0 else nl
        elif c == "\n" and depth == 0:
            if not _continues(text, code_end, i, js):
                break
            i += 1
        else:
            i += 1
    return found


def _string_end(text: str, i: int) -> int:
    """Index of the closing quote of the one-line string at ``i`` (-1 when it does not close on its line)."""
    quote, j = text[i], i + 1
    while j < len(text):
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if c == quote:
            return j
        if c == "\n" and quote != "`":
            return -1
        j += 1
    return -1


def _continues(text: str, code_end: int, nl: int, js: bool) -> bool:
    """A line break inside the value: the code before it ends in a binary operator, or (JS-like) the next line
    starts with one. ``code_end``: just past the last code character before the break (comments excluded)."""
    if text[max(0, code_end - 2) : code_end].endswith(TRAILING["js" if js else "any"]):
        return True
    if not js:
        return False
    j = nl + 1
    while j < len(text) and text[j] in " \t":
        j += 1
    return text.startswith(LEADING_JS, j)


def find_pins(root: Path, fixed: list[str]) -> list[Pin]:
    """API-version pins (``apiVersion:``, ``api_version =``, ``Stripe-Version``) in the source files of the
    projects that hold the fixed files: the nearest ancestor directory with a project file (package.json,
    requirements.txt, Gemfile, ...), else the file's own directory. Reported for the pull request body;
    never edited. A browser bundle's Stripe.js pin elsewhere in the repository is not the server's pin.
    """
    root = Path(root).resolve()
    if not fixed or not root.exists():
        return []
    if root.is_file():
        files, base = [root], root.parent
    else:
        base = root
        files, seen = [], set()
        for project in sorted({project_root(base, f) for f in fixed}):
            for path in scanner._iter_files(project)[0]:
                if path not in seen:
                    seen.add(path)
                    files.append(path)
    pins: list[Pin] = []
    for path in sorted(files):
        if path.suffix.lower() not in LANGUAGES:
            continue
        text = scanner._read(path)
        if text is None or "stripe" not in text.lower():
            continue
        rel = path.relative_to(base).as_posix()
        kinds = None
        for pos, version in pin_versions(text, path.suffix.lower() in JS_LIKE):
            kinds = kinds or Kinds(rel, text)  # once per file
            pins.append(Pin(rel, text.count("\n", 0, pos) + 1, version, kinds.of(pos)))
            if len(pins) >= MAX_PINS:
                return pins
    return pins


def project_root(base: Path, rel: str) -> Path:
    here = (base / rel).parent
    for d in [here, *here.parents]:
        if d == base or any((d / marker).exists() for marker in PROJECT_MARKERS):
            return d
        if base not in d.parents:
            break
    return here
