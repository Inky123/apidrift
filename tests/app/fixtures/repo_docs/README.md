# shop

Create a charge by hand:

    curl https://api.stripe.com/v1/charges -u sk_test_x: -d amount=100 -d currency=usd -d description=hello

Capture it later with POST https://api.stripe.com/v1/charges/ch_123/capture and look it up at
https://api.stripe.com/v1/charges/ch_123 (see https://api.stripe.com/v1/not_an_endpoint for nothing).
