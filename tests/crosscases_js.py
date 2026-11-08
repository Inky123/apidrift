OLD = "payment_method_types"

FUNC = (
    "const stripe = require('stripe')('sk');\n"
    "export async function createIntent(params) {\n"
    "  return stripe.paymentIntents.create(params);\n"
    "}\n"
)
SERVICE = (
    "import Stripe from 'stripe';\n"
    "const stripe = new Stripe('sk');\n"
    "export class PaymentService {\n"
    "  async createIntent(params: Stripe.PaymentIntentCreateParams) {\n"
    "    return stripe.paymentIntents.create(params);\n"
    "  }\n"
    "}\n"
)
CALLER = (
    "import { PaymentService } from '../stripe/payment.service';\n"
    "export class Checkout {\n"
    "  constructor(private readonly paymentService: PaymentService) {}\n"
    "  async pay() {\n"
    "    return this.paymentService.createIntent({\n"
    "      amount: 100,\n"
    f"      {OLD}: ['card'],\n"
    "    });\n"
    "  }\n"
    "}\n"
)
FILES = {"src/stripe/payment.service.ts": SERVICE, "src/checkout/checkout.ts": CALLER}

CASES = [
    ("shadowed import param", {
        "lib/billing.js": FUNC,
        "lib/go.js": "import { createIntent } from './billing';\n"
                     f"export const ok = () => createIntent({{ amount: 1, {OLD}: ['card'] }});\n"
                     f"export function other(createIntent) {{ return createIntent({{ amount: 2, {OLD}: ['x'] }}); }}\n",
    }),
    ("shadowed import local", {
        "lib/billing.js": FUNC,
        "lib/go.js": "import { createIntent } from './billing';\n"
                     "import { legacy } from './legacy';\n"
                     "export function other() {\n"
                     "  const createIntent = (p) => legacy.save(p);\n"
                     f"  return createIntent({{ amount: 2, {OLD}: ['x'] }});\n"
                     "}\n",
    }),
    ("string DI token useClass", {
        **FILES,
        "src/checkout/checkout.ts": CALLER.replace(
            "constructor(private readonly paymentService: PaymentService)",
            "constructor(@Inject('PAY') private readonly paymentService: PaymentService)"),
        "src/other.ts": "export class LegacyPay {\n  createIntent(p) { return legacyApi.post('/intents', p); }\n}\n",
        "src/module.ts": "import { LegacyPay } from './other';\nexport const m = { provide: 'PAY', useClass: LegacyPay };\n",
    }),
    ("implements the class", {
        **FILES,
        "src/fake.ts": "import { PaymentService } from './stripe/payment.service';\n"
                       "export class OfflinePay implements PaymentService {\n  async createIntent(p) { return queue.push(p); }\n}\n",
        "src/main.ts": "import { Checkout } from './checkout/checkout';\nimport { OfflinePay } from './fake';\n"
                       "export const c = new Checkout(new OfflinePay());\n",
    }),
    ("decorated framework entrypoint", {
        "src/stripe/payment.service.ts": SERVICE.replace("  async createIntent", "  @OnEvent('intent.create')\n  async createIntent"),
        "src/checkout/checkout.ts": CALLER,
        "src/orders.ts": f"export class Orders {{\n  place() {{ this.events.emit('intent.create', {{ amount: 5, {OLD}: ['card'] }}); }}\n}}\n",
    }),
    ("computed member call", {
        **FILES,
        "src/admin.ts": "import { PaymentService } from './stripe/payment.service';\n"
                        "export class Admin {\n  constructor(private readonly svc: PaymentService) {}\n"
                        f"  run(op: string) {{ return (this.svc as any)[op + 'Intent']({{ amount: 1, {OLD}: ['card'] }}); }}\n}}\n",
    }),
    ("vue sfc caller", {
        "src/billing.ts": FUNC,
        "src/go.ts": f"import {{ createIntent }} from './billing';\nexport const go = () => createIntent({{ amount: 1, {OLD}: ['card'] }});\n",
        "src/Pay.vue": "<script setup lang=\"ts\">\nimport { createIntent } from './billing';\n"
                       f"const pay = () => createIntent({{ amount: 9, {OLD}: ['card'] }});\n</script>\n",
    }),
    ("caller in dist dir", {
        "package.json": "{\"private\": true}",
        "src/billing.ts": FUNC,
        "src/go.ts": f"import {{ createIntent }} from './billing';\nexport const go = () => createIntent({{ amount: 1, {OLD}: ['card'] }});\n",
        "tmp/job.ts": f"import {{ createIntent }} from '../src/billing';\nexport const j = () => createIntent({{ amount: 9, {OLD}: ['card'] }});\n",
    }),
    ("caller passes same local to logger that mutates", {
        **FILES,
        "src/checkout/checkout.ts": CALLER.replace(
            "    return this.paymentService.createIntent({\n      amount: 100,\n" f"      {OLD}: ['card'],\n    }});\n",
            f"    const p = {{ amount: 100, {OLD}: ['card'] }};\n    const r = this.paymentService.createIntent(p);\n    this.log.write(p);\n    return r;\n"),
    }),
    ("caller local captured in closure", {
        **FILES,
        "src/checkout/checkout.ts": CALLER.replace(
            "    return this.paymentService.createIntent({\n      amount: 100,\n" f"      {OLD}: ['card'],\n    }});\n",
            f"    const p = {{ amount: 100, {OLD}: ['card'] }};\n    setTimeout(() => other.send(p));\n    return this.paymentService.createIntent(p);\n"),
    }),
    ("caller local mutated by keyed write after", {
        **FILES,
        "src/checkout/checkout.ts": CALLER.replace(
            "    return this.paymentService.createIntent({\n      amount: 100,\n" f"      {OLD}: ['card'],\n    }});\n",
            f"    const p = {{ amount: 100, {OLD}: ['card'] }};\n    const q = p;\n    return this.paymentService.createIntent(p);\n"),
    }),
    ("wrapper param transformed via spread default", {
        "src/stripe/payment.service.ts": SERVICE.replace("async createIntent(params: Stripe.PaymentIntentCreateParams)",
                                                         "async createIntent(params = { amount: 1 })"),
        "src/checkout/checkout.ts": CALLER,
    }),
    ("call via .call", {
        **FILES,
        "src/x.ts": "import { PaymentService } from './stripe/payment.service';\n"
                    "export class X {\n  constructor(private readonly s: PaymentService) {}\n"
                    f"  run() {{ return this.s.createIntent.call(this.s, {{ amount: 1, {OLD}: ['c'] }}); }}\n}}\n",
    }),
]
