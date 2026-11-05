const stripe = require('stripe')(process.env.STRIPE_SECRET_KEY);

// a spread hides the top-level fields of this call
async function charge(params) {
  const charge = await stripe.charges.create({ ...params, amount: 100 });
  return charge.receipt_url;
}

// shipping is sent, but its contents are not visible here
async function ship(order, shipping) {
  return stripe.charges.create({ amount: order.total, currency: 'usd', shipping });
}

// everything about this call is a variable
async function session(opts) {
  return stripe.checkout.sessions.create(opts);
}

async function capture(id) { const c = await stripe.charges.capture(id, { amount: 5 }); return c.status; }

function verify(req) {
  return stripe.webhooks.constructEvent(req.body, req.headers['stripe-signature'], process.env.WHSEC);
}

module.exports = { charge, ship, session, capture, verify };
