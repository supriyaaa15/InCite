from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db

router = APIRouter(prefix="/health", tags=["health"])


@router.get("")
def health():
    """Is the app process alive at all."""
    return {"status": "ok"}


@router.get("/db")
def health_db(db: Session = Depends(get_db)):
    """Can we actually reach Postgres and run a query."""
    try:
        db.execute(text("SELECT 1"))
        return {"status": "ok", "database": "connected"}
    except Exception as e:
        return {"status": "error", "database": "unreachable", "detail": str(e)}


@router.get("/chroma")
def health_chroma():
    """Can we actually reach Chroma."""
    try:
        import chromadb

        client = chromadb.HttpClient(host=settings.CHROMA_HOST, port=settings.CHROMA_PORT)
        client.heartbeat()
        return {"status": "ok", "chroma": "connected"}
    except Exception as e:
        return {"status": "error", "chroma": "unreachable", "detail": str(e)}
