"""What the whole checkout says about Python classes, for calls through ``self`` / ``cls`` (:mod:`pyself`) and
module-level SDK aliases (:mod:`pyalias`). Every answer fails closed: anything not seen or not modelled means
"not proven", exactly as if there were no checkout.

The checkout is walked once, by this module itself (dot directories, build output, oversize files and
symlinks included), and one word index is built over every ``.py`` file (:meth:`Repo.mentions`). Files the
scanner reads are parsed when a question names a word they contain; a file it skips (oversize, symlinked,
in a skipped directory), a file that does not parse, or a symlinked directory that names nothing we can
read, makes every question touching it unproven. Past :data:`MAX_FILES` files, :data:`MAX_BYTES` of source
or :data:`MAX_POSTINGS` words, nothing is proven.

Per parsed file (:class:`Facts`): each class (name, bases resolved through imports and aliases -- ``Base[int]``
is ``Base``; anything else is unresolved -- the names its body binds, its decorators, a metaclass); each call
of a name (``Cls(...)``, ``alias(...)``, ``mod.Cls(...)``) or of ``cls`` inside a class (any method whose first
parameter is ``cls``, or any ``@classmethod``), with which arguments are the stripe module; every other use
of a name as a value; names given to ``type(...)`` / ``new_class(...)``, named in an unresolved base, or
spelled in a string (``getattr(m, "Cls")``, ``"app.svc.Cls"``); and every attribute written (assignment and
annotated assignment targets, unpacking, ``del``, augmented assignment, ``setattr`` / ``__setattr__`` /
``delattr`` in any spelling; a computed name or a ``__dict__`` / ``vars()`` write counts as writing every name
the file spells as a string. A name computed from outside the source -- configuration, request data -- is the
one write this does not model).

Test files (:data:`scanner.TEST_DIRS`, :data:`scanner.TEST_FILE`) build services with stand-ins and patch
attributes; they make no real request, so their constructions, value uses and writes are not counted. Their
classes are.
"""

from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import pysyntax
from .pynodes import _all_strings, _builds_own_type, _dict_of, _in_all

MAX_FILES = 50_000
MAX_BYTES = 400_000_000  # source read in the one pass
MAX_FILE = 8_000_000  # a file past this is not read: it may name anything
MAX_POSTINGS = 40_000_000
MAX_FAMILY = 200
MAX_PARSED = 2_000  # files parsed to answer questions
WORD = re.compile(rb"[A-Za-z_][A-Za-z0-9_]*")
BUILTIN_BASES = {"object"}
SETATTRS = {"setattr", "delattr", "__setattr__", "__delattr__"}
DYNAMIC = {"type", "new_class", "make_dataclass", "namedtuple"}


@dataclass
class Klass:
    name: str
    bases: list[str | None]
    binds: set[str]
    decorators: list[str | None]
    metaclass: bool


@dataclass
class Facts:
    classes: list[Klass] = field(default_factory=list)
    calls: list[tuple[str, list[bool | None], dict[str | None, bool | None]]] = field(default_factory=list)
    cls_calls: list[tuple[str, list[bool | None], dict[str | None, bool | None]]] = field(default_factory=list)
    escapes: set[str] = field(default_factory=set)
    dynamic: set[str] = field(default_factory=set)  # named in type(...) / new_class(...) or an unresolved base
    strings: set[str] = field(default_factory=set)  # spelled as a string, or reached by getattr(...)(...)
    writes: dict[str, int] = field(default_factory=dict)
    any_write: bool = False  # setattr with a computed name, a __dict__ / vars() write


class Unknown(Exception):
    """Something the proof cannot see: the question is answered "not proven"."""


