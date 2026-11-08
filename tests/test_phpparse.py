"""The PHP reader: which calls are Stripe's, which endpoint, which fields, and what stays hidden.

Call shapes come from invoiceninja (static resource classes, imported or fully qualified, request options
as the last argument) and laravel/cashier-stripe (``$this->owner->stripe()->subscriptions->update(``,
service variables such as ``$customersService = static::stripe()->customers``).
"""

import pytest
from specs import sdk_doc

from apidrift.openapi import load_spec
from apidrift.scanner import scan_repo, scan_text
from apidrift.sdkmap import SdkIndex

H = "<?php\n"


@pytest.fixture(scope="module")
def index():
    return SdkIndex.build(load_spec(sdk_doc()))


def read(text, index, rel="app/Pay.php"):
    usages, unresolved, unread = scan_text(text, rel, index)
    assert unread is None and all(u.language == "php" for u in usages)
    return [(u.line, u.call, u.label, sorted(u.fields), u.complete) for u in usages], [call for _, _, call in unresolved]


def one(text, index):
    found, _ = read(text, index)
    assert len(found) == 1, found
    return found[0]


# --- static resource classes ----------------------------------------------------------


@pytest.mark.parametrize("head,call", [
    ("", "\\Stripe\\Charge::create"),
    ("", "Stripe\\Charge::create"),
    ("use Stripe\\Charge;\n", "Charge::create"),
    ("use Stripe\\Charge as StripeCharge;\n", "StripeCharge::create"),
    ("use Stripe\\{Refund, Charge};\n", "Charge::create"),
    ("use Stripe\\{Refund, Charge as C};\n", "C::create"),
    ("use Stripe;\n", "Stripe\\Charge::create"),
])
def test_static_calls_on_a_stripe_class_are_read(index, head, call):
    text = H + head + f"$charge = {call}(['amount' => 100, 'currency' => 'usd'], ['idempotency_key' => $key]);\n"
    line = text.count("\n")
    assert one(text, index) == (line, call, "POST /v1/charges", ["amount", "currency"], True)


def test_a_namespaced_resource_through_use_and_fully_qualified(index):
    text = H + (
        "use Stripe\\Checkout\\Session;\n"
        "$a = Session::create(['mode' => 'payment', 'line_items' => [['price' => $p, 'quantity' => 1]]]);\n"
        "$b = \\Stripe\\Checkout\\Session::create(array('mode' => 'setup'));\n"
    )
    assert read(text, index)[0] == [
        (3, "Session::create", "POST /v1/checkout/sessions", ["line_items", "line_items[].price", "line_items[].quantity", "mode"], True),
        (4, "\\Stripe\\Checkout\\Session::create", "POST /v1/checkout/sessions", ["mode"], True),
    ]


@pytest.mark.parametrize("text", [
    "Charge::create(['amount' => 100]);\n",  # an Eloquent model of the same name
    "use App\\Models\\Charge;\nCharge::create(['amount' => 100]);\n",
    "\\App\\Models\\Charge::create(['amount' => 100]);\n",
    "namespace App;\nBilling\\Charge::create(['amount' => 100]);\n",
    "use Stripe\\Checkout\\Session;\nCharge::create(['amount' => 100]);\n",
])
def test_a_static_call_on_a_class_outside_stripe_is_not_a_stripe_call(index, text):
    assert read(H + text, index) == ([], [])


def test_a_stripe_class_that_is_not_the_resource_is_listed_but_matches_no_endpoint(index):
    text = H + "\\Stripe\\Billing\\Charge::create(['amount' => 100]);\n"
    assert read(text, index) == ([], ["\\Stripe\\Billing\\Charge::create"])


