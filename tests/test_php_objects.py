"""The PHP reader on stripe-php objects and wrappers: calls on returned objects, ``save()``, nested collections,
StripeClient evidence, a vendored stripe-php under a renamed namespace, string-dispatch wrappers and
``Account::retrieve($id)``. Every rule has its hostile cases: what must NOT be read as a Stripe call.
"""

import pytest
from specs import _form, _op, sdk_doc

from apidrift.openapi import load_spec
from apidrift.scanner import scan_repo, scan_text
from apidrift.sdkmap import SdkIndex

H = "<?php\n"


def _doc():
    doc = sdk_doc()
    paths, schemas = doc["paths"], doc["components"]["schemas"]
    ok = {"200": {}}

    def add(path, method, props=None):
        op = {"responses": ok}
        if props is not None:
            op["requestBody"] = _form(props)
        paths.setdefault(path, {})[method] = op

    add("/v1/payment_intents", "post", {"amount": {"type": "integer"}})
    add("/v1/payment_intents/{intent}", "get")
    add("/v1/payment_intents/{intent}", "post", {"description": {"type": "string"}})
    add("/v1/payment_intents/{intent}/confirm", "post", {"payment_method_types": {"type": "array"}})
    add("/v1/payment_intents/search", "get")
    schemas["payment_intent"] = {"type": "object", "x-stripeResource": {"class_name": "PaymentIntent", "in_package": ""},
                                 "x-stripeOperations": [
                                     _op("create", "create", "post", "/v1/payment_intents"),
                                     _op("retrieve", "retrieve", "get", "/v1/payment_intents/{intent}"),
                                     _op("update", "update", "post", "/v1/payment_intents/{intent}"),
                                     _op("confirm", "custom", "post", "/v1/payment_intents/{intent}/confirm"),
                                     _op("search", "custom", "get", "/v1/payment_intents/search")]}
    sub = "/v1/subscriptions/{subscription_exposed_id}"
    add(sub, "get")
    add(sub, "post", {"default_payment_method": {"type": "string"}, "metadata": {"type": "object"}})
    add(sub, "delete")
    schemas["subscription"] = {"type": "object", "x-stripeResource": {"class_name": "Subscription", "in_package": ""},
                               "x-stripeOperations": [
                                   _op("retrieve", "retrieve", "get", sub), _op("update", "update", "post", sub),
                                   _op("cancel", "custom", "delete", sub)]}
    add("/v1/customers", "post", {"email": {"type": "string"}})
    add("/v1/customers/{customer}", "get")
    add("/v1/customers/{customer}", "delete")
    add("/v1/customers/{customer}/sources", "post", {"source": {"type": "string"}})
    add("/v1/customers/{customer}/sources", "get")
    schemas["customer"] = {"type": "object", "x-stripeResource": {"class_name": "Customer", "in_package": ""},
                           "x-stripeOperations": [
                               _op("create", "create", "post", "/v1/customers"),
                               _op("retrieve", "retrieve", "get", "/v1/customers/{customer}"),
                               _op("delete", "delete", "delete", "/v1/customers/{customer}")]}
    schemas["payment_source"] = {"type": "object", "x-stripeResource": {"class_name": "PaymentSource", "in_package": ""},
                                 "x-stripeOperations": [
                                     dict(_op("create", "create", "post", "/v1/customers/{customer}/sources"), method_on="collection"),
                                     dict(_op("list", "list", "get", "/v1/customers/{customer}/sources"), method_on="collection")]}
    add("/v1/sources", "post", {"type": {"type": "string"}})
    schemas["source"] = {"type": "object", "x-stripeResource": {"class_name": "Source", "in_package": ""},
                         "x-stripeOperations": [_op("create", "create", "post", "/v1/sources")]}
    add("/v1/account", "get")
    add("/v1/accounts/{account}", "get")
    schemas["account"] = {"type": "object", "x-stripeResource": {"class_name": "Account", "in_package": ""},
                          "x-stripeOperations": [
                              _op("retrieve", "retrieve", "get", "/v1/account"),
                              _op("retrieve", "retrieve", "get", "/v1/accounts/{account}")]}
    return doc


