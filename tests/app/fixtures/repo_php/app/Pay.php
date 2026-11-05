<?php

namespace App;

use Stripe\Charge;

class Pay
{
    // a literal array at the call: the documented rename applies here
    public function charge(int $amount, string $note)
    {
        return Charge::create(['amount' => $amount, 'currency' => 'usd', 'description' => $note]);
    }

    public function lookup(string $id)
    {
        $charge = \Stripe\Charge::retrieve($id);

        return $charge->receipt_url;
    }
}
