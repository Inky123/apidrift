import stripe


def literal():
    return stripe.financial_connections.Session.create(
        account_holder={"type": "customer", "customer": "cus_123"},
        permissions=["balances"],
        filters={"countries": ["US", "CA"]},
    )


def chosen(countries):
    return stripe.financial_connections.Session.create(
        account_holder={"type": "customer", "customer": "cus_123"},
        permissions=["ownership"],
        filters={"countries": countries},
    )


def from_request(request):
    return stripe.financial_connections.Session.create(
        account_holder={"type": "customer", "customer": "cus_123"},
        permissions=["payment_method"],
        filters=dict(countries=request.json["countries"]),
    )
