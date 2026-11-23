import logging
import os
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models, schemas
from .auth import INTERNAL_API_KEY, CurrentUser, get_current_user, require_admin
from .database import get_db, init_db

logger = logging.getLogger("support")

ORDERS_URL = os.getenv("ORDERS_SERVICE_URL", "http://orders-service").rstrip("/")
PAYMENTS_URL = os.getenv("PAYMENTS_SERVICE_URL", "http://payments-service").rstrip("/")
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "5"))


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="support-service", lifespan=lifespan)


def _call(method: str, url: str, **kwargs) -> httpx.Response:
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT) as client:
            return client.request(method, url, **kwargs)
    except httpx.HTTPError as exc:
        logger.error("Call to %s failed: %s", url, exc)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Upstream service unavailable: {url}")


def fetch_order(order_id: int, user: CurrentUser) -> dict:
    """Verify the order exists and belongs to the caller by forwarding their token to orders-service."""
    resp = _call("GET", f"{ORDERS_URL}/orders/{order_id}", headers={"Authorization": f"Bearer {user.token}"})
    if resp.status_code == 404:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Order {order_id} not found")
    if resp.is_error:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"orders-service error {resp.status_code}")
    return resp.json()


def request_refund(payment_id: int, amount: float, reason: str, idempotency_key: str) -> dict:
    resp = _call(
        "POST",
        f"{PAYMENTS_URL}/payments/refunds",
        json={"payment_id": payment_id, "amount": amount, "reason": reason, "idempotency_key": idempotency_key},
        headers={"X-Internal-Token": INTERNAL_API_KEY},
    )
    if resp.is_error:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Refund failed: {resp.text[:200]}")
    return resp.json()


def _get_visible(db: Session, model, obj_id: int, user: CurrentUser):
    obj = db.get(model, obj_id)
    if not obj or (obj.user_id != user.id and not user.is_admin):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return obj


@app.get("/health")
def health():
    return {"status": "ok", "service": "support"}


# ---------- Complaints ----------


@app.post("/support/complaints", response_model=schemas.ComplaintOut, status_code=status.HTTP_201_CREATED)
def create_complaint(
    payload: schemas.ComplaintCreate,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if payload.order_id is not None:
        fetch_order(payload.order_id, user)
    complaint = models.Complaint(user_id=user.id, status="open", **payload.model_dump())
    db.add(complaint)
    db.commit()
    db.refresh(complaint)
    return complaint


@app.get("/support/complaints", response_model=list[schemas.ComplaintOut])
def list_complaints(
    status_filter: schemas.ComplaintStatus | None = None,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    query = select(models.Complaint).order_by(models.Complaint.id.desc())
    if not user.is_admin:
        query = query.where(models.Complaint.user_id == user.id)
    if status_filter:
        query = query.where(models.Complaint.status == status_filter)
    return db.scalars(query).all()


@app.get("/support/complaints/{complaint_id}", response_model=schemas.ComplaintOut)
def get_complaint(complaint_id: int, user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    return _get_visible(db, models.Complaint, complaint_id, user)


@app.patch(
    "/support/complaints/{complaint_id}",
    response_model=schemas.ComplaintOut,
    dependencies=[Depends(require_admin)],
)
def update_complaint(complaint_id: int, payload: schemas.ComplaintUpdate, db: Session = Depends(get_db)):
    complaint = db.get(models.Complaint, complaint_id)
    if not complaint:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Complaint not found")
    complaint.status = payload.status
    db.commit()
    db.refresh(complaint)
    return complaint


# ---------- Returns ----------


@app.post("/support/returns", response_model=schemas.ReturnOut, status_code=status.HTTP_201_CREATED)
def create_return(
    payload: schemas.ReturnCreate,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    order = fetch_order(payload.order_id, user)
    if order["status"] != "paid":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Order status is '{order['status']}', only paid orders can be returned")

    purchased = {i["product_id"]: i for i in order["items"]}
    previous = db.scalars(
        select(models.ReturnRequest).where(
            models.ReturnRequest.order_id == payload.order_id, models.ReturnRequest.status != "rejected"
        )
    ).all()
    already_returned: dict[int, int] = {}
    for r in previous:
        for item in r.items:
            already_returned[item["product_id"]] = already_returned.get(item["product_id"], 0) + item["quantity"]

    requested: dict[int, int] = {}
    for item in payload.items:
        requested[item.product_id] = requested.get(item.product_id, 0) + item.quantity

    # Refund is proportional to what was actually paid (i.e. after campaign + promo discounts).
    paid_ratio = order["total"] / order["subtotal"] if order["subtotal"] else 0
    refund_amount = 0.0
    for product_id, quantity in requested.items():
        line = purchased.get(product_id)
        if not line:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Product {product_id} is not part of order")
        if quantity + already_returned.get(product_id, 0) > line["quantity"]:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Return quantity exceeds purchased for {product_id}")
        refund_amount += line["unit_price"] * quantity * paid_ratio

    # Avoid per-return rounding drift: never exceed what's left, and refund the exact remainder
    # once everything in the order has been returned.
    already_refundable = round(sum(r.refund_amount for r in previous), 2)
    remaining = round(order["total"] - already_refundable, 2)
    fully_returned = all(
        already_returned.get(pid, 0) + requested.get(pid, 0) >= line["quantity"] for pid, line in purchased.items()
    )
    refund_amount = remaining if fully_returned else min(round(refund_amount, 2), remaining)

    ret = models.ReturnRequest(
        user_id=user.id,
        order_id=payload.order_id,
        items=[{"product_id": pid, "quantity": qty} for pid, qty in requested.items()],
        reason=payload.reason,
        status="requested",
        refund_amount=max(refund_amount, 0.0),
        payment_id=order.get("payment_id"),
    )
    db.add(ret)
    db.commit()
    db.refresh(ret)
    return ret


@app.get("/support/returns", response_model=list[schemas.ReturnOut])
def list_returns(user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    query = select(models.ReturnRequest).order_by(models.ReturnRequest.id.desc())
    if not user.is_admin:
        query = query.where(models.ReturnRequest.user_id == user.id)
    return db.scalars(query).all()


@app.get("/support/returns/{return_id}", response_model=schemas.ReturnOut)
def get_return(return_id: int, user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    return _get_visible(db, models.ReturnRequest, return_id, user)


@app.patch("/support/returns/{return_id}", response_model=schemas.ReturnOut, dependencies=[Depends(require_admin)])
def decide_return(return_id: int, payload: schemas.ReturnDecision, db: Session = Depends(get_db)):
    """Approve (-> refund via payments-service) or reject a return request."""
    ret = db.scalar(select(models.ReturnRequest).where(models.ReturnRequest.id == return_id).with_for_update())
    if not ret:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Return not found")
    # "approved" is allowed again so a failed refund can be retried.
    if ret.status not in ("requested", "approved") or (ret.status == "approved" and payload.status == "rejected"):
        raise HTTPException(status.HTTP_409_CONFLICT, f"Return is already '{ret.status}'")

    if payload.status == "rejected":
        ret.status = "rejected"
        db.commit()
        db.refresh(ret)
        return ret

    ret.status = "approved"
    db.commit()
    if ret.payment_id and ret.refund_amount > 0:
        refund = request_refund(
            ret.payment_id,
            ret.refund_amount,
            f"Return #{ret.id}: {ret.reason}"[:500],
            idempotency_key=f"return-{ret.id}",
        )
        ret.refund_id = refund["id"]
    ret.status = "refunded"
    db.commit()
    db.refresh(ret)
    return ret
