const stripe = require('stripe')('sk');
export function go(emitter) {
  const extra = { mode: 'payment', success_url: 'x' }
  emitter.on('x', () => stripe.checkout.sessions.create(extra))
  extra.payment_method_types = ['card']
}
