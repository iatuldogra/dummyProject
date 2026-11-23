from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class PaymentCreate(BaseModel):
    order_id: int
    user_id: int
    amount: float = Field(gt=0)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    card_number: str | None = Field(
        default="4242424242424242", description="Mock card; numbers ending in 0000 are declined"
    )
    idempotency_key: str | None = Field(
        default=None, max_length=100, description="Retries with the same key return the original payment"
    )


class PaymentVoid(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=100)
    order_id: int
    user_id: int


class PaymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    order_id: int
    user_id: int
    amount: float
    refunded_amount: float
    currency: str
    status: str
    card_last4: str | None
    transaction_ref: str
    failure_reason: str | None
    created_at: datetime


class RefundCreate(BaseModel):
    payment_id: int
    amount: float = Field(gt=0)
    reason: str | None = Field(default=None, max_length=500)
    idempotency_key: str | None = Field(
        default=None, max_length=100, description="Retries with the same key return the original refund"
    )


class RefundOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    payment_id: int
    amount: float
    reason: str | None
    status: str
    created_at: datetime
