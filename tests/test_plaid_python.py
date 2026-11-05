"""plaid-python calls: which client is one, which endpoint a method reaches, which request fields are sent."""

import textwrap

from apidrift import plaidpy
from apidrift.plaidscan import scan_text
from plaidspec import plaid_index

INDEX = plaid_index()
HEAD = textwrap.dedent("""\
    import plaid
    from plaid.api import plaid_api
    from plaid.model.link_token_create_request import LinkTokenCreateRequest
    from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
    from plaid.model.account_filter import AccountFilter
    from plaid.model.products import Products
    from plaid.model.country_code import CountryCode
    from plaid.model.transactions_sync_request import TransactionsSyncRequest
    from plaid.model.transactions_sync_request_options import TransactionsSyncRequestOptions
    from plaid.model.transactions_get_request import TransactionsGetRequest
    from plaid.model.accounts_get_request import AccountsGetRequest
    from plaid.model.payment_create_request import PaymentCreateRequest
    from plaid.model.payment_amount import PaymentAmount
    from plaid.model.payment_amount_currency import PaymentAmountCurrency
    client = plaid_api.PlaidApi(plaid.ApiClient(plaid.Configuration(host=plaid.Environment.Sandbox)))
""")
HEAD_LINES = HEAD.count("\n")


def extract(source, head=HEAD):
    return plaidpy.extract(head + textwrap.dedent(source), INDEX)


def one(source, head=HEAD):
    found, other = extract(source, head)
    assert len(found) == 1 and not other, (found, other)
    return found[0]


def test_model_keywords_are_the_fields_and_nested_models_nest():
    call = one("""
        response = client.link_token_create(LinkTokenCreateRequest(
            client_name="App", language="en", country_codes=[CountryCode("US")], products=[Products("auth")],
            user=LinkTokenCreateRequestUser(client_user_id=uid, address={"city": "Leeds"}),
            account_filters=[AccountFilter(type="depository"), {"subtypes": ["checking"]}],
        ))
    """)
    assert (call.resolved.method, call.resolved.path, call.call) == ("post", "/link/token/create", "client.link_token_create")
    assert call.fields == {
        "client_name", "language", "country_codes", "products", "user", "user.client_user_id", "user.address",
        "user.address.city", "account_filters", "account_filters[].type", "account_filters[].subtypes",
    }
    assert call.complete and call.hidden == frozenset() and call.line == HEAD_LINES + 2


def test_a_request_variable_is_followed_with_the_writes_made_into_it():
    call = one("""
        def sync(token, cursor):
            request = TransactionsSyncRequest(access_token=token)
            if cursor:
                request.cursor = cursor
            request["options"] = TransactionsSyncRequestOptions(days_requested=30)
            return client.transactions_sync(request)
    """)
    assert call.fields == {"access_token", "cursor", "options", "options.days_requested"} and call.complete


def test_every_binding_that_can_reach_the_call_is_read():
    call = one("""
        def accounts(token, ids):
            if ids:
                request = AccountsGetRequest(access_token=token, options={"account_ids": ids})
            else:
                request = AccountsGetRequest(access_token=token)
            return poll(lambda: client.accounts_get(request))
    """)
    assert call.fields == {"access_token", "options", "options.account_ids"} and call.complete


def test_positional_arguments_are_the_required_fields_when_the_count_agrees():
    found, _ = extract("""
        client.transactions_get(TransactionsGetRequest(token, start, end))
        client.transactions_get(TransactionsGetRequest(token, start, end_date=end))
        client.transactions_get(TransactionsGetRequest(token, start))
        client.transactions_get(TransactionsGetRequest(*dates))
        client.payment_initiation_payment_create(PaymentCreateRequest(
            recipient_id=r, reference="x", amount=PaymentAmount(PaymentAmountCurrency("GBP"), value=100.0)))
    """)
    assert [(sorted(c.fields), sorted(c.hidden)) for c in found] == [
        (["access_token", "end_date", "start_date"], []),
        (["access_token", "end_date", "start_date"], []),
        ([], [""]),
        ([], [""]),
        (["amount", "amount.currency", "amount.value", "recipient_id", "reference"], []),
    ]


def test_kwargs_parameters_and_values_built_elsewhere_hide_fields_where_they_can_add_them():
    found, _ = extract("""
        BASE = {"access_token": TOKEN}
        def a(request):
            return client.transactions_sync(request)
        def b(**params):
            return client.transactions_sync(TransactionsSyncRequest(**params))
        def c(extra):
            return client.transactions_sync(TransactionsSyncRequest(access_token=t, **extra))
        def d(user):
            return client.link_token_create(LinkTokenCreateRequest(client_name="A", user=LinkTokenCreateRequestUser(**user)))
        def e():
            return client.transactions_sync(build_request())
        def f(BASE):
            return client.transactions_sync(BASE)
        def g():
            return client.transactions_sync(TransactionsSyncRequest(**BASE, cursor=c))
        def h(options):
            return client.transactions_sync(TransactionsSyncRequest(access_token=t, options=options))
        def i():
            request = TransactionsSyncRequest(access_token=t)
            request.update(other)
            return client.transactions_sync(request)
        def j():
            for page in pages:
                client.transactions_sync(request)
                request = TransactionsSyncRequest(access_token=t, cursor=page)
        def k():
            return client.transactions_sync(Wrapper(access_token=t))
    """)
    assert [(sorted(c.fields), sorted(c.hidden)) for c in found] == [
        ([], [""]),
        ([], [""]),
        (["access_token"], [""]),
        (["client_name", "user"], ["user."]),
        ([], [""]),
        ([], [""]),  # the parameter shadows the module-level dict
        (["access_token", "cursor"], []),
        (["access_token", "options"], []),  # an object with nothing visible: impact calls that possible
        (["access_token"], [""]),
        ([], [""]),  # rebound later in the loop that holds the call
        ([], [""]),  # not a class of the plaid package
    ]


