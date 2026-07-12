import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.user import User
from app.schemas.collection import CollectionCreateRequest, CollectionResponse
from app.services import collection_service
from app.services.collection_service import CollectionNotFoundError

router = APIRouter(prefix="/collections", tags=["collections"])


@router.post("", response_model=CollectionResponse, status_code=status.HTTP_201_CREATED)
def create_collection(
    payload: CollectionCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return collection_service.create_collection(db, user_id=current_user.id, name=payload.name)


@router.get("", response_model=list[CollectionResponse])
def list_collections(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return collection_service.list_collections(db, user_id=current_user.id)


@router.get("/{collection_id}", response_model=CollectionResponse)
def get_collection(
    collection_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        return collection_service.get_owned_collection(db, current_user.id, collection_id)
    except CollectionNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")


@router.delete("/{collection_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_collection(
    collection_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        collection_service.delete_collection(db, current_user.id, collection_id)
    except CollectionNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
