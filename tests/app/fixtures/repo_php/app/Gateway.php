<?php

namespace App;

class Gateway
{
    private $stripe;

    public function __construct(string $key)
    {
        $this->stripe = new \Stripe\StripeClient($key);
    }

    // nothing visible: the parameters arrive in a variable
    public function charge(array $params)
    {
        return $this->stripe->charges->create($params);
    }

    public function capture(string $id)
    {
        return $this->stripe->charges->capture($id, ['amount' => 5]);
    }

    // a raw call next to the SDK ones
    public function raw(string $key)
    {
        $ch = curl_init('https://api.stripe.com/v1/charges');
        curl_setopt($ch, CURLOPT_USERPWD, $key . ':');

        return curl_exec($ch);
    }
}
