from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Response, status
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from . import models, schemas
from .auth import require_admin, require_internal
from .database import get_db, init_db


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="products-service", lifespan=lifespan)


def _get_product(db: Session, product_id: int) -> models.Product:
    product = db.get(models.Product, product_id)
    if not product:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Product not found")
    return product


@app.get("/health")
def health():
    return {"status": "ok", "service": "products"}


@app.post(
    "/products",
    response_model=schemas.ProductOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
)
def create_product(payload: schemas.ProductCreate, db: Session = Depends(get_db)):
    product = models.Product(**payload.model_dump())
    db.add(product)
    db.commit()
    db.refresh(product)
    return product


@app.get("/products", response_model=list[schemas.ProductOut])
def list_products(category: str | None = None, skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    query = select(models.Product).order_by(models.Product.id)
    if category:
        query = query.where(models.Product.category == category)
    return db.scalars(query.offset(skip).limit(limit)).all()


@app.get("/products/{product_id}", response_model=schemas.ProductOut)
def get_product(product_id: int, db: Session = Depends(get_db)):
    return _get_product(db, product_id)


@app.patch("/products/{product_id}", response_model=schemas.ProductOut, dependencies=[Depends(require_admin)])
def update_product(product_id: int, payload: schemas.ProductUpdate, db: Session = Depends(get_db)):
    product = _get_product(db, product_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(product, field, value)
    db.commit()
    db.refresh(product)
    return product


@app.delete("/products/{product_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(require_admin)])
def delete_product(product_id: int, db: Session = Depends(get_db)):
    db.delete(_get_product(db, product_id))
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/products/{product_id}/inventory", response_model=schemas.InventoryOut)
def get_inventory(product_id: int, db: Session = Depends(get_db)):
    product = _get_product(db, product_id)
    return schemas.InventoryOut(product_id=product.id, stock=product.stock)


@app.put("/products/{product_id}/inventory", response_model=schemas.InventoryOut, dependencies=[Depends(require_admin)])
def set_inventory(product_id: int, payload: schemas.InventorySet, db: Session = Depends(get_db)):
    product = _get_product(db, product_id)
    product.stock = payload.stock
    db.commit()
    return schemas.InventoryOut(product_id=product.id, stock=product.stock)


@app.post(
    "/products/{product_id}/inventory/adjust",
    response_model=schemas.InventoryOut,
    dependencies=[Depends(require_internal)],
)
def adjust_inventory(product_id: int, payload: schemas.InventoryAdjust, db: Session = Depends(get_db)):
    """Atomically add/subtract stock; used by orders-service at checkout."""
    new_stock = db.execute(
        update(models.Product)
        .where(models.Product.id == product_id, models.Product.stock + payload.delta >= 0)
        .values(stock=models.Product.stock + payload.delta)
        .returning(models.Product.stock)
    ).scalar_one_or_none()
    if new_stock is None:
        db.rollback()
        _get_product(db, product_id)
        raise HTTPException(status.HTTP_409_CONFLICT, "Insufficient stock")
    db.commit()
    return schemas.InventoryOut(product_id=product_id, stock=new_stock)
