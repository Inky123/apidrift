require 'stripe'

def charge(params)
  Stripe::Charge.create(params)
end

def describe(amount, note)
  Stripe::Charge.create(amount: amount, currency: 'usd', description: note)
end
