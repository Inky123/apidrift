"""Stripe calls made with the app's own HTTP client: literal URLs joined with an id, and wrappers that join
a Stripe base with a path parameter (PHP, Go, Python). Detection only: the fixer never edits them."""

import copy

import pytest

from apidrift import httpurl
from apidrift.openapi import load_spec
from apidrift.scanner import scan_repo, scan_text
from apidrift.sdkmap import SdkIndex
from specs import _form, sdk_doc
from test_fixer import NEW, OLD, docs, plan_for
from apidrift.diff import BREAKING, diff_chain
from apidrift.impact import AFFECTED, assess
from apidrift.report import filter_changes

P = "<?php\n"


def _doc() -> dict:
    doc = sdk_doc()
    paths = doc["paths"]
    form = _form({"amount": {"type": "integer"}, "currency": {"type": "string"}, OLD: {"type": "array"}})
    ok = {"200": {}}
    paths["/v1/payment_intents"] = {"post": {"requestBody": copy.deepcopy(form), "responses": ok}}
    paths["/v1/payment_intents/{intent}"] = {"get": {"responses": ok}, "post": {"requestBody": copy.deepcopy(form), "responses": ok}}
    paths["/v1/setup_intents"] = {"post": {"requestBody": copy.deepcopy(form), "responses": ok}}
    paths["/v1/tokens"] = {"post": {"responses": ok}}
    paths["/v1/tokens/{token}"] = {"get": {"responses": ok}}
    paths["/v1/customers/{customer}/sources"] = {"post": {"responses": ok}}
    paths["/v1/customers/search"] = {"get": {"responses": ok}}
    paths["/v1/files"] = {"post": {"responses": ok}}
    paths["/v1/subscriptions/{subscription}"] = {"get": {"responses": ok}, "post": {"responses": ok}, "delete": {"responses": ok}}
    return doc


@pytest.fixture(scope="module")
def index():
    return SdkIndex.build(load_spec(_doc()))


def sites(text, rel, index):
    return [(u.line, u.method, u.path) for u in scan_text(text, rel, index)[0] if u.language in ("http", "doc")]


def one(text, rel, index):
    found = [u for u in scan_text(text, rel, index)[0] if u.language in ("http", "doc")]
    assert len(found) == 1, found
    return found[0]


# --- literal URLs joined with an expression -------------------------------------------------


@pytest.mark.parametrize("rel,src,expected", [
    ("a.php", P + "$r = wp_remote_get('https://api.stripe.com/v1/tokens/' . $token_id, ['method' => 'GET']);\n",
     ("get", "/v1/tokens/{token}")),
    # PMPro upgrade_1_8_9_1.php:79: the literal ends in "/" before the join; wp_remote_get names GET
    ("a.php", P + "$r = wp_remote_get('https://api.stripe.com/v1/subscriptions/' . $id);\n",
     ("get", "/v1/subscriptions/{subscription}")),
    ("a.php", P + "wp_remote_request('https://api.stripe.com/v1/subscriptions/' . $id, ['method' => 'DELETE']);\n",
     ("delete", "/v1/subscriptions/{subscription}")),
    ("a.php", P + "wp_remote_post('https://api.stripe.com/v1/subscriptions/' . $id, array('method' => 'GET'));\n",
     ("get", "/v1/subscriptions/{subscription}")),
    ("a.php", P + "wp_remote_post('https://api.stripe.com/v1/subscriptions/' . $id, ['body' => $b]);\n",
     ("post", "/v1/subscriptions/{subscription}")),
    ("a.go", 'package x\nfunc f() { http.NewRequest(http.MethodDelete, "https://api.stripe.com/v1/subscriptions/"+id, nil) }\n',
     ("delete", "/v1/subscriptions/{subscription}")),
    ("a.py", 'import stripe, requests\nrequests.post("https://api.stripe.com/v1/subscriptions/" + sid, data={})\n',
     ("post", "/v1/subscriptions/{subscription}")),
    ("a.js", "// stripe\nfetch('https://api.stripe.com/v1/customers/' + id + '/sources', { method: 'POST' })\n",
     ("post", "/v1/customers/{customer}/sources")),
])
def test_a_joined_id_fills_the_next_segment_and_the_call_names_the_method(index, rel, src, expected):
    u = one(src, rel, index)
    assert (u.method, u.path) == expected
    assert u.language == "http" and not u.complete


