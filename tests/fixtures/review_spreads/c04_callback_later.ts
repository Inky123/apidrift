const stripe = require('stripe')('sk');
export function go(emitter) {
  const extra = { mode: 'payment' }
  emitter.on('x', () => stripe.checkout.sessions.create({ ...extra, success_url: 'x' }))
  extra.payment_method_types = ['card']
}
