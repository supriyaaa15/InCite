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

Phase 4 — Hybrid Retrieval (BM25 + Vector + RRF)
--------------------------------------------------
retrieve() now supports two modes controlled by settings.RETRIEVAL_MODE:

  "vector"  — existing behaviour, unchanged.
  "hybrid"  — vector search (top_k * 4 candidates) + BM25 search over the
              full Chroma corpus, fused with Reciprocal Rank Fusion.

The return type of retrieve() is always list[tuple[Document, float]] so
build_citations(), filter_citations(), and to_public() are completely
unaffected by the mode change.

build_citations() is updated to prefer document_name from Chroma metadata
(set during ingestion since Phase 4) and fall back to a Postgres lookup
only for older chunks that lack the field.
"""

import re
import time
import uuid
from typing import Optional

from langchain_chroma import Chroma
from langchain_core.documents import Document
from sqlalchemy.orm import Session

from app.core.chroma_client import get_chroma_client, get_collection_store
from app.core.config import settings
from app.core.embeddings import get_langchain_embeddings
from app.repositories import document_repository


# ── Chroma vector store accessor ─────────────────────────────────────────────

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


# ── Vector retrieval ──────────────────────────────────────────────────────────

def _vector_retrieve(
    collection_id: uuid.UUID,
    query_text: str,
    top_k: int,
) -> tuple[list[tuple[Document, float]], dict[str, tuple[Document, float]]]:
    """
    Run Chroma vector similarity search.

    Returns
    -------
    (results, lookup_dict)

    results:
        list[tuple[Document, distance]] — same type as the old retrieve().
    lookup_dict:
        {chroma_id: (Document, distance)} — used by _rrf_fuse() to avoid a
        second Chroma call. The chroma_id is reconstructed from the document
        metadata (document_id, page_number, chunk_index), which is the exact
        format written during ingestion.
    """
    vectorstore = _get_vectorstore(collection_id)
    results = vectorstore.similarity_search_with_score(query_text, k=top_k)

    lookup: dict[str, tuple[Document, float]] = {}
    for doc, dist in results:
        m = doc.metadata
        chroma_id = f"{m['document_id']}_p{m['page_number']}_c{m['chunk_index']}"
        lookup[chroma_id] = (doc, dist)

    return results, lookup


# ── BM25 retrieval ────────────────────────────────────────────────────────────

def _bm25_retrieve(
    collection_id: uuid.UUID,
    query_text: str,
    top_n: int,
    db: Optional[Session],
) -> list[tuple[str, float]]:
    """
    Run BM25 search over the full Chroma corpus for this collection.

    Returns
    -------
    list[tuple[chroma_id, bm25_score]] sorted best-first.
    Returns [] on any error — the caller (retrieve()) treats an empty BM25
    result as a graceful fallback to vector-only mode.
    """
    from app.core import bm25_store

    try:
        raw_collection = get_collection_store(str(collection_id))
        index = bm25_store.get_index(collection_id, raw_collection, db)
        return bm25_store.search(index, query_text, top_n)
    except Exception as exc:
        raise   # re-raise so retrieve() can log it and fall back


# ── Reciprocal Rank Fusion ────────────────────────────────────────────────────

def _rrf_fuse(
    vector_lookup: dict[str, tuple[Document, float]],   # {chroma_id: (Doc, dist)}
    bm25_results: list[tuple[str, float]],              # [(chroma_id, bm25_score)]
    k: int,
    top_k: int,
) -> list[tuple[str, float, int | None, int | None]]:
    """
    Reciprocal Rank Fusion over the vector candidate pool.

    Only chunks already in vector_lookup participate. BM25-only results
    that are absent from the vector pool are skipped — with a pool of
    top_k * 4 this is uncommon and the simplicity benefit (no second
    Chroma call) outweighs the marginal quality loss.

    Returns
    -------
    list of (chroma_id, rrf_score, vector_rank, bm25_rank) sorted
    best-first, length ≤ top_k.
    """
    # Build rank maps (1-based)
    vector_rank_of: dict[str, int] = {
        cid: i + 1 for i, cid in enumerate(vector_lookup.keys())
    }
    bm25_rank_of: dict[str, int] = {
        cid: i + 1 for i, (cid, _) in enumerate(bm25_results)
    }

    scores: dict[str, float] = {}
    for cid in vector_lookup:
        score = 0.0
        if cid in vector_rank_of:
            score += 1.0 / (k + vector_rank_of[cid])
        if cid in bm25_rank_of:
            score += 1.0 / (k + bm25_rank_of[cid])
        scores[cid] = score

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:top_k]

    return [
        (
            cid,
            rrf_score,
            vector_rank_of.get(cid),
            bm25_rank_of.get(cid),
        )
        for cid, rrf_score in ranked
    ]


# ── Public API ────────────────────────────────────────────────────────────────

def retrieve(
    collection_id: uuid.UUID,
    query_text: str,
    db: Optional[Session] = None,
    top_k: int | None = None,
) -> list[tuple[Document, float]]:
    """
    Returns list[tuple[Document, score]] for the top_k most relevant chunks.

    In "vector" mode: score = cosine similarity (1 - distance).
    In "hybrid" mode: score = RRF score.

    The return type is identical in both modes so build_citations() is
    completely unaffected by the choice of retrieval mode.

    Parameters
    ----------
    db:
        Required for "hybrid" mode (BM25 index may need a Postgres fallback
        for legacy chunks). Unused (but accepted) in "vector" mode.
    """
    k = top_k or settings.TOP_K
    mode = settings.RETRIEVAL_MODE.lower()

    t_start = time.perf_counter()

    if mode != "hybrid":
        # ── Vector-only ───────────────────────────────────────────────────
        t_vec = time.perf_counter()
        results, _ = _vector_retrieve(collection_id, query_text, k)
        vec_ms = int((time.perf_counter() - t_vec) * 1000)
        total_ms = int((time.perf_counter() - t_start) * 1000)

        print(
            f"\n{'=' * 30}\n"
            f"Requested Mode : vector\n"
            f"Actual Mode    : vector\n\n"
            f"Vector Search  : {vec_ms:>4} ms   (candidates: {len(results)})\n"
            f"Total Retrieval: {total_ms:>4} ms   (returned: {min(k, len(results))})\n"
            f"{'=' * 30}\n"
        )
        return results

    # ── Hybrid mode ───────────────────────────────────────────────────────
    candidate_k = k * 4   # broad vector pool so BM25 can meaningfully rerank

    t_vec = time.perf_counter()
    vector_results, vector_lookup = _vector_retrieve(collection_id, query_text, candidate_k)
    vec_ms = int((time.perf_counter() - t_vec) * 1000)

    actual_mode = "hybrid"
    fallback_reason: str | None = None

    t_bm25 = time.perf_counter()
    try:
        bm25_results = _bm25_retrieve(collection_id, query_text, candidate_k, db)
    except Exception as exc:
        bm25_results = []
        actual_mode = "vector"
        fallback_reason = str(exc)
    bm25_ms = int((time.perf_counter() - t_bm25) * 1000)

    if actual_mode == "hybrid":
        t_rrf = time.perf_counter()
        fused = _rrf_fuse(vector_lookup, bm25_results, settings.BM25_RRF_K, k)
        rrf_ms = int((time.perf_counter() - t_rrf) * 1000)

        # Reconstruct the output list from the vector dict — no second Chroma call.
        final: list[tuple[Document, float]] = []
        for chroma_id, rrf_score, v_rank, b_rank in fused:
            doc, _dist = vector_lookup[chroma_id]
            # Attach debug metadata (stripped by to_public() before leaving the server)
            doc.metadata["_vector_rank"] = v_rank
            doc.metadata["_bm25_rank"] = b_rank
            doc.metadata["_rrf_score"] = round(rrf_score, 6)
            doc.metadata["_retrieval_mode"] = "hybrid"
            final.append((doc, rrf_score))

        total_ms = int((time.perf_counter() - t_start) * 1000)
        print(
            f"\n{'=' * 30}\n"
            f"Requested Mode : hybrid\n"
            f"Actual Mode    : hybrid\n\n"
            f"Vector Search  : {vec_ms:>4} ms   (candidates: {len(vector_results)})\n"
            f"BM25 Search    : {bm25_ms:>4} ms   (candidates: {len(bm25_results)})\n"
            f"RRF Fusion     : {rrf_ms:>4} ms\n"
            f"Total Retrieval: {total_ms:>4} ms   (returned: {len(final)})\n"
            f"{'=' * 30}\n"
        )
        return final

    else:
        # BM25 failed — fall back to trimmed vector results
        trimmed = vector_results[:k]
        total_ms = int((time.perf_counter() - t_start) * 1000)
        print(
            f"\n{'=' * 30}\n"
            f"Requested Mode : hybrid\n"
            f"Actual Mode    : vector   [BM25 unavailable: {fallback_reason}]\n\n"
            f"Vector Search  : {vec_ms:>4} ms   (candidates: {len(vector_results)})\n"
            f"Total Retrieval: {total_ms:>4} ms   (returned: {len(trimmed)})\n"
            f"{'=' * 30}\n"
        )
        return trimmed


def build_citations(db: Session, retrieved: list[tuple[Document, float]]) -> list[dict]:
    """
    Turns retrieve()'s output into the internal citation shape:
    [{document_id, document_name, page_number, chunk_index, chunk_text, score}, ...]

    Phase 4 update: prefers document_name from Chroma metadata (stored
    there since Phase 4 ingestion). Only performs the Postgres bulk lookup
    for chunks that are missing the field — i.e. documents ingested before
    Phase 4. Over time, as collections are re-uploaded, the Postgres path
    becomes unused.

    document_id/chunk_index are kept in this internal shape (stripped by
    to_public()) so chat_service can reconstruct a stable chunk identifier
    for query_logs without a second round-trip.
    """
    if not retrieved:
        return []

    # Separate chunks that already carry document_name from those that don't.
    need_pg: list[str] = []   # document_ids that need Postgres resolution
    for doc, _ in retrieved:
        if not doc.metadata.get("document_name"):
            need_pg.append(doc.metadata["document_id"])

    filename_by_id: dict[str, str] = {}
    if need_pg:
        doc_uuids = [uuid.UUID(did) for did in set(need_pg)]
        docs = document_repository.get_by_ids(db, doc_uuids)
        filename_by_id = {str(d.id): d.filename for d in docs}

    citations = []
    for doc, score_or_dist in retrieved:
        m = doc.metadata
        # In vector mode, retrieve() returns cosine distance → convert to similarity.
        # In hybrid mode, it returns the raw RRF score. RRF scores are in [0, 2/(k+1)]
        # with k = BM25_RRF_K (default 60 → max ≈ 0.0328). They are NOT in [0, 1], so
        # they would all fall below MIN_CITATION_SCORE (0.05) and be filtered out by
        # filter_citations(), causing every hybrid query to return "not found".
        # Normalize by the theoretical max (2 result lists × 1/(k+1)) to map to [0, 1].
        if m.get("_retrieval_mode") == "hybrid":
            max_rrf = 2.0 / (settings.BM25_RRF_K + 1)
            score = round(min(float(score_or_dist) / max_rrf, 1.0), 4)
        else:
            score = round(1 - float(score_or_dist), 4)   # cosine distance → similarity


        citations.append(
            {
                "document_id":   m["document_id"],
                "document_name": (
                    m.get("document_name")
                    or filename_by_id.get(m["document_id"], "unknown document")
                ),
                "page_number":   m["page_number"],
                "chunk_index":   m["chunk_index"],
                "chunk_text":    doc.page_content,
                "score":         score,
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
            "page_number":   c["page_number"],
            "excerpt":       _best_sentence(c["chunk_text"], query),
            "score":         c["score"],
        }
        for c in citations
    ]