def test_ids_come_first_and_request_options_are_never_parameters(index):
    text = H + (
        "use Stripe\\Charge;\n"
        "$a = Charge::retrieve($id, $this->stripe_connect_auth);\n"
        "$b = Charge::capture($charge->id, ['amount' => 5], ['stripe_account' => $acct]);\n"
        "$c = Charge::capture($id, $data, $opts);\n"
        "$d = Charge::all(['limit' => 3]);\n"
        "$e = Charge::retrieve(['id' => $id, 'expand' => ['customer']], $opts);\n"
    )
    assert read(text, index)[0] == [
        (3, "Charge::retrieve", "GET /v1/charges/{charge}", [], True),
        (4, "Charge::capture", "POST /v1/charges/{charge}/capture", ["amount"], True),
        (5, "Charge::capture", "POST /v1/charges/{charge}/capture", [], False),
        (6, "Charge::all", "GET /v1/charges", ["limit"], True),
        (7, "Charge::retrieve", "GET /v1/charges/{charge}", ["expand", "id"], True),
    ]


# --- StripeClient services ------------------------------------------------------------


@pytest.mark.parametrize("call,shown", [
    ("$stripe->charges->create", None),
    ("$this->stripe->charges->create", None),
    ("$this->stripe?->charges->create", None),
    ("$this->owner->stripe()->charges->create", None),
    ("$this->subscription->owner->stripe()->charges->create", None),
    ("Cashier::stripe()->charges->create", None),
    ("static::stripe()->charges->create", None),
    ("self::$stripe->charges->create", None),
    ("$this->getStripeClient()->charges->create", None),
    ("(new \\Stripe\\StripeClient($key))->charges->create", "(new \\Stripe\\StripeClient())->charges->create"),
    ("app('stripe')->charges->create", "app()->charges->create"),
    ("resolve(StripeClient::class)->charges->create", "resolve()->charges->create"),
    ("$stripe->v1->charges->create", None),
])
def test_service_calls_off_a_stripe_client_are_read(index, call, shown):
    text = H + f"function pay($key) {{\n    return {call}(['amount' => 100]);\n}}\n"
    assert one(text, index) == (3, shown or call, "POST /v1/charges", ["amount"], True)


def test_nested_services_and_a_chain_split_over_lines_report_the_line_the_chain_starts_on(index):
    text = H + (
        "$session = $stripe->checkout->sessions->create([\n"
        "    'mode' => 'payment',\n"
        "]);\n"
        "$refund = $this->owner->stripe()->charges\n"
        "    ->capture(\n"
        "        $this->stripe_id, ['amount' => 1]\n"
        "    );\n"
    )
    assert read(text, index)[0] == [
        (2, "$stripe->checkout->sessions->create", "POST /v1/checkout/sessions", ["mode"], True),
        (5, "$this->owner->stripe()->charges->capture", "POST /v1/charges/{charge}/capture", ["amount"], True),
    ]


@pytest.mark.parametrize("head,call", [
    ("$client = new \\Stripe\\StripeClient($key);\n", "$client->charges->create"),
    ("use Stripe\\StripeClient;\n$api = new StripeClient(['api_key' => $key]);\n", "$api->charges->create"),
    ("use Stripe\\StripeClient;\nclass Pay {\n    public function __construct(private StripeClient $client) {}\n", "$this->client->charges->create"),
    ("class Pay {\n    private \\Stripe\\StripeClient $sdk;\n", "$this->sdk->charges->create"),
    ("class Pay {\n    public function boot() { $this->api = new \\Stripe\\StripeClient($k); }\n", "$this->api->charges->create"),
])
def test_a_client_is_known_by_its_constructor_or_its_type(index, head, call):
    text = H + head + f"function go() {{ return {call}(['amount' => 1]); }}\n"
    found, _ = read(text, index)
    assert [(c, label, fields) for _, c, label, fields, _ in found] == [(call, "POST /v1/charges", ["amount"])]


