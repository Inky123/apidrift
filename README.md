# apidrift

Stripe changed its API. This is the line in your code that breaks.

apidrift watches Stripe's published OpenAPI spec. When a release removes a field, retires an
endpoint, or makes a new parameter required, it finds the call sites in your repository that
send exactly that, and opens one GitHub issue with a link to each line. Nothing else: no digest
of every change, no issue when you are unaffected.

Live: <https://142-93-123-94.sslip.io> · Install: <https://github.com/apps/apidrift-check>

## A real example

Stripe's 2026-09-30.endive release removed `payment_method_types` from `POST /v1/payment_intents`;
against Stripe's own sample repository, apidrift flags five servers: <https://github.com/Inky123/accept-a-payment/issues/1>

## How it works

1. **Watch.** A worker polls `spec3.sdk.json` from Stripe's `openapi` repository every 30 minutes
   and stores every release. A pure-Python differ classifies each change (breaking, warning,
   informational) and records the release that introduced it.
2. **Scan.** On install, on every push to the default branch, and on every Stripe release, the
   repository tarball is downloaded, scanned for Stripe call sites (Python via `ast`; JavaScript,
   TypeScript and Ruby via a bracket-aware parser; literal `api.stripe.com` URLs anywhere), and
   deleted. SDK method names map to endpoints through the spec's own `x-stripeOperations`. The code
   is never executed: scanning runs in a separate process with no credentials, a memory cap and timeout.
3. **Cross.** Each call site's request fields are matched against the diff. A removed field hits a
   call only if that call sends it; a field that became required binds only callers of its
   parent. The result is one issue per repository, updated in place, closed when the code is clean.
4. **Fix.** When Stripe's changelog documents what to send instead (a renamed field, a removed
   field, or a value that changed shape), the same process rewrites the request at every affected
   call site and re-scans the changed files; one pull request per repository is then opened from a
   branch `apidrift/stripe-<version>`. It lists the API-version pins it found and asks to be merged
   with the upgrade. `apidrift fix DIR --old X --new Y` is the dry run: it prints the diff.

## Permissions

Contents: read and write. Pull requests: read and write. Issues: read and write. Metadata: read.
Events: push. Writes: one branch and one pull request per repository, never the default branch.

## Run it locally (Python 3.11 or newer)

```bash
git clone https://github.com/Inky123/apidrift && cd apidrift
python3 -m venv .venv && . .venv/bin/activate && pip install -e '.[dev]'
apidrift snapshot stripe --url https://raw.githubusercontent.com/stripe/openapi/v2510/openapi/spec3.sdk.json
apidrift snapshot stripe                                  # current release
apidrift impact /path/to/your/repo --old previous --new latest --severity breaking
```

The first snapshot is an older tagged release (v2510 is 2026-08-26.dahlia) so there is something to
diff against; fetch the older one first, since snapshots are ordered by fetch time. `apidrift scan
REPO` lists every call site and its fields, `apidrift diff stripe --old X --new Y` the classified
spec diff, `apidrift check --local REPO --json` the App's payload; `pytest` runs the suite offline.

## Self-host the GitHub App

```bash
apidrift app-setup        # prints the App registration values and the .env lines
```

Register the App at <https://github.com/settings/apps/new> with the values it prints, save the
private key as `secrets/app.pem`, write `.env`, seed `data/snapshots/stripe/` with two releases, then:

```bash
docker compose up -d --build
apidrift status           # installations, repositories, jobs, last poll
```

`compose.yaml` runs two containers (web and worker) behind an existing reverse proxy; the header
comment in that file describes first boot and the proxy site block.

## What it does not do yet

Fix what Stripe has not documented a replacement for. The pull request covers renamed fields, removed
fields and fields whose value changed shape (today: `payment_method_types`, `payto`, `filters.countries`,
`billing_cycle_anchor`); anything else, such as a new required field whose value only you know, still
arrives as an issue. Vendors other than Stripe. Go, PHP, Java and .NET (literal URLs only; the issue
says so when such files mention Stripe). Stripe clients behind a local wrapper that never names Stripe.
