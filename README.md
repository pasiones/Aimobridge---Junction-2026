# AimoBridge

AimoBridge is a FastAPI purchasing simulation. It searches the supplied local Valio Aimo catalog, quotes at most six matching products through the documented mock availability and price endpoints, and records a **local simulated order**. The supplied OpenAPI contract has no order-creation endpoint. Every order response says `simulation: true` and `upstream_order_submitted: false`.

## Setup

Use Python 3.11 or later. From the repository root:

```powershell
py -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
```

Set `VALIO_AIMO_BASE_URL` to the HTTPS mock API base URL from the challenge portal, `VALIO_AIMO_API_KEY` to your key, and `VALIO_AIMO_CUSTOMER_ID` to the server-controlled customer ID. The provided OpenAPI document has no `servers` entry, so this repository intentionally has no default host. The old `.env` key `VALIO_API_KEY` is not read; use `VALIO_AIMO_API_KEY`. Never commit `.env`.

Set `PURCHASING_TOKENS_JSON` to a JSON object mapping server-issued bearer tokens to identities, for example:

```json
{"buyer-token":{"requester_id":"buyer-1","customer_id":"123","role":"buyer","auto_limit_eur":"500"},"reviewer-token":{"requester_id":"reviewer-1","customer_id":"123","role":"approver"}}
```

This is an MVP credential store for a trusted deployment environment. `auto_limit_eur` is a server-owned policy field. Omit it or set it to zero to disable auto-confirmation. Configure separate buyer and approver identities. For a local demo only, set `DEMO_MODE=true` and send `X-Demo-Identity` and optional `X-Demo-Role` headers; demo buyers always have a zero auto-confirmation limit.

For a public test site, set `PUBLIC_DEMO_CREDENTIALS=true` in the deployment environment. The UI then fetches the configured buyer and reviewer bearer tokens from `/api/demo-credentials` and fills both fields automatically. This intentionally gives every visitor both roles, including the ability to approve simulated orders and read records for the same customer. Use demo-only tokens and a demo-only database; leave this setting `false` for private credentials. The supplier API key is never returned by this endpoint.

Run:

```powershell
.venv\Scripts\python.exe -m uvicorn backend.app.main:app --reload
```

Open `http://127.0.0.1:8000/` for the test UI or `http://127.0.0.1:8000/docs` for Swagger. The UI lives in `frontend/index.html` and is served by FastAPI at `/`. It lets a buyer evaluate a request, select any viable or uncertain quoted offer, and submit a local simulated order. Excluded offers are disabled. A separate reviewer can approve or reject the order. Paste a proposal or order ID into **Open saved record** to check persisted results after refreshing. With `DEMO_MODE=true`, it uses localhost-only demo identities; with demo mode off, enter server-issued buyer and reviewer bearer tokens. The supplier API key is never sent to the browser. Run offline tests with `.venv\Scripts\python.exe -m pytest -q`; they use `httpx.MockTransport` and never call the challenge backend.

## Continuous integration

[GitHub Actions](./.github/workflows/ci.yml) runs the offline test suite on Python 3.11 and 3.14 for pushes, pull requests, and manual runs. It does not use the Valio Aimo API key or call the challenge backend. The workflow also rejects tracked `.env` files; keep credentials in deployment environment variables. A production deployment job runs after both test jobs pass when the repository variable `VERCEL_DEPLOY_ENABLED` is `true`.

## Deploy on Vercel

