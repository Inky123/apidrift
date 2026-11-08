const stripe = require('stripe')('sk');
async function run(types: string[]) {
  const base = { mode: 'payment' }
  for (const t of types) {
    await stripe.checkout.sessions.create({ ...base, success_url: 'x' })
    base.payment_method_types = [t]
  }
}
