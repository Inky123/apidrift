"""Calls whose arguments are only partly visible, and reads of response fields."""

import stripe


def charge(amount, **extra):
    return stripe.Charge.create(amount=amount, currency="usd", **extra)


def charge_with_address(amount, city):
    return stripe.Charge.create(amount=amount, currency="usd", shipping={"address": {"city": city}})


def refund(charge_id, reason):
    return stripe.Refund.create(charge_id, amount=5, reason=reason)


def lookup(charge_id):
    found = stripe.Charge.retrieve(charge_id)
    return found.billing_details.email, found.receipt_url


def recent():
    return [c.status for c in stripe.Charge.list(limit=3, status="succeeded").data]
