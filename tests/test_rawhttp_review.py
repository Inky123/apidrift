"""Raw-HTTP wrappers: the independent review's repros (scratchpad/review-rawhttp r1-r5, fx), one per item.

The rule throughout: what cannot be proved is hidden, or the method is ``?``; nothing is guessed.
"""

import pytest

from apidrift import httpwrap
from apidrift.scanner import scan_repo, scan_text
from test_fixer import OLD, plan_for
from test_rawhttp import API, P, _repo, index, one  # noqa: F401  (index is a fixture)


def repo_sites(tmp_path, index, files):
    root = _repo(tmp_path, files)
    result = scan_repo(root, index)
    return [(u.file, u.line, u.method, u.path, sorted(u.fields), u.complete) for u in result.usages
            if u.language in ("http", "doc")], result


# --- policy: apply_filters passes the body through unless the checkout hooks it ---------------

FILTERED = API.replace(
    "        return wp_remote_post(",
    "        $request = apply_filters('my_body', $request, $api);\n"
    "        $request = apply_filters_deprecated('old_body', [$request, $api], '1.0', 'my_body');\n"
    "        return wp_remote_post(", 1)
CALL_PI = P + "My_Stripe_API::request(['payment_method_types' => ['card']], 'payment_intents');\n"


@pytest.mark.parametrize("extra,complete", [
    ({}, True),  # policy: a literal hook nothing in the checkout hooks into passes the body through
    ({"tests/hooks.php": P + "add_filter('my_body', function ($b) { return $b; });\n"}, False),
    ({"x/hooks.php": P + "add_filter( \"old_body\", 'cb' );\n"}, False),
])
def test_apply_filters_passes_the_body_through_unless_the_checkout_hooks_it(tmp_path, index, extra, complete):
    root = _repo(tmp_path, {"inc/api.php": FILTERED, "inc/pay.php": CALL_PI, **extra})
    [u] = [u for u in scan_repo(root, index).usages if u.file == "inc/pay.php"]
    assert u.fields == {OLD} and u.complete == complete


def test_a_filter_hook_that_is_not_a_literal_hides_the_body(index):
    src = API.replace("        return wp_remote_post(", "        $request = apply_filters($hook, $request);\n        return wp_remote_post(", 1)
    u = one(src + "My_Stripe_API::request(['amount' => 1], 'payment_intents');\n", "a.php", index)
    assert not u.complete


def test_phpfilters_api():
    from apidrift import phpfilters

    hooks = phpfilters.registered({"a.php": "<?php add_filter('a', 'f'); add_filter( \"b\" , $g); add_filter($x, 'h');"})
    assert hooks == {"a", "b"}
    assert phpfilters.pass_through("c", hooks) and not phpfilters.pass_through("a", hooks)
    assert not phpfilters.pass_through(None, hooks)


# --- item 3: cross-file matching proves which function is called -------------------------------

NS_WRAP = P + """namespace Stripe;
class API {
    const ENDPOINT = 'https://api.stripe.com/v1/';
    public static function request($request, $api = 'charges', $method = 'POST') {
        return wp_remote_post(self::ENDPOINT . $api, ['method' => $method, 'body' => $request]);
    }
}
"""


def test_php_static_calls_resolve_the_namespace(tmp_path, index):
    paypal = P + """namespace PayPal;
class API { public static function request($r, $api) { return wp_remote_post('https://api.paypal.com/v1/' . $api, ['body' => $r]); } }
"""
    files = {
        "stripe/api.php": NS_WRAP, "paypal/api.php": paypal,
        "a.php": P + "\\PayPal\\API::request(['amount' => 1], 'charges');\n",
        "b.php": P + "namespace Shop;\nuse Stripe\\API;\nAPI::request(['amount' => 2], 'charges');\n",
        "c.php": P + "namespace Shop;\nAPI::request(['amount' => 3], 'charges');\n",  # Shop\\API: not Stripe's
        "d.php": P + "use Stripe\\API as S;\nS::request(['amount' => 4], 'charges');\n",
    }
    got, _ = repo_sites(tmp_path, index, files)
    assert [(f, line, fields) for f, line, _, _, fields, _ in got] == [("b.php", 4, ["amount"]), ("d.php", 3, ["amount"])]


def test_php_this_calls_only_in_the_wrappers_class_or_a_subclass_it_names(tmp_path, index):
    client = P + """namespace Stripe;
class Client {
    const BASE = 'https://api.stripe.com/v1/';
    public function request($body, $path) { return wp_remote_post(self::BASE . $path, ['body' => $body]); }
}
"""
    files = {
        "stripe/client.php": client,
        "mc/client.php": P + "namespace Mailchimp;\nclass Client {\n  function add($e) { return $this->request(['email' => $e], 'charges'); }\n}\n",
        "stripe/sub.php": P + "namespace Stripe;\nclass Sub extends Client {\n  function pay() { return $this->request(['amount' => 1], 'charges'); }\n}\n",
    }
    got, _ = repo_sites(tmp_path, index, files)
    assert [(f, line) for f, line, *_ in got] == [("stripe/sub.php", 4)]


