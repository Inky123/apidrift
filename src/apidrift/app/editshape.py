"""Whether one edit the sandboxed child sent is the documented action on the documented key, written the way
the fixer writes it. Used by the parent for code fixes (:mod:`fixcheck`) and test fixes (:mod:`fixtestcheck`).

The rule everywhere: the bytes an edit removes are explained (the key's name, or one whole ``key: value``
entry), and the bytes it writes are a fixed template around verbatim copies of the original text (the new
key name, ``{ type: V }``, ``V[0]``, ...). So an accepted edit can never bring in code that was not already in
the file next to that key. What it cannot check is the data flow (that this literal feeds the call); the
child's oracle checked that, and the parent's checks bound what a lying child can do to the same kind of
rewrite on a key of the same name.

Shapes (``code=True`` also allows the forms that occur outside a literal's entries):

- rename: the old name at a key position (bare, quoted or a Ruby symbol, then its separator) becomes the new
  name; a JS shorthand ``{ k }`` becomes ``new: k``. Code also: a subscript or member write
  (``params['k'] =``, ``params[:k] =``, ``$p['k'] =``, ``p.k =``), Python ``setdefault('k', ...)``, and Go
  struct fields (``Old:`` in a literal, ``p.Old`` anywhere: the child proved the struct type).
- delete: one balanced entry with at most one adjacent comma; an own-line deletion of a last entry may keep
  only the rest of its comma's line.
- rename_first: the entry's key renamed, the value its literal array's first element or a fixed template
  around it (``V[0]``, ``(V)[0]``, ``V[0] ?? null``, ``V[0] if V else None``; Go ``New: first``).
- wrap_type: the value, verbatim, inside the language's ``{type: ...}`` template (Go: ``&stripe.X{Type: V}``).
- Go only: whitespace between a sibling field's ``Key:`` and its value (gofmt column alignment).
"""

from __future__ import annotations

import re

from .. import migrations

SEPARATORS = {  # (key form, separator) pairs each language writes a parameter key in
    "ruby": {("bare", ":"), ("symbol", "=>"), ("quoted", "=>"), ("quoted", ":")},
    "javascript": {("bare", ":"), ("quoted", ":")},
    "typescript": {("bare", ":"), ("quoted", ":")},
    "python": {("bare", "="), ("quoted", ":")},
    "php": {("quoted", "=>")},
    "go": {("bare", ":")},
}
CHAIN = re.compile(r"(?:\$|@{1,2})?[A-Za-z_]\w*(?:(?:\.|->|::|&\.)[A-Za-z_]\w*)*")
SAFE_STRING = re.compile(r"'[^'\\\n]*'|\"[^\"\\\n#${]*\"")
INTEGER = re.compile(r"\d+")
LINE_COMMENT = {
    "ruby": ("#",), "python": ("#",), "php": ("#", "//"), "javascript": ("//",), "typescript": ("//",), "go": ("//",),
}
ASSIGN = re.compile(r"[ \t]*(?:\|\||\?\?)?=(?![=>~])")


def names(m: migrations.Migration, language: str) -> tuple[str, str]:
    """The old and new spelling of the edited key: the leaf names, or stripe-go's struct fields."""
    if language == "go":
        from ..gofix import GO_RULES  # a table; nothing in it reads repository text

        rule = GO_RULES.get((m.path, m.target))
        return (rule.old, rule.new) if rule is not None else ("", "")
    return m.target.rsplit(".", 1)[-1], m.replacement.rsplit(".", 1)[-1] if m.replacement else ""


