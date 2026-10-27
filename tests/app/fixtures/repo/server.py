"""A tiny checkout server that creates Stripe charges."""

import os

import stripe

stripe.api_key = os.environ["STRIPE_SECRET_KEY"]


def charge(amount, order_id):
    return stripe.Charge.create(amount=amount, currency="usd", description=f"order {order_id}")


def lookup(charge_id):
    return stripe.Charge.retrieve(charge_id)
