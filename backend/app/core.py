"""Catalog, intent and quote logic. No model output can decide stock or approval."""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

HELSINKI = ZoneInfo("Europe/Helsinki")
CENT = Decimal("0.01")
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def money(value: Decimal) -> str:
    return str(value.quantize(CENT, rounding=ROUND_HALF_UP))


class DomainError(Exception):
    def __init__(self, code: str, message: str, status: int = 422):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def parse_intent(raw: str, now: datetime | None = None) -> dict:
    now = (now or datetime.now(HELSINKI)).astimezone(HELSINKI)
    match = re.search(r"\b([\d, ]+(?:\.\d+)?)\s*(litres?|liters?|l)\b", raw, re.I)
    if not match:
        raise DomainError("needs_clarification", "Specify a positive quantity in litres.")
    quantity = Decimal(match.group(1).replace(",", "").replace(" ", ""))
    if quantity <= 0 or quantity > 100000:
        raise DomainError("invalid_intent", "Quantity must be between 0 and 100000 L.")
    if not re.search(r"\bmilk\b", raw, re.I):
        raise DomainError("needs_clarification", "Specify a supported product type (milk).")
    lactose_free = bool(re.search(r"lactose[ -]?free", raw, re.I))
    iso = re.search(r"\b\d{4}-\d{2}-\d{2}\b", raw)
    if iso:
        try:
            delivery = date.fromisoformat(iso.group())
        except ValueError as exc:
            raise DomainError("invalid_intent", "Invalid delivery date.") from exc
    else:
        weekdays = list(re.finditer(r"\b(?:(next|this)\s+)?(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b", raw, re.I))
        if len(weekdays) != 1:
            raise DomainError("needs_clarification", "Specify one delivery weekday or an ISO date (YYYY-MM-DD).")
        modifier, name = weekdays[0].groups()
        target = WEEKDAYS.index(name.lower())
        if modifier and modifier.lower() == "this":
            delivery = now.date() + timedelta(days=target - now.weekday())
        else:
            days = (target - now.weekday()) % 7 or 7
            if modifier and modifier.lower() == "next":
                days += 7
            delivery = now.date() + timedelta(days=days)
    if delivery < now.date():
        raise DomainError("invalid_intent", "Delivery date has passed.")
    date_phrase = iso.group() if iso else weekdays[0].group()
    delivery_mode = "deadline" if re.search(r"\bby\s+" + re.escape(date_phrase), raw, re.I) else "exact"
    return {"product_query": "lactose-free milk" if lactose_free else "milk",
            "quantity": str(quantity), "unit": "L", "requested_delivery_date": delivery.isoformat(),
            "delivery_mode": delivery_mode,
            "currency": "EUR", "constraints": {"must_be_lactose_free": lactose_free},
            "preferences": {"sort": "lowest_total_price"}}


def _english_name(product: dict) -> str:
    names = product.get("synkkaData", {}).get("names") or []
    return next((n["value"] for n in names if n.get("language") == "en"),
                names[0]["value"] if names else "")


def _lactose_claim(product: dict) -> bool:
    return any(c.get("name") == "nutritionalClaim" and any(
        v.get("id") == "FREE_FROM_LACTOSE" and v.get("unit") == "FREE_FROM"
        for v in c.get("values", [])) for c in product.get("synkkaData", {}).get("classifications") or [])


def litres_per_sales_unit(product: dict) -> Decimal | None:
    synkka = product.get("synkkaData") or {}
    sales, base = product.get("salesUnit"), product.get("baseUnit")
    units = [u for u in product.get("units", []) if u.get("unitId") == sales]
    if len(units) != 1:
        return None
    factor = Decimal(str(units[0]["sizeInBaseUnits"]))
    if factor <= 0:
        return None
    conversions = synkka.get("unitConversions") or []
    target_type = "BASE_UNIT_OR_EACH" if sales == base else "CASE"
    values = [c["volume"] for c in conversions if c.get("synkkaPackagingType") == target_type
              and c.get("volume", {}).get("unit") in ("LTR", "MLT")]
    if len(values) != 1:
        return None
    volume = Decimal(str(values[0]["value"]))
    if values[0]["unit"] == "MLT":
        volume /= 1000
    return volume if volume > 0 else None


class Catalog:
    def __init__(self, path: str | Path):
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("Catalog must be a product array")
        self.products = {p["id"]: p for p in data if isinstance(p, dict) and p.get("id") and not p.get("deleted")}

    def shortlist(self, intent: dict, limit: int = 6) -> list[dict]:
        matches = []
        for p in self.products.values():
            name = _english_name(p)
            lower = name.casefold()
            if not re.search(r"\bmilk(?:drink)?\b", lower):
                continue
            if any(x in lower for x in ("chocolate", "coffee", "latte", "quark", "powder", "formula", "flour", "mix", "yoghurt", "yogurt", "fermented", "cream", "ice cream")):
                continue
            claim = _lactose_claim(p)
            if intent["constraints"]["must_be_lactose_free"] and not claim:
                continue
            volume = litres_per_sales_unit(p)
            score = (10 if claim else 0) + (5 if volume else 0) + (3 if "milk" in lower else 0)
            if "milk drink" in lower or "milkdrink" in lower:
                score -= 1
            matches.append({"product_id": p["id"], "name": name, "sales_unit": p["salesUnit"],
                            "litres_per_sales_unit": str(volume) if volume else None,
                            "lactose_free_verified": claim, "match_reason": "Catalog name and declared lactose-free claim" if claim else "Catalog name",
                            "_score": score})
        matches.sort(key=lambda x: (-x["_score"], x["product_id"]))
        for m in matches:
            m.pop("_score")
        return matches[:limit]