def shape(text: str, language: str, e: dict, m: migrations.Migration, *, code: bool = False) -> int | None:
    """Where the edited key is when ``e`` is ``m``'s action written the way the fixer writes it, else None."""
    old, new = names(m, language)
    start, end, span, out = e["start"], e["end"], text[e["start"] : e["end"]], e["text"]
    if not old or language not in SEPARATORS or (language == "go" and not code):
        return None
    if language == "go" and _padding(text, start, end, out):
        return start
    if m.action == "rename":
        if span == old and out == new and (_key(text, language, start, old) or (code and _written(text, language, start, end))):
            return start
        js = language in ("javascript", "typescript")
        if js and span == old and out == f"{new}: {old}" and _shorthand(text, start, end):
            return start
        return None
    if m.action == "delete":
        return _deletion(text, language, start, end, out, old)
    if m.action == "rename_first":
        if language in ("javascript", "typescript") and span == old and _shorthand(text, start, end):
            return start if out == f"{new}: {old}[0]" else None  # ``{ countries }`` -> ``country: countries[0]``
        key = _entry_key(text, language, start, old)
        if key is None or key[0] != start:
            return None
        name_at, value_at = key[1], key[2]
        value = text[value_at:end]
        if language == "go":  # ``Old: []*string{stripe.String("US")}`` -> ``New: stripe.String("US")``
            m2 = re.fullmatch(rf"{re.escape(new)}:([ \t]+)(.+)", out, re.S)
            if not m2:
                return None
            sliced = re.fullmatch(r"(\w+)\.StringSlice\(\[\]string\{(.*)\}\)", value, re.S)
            if sliced:  # ``stripe.StringSlice([]string{"US", "CA"})`` -> ``stripe.String("US")``
                inner = value_at + sliced.start(2)
                stop = _value_end(text, inner, value_at + sliced.end(2), language)
                first = text[inner:stop].strip() if stop is not None else ""
                return start if first and m2.group(2) == f"{sliced.group(1)}.String({first})" else None
            return start if _first(text, value_at, end, value, m2.group(2), language, code) else None
        head = text[start:name_at] + new + text[name_at + len(old) : value_at]
        return start if out.startswith(head) and _first(text, value_at, end, value, out[len(head) :], language, code) else None
    if m.action == "wrap_type":
        return start if _wrapped(text, language, start, end, span, out, old, m, code) else None
    return None


def _key(text: str, language: str, at: int, name: str) -> bool:
    """``text[at:]`` starts with ``name`` as a parameter key: bare, quoted or a Ruby symbol, then its separator."""
    if text[at : at + len(name)] != name:
        return False
    before = text[at - 1] if at else ""
    if before in "'\"" and before:
        form, after = "quoted", at + len(name) + 1
        if text[after - 1 : after] != before or (at >= 2 and (_ident(text[at - 2]) or text[at - 2] == ":")):
            return False  # unclosed, a prefixed string (r'', f''), or a Ruby :"symbol"
    elif before == ":" and language == "ruby":
        form, after = "symbol", at + len(name)
        if at >= 2 and (_ident(text[at - 2]) or text[at - 2] == ":"):
            return False
    else:
        form, after = "bare", at + len(name)
        if before and (_ident(before) or before in ".$@#:"):
            return False
        if after < len(text) and (_ident(text[after]) or text[after] in "?!"):
            return False
    if not _opens_entry(text, at - (0 if form == "bare" else 1), language):
        return False
    j = after
    while j < len(text) and text[j] in " \t":
        j += 1
    seps = [s for f, s in SEPARATORS.get(language, set()) if f == form]
    return any(
        text.startswith(s, j) and not text.startswith({":": "::", "=": "==", "=>": "=>>"}[s], j)
        and not (s == "=" and text.startswith("=>", j))
        for s in seps
    )


def _written(text: str, language: str, start: int, end: int) -> bool:
    """The name at ``[start, end)`` is a key written by a statement: ``p['k'] =``, ``p[:k] =``, ``p.k =``,
    Python ``p.setdefault('k', ...)``; Go any member ``p.Old`` (a struct field of the proven type)."""
    before, after = text[max(0, start - 40) : start], text[end : end + 40]
    if language == "go":
        return before.endswith(".") and len(before) > 1 and (_ident(before[-2]) or before[-2] in ")]") and not _ident(after[:1])
    quote = before[-1:] if before[-1:] in ("'", '"') else ""
    if quote and after.startswith(quote):
        inner_before, inner_after = before[:-1], after[1:]
        if re.search(r"[\w)\]]\s*\[\s*$", inner_before) and re.match(r"\s*\]", inner_after):
            return bool(ASSIGN.match(inner_after[re.match(r"\s*\]", inner_after).end() :]))
        if language == "python" and re.search(r"\.\s*setdefault\(\s*$", inner_before) and re.match(r"\s*,", inner_after):
            return True
        return False
    if language == "ruby" and re.search(r"[\w)\]]\s*\[\s*:$", before) and re.match(r"\s*\]", after):
        return bool(ASSIGN.match(after[re.match(r"\s*\]", after).end() :]))
    if language in ("javascript", "typescript", "python") and re.search(r"[\w$)\]]\s*\.$", before) and not _ident(after[:1]):
        return bool(ASSIGN.match(after))
    return False


