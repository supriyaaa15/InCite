"""
Retrieval, isolated from chat orchestration on purpose. When Hybrid Search
(BM25 + vector) or Reranking get built later, they change what happens
inside retrieve() — chat_service.py, the routes, and everything else stay
untouched, because they only ever call this module's public functions.
"""

import uuid

from sqlalchemy.orm import Session

from app.core.chroma_client import get_collection_store
from app.core.config import settings
from app.core.embeddings import get_embedder
from app.repositories import document_repository


def retrieve(collection_id: uuid.UUID, query_text: str, top_k: int | None = None) -> dict:
    """
    Returns Chroma's raw query result for the top_k most similar chunks
    in this collection. top_k defaults to settings.TOP_K but can be
    overridden per-call (useful later for e.g. a comparison feature that
    wants a wider net than normal chat).
    """
    embedder = get_embedder()
    query_embedding = embedder.encode([query_text]).tolist()
    store = get_collection_store(str(collection_id))
    return store.query(query_embeddings=query_embedding, n_results=top_k or settings.TOP_K)


def build_citations(db: Session, retrieved: dict) -> list[dict]:
    """
    Turns a raw Chroma result into [{document_name, page_number,
    chunk_text, score}, ...] — resolves document_id -> filename in one
    bulk query rather than one lookup per chunk.
    """
    if not retrieved["ids"][0]:
        return []

    doc_ids = {uuid.UUID(m["document_id"]) for m in retrieved["metadatas"][0]}
    documents = document_repository.get_by_ids(db, list(doc_ids))
    filename_by_id = {str(d.id): d.filename for d in documents}

    citations = []
    for doc, meta, distance in zip(
        retrieved["documents"][0], retrieved["metadatas"][0], retrieved["distances"][0]
    ):
        citations.append(
            {
                "document_name": filename_by_id.get(meta["document_id"], "unknown document"),
                "page_number": meta["page_number"],
                "chunk_text": doc,
                "score": round(1 - distance, 4),  # cosine distance -> similarity
            }
        )
    return citations


def _excerpt(text: str, max_chars: int = 180) -> str:
    """Short, clean preview for anything leaving the server. Full chunk
    content stays recoverable from Postgres (chunks table, via
    document_id/page_number/chunk_index) if ever needed for debugging —
    there's no need to ship the whole chunk in every API response."""
    text = " ".join(text.split())  # normalize whitespace
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars]
    last_space = truncated.rfind(" ")
    if last_space > 0:
        truncated = truncated[:last_space]
    return truncated + "..."


def to_public(citations: list[dict]) -> list[dict]:
    """Strips full chunk_text down to a short excerpt. Used for anything
    that leaves the server — the API response and what gets stored in
    Message.citations. build_citations()'s full chunk_text is only ever
    used internally, to build the LLM prompt."""
    return [
        {
            "document_name": c["document_name"],
            "page_number": c["page_number"],
            "excerpt": _excerpt(c["chunk_text"]),
            "score": c["score"],
        }
        for c in citations
    ]