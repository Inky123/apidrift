class Pay
  def opts
    { payment_method_types: ["card"] }
  end

  def go
    attrs = { amount: 1, currency: "usd" }
    attrs.merge!(extra)
    ::Stripe::PaymentIntent.create(attrs.merge(opts))
  end
end
