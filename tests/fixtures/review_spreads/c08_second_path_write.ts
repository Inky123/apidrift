const stripe = require('stripe')('sk');
function pm(types, apiVersion) {
  return apiVersion >= '2026-07-29' ? { allowed_payment_method_types: types } : { payment_method_types: types }
}
export function go(types, v) {
  const p = { amount: 1, currency: 'usd', ...pm(types, v) }
  p.payment_method_types = types
  return stripe.paymentIntents.create(p)
}