@pytest.fixture(scope="module")
def index():
    return SdkIndex.build(load_spec(_doc()))


def read(text, index):
    usages, unresolved, _ = scan_text(H + text, "app/Pay.php", index)
    return [(u.line, u.label, u.call, sorted(u.fields), u.complete) for u in usages]


def labels(text, index):
    return [(call, label) for _, label, call, _, _ in read(text, index)]


# --- calls on returned objects -----------------------------------------------------------


def test_instance_methods_of_a_retrieved_object_map_to_its_own_paths(index):
    text = (
        "function f($id) {\n"
        "  $pi = \\Stripe\\PaymentIntent::retrieve($id);\n"
        "  $pi->confirm(['payment_method_types' => ['card']], ['idempotency_key' => 'k']);\n"
        "  $pi->toArray();\n"
        "}\n"
    )
    assert read(text, index)[1] == (4, "POST /v1/payment_intents/{intent}/confirm", "$pi->confirm", ["payment_method_types"], True)
    assert len(read(text, index)) == 2


def test_create_and_a_typed_parameter_give_the_object_too(index):
    text = (
        "use Stripe\\Customer;\n"
        "function f(\\Stripe\\PaymentIntent $pi, ?Customer $c) {\n"
        "  $pi->confirm(['payment_method_types' => ['card']]);\n"
        "  $c->delete();\n"
        "  $n = \\Stripe\\Customer::create(['email' => 'e']);\n"
        "  $n->sources->create(['source' => 'tok']);\n"
        "  $n->sources->all(['limit' => 3]);\n"
        "}\n"
    )
    assert labels(text, index) == [
        ("$pi->confirm", "POST /v1/payment_intents/{intent}/confirm"),
        ("$c->delete", "DELETE /v1/customers/{customer}"),
        ("\\Stripe\\Customer::create", "POST /v1/customers"),
        ("$n->sources->create", "POST /v1/customers/{customer}/sources"),
        ("$n->sources->all", "GET /v1/customers/{customer}/sources"),
    ]


def test_subscription_cancel_is_the_delete_on_its_own_path(index):
    text = "function f($id) {\n  $s = \\Stripe\\Subscription::retrieve($id);\n  $s->cancel();\n}\n"
    assert labels(text, index)[1] == ("$s->cancel", f"DELETE /v1/subscriptions/{{subscription_exposed_id}}")


@pytest.mark.parametrize("body", [
    "function f($pi) { $pi->confirm(['payment_method_types' => ['card']]); }",  # an untyped parameter
    "function f(\\App\\PaymentIntent $pi) { $pi->confirm(['x' => 1]); }",  # a class of the same name elsewhere
    "function f(\\Stripe\\PaymentIntent|false $pi) { $pi->confirm(['x' => 1]); }",  # a union type
    "function f(\\Stripe\\PaymentIntent $pi) { $pi = g(); $pi->confirm(['x' => 1]); }",  # reassigned
    "function f($id, $c) { $pi = \\Stripe\\PaymentIntent::retrieve($id); if ($c) { $pi = g(); } $pi->confirm(['x' => 1]); }",
    "function f($id) { $pi = \\Stripe\\PaymentIntent::retrieve($id)->latest_charge; $pi->confirm(['x' => 1]); }",
    "function f($id) { $pi = $this->get_intent($id); $pi->confirm(['x' => 1]); }",  # a helper's value
    "function f($rows) { foreach ($rows as $pi) { $pi->confirm(['x' => 1]); } }",
    "function f($id) { $pi = \\Stripe\\PaymentIntent::retrieve($id); $q = &$pi; $pi->confirm(['x' => 1]); }",
    "function f($id) { $pi = \\Stripe\\PaymentIntent::retrieve($id); $pi->search(['query' => 'q']); }",  # static only
    "function f($id) { $pi = \\Stripe\\PaymentIntent::retrieve($id); $pi->update($id, ['x' => 1]); }",
    "function f($id) { $pi = \\Stripe\\PaymentIntent::retrieve($id); $pi->charges->all(); }",  # not a collection of the spec
    "function f($id) { $c = \\Stripe\\Customer::retrieve($id); $c->subscriptions->all(); }",
])
def test_an_object_that_is_not_known_for_certain_maps_nothing(index, body):
    assert [c for c, _ in labels(body + "\n", index) if "->" in c] == []


