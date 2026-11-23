import logging
import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import Depends, FastAPI, HTTPException, Response, status
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from . import models, schemas
from .auth import INTERNAL_API_KEY, CurrentUser, get_current_user
from .database import SessionLocal, get_db, init_db

logger = logging.getLogger("orders")

PRODUCTS_URL = os.getenv("PRODUCTS_SERVICE_URL", "http://products-service").rstrip("/")
PROMOTIONS_URL = os.getenv("PROMOTIONS_SERVICE_URL", "http://promotions-service").rstrip("/")
PAYMENTS_URL = os.getenv("PAYMENTS_SERVICE_URL", "http://payments-service").rstrip("/")
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "5"))
INTERNAL_HEADERS = {"X-Internal-Token": INTERNAL_API_KEY}
RECONCILE_INTERVAL = float(os.getenv("RECONCILE_INTERVAL", "30"))
# Checkouts still pending after this long were interrupted (each runs a handful of HTTP_TIMEOUT-bound calls).
STALE_PENDING_AFTER = timedelta(minutes=float(os.getenv("STALE_PENDING_MINUTES", "15")))


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    stop = threading.Event()
    worker = threading.Thread(target=_reconcile_loop, args=(stop,), daemon=True)
    worker.start()
    yield
    stop.set()


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


def _order_key(order: models.Order) -> str:
    return f"order-{order.id}"


def _stock_key(order: models.Order, product_id: int) -> str:
    return f"order-{order.id}-product-{product_id}"


def reserve_stock(order: models.Order, product_id: int, quantity: int) -> bool:
    resp = _call(
        "POST",
        f"{PRODUCTS_URL}/products/{product_id}/inventory/reserve",
        json={"key": _stock_key(order, product_id), "quantity": quantity},
        headers=INTERNAL_HEADERS,
    )
    if resp.status_code == 409:
        return False
    if resp.is_error:
        raise _upstream_error(resp)
    return True


def release_stock(order: models.Order, product_id: int) -> None:
    resp = _call(
        "POST",
        f"{PRODUCTS_URL}/products/{product_id}/inventory/release",
        json={"key": _stock_key(order, product_id)},
        headers=INTERNAL_HEADERS,
    )
    if resp.is_error:
        raise _upstream_error(resp)


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


def redeem_promo(order: models.Order) -> bool:
    """Consume one use of the order's promo code; False if it was used up concurrently."""
    resp = _call(
        "POST",
        f"{PROMOTIONS_URL}/promotions/promo-codes/redeem",
        json={"code": order.promo_code, "key": _order_key(order)},
        headers=INTERNAL_HEADERS,
    )
    if resp.status_code == 409:
        return False
    if resp.is_error:
        raise _upstream_error(resp)
    return True


def release_promo(order: models.Order) -> None:
    resp = _call(
        "POST",
        f"{PROMOTIONS_URL}/promotions/promo-codes/release",
        json={"code": order.promo_code, "key": _order_key(order)},
        headers=INTERNAL_HEADERS,
    )
    if resp.is_error:
        raise _upstream_error(resp)


def charge(order: models.Order, card_number: str | None) -> dict:
    resp = _call(
        "POST",
        f"{PAYMENTS_URL}/payments",
        json={
            "order_id": order.id,
            "user_id": order.user_id,
            "amount": order.total,
            "card_number": card_number,
            "idempotency_key": _order_key(order),
        },
        headers=INTERNAL_HEADERS,
    )
    if resp.is_error:
        raise _upstream_error(resp)
    return resp.json()


def void_payment(order: models.Order) -> None:
    resp = _call(
        "POST",
        f"{PAYMENTS_URL}/payments/void",
        json={"idempotency_key": _order_key(order), "order_id": order.id, "user_id": order.user_id},
        headers=INTERNAL_HEADERS,
    )
    if resp.is_error:
        raise _upstream_error(resp)


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


def _compensate(order: models.Order) -> bool:
    """Undo a failed checkout's side effects. Every call is keyed by the order, so repeating is safe
    and also covers calls whose response was lost. Returns False if something must be retried."""
    actions = [(release_stock, item.product_id) for item in order.items]
    if order.promo_code:
        actions.append((release_promo,))
    if order.payment_attempted:
        actions.append((void_payment,))
    done = True
    for action, *args in actions:
        try:
            action(order, *args)
        except HTTPException as exc:
            logger.error("Compensation %s%s for order %s failed: %s", action.__name__, args, order.id, exc.detail)
            done = False
    return done


def _fail_order(db: Session, order: models.Order, reason: str) -> None:
    order.status, order.failure_reason, order.needs_compensation = "failed", reason, True
    db.commit()
    if _compensate(order):
        order.needs_compensation = False
        db.commit()


