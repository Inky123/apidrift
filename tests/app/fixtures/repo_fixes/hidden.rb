require 'stripe'

# nothing visible to rewrite: the arguments arrive in a variable
def charge(params)
  Stripe::Charge.create(params)
end
