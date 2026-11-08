const stripe = require('stripe')('sk', { apiVersion: '2026-05-27.dahlia' });
function pm(types, event) {
  return event.api_version >= '2026-07-29' ? { allowed_payment_method_types: types } : { payment_method_types: types }
}
export function go(types, event) {
  return stripe.paymentIntents.create(pm(types, event))
}