def reconcile_orders() -> None:
    """Fail interrupted checkouts and retry compensations that couldn't complete earlier."""
    with SessionLocal() as db:
        cutoff = datetime.now(timezone.utc) - STALE_PENDING_AFTER
        stale = db.scalars(
            select(models.Order)
            .where(models.Order.status == "pending", models.Order.created_at < cutoff)
            .with_for_update(skip_locked=True)
        ).all()
        for order in stale:
            order.status, order.failure_reason, order.needs_compensation = "failed", "Checkout interrupted", True
            if order.from_cart:
                for item in sorted(order.items, key=lambda i: i.product_id):
                    _add_cart_quantity(db, order.user_id, item.product_id, item.quantity)
        db.commit()
        for order in db.scalars(select(models.Order).where(models.Order.needs_compensation.is_(True))).all():
            if _compensate(order):
                order.needs_compensation = False
                db.commit()


def _reconcile_loop(stop: threading.Event) -> None:
    while not stop.wait(RECONCILE_INTERVAL):
        try:
            reconcile_orders()
        except Exception:  # noqa: BLE001
            logger.exception("Order reconciliation failed")


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
    _add_cart_quantity(db, user.id, line.product_id, line.quantity)
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


def _add_cart_quantity(db: Session, user_id: int, product_id: int, quantity: int) -> None:
    """Atomic upsert so concurrent cart writes add up instead of colliding."""
    stmt = insert(models.CartItem).values(user_id=user_id, product_id=product_id, quantity=quantity)
    db.execute(
        stmt.on_conflict_do_update(
            index_elements=[models.CartItem.user_id, models.CartItem.product_id],
            set_={"quantity": models.CartItem.quantity + stmt.excluded.quantity},
        )
    )


def _restore_cart(db: Session, user_id: int, lines: dict[int, int]) -> None:
    db.rollback()
    # Fixed product order so overlapping restores for one user can't deadlock.
    for product_id, quantity in sorted(lines.items()):
        _add_cart_quantity(db, user_id, product_id, quantity)
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
        order = _price_order(user, lines, payload)
        db.add(order)
        db.commit()
        return _complete_order(db, order, payload)

    cart = db.execute(
        select(models.CartItem.product_id, models.CartItem.quantity).where(models.CartItem.user_id == user.id)
    ).all()
    lines = dict(cart)
    if not lines:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cart is empty")
    order = _price_order(user, lines, payload)
    # Claim the cart and save the order in one transaction: concurrent checkouts can't buy the same
    # cart twice, and a crash can't lose the items.
    claimed = db.execute(
        delete(models.CartItem)
        .where(models.CartItem.user_id == user.id)
        .returning(models.CartItem.product_id, models.CartItem.quantity)
    ).all()
    if dict(claimed) != lines:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Cart changed during checkout, please retry")
    order.from_cart = True
    db.add(order)
    db.commit()
    try:
        return _complete_order(db, order, payload)
    except Exception:
        _restore_cart(db, user.id, lines)
        raise


def _price_order(user: CurrentUser, lines: dict[int, int], payload: schemas.CheckoutRequest) -> models.Order:
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
    return models.Order(
        user_id=user.id,
        status="pending",
        subtotal=subtotal,
        discount=discount,
        total=round(subtotal - discount, 2),
        promo_code=promo_code,
        items=items,
    )


def _complete_order(db: Session, order: models.Order, payload: schemas.CheckoutRequest) -> models.Order:
    def fail(code: int, reason: str):
        _fail_order(db, order, reason)
        raise HTTPException(code, reason)

    try:
        # 3. Reserve inventory (products-service).
        for item in order.items:
            if not reserve_stock(order, item.product_id, item.quantity):
                fail(status.HTTP_409_CONFLICT, f"Insufficient stock for product {item.product_id}")

        # 4. Consume the promo code before charging so its usage limit can't be exceeded.
        if order.promo_code and not redeem_promo(order):
            fail(status.HTTP_400_BAD_REQUEST, "Promo code usage limit reached")

        # 5. Charge (payments-service).
        if order.total > 0:
            order.payment_attempted = True
            db.commit()
            payment = charge(order, payload.card_number)
            order.payment_id = payment["id"]
            if payment["status"] != "succeeded":
                fail(status.HTTP_402_PAYMENT_REQUIRED, payment.get("failure_reason") or "Payment failed")
    except HTTPException as exc:
        if order.status != "failed":
            fail(exc.status_code, str(exc.detail))
        raise

    # Only a still-pending order may become paid; reconciliation may have failed it meanwhile.
    paid = db.execute(
        update(models.Order)
        .where(models.Order.id == order.id, models.Order.status == "pending")
        .values(status="paid", payment_id=order.payment_id)
    ).rowcount
    db.commit()
    if not paid:
        db.refresh(order)
        fail(status.HTTP_409_CONFLICT, order.failure_reason or "Checkout interrupted")
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
