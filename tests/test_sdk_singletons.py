"""SDK names shared by a singleton and its id form, nested collections with a bare verb, and old names.

``Stripe::Account.retrieve(id)`` is ``GET /v1/accounts/{account}``; with no id it is ``GET /v1/account``.
The alias table used to hold both under ``Account.retrieve`` and the pick preferred the path whose first
segment is the class name (``account``): the singleton, for every call. Now the plain alias names the id
form and the singleton is asked for only when the call passes no id, in every language.
"""

import pytest
from sdkmore import more_doc

from apidrift.openapi import load_spec
from apidrift.scanner import scan_text
from apidrift.sdkmap import NO_ID, SdkIndex, without_id


@pytest.fixture(scope="module")
def index():
    return SdkIndex.build(load_spec(more_doc()))


def found(index, text, rel):
    usages, unresolved, _ = scan_text(text, rel, index)
    return [(u.line, u.label) for u in usages], [call for _, _, call in unresolved]


def test_the_plain_alias_names_the_id_form_and_no_id_names_the_singleton(index):
    assert index.resolve(["Stripe", "Account", "retrieve"]).label == "GET /v1/accounts/{account}"
    assert index.resolve(["stripe", "accounts", "retrieve"]).label == "GET /v1/accounts/{account}"
    assert index.resolve(["Account", "retrieve", NO_ID]).label == "GET /v1/account"
    assert index.resolve(["accounts", "retrieve", NO_ID]).label == "GET /v1/account"
    assert index.resolve(["client", "v1", "accounts", "retrieve_current"]).label == "GET /v1/account"
    assert index.resolve(["stripe", "accounts", "retrieveCurrent"]).label == "GET /v1/account"


def test_without_id_changes_only_a_resource_that_has_a_singleton(index):
    customer = index.resolve(["Stripe", "Customer", "retrieve"])
    assert without_id(index.resolve, customer) == customer  # no GET /v1/customer: stays the id form
    balance = index.resolve(["Stripe", "Balance", "retrieve"])
    assert without_id(index.resolve, balance) == balance
    single = without_id(index.resolve, index.resolve(["Stripe", "Account", "retrieve"]))
    assert single.label == "GET /v1/account" and single.key == "Account.retrieve"
    assert without_id(index.resolve, None) is None


def test_no_id_is_not_a_part_any_chain_reaches_by_itself(index):
    assert index.resolve(["retrieve", NO_ID]) is None
    assert index.resolve(["Customer", "retrieve", NO_ID]) is None
    assert index.resolve(["Account", "update", NO_ID]) is None  # no singleton update in this spec


@pytest.mark.parametrize(
    "rel,text,labels",
    [
        ("a.rb", "a = Stripe::Account.retrieve(id)\nb = Stripe::Account.retrieve\nc = Stripe::Account.retrieve()\n"
         "d = Stripe::Account.retrieve(nil)\ne = Stripe::Account.retrieve id\n",
         ["GET /v1/accounts/{account}", "GET /v1/account", "GET /v1/account", "GET /v1/account", "GET /v1/accounts/{account}"]),
        ("a.js", "const stripe = new Stripe(k);\nstripe.accounts.retrieve(id);\nstripe.accounts.retrieve();\n"
         "stripe.accounts.retrieve(null, { stripeAccount: id });\nstripe.accounts.retrieve({ stripeAccount: id });\n",
         ["GET /v1/accounts/{account}", "GET /v1/account", "GET /v1/account", "GET /v1/account"]),
        ("a.py", "import stripe\nstripe.Account.retrieve(acct)\nstripe.Account.retrieve()\nstripe.Account.retrieve(id=acct)\n"
         "stripe.Account.retrieve(stripe_account=acct)\nstripe.Account.retrieve(None)\nstripe.Account.retrieve(**kw)\n",
         ["GET /v1/accounts/{account}", "GET /v1/account", "GET /v1/accounts/{account}", "GET /v1/account",
          "GET /v1/account", "GET /v1/accounts/{account}"]),
        ("a.php", "<?php\n\\Stripe\\Account::retrieve($id);\n\\Stripe\\Account::retrieve();\n"
         "\\Stripe\\Account::retrieve(null, ['stripe_account' => $id]);\n$stripe = new \\Stripe\\StripeClient('k');\n"
         "$stripe->accounts->retrieve($id);\n$stripe->accounts->retrieve();\n",
         ["GET /v1/accounts/{account}", "GET /v1/account", "GET /v1/account", "GET /v1/accounts/{account}", "GET /v1/account"]),
    ],
)
def test_each_language_reaches_the_singleton_only_without_an_id(index, rel, text, labels):
    assert [label for _, label in found(index, text, rel)[0]] == labels