def test_a_call_on_what_an_instance_method_returned_is_not_followed(index):
    text = "function f($id) {\n  $pi = \\Stripe\\PaymentIntent::retrieve($id);\n  $pi->confirm()->capture();\n}\n"
    assert [c for c, _ in labels(text, index)] == ["\\Stripe\\PaymentIntent::retrieve", "$pi->confirm"]


def test_a_retrieved_customer_named_stripe_is_still_a_customer(index):
    text = "function f($id) {\n  $stripe_customer = \\Stripe\\Customer::retrieve($id);\n  $stripe_customer->sources->create(['source' => 't']);\n}\n"
    assert labels(text, index)[1] == ("$stripe_customer->sources->create", "POST /v1/customers/{customer}/sources")


# --- save() ------------------------------------------------------------------------------


def test_save_sends_the_properties_assigned_before_it(index):
    text = (
        "function f($id, $pm) {\n"
        "  $s = \\Stripe\\Subscription::retrieve($id);\n"
        "  $s->default_payment_method = $pm;\n"
        "  echo $s->status;\n"
        "  $s->save();\n"
        "}\n"
    )
    assert read(text, index)[1] == (6, "POST /v1/subscriptions/{subscription_exposed_id}", "$s->save", ["default_payment_method"], True)


@pytest.mark.parametrize("between,fields,complete", [
    ("$s->metadata['k'] = 'v';", ["metadata"], False),  # a nested write hides that property's contents
    ("helper($s);", [], False),  # passed on
    ("$s->updateAttributes(['metadata' => []]);", [], False),  # a method of the object
    ("$s['default_payment_method'] = 'x';", [], False),  # ArrayAccess
])
def test_save_hides_what_it_cannot_follow(index, between, fields, complete):
    text = f"function f($id) {{\n  $s = \\Stripe\\Subscription::retrieve($id);\n  {between}\n  $s->save();\n}}\n"
    found = read(text, index)
    assert [(f, c) for _, label, call, f, c in found if call == "$s->save"] == [(fields, complete)]


def test_save_with_nothing_assigned_sends_no_request_and_is_not_listed(index):
    text = "function f($id) {\n  $s = \\Stripe\\Subscription::retrieve($id);\n  $s->save();\n}\n"
    assert [c for c, _ in labels(text, index)] == ["\\Stripe\\Subscription::retrieve"]


def test_save_inside_a_loop_hides_the_rest(index):
    text = "function f($id, $xs) {\n  $s = \\Stripe\\Subscription::retrieve($id);\n  foreach ($xs as $x) { $s->metadata = $x; $s->save(); }\n}\n"
    assert [(f, c) for _, _, call, f, c in read(text, index) if call == "$s->save"] == [(["metadata"], False)]


# --- StripeClient evidence -----------------------------------------------------------------


@pytest.mark.parametrize("owner,found", [
    ("$stripe", True), ("$stripeClient", True), ("$this->stripe", True), ("$this->getStripeClient()", True),
    ("app('stripe')", True), ("$stripe_customer", False), ("$this->stripeCustomer", False),
    ("$user->createAsStripeCustomer()", False), ("$client", False),
])
def test_a_client_by_name_needs_a_name_that_says_client_and_nothing_else(index, owner, found):
    text = f"function f() {{\n  {owner}->sources->create(['type' => 'x']);\n}}\n"
    assert bool(read(text, index)) is found


def test_a_client_by_construction_or_type_needs_no_name(index):
    text = (
        "function f(\\Stripe\\StripeClient $client) { $client->sources->create(['type' => 'x']); }\n"
        "function g($k) { $c = new \\Stripe\\StripeClient($k); $c->sources->create(['type' => 'y']); }\n"
    )
    assert labels(text, index) == [("$client->sources->create", "POST /v1/sources"), ("$c->sources->create", "POST /v1/sources")]


# --- Account::retrieve -------------------------------------------------------------------------


