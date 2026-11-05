"""The "Your options" block of the issue: which groups get one, what it says, how it folds, and what it never shows."""

import json

from conftest import NEW_VERSION, OLD_VERSION, SHA, claim

from apidrift import guidance
from apidrift.app import db, issue
from apidrift.app.checker import run_job
from apidrift.app.prbody import UNFIXABLE_WHY

FP = "cd" * 32
ENDIVE = "2026-09-30.endive"
PIN_LINE = "- Stay on your current API version until this code is changed \\(how\\: the note at the end of this issue\\)."
NOTE_START = "Staying on your current API version\\: each Stripe SDK release sends"
PMT_DOC = ("https://docs.stripe.com/changelog/endive/2026-09-30/"
           "removes-the-payment-method-types-parameter-from-payment-intents-and-setup-intents")


def finding(**overrides):
    base = {
        "file": "src/pay.py", "line": 12, "method": "POST", "path": "/v1/charges", "label": "",
        "call": "stripe.Charge.create", "language": "python", "change_kind": "body_removed",
        "change_message": "m", "change_target": "description", "introduced": NEW_VERSION,
        "consequence": "Requests that still send it are rejected.", "reason": "description is sent here",
        "certainty": "affected",
    }
    base.update(overrides)
    base["label"] = f"{base['method']} {base['path']}"
    base["url"] = issue.permalink("octo/shop", SHA, base["file"], base["line"])
    return base


def pmt(path="/v1/payment_intents", **overrides):
    """A finding the migration table answers (payment_method_types -> allowed_payment_method_types)."""
    return finding(path=path, change_target="payment_method_types", introduced=ENDIVE, **overrides)


def render(items, **overrides):
    kwargs = dict(full_name="octo/shop", sha=SHA, old_version=OLD_VERSION, new_version=NEW_VERSION, call_sites=9, fingerprint=FP)
    kwargs.update(overrides)
    return issue.render_issue(items, **kwargs)[1]


def options_of(body: str) -> list[str]:
    """The option bullets in a body (site bullets start with a link or a code span and carry " via ")."""
    return [ln for ln in body.splitlines() if ln.startswith("- ") and " via " not in ln]


def test_a_curated_change_lists_stripes_documented_choices_with_their_sources():
    body = render([finding(path="/v1/checkout/sessions", change_target="line_items[].dynamic_tax_rates")])
    assert options_of(body) == [
        "- Let Stripe Tax work out the rate\\: set `automatic_tax.enabled` to true on the session and drop the list of "
        "rates; Checkout then calculates tax from the customer's address \\(Stripe Tax has to be set up on the "
        "account\\). ([Stripe docs](https://docs.stripe.com/payments/checkout/taxes))",
        "- Pass fixed rates instead\\: put the Tax Rate IDs in `line_items[].tax_rates` \\(for subscriptions, "
        "`subscription_data.default_tax_rates`\\); this fits when you know the rate before checkout starts. "
        "([Stripe docs](https://docs.stripe.com/payments/checkout/use-manual-tax-rates))",
        PIN_LINE,
    ]
    assert "\n\nYour options:\n- Let Stripe Tax" in body and "<details>" not in body
    assert body.count(NOTE_START) == 1 and "([Stripe docs](https://docs.stripe.com/api/versioning))\n\n---\n" in body


def test_a_change_without_an_entry_gets_the_generic_options_for_its_kind():
    removed = options_of(render([finding()]))
    assert len(removed) == 2 and removed[0].startswith("- Remove the field from the request at the listed lines") and removed[1] == PIN_LINE
    reader = finding(path="/v1/terminal/readers/{reader}/cancel_action", change_kind="response_schema_changed", change_target="")
    assert options_of(render([reader])) == [
        "- Where the result of the listed calls is read, handle each shape the response can now take.", PIN_LINE,
    ]
    gone = options_of(render([finding(change_kind="endpoint_removed", change_target="")]))
    assert gone == [PIN_LINE]
    assert "Your options" not in render([finding(change_kind="endpoint_deprecated", change_target="")])


def test_a_group_the_fix_pull_request_covers_is_rendered_exactly_as_before():
    items = [pmt(**{issue.FIX_KEY: issue.PLANNED}), pmt(line=30, **{issue.FIX_KEY: issue.PLANNED})]
    body = render(items)
    assert body == (
        f"Upgrading to Stripe {ENDIVE} breaks 2 call sites in octo/shop at 1a57c3b.\n"
        "\n"
        f"**payment_method_types removed from POST /v1/payment_intents** (introduced in Stripe {ENDIVE}, [changelog]({PMT_DOC}))\n"
        "Requests that still send it are rejected.\n"
        f"- https://github.com/octo/shop/blob/{SHA}/src/pay.py#L12 via `stripe.Charge.create`\n"
        f"- https://github.com/octo/shop/blob/{SHA}/src/pay.py#L30 via `stripe.Charge.create`\n"
        "\n"
        "---\n"
        f"Scanned octo/shop at {SHA} against Stripe {OLD_VERSION} -> {NEW_VERSION}: 9 Stripe call sites; "
        "unaffected call sites are not listed.\n"
        f"<!-- apidrift:fp={FP} -->\n"
    )


