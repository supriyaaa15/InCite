import logging
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.core.collection_artifacts import collection_artifact_lock
from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.user import User
from app.repositories import document_repository
from app.schemas.document import DocumentResponse
from app.services import collection_service, deletion_service, ingestion_service
from app.services.collection_service import CollectionNotFoundError
from app.services.storage_service import storage_service
from app.utils.pdf_validator import validate_pdf_file, validate_upload
from app.utils.upload_errors import UploadValidationError

router = APIRouter(tags=["documents"])

logger = logging.getLogger(__name__)


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

    # ------------------------------------------------------------------ #
    # Phase 1 — cheap pre-save checks (size limit + MIME/ext hints).     #
    # Runs before writing to disk so we don't waste I/O.                 #
    # ------------------------------------------------------------------ #
    try:
        validate_upload(file)
    except UploadValidationError as exc:
        logger.warning(
            "Upload rejected before save | user=%s collection=%s filename=%r "
            "error_code=%s",
            current_user.id,
            collection_id,
            file.filename,
            exc.error_code,
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.user_message,
        )

    user_id = current_user.id
    db.rollback()  # Release the authorization read before waiting for artifact I/O.
    with collection_artifact_lock(collection_id):
        document = _save_and_register_upload(db, user_id, collection_id, file)
    background_tasks.add_task(ingestion_service.ingest_document, str(document.id))
    return document


def _save_and_register_upload(db, user_id, collection_id, file):
    # The collection could have been deleted while this upload waited for its
    # artifact lock. Close the read transaction before file I/O.
    try:
        collection_service.get_owned_collection(db, user_id, collection_id)
    except CollectionNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    db.rollback()
    file_path = storage_service.save_file(str(user_id), str(collection_id), file)
    try:
        validate_pdf_file(file_path)
    except UploadValidationError as exc:
        # Delete the file we just saved — we don't want orphaned bytes on disk.
        storage_service.delete_file(file_path)
        logger.warning(
            "Upload rejected after save | user=%s collection=%s filename=%r "
            "error_code=%s",
            user_id,
            collection_id,
            file.filename,
            exc.error_code,
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.user_message,
        )

    # The file was validated outside the SQL lock. Recheck admission before the
    # document insert so it cannot commit after the parent is marked deleted.
    try:
        collection_service.lock_owned_active_collection(db, user_id, collection_id)
    except CollectionNotFoundError:
        db.rollback()
        try:
            storage_service.delete_file(file_path)
        except OSError:
            logger.exception("Could not remove unregistered upload | collection=%s", collection_id)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")

    return document_repository.create(
        db, collection_id=collection_id, filename=file.filename, file_path=file_path
    )


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


@router.delete("/documents/{document_id}", status_code=status.HTTP_202_ACCEPTED)
def delete_document(
    document_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        deletion_service.request_deletion(db, current_user.id, document_id=document_id)
    except deletion_service.DeletionTargetNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    return {"status": "cleanup_pending"}

