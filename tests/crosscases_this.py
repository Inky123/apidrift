from crosscases_js import SERVICE, OLD
CALLER2 = (
    "import { PaymentService } from '../stripe/payment.service';\n"
    "export class Checkout {\n"
    "  constructor(private readonly paymentService: PaymentService) {}\n"
    "  handlers = {\n"
    "    paymentService: legacyQueue,\n"
    f"    pay() {{ return this.paymentService.createIntent({{ amount: 1, {OLD}: ['card'] }}); }},\n"
    "  };\n"
    "}\n"
)
CALLER3 = (
    "import { PaymentService } from '../stripe/payment.service';\n"
    "export class Checkout {\n"
    "  constructor(private readonly paymentService: PaymentService) {}\n"
    "  run(items, other) {\n"
    f"    items.forEach(function () {{ this.paymentService.createIntent({{ amount: 1, {OLD}: ['card'] }}); }}, other);\n"
    "  }\n"
    "}\n"
)
CASES = [
    ("object literal this", {"src/stripe/payment.service.ts": SERVICE, "src/checkout/checkout.ts": CALLER2}),
    ("function this thisArg", {"src/stripe/payment.service.ts": SERVICE, "src/checkout/checkout.ts": CALLER3}),
]
