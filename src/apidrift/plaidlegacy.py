"""Method names of the pre-2021 plaid-node client (``new plaid.Client({...})``, package versions up to 7).

That client was written by hand: ``client.getTransactions(access_token, start, end, options)`` rather than
one generated method per operation taking a request object. It has been frozen since 2021, so the table
is complete: every public method of ``lib/PlaidClient.js`` in ``plaid@7.1.0`` and the endpoint its body
posts to (``createStripeToken`` and ``getAllTransactions`` through the method they call). Arguments are
positional and each method builds the request itself, so a call names its endpoint and no request fields.

The pre-2021 plaid-python client needs no table: its chains spell the path (see :meth:`PlaidIndex.legacy`).
"""

from __future__ import annotations

NODE_METHODS = {
    "createAssetReport": "/asset_report/create", "createAuditCopy": "/asset_report/audit_copy/create",
    "createDepositSwitch": "/deposit_switch/create", "createDepositSwitchToken": "/deposit_switch/token/create",
    "createItemAddToken": "/item/add_token/create", "createLinkToken": "/link/token/create",
    "createPayment": "/payment_initiation/payment/create",
    "createPaymentRecipient": "/payment_initiation/recipient/create",
    "createPaymentToken": "/payment_initiation/payment/token/create",
    "createProcessorToken": "/processor/token/create", "createPublicToken": "/item/public_token/create",
    "createStripeToken": "/processor/token/create", "exchangePublicToken": "/item/public_token/exchange",
    "filterAssetReport": "/asset_report/filter", "getAccounts": "/accounts/get",
    "getAllTransactions": "/transactions/get",
    "getAssetReport": "/asset_report/get", "getAssetReportPdf": "/asset_report/pdf/get",
    "getAuditCopy": "/asset_report/audit_copy/get", "getAuth": "/auth/get", "getBalance": "/accounts/balance/get",
    "getCategories": "/categories/get", "getCreditDetails": "/credit_details/get",
    "getDepositSwitch": "/deposit_switch/get", "getHoldings": "/investments/holdings/get",
    "getIdentity": "/identity/get", "getIncome": "/income/get", "getInstitutionById": "/institutions/get_by_id",
    "getInstitutions": "/institutions/get", "getInvestmentTransactions": "/investments/transactions/get",
    "getItem": "/item/get", "getLiabilities": "/liabilities/get", "getLinkToken": "/link/token/get",
    "getPayment": "/payment_initiation/payment/get", "getPaymentRecipient": "/payment_initiation/recipient/get",
    "getTransactions": "/transactions/get", "getWebhookVerificationKey": "/webhook_verification_key/get",
    "importItem": "/item/import", "invalidateAccessToken": "/item/access_token/invalidate",
    "listPaymentRecipients": "/payment_initiation/recipient/list",
    "listPayments": "/payment_initiation/payment/list", "refreshAssetReport": "/asset_report/refresh",
    "refreshTransactions": "/transactions/refresh", "removeAssetReport": "/asset_report/remove",
    "removeAuditCopy": "/asset_report/audit_copy/remove", "removeItem": "/item/remove",
    "resetLogin": "/sandbox/item/reset_login", "sandboxItemFireWebhook": "/sandbox/item/fire_webhook",
    "sandboxItemSetVerificationStatus": "/sandbox/item/set_verification_status",
    "sandboxPublicTokenCreate": "/sandbox/public_token/create", "searchInstitutionsByName": "/institutions/search",
    "updateItemWebhook": "/item/webhook/update",
}
