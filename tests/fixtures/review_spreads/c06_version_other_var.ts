const stripe = require('stripe')('sk', { apiVersion: '2026-05-27.dahlia' });
const SINCE = '2026-07-29'
function pm(types, event) {
  return event.api_version >= SINCE ? { allowed_payment_method_types: types } : { payment_method_types: types }
}
export function go(types, event) {
  return stripe.paymentIntents.create({ amount: 1, currency: 'usd', ...pm(types, event) })
}
