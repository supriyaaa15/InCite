from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.chroma_client import get_chroma_client
from app.core.database import get_db

router = APIRouter(prefix="/health", tags=["health"])


@router.get("")
@router.head("")
def health():
    """Is the app process alive at all. HEAD supported explicitly —
    uptime monitors (UptimeRobot etc.) commonly default to HEAD requests
    to save bandwidth, and FastAPI doesn't auto-add HEAD for a GET route."""
    return {"status": "ok"}


@router.get("/db")
@router.head("/db")
def health_db(db: Session = Depends(get_db)):
    """Can we actually reach Postgres and run a query."""
    try:
        db.execute(text("SELECT 1"))
        return {"status": "ok", "database": "connected"}
    except Exception as e:
        return {"status": "error", "database": "unreachable", "detail": str(e)}


@router.get("/chroma")
@router.head("/chroma")
def health_chroma():
    """
    Can we actually reach Chroma. Embedded (PersistentClient) now, not a
    separate server — this checks that the local persist path is
    readable/writable, not network connectivity. Uses the same shared
    client singleton as ingestion and retrieval, rather than constructing
    its own — that was a pre-existing inconsistency worth fixing while
    touching this file anyway.
    """
    try:
        client = get_chroma_client()
        client.heartbeat()
        return {"status": "ok", "chroma": "connected"}
    except Exception as e:
        return {"status": "error", "chroma": "unreachable", "detail": str(e)}