<?php

function literal(\Stripe\StripeClient $stripe)
{
    return $stripe->financialConnections->sessions->create([
        'account_holder' => ['type' => 'customer', 'customer' => 'cus_123'],
        'permissions' => ['balances'],
        'filters' => ['countries' => ['US', 'CA']],
    ]);
}

function from_request($request)
{
    return \Stripe\FinancialConnections\Session::create([
        'account_holder' => ['type' => 'customer', 'customer' => 'cus_123'],
        'permissions' => ['ownership'],
        'filters' => [
            'countries' => $request->countries,
        ],
    ]);
}

function from_variable(\Stripe\StripeClient $stripe, array $countries)
{
    $params = [
        'account_holder' => ['type' => 'customer', 'customer' => 'cus_123'],
        'permissions' => ['balances'],
        'filters' => array("countries" => $countries),
    ];

    return $stripe->financialConnections->sessions->create($params);
}
