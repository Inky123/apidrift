const stripe = require('stripe')('sk');
function pm(types, apiVersion) {
  return apiVersion >= '2026-12-01' ? { allowed_payment_method_types: types } : { payment_method_types: types }
}
export function go(types, v) {
  return stripe.paymentIntents.create(pm(types, v))
}
