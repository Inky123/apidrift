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
from dataclasses import dataclass, field

from . import httpurl

MAX_WRAPPERS = 200  # wrappers kept per repository
MAX_FILES = 400  # caller files rescanned


@dataclass(frozen=True)
class Wrapper:
    language: str  # php / go / python
    rel: str
    name: str
    owner: str  # PHP / Python class, Go package directory; "" for a plain PHP / Python function
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


@dataclass
class Registry:
    """The wrappers of a whole checkout and the files that call them."""

    collecting: bool = True
    wrappers: list[Wrapper] = field(default_factory=list)
    names: dict[str, dict[str, int]] = field(default_factory=dict)  # language -> function name -> definitions
    go_names: dict[tuple[str, str], int] = field(default_factory=dict)  # (dir, name) -> definitions
    calls: dict[str, set[str]] = field(default_factory=dict)  # rel -> names called in it
    rescan: set[str] = field(default_factory=set)

    def collect(self, text: str, rel: str) -> None:
        """Note the wrappers and function names one file defines and the names it calls."""
        from . import httpgo, httpphp, httppy

        module = {"php": httpphp, "go": httpgo, "python": httppy}.get(_language(rel))
        if module is None:
            return
        found, defined, called = module.survey(text, rel)
        lang = _language(rel)
        if lang == "go":
            for name in defined:
                key = (_dir(rel), name)
                self.go_names[key] = self.go_names.get(key, 0) + 1
        else:
            names = self.names.setdefault(lang, {})
            for name in defined:
                names[name.lower()] = names.get(name.lower(), 0) + 1
        if len(self.wrappers) < MAX_WRAPPERS:
            self.wrappers += found[: MAX_WRAPPERS - len(self.wrappers)]
        if called:
            self.calls[rel] = called

    def finish(self) -> None:
        self.collecting = False
        # a PHP method is called through its class name elsewhere; anything else through its own name
        keys = {(w.owner if w.owner and w.language == "php" else w.name).lower() for w in self.wrappers}
        for rel, called in sorted(self.calls.items()):
            if len(self.rescan) >= MAX_FILES:
                break
            if {c.lower() for c in called} & keys and any(w.rel != rel for w in self.wrappers):
                self.rescan.add(rel)
        self.calls = {}

    def unique(self, language: str, name: str) -> bool:
        return self.names.get(language, {}).get(name.lower(), 0) == 1

    def unique_in(self, directory: str, name: str) -> bool:
        return self.go_names.get((directory, name), 0) == 1


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
    if wrapper.rewrites or not wrapper.body_seen:
        hidden.add("")
    shown = f"{call}('{httpurl.template(pieces)}')"
    return Usage(rel, line, picked, path, shown, frozenset(fields), not hidden, language, reads, frozenset(hidden))


def _language(rel: str) -> str | None:
    from .httplit import language_of

    return language_of(rel)


def _dir(rel: str) -> str:
    return rel.rsplit("/", 1)[0] if "/" in rel else ""
