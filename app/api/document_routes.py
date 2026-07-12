import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.user import User
from app.repositories import document_repository
from app.schemas.document import DocumentResponse
from app.services import collection_service, ingestion_service
from app.services.collection_service import CollectionNotFoundError
from app.services.storage_service import storage_service

router = APIRouter(tags=["documents"])


@router.post(
    "/collections/{collection_id}/documents",
    response_model=DocumentResponse,
    status_code=status.HTTP_201_CREATED,
)
def upload_document(
    collection_id: uuid.UUID,
    file: UploadFile,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Confirms the collection exists AND belongs to this user before
    # accepting the upload — never trust collection_id alone.
    try:
        collection_service.get_owned_collection(db, current_user.id, collection_id)
    except CollectionNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")

    file_path = storage_service.save_file(str(current_user.id), str(collection_id), file)
    document = document_repository.create(
        db, collection_id=collection_id, filename=file.filename, file_path=file_path
    )

    # Returns immediately with status=processing; chunking/embedding happens
    # after the response is sent. The frontend polls GET /documents/{id}
    # to find out when it flips to ready (or failed).
    background_tasks.add_task(ingestion_service.ingest_document, str(document.id))

    return document


@router.get("/collections/{collection_id}/documents", response_model=list[DocumentResponse])
def list_documents(
    collection_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        collection_service.get_owned_collection(db, current_user.id, collection_id)
    except CollectionNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")

    return document_repository.list_by_collection(db, collection_id)


def _get_owned_document(db: Session, user_id: uuid.UUID, document_id: uuid.UUID):
    """Shared ownership check: a document is "owned" if its collection is."""
    document = document_repository.get_by_id(db, document_id)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    try:
        collection_service.get_owned_collection(db, user_id, document.collection_id)
    except CollectionNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    return document


@router.get("/documents/{document_id}", response_model=DocumentResponse)
def get_document(
    document_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The frontend polls this to check upload/ingestion status."""
    return _get_owned_document(db, current_user.id, document_id)


@router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document(
    document_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    document = _get_owned_document(db, current_user.id, document_id)
    storage_service.delete_file(document.file_path)
    document_repository.delete(db, document)  # cascades to chunks in Postgres
    # Note: this does not remove the corresponding vectors from Chroma —
    # a known gap, tracked in design-decisions.md as a Phase 2 cleanup item.