@pytest.mark.parametrize("args,label", [
    ("$id", "GET /v1/accounts/{account}"),
    ("$id, ['stripe_account' => $a]", "GET /v1/accounts/{account}"),
    ("['id' => $id, 'expand' => ['x']]", "GET /v1/accounts/{account}"),
    ("", "GET /v1/account"),
    ("null, $opts", "GET /v1/account"),
    ("'sk_test_123'", "GET /v1/account"),
    ("['expand' => ['x']]", "GET /v1/account"),
])
def test_account_retrieve_with_an_id_is_that_account(index, args, label):
    assert labels(f"\\Stripe\\Account::retrieve({args});\n", index) == [("\\Stripe\\Account::retrieve", label)]


# --- a vendored, renamed stripe-php ------------------------------------------------------------

API_RESOURCE = (
    "<?php\n\nnamespace EDD\\Vendor\\Stripe;\n\nabstract class ApiResource extends StripeObject\n{\n"
    "    public static function baseUrl() { return Stripe::$apiBase; }\n"
    "    public static function classUrl() { return '/v1/x'; }\n"
    "    public static function resourceUrl($id) { return static::classUrl() . '/' . $id; }\n"
    "}\n"
)
SIBLINGS = {  # stripe-php's shape: ApiResource next to StripeObject and ApiRequestor, one namespace
    "StripeObject.php": "<?php\nnamespace EDD\\Vendor\\Stripe;\nclass StripeObject implements \\ArrayAccess {}\n",
    "ApiRequestor.php": "<?php\nnamespace EDD\\Vendor\\Stripe;\nclass ApiRequestor {}\n",
}


def vendored(where="lib/Stripe", resource=None, siblings=None):
    """The files of a stripe-php copy under the EDD\\Vendor prefix, at ``where``."""
    out = {f"{where}/ApiResource.php": API_RESOURCE if resource is None else resource}
    out.update({f"{where}/{k}": v for k, v in (SIBLINGS if siblings is None else siblings).items()})
    return out


def scan(tmp_path, files, index):
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    result = scan_repo(tmp_path, index)
    return [(u.file, u.line, u.call, u.label, sorted(u.fields), u.complete) for u in result.usages if u.language == "php"]


PREFIXED = H + (
    "use EDD\\Vendor\\Stripe\\Customer;\n"
    "\\EDD\\Vendor\\Stripe\\Charge::create(['amount' => 1]);\n"
    "Customer::create(['email' => 'e']);\n"
    "\\App\\Billing\\Stripe\\Charge::create(['amount' => 2]);\n"
    "\\Acme\\Vendor\\Stripe\\Charge::create(['amount' => 3]);\n"
)


def test_a_prefixed_namespace_counts_when_the_checkout_holds_that_copy(tmp_path, index):
    found = scan(tmp_path, {**vendored("libraries/Stripe/lib"), "src/pay.php": PREFIXED}, index)
    assert [(c, label) for _, _, c, label, _, _ in found] == [
        ("\\EDD\\Vendor\\Stripe\\Charge::create", "POST /v1/charges"), ("Customer::create", "POST /v1/customers"),
    ]


@pytest.mark.parametrize("marker", [
    None,  # no copy at all
    API_RESOURCE.replace("abstract class ApiResource", "class Thing"),  # not stripe-php's class
    API_RESOURCE.replace("EDD\\Vendor\\Stripe", "EDD\\Vendor\\StripeX"),
])
def test_without_the_vendored_copy_a_prefixed_name_is_not_stripe(tmp_path, index, marker):
    files = {"src/pay.php": PREFIXED}
    if marker:
        files.update(vendored("libraries/Stripe/lib", resource=marker))
    assert scan(tmp_path, files, index) == []


@pytest.mark.parametrize("resource,siblings", [
    (API_RESOURCE.replace(" extends StripeObject", ""), None),  # not stripe-php's base class
    (API_RESOURCE.replace("function classUrl", "function otherUrl"), None),
    (None, {"StripeObject.php": SIBLINGS["StripeObject.php"]}),  # no ApiRequestor next to it
    (None, {**SIBLINGS, "ApiRequestor.php": "<?php\nnamespace Other\\Stripe;\nclass ApiRequestor {}\n"}),
])
def test_only_stripe_phps_own_shape_counts_as_the_vendored_copy(tmp_path, index, resource, siblings):
    files = {"src/pay.php": PREFIXED, **vendored("libraries/Stripe/lib", resource=resource, siblings=siblings)}
    assert scan(tmp_path, files, index) == []


