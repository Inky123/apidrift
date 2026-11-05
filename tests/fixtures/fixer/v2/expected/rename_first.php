<?php

function literal(\Stripe\StripeClient $stripe)
{
    return $stripe->financialConnections->sessions->create([
        'account_holder' => ['type' => 'customer', 'customer' => 'cus_123'],
        'permissions' => ['balances'],
        'filters' => ['country' => 'US'],
    ]);
}

function from_request($request)
{
    return \Stripe\FinancialConnections\Session::create([
        'account_holder' => ['type' => 'customer', 'customer' => 'cus_123'],
        'permissions' => ['ownership'],
        'filters' => [
            'country' => $request->countries[0] ?? null,
        ],
    ]);
}

function from_variable(\Stripe\StripeClient $stripe, array $countries)
{
    $params = [
        'account_holder' => ['type' => 'customer', 'customer' => 'cus_123'],
        'permissions' => ['balances'],
        'filters' => array("country" => $countries[0] ?? null),
    ];

    return $stripe->financialConnections->sessions->create($params);
}
