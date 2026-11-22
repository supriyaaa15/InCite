import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.collection import Collection


def create(db: Session, user_id: uuid.UUID, name: str) -> Collection:
    collection = Collection(user_id=user_id, name=name)
    db.add(collection)
    db.commit()
    db.refresh(collection)
    return collection


def get_by_id(db: Session, collection_id: uuid.UUID) -> Collection | None:
    return db.scalar(select(Collection).where(
        Collection.id == collection_id, Collection.delete_requested_at.is_(None)
    ))


def lock_by_id(
    db: Session, collection_id: uuid.UUID, *, user_id: uuid.UUID | None = None
) -> Collection | None:
    statement = select(Collection).where(Collection.id == collection_id)
    if user_id is not None:
        statement = statement.where(Collection.user_id == user_id)
    return db.scalar(statement.with_for_update().execution_options(populate_existing=True))


def list_by_user(db: Session, user_id: uuid.UUID) -> list[Collection]:
    return list(db.scalars(select(Collection).where(
        Collection.user_id == user_id, Collection.delete_requested_at.is_(None)
    )))
