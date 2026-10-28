# apidrift

Stripe changed its API. This is the line in your code that breaks.

apidrift watches Stripe's published OpenAPI spec. When a release removes a field, retires an
endpoint, or makes a new parameter required, it finds the call sites in your repository that
send exactly that, and opens one GitHub issue with a link to each line. Nothing else: no digest
of every change, no issue when you are unaffected.

Live: <https://142-93-123-94.sslip.io> · Install: <https://github.com/apps/apidrift-check>

## A real example

Stripe's 2026-09-30.endive release removed `payment_method_types` from `POST /v1/payment_intents`.
Run against Stripe's own sample repository, apidrift flags the five servers that still send it:
<https://github.com/Inky123/accept-a-payment/issues/1>

## How it works

1. **Watch.** A worker polls `spec3.sdk.json` from Stripe's `openapi` repository every 30 minutes
   and stores every release. A pure-Python differ classifies each change (breaking, warning,
   informational) and records the release that introduced it.
2. **Scan.** On install, on every push to the default branch, and on every Stripe release, the
   repository tarball is downloaded, scanned for Stripe call sites (Python via `ast`; JavaScript,
   TypeScript and Ruby via a bracket-aware parser; literal `api.stripe.com` URLs anywhere), and
   deleted. SDK method names map to endpoints through the spec's own `x-stripeOperations`.
3. **Cross.** Each call site's request fields are matched against the diff. A removed field hits a
   call only if that call sends it; a field that became required binds only callers of its
   parent. The result is one issue per repository, updated in place, closed when the code is clean.

The code is never executed. Scanning runs in a separate process with no credentials, a memory
cap and a timeout.

## Permissions

Contents: read. Issues: read and write. Metadata: read. Events: push. That is the whole list.

## Run it locally

Python 3.11 or newer.

```bash
git clone https://github.com/Inky123/apidrift && cd apidrift
python3 -m venv .venv && . .venv/bin/activate && pip install -e '.[dev]'
apidrift snapshot stripe --url https://raw.githubusercontent.com/stripe/openapi/v2510/openapi/spec3.sdk.json
apidrift snapshot stripe                                  # current release
apidrift impact /path/to/your/repo --old previous --new latest --severity breaking
```

The first snapshot is an older tagged release (v2510 is 2026-08-26.dahlia) so there is something
to diff against; snapshots are ordered by fetch time, so fetch the older one first. Other commands: `apidrift scan REPO` lists every call site and the fields it
sends; `apidrift diff stripe --old X --new Y` prints the classified spec diff;
`apidrift check --local REPO --json` emits the payload the GitHub App turns into an issue.
`pytest` runs the suite offline.

## Self-host the GitHub App

```bash
apidrift app-setup        # prints the App registration values and the .env lines
```

Register the App at <https://github.com/settings/apps/new> with the values it prints, download the
private key to `secrets/app.pem`, write `.env`, seed `data/snapshots/stripe/` with at least two
releases, then:

```bash
docker compose up -d --build
apidrift status           # installations, repositories, jobs, last poll
```

`compose.yaml` runs two containers (web and worker) behind an existing reverse proxy; the header
comment in that file describes first boot and the proxy site block.

## What it does not do yet

Open the fixing pull request. Vendors other than Stripe. Go, PHP, Java and .NET call sites
(only literal URLs are found there). Stripe clients hidden behind a local wrapper module that
never mentions Stripe by name.