def _padding(text: str, start: int, end: int, out: str) -> bool:
    """gofmt alignment: only spaces between a field's ``Key:`` and its value on the same line change."""
    line_start = text.rfind("\n", 0, start) + 1
    return (
        bool(re.fullmatch(r"[ \t]+", text[start:end])) and bool(re.fullmatch(r" +", out))
        and bool(re.fullmatch(r"[ \t]*[A-Z]\w*:", text[line_start:start]))
        and end < len(text) and text[end] not in " \t\r\n"
    )


def _opens_entry(text: str, token: int, language: str) -> bool:
    """The code before a key token ends an opener or a comma (Ruby: or a paren-less call's name)."""
    j = token
    while True:
        while j > 0 and text[j - 1].isspace():
            j -= 1
        line_start = text.rfind("\n", 0, j) + 1
        comment = _trailing_comment(text[line_start:j], language)
        if comment is None:
            break
        j = line_start + comment  # a comment ending the line before: the code before it counts
    c = text[j - 1] if j else ""
    return c in "{[(," and c != "" or (language == "ruby" and c != "" and (_ident(c) or c == "|") and j < token)


def _trailing_comment(line: str, language: str) -> int | None:
    """Where a line comment without quotes in it starts, when one ends ``line``."""
    marks = "|".join(re.escape(c) for c in LINE_COMMENT.get(language, ()))
    m = re.search(rf"(?:{marks})[^'\"`\n]*$", line) if marks else None
    return m.start() if m and m.start() < len(line) else None


def _entry_key(text: str, language: str, at: int, name: str) -> tuple[int, int, int] | None:
    """For an entry starting at ``at``: (token start, name start, value start) when its key is ``name``."""
    if text[at : at + 1] in ("'", '"') or (text[at : at + 1] == ":" and language == "ruby"):
        name_at = at + 1
    else:
        name_at = at
    if not _key(text, language, name_at, name):
        return None
    j = name_at + len(name) + (1 if text[at] in "'\"" else 0)
    while j < len(text) and text[j] in " \t":
        j += 1
    j += 2 if text.startswith("=>", j) else 1
    while j < len(text) and text[j] in " \t\r\n":
        j += 1
    return at, name_at, j


def _shorthand(text: str, start: int, end: int) -> bool:
    before = text[:start].rstrip()
    after = text[end:].lstrip()
    return before[-1:] in ("{", ",") and after[:1] in (",", "}") and not (start and _ident(text[start - 1]))


def _deletion(text: str, language: str, start: int, end: int, out: str, name: str) -> int | None:
    """One ``key: value`` entry and at most one comma next to it; ``out`` may only keep the rest of the line
    of the comma it removes (the fixer's own-line deletion of a last entry)."""
    lo, comma = start, False
    comments = "|".join(re.escape(c) for c in LINE_COMMENT[language])
    if language == "go" and not out:  # the whole ``p.Old = value`` statement, on lines of its own
        stmt = re.fullmatch(rf"[ \t]*(?:[A-Za-z_]\w*\.)+({re.escape(name)})[ \t]*=[ \t]*(.+?)[ \t]*(?://[^\n]*)?\r?\n",
                            text[start:end], re.S)
        if stmt and (start == 0 or text[start - 1] == "\n"):
            v = start + stmt.start(2)
            if _balanced(text, v, v + len(stmt.group(2)), language):
                return start + stmt.start(1)
    if out:
        rest_of_line = re.fullmatch(rf"[ \t]*(?:(?:{comments})[^\n]*)?\r?\n", out)
        if not (text[start : start + 1] == "," and text[start + 1 : start + 1 + len(out)] == out and rest_of_line):
            return None  # only the rest of the comma's own line (spaces, a comment, the line break) may be kept
        lo, comma = start + 1 + len(out), True
        if text[lo:end].lstrip(" \t")[:1] in ("", "\n", "\r"):
            return None  # the entry must start the next line
    i = _skip_space(text, lo, end)
    if text[i : i + 1] == ",":
        if comma:
            return None
        i, comma = _skip_space(text, i + 1, end), True
    key = _entry_key(text, language, i, name)
    if key is None or key[2] >= end:
        return None
    stop = _value_end(text, key[2], end, language)
    if stop is None or not text[key[2] : stop].strip():
        return None
    rest = text[stop:end]
    if rest.startswith(","):
        if comma:
            return None
        comma, rest = True, rest[1:]
    elif not re.match(r"\s*[)\]},]", text[end:]):
        return None  # the value did not end where the entry ends
    if not re.fullmatch(rf"[ \t]*(?:(?:{comments})[^\n]*)?\s*", rest):
        return None
    return key[1]


