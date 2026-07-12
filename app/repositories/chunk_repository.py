import uuid

from sqlalchemy.orm import Session

from app.models.chunk import Chunk


def bulk_create(db: Session, chunks: list[dict]) -> None:
    """
    chunks: [{document_id, page_number, chunk_index, content, chroma_id}, ...]
    One bulk insert instead of one commit per chunk — a document can easily
    produce hundreds of chunks, and committing individually would be slow
    and put unnecessary load on Postgres.
    """
    db.bulk_insert_mappings(Chunk, chunks)
    db.commit()


def delete_by_document(db: Session, document_id: uuid.UUID) -> None:
    db.query(Chunk).filter(Chunk.document_id == document_id).delete()
    db.commit()
