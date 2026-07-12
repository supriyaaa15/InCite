import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.user import User
from app.schemas.chat import ChatRequest, ChatResponse, ChatSessionResponse, MessageResponse
from app.services import chat_service
from app.services.chat_service import ChatSessionNotFoundError, SessionCollectionMismatchError
from app.services.collection_service import CollectionNotFoundError

router = APIRouter(tags=["chat"])


@router.post("/collections/{collection_id}/chat", response_model=ChatResponse)
def send_message(
    collection_id: uuid.UUID,
    payload: ChatRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        session, answer, reasoning, citations = chat_service.send_message(
            db,
            user_id=current_user.id,
            collection_id=collection_id,
            message_text=payload.message,
            session_id=payload.session_id,
        )
    except CollectionNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    except ChatSessionNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat session not found")
    except SessionCollectionMismatchError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="session_id belongs to a different collection",
        )

    return ChatResponse(session_id=session.id, message=answer, reasoning=reasoning, citations=citations)


@router.get("/sessions", response_model=list[ChatSessionResponse])
def list_sessions(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return chat_service.list_sessions(db, current_user.id)


@router.get("/sessions/{session_id}/messages", response_model=list[MessageResponse])
def get_session_messages(
    session_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        return chat_service.list_messages(db, current_user.id, session_id)
    except ChatSessionNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat session not found")


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_session(
    session_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        chat_service.delete_session(db, current_user.id, session_id)
    except ChatSessionNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat session not found")
