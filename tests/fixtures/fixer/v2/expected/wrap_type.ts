import Stripe from 'stripe';

const stripe = new Stripe(process.env.STRIPE_SECRET_KEY as string);

export async function resetAnchor(id: string): Promise<Stripe.Subscription> {
  const params = {
    billing_cycle_anchor: { type: 'now' },
    proration_behavior: 'none' as const,
  };
  return stripe.subscriptions.update(id, params);
}

export async function preview(customer: string): Promise<Stripe.Invoice> {
  return stripe.invoices.createPreview({ customer, subscription_details: { billing_cycle_anchor: { type: 'timestamp', timestamp: 1767225600 } } });
}
