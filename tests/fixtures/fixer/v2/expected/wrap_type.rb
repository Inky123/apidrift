require 'stripe'

def reset_anchor(id)
  Stripe::Subscription.update(id, billing_cycle_anchor: { type: 'now' }, proration_behavior: 'create_prorations')
end

def resume(id, anchor)
  Stripe::Subscription.resume(id, { :billing_cycle_anchor => { type: anchor } })
end

def preview(customer)
  Stripe::Invoice.create_preview(customer: customer, subscription_details: { billing_cycle_anchor: { type: 'timestamp', timestamp: 1767225600 } })
end
