const stripe = require('stripe')(process.env.STRIPE_SECRET_KEY);

async function inline(id) {
  await stripe.paymentMethods.update(id, { metadata: { order: '1' } });
}

async function lastEntry(id) {
  await stripe.paymentMethods.update(id, { metadata: { order: '2' } });
}

async function ownLines(id) {
  const params = {
    metadata: { order: '3' }, // keep this comment
    billing_details: { name: 'Jenny Rosen' },
  };
  await stripe.paymentMethods.update(id, params);
}

async function lastOwnLine(id) {
  await stripe.paymentMethods.update(id, {
    billing_details: { name: 'Jenny Rosen' } // and this one
  });
}

async function spread(id, extra) {
  await stripe.paymentMethods.update(id, { ...extra, metadata: {} });
}

module.exports = { inline, lastEntry, ownLines, lastOwnLine, spread };
