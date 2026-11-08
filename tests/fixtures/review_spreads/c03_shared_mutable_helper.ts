const stripe = require('stripe')('sk');
const DEFAULTS = { mode: 'payment' }
function defaults() { return DEFAULTS }
export function configure(types) { DEFAULTS.payment_method_types = types }
export function go() { return stripe.checkout.sessions.create({ ...defaults(), success_url: 'x' }) }
