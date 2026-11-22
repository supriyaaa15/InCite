import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import TimestampedUUIDBase


class Collection(TimestampedUUIDBase):
    __tablename__ = "collections"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    delete_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cleanup_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cleanup_attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    cleanup_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cleanup_error: Mapped[str | None] = mapped_column(String(120))

    user: Mapped["User"] = relationship(back_populates="collections")
    documents: Mapped[list["Document"]] = relationship(
        back_populates="collection", cascade="all, delete-orphan"
    )
    chat_sessions: Mapped[list["ChatSession"]] = relationship(
        back_populates="collection", cascade="all, delete-orphan"
    )