def _first(text: str, lo: int, hi: int, value: str, new: str, language: str, code: bool = False) -> bool:
    """``new`` is the first element of the array literal ``value``, or a fixed template around ``value``."""
    go_slice = re.match(r"\[\][\w.*]+\{", value) if language == "go" else None
    if value.startswith("[") and not go_slice or (language == "php" and value[:6].lower() == "array("):
        opener = lo if value.startswith("[") else lo + len("array")
    elif go_slice:
        opener = lo + go_slice.end() - 1
    else:
        opener = None
    if opener is not None:
        closer = {"[": "]", "(": ")", "{": "}"}[text[opener]]
        if text[hi - 1 : hi] != closer or _value_end(text, opener + 1, hi - 1, language, commas=False) is None:
            return False  # the literal is not the whole value
        i = _skip_space(text, opener + 1, hi - 1)
        stop = _value_end(text, i, hi - 1, language)
        return stop is not None and stop > i and new == text[i:stop].rstrip()
    if not (CHAIN.fullmatch(value) or (code and _balanced(text, lo, hi, language))):
        return False
    return new in (f"{value}[0]", f"({value})[0]", f"{value}[0] ?? null", f"{value}[0] if {value} else None")


def _wrapped(text: str, language: str, start: int, end: int, value: str, new: str, name: str, m, code: bool = False) -> bool:
    if language in ("javascript", "typescript") and value == name and new == f"{name}: {{ type: {name} }}":
        return m.wrap_any and _shorthand(text, start, end)
    if language == "go":
        return _go_wrapped(text, start, end, value, new, name, m)
    key = re.search(rf"(?:['\"]|:)?{re.escape(name)}['\"]?[ \t]*(?:=>|:|=)\s*$", text[max(0, start - 200) : start])
    if key is None:
        return False
    key_at = start - (len(text[max(0, start - 200) : start]) - key.start())
    entry = _entry_key(text, language, key_at, name)
    if entry is None or entry[2] != start or not _ends_entry(text, end, language, start):
        return False
    is_int = bool(INTEGER.fullmatch(value))
    content = value[1:-1] if SAFE_STRING.fullmatch(value) else (value[1:] if language == "ruby" and re.fullmatch(r":\w+", value) else None)
    if is_int:
        ok = m.timestamps
    elif content is not None:
        ok = content in m.wrap_values
    elif code:  # the child's allow-list judged the expression; here: it is copied whole and balanced
        ok = m.wrap_any and _balanced(text, start, end, language)
    else:
        ok = m.wrap_any and bool(CHAIN.fullmatch(value)) and value not in ("nil", "null", "None", "true", "false")
    forms = _wrap_forms(language, value)
    return ok and (new in forms["timestamp"] if is_int else new in forms["plain"])


def _go_wrapped(text: str, start: int, end: int, value: str, new: str, name: str, m) -> bool:
    from ..gofix import GO_RULES

    rule = GO_RULES.get((m.path, m.target))
    if rule is None or not rule.wrapper:
        return False
    flag = re.fullmatch(r"([A-Z]\w*):[ \t]*(\w+)\.Bool\(true\)", value)
    if flag and dict(rule.flags).get(flag.group(1)) and _key(text, "go", start, flag.group(1)):
        root, kind = flag.group(2), dict(rule.flags)[flag.group(1)]  # ``BillingCycleAnchorNow: stripe.Bool(true)``
        typed = re.fullmatch(rf"{re.escape(rule.old)}:( +)&{re.escape(root)}\.{re.escape(rule.wrapper)}"
                             rf"\{{Type: {re.escape(root)}\.String\(\"{re.escape(kind)}\"\)\}}", new)
        return bool(typed)
    before = text[max(0, start - 200) : start]
    entry = re.search(rf"(?<![\w.]){re.escape(name)}:[ \t]*$", before) and _ends_entry(text, end, "go", start)
    if not (entry or _go_statement(text, start, end, name)):
        return False  # the value of the ``Old:`` entry or of the ``p.Old = value`` statement, whole
    helper = re.fullmatch(r"(\w+)\.(String|Int64|Bool)\((.*)\)", value, re.S)
    if helper is None or not _balanced(text, start + helper.start(3), start + helper.end(3), "go"):
        return False  # the helper's argument is one balanced expression: no ``); other()`` smuggled in
    root, kind, arg = helper.groups()
    wrapped = f"&{root}.{rule.wrapper}"
    if kind == "String" and not m.timestamps and new == f"{wrapped}{{Type: {value}}}":
        return m.wrap_any or arg.strip().strip('"') in m.wrap_values
    if kind == "Int64" and m.timestamps:
        return new == f'{wrapped}{{Type: {root}.String("timestamp"), Timestamp: {value}}}'
    return False