@pytest.mark.parametrize("call", [
    "$this->client->charges->create",  # nothing says this client is Stripe's
    "$user->charges()->create",  # an Eloquent relation
    "$this->stripe->customer->charges()->create",
    "$user->stripeCharges()->create",
    "$this->stripe->client->invoices()->charges->create",
    "$order->charges->create",
])
def test_services_that_do_not_hang_off_a_stripe_client_are_not_stripe_calls(index, call):
    found, _ = read(H + f"function go() {{ return {call}(['amount' => 1]); }}\n", index)
    assert found == []


def test_a_service_held_in_a_variable_is_followed_to_its_client(index):
    text = H + (
        "class Billable {\n"
        "    public function charge(array $options) {\n"
        "        $chargesService = static::stripe()->charges;\n"
        "\n"
        "        return $chargesService->create(['amount' => 100] + $options);\n"
        "    }\n"
        "    public function sessions() {\n"
        "        if ($this->owner) {\n"
        "            $stripe = $this->owner->stripe();\n"
        "        } else {\n"
        "            $stripe = Cashier::stripe();\n"
        "        }\n"
        "        $sessions = $stripe->checkout->sessions;\n"
        "        return $sessions->create(['mode' => 'setup']);\n"
        "    }\n"
        "    public function other() {\n"
        "        return $chargesService->create(['amount' => 1]);\n"  # another method's variable
        "    }\n"
        "}\n"
    )
    assert read(text, index)[0] == [
        (6, "$chargesService->create", "POST /v1/charges", ["amount"], False),
        (15, "$sessions->create", "POST /v1/checkout/sessions", ["mode"], True),
    ]


def test_all_is_stripe_phps_name_for_list(index):
    text = H + "$a = $stripe->charges->all(['limit' => 10]);\n$b = \\Stripe\\Charge::all();\n"
    assert [(label, fields) for _, _, label, fields, _ in read(text, index)[0]] == [
        ("GET /v1/charges", ["limit"]), ("GET /v1/charges", []),
    ]


def test_other_stripe_calls_are_listed_once_and_a_receiver_is_not_a_call_of_its_own(index):
    text = H + (
        "\\Stripe\\Stripe::setApiKey($key);\n"
        "$event = \\Stripe\\Webhook::constructEvent($payload, $sig, $secret);\n"
        "$this->owner->stripe()->charges->create(['amount' => 1]);\n"
        "$customer = $this->asStripeCustomer();\n"
    )
    found, unresolved = read(text, index)
    assert [call for _, call, _, _, _ in found] == ["$this->owner->stripe()->charges->create"]
    assert unresolved == ["\\Stripe\\Stripe::setApiKey", "\\Stripe\\Webhook::constructEvent", "$this->asStripeCustomer"]


# --- arrays ---------------------------------------------------------------------------


def fields_of(args, index, head=""):
    return one(H + head + f"$r = \\Stripe\\Charge::create({args});\n", index)[3:]


@pytest.mark.parametrize("args,fields", [
    ("[]", []),
    ("['amount' => 1, \"currency\" => 'usd',]", ["amount", "currency"]),
    ("array('amount' => 1, 'shipping' => array('address' => array('city' => 'x')))", ["amount", "shipping", "shipping.address", "shipping.address.city"]),
    ("[\n  'amount' => 1, // cents\n  /* 'skipped' => 1, */\n  # 'also' => 2,\n  'currency' => 'usd',\n]", ["amount", "currency"]),
    ("['items' => [['price' => 'p', 'tax' => ['a', 'b']], ['quantity' => 2]]]", ["items", "items[].price", "items[].quantity", "items[].tax"]),
    ("['items' => [0 => ['price' => 'p']]]", ["items", "items[].price"]),
    ("['description' => \"a {$o['k']} => 'x' => b\", 'amount' => 1]", ["amount", "description"]),
    ("['description' => 'it\\'s => [not, a, key', 'amount' => 1]", ["amount", "description"]),
    ("['callback' => fn ($x) => $x * 2, 'amount' => $a ? 1 : 2]", ["amount", "callback"]),
    ("['metadata' => ['when' => match ($x) { 1 => 'a', default => 'b' }]]", ["metadata", "metadata.when"]),
])
def test_array_literals_are_read_into_dotted_paths(index, args, fields):
    assert fields_of(args, index) == (fields, True)


