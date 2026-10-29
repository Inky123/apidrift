import Stripe from 'stripe';

const stripe = new Stripe(process.env.STRIPE_SECRET_KEY as string);

export async function session(customer: string, allowed: string[]): Promise<Stripe.FinancialConnections.Session> {
  return stripe.financialConnections.sessions.create({
    account_holder: { type: 'customer', customer },
    permissions: ['balances'],
    filters: { 'countries': allowed },
  });
}
