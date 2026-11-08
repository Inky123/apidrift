const stripe = require('stripe')('sk', { apiVersion: '2026-10-28.endive' });
const LEGACY_VERSION = '2024-04-10'
function pm(types) {
  return LEGACY_VERSION >= '2026-07-29' ? { allowed_payment_method_types: types } : { payment_method_types: types }
}
export function go(types) {
  return stripe.paymentIntents.create(pm(types))
}
