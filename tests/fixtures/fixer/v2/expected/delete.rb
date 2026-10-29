require 'stripe'

def inline(id)
  Stripe::PaymentMethod.update(id, { metadata: { order: '1' } })
end

def keyword_args(id)
  Stripe::PaymentMethod.update(id, metadata: { order: '2' })
end

def own_lines(id)
  params = {
    :metadata => { order: '3' }, # keep this comment
  }
  Stripe::PaymentMethod.update(id, params)
end
