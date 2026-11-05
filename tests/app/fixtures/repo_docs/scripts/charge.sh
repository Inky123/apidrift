#!/bin/sh
# real traffic, not a stub: a literal URL in code counts as a call site whose fields are not visible
curl -X POST https://api.stripe.com/v1/charges -u "$STRIPE_SECRET_KEY:" -d amount="$1" -d currency=usd -d description="$2"
curl https://api.stripe.com/v1/charges/"$3"
