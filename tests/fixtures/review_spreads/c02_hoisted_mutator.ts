const stripe = require('stripe')('sk');
const base = { mode: 'payment' }
addTypes()
stripe.checkout.sessions.create({ ...base, success_url: 'x' })
function addTypes() { base.payment_method_types = ['card'] }
