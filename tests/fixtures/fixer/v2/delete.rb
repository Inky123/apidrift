require 'stripe'

def inline(id)
  Stripe::PaymentMethod.update(id, { payto: { pay_id: 'jenny@example.com' }, metadata: { order: '1' } })
end

def keyword_args(id)
  Stripe::PaymentMethod.update(id, metadata: { order: '2' }, payto: { pay_id: 'a@example.com' })
end

def own_lines(id)
  params = {
    :metadata => { order: '3' }, # keep this comment
    :payto => { pay_id: 'jenny@example.com' },
  }
  Stripe::PaymentMethod.update(id, params)
end
