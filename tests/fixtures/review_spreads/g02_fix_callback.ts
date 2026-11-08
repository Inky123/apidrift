import Stripe from 'stripe'
const stripe = new Stripe('sk')
export function go(emitter, opts) {
  const extra = { description: 'x' }
  emitter.on('pay', () => stripe.paymentIntents.create({ amount: 1, currency: 'usd', payment_method_types: ['card'], ...extra }))
  Object.assign(extra, opts)
}
