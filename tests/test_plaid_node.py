"""plaid-node calls: which client is one, which endpoint a method reaches, which request fields are sent."""

import textwrap

from apidrift import plaidjs
from apidrift.plaidscan import scan_text
from plaidspec import plaid_doc, plaid_index

INDEX = plaid_index()
HEAD = "import { Configuration, PlaidApi, PlaidEnvironments } from 'plaid';\nconst client = new PlaidApi(new Configuration({}));\n"


def extract(source, head=HEAD):
    found, other = plaidjs.extract(head + textwrap.dedent(source), INDEX)
    return found, other


def one(source, head=HEAD):
    found, other = extract(source, head)
    assert len(found) == 1 and not other, (found, other)
    return found[0]


def test_the_method_name_is_the_operation_id_and_the_literal_keys_are_the_fields():
    call = one("""
        const res = await client.linkTokenCreate({
          user: { client_user_id: id, address: { city: 'Leeds' } },
          client_name: 'App',
          products: ['auth'],
          account_filters: [{ type: 'depository' }],
        });
    """)
    assert (call.resolved.method, call.resolved.path, call.call) == ("post", "/link/token/create", "client.linkTokenCreate")
    assert call.fields.paths == {
        "user", "user.client_user_id", "user.address", "user.address.city", "client_name", "products",
        "account_filters", "account_filters[].type",
    }
    assert call.fields.complete and call.line == 4


def test_index_resolves_node_python_and_legacy_python_names_and_nothing_else():
    assert INDEX.method("transactionsSync").path == "/transactions/sync"
    assert INDEX.method("transactions_sync").path == "/transactions/sync"
    assert INDEX.legacy(["Item", "public_token", "exchange"]).path == "/item/public_token/exchange"
    assert INDEX.method("create") is None and INDEX.method("sync") is None and INDEX.legacy(["get"]) is None
    assert not INDEX.empty and plaid_index({"openapi": "3.0.0", "paths": {}}).empty


def test_a_method_two_specs_place_differently_keeps_the_first_specs_endpoint():
    moved = plaid_doc()
    moved["paths"]["/accounts/list"] = moved["paths"].pop("/accounts/get")
    assert plaid_index(plaid_doc(), moved).method("accountsGet").path == "/accounts/get"
    assert plaid_index(moved, plaid_doc()).method("accountsGet").path == "/accounts/list"


def test_the_second_argument_is_http_options_and_is_not_read():
    call = one("await client.accountsGet({ access_token: t }, { timeout: 500, headers: { 'X-Trace': id } });")
    assert call.fields.paths == {"access_token"} and call.fields.complete


def test_a_request_variable_is_followed_to_its_literal_and_writes_into_it_leave_it_incomplete():
    call = one("""
        async function sync(token, cursor) {
          const request = { access_token: token };
          if (cursor) request.cursor = cursor;
          return client.transactionsSync(request);
        }
    """)
    assert call.fields.paths == {"access_token", "cursor"}
    assert not call.fields.complete and call.fields.hidden == {""}


def test_a_typed_literal_binding_and_a_cast_are_read():
    found, _ = extract("""
        const request: TransactionsSyncRequest = { access_token: token, options: { days_requested: 30 } };
        await client.transactionsSync(request);
        await client.accountsGet({ access_token: token } as AccountsGetRequest);
        await client.accountsGet(req as AccountsGetRequest);
    """)
    assert [(c.fields.paths, c.fields.complete) for c in found] == [
        ({"access_token", "options", "options.days_requested"}, True), ({"access_token"}, True), (set(), False),
    ]


def test_spreads_parameters_and_values_built_elsewhere_hide_fields_where_they_can_add_them():
    found, _ = extract("""
        const base = { access_token: t };
        async function a(request) { return client.transactionsSync(request); }
        async function b(extra) { return client.transactionsSync({ ...extra, cursor: c }); }
        async function c2(user) { return client.linkTokenCreate({ client_name: 'A', user: { ...user, legal_name: n } }); }
        async function d() { return client.transactionsSync(buildRequest()); }
        async function e(base) { return client.transactionsSync(base); }
    """)
    assert [(sorted(c.fields.paths), sorted(c.fields.hidden)) for c in found] == [
        ([], [""]),
        (["cursor"], [""]),
        (["client_name", "user", "user.legal_name"], ["user."]),
        ([], [""]),
        ([], [""]),  # the parameter shadows the module-level literal
    ]


def test_a_spread_of_literal_branches_is_read_and_adds_nothing_unseen():
    call = one("await client.transactionsSync({ access_token: t, ...(cursor ? { cursor } : {}) });")
    assert call.fields.paths == {"access_token", "cursor"} and call.fields.complete


