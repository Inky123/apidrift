const stripe = require('stripe')('sk');
class P {
  build() { return this.base }
  go() { return stripe.checkout.sessions.create({ ...this.build(), success_url: 'x' }) }
}
