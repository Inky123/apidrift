"""Python wrappers followed across files (:mod:`crosstable` holds the passes; this is the Python half).

A candidate is a module-level function, or a method written directly in a module-level class, with no
decorator, whose body hands one parameter to one SDK call as that call's request, spread as ``**NAME``
(the call's other keywords are request options; positional arguments are the path's ids only)::

    async def create_payment_intent(self, **params):        # the wrapper's **kwargs: the caller's keywords
        if params.get("setup_future_usage") == "off_session":
            params["excluded_payment_method_types"] = [...]   # a key the wrapper adds: sent through every caller
        return await stripe.PaymentIntent.create_async(**params)

Inside the wrapper ``NAME`` may only be read by a literal key (``NAME.get('k')``, ``NAME['k']``,
``'k' in NAME``) or written by one (``NAME['k'] = v``, before the call: those keys join every caller's
fields); anything else (passed on, rebound, updated, used in a nested function) and it is not a candidate.

It is followed when its name is defined once (``def NAME(``) in the Python files the scan reads, tests left
out, and is not too common (:func:`crosstable.single`). A use is a *visible caller* only when its receiver
provably reaches it:

- a function: ``NAME(`` in its own module, or ``L(`` where the caller did ``from M import NAME [as L]``
  and ``M`` resolves to the wrapper's file (a relative import, or the one file under a top-level
  directory that is not itself a package);
- a method of ``C``: ``I.NAME(`` where ``I`` is a module-level instance of the wrapper's module bound
  once to ``C(...)``, used there or imported (``from M import I [as L]``); or ``self.NAME(`` in a method
  written directly in ``C`` when nothing in the checkout subclasses ``C`` and the code is not a
  distributable package (whose subclasses live elsewhere);

where the local name is bound nowhere else in the caller's module (no parameter, local, other import, star
import), neither the name nor the instance is set as an attribute anywhere (``x.NAME = ``, ``setattr``),
and the caller's request is visible: its keywords (minus the wrapper's other parameters and the request
options), and every ``**D`` a dict literal or a local every reaching binding of which is one, written into
only by literal keys (read as the direct reader reads a ``**params`` local). Pass 3 adds each visible
caller as a call site; the wrapper's own site stays (:mod:`crosstable`). The fixer edits a caller at its
own keywords and dict (:func:`fixcross.py_sites`), never the wrapper, and refuses a key the wrapper reads.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

from . import crosstable, pyparse, pysyntax, scanner
from .crosstable import Table, Wrapper
from .pycallers import Module, bindings, uses

LANG = "python"
SUFFIXES = {".py"}
SHAPES = "python-shapes"
NAMESPACE = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
DYNAMIC = {"locals", "vars", "eval", "exec", "globals"}


@dataclass(frozen=True)
class Shape:
    """How a caller's arguments become the wrapper's request."""

    kwargs: bool  # the wrapper spreads its ``**kwargs``: the caller's keywords are the request
    params: tuple[str, ...]  # the wrapper's parameters after ``self`` (``*``/``**`` ones excluded)
    forwarded: str  # the dict parameter spread into the SDK call (kwargs=False)
    added: frozenset[str]  # keys the wrapper writes into the request before the call
    touched: frozenset[str]  # keys the wrapper reads or writes (a fix must not rename them at a caller)


# --- pass 1: candidates ------------------------------------------------------------------


def collect(calls: list[pyparse.PyCall], table: Table, rel: str) -> None:
    for c in calls:
        d = c.fn
        if not isinstance(d, (ast.FunctionDef, ast.AsyncFunctionDef)) or d.decorator_list:
            continue
        holder = c.scopes.parents.get(d)
        owner = ""
        if isinstance(holder, ast.ClassDef) and isinstance(c.scopes.parents.get(holder), ast.Module):
            owner = holder.name
        elif not isinstance(holder, ast.Module):
            continue
        shape = _shape(c, d, bool(owner))
        if shape is not None:
            index = -1 if shape.kwargs else shape.params.index(shape.forwarded)
            w = Wrapper(LANG, d.name, rel, c.node.lineno, c.call, c.resolved, index, bool(owner), owner, rel)
            table.add(w)
            table.facts.setdefault(SHAPES, {})[(rel, c.node.lineno, d.name)] = shape


def _shape(c: pyparse.PyCall, d, method: bool) -> Shape | None:
    spreads = [kw for kw in c.node.keywords if kw.arg is None]
    if len(spreads) != 1 or not isinstance(spreads[0].value, ast.Name):
        return None
    if any(kw.arg is not None and kw.arg not in pyparse.OPTION_KWARGS for kw in c.node.keywords):
        return None  # a field the wrapper sends itself: not followed
    if any(isinstance(a, (ast.Starred, ast.Dict)) for a in c.node.args) or (c.node.args and "{" not in c.resolved.path):
        return None
    name = spreads[0].value.id
    a = d.args
    positional = [x.arg for x in [*a.posonlyargs, *a.args]]
    if method:
        if not positional:
            return None
        positional = positional[1:]  # self
    params = tuple(positional + [x.arg for x in a.kwonlyargs])
    kwargs = a.kwarg is not None and a.kwarg.arg == name
    if not kwargs and name not in params:
        return None
    keys = _uses(d, name, spreads[0].value, c.node.lineno)
    if keys is None:
        return None
    added, touched = keys
    return Shape(kwargs, params, "" if kwargs else name, frozenset(added), frozenset(touched))


def _uses(d, name: str, forward: ast.Name, line: int) -> tuple[set[str], set[str]] | None:
    """Keys the wrapper writes into ``name`` before ``line``, and every key it reads or writes; None when
    ``name`` is used some other way."""
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(d):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    added: set[str] = set()
    touched: set[str] = set()
    for node in ast.walk(d):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in DYNAMIC:
            return None
        if isinstance(node, (ast.Global, ast.Nonlocal)) and name in node.names:
            return None
        if not isinstance(node, ast.Name) or node.id != name or node is forward:
            continue
        up, nested = parents.get(node), False
        while up is not None and up is not d:
            nested = nested or isinstance(up, NAMESPACE)
            up = parents.get(up)
        parent = parents.get(node)
        if nested or not isinstance(node.ctx, ast.Load):
            return None  # captured by a nested function, or rebound
        if isinstance(parent, ast.Subscript) and parent.value is node and pyparse._constant_str(parent.slice):
            touched.add(pyparse._constant_str(parent.slice))
            if isinstance(parent.ctx, ast.Load):
                continue
            target = parents.get(parent)
            if isinstance(parent.ctx, ast.Store) and isinstance(target, ast.Assign) and target.targets == [parent]:
                if target.lineno < line:
                    added.add(pyparse._constant_str(parent.slice))
                continue
            return None  # deleted, or written in place
        if isinstance(parent, ast.Attribute) and parent.attr == "get":
            call = parents.get(parent)
            if isinstance(call, ast.Call) and call.func is parent and call.args and pyparse._constant_str(call.args[0]):
                touched.add(pyparse._constant_str(call.args[0]))
                continue
        if isinstance(parent, ast.Compare) and node in parent.comparators and len(parent.ops) == 1 and isinstance(
                parent.ops[0], (ast.In, ast.NotIn)) and pyparse._constant_str(parent.left):
            touched.add(pyparse._constant_str(parent.left))
            continue
        return None
    return added, touched


# --- pass 2: what is followed, and who calls it --------------------------------------------


@dataclass
class Facts:
    files: set[str]  # repo-relative paths of every Python file walked (modules resolve against them)
    subclassed: set[str]  # names in some class's bases
    written: set[str]  # attribute names set somewhere (``x.NAME = ``, ``setattr(x, 'NAME'``)
    instances: dict[tuple[str, str], str]  # (wrapper file, owner) -> the module-level instance's name
    library: set[str]  # wrapper files in a distributable package


BASES = re.compile(r"^[ \t]*class[ \t]+\w+[ \t]*\(([^)]{0,500})\)", re.M)


def finish(table: Table, base: Path, sources: list[Path], resolve) -> None:
    names = crosstable.single(table, LANG)
    if not names:
        return
    pattern = "|".join(re.escape(w.name) for w in names.values())
    definition = re.compile(rf"def(?<![\w.]def)\s+({pattern})\s*\(")
    mention = re.compile(rf"(?:{crosstable.names_pattern([re.escape(w.name) for w in names.values()], r'\w')})(?!\w)")
    defs: dict[str, int] = {}
    facts = Facts(set(), set(), set(), {}, set())
    source = {str(p) for p in sources}
    test = lambda rel: scanner._doc_like(rel, ".py")  # noqa: E731
    mentioned: list[Path] = []
    for item in crosstable.walk(base, SUFFIXES, skip=test):
        if item is None:
            for w in names.values():
                table.note(w, "cap: too many Python files")
            return
        path, text = item
        facts.files.add(scanner.rel_path(path, base))
        for m in definition.finditer(text):
            defs[m.group(1)] = defs.get(m.group(1), 0) + 1
        if "class" in text:
            for m in BASES.finditer(text):
                facts.subclassed |= set(re.findall(r"[A-Za-z_]\w*", m.group(1)))
        facts.written |= set(re.findall(r"\.\s*([A-Za-z_]\w*)\s*(?:[-+*/%&|^@]|//|\*\*|>>|<<)?=(?!=)", text))
        if "setattr" in text:
            facts.written |= set(re.findall(r"setattr\s*\([^,()]*,\s*['\"]([A-Za-z_]\w*)['\"]", text))
        if str(path) in source and mention.search(text):
            mentioned.append(path)
    for key, w in names.items():
        if defs.get(w.name, 0) != 1:
            table.note(w, f"{defs.get(w.name, 0)} definitions of the name")
        elif w.name in facts.written:
            table.note(w, "the name is set as an attribute somewhere")
        else:
            table.followed[key] = w
            if w.method:
                _instance(base, w, facts)
                if _distributable(base, w.file):
                    facts.library.add(w.file)
    table.facts[LANG] = facts
    followed = table.wrappers(LANG)
    if len(mentioned) > crosstable.MAX_PARSE_FILES:
        for w in followed:
            table.note(w, "cap: too many files name it")
            table.followed.pop(w.key)
        return
    for path in mentioned:
        rel, text = scanner.rel_path(path, base), scanner._read(path)
        ctx = Module(text, rel) if text is not None else None
        for w in followed:
            for line, _, visible in (uses(ctx, w, table) if ctx is not None and ctx.tree is not None else [(0, None, None)]):
                crosstable.record(table, w, rel, line, visible is not None)
    crosstable.settle(table, LANG)


def _instance(base: Path, w: Wrapper, facts: Facts) -> None:
    """The module-level name the wrapper's module binds once to ``Owner(...)``, if any."""
    text = scanner._read(base / w.file)
    tree = pysyntax.parse(text) if text is not None else None
    if tree is None:
        return
    found = [
        n.targets[0].id for n in tree.body
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)
        and isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Name) and n.value.func.id == w.owner
    ]
    if len(found) == 1 and found[0] not in facts.written and bindings(tree).get(found[0], 0) == 1:
        if bindings(tree).get(w.owner, 0) == 1:  # the class itself is not rebound either
            facts.instances[(w.file, w.owner)] = found[0]


