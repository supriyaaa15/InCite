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


class RecommendedDocument(BaseModel):
    document_name: str
    rank: int                # 1 = best match for the user's goal
    suitability_label: str   # dynamic, inferred from the user's request by the LLM
    reason: str              # 1-2 sentences grounded in the document's content


class ComparisonSection(BaseModel):
    title: str    # e.g. "Summary", "Common Concepts", "Key Differences"
    content: str  # prose paragraph grounded in retrieved chunks


class ChatResponse(BaseModel):
    session_id: uuid.UUID
    message: str
    reasoning: str
    citations: list[Citation]
    # None for RECOMMEND and COMPARE responses — those don't use retrieval
    # similarity scoring, so the existing "none"|"low"|"medium"|"high"|"error"
    # vocabulary doesn't apply.  None is falsy in JS so the frontend badge
    # renders correctly without any code change.
    confidence: str | None = None
    follow_up_questions: list[str] = []
    # Populated only for RECOMMEND responses
    recommended_documents: list[RecommendedDocument] = []
    # Populated only for COMPARE responses
    comparison_sections: list[ComparisonSection] = []


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
    recommended_documents: list[RecommendedDocument] | None = None
    comparison_sections: list[ComparisonSection] | None = None
    created_at: datetime

    class Config:
        from_attributes = True
