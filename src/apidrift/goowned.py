"""Whether a Go field name left in a fixed file is provably the app's own field, not a stripe-go one.

The Go oracle refuses a file while any use of a removed stripe-go field name remains (:func:`gofix.oracle_tokens`).
A use is accepted here only when the compiler would bind it to a field of a struct the app declares:
``req.PaymentMethodTypes`` where ``req`` is a parameter of the enclosing function whose declared type is a
named struct of this repository (``*dto.CreateSetupIntentRequest``), each link of a longer chain
(``input.Options.PaymentMethodTypes``) is a field that struct declares by name with such a type, and the last
struct declares the field itself. A field declared at depth zero hides any promoted field of the same name,
so an embedded stripe-go struct cannot be what the name means. The struct declarations are read from the
package directory the import path names under the nearest ``go.mod``. Anything else (a stripe-go type, a
local variable, a call or index in the chain, an alias or a generic type, a package outside this module)
stays a leftover.
"""

from __future__ import annotations

import re
from pathlib import Path

from .golex import ID, STR, GoSource
from .govars import Vars

STRIPE_GO = re.compile(r"github\.com/stripe/stripe-go(?:/|$)")
MAX_FILES, MAX_BYTES = 400, 2_000_000  # per package directory read
Struct = tuple[GoSource, int, Path]  # the declaring file, the ``{`` of the struct body, that file's directory


class Repo:
    """The repository around one fixed Go file: its directory and the module that holds it."""

    def __init__(self, base: Path | None, file: str, src: GoSource) -> None:
        self.base = base.resolve() if base is not None else None
        path = (self.base / file) if self.base is not None else Path(file)
        self.dir, self.path, self.src = path.parent, path, src
        self._pkgs: dict[Path, tuple[str | None, dict[str, list[Struct]]]] = {}
        self._module: tuple[Path, str] | None | bool = False
        self._vars: Vars | None = None

    def owned(self, i: int) -> bool:
        """Token ``i`` of the file is ``x.F`` / ``x.A.F`` with ``x`` a parameter typed as an app struct
        whose chain ends in a struct that declares ``F`` itself."""
        src = self.src
        chain = _chain(src, i)
        if chain is None:
            return False
        root, links = chain
        if self._vars is None:
            self._vars = Vars(src, set())
        typed = _param_type(src, self._vars, root)
        if typed is None:
            return False
        struct = self._struct(src, self.dir, typed)
        for link in links:
            if struct is None:
                return False
            fields = _fields(*struct[:2])
            if fields is None or link not in fields:
                return False
            struct = self._struct(struct[0], struct[2], fields[link])
        if struct is None:
            return False
        fields = _fields(*struct[:2])
        return fields is not None and src.tok[i] in fields

    # --- resolving a type name to its struct declaration -------------------------------

    def _struct(self, src: GoSource, here: Path, typ: tuple[str, str] | None) -> Struct | None:
        if typ is None:
            return None
        alias, name = typ
        if not alias:
            pkg_dir = here
        else:
            path = _imports(src).get(alias)
            if path is None or STRIPE_GO.match(path):
                return None
            pkg_dir = self._package_dir(path)
            if pkg_dir is None:
                return None
        package, types = self._package(pkg_dir)
        if alias and alias not in _imports(src, explicit=True) and package != alias:
            return None  # an import without an alias names the package by its clause, not its path
        found = types.get(name, [])
        return found[0] if len(found) == 1 else None

    def _package(self, pkg_dir: Path) -> tuple[str | None, dict[str, list[Struct]]]:
        if pkg_dir in self._pkgs:
            return self._pkgs[pkg_dir]
        names: set[str] = set()
        types: dict[str, list[Struct]] = {}
        files = [(self.src, self.dir)] if pkg_dir == self.dir else []
        if self.base is not None and pkg_dir.is_dir():
            others = sorted(p for p in pkg_dir.glob("*.go") if not p.name.endswith("_test.go") and p != self.path)
            for p in others[:MAX_FILES]:
                try:
                    raw = p.read_bytes()
                    if len(raw) <= MAX_BYTES:
                        files.append((GoSource(raw.decode("utf-8")), pkg_dir))
                except (OSError, UnicodeDecodeError):
                    continue
        for src, d in files:
            names.add(_package_name(src) or "")
            for name, brace in _struct_decls(src):
                types.setdefault(name, []).append((src, brace, d))
        package = names.pop() if len(names) == 1 else None
        self._pkgs[pkg_dir] = (package, types)
        return self._pkgs[pkg_dir]

    def _package_dir(self, path: str) -> Path | None:
        module = self._find_module()
        if module is None:
            return None
        root, name = module
        if path == name:
            return root
        if not path.startswith(name + "/"):
            return None  # another module: not this repository's code
        target = (root / path[len(name) + 1 :]).resolve()
        return target if self.base is not None and target.is_relative_to(self.base) else None

    def _find_module(self) -> tuple[Path, str] | None:
        if self._module is False:
            self._module = None
            if self.base is not None:
                for d in [self.dir, *self.dir.parents]:
                    if not d.is_relative_to(self.base):
                        break
                    mod = d / "go.mod"
                    if mod.is_file():
                        m = re.search(r"(?m)^module\s+(\S+)", mod.read_text("utf-8", "replace"))
                        self._module = (d, m.group(1).strip('"')) if m else None
                        break
        return self._module  # type: ignore[return-value]


def _chain(src: GoSource, i: int) -> tuple[int, list[str]] | None:
    """``root.a.b.F`` ending at token ``i``: the root token and the names between it and ``F``."""
    if i < 2 or src.tok[i - 1] != "." or src.kind[i - 2] != ID:
        return None
    j, links = i - 2, []
    while j >= 2 and src.tok[j - 1] == "." and src.kind[j - 2] == ID:
        links.append(src.tok[j])
        j -= 2
    if j >= 1 and src.tok[j - 1] in (".", ")", "]", "}"):
        return None  # a call, index or literal in the chain
    return j, links[::-1]


