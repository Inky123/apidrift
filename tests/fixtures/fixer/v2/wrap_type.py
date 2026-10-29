import stripe


def reset_anchor(subscription_id):
    stripe.Subscription.modify(subscription_id, billing_cycle_anchor="now", proration_behavior="create_prorations")


def resume(subscription_id, anchor):
    params = {"billing_cycle_anchor": anchor}
    stripe.Subscription.resume(subscription_id, **params)


def preview(customer):
    return stripe.Invoice.create_preview(
        customer=customer,
        subscription_details={"billing_cycle_anchor": 1767225600},
    )
