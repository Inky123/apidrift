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


def test_a_request_built_key_by_key_is_read_with_the_objects_written_into_it():
    call = one("""
        async function sync(token, cursor, days) {
          const request = { access_token: token };
          if (cursor) request.cursor = cursor;
          if (days) { request['options'] = { days_requested: days }; }
          Object.assign(request, { count: 100 });
          delete request.count;
          return client.transactionsSync(request);
        }
    """)
    assert call.fields.paths == {"access_token", "cursor", "options", "options.days_requested", "count"}
    assert call.fields.complete and call.fields.hidden == set()


def test_a_request_variable_stays_open_when_keys_or_writers_are_not_visible():
    found, _ = extract("""
        const shared = { access_token: t };
        function prepare() { shared.cursor = load(); }
        async function a(key, v) { const r = { access_token: t }; r[key] = v; return client.transactionsSync(r); }
        async function b(more) { const r = { access_token: t }; Object.assign(r, more); return client.transactionsSync(r); }
        async function c() { const r = { access_token: t }; decorate(r); return client.transactionsSync(r); }
        async function d() { const r = { access_token: t }; r.options.days_requested = 3; return client.transactionsSync(r); }
        async function e() { return client.transactionsSync(shared); }
        async function f() { const r = {}; return client.transactionsSync(r); }
    """)
    assert [(sorted(c.fields.paths), sorted(c.fields.hidden)) for c in found] == [
        (["access_token"], [""]),
        (["access_token"], [""]),
        (["access_token"], [""]),
        (["access_token", "options"], ["options."]),
        (["access_token"], [""]),  # another function writes into the module-level object
        ([], []),
    ]


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


def test_a_projects_own_wrapper_is_not_the_client_even_when_it_borrows_the_sdk_names():
    # midday-ai/midday: its own ``PlaidApi`` class wraps the SDK's and shares method names with it
    wrapper = """
        import { PlaidApi as PlaidBaseApi } from 'plaid';
        export class PlaidApi {
          #client: PlaidBaseApi;
          constructor() { this.#client = new PlaidBaseApi(config); }
          async linkTokenCreate({ userId, language }) {
            return this.#client.linkTokenCreate({ user: { client_user_id: userId }, client_name: 'A', language });
          }
          async getAccounts(id) { return this.institutionsGetById(id); }
        }
    """
    found, other = extract(wrapper, head="")
    assert [(c.call, c.line) for c in found] == [("client.linkTokenCreate", 7)] and other == []

    caller = """
        import { PlaidApi } from '@acme/banking/plaid';
        import { GoCardLessApi } from '@acme/banking/gocardless';
        export async function link(userId) {
          const api = new PlaidApi();
          return api.linkTokenCreate({ userId, language: 'en' });
        }
        export async function accounts(id) {
          const api = new GoCardLessApi();
          return api.getAccounts(id);
        }
    """
    assert extract(caller, head="") == ([], [])


def test_a_receiver_known_by_name_alone_is_dropped_when_its_argument_cannot_be_a_request():
    found, _ = extract("""
        await plaidService.accountsGet({ userId, access_token: t });
        await plaidService.accountsGet('user_1');
        await plaidService.accountsGet(`user_${id}`);
        await plaidService.accountsGet({ access_token: t });
        await plaidService.accountsGet(user.id);
        await client.accountsGet({ userId });
    """)
    assert [(c.line, c.call, sorted(c.fields.paths), c.fields.complete) for c in found] == [
        (7, "plaidService.accountsGet", ["access_token"], True),
        (8, "plaidService.accountsGet", [], False),  # cannot be told from a request held in an object: kept, fields unseen
        (9, "client.accountsGet", ["userId"], True),  # a client this file builds is believed
    ]


def test_a_known_variable_is_a_client_only_where_its_own_assignment_makes_it_one():
    found, other = extract("""
        async function a() { const api = new PlaidApi(config); return api.accountsGet({ access_token: t }); }
        async function b() { const api = new OtherBank(config); api.accountsGet({ access_token: t }); return api.getAccounts(); }
        async function c(api) { return api.accountsGet({ access_token: t }); }
    """, head="import { PlaidApi } from 'plaid';\n")
    assert [c.line for c in found] == [3, 5] and other == []  # an untyped parameter of that name may be handed the client


def test_the_pre_2021_client_resolves_by_its_own_method_table_with_fields_unseen():
    found, other = extract("""
        const plaid = require('plaid');
        const client = new plaid.Client({ clientID: id, secret, env: plaid.environments.sandbox });
        client.getTransactions(token, start, end, { count: 250 }, cb);
        const res = await client.exchangePublicToken(publicToken);
        client.getAllTransactions(token, start, end);
        client.getCategories(cb);
        client.somethingElse();
        plaidClient.getTransactions(token, start, end);
    """, head="")
    assert [(c.line, c.call, c.resolved.path, sorted(c.fields.paths), sorted(c.fields.hidden)) for c in found] == [
        (4, "client.getTransactions", "/transactions/get", [], [""]),
        (5, "client.exchangePublicToken", "/item/public_token/exchange", [], [""]),
        (6, "client.getAllTransactions", "/transactions/get", [], [""]),
    ]
    # an endpoint the specs do not have, a method the client never had; a client known by name alone is not asked
    assert other == [(7, "client.getCategories"), (8, "client.somethingElse")]


def test_the_legacy_table_names_only_paths_and_every_one_is_a_post_in_the_real_shape():
    from apidrift.plaidlegacy import NODE_METHODS

    assert len(NODE_METHODS) == 52 and all(path.startswith("/") and path == path.lower() for path in NODE_METHODS.values())
    assert NODE_METHODS["createStripeToken"] == NODE_METHODS["createProcessorToken"] == "/processor/token/create"


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
