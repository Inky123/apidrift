"""Go fixes: only the struct fields stripe-go really changed, through the same oracle as every other language."""

import json
from pathlib import Path

import pytest

from apidrift import gofix, migrations
from apidrift.app import fixcheck, prbody
from apidrift.fixvalue import leaf
from gospec import go, plan

STRUCTS = json.loads((Path(__file__).parent / "fixtures" / "stripe_go_structs.json").read_text())
V86, V87 = STRUCTS["v86.0.0"]["structs"], STRUCTS["v87.0.0"]["structs"]
PI = "paymentintent"
CARD = 'stripe.StringSlice([]string{"card"})'


def fixed(tmp_path, text: str, name: str = "pay.go") -> str:
    p = plan(tmp_path, {name: text})
    assert p.unfixable == [] and p.not_attempted == [] and len(p.fixes) == 1, p.to_dict()
    return p.fixes[0].content


def refused(tmp_path, text: str) -> list[str]:
    p = plan(tmp_path, {"pay.go": text})
    assert p.fixes == [], p.fixes[0].diff()
    return sorted({u.why for u in p.unfixable + p.not_attempted})


# --- rename ---------------------------------------------------------------------------


def test_a_rename_changes_the_struct_field_in_every_call_style_and_keeps_the_block_aligned(tmp_path):
    text = go(
        "func legacy() {\n"
        "\tparams := &stripe.PaymentIntentParams{\n"
        "\t\tAmount:             stripe.Int64(2000),\n"
        "\t\t// what the checkout page offers\n"
        f"\t\tPaymentMethodTypes: {CARD},\n"
        "\t\tCurrency:           stripe.String(\"usd\"),\n"
        "\n"
        "\t\tCustomer: stripe.String(\"cus_1\"),\n"
        "\t}\n"
        "\tpaymentintent.New(params)\n}\n\n"
        "func api(sc *client.API, id string) {\n"
        f"\tsc.PaymentIntents.Update(id, &stripe.PaymentIntentParams{{PaymentMethodTypes: {CARD}}})\n"
        f"\tsc.PaymentIntents.Confirm(id, &stripe.PaymentIntentConfirmParams{{PaymentMethodTypes: {CARD}}})\n}}\n\n"
        "func modern(ctx context.Context, sc *stripe.Client) {\n"
        "\tparams := &stripe.SetupIntentCreateParams{}\n"
        "\tfor _, t := range kinds {\n"
        "\t\tparams.PaymentMethodTypes = append(params.PaymentMethodTypes, stripe.String(t))\n"
        "\t}\n"
        "\tsc.V1SetupIntents.Create(ctx, params)\n"
        f"\tsc.V1CheckoutSessions.Create(ctx, &stripe.CheckoutSessionCreateParams{{PaymentMethodTypes: {CARD}}})\n}}\n",
        PI, "client",
    )
    want = (
        text.replace("PaymentMethodTypes", "AllowedPaymentMethodTypes")
        .replace("\t\tAmount:             ", "\t\tAmount:                    ")
        .replace("\t\tCurrency:           ", "\t\tCurrency:                  ")
    )
    p = plan(tmp_path, {"pay.go": text})
    assert p.unfixable == [] and p.fixes[0].content == want
    assert [(s.line, s.path, s.action) for s in p.fixes[0].sites] == [
        (20, "/v1/payment_intents", "rename"), (24, "/v1/payment_intents/{intent}", "rename"),
        (25, "/v1/payment_intents/{intent}/confirm", "rename"), (33, "/v1/setup_intents", "rename"),
        (34, "/v1/checkout/sessions", "rename"),
    ]
    assert set(p.to_dict()) == {"fixes", "unfixable", "pins", "migrations", "leftovers", "not_attempted", "possibly_remaining", "tests",
        "test_fixes", "cassettes"}
    assert set(p.to_dict()["fixes"][0]["sites"][0]) == {"line", "path", "target", "replacement", "action"}


