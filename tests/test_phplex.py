"""The PHP pass that decides what is code: strings, heredocs, comments, inline HTML, bracket pairs.

The fixer has no PHP binary to ask, so it leans on this pass twice: a file it cannot read with certainty
is refused, and an edit must leave everything outside itself structurally unchanged.
"""

from dataclasses import dataclass

import pytest

from apidrift.phplex import COMMENT, HEREDOC_KIND, HTML, STRING, Lex, mask_spans, same_structure

H = "<?php\n"


def kinds(text):
    lex = Lex(text)
    return [(text[a:b], kind) for a, b, kind in lex.spans if kind != HTML], lex


@pytest.mark.parametrize("body,spans", [
    ("$a = 'it\\'s [not (code';", [("'it\\'s [not (code'", STRING)]),
    ("$a = \"x {$o[\"k\"]} ) y\";", [("\"x {$o[\"k\"]} ) y\"", STRING)]),
    ("$a = \"${v} \\\" ]\";", [("\"${v} \\\" ]\"", STRING)]),
    ("$a = `ls {$dir}`;", [("`ls {$dir}`", STRING)]),
    ("f(); // it's (\ng();", [("// it's (", COMMENT)]),
    ("f(); # don't [\ng();", [("# don't [", COMMENT)]),
    ("f(/* ' ( */ 1);", [("/* ' ( */", COMMENT)]),
    ("$a = <<<EOT\n it's ( {$x['k']}\nEOT;\n", [("<<<EOT\n it's ( {$x['k']}\nEOT", HEREDOC_KIND)]),
    ("$a = <<<'EOT'\n \" [\n  EOT;\n", [("<<<'EOT'\n \" [\n  EOT", HEREDOC_KIND)]),
    ("f(<<<\"X\"\n a\n X, 2);", [("<<<\"X\"\n a\n X", HEREDOC_KIND)]),
    ("#[Route('/x')]\nfunction f() {}", [("'/x'", STRING)]),
])
def test_strings_comments_and_heredocs_are_opaque_and_the_rest_balances(body, spans):
    found, lex = kinds(H + body)
    assert found == spans
    assert lex.balanced and not lex.uncertain


def test_html_outside_the_tags_is_not_code_and_a_closing_tag_ends_a_line_comment():
    text = "<p>it's (</p><?php f(1); // done ?><b>]</b><?= $x ?> tail ("
    lex = Lex(text)
    assert [(text[a:b], k) for a, b, k in lex.spans] == [
        ("<p>it's (</p><?php", HTML), ("// done ", COMMENT), ("?><b>]</b><?=", HTML), ("?> tail (", HTML),
    ]
    assert lex.balanced and not lex.uncertain
    assert lex.code.replace(" ", "") == "f(1);$x"


@pytest.mark.parametrize("body,balanced,uncertain", [
    ("f(1;", False, False),
    ("f(1));", False, False),
    ("f([1)];", False, False),
    ("$a = 'never closed;\n", True, True),
    ("/* never closed\n", True, True),
    ("$a = <<<EOT\n never closed\n", True, True),
    # PHP 7.3 ends this heredoc at "  EOT is here"; older PHP at the last line. The pass does not choose.
    ("$a = <<<EOT\n  EOT is here\nEOT;\n", True, True),
    ("$a = <<<EOT\n  text\n  EOT;\n", True, False),
    ("$a = <<<EOT\n  text\nEOT;\n", True, False),
])
def test_a_file_that_cannot_be_read_with_certainty_says_so(body, balanced, uncertain):
    lex = Lex(H + body)
    assert (lex.balanced, lex.uncertain) == (balanced, uncertain)


def test_bracket_pairs_and_the_bracket_around_a_position():
    text = H + "f([1, g(2)], 'x)');"
    lex = Lex(text)
    f, sq, g = text.index("("), text.index("["), text.index("(", text.index("g"))
    assert lex.pairs == {f: text.rindex(")"), sq: text.index("]"), g: text.index(")", g)}
    assert lex.enclosing(text.index("2")) == g and lex.enclosing(text.index("1")) == sq
    assert lex.enclosing(text.index("'x")) == f and lex.enclosing(2) == -1
    assert [text[a:b].strip() for a, b in lex.args(f)] == ["[1, g(2)]", "'x)'"]


def test_replacement_text_without_a_tag_is_read_as_code():
    assert mask_spans("'k' => $v // c") == [(0, 3), (10, 14)]
    assert mask_spans("plain <?php 'k'") == [(0, 11), (12, 15)]


@dataclass(frozen=True)
class E:
    start: int
    end: int
    text: str


def edit(text, old, new):
    at = text.index(old)
    return text[:at] + new + text[at + len(old):], [E(at, at + len(old), new)]


BASE = H + "f(['k' => 'v', 'n' => [1, 2]], \"s {$a['b']}\"); // tail\n$x = <<<T\n body\n T;\n"


@pytest.mark.parametrize("old,new", [
    ("k", "renamed"),  # inside a string
    ("'v'", "['type' => 'v']"),  # a value wrapped
    ("'k' => 'v', ", ""),  # an entry deleted
    ("[1, 2]", "1"),  # a whole array replaced
])
def test_an_edit_that_changes_only_itself_keeps_the_structure(old, new):
    after, edits = edit(BASE, old, new)
    assert same_structure(BASE, after, edits)


@pytest.mark.parametrize("old,new", [
    ("k", "k' => 1, 'j"),  # still balanced, but the string it was in is now two strings
    ("'v'", "'v"),  # the closing quote is gone: everything after reads differently
    ("'v'", "['type' => 'v'"),  # one bracket short
    ("'v'", "'v']"),  # one bracket too many: pairs outside the edit move
    ("1, 2", "1, 2] + [3"),  # balanced overall, but the outer pair no longer closes where it did
    ("'v'", "'v' /* open"),  # swallows the rest of the file into a comment
    ("'v'", "<<<T\n v\n T"),  # fine on its own...
])
def test_an_edit_that_changes_how_the_rest_is_read_is_caught(old, new):
    after, edits = edit(BASE, old, new)
    assert same_structure(BASE, after, edits) is (new.startswith("<<<"))


def test_same_structure_refuses_a_file_that_was_not_readable_to_begin_with():
    broken = H + "f(['k' => 'v'];\n"
    after, edits = edit(broken, "k", "j")
    assert not same_structure(broken, after, edits)
