"""One SDK call, one raw HTTP call, and a file the scanner cannot read next to it."""

import requests
import stripe


def charge(amount, note):
    made = stripe.Charge.create(amount=amount, currency="usd", description=note)
    return made.receipt_url


def capture(charge_id, key):
    return requests.post(f"https://api.stripe.com/v1/charges/{charge_id}/capture", auth=(key, ""), data={"amount": 5})
