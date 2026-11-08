"""Stripe REST calls made through the app's own HTTP wrapper: ``request($body, $api, $method = 'POST')``.

A function or method is a wrapper when the URL it requests is a Stripe base (a literal, or a constant or
field holding one in the same file) joined with one of its parameters, inside a call of an HTTP client
(``wp_remote_post(self::ENDPOINT . $api, ...)``, ``http.NewRequest(method, "https://api.stripe.com/v1/"+path, ...)``).
Its other parameters are read for the method (a parameter, with its literal default) and the request body.
A function that only forwards its own parameter to a wrapper's path is one too.

Every call of a wrapper whose path argument is a literal (or literals joined with expressions, each of
which then fills one path segment) is a call site at the caller's line, mapped through the spec's paths
(``index.match_path`` / ``methods_for``, the questions the URL scan already asks). Its fields are the body
argument's keys when that is a visible literal; when the wrapper changes the body before sending it
(``$request = apply_filters(...)``), or the body is not visible, unseen keys may be sent.

Sites are language ``http``: detection only, the fixer never edits them (the same request arrays are
often read back, ``$request['payment_method_types']``, so a key rename there could change the app).

Same-file wrappers are always followed. Across files, :class:`Registry` collects the wrappers of every
file in one pass (``collect``), then names the files that call one (``finish``): a PHP static call names
its class, a Go call its package directory, and any other call counts only when no other function in the
repository (Go: in the package) has that name.
"""

from __future__ import annotations

import re
import re
from dataclasses import dataclass, field

from . import httpurl

MAX_WRAPPERS = 200  # wrappers kept per repository
MAX_FILES = 400  # caller files rescanned
OVER_CAP = "raw-HTTP wrapper caller"  # the ``unread`` entry for caller files past MAX_FILES


@dataclass(frozen=True)
class Wrapper:
    language: str  # php / go / python
    rel: str
    name: str
    owner: str  # PHP class (fully qualified) / Python class / Go receiver type; "" for a plain PHP / Python function
    base: str  # what the path parameter is appended to: "https://api.stripe.com/v1/"
    path: int  # index of the path parameter
    method: str | None = None  # the method every call uses, when the wrapper fixes it
    method_param: int | None = None  # index of the method parameter
    body: int | None = None  # index of the request-body parameter; None: the wrapper sends no body
    body_seen: bool = True  # False: a body may be sent that is not any parameter (unseen keys)
    rewrites: bool = False  # the wrapper changes the body before sending it: unseen keys may be added
    adds: frozenset[str] = frozenset()  # literal keys the wrapper writes into the body itself
    defaults: tuple[str | None, ...] = ()  # each parameter's default as source text, None for none
    is_method: bool = False  # a method (``$this->x()`` / ``self.x()`` / ``c.x()``), not a plain function
    line: int = 0
    names: tuple[str, ...] = ()  # parameter names, for arguments passed by keyword (Python)
    field: tuple[str, str] | None = None  # Go: the (struct type, field) the base is read from
    # keys the wrapper writes only under a condition: (field path, the literal the path argument must
    # equal for it to be written, or None when the condition is not known)
    maybe: tuple[tuple[str, str | None], ...] = ()
    filters: frozenset[str] = frozenset()  # PHP: filter hooks the body passes through (rewrites if hooked)