def test_clients_are_known_by_construction_typing_factories_and_aliases():
    found, other = extract("""
        const plaid = require('plaid');
        class Bank {
          private api: PlaidApi;
          constructor(private readonly sdk: PlaidApi) { this.inner = new plaid.PlaidApi(config); }
          one() { return this.inner.accountsGet({ access_token: a }); }
          two() { return this.sdk.accountsGet({ access_token: a }); }
          three() { return this.api.accountsGet({ access_token: a }); }
        }
        function make() { return new PlaidApi(config); }
        const build = () => new PlaidApi(config);
        const made = make();
        const built = build();
        const again = made;
        made.accountsGet({ access_token: a });
        built.accountsGet({ access_token: a });
        again.accountsGet({ access_token: a });
        make().accountsGet({ access_token: a });
        made.setConfiguration(x);
    """, head="")
    assert [c.call for c in found] == [
        "this.inner.accountsGet", "this.sdk.accountsGet", "this.api.accountsGet", "made.accountsGet",
        "built.accountsGet", "again.accountsGet", "make().accountsGet",
    ]
    assert other == [(19, "made.setConfiguration")]


def test_a_client_from_another_module_is_read_when_it_or_its_module_is_named_after_plaid():
    found, other = extract("""
        import { plaidClient } from '@/lib/plaid';
        import { client as api } from '../services/plaid';
        import bank from './bank';
        const wrapper = require('../plaid');
        await plaidClient.accountsGet({ access_token: a });
        await api.accountsGet({ access_token: a });
        await wrapper.transactionsSync(request);
        await this.plaid.accountsGet({ access_token: a });
        await ctx.services.plaidApi.accountsGet({ access_token: a });
        await bank.accountsGet({ access_token: a });
        await plaidClient.somethingElse();
    """, head="")
    assert [c.call for c in found] == [
        "plaidClient.accountsGet", "api.accountsGet", "wrapper.transactionsSync", "this.plaid.accountsGet",
        "ctx.services.plaidApi.accountsGet",
    ]
    assert other == []  # only a client this file builds or types has its other methods listed


def test_code_that_is_not_a_plaid_client_is_not_matched_even_with_the_same_method_names():
    found, other = extract("""
        import { usePlaidLink } from 'react-plaid-link';
        import { linkTokenCreate } from 'react-plaid-link';
        const { open, ready } = usePlaidLink({ token, onSuccess });
        const handler = Plaid.create({ token, onSuccess });
        await db.accountsGet({ id });
        await this.store.transactionsSync(userId);
        await linkTokenCreate.accountsGet({ id });
        class PlaidService { async accountsGet(userId) { return this.repo.accountsGet(userId); } }
        // await plaidClient.accountsGet({ access_token: a });
        const s = "plaidClient.accountsGet({ access_token: a })";
        await plaidClient.accounts_get({ access_token: a });
    """, head="")
    assert found == [] and other == []


def test_the_pre_2021_client_is_recognised_and_its_methods_are_listed_as_not_matched():
    found, other = extract("""
        const plaid = require('plaid');
        const client = new plaid.Client({ clientID: id, secret, env: plaid.environments.sandbox });
        client.getTransactions(token, start, end, { count: 250 }, cb);
        const res = await client.exchangePublicToken(publicToken);
    """, head="")
    assert found == [] and other == [(4, "client.getTransactions"), (5, "client.exchangePublicToken")]


def test_scan_text_tags_language_reads_urls_and_names_unread_languages():
    usages, other, unread = scan_text(
        HEAD + "const r = await client.transactionsSync({ access_token: t });\nconsole.log(r.data.next_cursor, r.data.added);\n",
        "src/sync.ts", INDEX,
    )
    assert [(u.language, u.label, u.call, sorted(u.fields), u.complete) for u in usages] == [
        ("typescript", "POST /transactions/sync", "client.transactionsSync", ["access_token"], True)
    ]
    assert {"next_cursor", "added"} <= usages[0].reads and not other and unread is None

    raw = "await fetch(`https://${env}.plaid.com/transactions/sync`, { method: 'POST', body });\n// see https://plaid.com/docs/api/products/transactions/\n"
    usages, _, _ = scan_text(raw, "lib/raw.js", INDEX)
    assert [(u.language, u.label, u.complete, u.line) for u in usages] == [("http", "POST /transactions/sync", False, 1)]
    usages, _, _ = scan_text("curl https://sandbox.plaid.com/accounts/get\n", "docs/api.md", INDEX)
    assert [(u.language, u.label) for u in usages] == [("doc", "POST /accounts/get")]

    assert scan_text("client = Plaid::PlaidApi.new(api_client)\n", "app/bank.rb", INDEX) == ([], [], "Ruby")
    assert scan_text("// Plaid Link\n", "ios/Link.swift", INDEX) == ([], [], None)