class Repo:
    def __init__(self, base: Path, readable: list[Path]) -> None:
        self.base = Path(base).resolve()
        self.readable = {str(p) for p in readable}  # the scanner walks the same resolved root: the same spelling
        self._index: dict[bytes, list[int]] | None = None
        self.paths: list[Path] = []
        self.skipped: list[bool] = []  # a file the scanner does not read: naming a class there leaves it unproven
        self.usable = True  # False: some Python source is out of sight (too much, a symlinked directory, unreadable)
        self._facts: dict[int, Facts | None] = {}
        self._full: set[int] = set()  # entries of _facts with the class facts too (facts(n, light=False))

    # --- the one pass ----------------------------------------------------------------------

    def _build(self) -> None:
        index: dict[bytes, list[int]] = {}
        read = postings = 0
        for dirpath, dirs, names in os.walk(self.base):
            here = Path(dirpath)
            for d in list(dirs):
                if (here / d).is_symlink() and "node_modules" not in here.parts and d != "node_modules":
                    self.usable = False  # its Python files are out of sight
            dirs[:] = [d for d in dirs if d != ".git" and not (here / d).is_symlink()]
            for name in names:
                if not name.endswith(".py"):
                    continue
                path = here / name
                n = len(self.paths)
                self.paths.append(path)
                if len(self.paths) > MAX_FILES:
                    self.usable = False
                    return
                try:
                    link = path.is_symlink()
                    if (link and not path.resolve().is_relative_to(self.base)) or path.stat().st_size > MAX_FILE:
                        self.usable = False  # its words cannot be read: it may name anything
                        return
                    raw = path.read_bytes()
                except OSError:
                    self.usable = False
                    return
                read += len(raw)
                self.skipped.append(link or str(path) not in self.readable)
                words = set(WORD.findall(raw))
                postings += len(words)
                if read > MAX_BYTES or postings > MAX_POSTINGS:
                    self.usable = False
                    return
                for w in words:
                    index.setdefault(w, []).append(n)
        self._index = index

    def mentions(self, name: str) -> list[int]:
        """Files whose text has ``name`` as a word; :class:`Unknown` when one of them cannot be read, or a file
        that could name it was not seen."""
        if self._index is None and self.usable:
            self._build()
        if not self.usable or self._index is None:
            raise Unknown(name)
        found = self._index.get(name.encode(), [])
        if any(self.skipped[n] for n in found):
            raise Unknown(name)  # named in a file the scanner skips (oversize, a build or dot directory, a symlink)
        return found

    def test_file(self, n: int) -> bool:
        from .scanner import TEST_DIRS, TEST_FILE  # the scanner builds on this module

        *dirs, name = self.paths[n].relative_to(self.base).parts
        return any(d in TEST_DIRS for d in dirs) or bool(TEST_FILE.match(name))

    def facts(self, n: int, *, light: bool = False) -> Facts:
        """The module's facts; ``light``: only ``writes``, ``any_write`` and ``strings`` are needed (the
        question most files are parsed for), so the class facts are not collected unless asked for later."""
        if n not in self._facts:
            if len(self._facts) >= MAX_PARSED:
                raise Unknown(str(self.paths[n]))
            self._facts[n] = self._collect(n, light)
            if not light:
                self._full.add(n)
        elif not light and n not in self._full and self._facts[n] is not None:
            self._facts[n] = self._collect(n, False)  # the same file read and parsed again: the same tree
            self._full.add(n)
        if self._facts[n] is None:
            raise Unknown(str(self.paths[n]))
        return self._facts[n]

    def _collect(self, n: int, light: bool) -> Facts | None:
        try:
            tree = pysyntax.parse(self.paths[n].read_text(errors="replace"))
            return collect(tree, light=light) if tree is not None else None
        except (OSError, RecursionError, MemoryError):
            return None

    # --- questions (each raises Unknown when it cannot be answered) -----------------------

    def classes(self, name: str) -> list[Klass]:
        """Every class of that name in the checkout."""
        return [k for n in self.mentions(name) for k in self.facts(n).classes if k.name == name]

    def family(self, name: str) -> set[str]:
        """``name`` and every class that subclasses it, by name, transitively."""
        out, todo = {name}, [name]
        while todo:
            current = todo.pop()
            for n in self.mentions(current):
                facts = self.facts(n)
                if current in facts.dynamic:
                    raise Unknown(current)  # type("Sub", (Base,), {...}), getattr(m, "Base"), an unresolved base
                for k in facts.classes:
                    if None in k.bases:
                        raise Unknown(k.name)  # a base we cannot name, in a file naming the class
                    if current in k.bases and k.name not in out:
                        out.add(k.name)
                        todo.append(k.name)
            if len(out) > MAX_FAMILY:
                raise Unknown(name)
        return out

    def ancestors(self, name: str, *, strict: bool = True, depth: int = 0) -> set[str]:
        """Every class ``name`` inherits from, by name. A base not defined in the checkout raises
        :class:`Unknown` (``strict``) or is listed without its own bases."""
        if depth > 32:
            raise Unknown(name)
        defs = self.classes(name)
        if not defs:
            if strict or depth == 0:
                raise Unknown(name)
            return set()
        out: set[str] = set()
        for k in defs:
            for b in k.bases:
                if b not in BUILTIN_BASES:
                    out |= {b} | self.ancestors(b, strict=strict, depth=depth + 1)
        return out

    def ahead(self, owner: str, names: set[str], *, all_of_mro: bool = False) -> None:
        """Every class that can come before ``owner`` in the MRO of ``owner`` or of a subclass anywhere (any
        ancestor of a subclass that is not ``owner`` or behind it: a superset of the real MRO prefix) is defined
        in the checkout, has no metaclass, no decorator but :data:`pyself.KEEP`, and binds none of ``names``.
        With ``all_of_mro`` the same holds for ``owner`` and every class behind it. Raises :class:`Unknown`."""
        from .pyself import KEEP  # pyself builds on this module

        family = self.family(owner)
        behind = self.ancestors(owner, strict=all_of_mro) | {owner}
        group = {owner} | (behind if all_of_mro else set())
        for sub in family - {owner}:
            group |= ({sub} | self.ancestors(sub, strict=False)) - behind
        for cls in group:
            defs = self.classes(cls)
            if not defs:
                raise Unknown(cls)  # ahead of the owner and not in the checkout: it may bind anything
            for k in defs:
                if k.metaclass or any(d not in KEEP for d in k.decorators):
                    raise Unknown(cls)
                if (cls != owner or all_of_mro) and k.binds & names:
                    raise Unknown(cls)

    def writes(self, attr: str) -> int:
        """Writes of ``.attr`` outside test files (a file that may write anything counts as unknown)."""
        total = 0
        for n in self.mentions(attr):
            if self.test_file(n):
                continue
            facts = self.facts(n, light=True)
            if facts.any_write and attr in facts.strings:
                raise Unknown(attr)  # setattr(obj, name, v) in a file that spells the name as a string
            total += facts.writes.get(attr, 0)
        return total

    def injected_stripe(self, owner: str, position: int, keyword: str) -> int:
        """How many constructions build ``owner`` or a subclass outside tests, each passing the stripe module as
        argument ``position`` (or keyword ``keyword``); :class:`Unknown` when one passes anything else, a class
        of the family is used as a value, or one is built out of sight."""
        family = self.family(owner)
        lineage = set(family)
        for sub in family:
            lineage |= self.ancestors(sub)  # an inherited factory's cls(...) builds the subclass it is called on
        seen = 0
        for name in lineage:
            for n in self.mentions(name):
                if self.test_file(n):
                    continue
                facts = self.facts(n)
                if family & (facts.escapes | facts.dynamic | facts.strings):
                    raise Unknown(name)
                built = [c for c in facts.calls if c[0] in family] + [c for c in facts.cls_calls if c[0] in lineage]
                for _, args, kwargs in built:
                    value = args[position] if position < len(args) else kwargs.get(keyword, False)
                    if value is not True or None in args[: position + 1] or None in kwargs:
                        raise Unknown(name)
                    seen += 1
        return seen


