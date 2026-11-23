import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, status
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import models, schemas
from .auth import CurrentUser, get_current_user, require_internal
from .database import get_db, init_db


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="payments-service", lifespan=lifespan)


def _get_visible_payment(db: Session, payment_id: int, user: CurrentUser) -> models.Payment:
    payment = db.get(models.Payment, payment_id)
    if not payment or (payment.user_id != user.id and not user.is_admin):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Payment not found")
    return payment


def _payment_by_key(db: Session, key: str) -> models.Payment | None:
    return db.scalar(select(models.Payment).where(models.Payment.idempotency_key == key))


@app.get("/health")
def health():
    return {"status": "ok", "service": "payments"}


@app.post(
    "/payments",
    response_model=schemas.PaymentOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_internal)],
)
def create_payment(payload: schemas.PaymentCreate, db: Session = Depends(get_db)):
    """Mock charge. Always records the attempt; cards ending in 0000 are declined."""
    if payload.idempotency_key and (existing := _payment_by_key(db, payload.idempotency_key)):
        return existing
    card = (payload.card_number or "").replace(" ", "")
    declined = card.endswith("0000")
    payment = models.Payment(
        order_id=payload.order_id,
        user_id=payload.user_id,
        amount=round(payload.amount, 2),
        refunded_amount=0,
        currency=payload.currency.upper(),
        status="failed" if declined else "succeeded",
        card_last4=card[-4:] or None,
        transaction_ref=f"mock_{uuid.uuid4().hex}",
        failure_reason="Card declined" if declined else None,
        idempotency_key=payload.idempotency_key,
    )
    db.add(payment)
    try:
        db.commit()
    except IntegrityError:  # concurrent request with the same key won
        db.rollback()
        return _payment_by_key(db, payload.idempotency_key)
    db.refresh(payment)
    return payment


@app.post("/payments/void", response_model=schemas.PaymentOut, dependencies=[Depends(require_internal)])
def void_payment(payload: schemas.PaymentVoid, db: Session = Depends(get_db)):
    """Cancel the charge made under a key; safe to repeat and to call for a charge that never arrived."""
    Payment = models.Payment
    db.execute(
        insert(Payment)
        .values(
            order_id=payload.order_id,
            user_id=payload.user_id,
            amount=0,
            refunded_amount=0,
            currency="USD",
            status="voided",
            transaction_ref=f"mock_{uuid.uuid4().hex}",
            failure_reason="Voided before charge",
            idempotency_key=payload.idempotency_key,
        )
        .on_conflict_do_nothing(index_elements=[Payment.idempotency_key])
    )
    payment = db.scalar(select(Payment).where(Payment.idempotency_key == payload.idempotency_key).with_for_update())
    if payment.status == "succeeded":
        payment.status, payment.failure_reason = "voided", "Voided: checkout failed"
    db.commit()
    db.refresh(payment)
    return payment


@app.post(
    "/payments/refunds",
    response_model=schemas.RefundOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_internal)],
)
def create_refund(payload: schemas.RefundCreate, db: Session = Depends(get_db)):
    payment = db.scalar(select(models.Payment).where(models.Payment.id == payload.payment_id).with_for_update())
    if not payment:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Payment not found")
    if payload.idempotency_key:
        existing = db.scalar(select(models.Refund).where(models.Refund.idempotency_key == payload.idempotency_key))
        if existing:
            return existing
    if payment.status not in ("succeeded", "partially_refunded"):
        raise HTTPException(status.HTTP_409_CONFLICT, f"Cannot refund a payment with status '{payment.status}'")
    amount = round(payload.amount, 2)
    refundable = round(payment.amount - payment.refunded_amount, 2)
    if amount > refundable:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Refund exceeds refundable amount ({refundable})")

    refund = models.Refund(
        payment_id=payment.id,
        amount=amount,
        reason=payload.reason,
        idempotency_key=payload.idempotency_key,
        status="succeeded",
    )
    payment.refunded_amount = round(payment.refunded_amount + amount, 2)
    payment.status = "refunded" if payment.refunded_amount >= payment.amount else "partially_refunded"
    db.add(refund)
    db.commit()
    db.refresh(refund)
    return refund


@app.get("/payments", response_model=list[schemas.PaymentOut])
def list_payments(
    order_id: int | None = None,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    query = select(models.Payment).order_by(models.Payment.id.desc())
    if not user.is_admin:
        query = query.where(models.Payment.user_id == user.id)
    if order_id is not None:
        query = query.where(models.Payment.order_id == order_id)
    return db.scalars(query).all()


@app.get("/payments/{payment_id}", response_model=schemas.PaymentOut)
def get_payment(payment_id: int, user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    return _get_visible_payment(db, payment_id, user)


@app.get("/payments/{payment_id}/refunds", response_model=list[schemas.RefundOut])
def list_refunds(payment_id: int, user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    return _get_visible_payment(db, payment_id, user).refunds
