const stripe = require('stripe')('sk');
function pm(types, apiVersion) {
  return apiVersion >= '2026-07-29' ? { allowed_payment_method_types: types } : { payment_method_types: types }
}
export function go(data, v) {
  const { payment_method_types, ...rest } = data
  const again = { ...data }
  return stripe.paymentIntents.create({ amount: 1, currency: 'usd', ...rest, ...pm(payment_method_types, v), ...again })
}