def _distributable(base: Path, rel: str) -> bool:
    """The nearest project file above ``rel`` builds a package others install (``setup.py``/``setup.cfg``,
    or a ``pyproject.toml`` with a ``[build-system]``)."""
    here = (base / rel).parent
    while True:
        if (here / "setup.py").is_file() or (here / "setup.cfg").is_file():
            return True
        project = here / "pyproject.toml"
        if project.is_file():
            try:
                return re.search(r"^\[build-system\]", project.read_text(errors="replace"), re.M) is not None
            except OSError:
                return True
        if here == base or base not in here.parents:
            return False
        here = here.parent


# --- pass 3: the call sites ----------------------------------------------------------------


def extend(text: str, table: Table | None, rel: str, found: list, calls: list[pyparse.PyCall]) -> list:
    """``found`` with each visible caller of a followed wrapper added (the wrapper's own site stays)."""
    if table is None:
        return found
    if table.collecting:
        collect(calls, table, rel)
        return found
    wrappers = [w for w in table.wrappers(LANG) if w.name in text]
    if not wrappers or scanner._doc_like(rel, ".py"):
        return found
    ctx = Module(text, rel)
    if ctx.tree is None:
        return found
    out = list(found)
    for w in wrappers:
        for line, _, visible in uses(ctx, w, table):
            if visible is not None:
                fields, hidden = visible
                out.append(pyparse.PyFound(line, w.call + w.through(), w.resolved, fields, not hidden, frozenset(hidden)))
    return out
