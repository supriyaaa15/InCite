import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.collection import Collection
from app.models.document import Document, DocumentStatus
from app.repositories import collection_repository


def create(db: Session, collection_id: uuid.UUID, filename: str, file_path: str) -> Document:
    document = Document(
        collection_id=collection_id,
        filename=filename,
        file_path=file_path,
        status=DocumentStatus.processing,
    )
    db.add(document)
    db.commit()
    db.refresh(document)
    return document


def get_by_id(db: Session, document_id: uuid.UUID) -> Document | None:
    return db.scalar(select(Document).join(Collection).where(
        Document.id == document_id,
        Document.delete_requested_at.is_(None),
        Collection.delete_requested_at.is_(None),
    ).execution_options(populate_existing=True))


def collection_id_for(db: Session, document_id: uuid.UUID) -> uuid.UUID | None:
    return db.scalar(select(Document.collection_id).where(Document.id == document_id))


def lock_by_id(db: Session, document_id: uuid.UUID, collection_id: uuid.UUID) -> Document | None:
    return db.scalar(
        select(Document).where(Document.id == document_id, Document.collection_id == collection_id)
        .with_for_update().execution_options(populate_existing=True)
    )


def lock_processing(db: Session, document_id: uuid.UUID, collection_id: uuid.UUID) -> Document | None:
    collection = collection_repository.lock_by_id(db, collection_id)
    if collection is None or collection.delete_requested_at is not None:
        return None
    document = lock_by_id(db, document_id, collection_id)
    if document is None or document.delete_requested_at is not None or document.status != DocumentStatus.processing:
        return None
    return document


def get_by_ids(db: Session, document_ids: list[uuid.UUID]) -> list[Document]:
    """Bulk lookup for the legacy BM25 filename resolution path."""
    return list(db.scalars(select(Document).join(Collection).where(
        Document.id.in_(document_ids),
        Document.delete_requested_at.is_(None),
        Collection.delete_requested_at.is_(None),
    )))


def get_ready_document_names(
    db: Session, collection_id: uuid.UUID, document_ids: set[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """Return current filenames for the ready candidates in this collection."""
    statement = select(Document.id, Document.filename).join(Collection).where(
        Document.id.in_(document_ids),
        Document.collection_id == collection_id,
        Document.status == DocumentStatus.ready,
        Document.delete_requested_at.is_(None),
        Collection.delete_requested_at.is_(None),
    )
    return {document_id: filename for document_id, filename in db.execute(statement)}


def list_by_collection(db: Session, collection_id: uuid.UUID) -> list[Document]:
    return list(db.scalars(select(Document).join(Collection).where(
        Document.collection_id == collection_id,
        Document.delete_requested_at.is_(None),
        Collection.delete_requested_at.is_(None),
    )))


def finish_processing(
    db: Session,
    document: Document,
    status: DocumentStatus,
    page_count: int | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> bool:
    current = lock_processing(db, document.id, document.collection_id)
    if current is None:
        db.rollback()
        return False
    current.status = status
    if page_count is not None:
        current.page_count = page_count
    if error_code is not None:
        current.error_code = error_code
    if error_message is not None:
        current.error_message = error_message
    db.commit()
    return True