@pytest.mark.parametrize("args,fields,hidden", [
    ("['amount' => 1, ...$extra]", ["amount"], {""}),
    ("['amount' => 1, 'metadata' => ['a' => 1, ...$m]]", ["amount", "metadata", "metadata.a"], {"metadata.", "metadata[]."}),
    ("['amount' => 1, 'metadata' => [$key => 1]]", ["amount", "metadata"], {"metadata.", "metadata[]."}),
    ("['amount' => 1, self::KEY => 2]", ["amount"], {""}),
    ("['amount' => 1, 'pre_' . $k => 2]", ["amount"], {""}),
    ("['amount' => 1, \"k_$i\" => 2]", ["amount"], {""}),
    ("['amount' => 1, 'metadata' => ['a' => 1] + $more]", ["amount", "metadata", "metadata.a"], {"metadata.", "metadata[]."}),
    ("['amount' => 1] + $options", ["amount"], {""}),
    ("array_merge(['amount' => 1], $options)", ["amount"], {""}),
    ("array_filter(['amount' => $a, 'currency' => $c])", ["amount", "currency"], {""}),
    ("$options", [], {""}),
    ("$this->params", [], {""}),
    ("compact('amount', 'currency')", [], {""}),
    ("...$args", [], {""}),
    ("[$a, $b]", [], {""}),
])
def test_what_cannot_be_seen_hides_only_its_own_array(index, args, fields, hidden):
    usages, _, _ = scan_text(H + f"function f($options, $args) {{ return \\Stripe\\Charge::create({args}); }}\n", "a.php", index)
    assert (sorted(usages[0].fields), usages[0].complete, set(usages[0].hidden)) == (fields, False, hidden)


def test_a_nested_value_that_is_a_variable_is_a_field_whose_contents_are_not_listed(index):
    assert fields_of("['amount' => 1, 'metadata' => $meta, 'shipping' => array_merge($a, ['name' => 'x'])]", index) == (
        ["amount", "metadata", "shipping"], True,
    )


# --- variables ------------------------------------------------------------------------


def test_a_variable_bound_to_a_literal_in_the_same_function_is_read(index):
    text = H + (
        "function pay($stripe) {\n"
        "    $params = [\n"
        "        'amount' => 100,\n"
        "        'shipping' => ['name' => 'x'],\n"
        "    ];\n"
        "    return $stripe->charges->create($params, ['idempotency_key' => 'k']);\n"
        "}\n"
    )
    assert one(text, index) == (7, "$stripe->charges->create", "POST /v1/charges", ["amount", "shipping", "shipping.name"], True)


def test_keys_written_into_the_variable_by_a_literal_path_are_fields_and_hide_nothing_else(index):
    text = H + (
        "function pay($stripe, $extra) {\n"
        "    $data = ['amount' => 100];\n"
        "    $data['currency'] = 'usd';\n"
        "    $data['metadata']['order'] = 7;\n"
        "    $data += ['description' => 'x'];\n"
        "    return $stripe->charges->create($data);\n"
        "}\n"
    )
    assert one(text, index)[3:] == (["amount", "currency", "description", "metadata", "metadata.order"], True)


