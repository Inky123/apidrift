require 'stripe'

def literal
  Stripe::FinancialConnections::Session.create(
    account_holder: { type: 'customer', customer: 'cus_123' },
    permissions: ['balances'],
    filters: { country: 'US' },
  )
end

def from_params(params)
  Stripe::FinancialConnections::Session.create({
    account_holder: { type: 'customer', customer: 'cus_123' },
    permissions: ['ownership'],
    filters: { 'country' => (params[:countries])[0] },
  })
end