def test_an_applications_own_stripe_namespace_is_not_stripe_php(tmp_path, index):
    files = {
        "app/Stripe/ApiResource.php": H + "namespace App\\Stripe;\nabstract class ApiResource { }\n",
        "app/Pay.php": H + "// Stripe\nuse App\\Stripe\\PaymentIntent;\nPaymentIntent::create(['amount' => 1]);\n",
    }
    assert scan(tmp_path, files, index) == []


def test_a_file_read_alone_knows_no_prefixes(index):
    assert read("\\EDD\\Vendor\\Stripe\\Charge::create(['amount' => 1]);\n", index) == []


# --- string-dispatch wrappers -------------------------------------------------------------------

API = H + (
    "namespace EDD\\Gateways\\Stripe;\n"
    "class API {\n"
    "  public function request($stripe_object, $method, ...$args) {\n"
    "    $classname = 'EDD\\\\Vendor\\\\Stripe\\\\' . $stripe_object;\n"
    "    if (!is_callable(array($classname, $method))) { throw new \\Exception($classname); }\n"
    "    @WRITE@\n"
    "    return call_user_func_array(array($classname, $method), $args);\n"
    "  }\n"
    "}\n"
)
FORWARD = H + (
    "function edds_api_request($stripe_object, $method, ...$args) {\n"
    "  $api = new \\EDD\\Gateways\\Stripe\\API();\n"
    "  return $api->request($stripe_object, $method, ...$args);\n"
    "}\n"
)
CALLS = H + (
    "// Stripe payments (a file that never mentions Stripe is not read at all)\n"
    "function pay($id, $type, $p) {\n"
    "  edds_api_request('Charge', 'create', ['amount' => 1, 'currency' => 'usd']);\n"
    "  $pi = edds_api_request('PaymentIntent', 'retrieve', $id);\n"
    "  $pi->confirm(['payment_method_types' => ['card']]);\n"
    "  edds_api_request('PaymentIntent', 'update', $id, ['description' => 'd']);\n"
    "  edds_api_request($type, 'create', ['amount' => 1]);\n"
    "  edds_api_request('Charge', $p, ['amount' => 1]);\n"
    "  edds_api_request(\"Charge$type\", 'create', ['amount' => 1]);\n"
    "}\n"
)


def dispatch(tmp_path, index, write="", **over):
    files = {**vendored(), "src/API.php": API.replace("@WRITE@", write),
             "inc/functions.php": FORWARD, "inc/pay.php": CALLS}
    files.update(over)
    return [(c, label, f, complete) for file, _, c, label, f, complete in scan(tmp_path, files, index) if file == "inc/pay.php"]


def test_a_dispatch_wrapper_call_with_literal_strings_is_the_static_call(tmp_path, index):
    assert dispatch(tmp_path, index) == [
        ("edds_api_request('Charge', 'create')", "POST /v1/charges", ["amount", "currency"], True),
        ("edds_api_request('PaymentIntent', 'retrieve')", "GET /v1/payment_intents/{intent}", [], True),
        ("$pi->confirm", "POST /v1/payment_intents/{intent}/confirm", ["payment_method_types"], True),
        ("edds_api_request('PaymentIntent', 'update')", "POST /v1/payment_intents/{intent}", ["description"], True),
    ]


@pytest.mark.parametrize("write,update", [
    ("if ($x) { $args[1]['expand'] = ['charges']; }", (["description", "expand"], True)),  # a literal key path
    ("if (!isset($args[1])) { $args[1] = array(); }", (["description"], True)),  # an empty default, when unset
    ("$args[1] = array();", (["description"], False)),  # unconditional: the caller's array is gone
    ("$args[1] = $other;", (["description"], False)),
    ("$args[1][$k] = 1;", (["description"], False)),
])
def test_a_wrapper_that_writes_into_one_argument_changes_only_that_position(tmp_path, index, write, update):
    found = {c.split("'")[3]: (f, ok) for c, _, f, ok in dispatch(tmp_path, index, write=write) if c.startswith("edds")}
    assert found["create"] == (["amount", "currency"], True) and found["update"] == update


