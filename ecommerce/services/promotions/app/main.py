from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Response, status
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import models, schemas
from .auth import require_admin, require_internal
from .database import get_db, init_db


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="promotions-service", lifespan=lifespan)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _get_or_404(db: Session, model, obj_id: int):
    obj = db.get(model, obj_id)
    if not obj:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{model.__name__} not found")
    return obj


def _apply_update(obj, changes: dict, create_schema) -> None:
    """Apply a partial update, then re-validate the merged object with the create schema."""
    merged = {field: getattr(obj, field) for field in create_schema.model_fields} | changes
    try:
        validated = create_schema.model_validate(merged)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors(include_url=False, include_context=False, include_input=False))
    for field in changes:
        setattr(obj, field, getattr(validated, field))


@app.get("/health")
def health():
    return {"status": "ok", "service": "promotions"}


# ---------- Promo codes ----------


@app.post(
    "/promotions/promo-codes",
    response_model=schemas.PromoCodeOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
)
def create_promo_code(payload: schemas.PromoCodeCreate, db: Session = Depends(get_db)):
    promo = models.PromoCode(**payload.model_dump(), times_used=0)
    db.add(promo)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Promo code already exists")
    db.refresh(promo)
    return promo


@app.get("/promotions/promo-codes", response_model=list[schemas.PromoCodeOut], dependencies=[Depends(require_admin)])
def list_promo_codes(db: Session = Depends(get_db)):
    return db.scalars(select(models.PromoCode).order_by(models.PromoCode.id)).all()


@app.post("/promotions/promo-codes/validate", response_model=schemas.PromoValidateResponse)
def validate_promo_code(payload: schemas.PromoValidateRequest, db: Session = Depends(get_db)):
    code = payload.code.strip().upper()
    promo = db.scalar(select(models.PromoCode).where(models.PromoCode.code == code))

    def invalid(message: str) -> schemas.PromoValidateResponse:
        return schemas.PromoValidateResponse(valid=False, code=code, message=message)

    if not promo:
        return invalid("Promo code not found")
    if not promo.is_active:
        return invalid("Promo code is inactive")
    if promo.expires_at and promo.expires_at <= _now():
        return invalid("Promo code has expired")
    if promo.usage_limit is not None and promo.times_used >= promo.usage_limit:
        return invalid("Promo code usage limit reached")
    if payload.subtotal < promo.min_order_amount:
        return invalid(f"Minimum order amount is {promo.min_order_amount:.2f}")

    if promo.discount_type == "percent":
        amount = payload.subtotal * promo.discount_value / 100
    else:
        amount = min(promo.discount_value, payload.subtotal)
    return schemas.PromoValidateResponse(
        valid=True,
        code=code,
        message="Promo code applied",
        discount_type=promo.discount_type,
        discount_value=promo.discount_value,
        discount_amount=round(amount, 2),
    )


@app.post("/promotions/promo-codes/redeem", response_model=schemas.PromoCodeOut, dependencies=[Depends(require_internal)])
def redeem_promo_code(payload: schemas.PromoRedeemRequest, db: Session = Depends(get_db)):
    """Atomically increment usage; called by orders-service after a successful checkout."""
    PromoCode = models.PromoCode
    promo_id = db.execute(
        update(PromoCode)
        .where(
            PromoCode.code == payload.code.strip().upper(),
            PromoCode.is_active.is_(True),
            or_(PromoCode.usage_limit.is_(None), PromoCode.times_used < PromoCode.usage_limit),
        )
        .values(times_used=PromoCode.times_used + 1)
        .returning(PromoCode.id)
    ).scalar_one_or_none()
    if promo_id is None:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Promo code cannot be redeemed")
    db.commit()
    return db.get(PromoCode, promo_id)


@app.post("/promotions/promo-codes/release", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(require_internal)])
def release_promo_code(payload: schemas.PromoRedeemRequest, db: Session = Depends(get_db)):
    """Give back one use; called by orders-service when a checkout fails after redeeming."""
    PromoCode = models.PromoCode
    db.execute(
        update(PromoCode)
        .where(PromoCode.code == payload.code.strip().upper(), PromoCode.times_used > 0)
        .values(times_used=PromoCode.times_used - 1)
    )
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get(
    "/promotions/promo-codes/{promo_id}",
    response_model=schemas.PromoCodeOut,
    dependencies=[Depends(require_admin)],
)
def get_promo_code(promo_id: int, db: Session = Depends(get_db)):
    return _get_or_404(db, models.PromoCode, promo_id)


@app.patch(
    "/promotions/promo-codes/{promo_id}",
    response_model=schemas.PromoCodeOut,
    dependencies=[Depends(require_admin)],
)
def update_promo_code(promo_id: int, payload: schemas.PromoCodeUpdate, db: Session = Depends(get_db)):
    promo = _get_or_404(db, models.PromoCode, promo_id)
    _apply_update(promo, payload.model_dump(exclude_unset=True), schemas.PromoCodeCreate)
    db.commit()
    db.refresh(promo)
    return promo


@app.delete(
    "/promotions/promo-codes/{promo_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_admin)],
)
def delete_promo_code(promo_id: int, db: Session = Depends(get_db)):
    db.delete(_get_or_404(db, models.PromoCode, promo_id))
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------- Campaigns / sales events ----------


@app.post(
    "/promotions/campaigns",
    response_model=schemas.CampaignOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
)
def create_campaign(payload: schemas.CampaignCreate, db: Session = Depends(get_db)):
    campaign = models.Campaign(**payload.model_dump())
    db.add(campaign)
    db.commit()
    db.refresh(campaign)
    return campaign


@app.get("/promotions/campaigns", response_model=list[schemas.CampaignOut])
def list_campaigns(db: Session = Depends(get_db)):
    return db.scalars(select(models.Campaign).order_by(models.Campaign.start_at)).all()


@app.get("/promotions/campaigns/active", response_model=list[schemas.CampaignOut])
def active_campaigns(db: Session = Depends(get_db)):
    now = _now()
    Campaign = models.Campaign
    return db.scalars(
        select(Campaign)
        .where(Campaign.is_active.is_(True), Campaign.start_at <= now, Campaign.end_at > now)
        .order_by(Campaign.id)
    ).all()


@app.get("/promotions/campaigns/{campaign_id}", response_model=schemas.CampaignOut)
def get_campaign(campaign_id: int, db: Session = Depends(get_db)):
    return _get_or_404(db, models.Campaign, campaign_id)


@app.patch(
    "/promotions/campaigns/{campaign_id}",
    response_model=schemas.CampaignOut,
    dependencies=[Depends(require_admin)],
)
def update_campaign(campaign_id: int, payload: schemas.CampaignUpdate, db: Session = Depends(get_db)):
    campaign = _get_or_404(db, models.Campaign, campaign_id)
    _apply_update(campaign, payload.model_dump(exclude_unset=True), schemas.CampaignCreate)
    db.commit()
    db.refresh(campaign)
    return campaign


@app.delete(
    "/promotions/campaigns/{campaign_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_admin)],
)
def delete_campaign(campaign_id: int, db: Session = Depends(get_db)):
    db.delete(_get_or_404(db, models.Campaign, campaign_id))
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
