"""Local simulated-order records and audit trail."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def uid(prefix: str) -> str:
    return prefix + "_" + uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class PurchaseRequest(Base):
    __tablename__ = "purchase_requests"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    requester_id: Mapped[str] = mapped_column(String)
    customer_id: Mapped[str] = mapped_column(String)
    intent: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class Proposal(Base):
    __tablename__ = "proposals"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    request_id: Mapped[str] = mapped_column(ForeignKey("purchase_requests.id"), unique=True)
    data: Mapped[dict] = mapped_column(JSON)
    quoted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    proposal_id: Mapped[str] = mapped_column(ForeignKey("proposals.id"), unique=True)
    requester_id: Mapped[str] = mapped_column(String)
    customer_id: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    data: Mapped[dict] = mapped_column(JSON)
    policy_reason: Mapped[str] = mapped_column(String)
    approver_id: Mapped[str | None] = mapped_column(String, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Audit(Base):
    __tablename__ = "audit"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"))
    event: Mapped[str] = mapped_column(String)
    actor_id: Mapped[str] = mapped_column(String)
    reason: Mapped[str] = mapped_column(String)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


def make_session_factory(url: str):
    engine = create_engine(url, connect_args={"check_same_thread": False} if url.startswith("sqlite") else {})
    Base.metadata.create_all(engine)
    if url.startswith("sqlite"):
        # Early MVP databases stored raw request text. Remove that column so
        # existing installations keep working and no longer retain the text.
        with engine.begin() as connection:
            columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(purchase_requests)")}
            if "raw_text" in columns:
                connection.exec_driver_sql("ALTER TABLE purchase_requests DROP COLUMN raw_text")
    return sessionmaker(engine, expire_on_commit=False)