def test_a_wrapper_that_rebinds_its_arguments_hides_every_position(tmp_path, index):
    found = dispatch(tmp_path, index, write="$args = array_values($args);")
    assert all(not complete for c, _, _, complete in found if c.startswith("edds") and "retrieve" not in c)


def _with_method(method):
    return {"src/API.php": API.replace("@WRITE@", "@CALL@").replace("  }\n}\n", f"  }}\n  {method}\n}}\n")}


@pytest.mark.parametrize("write,extra", [
    ("self::prepare($args);", None),  # a helper this file does not define: it may take it by reference
    ("$this->defaults($args);", "private function defaults(array &$args) { $args[0]['currency'] = 'eur'; }"),
    ("prepare_args($args[0]);", None),
    ("do_action_ref_array('edds_pre', array(&$args));", None),
    ("foreach ($args as &$a) { $a = apply_filters('x', $a); }", None),
    ("foreach ($args[0] as $k => &$v) { $v = 1; }", None),
    ("$first = &$args[0]; $first['currency'] = 'usd';", None),
    ("unset($args[0]['currency']);", None),
    ("array_unshift($args, $x);", None),
    ("$args = apply_filters('edds_args', $args);", None),
    ("extract($args);", None),
    ("$f = function () use (&$args) { $args[0] = []; };", None),
])
def test_anything_else_a_wrapper_may_do_to_its_arguments_hides_every_position(tmp_path, index, write, extra):
    over = _with_method(extra) if extra else {}
    if extra:
        over["src/API.php"] = over["src/API.php"].replace("@CALL@", write)
    found = dispatch(tmp_path, index, write=write, **over)
    assert {c.split("'")[3]: ok for c, _, _, ok in found if c.startswith("edds")} == {
        "create": False, "retrieve": True, "update": False,
    }


@pytest.mark.parametrize("write,extra", [
    ("do_action('edds_pre', $this, $stripe_object, $method, $args);", None),
    ("if (isset($args[1]) && count($args) > 1) { $n = 1; }", None),
    ("$this->check($stripe_object, $method, $args);", "private function check($o, $m, $args): bool { return true; }"),
    ("log_it(count($args));", None),  # an expression is a value, never a reference
])
def test_what_takes_the_arguments_by_value_changes_nothing(tmp_path, index, write, extra):
    over = _with_method(extra) if extra else {}
    if extra:
        over["src/API.php"] = over["src/API.php"].replace("@CALL@", write)
    found = dispatch(tmp_path, index, write=write, **over)
    assert all(ok for c, _, _, ok in found if c.startswith("edds"))


@pytest.mark.parametrize("change", [
    {"src/API.php": API.replace("'EDD\\\\Vendor\\\\Stripe\\\\' . ", "'App\\\\Models\\\\' . ").replace("@WRITE@", "")},
    {"src/API.php": API.replace("@WRITE@", "$method = 'create';")},  # the method is changed in the wrapper
    {"src/API.php": API.replace("@WRITE@", "$classname = 'X';")},  # the class name is bound twice
    {"lib/Stripe/ApiResource.php": "<?php\nnamespace EDD\\Vendor\\Stripe;\nclass Other {}\n"},  # no vendored copy
    {"lib/Stripe/ApiRequestor.php": "<?php\nnamespace App\\Stripe;\nclass ApiRequestor {}\n"},  # not stripe-php's shape
    {"inc/other.php": FORWARD},  # the forwarder is defined twice
    {"inc/functions.php": FORWARD.replace("...$args);", "$args);")},  # not a plain forward
    {"inc/functions.php": FORWARD.replace("return $api->request(", "log(); return $api->request(")},
])
def test_no_dispatch_without_the_whole_rule(tmp_path, index, change):
    assert [c for c, _, _, _ in dispatch(tmp_path, index, **change) if c.startswith("edds")] == []
