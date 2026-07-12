import uuid

from sqlalchemy.orm import Session as DBSession

from app.models.chat import ChatSession, Message, MessageRole


def create_session(
    db: DBSession, user_id: uuid.UUID, collection_id: uuid.UUID, title: str | None
) -> ChatSession:
    session = ChatSession(user_id=user_id, collection_id=collection_id, title=title)
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def get_session_by_id(db: DBSession, session_id: uuid.UUID) -> ChatSession | None:
    return db.query(ChatSession).filter(ChatSession.id == session_id).first()


def list_sessions_by_user(db: DBSession, user_id: uuid.UUID) -> list[ChatSession]:
    return (
        db.query(ChatSession)
        .filter(ChatSession.user_id == user_id)
        .order_by(ChatSession.created_at.desc())
        .all()
    )


def create_message(
    db: DBSession,
    session_id: uuid.UUID,
    role: MessageRole,
    content: str,
    citations: list[dict] | None = None,
) -> Message:
    message = Message(session_id=session_id, role=role, content=content, citations=citations)
    db.add(message)
    db.commit()
    db.refresh(message)
    return message


def list_messages_by_session(db: DBSession, session_id: uuid.UUID) -> list[Message]:
    return (
        db.query(Message)
        .filter(Message.session_id == session_id)
        .order_by(Message.created_at.asc())
        .all()
    )


def delete_session(db: DBSession, session: ChatSession) -> None:
    db.delete(session)  # cascades to messages
    db.commit()