def collect(tree: ast.Module, *, light: bool = False) -> Facts:
    """The module's facts; ``light``: ``writes``, ``any_write`` and ``strings`` only (``classes``, ``calls``,
    ``cls_calls``, ``escapes`` and ``dynamic`` stay empty), which needs the bindings only for a call whose
    callee is itself a call or a subscript."""
    from .pyalias import Binders  # pyalias builds on pyparse, which reads this module's answers

    facts, parents = Facts(), {}
    built: list[Binders] = [] if light else [Binders(tree)]

    def binders() -> Binders:
        if not built:
            built.append(Binders(tree))
        return built[0]

    annotations: set[int] = set()
    in_all: set[int] = set()  # light: the string constants of an ``__all__`` assignment (what _in_all climbs to)
    resolve = lambda node: _resolve(node, binders())  # noqa: E731
    # One breadth-first walk: a node's parent, and whether it sits in an annotation, were recorded when its
    # ancestors came off the queue, which is before it does. The light collection reads parents only to
    # tell the strings of ``__all__``, so it notes those at the assignment instead of mapping every node.
    for node in ast.walk(tree):
        if not light:
            for child in ast.iter_child_nodes(node):
                parents[child] = node
        elif isinstance(node, (ast.Assign, ast.AugAssign)):
            in_all |= _all_strings(node)
        for ann in (getattr(node, "annotation", None), getattr(node, "returns", None)):
            if isinstance(ann, ast.AST):
                annotations |= {id(n) for n in ast.walk(ann)}
        if id(node) in annotations:
            continue
        if isinstance(node, ast.ClassDef):
            if light:
                continue
            bases = [_base(b, binders()) for b in node.bases]
            for b, name in zip(node.bases, bases):
                if name is None:
                    facts.dynamic |= _names(b, binders())
            decorators = [_dotted(d) for d in node.decorator_list]
            facts.classes.append(Klass(node.name, bases, set(binders().by_scope.get(node, {})), decorators, bool(node.keywords)))
        elif isinstance(node, ast.Call):
            _call(node, parents, binders, facts, light)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and len(node.value) < 300 and not (
                id(node) in in_all if light else _in_all(node, parents)):
            facts.strings.add(node.value.rpartition(".")[2].rpartition(":")[2])  # getattr(m, "Cls"), "app.svc.Cls"
        if not light and isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, ast.Load):
            name = resolve(node)
            if name is not None and not _allowed(node, parents):
                facts.escapes.add(name)
        if isinstance(node, ast.Attribute) and not isinstance(node.ctx, ast.Load):
            facts.writes[node.attr] = facts.writes.get(node.attr, 0) + 1
            if node.attr == "__dict__":
                facts.any_write = True
        if isinstance(node, ast.Subscript) and not isinstance(node.ctx, ast.Load) and _dict_of(node.value):
            facts.any_write = True  # obj.__dict__["k"] = v, vars(obj)["k"] = v
    return facts