def _go_statement(text: str, start: int, end: int, name: str) -> bool:
    """``[start, end)`` is the whole right side of a ``p.Old = value`` statement, as gofix writes the wrap: the
    statement starts its line or follows a ``{`` or ``;`` on it (``if reset { p.Old = v }``), and the value is
    followed by the line's end (a comment allowed), a ``;`` or the ``}`` closing a one-line block."""
    line_start = text.rfind("\n", 0, start) + 1
    head = re.fullmatch(rf"(?:[^\n]*[{{;])?[ \t]*(?:[A-Za-z_]\w*\.)+{re.escape(name)}[ \t]*=[ \t]*", text[line_start:start])
    tail = re.match(r"[ \t]*(?:[;}]|(?://[^\n]*)?(?:\r?\n|\Z))", text[end:])
    return bool(head and tail)


def _wrap_forms(language: str, v: str) -> dict[str, set[str]]:
    qs = ("'", '"')
    if language == "python":
        return {"plain": {f"{{{q}type{q}: {v}}}" for q in qs},
                "timestamp": {f"{{{q}type{q}: {q}timestamp{q}, {q}timestamp{q}: {v}}}" for q in qs}}
    if language == "php":
        return {"plain": {f"[{q}type{q} => {v}]" for q in qs},
                "timestamp": {f"[{q}type{q} => {q}timestamp{q}, {q}timestamp{q} => {v}]" for q in qs}}
    return {"plain": {f"{{ type: {v} }}"}, "timestamp": {f"{{ type: {q}timestamp{q}, timestamp: {v} }}" for q in qs}}


# --- a small balanced reader for the removed and kept text ----------------------------------


def _skip_space(text: str, i: int, hi: int) -> int:
    while i < hi and text[i].isspace():
        i += 1
    return i


def _balanced(text: str, lo: int, hi: int, language: str) -> bool:
    return lo < hi and _value_end(text, lo, hi, language, commas=False) == hi


def _ends_entry(text: str, end: int, language: str, start: int | None = None) -> bool:
    """The value that starts at ``start`` ends exactly at ``end``: the next top-level comma or closer comes
    after it, with only spaces and comments between."""
    if start is None:
        return False
    stop = _value_end(text, start, min(len(text), end + 100_000), language, closer_stops=True)
    if stop is None or stop < end:
        return False
    comments = "|".join(re.escape(c) for c in LINE_COMMENT[language])
    return bool(re.fullmatch(rf"(?:\s|(?:{comments})[^\n]*)*", text[end:stop]))


def _value_end(text: str, i: int, hi: int, language: str, commas: bool = True, closer_stops: bool = False) -> int | None:
    """Index of the first top-level ``,`` in ``[i, hi)`` (``hi`` when none, or when not ``commas``); None when
    a string, comment or bracket there is unbalanced (``closer_stops``: an unmatched closer ends it there)."""
    quotes = "'\"`" if language in ("javascript", "typescript", "go") else "'\""
    pairs = {"(": ")", "[": "]", "{": "}"}
    stack: list[str] = []
    while i < hi:
        c = text[i]
        if c in quotes:
            j = i + 1
            while j < hi and text[j] != c:
                j += 2 if text[j] == "\\" and c != "`" else 1
            if j >= hi:
                return None
            i = j + 1
            continue
        if any(text.startswith(x, i) for x in LINE_COMMENT[language]):
            nl = text.find("\n", i, hi)
            if nl < 0:
                return None
            i = nl
            continue
        if text.startswith("/*", i) and language not in ("ruby", "python"):
            close = text.find("*/", i + 2, hi)
            if close < 0:
                return None
            i = close + 2
            continue
        if c in pairs:
            stack.append(pairs[c])
        elif c in ")]}":
            if not stack and closer_stops:
                return i
            if not stack or stack.pop() != c:
                return None
        elif c == "," and not stack and commas:
            return i
        i += 1
    return None if stack else hi


def _ident(c: str) -> bool:
    return c.isalnum() or c in "_$"
