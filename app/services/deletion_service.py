"""Commit logical deletion requests for the physical cleanup worker."""

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy.orm import Session

from app.core import bm25_store
from app.repositories import collection_repository, document_repository


class DeletionTargetNotFound(Exception):
    pass


def request_deletion(db: Session, user_id: UUID, *, collection_id: UUID | None = None,
                     document_id: UUID | None = None) -> bool:
    """Return whether an archived collection has already completed cleanup."""
    try:
        if document_id is not None:
            collection_id = document_repository.collection_id_for(db, document_id)
        if collection_id is None:
            raise DeletionTargetNotFound()
        collection = collection_repository.lock_by_id(db, collection_id, user_id=user_id)
        if collection is None:
            raise DeletionTargetNotFound()
        if document_id is None and collection.cleanup_completed_at is not None:
            db.commit()
            return True

        target = collection
        if document_id is not None:
            target = document_repository.lock_by_id(db, document_id, collection_id)
            if target is None:
                raise DeletionTargetNotFound()
        if target.delete_requested_at is None:
            target.delete_requested_at = datetime.now(timezone.utc)
        db.commit()
    except Exception:
        db.rollback()
        raise
    bm25_store.invalidate(collection_id)
    return False
