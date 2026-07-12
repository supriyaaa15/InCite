import uuid

from sqlalchemy.orm import Session

from app.models.collection import Collection


def create(db: Session, user_id: uuid.UUID, name: str) -> Collection:
    collection = Collection(user_id=user_id, name=name)
    db.add(collection)
    db.commit()
    db.refresh(collection)
    return collection


def get_by_id(db: Session, collection_id: uuid.UUID) -> Collection | None:
    return db.query(Collection).filter(Collection.id == collection_id).first()


def list_by_user(db: Session, user_id: uuid.UUID) -> list[Collection]:
    return db.query(Collection).filter(Collection.user_id == user_id).all()


def delete(db: Session, collection: Collection) -> None:
    db.delete(collection)  # cascades to documents, chunks, chat_sessions, messages
    db.commit()
