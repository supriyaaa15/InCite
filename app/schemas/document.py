import uuid
from datetime import datetime

from pydantic import BaseModel

from app.models.document import DocumentStatus


class DocumentResponse(BaseModel):
    id: uuid.UUID
    filename: str
    status: DocumentStatus
    page_count: int | None
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime

    class Config:
        from_attributes = True
