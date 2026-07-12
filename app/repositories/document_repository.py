import uuid

from sqlalchemy.orm import Session

from app.models.document import Document, DocumentStatus


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
    return db.query(Document).filter(Document.id == document_id).first()


def list_by_collection(db: Session, collection_id: uuid.UUID) -> list[Document]:
    return db.query(Document).filter(Document.collection_id == collection_id).all()


def update_status(
    db: Session, document: Document, status: DocumentStatus, page_count: int | None = None
) -> Document:
    document.status = status
    if page_count is not None:
        document.page_count = page_count
    db.commit()
    db.refresh(document)
    return document


def delete(db: Session, document: Document) -> None:
    db.delete(document)  # cascades to chunks
    db.commit()