def test_python_plain_names_resolve_imports(tmp_path, index):
    files = {
        "app/stripe_api.py": 'import requests\nSTRIPE = "https://api.stripe.com/v1/"\n'
                             "def post(path, params):\n    return requests.post(STRIPE + path, data=params)\n",
        "app/crm.py": "from hubspot import post\npost('charges', {'name': 'x'})\n",
        "app/pay.py": "from app.stripe_api import post\npost('charges', {'amount': 1})\n",
        "app/rel.py": "from .stripe_api import post as stripe_post\nstripe_post('charges', {'amount': 2})\n",
        "app/mod.py": "from app import stripe_api\nstripe_api.post('charges', {'amount': 3})\n",
    }
    got, _ = repo_sites(tmp_path, index, files)
    assert [(f, line) for f, line, *_ in got] == [("app/mod.py", 2), ("app/pay.py", 2), ("app/rel.py", 2)]


def test_go_dotted_calls_check_the_receiver_type(tmp_path, index):
    files = {
        "pay/a.go": 'package pay\n\nconst base = "https://api.stripe.com/v1/"\n\ntype Client struct{}\n\n'
                    "func (c *Client) Post(path string, form url.Values) { http.PostForm(base+path, form) }\n"
                    'func (c *Client) Refund() { c.Post("charges", url.Values{"amount": {"1"}}) }\n',
        "pay/b.go": 'package pay\n\nfunc other(hs *HubSpot, c *Client) {\n\ths.Post("charges", nil)\n\tc.Post("charges", nil)\n'
                    '\tx := &Client{}\n\tx.Post("charges", nil)\n}\n',
    }
    got, _ = repo_sites(tmp_path, index, files)
    assert [(f, line) for f, line, *_ in got] == [("pay/a.go", 8), ("pay/b.go", 5), ("pay/b.go", 7)]


# --- item 4: a Go base field is tied to its struct type, package-wide -----------------------------

GO_DO = ('func (p *{T}) do(method, path string) {{ req, _ := http.NewRequest(method, p.BaseURL+path, nil); _ = req }}\n'
         'func (p *{T}) Refund() {{ p.do("POST", "charges") }}\n')


def test_go_field_shared_by_two_structs(tmp_path, index):
    src = ('package pay\n\ntype Stripe struct{ BaseURL string }\ntype Paypal struct{ BaseURL string }\n\n'
           'func NewStripe() *Stripe { s := &Stripe{}; s.BaseURL = "https://api.stripe.com/v1/"; return s }\n'
           "func NewPaypal(u string) *Paypal { return &Paypal{BaseURL: u} }\n\n" + GO_DO.format(T="Paypal"))
    got, _ = repo_sites(tmp_path, index, {"pay/clients.go": src})
    assert got == []  # Paypal.BaseURL is set from a parameter; only Stripe.BaseURL is Stripe's


def test_go_field_of_a_struct_only_ever_set_to_a_stripe_base(tmp_path, index):
    src = ('package pay\n\ntype Stripe struct{ BaseURL string }\n\n'
           'func NewStripe() *Stripe { return &Stripe{BaseURL: "https://api.stripe.com/v1/"} }\n' + GO_DO.format(T="Stripe"))
    got, _ = repo_sites(tmp_path, index, {"pay/clients.go": src})
    assert [(line, method, path) for _, line, method, path, *_ in got] == [(7, "post", "/v1/charges")]


def test_go_field_set_to_another_host_elsewhere_in_the_package(tmp_path, index):
    a = ('package pay\n\ntype Client struct{ BaseURL string }\n\n'
         'func (c *Client) init() { if c.BaseURL == "" { c.BaseURL = "https://api.stripe.com/v1/" } }\n'
         'func (c *Client) call(method, path string) { req, _ := http.NewRequest(method, c.BaseURL+path, nil); _ = req }\n'
         'func (c *Client) Pay() { c.call("POST", "charges") }\n')
    b = 'package pay\n\nfunc useOther() {\n\tc := &Client{}\n\tc.BaseURL = "https://api.example.com/v1/"\n\tc.call("POST", "charges")\n}\n'
    got, _ = repo_sites(tmp_path, index, {"pay/a.go": a, "pay/b.go": b})
    assert got == []
    got, _ = repo_sites(tmp_path / "t", index, {"pay/a.go": a, "pay/b_test.go": b})  # a test's mock host does not count
    assert [(f, line) for f, line, *_ in got] == [("pay/a.go", 7), ("pay/b_test.go", 6)]


