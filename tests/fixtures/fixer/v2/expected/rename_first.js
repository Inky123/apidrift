const stripe = require('stripe')(process.env.STRIPE_SECRET_KEY);

async function literal() {
  return stripe.financialConnections.sessions.create({
    account_holder: { type: 'customer', customer: 'cus_123' },
    permissions: ['balances'],
    filters: { country: 'US' },
  });
}

async function shorthand(countries) {
  return stripe.financialConnections.sessions.create({
    account_holder: { type: 'customer', customer: 'cus_123' },
    permissions: ['ownership'],
    filters: { country: countries[0] },
  });
}

async function fromRequest(req) {
  const params = {
    account_holder: { type: 'customer', customer: req.body.customer },
    permissions: ['payment_method'],
    filters: { country: req.body.countries[0] },
  };
  return stripe.financialConnections.sessions.create(params);
}

module.exports = { literal, shorthand, fromRequest };