def test_a_possibly_affected_site_of_a_documented_migration_is_told_where_to_apply_it():
    hidden = pmt("/v1/setup_intents", certainty="possible", reason="arguments are not fully visible at this call",
                 **{issue.FIX_KEY: issue.MANUAL})
    body = render([hidden, pmt(**{issue.FIX_KEY: issue.PLANNED})])
    assert options_of(body) == [
        "- For a possibly affected site, find where the arguments apidrift cannot see are built \\(the spread or "
        "variable in that call\\) and, if they carry the field, rename `payment_method_types` to "
        f"`allowed_payment_method_types` there. ([Stripe docs]({PMT_DOC}))",
        PIN_LINE,
    ]
    covered, uncovered = body.index("POST /v1/payment_intents**"), body.index("POST /v1/setup_intents**")
    assert covered < uncovered < body.index("Your options:")  # only the group the pull request leaves alone has a list
    assert body.count("Your options") == 1


def test_a_refused_site_says_why_and_offers_the_migration_by_hand():
    refused = pmt(line=40, **{issue.FIX_KEY: "nested"})
    body = render([pmt(**{issue.FIX_KEY: issue.PLANNED}), refused])
    assert f"#L40 via `stripe.Charge.create` (not rewritten by apidrift: {UNFIXABLE_WHY['nested']})\n" in body
    assert "#L12 via `stripe.Charge.create`\n" in body  # the planned site is unchanged
    assert "\n\nYour options for the call sites apidrift does not rewrite:\n" in body
    assert options_of(body) == [
        "- Make Stripe's documented change by hand at the lines apidrift did not rewrite\\: rename "
        f"`payment_method_types` to `allowed_payment_method_types`. ([Stripe docs]({PMT_DOC}))",
        PIN_LINE,
    ]
    alone = render([refused])
    assert "\n\nYour options:\n- Make Stripe's documented change by hand" in alone
    unexplained = render([pmt(**{issue.FIX_KEY: issue.MANUAL})])  # documented, not planned, no reason known
    assert "not rewritten by apidrift" not in unexplained and "by hand at the lines apidrift did not rewrite" in unexplained


def plan_payload(**plan) -> dict:
    def impact(file, line, path, certainty="affected"):
        return {
            "certainty": certainty, "reason": "r",
            "usage": {"file": file, "line": line, "method": "post", "path": path, "call": "c", "language": "javascript"},
            "change": {"severity": "breaking", "kind": "body_removed", "message": "m", "method": "post", "path": path,
                       "target": "payment_method_types", "introduced": ENDIVE},
        }

    impacts = [
        impact("a.js", 1, "/v1/payment_intents"), impact("b.js", 2, "/v1/payment_intents"),
        impact("c.js", 3, "/v1/payment_intents"), impact("d.js", 4, "/v1/payment_intents"),
        impact("e.js", 5, "/v1/setup_intents", "possible"),
    ]
    impacts.append(impact("f.js", 6, "/v1/charges"))
    impacts[-1]["change"]["target"] = "description"
    return {"impacts": impacts, "new_version": "2026-10-28.endive", **plan}


def test_findings_read_the_fix_plan_by_keys_the_parent_derives():
    site = {"path": "/v1/payment_intents", "target": "payment_method_types"}
    payload = plan_payload(
        fixes=[{"file": "a.js", "sites": [{"line": 1, **site, "replacement": "x", "action": "rename"}]}],
        unfixable=[{"file": "b.js", "line": 2, **site, "why": "nested"},
                   {"file": "c.js", "line": 3, **site, "why": "@evil <img src=x> [x](http://evil.test)"}],
        not_attempted=[{"file": "e.js", "line": 5, "path": "/v1/setup_intents", "target": "payment_method_types", "why": "arguments_not_visible"}],
    )
    items = issue.findings(payload, "octo/shop", SHA)
    assert [(f["file"], f[issue.FIX_KEY]) for f in items] == [
        ("a.js", "planned"), ("b.js", "nested"), ("c.js", "manual"), ("d.js", "manual"), ("e.js", "manual"), ("f.js", ""),
    ]
    body = render(items)
    assert "evil" not in body and "<img" not in body
    assert body.count("not rewritten by apidrift") == 1
    # a plan that names a change the migration table does not know changes nothing
    stray = plan_payload(fixes=[{"file": "f.js", "sites": [{"line": 6, "path": "/v1/charges", "target": "description"}]}])
    assert [f[issue.FIX_KEY] for f in issue.findings(stray, "octo/shop", SHA)][-1] == ""
    # and a malformed plan is ignored
    broken = plan_payload(fixes="nope", unfixable=[3, {"file": "a.js", "line": True, **site, "why": "nested"}])
    assert {f[issue.FIX_KEY] for f in issue.findings(broken, "octo/shop", SHA)} == {"manual", ""}