def sales_units_needed(quantity_l: Decimal, volume_l: Decimal, lot_size: Decimal) -> int:
    if volume_l <= 0:
        raise DomainError("quantity_conversion_unknown", "Missing verified volume per sales unit.")
    # Physical packages are integral even when allowedLotSize is fractional.
    units = int((quantity_l / volume_l).to_integral_value(rounding=ROUND_CEILING))
    if lot_size > 1:
        units = int((Decimal(units) / lot_size).to_integral_value(rounding=ROUND_CEILING) * lot_size)
    return units


def _date_from_api(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def evaluate(candidate: dict, product: dict, intent: dict, availability: dict, price: dict,
             currency: str, now: datetime | None = None) -> dict:
    now = (now or datetime.now(HELSINKI)).astimezone(HELSINKI)
    result = dict(candidate)
    result.update(status="excluded", reasons=[], simulation=True, upstream_order_submitted=False)
    reasons = result["reasons"]
    volume = candidate["litres_per_sales_unit"]
    if volume is None:
        reasons.append("quantity_conversion_unknown")
        return result
    units = sales_units_needed(Decimal(intent["quantity"]), Decimal(volume), Decimal(str(product.get("allowedLotSize") or 1)))
    ordered_l = Decimal(units) * Decimal(volume)
    result.update(sales_units_needed=units, ordered_quantity_l=str(ordered_l),
                  oversupply_l=str(ordered_l - Decimal(intent["quantity"])))
    requested = date.fromisoformat(intent["requested_delivery_date"])
    proposed = date.fromisoformat(candidate.get("proposed_delivery_date", intent["requested_delivery_date"]))
    result["requested_delivery_date"] = requested.isoformat()
    result["delivery_mode"] = intent.get("delivery_mode", "exact")
    result["proposed_delivery_date"] = proposed.isoformat()
    if availability.get("salesBlock"):
        reasons.append("sales_blocked")
    lines = availability.get("scheduleLines") or []
    if not lines:
        reasons.append("unavailable")
    elif len(lines) != 1:
        reasons.append("availability_ambiguous")
    else:
        line = lines[0]
        if line.get("salesBlock"):
            reasons.append("sales_blocked")
        if line.get("salesUnit") != product.get("salesUnit"):
            reasons.append("sales_unit_mismatch")
        available_date = _date_from_api(line.get("availableDate"))
        if proposed > requested or (proposed != requested and intent.get("delivery_mode") != "deadline"):
            reasons.append("delivery_date_unconfirmed")
        if available_date is None or available_date > proposed:
            reasons.append("delivery_date_unavailable")
        elif available_date < proposed:
            reasons.append("delivery_date_unconfirmed")
        logistic = (availability.get("logisticModel") or "").casefold()
        if logistic == "terminal":
            reasons.append("supplier_stock_unconfirmed")
        elif logistic != "stock":
            reasons.append("logistics_unknown")
        elif Decimal(str(line.get("availableAmount", 0))) < units:
            reasons.append("insufficient_stock")
    cutoff = availability.get("closingTime")
    if cutoff:
        try:
            parsed = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                reasons.append("cutoff_timezone_unknown")
            elif now > parsed.astimezone(HELSINKI):
                reasons.append("cutoff_passed")
        except ValueError:
            reasons.append("cutoff_invalid")
    else:
        reasons.append("cutoff_unconfirmed")
    if price.get("error") or currency != "EUR" or price.get("quantity") != 1:
        reasons.append("invalid_price")
    catalog_price, with_tax = price.get("catalogPrice"), price.get("priceWithTax")
    if not catalog_price or not with_tax:
        reasons.append("invalid_price")
    else:
        for item in (catalog_price, with_tax):
            if item.get("currency") != "EUR" or not (_date_from_api(item.get("validFrom")) and _date_from_api(item.get("validTo"))):
                reasons.append("invalid_price")
            elif not (_date_from_api(item["validFrom"]) <= proposed <= _date_from_api(item["validTo"])):
                reasons.append("price_not_valid_on_delivery_date")
        if "invalid_price" not in reasons and "price_not_valid_on_delivery_date" not in reasons:
            ex = Decimal(str(catalog_price["value"]))
            inc = Decimal(str(with_tax["value"]))
            if ex < 0 or inc < ex or Decimal(str(catalog_price["valueWithTax"])) != inc:
                reasons.append("invalid_price")
            else:
                result.update(unit_price_ex_tax=money(ex), unit_price_inc_tax=money(inc),
                              subtotal_ex_tax=money(ex * units), total_inc_tax=money(inc * units), currency="EUR",
                              net_price_found=price.get("netPriceFound"))
    reasons[:] = list(dict.fromkeys(reasons))
    hard = {"quantity_conversion_unknown", "sales_blocked", "unavailable", "availability_ambiguous",
            "sales_unit_mismatch", "delivery_date_unavailable", "insufficient_stock", "cutoff_passed",
            "invalid_price", "price_not_valid_on_delivery_date"}
    result["status"] = "excluded" if hard.intersection(reasons) else "uncertain" if reasons else "viable"
    return result
