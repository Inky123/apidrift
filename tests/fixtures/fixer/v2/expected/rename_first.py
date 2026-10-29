import stripe


def literal():
    return stripe.financial_connections.Session.create(
        account_holder={"type": "customer", "customer": "cus_123"},
        permissions=["balances"],
        filters={"country": "US"},
    )


def chosen(countries):
    return stripe.financial_connections.Session.create(
        account_holder={"type": "customer", "customer": "cus_123"},
        permissions=["ownership"],
        filters={"country": countries[0] if countries else None},
    )


def from_request(request):
    return stripe.financial_connections.Session.create(
        account_holder={"type": "customer", "customer": "cus_123"},
        permissions=["payment_method"],
        filters=dict(country=request.json["countries"][0] if request.json["countries"] else None),
    )
