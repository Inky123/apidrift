"""Render the App's templates with plain Jinja2 (no Starlette) and check the UI contract."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader

from apidrift.app import issue

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


# --- the demo data --------------------------------------------------------------------


def test_demo_names_the_release_that_introduced_the_break_not_the_latest():
    demo = _demo()
    assert demo["introduced"] == "2026-09-30.endive" and demo["new_version"] == "2026-10-28.endive"
    assert demo["versions"] == ["2026-08-26.dahlia", "2026-09-30.endive", "2026-10-28.endive"]
    assert demo["title"] == f"Upgrading to Stripe 2026-09-30.endive breaks {len(demo['findings'])} call sites"
    assert len(demo["groups"]) == 1
    group = demo["groups"][0]
    assert group["heading"] == "payment_method_types removed from POST /v1/payment_intents"
    assert group["changelog"] == "https://docs.stripe.com/changelog/endive"
    assert group["consequence"] == "Requests that still send it are rejected."


def test_demo_findings_match_the_issue_contract_and_the_groups():
    demo = _demo()
    assert len(demo["findings"]) >= 4
    assert [tuple(f) for f in demo["findings"]] == [issue.FINDING_KEYS] * len(demo["findings"])
    assert [f for g in demo["groups"] for f in g["findings"]] == demo["findings"]
    assert {f["introduced"] for f in demo["findings"]} == {"2026-09-30.endive"}
    assert demo["call_sites"] - demo["unaffected"] == issue.distinct_sites(demo["findings"])


# --- the landing page -----------------------------------------------------------------


def test_landing_lists_every_demo_finding_with_its_permalink(pages):
    html = pages["landing.html"]
    demo = _demo()
    for finding in demo["findings"]:
        assert f'{finding["file"]}:{finding["line"]}' in html
        assert f'href="{finding["url"]}"' in html
        assert finding["call"] in html and finding["reason"] in html
    for group in demo["groups"]:
        assert f"<b>{group['heading']}</b> (introduced in Stripe {group['introduced']}" in html
        assert group["consequence"] in html and f'href="{group["changelog"]}"' in html
    assert f'{demo["repo_url"]}/tree/{demo["sha"]}' in html
    assert str(demo["call_sites"]) in html and str(demo["unaffected"]) in html
    assert demo["title"] in html


def test_landing_names_the_introducing_release_not_the_latest(pages):
    html = pages["landing.html"]
    assert "Stripe's <code>2026-09-30.endive</code> release removed <code>payment_method_types</code>" in html
    assert "<code>2026-10-28.endive</code>, adds nothing that breaks this repository" in html
    assert "2026-10-28.endive</code> release" not in html


def test_affected_findings_are_marked_with_bang_and_no_legend(pages):
    html = pages["landing.html"]
    assert html.count('<b class="mark">!</b>') == len(_demo()["findings"])
    assert "?</b>" not in html
    assert "possibly affected (arguments not visible to the scanner)" not in html
    assert 'class="legend"' not in html


def _mark_possible(demo: dict, index: int) -> dict:
    for item in (demo["findings"][index], demo["groups"][0]["findings"][index]):
        item["certainty"] = "possible"
    return demo


def test_possible_finding_is_marked_with_question_mark_and_legend_appears():
    demo = _mark_possible(_demo(), 1)
    html = render("landing.html", demo=demo)
    assert html.count('<b class="mark">!</b>') == len(demo["findings"])  # all but one finding, plus the legend
    assert html.count('<b class="mark possible">?</b>') == 2  # one finding plus the legend
    assert f'<b class="mark possible">?</b> <a href="{demo["findings"][1]["url"]}">' in html
    legend = '<b class="mark">!</b> affected   <b class="mark possible">?</b> possibly affected (arguments not visible to the scanner)'
    assert html.count(legend) == 1
    assert html.index(legend) > html.index(demo["findings"][-1]["url"])  # legend is the last line of the block


def test_finding_block_mirrors_the_issue_shape(pages):
    html = pages["landing.html"]
    block = html[html.index('<pre class="findings">'):html.index("</pre>")]
    count = len(_demo()["findings"])
    assert block.count('<span class="group">') == 1
    assert block.count('<span class="finding">') == count
    assert block.count('<span class="detail">') == 2 * count  # "via" and "why" per finding; the change is in the group heading
    assert "\n\n<span class=\"group\">" in block  # one blank line after the title line
    assert "</span><span class=\"finding\">" in block  # the first finding follows the group heading at once
    assert "</span></span><span class=\"finding\">" in block  # no blank lines between entries


def test_untrusted_finding_fields_are_escaped():
    demo = _demo()
    for item in (demo["findings"][0], demo["groups"][0]["findings"][0]):
        item["file"] = 'x/<script>alert(1)</script>.py'
        item["call"] = "stripe.`a`|<b>"
    demo["groups"][0]["heading"] = "<img src=x>"
    html = render("landing.html", demo=demo)
    assert "<script" not in html and "<img" not in html
    assert "&lt;script&gt;" in html and "&lt;b&gt;" in html and "&lt;img" in html


def test_install_button_once_plus_a_small_link_after_the_lede(pages):
    html = pages["landing.html"]
    assert len(re.findall(r'<a class="button" href="/install">Install on GitHub</a>', html)) == 1
    assert html.count(BUTTON) == 2
    lede = html.index('class="lede"')
    aside = html.index('<p class="aside"><a href="/install">Install on GitHub</a></p>')
    assert lede < aside < html.index("<h2>A real example</h2>")
    assert pages["installed.html"].count(BUTTON) == 0
    assert pages["privacy.html"].count(BUTTON) == 0


def test_pricing_line_sits_under_the_install_button(pages):
    html = pages["landing.html"]
    button = html.index('<a class="button" href="/install">')
    pricing = html.index('<p class="pricing">Free while it is Stripe-only.</p>')
    assert button < pricing < button + 200


def test_what_apidrift_is_comes_before_the_example(pages):
    html = pages["landing.html"]
    sentence = html.index("apidrift watches Stripe's published OpenAPI spec.")
    assert html.index('class="lede"') < sentence < html.index("<h2>A real example</h2>")


def test_landing_states_what_it_does_and_does_not_do(pages):
    html = pages["landing.html"]
    assert "never runs your code" in html
    assert "Read access to code and permission to open issues; nothing else." not in html
    assert (
        "Four permissions: read and write access to code and pull requests, read and write access to issues, "
        "and the repository metadata GitHub requires of every App. What it writes is one branch and one pull "
        "request per repository; it never pushes to your default branch." in html
    )
    assert "marked <code>?</code> (possibly affected)" in html
    assert "What it does not do yet" in html
    assert "No fix pull requests" not in html and "That is next" not in html
    assert "Stripe only." in html
    assert "possibly affected" in html


def test_landing_describes_the_pull_request_step_honestly(pages):
    html = pages["landing.html"]
    assert "When Stripe documents a replacement, it also opens one pull request with the rename applied." in html
    assert "pushes a branch named <code>apidrift/stripe-&lt;version&gt;</code>, and opens one pull request per repository" in html
    assert "re-scans the changed files to confirm they no longer send the old one" in html
    assert "lists the API-version pins it found and asks you to merge it together with your API-version and SDK upgrade" in html
    assert "Merging stays a human click." in html
    limitation = (
        "Pull requests only for migrations Stripe has documented a replacement for (today: "
        "<code>payment_method_types</code> renamed to <code>allowed_payment_method_types</code>), "
        "verified by re-scanning the changed files. Every other breaking change still arrives as an issue."
    )
    assert f"<li>{limitation}</li>" in html
    assert html.index("<h2>What it does not do yet</h2>") < html.index(limitation)
    assert html.count("<li>") == 9  # four "How it works" steps and five limitations
    assert "merges" not in html  # it never claims to merge anything itself


def test_every_claim_links_to_its_evidence(pages):
    html = pages["landing.html"]
    assert 'href="https://docs.stripe.com/changelog/endive"' in html
    assert 'href="https://docs.stripe.com/changelog"' not in html  # the per-codename page, not the index
    assert 'href="https://github.com/stripe-samples/accept-a-payment/tree/1a57c3b2f5aaf4ecd8de59ab0b1c3c5638827625"' in html


def test_context_values_are_used(pages):
    assert "2026-10-28.endive" in pages["landing.html"]
    assert "example.test" in pages["privacy.html"]
    assert "(the latest it holds is" not in render("landing.html", stripe_latest=None)


# --- every page -----------------------------------------------------------------------


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
def test_head_carries_description_open_graph_and_a_data_uri_favicon(name, pages):
    html = pages[name]
    default = "Stripe changed its API. apidrift opens one GitHub issue that links to the line in your code that breaks."
    assert f'<meta name="description" content="{default}">' in html
    assert '<meta property="og:title" content="apidrift">' in html
    assert f'<meta property="og:description" content="{default}">' in html
    assert re.search(r'<link rel="icon" href="data:image/svg\+xml,[^"]+">', html)
    assert "http" not in re.search(r'<link rel="icon" href="([^"]+)"', html).group(1).replace("xmlns='http://www.w3.org/2000/svg'", "")


def test_a_page_specific_description_replaces_the_default():
    html = render("privacy.html", description="What apidrift reads.")
    assert '<meta name="description" content="What apidrift reads.">' in html
    assert '<meta property="og:description" content="What apidrift reads.">' in html


@pytest.mark.parametrize("name", PAGES)
def test_footer_has_privacy_but_no_source_link_while_the_repo_is_private(name, pages):
    html = pages[name]
    assert 'href="/privacy"' in html
    assert ">Source<" not in html and "github.com/Inky123/apidrift" not in html


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


# --- installed and privacy ------------------------------------------------------------


def test_installed_page_says_what_happens_next(pages):
    html = pages["installed.html"]
    assert "A repository with Issues disabled is skipped" in html
    assert "There is nothing to configure." in html
    assert "within a few minutes" in html
    assert "<code>apidrift-check[bot]</code> opens one issue in that repository's Issues tab" in html
    assert "Repositories that are clean get nothing." in html
    assert 'href="https://github.com/settings/installations"' in html


def test_installed_page_says_a_pull_request_may_follow_and_is_safe_to_close(pages):
    html = pages["installed.html"]
    assert "may also receive a pull request from <code>apidrift-check[bot]</code>" in html
    assert "on a branch named <code>apidrift/stripe-&lt;version&gt;</code>, with the rename applied" in html
    assert "It is safe to close it: apidrift never merges and never pushes to your default branch." in html
    assert "the issue gets one comment with the link to accept them instead" in html


def test_privacy_page_covers_the_required_points(pages):
    html = pages["privacy.html"]
    for phrase in ("default branch", "Never file paths", "never tokens", "uninstall", "zachzap2@gmail.com"):
        assert phrase in html
    assert "four permissions" in html and "three permissions" not in html
    for permission in ("Metadata: read", "Contents: read and write", "Pull requests: read and write", "Issues: read and write"):
        assert html.count(permission) == 1
    assert "Issues: read and write. Nothing else." in html
    assert "writes exactly two things more: one branch named <code>apidrift/stripe-&lt;version&gt;</code> and one pull request per repository" in html
    assert "It never writes to the default branch and never force-pushes over a branch a person has changed." in html
    assert (
        "number of Stripe call sites scanned, the number affected, a hash of the finding set, the issue number and, "
        "when a pull request was opened, its number, branch name, head commit and a hash of the fix set" in html
    )
    assert "any fix is computed, in a separate process that holds no credentials" in html
    assert 'href="https://github.com/settings/installations"' in html
    assert "DigitalOcean" in html
    assert "The code is never executed." in html
    assert "Logs contain repository ids only" in html
    assert "never written to disk" in html
