"""Render the App's templates with plain Jinja2 (no Starlette) and check the UI contract."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader

APP = Path(__file__).resolve().parents[2] / "src" / "apidrift" / "app"
STYLESHEET = APP / "static" / "style.css"
PAGES = ("landing.html", "installed.html", "privacy.html")
BUTTON = "Install on GitHub"


def _env() -> Environment:
    return Environment(loader=FileSystemLoader(APP / "templates"), autoescape=True)


def _demo() -> dict:
    return json.loads((APP / "demo.json").read_text())


def _context(**extra) -> dict:
    ctx = {"title": "apidrift", "stripe_latest": "2026-10-28.endive", "host": "example.test", "install_url": "/install"}
    ctx.update(extra)
    return ctx


def render(name: str, **extra) -> str:
    ctx = _context(**extra)
    if name == "landing.html" and "demo" not in ctx:
        ctx["demo"] = _demo()
    return _env().get_template(name).render(**ctx)


@pytest.fixture(scope="module")
def pages() -> dict[str, str]:
    return {name: render(name) for name in PAGES}


def test_landing_lists_every_demo_finding_with_its_permalink(pages):
    html = pages["landing.html"]
    demo = _demo()
    assert len(demo["findings"]) == 4
    for finding in demo["findings"]:
        assert f'{finding["file"]}:{finding["line"]}' in html
        assert f'href="{finding["url"]}"' in html
        assert finding["change_message"] in html
        assert finding["reason"] in html
    assert f'{demo["repo_url"]}/tree/{demo["sha"]}' in html
    assert str(demo["call_sites"]) in html and str(demo["unaffected"]) in html


def test_affected_findings_are_marked_with_bang_and_no_legend(pages):
    html = pages["landing.html"]
    assert html.count('<b class="mark">!</b>') == 4
    assert "?</b>" not in html
    assert "possibly affected (arguments not visible to the scanner)" not in html
    assert 'class="legend"' not in html
    assert "This is the block the issue contains" not in html


def test_possible_finding_is_marked_with_question_mark_and_legend_appears():
    demo = _demo()
    demo["findings"][1]["certainty"] = "possible"
    html = render("landing.html", demo=demo)
    assert html.count('<b class="mark">!</b>') == 4  # three findings plus the legend
    assert html.count('<b class="mark possible">?</b>') == 2  # one finding plus the legend
    assert f'<b class="mark possible">?</b> <a href="{demo["findings"][1]["url"]}">' in html
    legend = '<b class="mark">!</b> affected   <b class="mark possible">?</b> possibly affected (arguments not visible to the scanner)'
    assert html.count(legend) == 1
    assert html.index(legend) > html.index(demo["findings"][-1]["url"])  # legend is the last line of the block


def test_finding_block_has_one_shape_per_entry(pages):
    html = pages["landing.html"]
    block = html[html.index('<pre class="findings">'):html.index("</pre>")]
    assert block.count('<span class="finding">') == 4
    assert block.count('<span class="detail">') == 12
    assert "\n\n<span class=\"finding\">" in block  # one blank line after the title line
    assert "</span></span><span class=\"finding\">" in block  # no blank lines between entries


def test_untrusted_finding_fields_are_escaped():
    demo = _demo()
    demo["findings"][0]["file"] = 'x/<script>alert(1)</script>.py'
    demo["findings"][0]["call"] = "stripe.`a`|<b>"
    html = render("landing.html", demo=demo)
    assert "<script" not in html
    assert "&lt;script&gt;" in html and "&lt;b&gt;" in html


def test_install_button_appears_exactly_once_on_the_landing_only(pages):
    assert pages["landing.html"].count(BUTTON) == 1
    assert re.search(r'<a class="button" href="/install">Install on GitHub</a>', pages["landing.html"])
    assert pages["installed.html"].count(BUTTON) == 0
    assert pages["privacy.html"].count(BUTTON) == 0


def test_landing_states_what_it_does_and_does_not_do(pages):
    html = pages["landing.html"]
    assert "never runs your code" in html
    assert "Read access to code and permission to open issues; nothing else." in html
    assert "marked <code>?</code> (possibly affected)" in html
    assert "What it does not do yet" in html
    assert "No fix pull requests" in html and "Stripe only." in html
    assert "possibly affected" in html


def test_every_claim_links_to_its_evidence(pages):
    html = pages["landing.html"]
    assert 'href="https://docs.stripe.com/changelog"' in html
    assert 'href="https://github.com/Inky123/apidrift"' in html
    assert 'href="https://github.com/stripe-samples/accept-a-payment/tree/1a57c3b2f5aaf4ecd8de59ab0b1c3c5638827625"' in html


def test_context_values_are_used(pages):
    assert "2026-10-28.endive" in pages["landing.html"]
    assert "example.test" in pages["privacy.html"]
    assert "(the latest it holds is" not in render("landing.html", stripe_latest=None)


@pytest.mark.parametrize("name", PAGES)
def test_no_javascript_no_webfonts_no_inline_styles(name, pages):
    html = pages[name]
    assert "<script" not in html
    assert "fonts.googleapis" not in html and "@font-face" not in html
    assert "style=" not in html
    assert html.count('<link rel="stylesheet" href="/static/style.css">') == 1
    assert html.lstrip().startswith("<!doctype html>")
    assert "<title>apidrift</title>" in html


@pytest.mark.parametrize("name", PAGES)
def test_pages_use_sentence_case_headings_without_exclamation_marks(name, pages):
    html = pages[name]
    text = re.sub(r"<[^>]+>", "", html)
    assert "!" not in text.replace("! ", "")  # only the finding marker uses "!"
    for heading in re.findall(r"<h[12]>(.*?)</h[12]>", html):
        words = heading.split()
        assert words[0] == "apidrift" or words[0][0].isupper()
        assert all(w[0].islower() or w in {"Stripe", "GitHub", "API"} for w in words[1:]), heading


def test_stylesheet_is_small_and_has_no_webfonts_or_scripts():
    css = STYLESHEET.read_text()
    assert STYLESHEET.stat().st_size < 30 * 1024
    assert "@import" not in css and "@font-face" not in css and "url(" not in css
    assert "linear-gradient" not in css and "box-shadow" not in css and "border-radius" not in css
    assert "prefers-color-scheme: dark" in css


def test_installed_page_says_issues_disabled_is_skipped(pages):
    html = pages["installed.html"]
    assert "A repository with Issues disabled is skipped" in html
    assert "There is nothing to configure." in html


def test_privacy_page_covers_the_required_points(pages):
    html = pages["privacy.html"]
    for phrase in ("default branch", "Never file paths", "never tokens", "uninstall", "zachzap2@gmail.com"):
        assert phrase in html
    assert "Metadata: read" in html and "Contents: read" in html and "Issues: read and write" in html
    assert "number of Stripe call sites scanned, the number affected, a hash of the finding set, and the issue number" in html
    assert 'href="https://github.com/settings/installations"' in html
