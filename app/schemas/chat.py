import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.chat import MessageRole


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    session_id: uuid.UUID | None = None  # omit to start a new session


class Citation(BaseModel):
    document_name: str
    page_number: int
    excerpt: str
    score: float


class ChatResponse(BaseModel):
    session_id: uuid.UUID
    message: str
    reasoning: str
    citations: list[Citation]


class ChatSessionResponse(BaseModel):
    id: uuid.UUID
    collection_id: uuid.UUID
    title: str | None
    created_at: datetime

    class Config:
        from_attributes = True


class MessageResponse(BaseModel):
    id: uuid.UUID
    role: MessageRole
    content: str
    citations: list[Citation] | None
    created_at: datetime

    class Config:
        from_attributes = True
