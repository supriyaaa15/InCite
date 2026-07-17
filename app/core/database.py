from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings

engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,  # test each connection before reuse — serverless
    # Postgres (Neon) can close idle connections server-side; without this,
    # SQLAlchemy hands out a stale connection and the query fails with
    # "SSL connection has been closed unexpectedly" instead of transparently
    # reconnecting.
    pool_recycle=300,  # proactively recycle connections every 5 minutes,
    # well under any idle-timeout window a managed provider might enforce
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    """FastAPI dependency — yields a DB session, always closes it after the request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()