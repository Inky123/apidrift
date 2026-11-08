const stripe = require('stripe')('sk');
function pm(types, apiVersion) {
  return apiVersion >= '2026-07-29' ? { allowed_payment_method_types: types } : { payment_method_types: types }
}
export async function go(data, v, list) {
  const { payment_method_types, ...rest } = data
  for (const x of list) {
    await stripe.paymentIntents.create({ amount: 1, currency: 'usd', ...rest, ...pm(payment_method_types, v) })
    rest.payment_method_types = x
  }
}
