import Stripe from 'stripe'
import { addTypes } from './types'
const stripe = new Stripe('sk')
export async function go(list: string[]) {
  const extra = { description: 'x' }
  for (const x of list) {
    await stripe.paymentIntents.create({ amount: 1, currency: 'usd', payment_method_types: ['card'], ...extra })
    addTypes(extra, x)
  }
}
