"""Recorded cassettes whose requests send a changed field are listed for re-recording, never edited."""

from test_fixer import plan_for

from apidrift.fixcassettes import recorded

VCR = """---
http_interactions:
- request:
    method: post
    uri: https://api.stripe.com/v1/payment_intents
    body:
      encoding: UTF-8
      string: amount=1000&currency=usd&payment_method_types[0]=card
    headers:
      Content-Type:
      - application/x-www-form-urlencoded
  response:
    status:
      code: 200
    body:
      string: '{"id": "pi_1", "payment_method_types": ["card"]}'
- request:
    method: post
    uri: https://api.stripe.com/v1/payment_intents/pi_1/confirm
    body:
      string: payment_method_types%5B0%5D=card
- request:
    method: get
    uri: https://api.stripe.com/v1/payment_intents?payment_method_types[0]=card
    body:
      string: ''
- request:
    method: post
    uri: https://api.stripe.com/v1/charges
    body:
      string: amount=1&payment_method_types[0]=card
- request:
    method: post
    uri: https://api.stripe.com/v1/payment_intents
    body:
      string: amount=1&allowed_payment_method_types[0]=card
recorded_with: VCR 6.2.0
"""
VCRPY = """interactions:
- request:
    body: filters%5Bcountries%5D%5B0%5D=US&account_holder%5Btype%5D=customer
    headers: {}
    method: POST
    uri: https://api.stripe.com/v1/financial_connections/sessions
  response:
    body: {string: '{"filters": {"countries": ["US"]}}'}
version: 1
"""
CHANGES = [("post", "/v1/payment_intents", "payment_method_types"), ("post", "/v1/payment_intents/{intent}/confirm",
           "payment_method_types"), ("post", "/v1/financial_connections/sessions", "filters.countries")]


def test_request_bodies_with_the_old_key_on_a_changed_endpoint_are_listed(tmp_path):
    for rel, text in (("spec/cassettes/pay.yml", VCR), ("tests/cassettes/fc.yaml", VCRPY), ("config/app.yml", "uri: x\n")):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    (tmp_path / "spec/fixtures").mkdir(parents=True)
    (tmp_path / "spec/fixtures/notes.yml").write_text("text: payment_method_types[0]=card on /v1/payment_intents\n")
    # The create and the confirm (percent-encoded); not the GET query, the other endpoint, the new key or responses.
    assert recorded(tmp_path, CHANGES) == [("spec/cassettes/pay.yml", 8), ("spec/cassettes/pay.yml", 21),
                                           ("tests/cassettes/fc.yaml", 3)]
    assert recorded(tmp_path, []) == []


def test_the_fix_plan_carries_the_cassettes_and_leaves_them_alone(tmp_path):
    app = "Stripe::PaymentIntent.create({ payment_method_types: ['card'], amount: 1 })\n"
    plan = plan_for(tmp_path, {"app/pay.rb": app, "spec/cassettes/pay.yml": VCR})
    assert [f.file for f in plan.fixes] == ["app/pay.rb"] and plan.cassettes == [("spec/cassettes/pay.yml", 8)]
    assert plan.to_dict()["cassettes"] == [{"file": "spec/cassettes/pay.yml", "line": 8}]
    assert (tmp_path / "spec/cassettes/pay.yml").read_text() == VCR
