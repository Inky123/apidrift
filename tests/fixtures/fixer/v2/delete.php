<?php

use Stripe\PaymentMethod;

function inline(string $id)
{
    return \Stripe\PaymentMethod::update($id, ['payto' => ['pay_id' => 'jenny@example.com'], 'metadata' => ['order' => '1']]);
}

function last_inline($stripe, string $id)
{
    return $stripe->paymentMethods->update($id, ['metadata' => ['order' => '2'], 'payto' => ['pay_id' => 'a@example.com']]);
}

function own_lines(string $id)
{
    $params = [
        'metadata' => ['order' => '3'], // keep this comment
        'payto' => [
            'pay_id' => 'jenny@example.com',
        ],
    ];

    return PaymentMethod::update($id, $params);
}

function last_without_comma(\Stripe\StripeClient $client, string $id)
{
    return $client->paymentMethods->update($id, array(
        'billing_details' => array('name' => 'Jenny'),
        'payto' => array('pay_id' => 'jenny@example.com')
    ));
}
