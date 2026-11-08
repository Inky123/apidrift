const stripe = require('stripe')('sk');
const SINCE = '2026-07-29'
function pm(types) {
  return '2024-04-10' >= SINCE ? { allowed_payment_method_types: types } : { payment_method_types: types }
}
export function go(types) {
  return stripe.paymentIntents.create(pm(types))
}
