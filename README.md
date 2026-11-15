# AimoBridge

AimoBridge is a FastAPI purchasing simulation. It searches the supplied local Valio Aimo catalog, quotes at most six matching products through the documented mock availability and price endpoints, and records a **local simulated order**. The supplied OpenAPI contract has no order-creation endpoint. Every order response says `simulation: true` and `upstream_order_submitted: false`.

## Setup

Use Python 3.11 or later. From the repository root:

```powershell
py -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Set `VALIO_AIMO_BASE_URL` to the HTTPS mock API base URL from the challenge portal, `VALIO_AIMO_API_KEY` to your key, and `VALIO_AIMO_CUSTOMER_ID` to the server-controlled customer ID. The provided OpenAPI document has no `servers` entry, so this repository intentionally has no default host. The old `.env` key `VALIO_API_KEY` is not read; use `VALIO_AIMO_API_KEY`. Never commit `.env`.

Set `PURCHASING_TOKENS_JSON` to a JSON object mapping server-issued bearer tokens to identities, for example:

```json
{"buyer-token":{"requester_id":"buyer-1","customer_id":"123","role":"buyer","auto_limit_eur":"500"},"reviewer-token":{"requester_id":"reviewer-1","customer_id":"123","role":"approver"}}
```

This is an MVP credential store for a trusted deployment environment. `auto_limit_eur` is a server-owned policy field. Omit it or set it to zero to disable auto-confirmation. Configure separate buyer and approver identities. For a local demo only, set `DEMO_MODE=true` and send `X-Demo-Identity` and optional `X-Demo-Role` headers; demo buyers always have a zero auto-confirmation limit.

Run:

```powershell
.venv\Scripts\python.exe -m uvicorn backend.app.main:app --reload
```

Open `http://127.0.0.1:8000/` for the test UI or `http://127.0.0.1:8000/docs` for Swagger. The UI lives in `frontend/index.html` and is served by FastAPI at `/`. It lets a buyer evaluate a request, select any viable or uncertain quoted offer, and submit a local simulated order. Excluded offers are disabled. A separate reviewer can approve or reject the order. Paste a proposal or order ID into **Open saved record** to check persisted results after refreshing. With `DEMO_MODE=true`, it uses localhost-only demo identities; with demo mode off, enter server-issued buyer and reviewer bearer tokens. The supplier API key is never sent to the browser. Run offline tests with `.venv\Scripts\python.exe -m pytest -q`; they use `httpx.MockTransport` and never call the challenge backend.

## Continuous integration

[GitHub Actions](./.github/workflows/ci.yml) runs the offline test suite on Python 3.11 and 3.14 for pushes, pull requests, and manual runs. It does not use the Valio Aimo API key or call the challenge backend. The workflow also rejects tracked `.env` files; keep credentials in deployment environment variables. No Git remote is configured yet, so the workflow starts running after this repository is pushed to GitHub.

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
- Product matching currently supports English milk and milk-drink names with declared lactose-free claims. Other product types, languages, and more complex natural-language constraints require an expanded parser and catalog adapter. The deterministic parser supports ISO dates and every weekday name using Europe/Helsinki time. A bare weekday means its next occurrence after today; “next Friday” means one week after that occurrence; “this Friday” means that day in the current Monday–Sunday week and is rejected if it has passed. Requests naming multiple weekdays require clarification.
- The proposal distinguishes sales-unit price from physical litres, rounds up to whole packages, and reports ordered litres. Final EUR totals are rounded half-up to cents. Price and availability are rechecked before reviewer approval; quotes are not reservations or delivery guarantees.
- Without the configured mock host/key, the service returns a static shortlist with `UPSTREAM_NOT_CONFIGURED`; no product is recommended or submitted.
- The raw natural-language request is processed in memory and is not retained. The validated intent, selected offer, policy reason, and audit events are stored in SQLite.
