const nock = require('nock');

test('charges', async () => {
  nock('https://api.stripe.com').post('/v1/charges').reply(200, { id: 'ch_1' });
  const res = await fetch('https://api.stripe.com/v1/charges', { method: 'POST', body: 'amount=1&description=x' });
  expect(res.status).toBe(200);
  await fetch('https://api.stripe.com/v1/charges/ch_1/refunds', { method: 'POST' });
});
