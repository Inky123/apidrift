const stripe = require('stripe')('sk');
async function run(types: string[]) {
  const base = { mode: 'payment', success_url: 'x' }
  for (const t of types) {
    await stripe.checkout.sessions.create(base)
    base.payment_method_types = [t]
  }
}
