"""JSON API for auditable local purchasing simulations."""
from __future__ import annotations

import json
import secrets
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import select

from .core import Catalog, DomainError, HELSINKI, evaluate, parse_intent
from .storage import Audit, Order, Proposal, PurchaseRequest, make_session_factory, uid
from .valio import ValioClient


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    valio_aimo_base_url: str = ""
    valio_aimo_api_key: str = ""
    valio_aimo_customer_id: str = "123"
    product_catalog_path: str = "./data/valio_aimo_junction26_product_data.json"
    database_url: str = "sqlite:///./orders.db"
    quote_ttl_minutes: int = 15
    demo_mode: bool = False
    purchasing_tokens_json: str = "{}"


class PurchaseInput(BaseModel):
    text: str = Field(min_length=3, max_length=2000)


class SubmitInput(BaseModel):
    product_id: str | None = Field(default=None, min_length=1)


class ApprovalInput(BaseModel):
    acknowledge_uncertainty: bool = False


def create_app(settings: Settings | None = None, transport=None) -> FastAPI:
    settings = settings or Settings()
    app = FastAPI(title="AimoBridge", version="0.1.0")
    app.state.settings = settings
    app.state.catalog = Catalog(settings.product_catalog_path)
    app.state.sessions = make_session_factory(settings.database_url)
    app.state.transport = transport
    tokens = json.loads(settings.purchasing_tokens_json)

    @app.exception_handler(DomainError)
    async def domain_error(_request: Request, exc: DomainError):
        return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status)

    @app.middleware("http")
    async def body_limit(request: Request, call_next):
        chunks = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > 4096:
                return JSONResponse({"error": {"code": "request_too_large", "message": "Request body exceeds 4 KiB."}}, status_code=413)
            chunks.append(chunk)
        request._body = b"".join(chunks)
        return await call_next(request)

    def identity(request: Request, authorization: str | None = Header(default=None), x_demo_identity: str | None = Header(default=None),
                 x_demo_role: str | None = Header(default=None)) -> dict:
        if settings.demo_mode and x_demo_identity:
            if not request.client or request.client.host not in ("127.0.0.1", "::1", "localhost", "testclient"):
                raise DomainError("forbidden", "Demo identities are available only from localhost.", 403)
            return {"requester_id": x_demo_identity[:100], "customer_id": settings.valio_aimo_customer_id,
                    "role": x_demo_role if x_demo_role in ("buyer", "approver") else "buyer", "auto_limit_eur": "0"}
        if not authorization or not authorization.startswith("Bearer "):
            raise DomainError("unauthorized", "Bearer identity required.", 401)
        supplied = authorization[7:]
        match = next((v for k, v in tokens.items() if secrets.compare_digest(k, supplied)), None)
        if not match or not all(k in match for k in ("requester_id", "customer_id", "role")):
            raise DomainError("unauthorized", "Unknown identity.", 401)
        return match

    def visible(owner: str, customer: str, actor: dict):
        if actor["customer_id"] != customer or (actor["requester_id"] != owner and actor["role"] != "approver"):
            raise DomainError("forbidden", "Record is not accessible to this identity.", 403)

    def proposal_view(p: Proposal) -> dict:
        return {"id": p.id, "request_id": p.request_id, "quoted_at": p.quoted_at.isoformat(), **p.data,
                "simulation": True, "upstream_order_submitted": False}

    def order_view(o: Order, audits: list[Audit]) -> dict:
        return {"id": o.id, "proposal_id": o.proposal_id, "status": o.status, "policy_reason": o.policy_reason,
                "requester_id": o.requester_id, "customer_id": o.customer_id, "offer": o.data,
                "approver_id": o.approver_id, "decided_at": o.decided_at.isoformat() if o.decided_at else None,
                "audit": [{"event": a.event, "actor_id": a.actor_id, "reason": a.reason, "at": a.at.isoformat()} for a in audits],
                "simulation": True, "upstream_order_submitted": False}

    async def refreshed(intent: dict, customer: str, shortlist: list[dict]) -> tuple[list[dict], str]:
        if not settings.valio_aimo_base_url or not settings.valio_aimo_api_key:
            return ([{**c, "status": "excluded", "reasons": ["upstream_not_configured"]} for c in shortlist], "UPSTREAM_NOT_CONFIGURED")
        client = ValioClient(settings.valio_aimo_base_url, settings.valio_aimo_api_key, transport)
        try:
            requested = date.fromisoformat(intent["requested_delivery_date"])
            available, prices, currency = await client.quote(customer, requested, [c["product_id"] for c in shortlist])
            currencies = {c["product_id"]: currency for c in shortlist}
            proposed_dates: dict[str, str] = {}
            if intent.get("delivery_mode") == "deadline":
                earlier: dict[date, list[str]] = {}
                for candidate in shortlist:
                    product_id = candidate["product_id"]
                    item = available[product_id]
                    lines = item.get("scheduleLines") or []
                    if item.get("salesBlock") or len(lines) != 1 or lines[0].get("salesBlock"):
                        continue
                    raw_date = lines[0].get("availableDate")
                    try:
                        first_date = date.fromisoformat(raw_date[:10]) if raw_date else None
                    except ValueError:
                        continue
                    if first_date and datetime.now(HELSINKI).date() <= first_date < requested:
                        earlier.setdefault(first_date, []).append(product_id)
                for first_date, ids in earlier.items():
                    try:
                        early_available, early_prices, early_currency = await client.quote(customer, first_date, ids)
                    except DomainError:
                        # Keep the original quote uncertain when an earlier date
                        # cannot be rechecked and priced.
                        continue
                    for product_id in ids:
                        available[product_id] = early_available[product_id]
                        prices[product_id] = early_prices[product_id]
                        currencies[product_id] = early_currency
                        proposed_dates[product_id] = first_date.isoformat()
        finally:
            await client.close()
        now = datetime.now(HELSINKI)
        evaluated = [evaluate({**c, **({"proposed_delivery_date": proposed_dates[c["product_id"]]} if c["product_id"] in proposed_dates else {})},
                              app.state.catalog.products[c["product_id"]], intent,
                              available[c["product_id"]], prices[c["product_id"]], currencies[c["product_id"]], now)
                     for c in shortlist]
        return evaluated, "QUOTED"

    @app.get("/", include_in_schema=False)
    def test_ui():
        return FileResponse(Path(__file__).resolve().parents[2] / "frontend" / "index.html")

    @app.get("/api/health")
    def health():
        return {"status": "ok", "catalog_products": len(app.state.catalog.products),
                "upstream_configured": bool(settings.valio_aimo_base_url and settings.valio_aimo_api_key),
                "demo_mode": settings.demo_mode,
                "simulation": True}

    @app.post("/api/purchase-requests")
    async def purchase(payload: PurchaseInput, actor: dict = Depends(identity)):
        try:
            intent = parse_intent(payload.text)
        except DomainError as exc:
            if exc.code == "needs_clarification":
                return JSONResponse({"status": "NEEDS_CLARIFICATION", "error": {"code": exc.code, "message": exc.message}}, status_code=422)
            raise
        shortlist = app.state.catalog.shortlist(intent)
        if not shortlist:
            status, candidates = "NO_CATALOG_MATCH", []
        else:
            candidates, status = await refreshed(intent, actor["customer_id"], shortlist)
        ranked = sorted((c for c in candidates if c["status"] in ("viable", "uncertain") and c.get("total_inc_tax")),
                        key=lambda c: (c["status"] != "viable", Decimal(c["total_inc_tax"]), c["product_id"]))
        if status == "QUOTED" and not ranked:
            status = "NO_SUITABLE_OFFER"
        data = {"status": status, "intent": intent, "candidates": candidates,
                "recommended_product_id": ranked[0]["product_id"] if ranked else None,
                "explanation": "Earliest availability is not a delivery guarantee; uncertain offers need reviewer acknowledgment."}
        with app.state.sessions.begin() as db:
            request_row = PurchaseRequest(id=uid("req"), requester_id=actor["requester_id"],
                                          customer_id=actor["customer_id"],
                                          intent=intent, status=status)
            db.add(request_row)
            proposal = Proposal(id=uid("prop"), request_id=request_row.id, data=data)
            db.add(proposal)
            db.flush()
            result = {"request_id": request_row.id, "proposal": proposal_view(proposal)}
        return result

    @app.get("/api/purchase-requests/{request_id}")
    def get_request(request_id: str, actor: dict = Depends(identity)):
        with app.state.sessions() as db:
            row = db.get(PurchaseRequest, request_id)
            if not row:
                raise DomainError("not_found", "Request not found.", 404)
            visible(row.requester_id, row.customer_id, actor)
            return {"id": row.id, "intent": row.intent, "status": row.status, "created_at": row.created_at.isoformat()}

    @app.get("/api/proposals/{proposal_id}")
    def get_proposal(proposal_id: str, actor: dict = Depends(identity)):
        with app.state.sessions() as db:
            proposal = db.get(Proposal, proposal_id)
            if not proposal:
                raise DomainError("not_found", "Proposal not found.", 404)
            request_row = db.get(PurchaseRequest, proposal.request_id)
            visible(request_row.requester_id, request_row.customer_id, actor)
            return proposal_view(proposal)

    @app.post("/api/proposals/{proposal_id}/submit")
    def submit(proposal_id: str, payload: SubmitInput | None = None, actor: dict = Depends(identity)):
        if actor["role"] != "buyer":
            raise DomainError("forbidden", "Buyer role required.", 403)
        with app.state.sessions.begin() as db:
            proposal = db.get(Proposal, proposal_id)
            if not proposal:
                raise DomainError("not_found", "Proposal not found.", 404)
            request_row = db.get(PurchaseRequest, proposal.request_id)
            if request_row.requester_id != actor["requester_id"] or request_row.customer_id != actor["customer_id"]:
                raise DomainError("forbidden", "Only the requester can submit.", 403)
            existing = db.scalar(select(Order).where(Order.proposal_id == proposal_id))
            product_id = (payload.product_id if payload and payload.product_id else
                          existing.data["product_id"] if existing else proposal.data["recommended_product_id"])
            if existing and existing.data["product_id"] == product_id:
                audits = list(db.scalars(select(Audit).where(Audit.order_id == existing.id).order_by(Audit.at)))
                return order_view(existing, audits)
            if existing and existing.status != "PENDING_APPROVAL":
                raise DomainError("invalid_state", "A finalized simulated order cannot change products; make a new request.", 409)
            if datetime.now(timezone.utc).replace(tzinfo=None) - proposal.quoted_at.replace(tzinfo=None) > timedelta(minutes=settings.quote_ttl_minutes):
                raise DomainError("stale_quote", "Proposal expired; submit a new purchase request.", 409)
            chosen = next((c for c in proposal.data["candidates"] if c["product_id"] == product_id), None)
            if payload and payload.product_id and not chosen:
                raise DomainError("invalid_selection", "Selected product is not in this proposal.")
            if not chosen or chosen["status"] not in ("viable", "uncertain") or not chosen.get("total_inc_tax"):
                raise DomainError("no_suitable_offer", "No orderable quoted candidate.", 409)
            if existing:
                old_product_id = existing.data["product_id"]
                existing.data = chosen
                existing.policy_reason = "offer_changed_pending_approval"
                db.add(Audit(id=uid("audit"), order_id=existing.id, event="OFFER_CHANGED",
                             actor_id=actor["requester_id"], reason=f"{old_product_id} -> {product_id}"))
                db.flush()
                audits = list(db.scalars(select(Audit).where(Audit.order_id == existing.id).order_by(Audit.at)))
                return order_view(existing, audits)
            limit = Decimal(str(actor.get("auto_limit_eur") or "0"))
            safe = chosen["status"] == "viable" and limit > 0 and Decimal(chosen["total_inc_tax"]) <= limit
            status = "CONFIRMED" if safe else "PENDING_APPROVAL"
            reason = "within_configured_limit_and_definitive" if safe else "human_approval_required"
            order = Order(id=uid("ord"), proposal_id=proposal_id, requester_id=actor["requester_id"],
                          customer_id=actor["customer_id"], status=status, data=chosen, policy_reason=reason)
            db.add(order)
            db.add(Audit(id=uid("audit"), order_id=order.id, event=status, actor_id=actor["requester_id"], reason=reason))
            db.flush()
            return order_view(order, list(db.scalars(select(Audit).where(Audit.order_id == order.id))))

    @app.get("/api/orders/{order_id}")
    def get_order(order_id: str, actor: dict = Depends(identity)):
        with app.state.sessions() as db:
            order = db.get(Order, order_id)
            if not order:
                raise DomainError("not_found", "Order not found.", 404)
            visible(order.requester_id, order.customer_id, actor)
            return order_view(order, list(db.scalars(select(Audit).where(Audit.order_id == order.id).order_by(Audit.at))))

    @app.post("/api/orders/{order_id}/approve")
    async def approve(order_id: str, payload: ApprovalInput, actor: dict = Depends(identity)):
        if actor["role"] != "approver":
            raise DomainError("forbidden", "Approver role required.", 403)
        with app.state.sessions.begin() as db:
            order = db.get(Order, order_id)
            if not order:
                raise DomainError("not_found", "Order not found.", 404)
            if order.customer_id != actor["customer_id"] or order.requester_id == actor["requester_id"]:
                raise DomainError("forbidden", "Independent customer approver required.", 403)
            if order.status != "PENDING_APPROVAL":
                raise DomainError("invalid_state", "Order is not pending approval.", 409)
            proposal = db.get(Proposal, order.proposal_id)
            shortlist = [{k: v for k, v in order.data.items() if k in ("product_id", "name", "sales_unit", "litres_per_sales_unit", "lactose_free_verified", "match_reason")}]
            refreshed_offers, _ = await refreshed(proposal.data["intent"], order.customer_id, shortlist)
            fresh = refreshed_offers[0]
            if fresh["status"] == "excluded":
                raise DomainError("stale_quote", "Offer no longer meets hard requirements.", 409)
            if fresh.get("total_inc_tax") != order.data.get("total_inc_tax") or fresh.get("reasons") != order.data.get("reasons"):
                raise DomainError("stale_quote", "Price or availability changed; request a new proposal.", 409)
            if fresh["status"] == "uncertain" and not payload.acknowledge_uncertainty:
                raise DomainError("approval_required", "Explicit uncertainty acknowledgment required.", 409)
            order.status, order.approver_id, order.decided_at = "CONFIRMED", actor["requester_id"], datetime.now(timezone.utc)
            order.policy_reason = "reviewer_approved_with_uncertainty" if fresh["status"] == "uncertain" else "reviewer_approved"
            db.add(Audit(id=uid("audit"), order_id=order.id, event="CONFIRMED", actor_id=actor["requester_id"], reason=order.policy_reason))
            db.flush()
            return order_view(order, list(db.scalars(select(Audit).where(Audit.order_id == order.id).order_by(Audit.at))))

    @app.post("/api/orders/{order_id}/reject")
    def reject(order_id: str, actor: dict = Depends(identity)):
        if actor["role"] != "approver":
            raise DomainError("forbidden", "Approver role required.", 403)
        with app.state.sessions.begin() as db:
            order = db.get(Order, order_id)
            if not order:
                raise DomainError("not_found", "Order not found.", 404)
            if order.customer_id != actor["customer_id"] or order.requester_id == actor["requester_id"]:
                raise DomainError("forbidden", "Independent customer approver required.", 403)
            if order.status != "PENDING_APPROVAL":
                raise DomainError("invalid_state", "Order is not pending approval.", 409)
            order.status, order.approver_id, order.decided_at = "REJECTED", actor["requester_id"], datetime.now(timezone.utc)
            order.policy_reason = "reviewer_rejected"
            db.add(Audit(id=uid("audit"), order_id=order.id, event="REJECTED", actor_id=actor["requester_id"], reason=order.policy_reason))
            db.flush()
            return order_view(order, list(db.scalars(select(Audit).where(Audit.order_id == order.id).order_by(Audit.at))))

    return app


app = create_app()
