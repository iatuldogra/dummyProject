from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ComplaintStatus = Literal["open", "in_progress", "resolved"]


class ComplaintCreate(BaseModel):
    order_id: int | None = None
    subject: str = Field(min_length=1, max_length=255)
    message: str = Field(min_length=1)


class ComplaintUpdate(BaseModel):
    status: ComplaintStatus


class ComplaintOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    order_id: int | None
    subject: str
    message: str
    status: str
    created_at: datetime
    updated_at: datetime


class ReturnItem(BaseModel):
    product_id: int
    quantity: int = Field(gt=0)


class ReturnCreate(BaseModel):
    order_id: int
    items: list[ReturnItem] = Field(min_length=1)
    reason: str = Field(min_length=1)


class ReturnDecision(BaseModel):
    status: Literal["approved", "rejected"]


class ReturnOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    order_id: int
    items: list[ReturnItem]
    reason: str
    status: str
    refund_amount: float
    payment_id: int | None
    refund_id: int | None
    created_at: datetime
    updated_at: datetime