@pytest.mark.parametrize("write,fields,hidden", [
    ("$d[$k] = 1;", ["amount"], {""}),
    ("$d[] = 1;", ["amount"], {""}),
    ("$d .= 'x';", ["amount"], {""}),
    ("$d += $more;", ["amount"], {""}),
    ("$d['shipping'][$k] = 1;", ["amount", "shipping"], {"", "shipping.", "shipping[]."}),
    ("$d['shipping'] = ['name' => 'n'] + $more;", ["amount", "shipping", "shipping.name"], {"shipping.", "shipping[]."}),
    ("$d['line_items'][] = ['price' => $p];", ["amount", "line_items", "line_items[].price"], set()),
    ("$d['line_items'][0]['quantity'] = 2;", ["amount", "line_items", "line_items[].quantity"], set()),
    ("$d['metadata'] .= 'x';", ["amount", "metadata"], set()),
    ("$d['shipping'] = $address;", ["amount", "shipping"], set()),
])
def test_a_later_write_hides_only_what_its_key_path_does_not_show(index, write, fields, hidden):
    text = H + f"function pay($stripe, $k, $more, $p, $address) {{\n  $d = ['amount' => 1];\n  {write}\n  return $stripe->charges->create($d);\n}}\n"
    usages, _, _ = scan_text(text, "a.php", index)
    assert [(sorted(u.fields), set(u.hidden)) for u in usages] == [(fields, hidden)]


def test_a_write_in_the_loop_after_the_call_reaches_it_on_the_next_pass(index):
    text = H + (
        "function pay($stripe, $rows) {\n"
        "    $d = ['amount' => 1];\n"
        "    foreach ($rows as $r) {\n"
        "        $stripe->charges->create($d);\n"
        "        $d['currency'] = 'usd';\n"
        "    }\n"
        "}\n"
    )
    assert one(text, index)[3:] == (["amount", "currency"], True)


def test_a_write_after_the_call_does_not_reach_it_when_the_loop_binds_the_variable_first(index):
    text = H + (
        "function pay($stripe, $rows) {\n"
        "    foreach ($rows as $r) {\n"
        "        $d = ['amount' => 1];\n"
        "        $stripe->charges->create($d);\n"
        "        $d['currency'] = 'usd';\n"
        "        log_it($d);\n"
        "    }\n"
        "}\n"
    )
    assert one(text, index)[3:] == (["amount"], True)


def test_a_wordpress_filter_of_the_variable_keeps_its_fields_and_hides_the_rest(index):
    text = H + (
        "function pay($stripe, $order) {\n"
        "    $p = ['amount' => 1, 'shipping' => ['name' => 'n']];\n"
        "    $p['currency'] = 'usd';\n"
        "    $p = apply_filters('my_hook', $p, $order);\n"
        "    $p['description'] = 'd';\n"
        "    return $stripe->charges->create($p);\n"
        "}\n"
    )
    assert one(text, index)[3:] == (["amount", "currency", "description", "shipping", "shipping.name"], False)


@pytest.mark.parametrize("rebind", [
    "$p = $this->add_fee($p);",  # any other call: what it returns is not known
    "$p = apply_filters('my_hook', $q);",  # a filter of another value
    "$p = apply_filters($hook, $p);",  # a hook name that is not a literal
    "$p = apply_filters('my_hook', $p) + $more;",
])
def test_any_other_rebinding_hides_everything(index, rebind):
    text = H + f"function pay($stripe, $q, $hook, $more) {{\n  $p = ['amount' => 1];\n  {rebind}\n  return $stripe->charges->create($p);\n}}\n"
    usages, _, _ = scan_text(text, "a.php", index)
    assert [(sorted(u.fields), set(u.hidden)) for u in usages] == [([], {""})]


def test_both_branches_of_an_if_reach_the_call(index):
    text = H + (
        "function pay($stripe, $x) {\n"
        "    if ($x) {\n"
        "        $p = ['amount' => 1];\n"
        "    } else {\n"
        "        $p = ['currency' => 'usd'];\n"
        "    }\n"
        "    return $stripe->charges->create($p);\n"
        "}\n"
    )
    assert one(text, index)[3:] == (["amount", "currency"], True)