def _call(node: ast.Call, parents, binders, facts: Facts, light: bool = False) -> None:
    """``binders()`` gives the module's :class:`pyalias.Binders`; ``light``: only what :meth:`Repo.writes` reads."""
    func = node.func
    callee = func.attr if isinstance(func, ast.Attribute) else (func.id if isinstance(func, ast.Name) else None)
    if callee in SETATTRS:
        # setattr(obj, "a", v), object.__setattr__(obj, "a", v): the name second; obj.__setattr__("a", v): first
        unbound = isinstance(func, ast.Name) or (isinstance(func.value, ast.Name) and func.value.id in ("object", "type"))
        args = node.args[1:] if unbound else node.args
        name = args[0] if args else None
        if isinstance(name, ast.Constant) and isinstance(name.value, str):
            facts.writes[name.value] = facts.writes.get(name.value, 0) + 1
        else:
            facts.any_write = True
        return
    if isinstance(func, ast.Attribute) and func.attr == "update" and _dict_of(func.value):
        facts.any_write = True  # obj.__dict__.update(...)
    if light:
        if isinstance(func, (ast.Call, ast.Subscript)):  # the one string fact below that a name cannot carry
            facts.strings |= _names(func, binders())
        return
    if callee in DYNAMIC:
        for arg in [*node.args, *(kw.value for kw in node.keywords)]:
            facts.dynamic |= _names(arg, binders())
    args = [None if isinstance(a, ast.Starred) else _is_stripe(a, binders()) for a in node.args]
    kwargs: dict[str | None, bool | None] = {kw.arg: _is_stripe(kw.value, binders()) for kw in node.keywords}
    if isinstance(func, ast.Name):
        owner = _cls_owner(func, parents, binders())
        if owner is not None:
            facts.cls_calls.append((owner, args, kwargs))
            return
    name = _resolve(func, binders())
    if name is not None:
        facts.calls.append((name, args, kwargs))
    elif isinstance(func, (ast.Call, ast.Subscript)):
        facts.strings |= _names(func, binders())  # getattr(m, "Cls")(...), m.__dict__["Cls"](...): built out of sight
    owner = _class_of(node, parents)
    if owner is not None and _builds_own_type(func):
        facts.escapes.add(owner)  # type(self)(...), self.__class__(...): built out of sight


