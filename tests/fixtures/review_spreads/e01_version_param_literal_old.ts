const stripe = require('stripe')('sk');
function pm(types, apiVersion) {
  return apiVersion >= '2026-07-29' ? { allowed_payment_method_types: types } : { payment_method_types: types }
}
export function go(types) {
  return stripe.paymentIntents.create({ amount: 1, currency: 'usd', ...pm(types, '2024-04-10') })
}