def test_dicts_and_the_named_keyword_are_read_like_models():
    found, _ = extract("""
        client.accounts_get({"access_token": t, "options": {"account_ids": ids}})
        client.accounts_get(accounts_get_request=AccountsGetRequest(access_token=t), _request_timeout=5)
        client.accounts_get(dict(access_token=t))
        client.accounts_get({**base, "access_token": t})
        client.accounts_get({key: t})
    """)
    assert [(sorted(c.fields), sorted(c.hidden)) for c in found] == [
        (["access_token", "options", "options.account_ids"], []), (["access_token"], []), (["access_token"], []),
        (["access_token"], [""]), ([], [""]),
    ]


def test_clients_are_known_by_construction_typing_factories_and_scope():
    found, other = extract("""
        from plaid.api.plaid_api import PlaidApi

        def make():
            return PlaidApi(api_client)

        class Bank:
            def __init__(self, sdk: PlaidApi):
                self.api = plaid_api.PlaidApi(api_client)
                self.sdk = sdk
                self.http = requests.Session()

            def one(self):
                return self.api.accounts_get(AccountsGetRequest(access_token=a))

            def two(self, c: "PlaidApi"):
                self.http.accounts_get(1)
                return c.accounts_get(AccountsGetRequest(access_token=a))

        def three():
            c = make()
            c.accounts_get(AccountsGetRequest(access_token=a))
            make().accounts_get(AccountsGetRequest(access_token=a))
            c.close()

        def four():
            global shared
            shared = PlaidApi(api_client)

        def five():
            shared.accounts_get(AccountsGetRequest(access_token=a))

        def six():
            c = boto3.client("s3")
            c.accounts_get(1)
            c.put_object(Bucket=b)
    """, head="import plaid\nfrom plaid.api import plaid_api\nfrom plaid.model.accounts_get_request import AccountsGetRequest\n")
    assert [c.call for c in found] == [
        "self.api.accounts_get", "c.accounts_get", "c.accounts_get", "make.accounts_get", "shared.accounts_get",
    ]
    assert [call for _, call in other] == ["c.close"]


def test_a_client_from_another_module_is_read_when_it_or_its_module_is_named_after_plaid():
    found, other = extract("""
        from app.services.plaid import client as api
        from app import bank
        from .plaid_client import get_client
        import plaid
        api.accounts_get(request)
        plaid_client.accounts_get(request)
        self.plaid.accounts_get(request)
        settings.PLAID_CLIENT.accounts_get(request)
        bank.accounts_get(request)
        plaid.accounts_get(request)
        plaid_client.accountsGet(request)
        plaid_client.close()
    """, head="")
    assert [c.call for c in found] == [
        "api.accounts_get", "plaid_client.accounts_get", "self.plaid.accounts_get", "settings.PLAID_CLIENT.accounts_get",
    ]
    assert other == [] and all(c.hidden == {""} for c in found)


def test_the_pre_2021_client_resolves_by_its_path_like_names_with_fields_unseen():
    found, other = extract("""
        from plaid import Client
        legacy = Client(client_id=cid, secret=s, environment="sandbox")
        legacy.Transactions.get(token, start, end)
        legacy.Item.public_token.exchange(public_token)
        legacy.Processor.stripeBankAccountTokenCreate(token, account)
    """, head="")
    assert [(c.resolved.path, sorted(c.fields), c.complete) for c in found] == [
        ("/transactions/get", [], False), ("/item/public_token/exchange", [], False),
    ]
    assert [call for _, call in other] == ["legacy.Processor.stripeBankAccountTokenCreate"]


def test_scan_text_makes_python_usages_and_survives_source_that_does_not_parse():
    usages, other, unread = scan_text(HEAD + "r = client.transactions_sync(TransactionsSyncRequest(access_token=t))\nprint(r.next_cursor)\n", "bank/sync.py", INDEX)
    assert [(u.language, u.label, sorted(u.fields), u.complete, u.hidden) for u in usages] == [
        ("python", "POST /transactions/sync", ["access_token"], True, frozenset())
    ]
    assert "next_cursor" in usages[0].reads and not other and unread is None
    assert scan_text("def broken(:\n    plaid_client.accounts_get(x)\n", "x.py", INDEX) == ([], [], None)
