import json
import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app.core import Catalog, evaluate, litres_per_sales_unit, parse_intent, sales_units_needed
from backend.app.core import DomainError
from backend.app.main import Settings, create_app
from backend.app.storage import make_session_factory, normalize_database_url
from backend.app.valio import ValioClient

ROOT = Path(__file__).resolve().parents[2]
HELSINKI = ZoneInfo("Europe/Helsinki")
CATALOG = ROOT / "data/valio_aimo_junction26_product_data.json"


@pytest.fixture(scope="module")
def product():
    return next(p for p in json.loads(CATALOG.read_text(encoding="utf-8")) if p["id"] == "462775")


def test_intent_monday_and_hard_filter(product):
    intent = parse_intent("I need 1,000 litres of lactose-free milk by Monday", datetime(2026, 11, 15, 12, tzinfo=HELSINKI))
    assert intent["requested_delivery_date"] == "2026-11-16"
    assert intent["constraints"]["must_be_lactose_free"]
    shortlist = Catalog(CATALOG).shortlist(intent)
    assert shortlist and all(c["lactose_free_verified"] for c in shortlist)
    assert all("chocolate" not in c["name"].lower() for c in shortlist)
    assert litres_per_sales_unit(product) == Decimal("1.0")
    assert sales_units_needed(Decimal("1000"), Decimal("1"), Decimal("0.01")) == 1000
    assert sales_units_needed(Decimal("1000"), Decimal("12"), Decimal("1")) == 84


def test_evaluation_stock_price_and_uncertainty(product):
    candidate = {"product_id": product["id"], "name": "milk", "sales_unit": "ST", "litres_per_sales_unit": "1"}
    intent = {"quantity": "1000", "requested_delivery_date": "2026-11-16"}
    avail = {"salesBlock": False, "logisticModel": "Stock", "closingTime": "2026-11-15T23:00:00+02:00",
             "scheduleLines": [{"salesBlock": False, "availableAmount": 1000, "availableDate": "2026-11-16", "salesUnit": "ST"}]}
    price = {"quantity": 1, "error": False, "netPriceFound": False,
             "catalogPrice": {"value": "1.19", "valueWithTax": "1.35", "validFrom": "2026-01-01", "validTo": "2026-12-31", "currency": "EUR"},
             "priceWithTax": {"value": "1.35", "validFrom": "2026-01-01", "validTo": "2026-12-31", "currency": "EUR"}}
    now = datetime(2026, 11, 15, 12, tzinfo=HELSINKI)
    result = evaluate(candidate, product, intent, avail, price, "EUR", now)
    assert result["status"] == "viable" and result["subtotal_ex_tax"] == "1190.00" and result["total_inc_tax"] == "1350.00"
    assert result["sales_units_needed"] == 1000
    assert evaluate(candidate, product, intent, {**avail, "logisticModel": "Terminal"}, price, "EUR", now)["status"] == "uncertain"
    assert "insufficient_stock" in evaluate(candidate, product, intent, {**avail, "scheduleLines": [{**avail["scheduleLines"][0], "availableAmount": 999}]}, price, "EUR", now)["reasons"]
    assert "cutoff_passed" in evaluate(candidate, product, intent, avail, price, "EUR", now + timedelta(days=1))["reasons"]
    assert "invalid_price" in evaluate(candidate, product, intent, avail, {**price, "error": True}, "EUR", now)["reasons"]


@pytest.mark.asyncio
async def test_valio_payload_ids_and_header():
    observed = []
    def responder(request):
        observed.append((request.url.path, dict(request.headers), json.loads(request.content)))
        if request.url.path.endswith("availability"):
            return httpx.Response(200, json={"timeNow": "2026-11-15T12:00:00+02:00", "requestedDate": "2026-11-16T00:00:00+02:00",
                "products": [{"productId": "462775", "salesBlock": False, "scheduleLines": []}]})
        return httpx.Response(200, json={"customerId": "123", "currency": "EUR", "pricingDate": "2026-11-16",
                "products": [{"productId": "462775", "quantity": 1, "netPriceFound": False, "error": True}]})
    client = ValioClient("https://example.invalid", "secret", httpx.MockTransport(responder))
    try:
        await client.quote("123", datetime(2026, 11, 16).date(), ["462775"])
    finally:
        await client.close()
    assert [o[0] for o in observed] == ["/api/availability", "/api/price"]
    assert observed[0][2] == observed[1][2] == {"customer": "123", "requestedDate": "2026-11-16", "products": ["462775"]}
    assert all(o[1]["x-api-key"] == "secret" for o in observed)


