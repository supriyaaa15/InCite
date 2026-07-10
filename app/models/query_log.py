import uuid

from sqlalchemy import ARRAY, Float, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import TimestampedUUIDBase


class QueryLog(TimestampedUUIDBase):
    __tablename__ = "query_logs"

    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    retrieved_chunk_ids: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False)
    similarity_scores: Mapped[list[float]] = mapped_column(ARRAY(Float), nullable=False)
    top_k: Mapped[int] = mapped_column(Integer, nullable=False)
    response_time_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    llm_model: Mapped[str] = mapped_column(String, nullable=False)

    message: Mapped["Message"] = relationship(back_populates="query_log")
