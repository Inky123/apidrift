const stripe = require('stripe')('sk');
export function go(types) {
  const p = { amount: 1, currency: 'usd' }
  types.forEach(t => { p.payment_method_types = [t] })
  return stripe.paymentIntents.create(p)
}
