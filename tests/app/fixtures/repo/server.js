const express = require('express');
const stripe = require('stripe')(process.env.STRIPE_SECRET_KEY);
const app = express();

app.post('/charge', async (req, res) => {
  const charge = await stripe.charges.create({
    amount: req.body.amount,
    currency: 'usd',
    description: `order ${req.body.orderId}`,
  });
  res.json({ id: charge.id });
});

app.get('/charge/:id', async (req, res) => {
  res.json(await stripe.charges.retrieve(req.params.id));
});

app.listen(4242);
