"""Code inside strings (:mod:`strholes`): interpolation holes are code, the text around them is not, and
the fixer's token count reads the key through them. Outside the holes the spans are the plain masks'
(``JS_MASK``, ``RUBY_MASK``, ``PY_MASK``, :func:`phplex.mask_spans`): nothing that was code becomes text."""

import pytest

from apidrift import fixtokens, strholes
from test_fixer import OLD


# --- holes per language ------------------------------------------------------------


def code(text: str, language: str) -> str:
    """``text`` with its masked spans blanked: what is left is read as code."""
    out = list(text)
    for a, b in strholes.masked_spans(text, language):
        out[a:b] = " " * (b - a)
    return "".join(out)


def test_js_template_holes_are_code_and_the_text_around_them_is_not():
    text = "f(`aa ${p.k} bb ${`in ${q.k} ner`} cc`, 'xx ${no}', \"yy\", // ${nor} \n /* ${nope} */ zz)"
    masked = code(text, "javascript")
    assert "p.k" in masked and "q.k" in masked and masked.endswith(" zz)")
    for hidden in ("aa", "bb", "in", "ner", "cc", "xx", "no", "yy", "nor", "nope"):
        assert hidden not in masked, hidden


def test_js_unterminated_template_is_code():
    text = "x = `abc ${p.k}"
    assert code(text, "javascript") == text


def test_ruby_holes_in_double_quotes_heredocs_and_percent_literals():
    text = 'f("aa #{p[:k]} bb", \'#{not}\', <<~M)\n  tt #{q[:k]}\nM\n# #{comment} xx\ny = %Q(#{r[:k]} ok)\n'
    masked = code(text, "ruby")
    assert "p[:k]" in masked and "q[:k]" in masked and "r[:k]" in masked  # the heredoc's and %Q's holes too
    for hidden in ("aa", "bb", "not", "comment", "xx"):
        assert hidden not in masked, hidden
    assert "tt" in masked and " ok" in masked  # heredoc and %Q text stay code, as RUBY_MASK reads them


def test_python_fstring_holes_nested_strings_and_doubled_braces():
    text = "f(f\"a {p['k']} {{not}} b\", rf'{q[\"k\"]}', 'c {no}', \"\"\"d {nope}\"\"\", f'''e {r.k}''', b'{bytes}')"
    masked = code(text, "python")
    for seen in ("p[", "q[", "r.k"):
        assert seen in masked
    for hidden in ("not", "no}", "nope", "bytes", "'k'", '"k"'):
        assert hidden not in masked


def test_php_holes_in_double_quotes_backticks_and_heredocs_not_in_single_quotes_or_nowdocs():
    text = (
        "<?php\n$a = \"x {$p['k']} $q[k] $r->k ${s} \\$t\";\n$b = 'no {$u[k]}';\n"
        "$c = <<<EOT\nh {$v['k']} $w[k]\nEOT;\n$d = <<<'EOT'\nn {$x['k']}\nEOT;\n$e = `$y[k]`;\n"
    )
    masked = code(text, "php")
    for seen in ("$p[", "$q[k]", "$r->k", " s ", "$v[", "$w[k]", "$y[k]"):
        assert seen in masked, seen
    for hidden in ("$t", "$u", "$x", "'k'", "no ", "h ", "n "):
        assert hidden not in masked, hidden


@pytest.mark.parametrize("language,text", [
    ("javascript", f"logger.info(`t ${{p.{OLD}}}`); x = '{OLD}'; y = `{OLD}`;"),
    ("ruby", f"logger.info(\"t #{{p[:{OLD}]}}\"); x = '{OLD}'"),
    ("python", f"log.info(f\"t {{p['{OLD}']}}\"); x = '{OLD}'"),
    ("php", f"<?php log(\"t {{$p['{OLD}']}} $q[{OLD}]\"); $x = '{OLD}';"),
])
def test_tokens_see_the_key_inside_a_hole(language, text):
    found = fixtokens.tokens(text, language, OLD)
    assert len(found) == (3 if language == "php" else 2)  # the read(s) in the hole and the quoted key; not the template
    assert fixtokens.accessed(text, language, OLD, found[0])
