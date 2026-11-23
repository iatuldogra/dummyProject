import logging
import os
import threading
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import models, schemas
from .auth import (
    CurrentUser,
    create_access_token,
    get_current_user,
    hash_password,
    require_admin,
    verify_password,
)
from .database import SessionLocal, get_db, init_db

logger = logging.getLogger("users")

ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "").strip().lower()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
LOGIN_MAX_FAILURES = int(os.getenv("LOGIN_MAX_FAILURES", "5"))
LOGIN_LOCKOUT_SECONDS = float(os.getenv("LOGIN_LOCKOUT_SECONDS", "900"))

_login_failures: dict[str, list[float]] = {}
_login_lock = threading.Lock()


def _recent_failures(email: str, now: float) -> list[float]:
    failures = [t for t in _login_failures.get(email, []) if now - t < LOGIN_LOCKOUT_SECONDS]
    if failures:
        _login_failures[email] = failures
    else:
        _login_failures.pop(email, None)
    return failures


def seed_admin() -> None:
    """Create (or take over) the bootstrap admin account from ADMIN_EMAIL / ADMIN_PASSWORD."""
    if not ADMIN_EMAIL or not ADMIN_PASSWORD:
        return
    with SessionLocal() as db:
        user = db.scalar(select(models.User).where(models.User.email == ADMIN_EMAIL))
        if user is None:
            user = models.User(email=ADMIN_EMAIL, full_name="Administrator")
            db.add(user)
        # Always reset the password so a pre-registered account with this email can't keep access.
        user.password_hash = hash_password(ADMIN_PASSWORD)
        user.role = "admin"
        try:
            db.commit()
        except IntegrityError:  # another replica seeded it concurrently
            db.rollback()
            logger.info("Admin account already seeded by another replica")


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    seed_admin()
    yield


app = FastAPI(title="users-service", lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok", "service": "users"}


@app.post("/users/register", response_model=schemas.UserOut, status_code=status.HTTP_201_CREATED)
def register(payload: schemas.UserCreate, db: Session = Depends(get_db)):
    email = payload.email.lower()
    user = models.User(
        email=email,
        full_name=payload.full_name,
        password_hash=hash_password(payload.password),
        role="customer",
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")
    db.refresh(user)
    return user


@app.post("/users/login", response_model=schemas.Token)
def login(payload: schemas.LoginRequest, db: Session = Depends(get_db)):
    email = payload.email.lower()
    with _login_lock:
        if len(_recent_failures(email, time.monotonic())) >= LOGIN_MAX_FAILURES:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many failed logins, try again later")
    user = db.scalar(select(models.User).where(models.User.email == email))
    if not user or not verify_password(payload.password, user.password_hash):
        with _login_lock:
            now = time.monotonic()
            _login_failures[email] = _recent_failures(email, now) + [now]
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")
    with _login_lock:
        _login_failures.pop(email, None)
    return schemas.Token(access_token=create_access_token(user.id, user.email, user.role))


@app.get("/users/me", response_model=schemas.UserOut)
def me(current: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    user = db.get(models.User, current.id)
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    return user


@app.get("/users", response_model=list[schemas.UserOut], dependencies=[Depends(require_admin)])
def list_users(skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    return db.scalars(select(models.User).order_by(models.User.id).offset(skip).limit(limit)).all()


@app.get("/users/{user_id}", response_model=schemas.UserOut, dependencies=[Depends(require_admin)])
def get_user(user_id: int, db: Session = Depends(get_db)):
    user = db.get(models.User, user_id)
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    return user
