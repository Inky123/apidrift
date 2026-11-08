const stripe = require('stripe')('sk');
class P {
  base() { const b = { description: 'x' }; this.last = b; return b }
  later() { this.last.payment_method_types = ['card'] }
  go() { return stripe.paymentIntents.create({ amount: 1, currency: 'usd', ...this.base() }) }
}