def test_an_aliased_root_import_crlf_and_the_old_name_in_strings_and_comments(tmp_path):
    text = go(
        "// PaymentMethodTypes is documented in `PaymentMethodTypes`\n"
        "func f() {\n"
        "\tlog.Print(\"PaymentMethodTypes\", `PaymentMethodTypes: {`)\n"
        "\tparams := &stripeapi.PaymentIntentParams{}\n"
        "\tif cards {\n"
        f"\t\tparams.PaymentMethodTypes = {CARD.replace('stripe.', 'stripeapi.')}\n"
        "\t}\n"
        "\tpaymentintent.New(params)\n}\n", PI, root="stripeapi",
    ).replace("\n", "\r\n")
    out = fixed(tmp_path, text)
    assert out == text.replace("params.PaymentMethodTypes", "params.AllowedPaymentMethodTypes") and out.count("\r\n") == text.count("\r\n")


def test_params_shared_by_two_calls_are_renamed_once_for_both(tmp_path):
    text = go(
        "func f(id string) {\n"
        f"\tparams := &stripe.PaymentIntentParams{{PaymentMethodTypes: {CARD}}}\n"
        "\tpaymentintent.New(params)\n\tpaymentintent.Update(id, params)\n}\n", PI,
    )
    p = plan(tmp_path, {"pay.go": text})
    assert p.fixes[0].content == text.replace("PaymentMethodTypes", "AllowedPaymentMethodTypes") and len(p.fixes[0].sites) == 2


@pytest.mark.parametrize("body, why", [
    # the params come from somewhere this file does not show
    ("func f(p *stripe.PaymentIntentParams) {\n\tpaymentintent.New(p)\n}\n", ["arguments_not_visible"]),
    # built across an if/else: the field is visible, the whole value is not
    ("func f(a bool) {\n\tvar p *stripe.PaymentIntentParams\n\tif a {\n"
     f"\t\tp = &stripe.PaymentIntentParams{{PaymentMethodTypes: {CARD}}}\n"
     "\t} else {\n\t\tp = other()\n\t}\n\tpaymentintent.New(p)\n}\n", ["incomplete_binding"]),
    # the same field assigned again after the call: that statement is not this call's, and would not compile
    (f"func f() {{\n\tp := &stripe.PaymentIntentParams{{PaymentMethodTypes: {CARD}}}\n\tpaymentintent.New(p)\n"
     "\tp.PaymentMethodTypes = nil\n}\n", ["leftover_tokens"]),
    # the response field of the same name is read (it still exists; apidrift cannot tell the two apart)
    (f"func f() {{\n\tpi, _ := paymentintent.New(&stripe.PaymentIntentParams{{PaymentMethodTypes: {CARD}}})\n"
     "\tlog.Print(pi.PaymentMethodTypes)\n}\n", ["leftover_tokens"]),
    # the application's own struct has a field with that name
    ("func f(req Request) {\n\tp := &stripe.PaymentIntentParams{}\n"
     "\tp.PaymentMethodTypes = stripe.StringSlice(req.PaymentMethodTypes)\n\tpaymentintent.New(p)\n}\n", ["leftover_tokens"]),
    # passed to a helper before the call
    (f"func f() {{\n\tp := &stripe.PaymentIntentParams{{PaymentMethodTypes: {CARD}}}\n\tdefaults(p)\n\tpaymentintent.New(p)\n}}\n",
     ["incomplete_binding"]),
    # a struct stripe-go did not change for this endpoint
    (f"func f() {{\n\tpaymentintent.New(&stripe.PaymentIntentConfirmParams{{PaymentMethodTypes: {CARD}}})\n}}\n", ["sdk_no_rewrite"]),
])
def test_a_rename_that_is_not_provably_complete_is_refused_with_a_reason(tmp_path, body, why):
    assert refused(tmp_path, go(body, PI)) == why


# --- delete, rename_first, wrap_type: the refusals (the fixtures in fixtures/fixer/v2 cover what is fixed) -----


UPDATE = "func f(id string, payto *stripe.PaymentMethodPaytoParams, list []string, anchor *string, now bool) {\n\t%s\n}\n"


