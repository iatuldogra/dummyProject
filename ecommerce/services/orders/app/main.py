import logging
import os
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, HTTPException, Response, status
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from . import models, schemas
from .auth import INTERNAL_API_KEY, CurrentUser, get_current_user
from .database import get_db, init_db

logger = logging.getLogger("orders")

PRODUCTS_URL = os.getenv("PRODUCTS_SERVICE_URL", "http://products-service").rstrip("/")
PROMOTIONS_URL = os.getenv("PROMOTIONS_SERVICE_URL", "http://promotions-service").rstrip("/")
PAYMENTS_URL = os.getenv("PAYMENTS_SERVICE_URL", "http://payments-service").rstrip("/")
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "5"))
INTERNAL_HEADERS = {"X-Internal-Token": INTERNAL_API_KEY}


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="orders-service", lifespan=lifespan)


# ---------- downstream service calls ----------


def _call(method: str, url: str, **kwargs) -> httpx.Response:
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT) as client:
            return client.request(method, url, **kwargs)
    except httpx.HTTPError as exc:
        logger.error("Call to %s failed: %s", url, exc)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Upstream service unavailable: {url}")


def _upstream_error(resp: httpx.Response) -> HTTPException:
    return HTTPException(status.HTTP_502_BAD_GATEWAY, f"Upstream error {resp.status_code}: {resp.text[:200]}")


def fetch_product(product_id: int) -> dict:
    resp = _call("GET", f"{PRODUCTS_URL}/products/{product_id}")
    if resp.status_code == 404:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Product {product_id} not found")
    if resp.is_error:
        raise _upstream_error(resp)
    return resp.json()


def adjust_stock(product_id: int, delta: int) -> bool:
    resp = _call(
        "POST",
        f"{PRODUCTS_URL}/products/{product_id}/inventory/adjust",
        json={"delta": delta},
        headers=INTERNAL_HEADERS,
    )
    if resp.status_code == 409:
        return False
    if resp.is_error:
        raise _upstream_error(resp)
    return True


def fetch_active_campaigns() -> list[dict]:
    resp = _call("GET", f"{PROMOTIONS_URL}/promotions/campaigns/active")
    if resp.is_error:
        raise _upstream_error(resp)
    return resp.json()


def validate_promo(code: str, subtotal: float) -> dict:
    resp = _call("POST", f"{PROMOTIONS_URL}/promotions/promo-codes/validate", json={"code": code, "subtotal": subtotal})
    if resp.is_error:
        raise _upstream_error(resp)
    return resp.json()


def redeem_promo(code: str) -> bool:
    """Consume one use of the promo code; False if it was used up concurrently."""
    resp = _call("POST", f"{PROMOTIONS_URL}/promotions/promo-codes/redeem", json={"code": code}, headers=INTERNAL_HEADERS)
    if resp.status_code == 409:
        return False
    if resp.is_error:
        raise _upstream_error(resp)
    return True


def release_promo(code: str) -> None:
    try:
        resp = _call("POST", f"{PROMOTIONS_URL}/promotions/promo-codes/release", json={"code": code}, headers=INTERNAL_HEADERS)
        if resp.is_error:
            logger.error("Failed to release promo %s: %s", code, resp.text)
    except HTTPException:
        logger.error("Failed to release promo %s: promotions-service unavailable", code)


def charge(order: models.Order, card_number: str | None) -> dict:
    resp = _call(
        "POST",
        f"{PAYMENTS_URL}/payments",
        json={"order_id": order.id, "user_id": order.user_id, "amount": order.total, "card_number": card_number},
        headers=INTERNAL_HEADERS,
    )
    if resp.is_error:
        raise _upstream_error(resp)
    return resp.json()


# ---------- pricing ----------


def _campaign_applies(campaign: dict, product: dict) -> bool:
    scope = campaign["scope"]
    if scope == "catalog":
        return True
    if scope == "category":
        return product["category"] in campaign["categories"]
    return product["id"] in campaign["product_ids"]


def _campaign_line_discount(campaign: dict, unit_price: float, quantity: int) -> float:
    line_total = unit_price * quantity
    if campaign["discount_type"] == "percent":
        return line_total * campaign["discount_value"] / 100
    return min(campaign["discount_value"] * quantity, line_total)


def best_campaign_discount(campaigns: list[dict], product: dict, quantity: int) -> float:
    """Pick the single best active campaign for a line (campaigns don't stack)."""
    discounts = [
        _campaign_line_discount(c, product["price"], quantity) for c in campaigns if _campaign_applies(c, product)
    ]
    return round(max(discounts, default=0.0), 2)


# ---------- helpers ----------


def _get_visible_order(db: Session, order_id: int, user: CurrentUser) -> models.Order:
    order = db.get(models.Order, order_id)
    if not order or (order.user_id != user.id and not user.is_admin):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found")
    return order


def _merge_lines(lines: list[schemas.CartLine]) -> dict[int, int]:
    merged: dict[int, int] = {}
    for line in lines:
        merged[line.product_id] = merged.get(line.product_id, 0) + line.quantity
    return merged


def _release_stock(reserved: dict[int, int]) -> None:
    for product_id, quantity in reserved.items():
        try:
            adjust_stock(product_id, quantity)
        except HTTPException:
            logger.error("Failed to release %s units of product %s", quantity, product_id)


# ---------- endpoints ----------


@app.get("/health")
def health():
    return {"status": "ok", "service": "orders"}


