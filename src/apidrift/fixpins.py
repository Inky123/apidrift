"""API-version pins near the fixed files: reported in the fix pull request, never edited.

A pin is an assignment whose name says it holds the Stripe API version (``apiVersion:``, ``api_version =``,
``'Stripe-Version' =>``, ``setApiVersion(``). Its value is read as one expression up to the end of the
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
from .jsparse import Source
from .scanner import LANGUAGES

MAX_PINS = 100
MAX_EXPRESSION = 400  # characters read after the name: a pin's value is short
PROJECT_MARKERS = scanner.PROJECT_FILES | {"requirements.txt", "Pipfile", "setup.cfg", "Gemfile.lock", "deno.json"}
PIN_HEAD = re.compile(
    r"""(?:\bapiVersion|\bapi_version|['"]?Stripe-Version['"]?)\s*(?:=>|[:=])|['"](?:api|stripe)_version['"]\s*=>"""
    r"""|\bsetApiVersion\("""
)
VERSION = re.compile(r"(\d{4}-\d{2}-\d{2}(?:\.[A-Za-z]\w*)?)(?:;[^\n]*)?")  # the whole string; a beta suffix after ";"
CONTINUES = ("?", ":", "||", "&&", "??", ".", "+", "or ", "and ", "if ", "else", "unless ")


KINDS = ("server", "test", "browser")
JS_SUFFIXES = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts", ".vue", ".svelte"}
# Stripe.js: ``loadStripe(pk, {apiVersion})`` (@stripe/stripe-js) or the global ``Stripe(pk, {apiVersion})``.
BROWSER_CALL = re.compile(r"(?<![\w$.])(loadStripe|Stripe)\s*\(")
NEW_STRIPE = re.compile(r"\bnew\s+Stripe\s*\(")
SERVER_IMPORT = re.compile(r"""\brequire\(\s*['"]stripe['"]\s*\)|\bfrom\s+['"]stripe['"]|\bimport\s+['"]stripe['"]""")


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
    if test_path(rel):
        return "test"
    if Path(rel).suffix.lower() in JS_SUFFIXES and _in_browser_call(text, pos):
        return "browser"
    return "server"


def _in_browser_call(text: str, pos: int) -> bool:
    """The pin sits in the arguments of ``loadStripe(...)``, or of a bare ``Stripe(...)`` (not ``new Stripe``) in
    a file that never imports the server SDK (``require('stripe')``, ``from 'stripe'``); or the file is Stripe.js
    code throughout: it calls one of those, never imports the server SDK and never writes ``new Stripe(``."""
    server = SERVER_IMPORT.search(text) is not None
    if not server and not NEW_STRIPE.search(text) and any(_browser_call(text, m) for m in BROWSER_CALL.finditer(text)):
        return True  # a Stripe.js file (``config.apiVersion = '...'; Stripe(pk, config)``): no server SDK in it at all
    src = None
    for m in reversed(list(BROWSER_CALL.finditer(text, max(0, pos - 2000), pos))):
        if m.group(1) == "Stripe" and (server or not _browser_call(text, m)):
            continue
        src = src or Source(text)
        if src.skip_balanced(m.end() - 1) > pos:
            return True
    return False


def _browser_call(text: str, m: re.Match[str]) -> bool:
    """``loadStripe(``, or ``Stripe(`` that is not ``new Stripe(``."""
    return m.group(1) == "loadStripe" or not re.search(r"\bnew\s*$", text[max(0, m.start() - 8) : m.start()])


def pin_versions(text: str) -> list[tuple[int, str]]:
    """(offset, version) of every pinned version string in ``text``, in order, once per statement."""
    out: list[tuple[int, str]] = []
    for m in PIN_HEAD.finditer(text):
        seen: set[str] = set()
        for pos, version in _strings(text, m.end()):
            if version not in seen:
                seen.add(version)
                out.append((pos, version))
    return out


def _strings(text: str, i: int) -> list[tuple[int, str]]:
    """Version strings in the expression starting at ``i``."""
    n = len(text)
    while i < n and text[i] in " \t":
        i += 1
    if i < n and text[i] in "=~>":  # ``api_version == x``, ``=~``: a comparison, not an assignment
        return []
    stop, depth, found = min(n, i + MAX_EXPRESSION), 0, []
    while i < stop:
        c = text[i]
        if c in "'\"`":
            end = _string_end(text, i)
            if end < 0:
                return found
            m = VERSION.fullmatch(text, i + 1, end)
            if m:
                found.append((i, m.group(1)))
            i = end + 1
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
        elif c == "#" or text.startswith("//", i):
            nl = text.find("\n", i)
            i = n if nl < 0 else nl
        elif c == "\n" and depth == 0:
            if not _continues(text, i):
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


def _continues(text: str, nl: int) -> bool:
    """A line break inside the value: the line ends in an operator, or the next line starts with one."""
    before = text[:nl].rstrip()
    if before.endswith(("?", ":", "||", "&&", "??", "(", "+", " or", " and", " if", " else", "\\")):
        return True
    after = text[nl + 1 :].lstrip(" \t")
    return after.startswith(CONTINUES)


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
        for pos, version in pin_versions(text):
            pins.append(Pin(rel, text.count("\n", 0, pos) + 1, version, pin_kind(rel, text, pos)))
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
