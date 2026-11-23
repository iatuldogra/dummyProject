import logging
import os
import time

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import DeclarativeBase, sessionmaker

logger = logging.getLogger(__name__)


def _database_url() -> str | URL:
    if url := os.getenv("DATABASE_URL"):
        return url
    return URL.create(
        "postgresql+psycopg2",
        username=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", "postgres"),
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "5432")),
        database=os.getenv("DB_NAME", "orders_db"),
    )


engine = create_engine(_database_url(), pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


# create_all only creates missing tables; columns added to existing tables are added here.
MIGRATIONS = [
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_attempted BOOLEAN NOT NULL DEFAULT false",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS needs_compensation BOOLEAN NOT NULL DEFAULT false",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS from_cart BOOLEAN NOT NULL DEFAULT false",
    "CREATE INDEX IF NOT EXISTS ix_orders_needs_compensation ON orders (needs_compensation)",
]


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db(retries: int = 30, delay: float = 2.0) -> None:
    """Create tables, retrying while Postgres is still starting up."""
    for attempt in range(1, retries + 1):
        try:
            Base.metadata.create_all(bind=engine)
            with engine.begin() as conn:
                for stmt in MIGRATIONS:
                    conn.execute(text(stmt))
            return
        except Exception as exc:  # noqa: BLE001
            if attempt == retries:
                raise
            logger.warning("Database not ready (attempt %s/%s): %s", attempt, retries, exc)
            time.sleep(delay)