@pytest.mark.parametrize("call, why", [
    # the value names a variable: deleting its only use could leave it unused, which Go rejects
    ("paymentmethod.Update(id, &stripe.PaymentMethodParams{Payto: payto, Metadata: map[string]string{}})", ["unsupported_value"]),
    ("paymentmethod.Update(id, &stripe.PaymentMethodParams{Payto: &stripe.PaymentMethodPaytoParams{PayID: stripe.String(pay)},"
     " Metadata: map[string]string{}})", ["unsupported_value"]),
    ("paymentmethod.Update(id, &stripe.PaymentMethodParams{Payto: &stripe.PaymentMethodPaytoParams{PayID: stripe.String(\"a\")}})",
     ["would_empty_literal"]),
    # a list that is not written out: its first element may not exist
    ("session.New(&stripe.FinancialConnectionsSessionParams{Filters: &stripe.FinancialConnectionsSessionFiltersParams{"
     "Countries: stripe.StringSlice(list)}})", ["unsupported_value"]),
    ("session.New(&stripe.FinancialConnectionsSessionParams{Filters: &stripe.FinancialConnectionsSessionFiltersParams{"
     "Countries: []*string{}}})", ["unsupported_value"]),
    # stripe-go v87 still takes BillingCycleAnchorNow on updates: there is no new Go shape to write
    ("subscription.Update(id, &stripe.SubscriptionParams{BillingCycleAnchorNow: stripe.Bool(true)})", ["sdk_no_rewrite"]),
    ("subscription.Update(id, &stripe.SubscriptionParams{BillingCycleAnchor: stripe.Int64(1767225600)})", ["sdk_no_rewrite"]),
    # a pointer that may be nil, a flag that may be false
    ("subscription.Resume(id, &stripe.SubscriptionResumeParams{BillingCycleAnchor: anchor})", ["unsupported_value"]),
    ("invoice.CreatePreview(&stripe.InvoiceCreatePreviewParams{SubscriptionDetails: "
     "&stripe.InvoiceCreatePreviewSubscriptionDetailsParams{BillingCycleAnchorNow: stripe.Bool(now)}})", ["unsupported_value"]),
    ("invoice.CreatePreview(&stripe.InvoiceCreatePreviewParams{SubscriptionDetails: "
     "&stripe.InvoiceCreatePreviewSubscriptionDetailsParams{BillingCycleAnchor: stripe.String(\"now\")}})", ["unsupported_value"]),
])
def test_value_shape_fixes_outside_the_verified_forms_are_left_for_a_person(tmp_path, call, why):
    text = go(UPDATE % call, "paymentmethod", "financialconnections/session", "subscription", "invoice")
    assert refused(tmp_path, text) == why


def test_a_wrapped_field_that_is_read_elsewhere_is_refused_and_an_already_wrapped_one_is_clean(tmp_path):
    read = go(
        "func f(id string) {\n\tp := &stripe.SubscriptionResumeParams{BillingCycleAnchor: stripe.String(\"now\")}\n"
        "\tif p.BillingCycleAnchor != nil {\n\t\tlog.Print(\"anchored\")\n\t}\n\tsubscription.Resume(id, p)\n}\n", "subscription",
    )
    assert refused(tmp_path, read) == ["leftover_tokens"]
    done = go(
        "func f(id string) {\n\tsubscription.Resume(id, &stripe.SubscriptionResumeParams{\n"
        "\t\tBillingCycleAnchor: &stripe.SubscriptionResumeBillingCycleAnchorParams{Type: stripe.String(\"now\")},\n\t})\n}\n",
        "subscription",
    )
    p = plan(tmp_path / "done", {"pay.go": done})
    assert p.fixes == [] and p.unfixable == [] and p.not_attempted == []


def test_an_edited_file_must_still_tokenise_with_every_bracket_closed():
    ok = "package x\n\nfunc f() {\n\tg(T{A: 1})\n}\n"
    assert gofix.balanced(ok, ok.replace("A: 1", "B: 2"))
    assert not gofix.balanced(ok, ok.replace("A: 1})", "A: 1}"))
    assert not gofix.balanced(ok, ok.replace("func f", "func h"))  # a declaration changed: not an entry edit
    assert not gofix.balanced("package x\nfunc f() {\n", "package x\nfunc f() {\n")


# --- the table against stripe-go's source ------------------------------------------------------