@dataclass
class Registry:
    """The wrappers of a whole checkout and the files that call them."""

    collecting: bool = True
    wrappers: list[Wrapper] = field(default_factory=list)
    names: dict[str, dict[str, int]] = field(default_factory=dict)  # language -> function name -> definitions
    calls: dict[str, dict[str, bool]] = field(default_factory=dict)  # rel -> name called -> with a string literal
    go_files: dict[str, list[str]] = field(default_factory=dict)  # package directory -> its Go files
    go_evidence: dict[str, list] = field(default_factory=dict)  # directory -> struct field assignments in it
    hooks: set[str] = field(default_factory=set)  # WordPress filter hooks the checkout registers (phpfilters)
    rescan: set[str] = field(default_factory=set)
    skipped: int = 0  # caller files past MAX_FILES, not read for wrapper calls

    def collect(self, text: str, rel: str) -> None:
        """Note the wrappers one file defines, the function names it defines and the names it calls."""
        from . import httpgo, httpphp, httppy, phpfilters

        lang = _language(rel)
        if lang == "go":
            self.go_files.setdefault(_dir(rel), []).append(rel)
            found, defined, called = httpgo.survey(text, rel)
        elif lang in ("php", "python"):
            if lang == "php" and "add_filter" in text:
                self.hooks |= phpfilters.registered({rel: text})
            found, defined, called = (httpphp if lang == "php" else httppy).survey(text, rel)
        else:
            return
        names = self.names.setdefault(lang, {})
        for name in defined:
            key = name.lower() if lang == "php" else name
            names[key] = names.get(key, 0) + 1
        if len(self.wrappers) < MAX_WRAPPERS:
            self.wrappers += found[: MAX_WRAPPERS - len(self.wrappers)]
        if called:
            self.calls[rel] = called

    def finish(self, read=None) -> None:
        """Decide which files are read again for wrapper calls: in PHP those naming a wrapper's class or
        function, in Python those calling a wrapper's name, in Go every file of a wrapper's package. Files
        that call one with a literal argument come first; past MAX_FILES the rest are counted in ``skipped``.
        ``read(rel)`` gives a file's text, for the package-wide evidence a Go field base needs."""
        from . import httpgo

        self.collecting = False
        for directory in sorted({_dir(w.rel) for w in self.wrappers if w.language == "go" and w.field}):
            if read is None:
                continue
            assigns = []
            for rel in self.go_files.get(directory, []):
                if not rel.endswith("_test.go"):
                    assigns += httpgo.evidence(read(rel) or "")
            self.go_evidence[directory] = assigns
        defining = {w.rel for w in self.wrappers}  # read again even when a wrapper is dropped below
        self.wrappers = [w for w in self.wrappers if not (w.language == "go" and w.field and _dir(w.rel) in self.go_evidence
                         and httpgo.field_base(self.go_evidence[_dir(w.rel)], *w.field) != w.base)]
        keys: dict[str, set[str]] = {"php": set(), "python": set()}
        for w in self.wrappers:
            if w.language == "php":
                keys["php"].add((w.owner.rsplit("\\", 1)[-1] if w.owner else w.name).lower())
            elif w.language == "python" and not w.is_method:
                keys["python"].add(w.name)
        ranked: list[tuple[int, str]] = []
        for rel, called in self.calls.items():
            lang = _language(rel)
            hits = [name for name in called if (name.lower() if lang == "php" else name) in keys.get(lang, ())]
            mine = {w.rel for w in self.wrappers if w.language == lang}
            if hits and mine - {rel}:
                ranked.append((0 if any(called[h] for h in hits) else 1, rel))
        for directory in sorted({_dir(w.rel) for w in self.wrappers if w.language == "go"}):
            ranked += [(1, rel) for rel in self.go_files.get(directory, [])]
        ranked += [(0, rel) for rel in sorted(defining)]  # their own calls, now with the checkout's evidence
        ranked = sorted(set(ranked))
        best: dict[str, int] = {}
        for rank, rel in ranked:
            best.setdefault(rel, rank)
        ranked = sorted((rank, rel) for rel, rank in best.items())
        chosen = [rel for _, rel in ranked[:MAX_FILES]]
        self.rescan = set(chosen)
        self.skipped = len({rel for _, rel in ranked} - self.rescan)
        self.calls = {}

    def unique(self, language: str, name: str) -> bool:
        return self.names.get(language, {}).get(name.lower() if language == "php" else name, 0) == 1


LITERAL_ARGS = re.compile(r"""(?:\s*::\s*\w+\s*\()?[^()]{0,300}?['"]""")


def called_names(text: str, pattern: re.Pattern[str]) -> dict[str, bool]:
    """Names ``pattern`` finds called in a file, each with whether a call of it passes a string literal
    (``request($r, 'payment_intents')``): the evidence the rescan ranks caller files by."""
    out: dict[str, bool] = {}
    for m in pattern.finditer(text):
        name = m.group(1)
        if not out.get(name):
            out[name] = LITERAL_ARGS.match(text, m.end()) is not None
    return out


def registry_of(table) -> Registry | None:
    """The registry a scan context carries: the object itself, or its ``http`` attribute."""
    if table is None or isinstance(table, Registry):
        return table
    return getattr(table, "http", None)


def scan(text: str, rel: str, index, reads: frozenset[str], language: str, table=None) -> list:
    """Call sites of raw-HTTP Stripe wrappers in one file (same-file wrappers, plus the registry's)."""
    from . import httpgo, httpphp, httppy

    registry = registry_of(table)
    if registry is not None and registry.collecting:
        return []
    module = {"php": httpphp, "go": httpgo, "python": httppy}.get(_language(rel))
    if module is None:
        return []
    return module.sites(text, rel, index, reads, language, registry)


def build(text: str, rel: str, index, wrapper: Wrapper, line: int, call: str, pieces: list[str | None],
          method: str | None, fields: set[str], hidden: set[str], reads: frozenset[str], language: str):
    """The Usage for one wrapper call, or None when its path is not a Stripe endpoint."""
    from .scanner import Usage

    if not pieces or pieces[0] is None:
        return None
    url = wrapper.base + httpurl.template(pieces)
    spec = httpurl.spec_path(index, url)
    if spec is None:
        return None
    path, query = spec
    picked = httpurl.pick(index, path, method or wrapper.method, explicit=bool(method or wrapper.method))
    fields = set(fields)
    hidden = set(hidden)
    if query:
        got, visible = httpurl.query_fields(query)
        fields |= got
        if not visible:
            hidden.add("")
    fields |= set(wrapper.adds)
    path_arg = httpurl.template(pieces)
    for key, when in wrapper.maybe:
        if when is not None and None not in pieces:
            if when == path_arg:
                fields.add(key)
        else:
            hidden.add(key)  # may or may not be sent: never claimed
    glued = any(a is None and b is None for a, b in zip(pieces, pieces[1:]))  # ``"/{$id}{$query}"``: a query may hide
    if wrapper.rewrites or not wrapper.body_seen or glued:
        hidden.add("")
    shown = f"{call}('{httpurl.template(pieces)}')"
    return Usage(rel, line, picked, path, shown, frozenset(fields), not hidden, language, reads, frozenset(hidden))


def _language(rel: str) -> str | None:
    from .httplit import language_of

    return language_of(rel)


def _dir(rel: str) -> str:
    return rel.rsplit("/", 1)[0] if "/" in rel else ""
