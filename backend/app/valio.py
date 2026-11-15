"""Strict, rate-conscious client for the documented on-demand API."""
from __future__ import annotations

import asyncio
import json
from datetime import date
from decimal import Decimal
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from .core import DomainError


class ScheduleLine(BaseModel):
    salesBlock: bool
    availableAmount: Decimal
    availableDate: str | None = None
    salesUnit: str


class AvailabilityProduct(BaseModel):
    productId: str
    closingTime: str | None = None
    salesBlock: bool
    logisticModel: str | None = None
    scheduleLines: list[ScheduleLine]


class AvailabilityResponse(BaseModel):
    timeNow: str
    requestedDate: str
    products: list[AvailabilityProduct]


class PriceValue(BaseModel):
    value: Decimal
    valueWithTax: Decimal
    validFrom: str
    validTo: str
    currency: str


class PriceWithTax(BaseModel):
    value: Decimal
    validFrom: str
    validTo: str
    currency: str


class PriceProduct(BaseModel):
    productId: str
    quantity: float
    catalogPrice: PriceValue | None = None
    priceWithTax: PriceWithTax | None = None
    comparisonPrice: PriceValue | None = None
    comparisonUnit: str | None = None
    tax: dict | None = None
    listPrice: PriceValue | None = None
    netPriceFound: bool
    error: bool


class PriceResponse(BaseModel):
    customerId: str
    currency: str
    pricingDate: str
    products: list[PriceProduct]


class ValioClient:
    def __init__(self, base_url: str, api_key: str, transport: httpx.AsyncBaseTransport | None = None):
        if not base_url or not base_url.startswith("https://"):
            raise DomainError("upstream_not_configured", "Configure the documented HTTPS mock API base URL.", 503)
        if not api_key:
            raise DomainError("upstream_not_configured", "Configure the mock API key.", 503)
        self.client = httpx.AsyncClient(base_url=base_url.rstrip("/"), transport=transport,
                                         headers={"X-API-Key": api_key, "Content-Type": "application/json"},
                                         timeout=httpx.Timeout(10, connect=3), follow_redirects=False)

    async def close(self):
        await self.client.aclose()

    async def _post(self, path: str, payload: dict) -> dict:
        for attempt in range(3):
            try:
                response = await self.client.post(path, json=payload)
                if response.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                    retry = response.headers.get("Retry-After", "")
                    delay = min(float(retry), 3.0) if retry.replace(".", "", 1).isdigit() else 0.2 * 2**attempt
                    await asyncio.sleep(delay)
                    continue
                response.raise_for_status()
                return json.loads(response.text, parse_float=Decimal)
            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                if attempt == 2:
                    raise DomainError("upstream_error", "Mock API timeout or connection failure.", 502) from exc
                await asyncio.sleep(0.2 * 2**attempt)
            except (httpx.HTTPStatusError, ValueError) as exc:
                raise DomainError("upstream_error", "Mock API returned an error or invalid JSON.", 502) from exc
        raise DomainError("upstream_error", "Mock API retry budget exhausted.", 502)

    async def quote(self, customer: str, delivery: date, product_ids: list[str]) -> tuple[dict, dict, str]:
        if not 1 <= len(product_ids) <= 10 or len(product_ids) != len(set(product_ids)):
            raise DomainError("invalid_shortlist", "Quote requires 1–10 unique product IDs.")
        payload = {"customer": customer, "requestedDate": delivery.isoformat(), "products": product_ids}
        try:
            availability = AvailabilityResponse.model_validate(await self._post("/api/availability", payload))
            pricing = PriceResponse.model_validate(await self._post("/api/price", payload))
        except ValidationError as exc:
            raise DomainError("upstream_error", "Mock API response did not match its schema.", 502) from exc
        if pricing.customerId != customer or pricing.pricingDate[:10] != delivery.isoformat() or availability.requestedDate[:10] != delivery.isoformat():
            raise DomainError("upstream_error", "Mock API response context did not match the request.", 502)
        def keyed(items: list[Any]) -> dict:
            ids = [p.productId for p in items]
            if len(ids) != len(set(ids)) or set(ids) != set(product_ids):
                raise DomainError("upstream_error", "Mock API returned missing, duplicate, or unexpected product IDs.", 502)
            return {p.productId: p.model_dump() for p in items}
        return keyed(availability.products), keyed(pricing.products), pricing.currency
