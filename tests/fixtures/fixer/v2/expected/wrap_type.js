const stripe = require('stripe')(process.env.STRIPE_SECRET_KEY);

async function resetAnchor(subscriptionId) {
  await stripe.subscriptions.update(subscriptionId, { billing_cycle_anchor: { type: 'now' }, proration_behavior: 'create_prorations' });
}

async function resume(subscriptionId, anchor) {
  await stripe.subscriptions.resume(subscriptionId, { billing_cycle_anchor: { type: anchor } });
}

async function preview(customer) {
  return stripe.invoices.createPreview({
    customer,
    subscription_details: { billing_cycle_anchor: { type: 'timestamp', timestamp: 1767225600 } },
  });
}

async function previewUnchanged(customer) {
  return stripe.invoices.createPreview({ customer, subscription_details: { billing_cycle_anchor: { type: "unchanged" } } });
}

module.exports = { resetAnchor, resume, preview, previewUnchanged };
