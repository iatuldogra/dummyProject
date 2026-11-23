from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base

Money = Numeric(10, 2, asdecimal=False)


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    amount: Mapped[float] = mapped_column(Money)
    refunded_amount: Mapped[float] = mapped_column(Money, default=0)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    # succeeded | failed | partially_refunded | refunded
    status: Mapped[str] = mapped_column(String(20))
    card_last4: Mapped[str | None] = mapped_column(String(4))
    transaction_ref: Mapped[str] = mapped_column(String(64), unique=True)
    failure_reason: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    refunds: Mapped[list["Refund"]] = relationship(back_populates="payment", order_by="Refund.id")


class Refund(Base):
    __tablename__ = "refunds"

    id: Mapped[int] = mapped_column(primary_key=True)
    payment_id: Mapped[int] = mapped_column(ForeignKey("payments.id"), index=True)
    amount: Mapped[float] = mapped_column(Money)
    reason: Mapped[str | None] = mapped_column(String(500))
    idempotency_key: Mapped[str | None] = mapped_column(String(100), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="succeeded")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    payment: Mapped[Payment] = relationship(back_populates="refunds")