def test_an_explicit_method_the_path_lacks_is_unknown_rather_than_the_only_method(index):
    u = one(P + "wp_remote_get('https://api.stripe.com/v1/tokens', ['method' => 'GET']); // stripe\n", "a.php", index)
    assert (u.method, u.path) == ("?", "/v1/tokens")


def test_a_hole_glued_to_a_segment_keeps_the_url_as_written(index):
    u = one(P + "wp_remote_post('https://api.stripe.com/v1/payment_intents' . $suffix); // stripe\n", "a.php", index)
    assert u.path == "/v1/payment_intents"  # not guessed to be /v1/payment_intents/{intent}


# --- PHP wrappers ---------------------------------------------------------------------------

API = P + """class My_Stripe_API {
    const ENDPOINT = 'https://api.stripe.com/v1/';
    public static function request($request, $api = 'charges', $method = 'POST') {
        return wp_remote_post(self::ENDPOINT . $api, ['method' => $method, 'body' => $request, 'timeout' => 70]);
    }
    public static function retrieve($api) {
        return wp_remote_get(self::ENDPOINT . $api, ['method' => 'GET']);
    }
    public static function with_level3($request, $api, $level3) {
        if (empty($level3)) {
            return self::request($request, $api);
        }
        $request['level3'] = $level3;
        return self::request($request, $api);
    }
    public static function attach($customer, $pm) {
        return self::request(['customer' => $customer], 'payment_methods/' . rawurlencode($pm) . '/attach');
    }
}
"""

CALLER = P + """class Controller {
    function pay($amount) {
        $request = ['amount' => $amount, 'currency' => 'usd', 'payment_method_types' => ['card']];
        $intent = My_Stripe_API::request($request, 'payment_intents');
        $token = My_Stripe_API::request([], 'tokens/' . $id, 'GET');
        $again = My_Stripe_API::retrieve("payment_intents/$id?expand[]=payment_method");
        $l3 = My_Stripe_API::with_level3(['amount' => 1], 'payment_intents/' . $id);
        $search = My_Stripe_API::request(['query' => 'email:"a"'], 'customers/search', 'GET');
        // My_Stripe_API::request([], 'setup_intents');
        $glued = My_Stripe_API::request([], 'payment_intents' . $maybe_id);
        $dynamic = My_Stripe_API::request([], "{$endpoint}/{$id}");
        $other = Other_API::request(['payment_method_types' => ['card']], 'payment_intents');
        $unknown = My_Stripe_API::request($request, 'payment_intents', $verb);
    }
}
"""


def _repo(tmp_path, files):
    for name, text in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return tmp_path


def test_php_wrapper_calls_in_another_file_are_call_sites_at_the_callers_line(tmp_path, index):
    root = _repo(tmp_path, {"includes/api.php": API, "includes/controller.php": CALLER})
    got = {(u.line, u.method, u.path): u for u in scan_repo(root, index).usages if u.file == "includes/controller.php"}
    assert sorted(got) == [
        (5, "post", "/v1/payment_intents"),
        (6, "get", "/v1/tokens/{token}"),
        (7, "get", "/v1/payment_intents/{intent}"),
        (8, "post", "/v1/payment_intents/{intent}"),
        (9, "get", "/v1/customers/search"),
        (14, "post", "/v1/payment_intents"),  # the method is a variable: the path's only one
    ]
    pi = got[(5, "post", "/v1/payment_intents")]
    assert pi.language == "http" and pi.fields == {"amount", "currency", OLD} and pi.complete
    assert pi.call == "My_Stripe_API::request('payment_intents')"
    assert got[(7, "get", "/v1/payment_intents/{intent}")].fields == {"expand"}
    l3 = got[(8, "post", "/v1/payment_intents/{intent}")]
    assert l3.fields == {"amount", "level3"} and l3.complete  # the wrapper's own literal key is sent too
    token = got[(6, "get", "/v1/tokens/{token}")]
    assert token.fields == frozenset() and token.complete