@pytest.mark.parametrize("body", [
    "function pay($stripe, $p) { return $stripe->charges->create($p); }",  # a parameter
    "$p = ['amount' => 1];\nfunction pay($stripe) { return $stripe->charges->create($p); }",  # not this function's
    "function pay($stripe) { $p = ['amount' => 1]; return function () use ($p, $stripe) { return $stripe->charges->create($p); }; }",
    "function pay($stripe) { $p = ['amount' => 1]; return fn ($p) => $stripe->charges->create($p); }",
    "function pay($stripe, $o) { $p = ['amount' => 1]; extract($o); return $stripe->charges->create($p); }",
    "function pay($stripe, $n) { $p = ['amount' => 1]; $$n = []; return $stripe->charges->create($p); }",
    "function pay($stripe, $o) { $p = ['amount' => 1]; $q = &$p; return $stripe->charges->create($p); }",
    "function pay($stripe, $rows) { $p = ['amount' => 1]; foreach ($rows as $p) { $stripe->charges->create($p); } }",
    "function pay($stripe, $rows) { $p = ['amount' => 1]; [$a, $p] = $rows; return $stripe->charges->create($p); }",
    "function pay($stripe, $rows) { $p = ['amount' => 1]; array_push($p, 1); return $stripe->charges->create($p); }",
    "function pay($stripe, $c) { $c && $p = ['amount' => 1]; return $stripe->charges->create($p); }",
    "function pay($stripe, $xs) { $p = ['amount' => 1]; foreach ($xs as $x) { $stripe->charges->create($p); $p = $x; } }",
    "function pay($stripe, $c) { $p = ['amount' => 1]; if ($c): $p = ['currency' => 'x']; endif; return $stripe->charges->create($p); }",
    "function pay($stripe, $k) { switch ($k) { case 1: $p = ['amount' => 1]; break; default: return $stripe->charges->create($p); } }",
    "function pay($stripe) { $p = array_merge(['amount' => 1], defaults()); return $stripe->charges->create($p); }",
])
def test_a_variable_whose_value_is_not_knowable_hides_everything(index, body):
    usages, _, _ = scan_text(H + body + "\n", "a.php", index)
    assert [(sorted(u.fields), u.complete, set(u.hidden)) for u in usages] == [([], False, {""})]


def test_an_arrow_function_sees_the_enclosing_binding_and_a_property_is_not_a_variable(index):
    text = H + (
        "class Pay {\n"
        "    private $p = ['description' => 'a property, not a variable'];\n"
        "    function go($stripe) {\n"
        "        $p = ['amount' => 1];\n"
        "        return fn () => $stripe->charges->create($p);\n"
        "    }\n"
        "}\n"
    )
    assert one(text, index)[3:] == (["amount"], True)


# --- what is not code -----------------------------------------------------------------


def test_calls_in_comments_strings_heredocs_and_html_are_not_calls(index):
    text = (
        "<html><?= $title ?>\n"
        "$stripe->charges->create(['amount' => 1]);\n"  # HTML: outside the PHP tags
        "<?php\n"
        "// $stripe->charges->create(['amount' => 2]);\n"
        "# $stripe->charges->create(['amount' => 3]);\n"
        "/* $stripe->charges->create(['amount' => 4]); */\n"
        "$a = '$stripe->charges->create([\"amount\" => 5]);';\n"
        "$b = \"{$stripe->charges->create(['amount' => 6])}\";\n"
        "$c = <<<EOT\n"
        "$stripe->charges->create(['amount' => 7]);\n"
        "EOT;\n"
        "$d = <<<'SQL'\n"
        "  $stripe->charges->create(['amount' => 8]);\n"
        "  SQL;\n"
        "?>\n"
        "<p>$stripe->charges->create(['amount' => 9]);</p>\n"
        "<?php\n"
        "#[Route('/pay')]\n"
        "function pay($stripe) { return $stripe->charges->create(['amount' => 10]); }\n"
    )
    found, _ = read(text, index)
    assert [(line, fields) for line, _, _, fields, _ in found] == [(19, ["amount"])]


def test_a_file_without_a_php_tag_has_no_code(index):
    assert read("$stripe->charges->create(['amount' => 1]);\n", index) == ([], [])


