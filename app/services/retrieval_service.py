"""
Retrieval, isolated from chat orchestration on purpose. When Hybrid Search
(BM25 + vector) or Reranking get built later, they change what happens
inside retrieve() — chat_service.py, the routes, and everything else stay
untouched, because they only ever call this module's public functions.

Migrated to LangChain (Day 14-15) — retrieve() now goes through
langchain_chroma.Chroma instead of the raw chromadb client. Storage itself
is unchanged: this attaches to the exact same per-collection Chroma
collections that ingestion_service.py already writes to, using the same
embedding weights (see core/embeddings.py's LangChain wrapper) — same
data, same vectors, just queried through LangChain's interface now.
"""

import re
import uuid

from langchain_chroma import Chroma
from langchain_core.documents import Document
from sqlalchemy.orm import Session

from app.core.chroma_client import get_chroma_client
from app.core.config import settings
from app.core.embeddings import get_langchain_embeddings
from app.repositories import document_repository


def _get_vectorstore(collection_id: uuid.UUID) -> Chroma:
    """
    Attaches to the existing per-collection Chroma collection (created
    during ingestion, via core/chroma_client.get_collection_store — that
    remains the only place a collection is actually created, so the
    cosine-distance metric set at creation is never at risk of being
    silently overridden here).
    """
    return Chroma(
        client=get_chroma_client(),
        collection_name=f"collection_{collection_id}",
        embedding_function=get_langchain_embeddings(),
    )


def retrieve(
    collection_id: uuid.UUID, query_text: str, top_k: int | None = None
) -> list[tuple[Document, float]]:
    """
    Returns LangChain's [(Document, distance), ...] for the top_k most
    similar chunks in this collection. Each Document has .page_content
    (the chunk text) and .metadata (document_id, page_number, chunk_index
    — set during ingestion). Lower distance = more similar, same
    convention as the raw chromadb client used before.
    """
    vectorstore = _get_vectorstore(collection_id)
    return vectorstore.similarity_search_with_score(query_text, k=top_k or settings.TOP_K)


def build_citations(db: Session, retrieved: list[tuple[Document, float]]) -> list[dict]:
    """
    Turns LangChain's retrieval result into
    [{document_id, document_name, page_number, chunk_index, chunk_text,
    score}, ...] — resolves document_id -> filename in one bulk query
    rather than one lookup per chunk. document_id/chunk_index are kept in
    this internal shape (stripped by to_public()) so chat_service can
    reconstruct a stable chunk identifier for query_logs without a second
    round-trip to Chroma.
    """
    if not retrieved:
        return []

    doc_ids = {uuid.UUID(doc.metadata["document_id"]) for doc, _ in retrieved}
    documents = document_repository.get_by_ids(db, list(doc_ids))
    filename_by_id = {str(d.id): d.filename for d in documents}

    citations = []
    for doc, distance in retrieved:
        citations.append(
            {
                "document_id": doc.metadata["document_id"],
                "document_name": filename_by_id.get(doc.metadata["document_id"], "unknown document"),
                "page_number": doc.metadata["page_number"],
                "chunk_index": doc.metadata["chunk_index"],
                "chunk_text": doc.page_content,
                "score": round(1 - distance, 4),  # cosine distance -> similarity
            }
        )
    return citations


def filter_citations(citations: list[dict], min_score: float) -> list[dict]:
    """
    Removes citations below min_score — pure noise reduction. This runs
    BEFORE generation, not just before display: a chunk too weak to show
    the user is also too weak to hand the LLM as "context" — feeding it
    in anyway is exactly what let a past query hallucinate an answer from
    the model's general knowledge instead of admitting the documents
    didn't cover it (see design-decisions.md, Day 24).
    """
    return [c for c in citations if c["score"] >= min_score]


_STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "in", "on", "at", "of",
    "to", "for", "and", "or", "but", "what", "how", "why", "does", "do",
    "can", "it", "this", "that", "with", "as", "be",
}


def _best_sentence(text: str, query: str, max_chars: int = 220) -> str:
    """
    Picks the sentence within a chunk most relevant to the query, instead
    of blindly showing the first N characters. Relevance = word overlap
    with the query (stopwords excluded) — deterministic, no extra LLM
    call or embedding lookup needed, since the words that matter in a
    short question are usually the meaningful ones already.
    Falls back to simple truncation if sentence splitting doesn't help
    (e.g. a chunk with no real sentence breaks).
    """
    text = " ".join(text.split())
    sentences = re.split(r"(?<=[.!?])\s+", text)
    sentences = [s.strip() for s in sentences if s.strip()]

    if len(sentences) <= 1:
        return _truncate(text, max_chars)

    query_words = {w for w in re.findall(r"\w+", query.lower()) if w not in _STOPWORDS}
    if not query_words:
        return _truncate(text, max_chars)

    best_sentence = None
    best_overlap = -1
    for sentence in sentences:
        sentence_words = set(re.findall(r"\w+", sentence.lower()))
        overlap = len(query_words & sentence_words)
        if overlap > best_overlap:
            best_overlap = overlap
            best_sentence = sentence

    if best_overlap <= 0:
        # nothing meaningfully matched — a first-N-chars preview is more
        # honest here than presenting an arbitrary sentence as "the" match
        return _truncate(text, max_chars)

    return _truncate(best_sentence, max_chars)


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars]
    last_space = truncated.rfind(" ")
    if last_space > 0:
        truncated = truncated[:last_space]
    return truncated + "..."


def to_public(citations: list[dict], query: str) -> list[dict]:
    """Strips full chunk_text down to the most relevant sentence. Used
    for anything that leaves the server — the API response and what gets
    stored in Message.citations. build_citations()'s full chunk_text is
    only ever used internally, to build the LLM prompt."""
    return [
        {
            "document_name": c["document_name"],
            "page_number": c["page_number"],
            "excerpt": _best_sentence(c["chunk_text"], query),
            "score": c["score"],
        }
        for c in citations
    ]