def test_php_wrapper_calls_inside_the_wrapper_file(index):
    found = sites(API, "includes/api.php", index)
    assert found == []  # attach's path has an id but /v1/payment_methods/... is not in this spec; forwarders are not sites
    doc_index = SdkIndex.build(load_spec(_with(_doc(), "/v1/payment_methods/{payment_method}/attach", "post")))
    u = one(API, "includes/api.php", doc_index)
    assert (u.line, u.method, u.path, u.fields) == (18, "post", "/v1/payment_methods/{payment_method}/attach", {"customer"})


def _with(doc, path, method):
    doc["paths"][path] = {method: {"responses": {"200": {}}}}
    return doc


def test_a_wrapper_that_rewrites_the_body_keeps_the_visible_keys_but_hides_the_rest(index):
    src = API.replace("        return wp_remote_post(", "        $request = apply_filters('body', $request, $api);\n        return wp_remote_post(", 1)
    src += "My_Stripe_API::request(['payment_method_types' => ['card']], 'payment_intents');\n"
    u = one(src, "a.php", index)
    assert u.fields == {OLD} and not u.complete and "" in u.hidden


@pytest.mark.parametrize("src", [
    # the joined URL is only logged, never requested
    P + "function req($api) { error_log('https://api.stripe.com/v1/' . $api); return true; }\nreq('payment_intents');\n",
    # a docs or dashboard link joined with a parameter
    P + "function link($id) { return wp_remote_get('https://dashboard.stripe.com/' . $id); }\nlink('payment_intents');\n",
    P + "function docs($p) { return wp_remote_get('https://stripe.com/docs/api/' . $p); }\ndocs('payment_intents');\n",
    # the method depends on which branch runs
    P + """class A { const E = 'https://api.stripe.com/v1/';
  static function request($b, $api, $m = 'POST') { return wp_remote_post(self::E . $api, ['method' => $m, 'body' => $b]); }
  static function retrieve($api) { return wp_remote_get(self::E . $api); }
  function sr($path, $params = null) { if (is_null($params)) { return self::retrieve($path); } return self::request($params, $path); }
  function go() { $this->sr('payment_intents'); } }
""",
    # a plain function with the same name as a method elsewhere
    P + "request(['payment_method_types' => ['card']], 'payment_intents'); // stripe\n",
])
def test_php_text_that_is_not_a_wrapper_call_is_not_a_call_site(index, src):
    assert [s for s in sites(src, "a.php", index) if s[1:] != ("?", "/v1/payment_intents")] == []


def test_a_path_built_from_a_variable_or_helper_with_literal_values_is_one_site_per_value(index):
    src = API + """class Gateway {
    private function endpoint($id) { return self::is_setup($id) ? 'setup_intents' : 'payment_intents'; }
    function update($id) {
        $endpoint = $this->endpoint($id);
        My_Stripe_API::request(['payment_method_types' => ['card']], "{$endpoint}/{$id}");
    }
    function create_or_update($intent) {
        $suffix = '';
        if ($intent) {
            $suffix = '/' . $intent->id;
        }
        My_Stripe_API::request(['payment_method_types' => ['card']], 'payment_intents' . $suffix);
    }
    function unknown($x) {
        $endpoint = get_option('endpoint');
        My_Stripe_API::request([], "{$endpoint}/{$x}");
    }
}
"""
    got = sorted(sites(src, "a.php", index))
    assert got == [(25, "post", "/v1/payment_intents/{intent}"), (32, "post", "/v1/payment_intents"),
                   (32, "post", "/v1/payment_intents/{intent}")]  # /v1/setup_intents/{intent} is not in this spec


def test_two_expressions_joined_in_a_row_may_carry_a_query(index):
    u = one(API + 'My_Stripe_API::retrieve("payment_intents/{$id}{$query}");\n', "a.php", index)
    assert (u.method, u.path, u.complete) == ("get", "/v1/payment_intents/{intent}", False)


def test_a_class_constant_url_in_an_http_call_is_a_call_site(index):
    src = P + """class Files { const UPLOAD = 'https://files.stripe.com/v1/files';
  function up() { $h = curl_init(self::UPLOAD); curl_setopt_array($h, [CURLOPT_POST => true]); } }
"""
    u = one(src, "a.php", index)
    assert (u.line, u.method, u.path, u.complete) == (3, "post", "/v1/files", False)