def test_php_files_are_read_in_a_checkout_and_no_longer_counted_as_unread(tmp_path, index):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "Pay.php").write_text(H + "$stripe->charges->create(['amount' => 1]);\n$r = $charge->receipt_url;\n")
    (tmp_path / "vendor" / "stripe").mkdir(parents=True)
    (tmp_path / "vendor" / "stripe" / "Lib.php").write_text(H + "$stripe->charges->create(['amount' => 1]);\n")
    result = scan_repo(tmp_path, index)
    assert result.unread == {} and result.by_language() == {"php": 1}
    assert [(u.where, u.label) for u in result.usages] == [("app/Pay.php:2", "POST /v1/charges")]


def test_response_reads_are_properties_and_subscripts_not_methods_or_the_request(index):
    text = H + (
        "$charge = $stripe->charges->retrieve($id);\n"
        "$x = [$charge->receipt_url, $charge['status'], $request->amount, $charge->refresh(), 'a' . $b];\n"
    )
    usages, _, _ = scan_text(text, "a.php", index)
    assert usages[0].reads == {"receipt_url", "status"}


# --- damaged and hostile files --------------------------------------------------------

SAMPLE = H + (
    "use Stripe\\{Charge, Refund as R};\n"
    "#[Route('/pay')]\n"
    "class Pay {\n"
    "    public function __construct(private \\Stripe\\StripeClient $client) {}\n"
    "    function go(array $o, $rows) {\n"
    "        $p = ['amount' => 1, 'metadata' => ['a' => \"x {$o['k']}\"], ...$o]; // note\n"
    "        foreach ($rows as [$a, $b]) { $p['currency'] = $a ?: 'usd'; }\n"
    "        $doc = <<<EOT\n  text (\n  EOT;\n"
    "        $svc = $this->client->charges;\n"
    "        return [$svc->create($p), Charge::all(array('limit' => 3)), fn ($x) => $svc->capture($x, [])];\n"
    "    }\n"
    "}\n"
    "?><p>tail</p>\n"
)


def test_the_sample_itself_is_read(index):
    found, _ = read(SAMPLE, index)
    assert [(call, label) for _, call, label, _, _ in found] == [
        ("$svc->create", "POST /v1/charges"), ("Charge::all", "GET /v1/charges"),
        ("$svc->capture", "POST /v1/charges/{charge}/capture"),
    ]


def test_a_file_cut_or_damaged_anywhere_is_read_without_an_error(index):
    """Repositories hold half-written and generated files; the reader must answer for every one of them."""
    from apidrift import phpfix

    for cut in range(len(SAMPLE) + 1):
        for text in (SAMPLE[:cut], SAMPLE[:cut] + SAMPLE[cut + 5:], SAMPLE[:cut] + "'" + SAMPLE[cut:], SAMPLE[:cut] + ")" + SAMPLE[cut:]):
            scan_text(text, "a.php", index)
            phpfix.Locator(text, "php", index)


@pytest.mark.parametrize("head", ["function", "fn", "use ", "class X", "$a = new", "\\Stripe\\Charge::create", "$stripe->charges->"])
def test_a_long_blank_stretch_does_not_cost_its_length_squared(index, head):
    """A pattern with two runs of whitespace that can trade characters is quadratic on a long gap, and an
    unterminated string or comment blanks the rest of a file. 300 KB must still be read at once."""
    import time

    for gap in (" " * 300_000, "\n" * 300_000, "/*" + "x" * 300_000, "'" + "y " * 150_000):
        started = time.monotonic()
        scan_text(H + head + gap + "\n$stripe->charges->create(['amount' => 1]);\n", "a.php", index)
        assert time.monotonic() - started < 5
    started = time.monotonic()
    scan_text(H + "x" * 300_000 + " $y;\n" + "use " * 20_000 + "class " * 20_000 + "function " * 20_000, "a.php", index)
    assert time.monotonic() - started < 5
