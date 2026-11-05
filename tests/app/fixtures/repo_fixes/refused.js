const stripe = require('stripe')(process.env.STRIPE_SECRET_KEY);

// the same word is used for something else in this file, so the fixer leaves the call for a person
const description = 'weekly box';

async function charge(amount) {
  return stripe.charges.create({ amount, currency: 'usd', description });
}

module.exports = { charge, description };