# --- Go wrappers ----------------------------------------------------------------------------

GO = """package billing

func stripeRequest(ctx context.Context, method, path string, form url.Values, out any) error {
	var body io.Reader
	if form != nil {
		body = strings.NewReader(form.Encode())
	}
	req, err := http.NewRequestWithContext(ctx, method, "https://api.stripe.com/v1/"+path, body)
	return err
}

func create(ctx context.Context, id string) {
	stripeRequest(ctx, "POST", "payment_intents", url.Values{"amount": {"1"}, "payment_method_types[0]": {"card"}}, nil)
	stripeRequest(ctx, "GET", "subscriptions/"+url.PathEscape(id), nil, nil)
	form := url.Values{}
	form.Set("amount", "1")
	form.Add("payment_method_types[]", "card")
	stripeRequest(ctx, http.MethodPost, "setup_intents", form, nil)
	if id != "" {
		form := url.Values{"payment_method_types[0]": {"card"}}
		_ = form
	}
	other, err := url.ParseQuery(saved)
	stripeRequest(ctx, "POST", "payment_intents", other, nil)
	path := fmt.Sprintf("subscriptions/%s", id)
	stripeRequest(ctx, "DELETE", path, nil, nil)
}
"""


def test_go_wrapper_calls_read_path_method_and_form(index):
    got = {(u.line, u.method, u.path): u for u in scan_text(GO, "billing/sub.go", index)[0] if u.language == "http"}
    assert sorted(got) == [
        (13, "post", "/v1/payment_intents"),
        (14, "get", "/v1/subscriptions/{subscription}"),
        (18, "post", "/v1/setup_intents"),
        (24, "post", "/v1/payment_intents"),
        (26, "delete", "/v1/subscriptions/{subscription}"),
    ]
    assert got[(13, "post", "/v1/payment_intents")].fields == {"amount", OLD} and got[(13, "post", "/v1/payment_intents")].complete
    assert got[(18, "post", "/v1/setup_intents")].fields == {"amount", OLD} and got[(18, "post", "/v1/setup_intents")].complete
    assert not got[(24, "post", "/v1/payment_intents")].complete  # url.ParseQuery: not visible


def test_go_base_in_a_config_field_and_callers_in_the_same_package_only(tmp_path, index):
    gw = """package stripe
const defaultAPIBaseURL = "https://api.stripe.com"
type Config struct { APIBaseURL string }
func (c *Config) Normalize() { c.APIBaseURL = strings.TrimRight(c.APIBaseURL, "/"); if c.APIBaseURL == "" { c.APIBaseURL = defaultAPIBaseURL } }
func doFormRequest(ctx context.Context, cfg *Config, method, path string, form url.Values) error {
	endpoint := strings.TrimRight(strings.TrimSpace(cfg.APIBaseURL), "/") + path
	_, err := http.NewRequestWithContext(ctx, method, endpoint, strings.NewReader(form.Encode()))
	return err
}
"""
    caller = """package stripe
func pay(ctx context.Context, cfg *Config) {
	form := url.Values{}
	form.Add("payment_method_types[]", "card")
	doFormRequest(ctx, cfg, http.MethodPost, "/v1/payment_intents", form)
}
"""
    paypal = """package paypal
const base = "https://api-m.paypal.com"
func doFormRequest(ctx context.Context, cfg *Config, method, path string, form url.Values) error {
	_, err := http.NewRequestWithContext(ctx, method, base+path, nil)
	return err
}
func pay(ctx context.Context, cfg *Config) { doFormRequest(ctx, cfg, http.MethodPost, "/v1/payment_intents", nil) }
"""
    other = 'package other\nfunc x() { doFormRequest(ctx, cfg, "POST", "/v1/payment_intents", nil) }\n'
    root = _repo(tmp_path, {"gw/stripe/stripe.go": gw, "gw/stripe/pay.go": caller, "gw/paypal/paypal.go": paypal,
                            "gw/other/x.go": other})
    found = [(u.file, u.line, u.method, u.path, sorted(u.fields)) for u in scan_repo(root, index).usages]
    assert found == [("gw/stripe/pay.go", 5, "post", "/v1/payment_intents", [OLD])]


