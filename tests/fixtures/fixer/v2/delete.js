const stripe = require('stripe')(process.env.STRIPE_SECRET_KEY);

async function inline(id) {
  await stripe.paymentMethods.update(id, { payto: { pay_id: 'jenny@example.com' }, metadata: { order: '1' } });
}

async function lastEntry(id) {
  await stripe.paymentMethods.update(id, { metadata: { order: '2' }, payto: { pay_id: 'a@example.com' } });
}

async function ownLines(id) {
  const params = {
    metadata: { order: '3' }, // keep this comment
    payto: {
      pay_id: 'jenny@example.com',
    },
    billing_details: { name: 'Jenny Rosen' },
  };
  await stripe.paymentMethods.update(id, params);
}

async function lastOwnLine(id) {
  await stripe.paymentMethods.update(id, {
    billing_details: { name: 'Jenny Rosen' }, // and this one
    payto: { pay_id: 'b@example.com' }
  });
}

async function spread(id, extra) {
  await stripe.paymentMethods.update(id, { ...extra, payto: { pay_id: 'c@example.com' }, metadata: {} });
}

module.exports = { inline, lastEntry, ownLines, lastOwnLine, spread };
