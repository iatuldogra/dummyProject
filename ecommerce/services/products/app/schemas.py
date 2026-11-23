from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ProductBase(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    category: str = Field(default="general", max_length=100)
    price: float = Field(ge=0)


class ProductCreate(ProductBase):
    stock: int = Field(default=0, ge=0)


class ProductUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    category: str | None = Field(default=None, max_length=100)
    price: float | None = Field(default=None, ge=0)


class ProductOut(ProductBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    stock: int
    created_at: datetime
    updated_at: datetime


class InventoryOut(BaseModel):
    product_id: int
    stock: int


class InventorySet(BaseModel):
    stock: int = Field(ge=0)


class InventoryAdjust(BaseModel):
    delta: int = Field(description="Negative to reserve/reduce stock, positive to restock")


class InventoryReserve(BaseModel):
    key: str = Field(min_length=1, max_length=100, description="Retries with the same key reserve only once")
    quantity: int = Field(gt=0)


class InventoryRelease(BaseModel):
    key: str = Field(min_length=1, max_length=100)