# --- Python -----------------------------------------------------------------------------------


def test_python_raw_request_and_a_requests_wrapper(index):
    src = '''import stripe, requests
client = stripe.StripeClient("sk")
STRIPE = "https://api.stripe.com/v1"

def call(path, params=None, method="post"):
    return requests.request(method, f"{STRIPE}/{path}", data=params)

def go(i):
    client.raw_request("post", "/v1/payment_intents", amount=1, payment_method_types=["card"], stripe_version="x")
    call("payment_intents", {"amount": 1})
    call(f"subscriptions/{i}", None, "delete")
    other.raw_request("post", "/v1/payment_intents", amount=1)
'''
    got = {(u.line, u.method, u.path): u for u in scan_text(src, "app/pay.py", index)[0] if u.language == "http"}
    assert sorted(got) == [(9, "post", "/v1/payment_intents"), (10, "post", "/v1/payment_intents"),
                           (11, "delete", "/v1/subscriptions/{subscription}")]
    assert got[(9, "post", "/v1/payment_intents")].fields == {"amount", OLD}
    assert got[(10, "post", "/v1/payment_intents")].fields == {"amount"}


# --- the fixer never edits them -----------------------------------------------------------------


def test_the_fixer_never_edits_a_raw_http_call_site(tmp_path):
    src = API + """function pay() {
    $request = ['amount' => 1, 'payment_method_types' => ['card']];
    $r = My_Stripe_API::request($request, 'payment_intents');
    if (in_array('card', $request['payment_method_types'])) { log_it(); }
}
"""
    plan = plan_for(tmp_path, {"inc/pay.php": src})
    assert plan.fixes == [] and plan.unfixable == [] and plan.not_attempted == []
    assert (tmp_path / "inc/pay.php").read_text() == src
    old, new = (load_spec(d) for d in docs())
    index = SdkIndex.build(old, new)
    result = scan_repo(tmp_path, index)
    hits = [i for i in assess(result.usages, filter_changes(diff_chain([old, new])[0], min_severity=BREAKING))
            if i.change.target == OLD]
    assert [(i.usage.line, i.usage.language, i.certainty) for i in hits] == [(23, "http", AFFECTED)]  # reported


def test_a_fix_whose_array_also_reaches_a_wrapper_is_refused(tmp_path):
    src = API + """function pay() {
    $p = ['amount' => 1, 'payment_method_types' => ['card']];
    \\Stripe\\PaymentIntent::create($p);
    My_Stripe_API::request($p, 'payment_intents');
}
"""
    plan = plan_for(tmp_path, {"inc/pay.php": src})
    assert plan.fixes == []
    assert [u.why for u in plan.unfixable] == ["oracle_changed_other_calls"]


def test_a_fix_beside_a_wrapper_call_leaves_the_wrapper_call_alone(tmp_path):
    src = API + """function pay() {
    \\Stripe\\PaymentIntent::create(['amount' => 1, 'payment_method_types' => ['card']]);
    My_Stripe_API::request(['amount' => 2, 'payment_method_types' => ['card']], 'payment_intents');
}
"""
    plan = plan_for(tmp_path, {"inc/pay.php": src})
    # the SDK call alone could be renamed, but the old key the wrapper call still sends stays in the file:
    # the token backstop refuses the file rather than half-fix it, and the wrapper call is never edited
    assert plan.fixes == []
    assert [u.why for u in plan.unfixable] == ["leftover_tokens"] and plan.leftovers == [("inc/pay.php", 23)]
    assert (tmp_path / "inc/pay.php").read_text() == src


def test_form_keys_become_field_paths():
    assert httpurl.form_paths("line_items[0][price_data][currency]") == [
        "line_items", "line_items[].price_data", "line_items[].price_data.currency"]
    assert httpurl.form_paths("payment_method_types[]") == ["payment_method_types"]
    assert httpurl.form_paths("metadata[tier]") == ["metadata", "metadata.tier"]
    assert httpurl.form_paths("x[$y]") is None