@pytest.mark.asyncio
async def test_valio_rejects_unexpected_ids_and_batch_limit():
    def responder(request):
        if request.url.path.endswith("availability"):
            return httpx.Response(200, json={"timeNow": "2026-11-15T12:00:00+02:00", "requestedDate": "2026-11-16",
                "products": [{"productId": "wrong-id", "salesBlock": False, "scheduleLines": []}]})
        return httpx.Response(200, json={"customerId": "123", "currency": "EUR", "pricingDate": "2026-11-16",
            "products": [{"productId": "462775", "quantity": 1, "netPriceFound": False, "error": True}]})
    client = ValioClient("https://example.invalid", "secret", httpx.MockTransport(responder))
    try:
        with pytest.raises(DomainError, match="missing, duplicate, or unexpected"):
            await client.quote("123", datetime(2026, 11, 16).date(), ["462775"])
        with pytest.raises(DomainError, match="1–10"):
            await client.quote("123", datetime(2026, 11, 16).date(), [str(i) for i in range(11)])
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_valio_does_not_forward_api_key_on_redirect():
    seen = []
    def responder(request):
        seen.append(request.url.host)
        return httpx.Response(302, headers={"Location": "https://other.example/receive"})
    client = ValioClient("https://api.example", "dummy-secret", httpx.MockTransport(responder))
    try:
        with pytest.raises(DomainError) as error:
            await client.quote("123", datetime(2026, 11, 16).date(), ["462775"])
        assert error.value.code == "upstream_error"
    finally:
        await client.close()
    assert seen == ["api.example"]


def test_date_intent_validation():
    now = datetime(2026, 11, 15, 12, tzinfo=HELSINKI)
    assert parse_intent("1 L milk by Tuesday", now)["delivery_mode"] == "deadline"
    assert parse_intent("1 L milk on Tuesday", now)["delivery_mode"] == "exact"
    assert parse_intent("1 L milk next Monday", now)["requested_delivery_date"] == "2026-11-23"
    assert parse_intent("1 L milk next Tuesday", now)["requested_delivery_date"] == "2026-11-24"
    monday = datetime(2026, 11, 16, 12, tzinfo=HELSINKI)
    assert parse_intent("1 L milk this Friday", monday)["requested_delivery_date"] == "2026-11-20"
    with pytest.raises(DomainError) as error:
        parse_intent("1 L milk by 2026-11-14", now)
    assert error.value.code == "invalid_intent"
    with pytest.raises(DomainError) as error:
        parse_intent("1 L milk this Saturday", now)
    assert error.value.code == "invalid_intent"
    with pytest.raises(DomainError) as error:
        parse_intent("1 L milk Monday or Tuesday", now)
    assert error.value.code == "needs_clarification"
    with pytest.raises(DomainError) as error:
        parse_intent("milk by Monday", now)
    assert error.value.code == "needs_clarification"


@pytest.mark.parametrize(("weekday", "expected"), [
    ("Monday", "2026-11-16"), ("Tuesday", "2026-11-17"),
    ("Wednesday", "2026-11-18"), ("Thursday", "2026-11-19"),
    ("Friday", "2026-11-20"), ("Saturday", "2026-11-21"),
    ("Sunday", "2026-11-22"),
])
def test_each_weekday(weekday, expected):
    now = datetime(2026, 11, 15, 12, tzinfo=HELSINKI)
    assert parse_intent(f"1 L milk by {weekday}", now)["requested_delivery_date"] == expected