def test_a_nested_collection_verb_is_named_after_it_on_the_parent(index):
    assert index.resolve(["Stripe", "Account", "create_login_link"]).label == "POST /v1/accounts/{account}/login_links"
    assert index.resolve(["stripe", "accounts", "createLoginLink"]).label == "POST /v1/accounts/{account}/login_links"
    assert index.resolve(["client", "v1", "accounts", "login_links", "create"]).label == "POST /v1/accounts/{account}/login_links"
    # the bare verb on the parent is the parent's own create, and only that
    assert index._aliases["Account.create"] == {("post", "/v1/accounts")}


def test_a_parent_method_named_after_a_collection_does_not_take_the_bare_verb(index):
    assert index._aliases["Customer.list"] == {("get", "/v1/customers")}
    assert index.resolve(["Customer", "list_payment_methods"]).label == "GET /v1/customers/{customer}/payment_methods"
    assert index.resolve(["customers", "payment_methods", "list"]).label == "GET /v1/customers/{customer}/payment_methods"


def test_the_old_delete_names_of_a_delete_named_cancel(index):
    for chain in (["stripe", "subscriptions", "del"], ["Stripe", "Subscription", "delete"], ["stripe", "Subscription", "delete"]):
        assert index.resolve(chain).label == "DELETE /v1/subscriptions/{subscription_exposed_id}"
    assert index.resolve(["stripe", "subscriptions", "cancel"]).label == "DELETE /v1/subscriptions/{subscription_exposed_id}"
    # a POST action is never a delete, and a custom DELETE below the item is not the item's delete
    assert index.resolve(["PaymentIntent", "delete"]) is None


def test_old_names_are_found_in_code(index):
    text = "const stripe = new Stripe(k);\nawait stripe.subscriptions.del(id);\nawait stripe.accounts.createLoginLink(id);\n"
    labels, unresolved = found(index, text, "a.js")
    assert labels == [(2, "DELETE /v1/subscriptions/{subscription_exposed_id}"), (3, "POST /v1/accounts/{account}/login_links")]
    assert unresolved == []


def test_the_singleton_is_asked_for_only_by_the_full_alias_of_the_same_resource(index):
    fca = index.resolve(["Stripe", "FinancialConnections", "Account", "retrieve"])
    assert fca.label == "GET /v1/financial_connections/accounts/{account}"
    # Account.retrieve.(no id) is a shorter alias of the same chain: another resource's singleton
    assert without_id(index.resolve, fca) == fca
    text = (
        "a = Stripe::FinancialConnections::Account.retrieve(expand: [\"ownership\"])\n"
        "b = Stripe::FinancialConnections::Account.retrieve\n"
    )
    assert [label for _, label in found(index, text, "a.rb")[0]] == ["GET /v1/financial_connections/accounts/{account}"] * 2
    js = "const stripe = new Stripe(k);\nstripe.financialConnections.accounts.retrieve({ expand: ['x'] });\n"
    assert [label for _, label in found(index, js, "a.js")[0]] == ["GET /v1/financial_connections/accounts/{account}"]


def test_a_ruby_hash_or_keywords_holding_id_pass_the_id(index):
    text = (
        "a = Stripe::Account.retrieve(id: account_id)\n"
        "b = Stripe::Account.retrieve({id: account_id, expand: [\"x\"]})\n"
        "c = Stripe::Account.retrieve(expand: [\"x\"], id: account_id)\n"
        "d = Stripe::Account.retrieve id: account_id\n"
        "e = Stripe::Account.retrieve(stripe_account: account_id)\n"
        "f = Stripe::Account.retrieve({ :id => account_id })\n"
        "g = Stripe::Account.retrieve({ expand: [\"x\"] })\n"
        "h = Stripe::Account.retrieve(customer: { id: 1 })\n"  # a nested id is not the id
    )
    assert [label for _, label in found(index, text, "a.rb")[0]] == [
        "GET /v1/accounts/{account}", "GET /v1/accounts/{account}", "GET /v1/accounts/{account}",
        "GET /v1/accounts/{account}", "GET /v1/account", "GET /v1/accounts/{account}", "GET /v1/account",
        "GET /v1/account",
    ]