@app.get("/orders/cart", response_model=schemas.CartOut)
def get_cart(user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    items = db.scalars(select(models.CartItem).where(models.CartItem.user_id == user.id).order_by(models.CartItem.id))
    return schemas.CartOut(items=items.all())


@app.post("/orders/cart/items", response_model=schemas.CartOut, status_code=status.HTTP_201_CREATED)
def add_to_cart(line: schemas.CartLine, user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    fetch_product(line.product_id)
    item = db.scalar(
        select(models.CartItem).where(models.CartItem.user_id == user.id, models.CartItem.product_id == line.product_id)
    )
    if item:
        item.quantity += line.quantity
    else:
        db.add(models.CartItem(user_id=user.id, product_id=line.product_id, quantity=line.quantity))
    db.commit()
    return get_cart(user, db)


@app.delete("/orders/cart/items/{product_id}", response_model=schemas.CartOut)
def remove_from_cart(product_id: int, user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    db.execute(
        delete(models.CartItem).where(models.CartItem.user_id == user.id, models.CartItem.product_id == product_id)
    )
    db.commit()
    return get_cart(user, db)


@app.delete("/orders/cart", status_code=status.HTTP_204_NO_CONTENT)
def clear_cart(user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    db.execute(delete(models.CartItem).where(models.CartItem.user_id == user.id))
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _restore_cart(db: Session, user_id: int, lines: dict[int, int]) -> None:
    db.rollback()
    for product_id, quantity in lines.items():
        item = db.scalar(
            select(models.CartItem).where(models.CartItem.user_id == user_id, models.CartItem.product_id == product_id)
        )
        if item:
            item.quantity += quantity
        else:
            db.add(models.CartItem(user_id=user_id, product_id=product_id, quantity=quantity))
    db.commit()


@app.post("/orders/checkout", response_model=schemas.OrderOut, status_code=status.HTTP_201_CREATED)
def checkout(
    payload: schemas.CheckoutRequest,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if payload.items is not None:
        lines = _merge_lines(payload.items)
        if not lines:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "No items to check out")
        return _place_order(db, user, lines, payload)

    # Claim the cart atomically so concurrent checkouts can't buy the same cart twice and items
    # added meanwhile stay in the cart. The claimed items are put back if checkout fails.
    claimed = db.execute(
        delete(models.CartItem)
        .where(models.CartItem.user_id == user.id)
        .returning(models.CartItem.product_id, models.CartItem.quantity)
    ).all()
    db.commit()
    lines = _merge_lines([schemas.CartLine(product_id=pid, quantity=qty) for pid, qty in claimed])
    if not lines:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cart is empty")
    try:
        return _place_order(db, user, lines, payload)
    except Exception:
        _restore_cart(db, user.id, lines)
        raise


def _place_order(
    db: Session, user: CurrentUser, lines: dict[int, int], payload: schemas.CheckoutRequest
) -> models.Order:
    # 1. Price items (products-service) and check stock.
    products = {pid: fetch_product(pid) for pid in lines}
    for pid, qty in lines.items():
        if products[pid]["stock"] < qty:
            raise HTTPException(status.HTTP_409_CONFLICT, f"Insufficient stock for product {pid}")

    # 2. Discounts: active campaigns per line, then promo code on the remainder (promotions-service).
    campaigns = fetch_active_campaigns()
    items = [
        models.OrderItem(
            product_id=pid,
            product_name=products[pid]["name"],
            unit_price=products[pid]["price"],
            quantity=qty,
            discount=best_campaign_discount(campaigns, products[pid], qty),
        )
        for pid, qty in lines.items()
    ]
    subtotal = round(sum(i.unit_price * i.quantity for i in items), 2)
    campaign_discount = round(sum(i.discount for i in items), 2)

    promo_code, promo_discount = None, 0.0
    if payload.promo_code:
        result = validate_promo(payload.promo_code, round(subtotal - campaign_discount, 2))
        if not result["valid"]:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, result["message"])
        promo_code, promo_discount = result["code"], result["discount_amount"]

    discount = round(min(subtotal, campaign_discount + promo_discount), 2)
    order = models.Order(
        user_id=user.id,
        status="pending",
        subtotal=subtotal,
        discount=discount,
        total=round(subtotal - discount, 2),
        promo_code=promo_code,
        items=items,
    )
    db.add(order)
    db.commit()

    reserved: dict[int, int] = {}
    promo_redeemed = False

    def fail(code: int, reason: str):
        _release_stock(reserved)
        if promo_redeemed:
            release_promo(promo_code)
        order.status, order.failure_reason = "failed", reason
        db.commit()
        raise HTTPException(code, reason)

    try:
        # 3. Reserve inventory (products-service).
        for pid, qty in lines.items():
            if not adjust_stock(pid, -qty):
                fail(status.HTTP_409_CONFLICT, f"Insufficient stock for product {pid}")
            reserved[pid] = qty

        # 4. Consume the promo code before charging so its usage limit can't be exceeded.
        if promo_code:
            if not redeem_promo(promo_code):
                fail(status.HTTP_400_BAD_REQUEST, "Promo code usage limit reached")
            promo_redeemed = True

        # 5. Charge (payments-service).
        if order.total > 0:
            payment = charge(order, payload.card_number)
            order.payment_id = payment["id"]
            if payment["status"] != "succeeded":
                fail(status.HTTP_402_PAYMENT_REQUIRED, payment.get("failure_reason") or "Payment failed")
    except HTTPException as exc:
        if order.status != "failed":
            fail(exc.status_code, str(exc.detail))
        raise

    order.status = "paid"
    db.commit()
    db.refresh(order)
    return order


@app.get("/orders", response_model=list[schemas.OrderOut])
def list_orders(user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    query = select(models.Order).order_by(models.Order.id.desc())
    if not user.is_admin:
        query = query.where(models.Order.user_id == user.id)
    return db.scalars(query).all()


@app.get("/orders/{order_id}", response_model=schemas.OrderOut)
def get_order(order_id: int, user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    return _get_visible_order(db, order_id, user)
