from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class CartLine(BaseModel):
    product_id: int
    quantity: int = Field(gt=0)


class CartItemOut(CartLine):
    model_config = ConfigDict(from_attributes=True)

    id: int


class CartOut(BaseModel):
    items: list[CartItemOut]


class CheckoutRequest(BaseModel):
    promo_code: str | None = None
    card_number: str | None = Field(
        default="4242424242424242", description="Mock card; numbers ending in 0000 are declined"
    )
    items: list[CartLine] | None = Field(
        default=None, description="Optional explicit items; if omitted the user's cart is checked out"
    )


class OrderItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    product_id: int
    product_name: str
    unit_price: float
    quantity: int
    discount: float


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    status: str
    subtotal: float
    discount: float
    total: float
    promo_code: str | None
    payment_id: int | None
    failure_reason: str | None
    created_at: datetime
    items: list[OrderItemOut]
