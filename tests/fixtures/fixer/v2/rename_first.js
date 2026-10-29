const stripe = require('stripe')(process.env.STRIPE_SECRET_KEY);

async function literal() {
  return stripe.financialConnections.sessions.create({
    account_holder: { type: 'customer', customer: 'cus_123' },
    permissions: ['balances'],
    filters: { countries: ['US', 'CA'] },
  });
}

async function shorthand(countries) {
  return stripe.financialConnections.sessions.create({
    account_holder: { type: 'customer', customer: 'cus_123' },
    permissions: ['ownership'],
    filters: { countries },
  });
}

async function fromRequest(req) {
  const params = {
    account_holder: { type: 'customer', customer: req.body.customer },
    permissions: ['payment_method'],
    filters: { countries: req.body.countries },
  };
  return stripe.financialConnections.sessions.create(params);
}

module.exports = { literal, shorthand, fromRequest };
