"""What the whole checkout says about a Python class, for calls made through ``self`` / ``cls``.

``self.stripe_class.create(...)`` and ``self.api.Customer.create(...)`` (``self.api = api`` in ``__init__``)
name an SDK object only if no subclass anywhere rebinds the attribute, and, for an injected one, every
construction of the class passes the ``stripe`` module. One file cannot show that, so :func:`scanner.scan_repo`
hands the reader a :class:`Repo` over every Python file of the checkout. It reads a file only when its text
names a class in question (by word), parses it once, and remembers:

* each class: its name, the last names of its bases, the names its body binds, whether it is decorated;
* each call of such a class by name (``StripeBillingService(...)``, ``mod.Cls(...)``) or of ``cls(...)`` inside
  one of its classmethods: whether each argument is the stripe module (a name bound to ``import stripe``);
* any other use of the class as a value (passed, returned, assigned, ``type(self)(...)``, ``self.__class__``),
  which could construct it out of sight.

Classes are matched by name, so two classes of the same name count as one (more is only stricter). Test
files (:data:`scanner.TEST_DIRS`, :data:`scanner.TEST_FILE`) build services with stand-ins and make no real
request: their constructions and uses are not counted, but their subclasses are. A class reached by a dotted
string and constructed directly would not be seen; ``config.maybe_dotted(...).create_service`` style access
goes through a classmethod, whose ``cls(...)`` is. Past :data:`MAX_FILES` Python files nothing is proven.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import pysyntax

MAX_FILES = 20_000
MAX_BYTES = 1_500_000
MAX_FAMILY = 200
MAX_KEPT = 64_000_000  # characters of source kept in memory between questions


@dataclass
class Klass:
    name: str
    bases: list[str]
    binds: set[str]


@dataclass
class Facts:
    classes: list[Klass] = field(default_factory=list)
    calls: list[tuple[str, list[bool | None], dict[str, bool | None]]] = field(default_factory=list)  # name, args, kwargs
    escapes: set[str] = field(default_factory=set)  # class names used as values


class Repo:
    def __init__(self, base: Path, files: list[Path]) -> None:
        self.base = base
        self.files = [p for p in files if p.suffix == ".py"]
        self.usable = len(self.files) <= MAX_FILES
        self._texts: dict[Path, str | None] = {}
        self._facts: dict[Path, Facts | None] = {}
        self._mentions: dict[str, list[Path]] = {}
        self._stores: dict[str, int] = {}
        self._kept = 0

    # --- reading ---------------------------------------------------------------------------

    def _text(self, path: Path) -> str | None:
        if path in self._texts:
            return self._texts[path]
        try:
            text = path.read_text(errors="replace") if path.stat().st_size <= MAX_BYTES else None
        except OSError:
            text = None
        if self._kept + len(text or "") <= MAX_KEPT:  # keep texts while they are few; read again past that
            self._texts[path] = text
            self._kept += len(text or "")
        return text

    def test_file(self, path: Path) -> bool:
        from .scanner import TEST_DIRS, TEST_FILE  # the scanner builds on this module

        *dirs, name = path.relative_to(self.base).parts
        return any(d in TEST_DIRS for d in dirs) or bool(TEST_FILE.match(name))

    def mentions(self, name: str) -> list[Path]:
        if name not in self._mentions:
            word = re.compile(rf"\b{re.escape(name)}\b")
            self._mentions[name] = [p for p in self.files if (t := self._text(p)) is not None and word.search(t)]
        return self._mentions[name]

    def facts(self, path: Path) -> Facts | None:
        if path not in self._facts:
            text = self._text(path)
            tree = pysyntax.parse(text) if text is not None else None
            self._facts[path] = collect(tree) if tree is not None else None
        return self._facts[path]

    # --- questions -------------------------------------------------------------------------

    def family(self, name: str) -> set[str] | None:
        """``name`` and every class anywhere that subclasses it (by base name), or None when unknown."""
        if not self.usable:
            return None
        out, todo = {name}, [name]
        while todo:
            if len(out) > MAX_FAMILY:
                return None
            current = todo.pop()
            for path in self.mentions(current):
                facts = self.facts(path)
                if facts is None:
                    return None  # a file naming the class that cannot be read
                for k in facts.classes:
                    if current in k.bases and k.name not in out:
                        out.add(k.name)
                        todo.append(k.name)
        return out

    def binds(self, names: set[str], attr: str) -> bool:
        """Some class of these names, anywhere, binds ``attr`` in its body."""
        for name in names:
            for path in self.mentions(name):
                facts = self.facts(path)
                if facts is None or any(k.name == name and attr in k.binds for k in facts.classes):
                    return True
        return False

    def stores(self, attr: str) -> int:
        """Writes of ``.attr`` (``x.attr = v``, ``x.attr += v``, ``setattr(x, "attr", v)``) outside test files."""
        if not self.usable:
            return 1 << 30
        if attr not in self._stores:
            a = re.escape(attr)
            pattern = re.compile(rf"\.\s*{a}\s*(?:[-+*/|&@%^]|//|<<|>>|\*\*)?=(?!=)|setattr\([^,]+,\s*['\"]{a}['\"]|\bdel\b[^\n]*\.\s*{a}\b")
            self._stores[attr] = sum(
                len(pattern.findall(t)) for p in self.files if not self.test_file(p) and (t := self._text(p)) is not None
            )
        return self._stores[attr]

    def injected_stripe(self, names: set[str], position: int, keyword: str) -> bool:
        """Every construction of these classes outside tests passes the stripe module as argument ``position``
        (or keyword ``keyword``), there is at least one, and no class of them is used as a value there."""
        seen = 0
        for name in names:
            for path in self.mentions(name):
                facts = self.facts(path)
                if facts is None:
                    return False
                if self.test_file(path):
                    continue
                if name in facts.escapes:
                    return False
                for callee, args, kwargs in facts.calls:
                    if callee != name:
                        continue
                    value = args[position] if position < len(args) else kwargs.get(keyword, False)
                    if value is not True or (None in args[: position + 1]) or None in kwargs:
                        return False  # another value, a *args / **kwargs spread, or not passed
                    seen += 1
        return seen > 0


def collect(tree: ast.Module) -> Facts:
    from .pyalias import Binders  # pyalias builds on pyparse, which reads this module's answers

    binders = Binders(tree)
    facts = Facts()
    parents: dict[ast.AST, ast.AST] = {}
    annotations: set[int] = set()  # nodes inside a type annotation: naming a class there builds nothing
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
        for ann in (getattr(node, "annotation", None), getattr(node, "returns", None)):
            if isinstance(ann, ast.AST):
                annotations |= {id(n) for n in ast.walk(ann)}
    for node in ast.walk(tree):
        if id(node) in annotations:
            continue
        if isinstance(node, ast.ClassDef):
            bases = [b.attr if isinstance(b, ast.Attribute) else getattr(b, "id", "") for b in node.bases]
            facts.classes.append(Klass(node.name, bases, set(binders.by_scope.get(node, {}))))
        elif isinstance(node, ast.Call):
            name = _callee(node.func, parents, binders)
            if name is not None:
                args = [None if isinstance(a, ast.Starred) else _is_stripe(a, binders) for a in node.args]
                kwargs = {kw.arg: _is_stripe(kw.value, binders) for kw in node.keywords if kw.arg}
                if any(kw.arg is None for kw in node.keywords):
                    kwargs[None] = None
                facts.calls.append((name, args, kwargs))
        elif isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, ast.Load):
            name = node.id if isinstance(node, ast.Name) else node.attr
            parent = parents.get(node)
            if isinstance(parent, ast.Call) and parent.func is node:
                continue  # a construction (or ``cls(...)``), read above
            if isinstance(parent, ast.Attribute) and parent.value is node:
                if parent.attr in ("__init__", "__new__", "__call__"):
                    facts.escapes.add(name)
                continue  # Cls.create_service
            if isinstance(parent, ast.ClassDef) and node in parent.bases:
                continue
            if isinstance(parent, ast.Call) and getattr(parent.func, "id", None) in ("isinstance", "issubclass"):
                continue
            if name == "cls" and isinstance(node, ast.Name):
                owner = _classmethod_owner(node, parents)
                if owner is not None:
                    facts.escapes.add(owner)
                continue
            facts.escapes.add(name)
        if isinstance(node, ast.Attribute) and node.attr == "__class__" or (
            isinstance(node, ast.Call) and isinstance(node.func, ast.Call) and getattr(node.func.func, "id", None) == "type"
        ):
            owner = _class_of(node, parents)
            if owner is not None:
                facts.escapes.add(owner)  # type(self)(...) / self.__class__(...): built out of sight
    return facts


def _callee(func: ast.expr, parents, binders) -> str | None:
    if isinstance(func, ast.Name):
        return _classmethod_owner(func, parents) if func.id == "cls" else func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_stripe(value: ast.expr, binders) -> bool:
    """A name bound only to ``import stripe`` (or ``import stripe as s``)."""
    if not isinstance(value, ast.Name):
        return False
    found = binders.binding(value)
    if found is None or found[0] != "import" or not isinstance(found[1], ast.Import):
        return False
    return any(a.name == "stripe" and (a.asname or a.name) == value.id for a in found[1].names)


def _classmethod_owner(node: ast.AST, parents) -> str | None:
    """The class whose classmethod ``cls`` is (the first parameter of an ``@classmethod`` in a class body)."""
    current = parents.get(node)
    while current is not None and not isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
        current = parents.get(current)
    if current is None:
        return None
    klass = parents.get(current)
    decorated = [getattr(d, "id", None) for d in current.decorator_list] == ["classmethod"]
    first = current.args.args[0].arg if current.args.args else None
    if isinstance(klass, ast.ClassDef) and decorated and first == "cls":
        return klass.name
    return None


def _class_of(node: ast.AST, parents) -> str | None:
    current = parents.get(node)
    while current is not None and not isinstance(current, ast.ClassDef):
        current = parents.get(current)
    return current.name if current is not None else None