# --- item 5: the oracle sees another file's wrapper call -------------------------------------------


def test_a_fix_whose_array_feeds_another_files_wrapper_is_refused(tmp_path):
    use = P + """function pay() {
    $p = ['amount' => 1, 'payment_method_types' => ['card']];
    \\Stripe\\PaymentIntent::create($p);
    My_Stripe_API::request($p, 'payment_intents');
}
"""
    plan = plan_for(tmp_path, {"inc/api.php": API, "inc/pay.php": use})
    assert plan.fixes == [] and [(u.file, u.why) for u in plan.unfixable] == [("inc/pay.php", "oracle_changed_other_calls")]
    assert (tmp_path / "inc/pay.php").read_text() == use


# --- item 6: the rescan cap ---------------------------------------------------------------------


def test_go_callers_are_found_among_many_unrelated_files(tmp_path, index):
    files = {
        "pkg/billing/client.go": 'package billing\n\ntype Client struct{}\n\nfunc (c *Client) do(method, path string) {\n'
                                 '\treq, _ := http.NewRequest(method, "https://api.stripe.com/v1/"+path, nil)\n\t_ = req\n}\n',
        "pkg/billing/use.go": 'package billing\n\nfunc (c *Client) Cancel(id string) { c.do("DELETE", "charges/"+id) }\n',
    }
    files.update({f"aaa/f{n}.go": "package aaa\n\nfunc f() { http.DefaultClient.Do(req) }\n" for n in range(450)})
    got, result = repo_sites(tmp_path, index, files)
    assert [(f, line, method) for f, line, method, *_ in got] == [("pkg/billing/use.go", 3, "?")]
    assert httpwrap.OVER_CAP not in result.unread


def test_callers_past_the_cap_are_reported_not_dropped(tmp_path, index, monkeypatch):
    monkeypatch.setattr(httpwrap, "MAX_FILES", 2)
    files = {"inc/api.php": API}
    files.update({f"inc/c{n}.php": P + f"My_Stripe_API::request(['amount' => {n}], 'charges');\n" for n in range(4)})
    got, result = repo_sites(tmp_path, index, files)
    assert len(got) == 1 and result.unread[httpwrap.OVER_CAP] == 3  # the wrapper's own file and one caller fit


# --- item 7: keys a wrapper writes only on some paths --------------------------------------------


@pytest.mark.parametrize("cond,path,fields,hidden", [
    ("$api === 'payment_intents'", "charges", ["email"], set()),  # decided from the literal path: not sent
    ("$api === 'charges'", "charges", ["email", "expand"], set()),  # decided: sent
    ("$flag", "charges", ["email"], {"expand"}),  # not decidable: hidden, never claimed
])
def test_php_conditional_wrapper_writes(index, cond, path, fields, hidden):
    src = P + f"""class API {{
    const ENDPOINT = 'https://api.stripe.com/v1/';
    public static function request($request, $api, $method = 'POST') {{
        if ({cond}) {{ $request['expand'] = ['latest_charge']; }}
        return wp_remote_post(self::ENDPOINT . $api, ['method' => $method, 'body' => $request]);
    }}
}}
API::request(['email' => 'x'], '{path}');
"""
    u = one(src, "a.php", index)
    assert sorted(u.fields) == fields and set(u.hidden) == hidden


def test_go_conditional_wrapper_writes(index):
    src = ('package pay\n\nconst base = "https://api.stripe.com/v1/"\n\n'
           "func stripeDo(method, path string, form url.Values) {\n"
           '\tif path == "payment_intents" { form.Set("expand[]", "latest_charge") }\n'
           '\tif debug { form.Set("metadata[debug]", "1") }\n'
           "\thttp.NewRequest(method, base+path, strings.NewReader(form.Encode()))\n}\n\n"
           'func f() { stripeDo("POST", "charges", url.Values{"amount": {"1"}}) }\n')
    u = one(src, "pay/a.go", index)
    assert u.fields == {"amount"} and u.hidden == {"metadata", "metadata.debug"}


# --- item 10: one site per path value, fields under the condition hidden ---------------------------


def test_fields_added_after_the_literal_are_hidden_when_the_path_names_several_endpoints(index):
    src = API + """class G {
    private function endpoint($id) { return self::is_setup($id) ? 'payment_intents' : 'charges'; }
    function update($id, $types) {
        $request = ['amount' => 1];
        if ($types) { $request['payment_method_types'] = $types; }
        $endpoint = $this->endpoint($id);
        My_Stripe_API::request($request, $endpoint);
    }
}
"""
    found = sorted((u.path, sorted(u.fields), sorted(u.hidden)) for u in scan_text(src, "a.php", index)[0]
                   if u.language == "http")
    assert found == [("/v1/charges", ["amount"], ["", OLD]), ("/v1/payment_intents", ["amount"], ["", OLD])]