def test_migrates_legacy_request_table(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE purchase_requests (id TEXT PRIMARY KEY, raw_text TEXT NOT NULL)")
    make_session_factory(f"sqlite:///{path}")
    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(purchase_requests)")}
    assert "raw_text" not in columns


def test_vercel_requires_persistent_database(monkeypatch):
    assert normalize_database_url("postgres://example/db") == "postgresql+psycopg://example/db"
    assert normalize_database_url("postgresql://example/db") == "postgresql+psycopg://example/db"
    monkeypatch.setenv("VERCEL", "1")
    with pytest.raises(RuntimeError, match="persistent external DATABASE_URL"):
        make_session_factory("sqlite:///./orders.db")


def test_deadline_requotes_earlier_delivery_date(tmp_path):
    catalog = Catalog(CATALOG)
    units = {p["id"]: p["salesUnit"] for p in catalog.products.values()}
    calls = []
    def responder(request):
        payload = json.loads(request.content)
        calls.append((request.url.path, payload["requestedDate"]))
        quote_date = payload["requestedDate"]
        if request.url.path.endswith("availability"):
            return httpx.Response(200, json={"timeNow": "2099-11-15T12:00:00+02:00", "requestedDate": quote_date,
                "products": [{"productId": pid, "salesBlock": False, "logisticModel": "Stock",
                              "closingTime": "2099-11-15T23:00:00+02:00",
                              "scheduleLines": [{"salesBlock": False, "availableAmount": 100000,
                                                 "availableDate": "2099-11-16", "salesUnit": units[pid]}]}
                             for pid in payload["products"]]})
        ex, inc = (1, 1.1) if quote_date == "2099-11-16" else (2, 2.2)
        return httpx.Response(200, json={"customerId": "123", "currency": "EUR", "pricingDate": quote_date,
            "products": [{"productId": pid, "quantity": 1, "netPriceFound": False, "error": False,
                          "catalogPrice": {"value": ex, "valueWithTax": inc, "validFrom": quote_date,
                                           "validTo": quote_date, "currency": "EUR"},
                          "priceWithTax": {"value": inc, "validFrom": quote_date,
                                           "validTo": quote_date, "currency": "EUR"}}
                         for pid in payload["products"]]})
    settings = Settings(product_catalog_path=str(CATALOG), database_url=f"sqlite:///{tmp_path / 'orders.db'}",
                        valio_aimo_base_url="https://example.invalid", valio_aimo_api_key="secret", demo_mode=True)
    app = create_app(settings, httpx.MockTransport(responder))
    headers = {"X-Demo-Identity": "buyer"}
    with TestClient(app) as client:
        by = client.post("/api/purchase-requests", json={"text": "1000 L lactose-free milk by 2099-11-17"}, headers=headers)
        assert by.status_code == 200, by.text
        proposal = by.json()["proposal"]
        chosen = next(c for c in proposal["candidates"] if c["product_id"] == proposal["recommended_product_id"])
        assert chosen["status"] == "viable"
        assert chosen["requested_delivery_date"] == "2099-11-17"
        assert chosen["proposed_delivery_date"] == "2099-11-16"
        assert chosen["unit_price_inc_tax"] == "1.10"
        assert calls == [("/api/availability", "2099-11-17"), ("/api/price", "2099-11-17"),
                         ("/api/availability", "2099-11-16"), ("/api/price", "2099-11-16")]
        calls.clear()
        exact = client.post("/api/purchase-requests", json={"text": "1000 L lactose-free milk on 2099-11-17"}, headers=headers)
        assert exact.status_code == 200, exact.text
        assert all(c["status"] != "viable" and "delivery_date_unconfirmed" in c["reasons"]
                   for c in exact.json()["proposal"]["candidates"] if c.get("litres_per_sales_unit"))
        assert calls == [("/api/availability", "2099-11-17"), ("/api/price", "2099-11-17")]


def test_end_to_end_without_upstream_is_honest(tmp_path):
    settings = Settings(product_catalog_path=str(CATALOG), database_url=f"sqlite:///{tmp_path / 'orders.db'}",
                        demo_mode=True, valio_aimo_base_url="", valio_aimo_api_key="")
    app = create_app(settings)
    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 200 and "<h1>AimoBridge</h1>" in page.text
        assert client.get("/openapi.json").json()["info"]["title"] == "AimoBridge"
        assert (ROOT / "frontend" / "index.html").is_file()
        assert "VALIO_AIMO_API_KEY" not in page.text
        assert client.get("/api/health").json()["demo_mode"] is True
        headers = {"X-Demo-Identity": "buyer-1"}
        response = client.post("/api/purchase-requests", json={"text": "1000 litres lactose-free milk by 2099-11-16"}, headers=headers)
        assert response.status_code == 200
        proposal = response.json()["proposal"]
        assert proposal["status"] == "UPSTREAM_NOT_CONFIGURED"
        assert proposal["recommended_product_id"] is None
        assert proposal["candidates"]
        assert client.post(f"/api/proposals/{proposal['id']}/submit", headers=headers).status_code == 409
        assert client.get(f"/api/proposals/{proposal['id']}").status_code == 401


def test_body_limit_without_content_length(tmp_path):
    settings = Settings(product_catalog_path=str(CATALOG), database_url=f"sqlite:///{tmp_path / 'orders.db'}",
                        demo_mode=True, valio_aimo_base_url="", valio_aimo_api_key="")
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.post("/api/purchase-requests", content=iter([b"x" * 3000, b"y" * 1500]),
                               headers={"X-Demo-Identity": "buyer", "Content-Type": "application/json"})
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "request_too_large"


def test_order_policy_idempotency_and_approval(tmp_path):
    catalog = Catalog(CATALOG)
    intent = parse_intent("100 litres lactose-free milk by 2099-11-16")
    units = {p["id"]: p["salesUnit"] for p in catalog.products.values()}
    calls = []
    def responder(request):
        payload = json.loads(request.content)
        calls.append((request.url.path, payload))
        if request.url.path.endswith("availability"):
            return httpx.Response(200, json={"timeNow": "2099-11-15T12:00:00+02:00", "requestedDate": "2099-11-16T00:00:00+02:00",
                "products": [{"productId": pid, "salesBlock": False, "logisticModel": "Stock", "closingTime": "2099-11-15T23:00:00+02:00",
                              "scheduleLines": [{"salesBlock": False, "availableAmount": 100000, "availableDate": "2099-11-16", "salesUnit": units[pid]}]}
                             for pid in payload["products"]]})
        return httpx.Response(200, json={"customerId": "123", "currency": "EUR", "pricingDate": "2099-11-16",
            "products": [{"productId": pid, "quantity": 1, "netPriceFound": False, "error": False,
                          "catalogPrice": {"value": 1, "valueWithTax": 1.1, "validFrom": "2099-01-01", "validTo": "2099-12-31", "currency": "EUR"},
                          "priceWithTax": {"value": 1.1, "validFrom": "2099-01-01", "validTo": "2099-12-31", "currency": "EUR"}}
                         for pid in payload["products"]]})
    tokens = {"buyer-token": {"requester_id": "buyer", "customer_id": "123", "role": "buyer", "auto_limit_eur": "500"},
              "reviewer-token": {"requester_id": "reviewer", "customer_id": "123", "role": "approver"}}
    settings = Settings(product_catalog_path=str(CATALOG), database_url=f"sqlite:///{tmp_path / 'orders.db'}",
                        valio_aimo_base_url="https://example.invalid", valio_aimo_api_key="secret",
                        purchasing_tokens_json=json.dumps(tokens))
    app = create_app(settings, httpx.MockTransport(responder))
    buyer = {"Authorization": "Bearer buyer-token"}
    reviewer = {"Authorization": "Bearer reviewer-token"}
    with TestClient(app) as client:
        response = client.post("/api/purchase-requests", json={"text": "100 litres lactose-free milk by 2099-11-16"}, headers=buyer)
        assert response.status_code == 200, response.text
        proposal = response.json()["proposal"]
        assert proposal["recommended_product_id"]
        assert len(calls) == 2 and len(calls[0][1]["products"]) <= 6
        order = client.post(f"/api/proposals/{proposal['id']}/submit", headers=buyer).json()
        assert order["status"] == "CONFIRMED" and order["simulation"] and not order["upstream_order_submitted"]
        again = client.post(f"/api/proposals/{proposal['id']}/submit", headers=buyer).json()
        assert again["id"] == order["id"]
        assert client.post(f"/api/orders/{order['id']}/approve", json={}, headers=reviewer).status_code == 409
        response2 = client.post("/api/purchase-requests", json={"text": "10000 litres lactose-free milk by 2099-11-16"}, headers=buyer)
        proposal2 = response2.json()["proposal"]
        alternatives = [c for c in proposal2["candidates"] if c["status"] == "viable"
                        and c["product_id"] != proposal2["recommended_product_id"]]
        assert alternatives
        selected_id = alternatives[0]["product_id"]
        submit_url = f"/api/proposals/{proposal2['id']}/submit"
        invalid = client.post(submit_url, json={"product_id": "not-in-proposal"}, headers=buyer)
        assert invalid.status_code == 422 and invalid.json()["error"]["code"] == "invalid_selection"
        pending = client.post(submit_url, json={"product_id": selected_id}, headers=buyer).json()
        assert pending["status"] == "PENDING_APPROVAL"
        assert pending["offer"]["product_id"] == selected_id
        assert client.post(submit_url, json={"product_id": selected_id}, headers=buyer).json()["id"] == pending["id"]
        revised = client.post(submit_url, json={"product_id": proposal2["recommended_product_id"]}, headers=buyer)
        assert revised.status_code == 200, revised.text
        assert revised.json()["id"] == pending["id"]
        assert revised.json()["status"] == "PENDING_APPROVAL"
        assert revised.json()["offer"]["product_id"] == proposal2["recommended_product_id"]
        assert revised.json()["audit"][-1]["event"] == "OFFER_CHANGED"
        assert client.get(f"/api/orders/{pending['id']}", headers=buyer).json()["offer"]["product_id"] == proposal2["recommended_product_id"]
        revised_again = client.post(submit_url, json={"product_id": selected_id}, headers=buyer).json()
        assert revised_again["id"] == pending["id"] and revised_again["offer"]["product_id"] == selected_id
        assert client.post(f"/api/orders/{pending['id']}/approve", json={}, headers=buyer).status_code == 403
        approved = client.post(f"/api/orders/{pending['id']}/approve", json={}, headers=reviewer)
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "CONFIRMED" and len(approved.json()["audit"]) == 4
        finalized_change = client.post(submit_url, json={"product_id": proposal2["recommended_product_id"]}, headers=buyer)
        assert finalized_change.status_code == 409 and finalized_change.json()["error"]["code"] == "invalid_state"
