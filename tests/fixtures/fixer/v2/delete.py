import stripe


def inline(pm_id):
    stripe.PaymentMethod.modify(pm_id, payto={"pay_id": "jenny@example.com"}, metadata={"order": "1"})


def last_keyword(pm_id):
    stripe.PaymentMethod.modify(pm_id, metadata={"order": "2"}, payto={"pay_id": "a@example.com"})


def own_lines(pm_id):
    params = {
        "metadata": {"order": "3"},  # keep this comment
        "payto": {
            "pay_id": "jenny@example.com",
        },
    }
    stripe.PaymentMethod.modify(pm_id, **params)


def dict_call(pm_id):
    stripe.PaymentMethod.modify(pm_id, **dict(metadata={"order": "4"}, payto={"pay_id": "b@example.com"}))