def test_every_documented_migration_has_a_verified_go_rule_or_is_named_as_having_none():
    without = {(m.path, m.target) for m in migrations.MIGRATIONS} - set(gofix.GO_RULES)
    assert without == {("/v1/subscriptions/{subscription_exposed_id}", "billing_cycle_anchor")}
    for name in ("SubscriptionParams", "SubscriptionUpdateParams"):  # unchanged across the two releases: nothing to write
        assert V86[name] == V87[name] and V87[name]["BillingCycleAnchorNow"] == ["-", "*bool"]
    assert STRUCTS["v87.0.0"]["api_version"] == migrations.ENDIVE and gofix.STRIPE_GO in prbody.GO_NOTE


@pytest.mark.parametrize("key", sorted(gofix.GO_RULES), ids=lambda k: f"{k[0]}:{k[1]}")
def test_each_go_rule_matches_what_stripe_go_changed_between_v86_and_v87(key):
    rule, m = gofix.GO_RULES[key], migrations.for_site(*key)
    assert gofix.go_name(leaf(m.target)) == rule.old
    for struct in rule.structs:
        before, after = V86[struct][rule.old], V87[struct].get(rule.old)
        assert before[0] == leaf(m.target)
        if m.action == "rename":
            assert after is None and V87[struct][rule.new] == [leaf(m.replacement), before[1]]  # same Go type
            assert rule.new not in V86[struct] and gofix.go_name(leaf(m.replacement)) == rule.new
        elif m.action == "rename_first":
            assert after is None and before[1] == "[]*string" and V87[struct][rule.new] == [leaf(m.replacement), "*string"]
        elif m.action == "delete":
            assert (after is None) == (struct == "PaymentMethodUpdateParams")  # the shared create struct keeps it
        else:
            assert after == [leaf(m.target), "*" + rule.wrapper] and rule.wrapper not in V86
            assert V87[rule.wrapper]["Type"] == ["type", "*string"]
            assert before[1] == ("*int64" if m.timestamps else "*string")
            assert ("Timestamp" in V87[rule.wrapper]) == m.timestamps
            for flag, _ in rule.flags:
                assert V86[struct][flag] == ["-", "*bool"] and flag not in V87[struct]


# --- the app's side ---------------------------------------------------------------------------


def test_the_pull_request_says_what_a_go_fix_needs_and_the_parent_accepts_go_files(tmp_path):
    p = plan(tmp_path, {"svc/pay.go": go(f"func f() {{\n\tpaymentintent.New(&stripe.PaymentIntentParams{{PaymentMethodTypes: {CARD}}})\n}}\n", PI)})
    payload = p.to_dict()
    fix = payload["fixes"][0]
    assert fixcheck.reject(fix["file"], fix["blob_sha"], fix["content"]) is None and fixcheck.reject_sites(fix["sites"]) is None
    render = lambda fixes, **extra: prbody.render_pr(  # noqa: E731
        {**payload, **extra}, fixes, full_name="o/r", sha="a" * 40, version=migrations.ENDIVE, issue_number=1, fingerprint="f" * 64
    )[1]
    body = render(payload["fixes"])
    assert prbody.GO_NOTE in body and "Merge this together with your API-version and SDK upgrade" in body
    assert "stripe-go v87" in prbody.GO_NOTE and "gofmt" in prbody.GO_NOTE
    assert prbody.GO_NOTE not in render([{**fix, "file": "pay.py"}])
    delete = [{**fix, "sites": [{"line": 3, "path": "/v1/payment_methods/{payment_method}", "target": "payto", "replacement": "", "action": "delete"}]}]
    assert prbody.GO_NOTE not in render(delete)  # a removed field compiles against either stripe-go
    left = render(payload["fixes"], unfixable=[{"file": "a.go", "line": 2, "why": gofix.NO_REWRITE}])
    assert "stripe-go models this field its own way" in left


def test_go_is_listed_among_the_languages_apidrift_reads():
    from apidrift.app.issue import render_unread_issue
    from apidrift.scanner import LANGUAGES, UNREAD_LANGUAGES

    _, body = render_unread_issue("o/r", "a" * 40, {"unread": {"Rust": 1}, "call_sites": 0})
    assert "apidrift reads Python, JavaScript, TypeScript, Ruby, PHP and Go call sites" in body
    assert LANGUAGES[".go"] == "go" and ".go" not in UNREAD_LANGUAGES
