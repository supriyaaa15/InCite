import uuid

from sqlalchemy.orm import Session

from app.models.collection import Collection
from app.repositories import collection_repository


class CollectionNotFoundError(Exception):
    """Raised for a missing collection AND for one that exists but belongs
    to someone else — same error either way, so a user can't probe for the
    existence of other users' collections by ID."""

    pass


def create_collection(db: Session, user_id: uuid.UUID, name: str) -> Collection:
    return collection_repository.create(db, user_id=user_id, name=name)


def get_owned_collection(db: Session, user_id: uuid.UUID, collection_id: uuid.UUID) -> Collection:
    """Every route that touches a specific collection should call this,
    never collection_repository.get_by_id directly — this is what enforces
    that users can only ever act on their own collections."""
    collection = collection_repository.get_by_id(db, collection_id)
    if collection is None or collection.user_id != user_id:
        raise CollectionNotFoundError()
    return collection


def list_collections(db: Session, user_id: uuid.UUID) -> list[Collection]:
    return collection_repository.list_by_user(db, user_id)


def delete_collection(db: Session, user_id: uuid.UUID, collection_id: uuid.UUID) -> None:
    collection = get_owned_collection(db, user_id, collection_id)
    collection_repository.delete(db, collection)
