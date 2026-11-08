const stripe = require('stripe')('sk');
function pm(types, flag) {
  return flag ? { payment_method_types: types, amount: 1 } : { amount: 2 }
}
export function go(types, f) {
  return stripe.paymentIntents.create(pm(types, f))
}