Vercel loads the root [FastAPI entrypoint](./app.py) ([Vercel FastAPI guide](https://vercel.com/docs/frameworks/backend/fastapi)). The catalog stays bundled with the function, while orders and audit history require an external PostgreSQL database: Vercel's writable `/tmp` is temporary, and this app refuses SQLite when `VERCEL` is set. A [Vercel Marketplace PostgreSQL integration](https://vercel.com/docs/marketplace-storage) such as Neon or Supabase can provide the database. Connecting Neon to the **Production** environment normally injects a pooled `DATABASE_URL` automatically. Verify that exact variable name in Vercel's Environment Variables; if the integration uses a prefix or another name, set `DATABASE_URL` to its pooled connection string manually. The tables are created on first startup; use a migration tool before wider production use.

1. Create a Vercel project linked to this GitHub repository. Keep the project root at the repository root. [vercel.json](./vercel.json) disables Vercel's automatic Git deployments so GitHub Actions deploys only after CI passes.
2. Connect the Neon database to Vercel Production and verify that `DATABASE_URL` appears there. Add the remaining Production variables: `VALIO_AIMO_BASE_URL`, `VALIO_AIMO_API_KEY`, `VALIO_AIMO_CUSTOMER_ID`, `PURCHASING_TOKENS_JSON`, and `DEMO_MODE=false`. For public testing, also set `PUBLIC_DEMO_CREDENTIALS=true`; otherwise leave it false. Keep supplier and buyer/reviewer secrets out of GitHub workflow files. The default catalog path is `./data/valio_aimo_junction26_product_data.json`.
3. Create a Vercel access token and link the project with `vercel link`. Copy `orgId` and `projectId` from the generated `.vercel/project.json` into GitHub Actions secrets named `VERCEL_ORG_ID` and `VERCEL_PROJECT_ID`; store the access token as `VERCEL_TOKEN`. The `.vercel/` folder is ignored by Git. See [Vercel's GitHub Actions guide](https://vercel.com/kb/guide/how-can-i-use-github-actions-with-vercel).
4. In GitHub, create the repository variable `VERCEL_DEPLOY_ENABLED=true`. A push to `main` then runs tests and, after success, uses the Vercel CLI to build and deploy production. Configure the GitHub `production` environment with required reviewers if you want a manual deployment gate.

The Vercel project needs an external database before deployment can succeed. The 87 MB catalog is included in the function bundle; verify the final bundle size and startup time in the first Vercel build. The local [`.env`](./.env) and database files are ignored and must not be uploaded.

## API example

```powershell
$h = @{ Authorization = 'Bearer buyer-token' }
$body = @{ text = 'I need 1,000 litres of lactose-free milk by Monday' } | ConvertTo-Json
$response = Invoke-RestMethod http://127.0.0.1:8000/api/purchase-requests -Method Post -Headers $h -ContentType application/json -Body $body
$response.proposal
```

Use the returned proposal ID with `POST /api/proposals/{id}/submit`. Send `{"product_id":"<one of the proposal's viable or uncertain product IDs>"}` to select an offer; omitting it uses the recommendation. Excluded offers cannot be submitted. While an order is `PENDING_APPROVAL`, sending a different eligible product ID to the same endpoint updates that order and records `OFFER_CHANGED` in its audit history. The UI shows the newly selected offer as an unsaved preview until you click **Update pending order**. Confirmed and rejected orders cannot be changed. Read the local order at `GET /api/orders/{id}`. A separate approver can call `POST /api/orders/{id}/approve` or `/reject`. If the offer is marked uncertain, approval requires `{"acknowledge_uncertainty":true}` and refreshes price and availability first. If they change, the API returns `stale_quote`; make a new request. Quotes expire after `QUOTE_TTL_MINUTES` (default 15).

## Catalog mapping and limitations

- `id` is the mock API product code. `synkkaData.names` supplies display names. `synkkaData.classifications` with `nutritionalClaim` / `FREE_FROM_LACTOSE` / `FREE_FROM` establishes the lactose-free constraint.
- `salesUnit` and `baseUnit` are technical unit codes. `units[].sizeInBaseUnits` identifies sales-unit packaging. `synkkaData.unitConversions[].volume` is used only when a matching `BASE_UNIT_OR_EACH` or `CASE` volume has `LTR` or `MLT`. Missing or incompatible volume yields `quantity_conversion_unknown`.
- The catalog contains 17,546 entries. Search is local; only up to six candidates go to each dynamic endpoint. The static data is not a stock or price source.
- `Stock` quantities are compared in verified sales units. `Terminal` amounts are unconfirmed supplier inventory and require human review. For a **by-date deadline**, an earlier `availableDate` can be proposed as the actual delivery date only after availability and price are rechecked for that earlier date. The proposal and order display both dates. For an **exact date** request, an earlier `availableDate` leaves that exact date unconfirmed. Missing cutoff or unknown logistics also requires review. A later date, passed cutoff, block, insufficient stock, or invalid price excludes an offer.
- Product matching currently supports English milk and milk-drink names. A lactose-free request requires a declared lactose-free claim; a plain milk request favors regular milk and does not give lactose-free products a ranking bonus. Other product types, languages, and more complex natural-language constraints require an expanded parser and catalog adapter. The deterministic parser supports ISO dates and every weekday name using Europe/Helsinki time. A bare weekday means its next occurrence after today; “next Friday” means one week after that occurrence; “this Friday” means that day in the current Monday–Sunday week and is rejected if it has passed. Requests naming multiple weekdays require clarification.
- The proposal distinguishes sales-unit price from physical litres, rounds up to whole packages, and reports ordered litres. Final EUR totals are rounded half-up to cents. Price and availability are rechecked before reviewer approval; quotes are not reservations or delivery guarantees.
- Without the configured mock host/key, the service returns a static shortlist with `UPSTREAM_NOT_CONFIGURED`; no product is recommended or submitted.
- The raw natural-language request is processed in memory and is not retained. The validated intent, selected offer, policy reason, and audit events are stored in SQLite.
