from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DiscountType = Literal["percent", "fixed"]
Scope = Literal["catalog", "category", "products"]


def _as_utc(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


class _DiscountMixin(BaseModel):
    discount_type: DiscountType
    discount_value: float = Field(gt=0)

    @model_validator(mode="after")
    def check_percent(self):
        if self.discount_type == "percent" and self.discount_value > 100:
            raise ValueError("percent discount_value must be <= 100")
        return self


# ---------- Promo codes ----------


class PromoCodeCreate(_DiscountMixin):
    code: str = Field(min_length=2, max_length=50)
    description: str | None = None
    min_order_amount: float = Field(default=0, ge=0)
    expires_at: datetime | None = None
    usage_limit: int | None = Field(default=None, ge=1)
    is_active: bool = True

    @field_validator("expires_at")
    @classmethod
    def normalize_tz(cls, v: datetime | None) -> datetime | None:
        return _as_utc(v)

    @field_validator("code")
    @classmethod
    def upper_code(cls, v: str) -> str:
        return v.strip().upper()


class PromoCodeUpdate(BaseModel):
    description: str | None = None
    discount_type: DiscountType | None = None
    discount_value: float | None = Field(default=None, gt=0)
    min_order_amount: float | None = Field(default=None, ge=0)
    expires_at: datetime | None = None
    usage_limit: int | None = Field(default=None, ge=1)
    is_active: bool | None = None

    @field_validator("expires_at")
    @classmethod
    def normalize_tz(cls, v: datetime | None) -> datetime | None:
        return _as_utc(v)


class PromoCodeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    description: str | None
    discount_type: str
    discount_value: float
    min_order_amount: float
    expires_at: datetime | None
    usage_limit: int | None
    times_used: int
    is_active: bool
    created_at: datetime


class PromoValidateRequest(BaseModel):
    code: str
    subtotal: float = Field(ge=0)


class PromoValidateResponse(BaseModel):
    valid: bool
    code: str
    message: str
    discount_type: str | None = None
    discount_value: float | None = None
    discount_amount: float = 0


class PromoRedeemRequest(BaseModel):
    code: str
    key: str = Field(min_length=1, max_length=100, description="Retries with the same key redeem only once")


# ---------- Campaigns ----------


class CampaignCreate(_DiscountMixin):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    start_at: datetime
    end_at: datetime
    scope: Scope = "catalog"
    categories: list[str] = Field(default_factory=list)
    product_ids: list[int] = Field(default_factory=list)
    is_active: bool = True

    @field_validator("start_at", "end_at")
    @classmethod
    def normalize_tz(cls, v: datetime | None) -> datetime | None:
        return _as_utc(v)

    @model_validator(mode="after")
    def check_campaign(self):
        if self.end_at <= self.start_at:
            raise ValueError("end_at must be after start_at")
        if self.scope == "category" and not self.categories:
            raise ValueError("categories are required when scope is 'category'")
        if self.scope == "products" and not self.product_ids:
            raise ValueError("product_ids are required when scope is 'products'")
        return self


class CampaignUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    discount_type: DiscountType | None = None
    discount_value: float | None = Field(default=None, gt=0)
    scope: Scope | None = None
    categories: list[str] | None = None
    product_ids: list[int] | None = None
    is_active: bool | None = None

    @field_validator("start_at", "end_at")
    @classmethod
    def normalize_tz(cls, v: datetime | None) -> datetime | None:
        return _as_utc(v)


class CampaignOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None
    start_at: datetime
    end_at: datetime
    discount_type: str
    discount_value: float
    scope: str
    categories: list[str]
    product_ids: list[int]
    is_active: bool
    created_at: datetime
