import Stripe from 'stripe';

const stripe = new Stripe(process.env.STRIPE_SECRET_KEY as string);

export async function removePayto(id: string): Promise<Stripe.PaymentMethod> {
  const params = {
    payto: { pay_id: 'jenny@example.com' },
    metadata: { source: 'typescript' },
  };
  await stripe.paymentMethods.update(id, params);
  return stripe.paymentMethods.update(id, { metadata: { a: 'b' }, payto: { pay_id: 'x@example.com' } });
}
