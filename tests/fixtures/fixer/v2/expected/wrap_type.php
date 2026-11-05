<?php

class Billing
{
    public function __construct(private \Stripe\StripeClient $stripe)
    {
    }

    public function resetAnchor(string $id)
    {
        return $this->stripe->subscriptions->update($id, [
            'billing_cycle_anchor' => ['type' => 'now'],
            'proration_behavior' => 'create_prorations',
        ]);
    }

    public function resume(string $id, string $anchor)
    {
        return $this->stripe->subscriptions->resume($id, ["billing_cycle_anchor" => ["type" => $anchor]]);
    }

    public function keep(string $id, bool $reset)
    {
        return \Stripe\Subscription::update($id, ['billing_cycle_anchor' => ['type' => $reset ? 'now' : 'unchanged']]);
    }

    public function preview(string $customer)
    {
        return $this->stripe->invoices->createPreview([
            'customer' => $customer,
            'subscription_details' => ['billing_cycle_anchor' => ['type' => 'timestamp', 'timestamp' => 1767225600]],
        ]);
    }
}