def _param_type(src: GoSource, scope: Vars, root: int) -> tuple[str, str] | None:
    """``(package alias, type name)`` of the parameter or receiver ``root`` refers to; None otherwise."""
    name = src.tok[root]
    decl = scope.decl_for(name, root)
    if decl is None or decl.tok not in src.funcs or decl.tok != decl.lo:
        return None  # not a parameter of the enclosing function (a local, a package-level name)
    func, body = src.funcs[decl.tok], decl.tok
    for a, b in _signature_groups(src, func, body):
        pending: list[str] = []
        for x, y in src.split(a + 1, b):
            toks = list(range(x, y))
            if len(toks) == 1 and src.kind[toks[0]] == ID:
                pending.append(src.tok[toks[0]])
                continue
            if len(toks) >= 2 and src.kind[toks[0]] == ID and src.tok[toks[1]] != ".":
                if name in (*pending, src.tok[toks[0]]):
                    return _named_type(src, toks[1:])
            pending = []
    return None


def _signature_groups(src: GoSource, func: int, body: int) -> list[tuple[int, int]]:
    """The receiver and parameter lists of the function whose ``func`` token and body ``{`` are given."""
    j, out = func + 1, []
    if j < body and src.tok[j] == "(" and src.match[j] > 0:
        first, j = (j, src.match[j]), src.match[j] + 1
        if j < body and src.kind[j] == ID:  # a method: the first list was the receiver
            out.append(first)
            j += 1
        else:
            return [first]  # a function literal
    elif j < body and src.kind[j] == ID:
        j += 1
    else:
        return []
    if j < body and src.tok[j] == "[":
        return []  # type parameters: the parameter types may name them
    if j < body and src.tok[j] == "(" and src.match[j] > 0:
        out.append((j, src.match[j]))
    return out


def _named_type(src: GoSource, toks: list[int]) -> tuple[str, str] | None:
    """``T``, ``*T``, ``pkg.T`` or ``*pkg.T`` (nothing else) as ``(alias, name)``."""
    words = [src.tok[t] for t in toks]
    if words and words[0] == "*":
        words = words[1:]
    if len(words) == 1 and src.kind[toks[-1]] == ID:
        return "", words[0]
    if len(words) == 3 and words[1] == "." and src.kind[toks[-1]] == ID and src.kind[toks[-3]] == ID:
        return words[0], words[2]
    return None


def _fields(src: GoSource, brace: int) -> dict[str, tuple[str, str] | None] | None:
    """Names a struct body declares itself, each with its named type (None for any other type)."""
    end = src.match[brace]
    if end < 0:
        return None
    decls, cur = [], []
    for j in range(brace + 1, end):
        if src.kind[j] == ";" and src.parent[j] == brace:
            decls.append(cur)
            cur = []
        else:
            cur.append(j)
    decls.append(cur)
    out: dict[str, tuple[str, str] | None] = {}
    for toks in decls:
        if toks and src.kind[toks[-1]] == STR and src.parent[toks[-1]] == brace:
            toks = toks[:-1]  # the tag
        if len(toks) < 2 or src.kind[toks[0]] != ID or src.tok[toks[1]] == ".":
            continue  # empty, or an embedded field (``Base``, ``pkg.Base``; ``*T`` starts with an operator)
        names, k = [src.tok[toks[0]]], 1
        while k + 1 < len(toks) and src.tok[toks[k]] == "," and src.kind[toks[k + 1]] == ID:
            names.append(src.tok[toks[k + 1]])
            k += 2
        for n in names:
            out[n] = _named_type(src, toks[k:])
    return out


def _struct_decls(src: GoSource) -> list[tuple[str, int]]:
    """Top-level ``type Name struct {`` declarations (also inside a ``type ( ... )`` group)."""
    out = []
    for t in src.ids("type"):
        if src.parent[t] != -1 or t + 1 >= src.n:
            continue
        if src.tok[t + 1] == "(" and src.match[t + 1] > 0:
            group, starts = t + 1, [t + 2]
            starts += [j + 1 for j in range(t + 2, src.match[group]) if src.parent[j] == group and src.kind[j] == ";"]
        else:
            starts = [t + 1]
        for s in starts:
            if s + 2 < src.n and src.kind[s] == ID and src.tok[s + 1] == "struct" and src.tok[s + 2] == "{":
                out.append((src.tok[s], s + 2))
    return out


def _package_name(src: GoSource) -> str | None:
    for t in src.ids("package"):
        if src.parent[t] == -1 and t + 1 < src.n and src.kind[t + 1] == ID:
            return src.tok[t + 1]
    return None


def _imports(src: GoSource, *, explicit: bool = False) -> dict[str, str]:
    """Local name -> import path; without ``explicit``, unaliased imports go by their last path element."""
    out: dict[str, str] = {}
    for i in src.ids("import"):
        if src.parent[i] != -1 or i + 1 >= src.n:
            continue
        lo, hi = (i + 2, src.match[i + 1]) if src.tok[i + 1] == "(" and src.match[i + 1] >= 0 else (i + 1, i + 3)
        for j in range(lo, min(hi, src.n)):
            if src.kind[j] != STR:
                continue
            path = src.tok[j][1:-1]
            alias = src.tok[j - 1] if j > lo and src.kind[j - 1] == ID else ""
            if alias:
                out[alias] = path
            elif not explicit:
                parts = path.split("/")
                last = parts[-2] if len(parts) > 1 and re.fullmatch(r"v\d+", parts[-1]) else parts[-1]
                out.setdefault(last, path)
    return out
