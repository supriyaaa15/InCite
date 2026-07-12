import uuid

from sqlalchemy.orm import Session

from app.models.query_log import QueryLog


def create(
    db: Session,
    message_id: uuid.UUID,
    retrieved_chunk_ids: list[str],
    similarity_scores: list[float],
    top_k: int,
    response_time_ms: int,
    llm_model: str,
) -> QueryLog:
    log = QueryLog(
        message_id=message_id,
        retrieved_chunk_ids=retrieved_chunk_ids,
        similarity_scores=similarity_scores,
        top_k=top_k,
        response_time_ms=response_time_ms,
        llm_model=llm_model,
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    return log