def _allowed(node: ast.expr, parents) -> bool:
    """A use of a class name that builds nothing out of sight."""
    parent = parents.get(node)
    if isinstance(parent, ast.Call) and parent.func is node:
        return True  # a construction, read in _call
    if isinstance(parent, ast.Attribute) and parent.value is node:
        return parent.attr not in ("__init__", "__new__", "__call__", "__class__", "__subclasses__")
    if isinstance(parent, ast.ClassDef) and node in parent.bases:
        return True
    if isinstance(parent, ast.Call) and getattr(parent.func, "id", None) in ("isinstance", "issubclass", "super"):
        return True
    if isinstance(parent, ast.Tuple) and isinstance(parents.get(parent), ast.Call):
        call = parents.get(parent)
        return getattr(call.func, "id", None) in ("isinstance", "issubclass")
    if isinstance(parent, ast.ExceptHandler) or (isinstance(parent, ast.Tuple) and isinstance(parents.get(parent), ast.ExceptHandler)):
        return True
    return False


def _resolve(node: ast.expr, binders, depth: int = 0) -> str | None:
    """The class name a name or dotted name stands for (through ``import ... as``, ``from x import C as D`` and
    ``D = C``); the last part of a dotted name; None for a name bound otherwise (a parameter, a loop target)."""
    if isinstance(node, ast.Attribute):
        return node.attr
    if not isinstance(node, ast.Name) or depth > 4:
        return None
    found = binders.binding(node)
    if found is None:
        return None if node.id in binders.declared else node.id  # unbound here: a builtin, a star import
    kind, bound = found
    if kind == "class":
        return node.id
    if kind == "import":
        for a in bound.names:
            if (a.asname or a.name).split(".")[0] == node.id:
                return a.name.rpartition(".")[2] if isinstance(bound, ast.ImportFrom) else None
        return None
    if kind == "assign" and isinstance(bound.value, (ast.Name, ast.Attribute)):
        return _resolve(bound.value, binders, depth + 1)
    return None


def _base(node: ast.expr, binders) -> str | None:
    if isinstance(node, ast.Subscript):  # Base[int], Generic[T]
        node = node.value
    if isinstance(node, (ast.Name, ast.Attribute)):
        return _resolve(node, binders)
    return None


def _names(node: ast.AST, binders) -> set[str]:
    out: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, (ast.Name, ast.Attribute)):
            r = _resolve(n, binders)
            out |= {r} if r else set()
            out |= {n.id} if isinstance(n, ast.Name) else {n.attr}
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            out.add(n.value.rpartition(".")[2])
    return out


def _dotted(node: ast.expr) -> str | None:
    if isinstance(node, ast.Call):
        node = node.func
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    return ".".join([node.id, *reversed(parts)]) if isinstance(node, ast.Name) else None


def _is_stripe(value: ast.expr, binders) -> bool:
    """A name bound only to ``import stripe`` (or ``import stripe as s``)."""
    if not isinstance(value, ast.Name):
        return False
    found = binders.binding(value)
    if found is None or found[0] != "import" or not isinstance(found[1], ast.Import):
        return False
    return any(a.name == "stripe" and (a.asname or a.name) == value.id for a in found[1].names)


def _cls_owner(func: ast.Name, parents, binders) -> str | None:
    """The class whose ``cls`` this is: the first parameter of a function defined in a class body that is a
    ``@classmethod`` (with any other decorators) or names its first parameter ``cls``."""
    found = binders.binding(func)
    if found is None or found[0] != "param":
        return None
    fn = parents.get(parents.get(found[1]))
    klass = parents.get(fn)
    if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) or not isinstance(klass, ast.ClassDef):
        return None
    first = [*fn.args.posonlyargs, *fn.args.args][:1]
    if not first or first[0] is not found[1]:
        return None
    classmethod_ = any(_dotted(d) in ("classmethod", "builtins.classmethod") for d in fn.decorator_list)
    return klass.name if classmethod_ or func.id == "cls" else None


def _class_of(node: ast.AST, parents) -> str | None:
    current = parents.get(node)
    while current is not None and not isinstance(current, ast.ClassDef):
        current = parents.get(current)
    return current.name if current is not None else None