def test_option_text_never_comes_from_the_payload():
    hostile = "x](http://evil.test) @octocat #1 <b>`"
    items = [finding(change_target=hostile, reason=hostile, call=hostile, **{issue.FIX_KEY: hostile, "options": [hostile]})]
    body = render(items)
    assert options_of(body) == options_of(render([finding()]))  # the same fixed sentences as a harmless finding
    assert "not rewritten by apidrift" not in body
    lines = [ln for ln in body.splitlines() if "evil" in ln]
    assert len(lines) == 2 and lines[0].startswith("**x\\]\\(http\\://evil.test\\)") and " via ``" in lines[1]


def test_marked_escapes_prose_and_puts_backtick_runs_in_code_spans():
    assert issue._marked("set `a[b]` (now): [x](y) <b> *z*") == "set `a[b]` \\(now\\)\\: \\[x\\]\\(y\\) \\<b> \\*z\\*"
    assert issue._option(guidance.Option("Do it.", "https://evil.test/x")) == "Do it."  # only Stripe's docs are linked
    assert issue._option(guidance.Option("Do it.", "https://docs.stripe.com/a b")) == "Do it."
    assert issue._option(guidance.Option("Do it.", "https://docs.stripe.com/a")) == "Do it. ([Stripe docs](https://docs.stripe.com/a))"


def groups(count: int, **overrides) -> list[dict]:
    return [finding(change_target=f"field_{n}", line=n + 1, **overrides) for n in range(count)]


def test_option_lists_fold_once_more_than_three_groups_carry_them():
    inline = render(groups(issue.MAX_INLINE_OPTION_GROUPS))
    assert inline.count("\nYour options:\n") == 3 and "<details>" not in inline
    folded = render(groups(issue.MAX_INLINE_OPTION_GROUPS + 1))
    assert "\nYour options:\n" not in folded
    assert folded.count("\n\n<details><summary>Your options (2)</summary>\n\n- Remove the field") == 4
    assert folded.count(PIN_LINE + "\n\n</details>\n") == 4 and folded.count(NOTE_START) == 1
    covered = [pmt(**{issue.FIX_KEY: issue.PLANNED})] + groups(3)  # a covered group does not count
    assert "<details>" not in render(covered)


def test_a_folded_site_list_closes_before_its_options():
    body = render([finding(line=n) for n in range(1, 13)])
    assert "#L12 via `stripe.Charge.create`\n\n</details>\n\nYour options:\n- Remove the field" in body


def test_the_body_stays_under_the_cap_with_options_on_every_group():
    many = render(groups(60))
    assert many.count("<details><summary>Your options (2)</summary>") == issue.MAX_FINDINGS and "\nand 10 more.\n" in many
    assert len(many) < issue.MAX_BODY_CHARS and many.count(NOTE_START) == 1
    assert issue.marker_in(many) == FP and many.rstrip().endswith(issue.marker(FP))
    long = render(groups(60, file="d/" + "f" * 1500 + ".py"))  # the character budget cuts before the finding cap does
    assert len(long) <= issue.MAX_BODY_CHARS and long.rstrip().endswith(issue.marker(FP))
    shown = long.count("**field_")
    assert 1 <= shown < issue.MAX_FINDINGS and long.count("</details>") == shown  # every list shown is closed
    assert f"\nand {60 - shown} more.\n\n{NOTE_START}" in long


def test_an_open_issue_gains_its_options_by_an_edit_in_place_without_a_comment(installed, conn, gh, settings):
    """An issue opened before options existed has another body hash: the next check edits it and says nothing."""
    assert run_job(claim(conn), conn, gh, settings) == "findings: opened #1"
    stored = db.get_repo(conn, 501)
    assert "Your options:" in installed.issues[1]["body"]
    installed.issues[1]["body"] = "the body as it was before options"
    db.update_repo(conn, 501, issue_body_sha="0" * 64)
    writes = len(installed.writes)
    db.enqueue(conn, "check", 501, "push")
    assert run_job(claim(conn), conn, gh, settings) == "findings: updated #1"
    assert installed.comments == [] and len(installed.issues) == 1
    assert [r.method for r in installed.writes[writes:]] == ["PATCH"]
    assert "Your options:" in installed.issues[1]["body"] and NOTE_START in installed.issues[1]["body"]
    after = db.get_repo(conn, 501)
    assert after["issue_body_sha"] == stored["issue_body_sha"] and json.loads(after["issue_items"]) == json.loads(stored["issue_items"])
    db.enqueue(conn, "check", 501, "push")
    assert run_job(claim(conn), conn, gh, settings) == "findings: #1 unchanged"
