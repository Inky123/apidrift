"""The documented rename applies here: the fix pull request rewrites this call."""

import stripe


def charge(amount, order_id):
    return stripe.Charge.create(amount=amount, currency="usd", description=f"order {order_id}")
