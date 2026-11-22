"""Collection-scoped vector, hybrid, and reranked retrieval with SQL eligibility."""

from functools import lru_cache
import logging
import re
import time
import uuid
from typing import Optional

from chromadb.errors import InvalidCollectionException
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.chroma_client import get_existing_collection_store
from app.core.config import settings
from app.core.embeddings import embed_query
from app.core.retrieved_chunk import RetrievedChunk
from app.repositories import document_repository


logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _get_reranker(model_name: str):
    """
    Lazy singleton for sentence_transformers.CrossEncoder.
    Loaded once on first call, reused for subsequent reranking requests.
    """
    from sentence_transformers import CrossEncoder
    return CrossEncoder(model_name)


# ── Vector retrieval ──────────────────────────────────────────────────────────

def _document_uuid(value: object) -> uuid.UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _chunk_id(doc: RetrievedChunk) -> str | None:
    metadata = doc.metadata
    document_id = metadata.get("document_id")
    page = metadata.get("page_number")
    index = metadata.get("chunk_index")
    if _document_uuid(document_id) is None or not isinstance(page, int) or not isinstance(index, int):
        return None
    return f"{document_id}_p{page}_c{index}"


def _use_ready_document(doc: RetrievedChunk, ready_names: dict[uuid.UUID, str]) -> bool:
    if _chunk_id(doc) is None:
        return False
    document_id = _document_uuid(doc.metadata["document_id"])
    if document_id is None or document_id not in ready_names:
        return False
    doc.metadata["document_name"] = ready_names[document_id]
    return True


def _vector_retrieve(
    collection_id: uuid.UUID,
    query_text: str,
    top_k: int,
) -> tuple[list[tuple[RetrievedChunk, float]], dict[str, tuple[RetrievedChunk, float]]]:
    """
    Run Chroma vector similarity search.

    Returns
    -------
    (results, lookup_dict)

    results:
        Ordered pairs of retrieved chunks and raw distances.
    lookup_dict:
        {chroma_id: (RetrievedChunk, distance)} — used by _rrf_fuse() to avoid a
        second Chroma call. The chroma_id is reconstructed from the document
        metadata (document_id, page_number, chunk_index), which is the exact
        format written during ingestion.
    """
    try:
        collection = get_existing_collection_store(str(collection_id))
        if collection is None:
            return [], {}
        embedding = embed_query(query_text)
        response = collection.query(
            query_embeddings=[embedding], n_results=top_k,
            include=["documents", "metadatas", "distances"],
        )
    except InvalidCollectionException:
        return [], {}

    # Chroma groups rows by query. Validate the text in the first result batch.
    results = [
        (RetrievedChunk(page_content=text, metadata=metadata or {}), distance)
        for text, metadata, distance in zip(
            response["documents"][0], response["metadatas"][0], response["distances"][0],
        )
    ]
    lookup: dict[str, tuple[RetrievedChunk, float]] = {}
    for doc, dist in results:
        chroma_id = _chunk_id(doc)
        if chroma_id is not None:
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
        raw_collection = get_existing_collection_store(str(collection_id))
        if raw_collection is None:
            return []
        index = bm25_store.get_index(collection_id, raw_collection, db)
        return bm25_store.search(index, query_text, top_n)
    except Exception as exc:
        raise   # re-raise so retrieve() can log it and fall back


# ── Reciprocal Rank Fusion ────────────────────────────────────────────────────

def _rrf_fuse(
    vector_lookup: dict[str, tuple[RetrievedChunk, float]],
    bm25_results: list[tuple[str, float]],              # [(chroma_id, bm25_score)]
    k: int,
    top_k: int,
) -> list[tuple[str, float, int | None, int | None]]:
    """
    Reciprocal Rank Fusion over the UNION of Vector and BM25 candidates.

    Phase 3 (independent candidate-pool architecture):
    Both retrieval systems run independently. RRF must score every candidate
    that appears in EITHER list, not only those in the Vector pool.
    A BM25-only chunk receives its BM25 rank contribution; a Vector-only
    chunk receives its Vector rank contribution; a chunk in both lists
    accumulates both contributions and naturally ranks higher.

    Scoring formula (standard RRF):
        score(cid) = Σ  1 / (k + rank_in_list)
    where the sum runs over every list in which cid appears.

    Parameters
    ----------
    vector_lookup : dict mapping chroma_id → (RetrievedChunk, distance)
        Ordered by Vector search rank (dict insertion order preserved, Python 3.7+).
    bm25_results : list of (chroma_id, bm25_score) sorted best-first.
    k : RRF smoothing constant (60 is the TREC standard default).
    top_k : maximum number of results to return.

    Returns
    -------
    list of (chroma_id, rrf_score, vector_rank | None, bm25_rank | None)
    sorted best-first, length ≤ top_k.
    vector_rank / bm25_rank are None when the chunk was absent from that list.
    """
    # Build rank maps (1-based)
    vector_rank_of: dict[str, int] = {
        cid: i + 1 for i, cid in enumerate(vector_lookup.keys())
    }
    bm25_rank_of: dict[str, int] = {
        cid: i + 1 for i, (cid, _) in enumerate(bm25_results)
    }

    # Score every candidate in the UNION of both lists.
    # Using a set prevents double-counting candidates appearing in both.
    all_candidate_ids: set[str] = set(vector_rank_of.keys()) | set(bm25_rank_of.keys())

    scores: dict[str, float] = {}
    for cid in all_candidate_ids:
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
            vector_rank_of.get(cid),    # None when BM25-only
            bm25_rank_of.get(cid),      # None when Vector-only
        )
        for cid, rrf_score in ranked
    ]


# ── Public API ────────────────────────────────────────────────────────────────

def retrieve(
    collection_id: uuid.UUID,
    query_text: str,
    db: Session | None,
    top_k: int | None = None,
) -> list[tuple[RetrievedChunk, float]]:
    """Return ranked chunks of ready documents in an authorized collection.

    Request callers must verify collection ownership before retrieval.
    Scores retain their mode-specific meaning: vector distance, RRF score,
    or reranker output.
    """
    if db is None:
        return []
    k = top_k or settings.TOP_K
    mode = settings.RETRIEVAL_MODE.lower()
    t_start = time.perf_counter()

    # Bounded overfetch lets ready chunks below stale hits fill the result.
    candidate_k = k * 4
    t_vec = time.perf_counter()
    vector_results, vector_lookup = _vector_retrieve(collection_id, query_text, candidate_k)
    vec_ms = int((time.perf_counter() - t_vec) * 1000)

    actual_mode = mode
    fallback_reason: str | None = None
    bm25_results: list[tuple[str, float]] = []
    t_bm25 = time.perf_counter()
    if mode in ("hybrid", "hybrid_rerank"):
        try:
            # Names come from the eligibility query, including for legacy chunks.
            bm25_results = _bm25_retrieve(collection_id, query_text, candidate_k, None)
        except Exception as exc:
            actual_mode = "vector"
            fallback_reason = str(exc)
    bm25_ms = int((time.perf_counter() - t_bm25) * 1000)

    candidate_chunk_ids = set(vector_lookup) | {chunk_id for chunk_id, _ in bm25_results}
    candidate_ids = {
        document_id
        for chunk_id in candidate_chunk_ids
        if (document_id := _document_uuid(chunk_id.partition("_p")[0])) is not None
    }
    if not candidate_ids:
        return []
    try:
        ready_names = document_repository.get_ready_document_names(db, collection_id, candidate_ids)
    except SQLAlchemyError:
        logger.warning("Document eligibility query failed for collection %s", collection_id)
        db.rollback()
        return []
    if not ready_names:
        return []

    if mode not in ("hybrid", "hybrid_rerank"):
        results = [(doc, score) for doc, score in vector_results if _use_ready_document(doc, ready_names)][:k]
        total_ms = int((time.perf_counter() - t_start) * 1000)

        print(
            f"\n{'=' * 30}\n"
            f"Requested Mode : {mode}\n"
            f"Actual Mode    : vector\n\n"
            f"Vector Search  : {vec_ms:>4} ms   (candidates: {len(vector_results)})\n"
            f"Total Retrieval: {total_ms:>4} ms   (returned: {len(results)})\n"
            f"{'=' * 30}\n"
        )
        return results

    if actual_mode in ("hybrid", "hybrid_rerank"):
        t_rrf = time.perf_counter()
        rrf_top_k = settings.RERANK_POOL_K if mode == "hybrid_rerank" else k
        # Keep the original rank contributions and filter before applying the cutoff.
        fused = _rrf_fuse(vector_lookup, bm25_results, settings.BM25_RRF_K, len(candidate_chunk_ids))
        fused = [hit for hit in fused if _document_uuid(hit[0].partition("_p")[0]) in ready_names]
        rrf_ms = int((time.perf_counter() - t_rrf) * 1000)

        # ── Fetch Documents for BM25-only survivors ───────────────────────
        bm25_only_ids: list[str] = [
            chroma_id
            for chroma_id, _, _, _ in fused
            if chroma_id not in vector_lookup
        ]

        bm25_only_lookup: dict[str, tuple[RetrievedChunk, float]] = {}
        if bm25_only_ids:
            try:
                raw_collection = get_existing_collection_store(str(collection_id))
                fetched = raw_collection.get(
                    ids=bm25_only_ids,
                    include=["documents", "metadatas"],
                ) if raw_collection is not None else {"ids": []}
                for i, cid in enumerate(fetched["ids"]):
                    text = (fetched["documents"] or [])[i] if i < len(fetched["documents"] or []) else ""
                    meta = (fetched["metadatas"] or [])[i] if i < len(fetched["metadatas"] or []) else {}
                    bm25_only_lookup[cid] = (
                        RetrievedChunk(page_content=text, metadata=meta),
                        0.0,   # no cosine distance — this chunk came from BM25 only
                    )
            except Exception as exc:
                logger.warning(
                    "Hybrid: Chroma fetch for BM25-only IDs failed — "
                    "those candidates will be excluded from the result. Error: %s", exc
                )

        # ── Construct candidate list ──────────────────────────────────────
        candidates: list[dict] = []
        for chroma_id, rrf_score, v_rank, b_rank in fused:
            if chroma_id in vector_lookup:
                doc, _dist = vector_lookup[chroma_id]
            elif chroma_id in bm25_only_lookup:
                doc, _dist = bm25_only_lookup[chroma_id]
            else:
                continue
            if _chunk_id(doc) != chroma_id or not _use_ready_document(doc, ready_names):
                continue

            candidates.append({
                "doc": doc,
                "chroma_id": chroma_id,
                "rrf_score": rrf_score,
                "v_rank": v_rank,
                "b_rank": b_rank,
            })
        candidates = candidates[:rrf_top_k]

        if mode == "hybrid":
            final: list[tuple[RetrievedChunk, float]] = []
            for c in candidates[:k]:
                doc = c["doc"]
                doc.metadata["_vector_rank"] = c["v_rank"]
                doc.metadata["_bm25_rank"] = c["b_rank"]
                doc.metadata["_rrf_score"] = round(c["rrf_score"], 6)
                doc.metadata["_retrieval_mode"] = "hybrid"
                final.append((doc, c["rrf_score"]))

            total_ms = int((time.perf_counter() - t_start) * 1000)
            bm25_only_count = sum(c["v_rank"] is None for c in candidates)
            print(
                f"\n{'=' * 30}\n"
                f"Requested Mode : hybrid\n"
                f"Actual Mode    : hybrid\n\n"
                f"Vector Search  : {vec_ms:>4} ms   (candidates: {len(vector_results)})\n"
                f"BM25 Search    : {bm25_ms:>4} ms   (candidates: {len(bm25_results)})\n"
                f"RRF Fusion     : {rrf_ms:>4} ms   (union size: {len(candidate_chunk_ids)}, bm25-only in top-k: {bm25_only_count})\n"
                f"Total Retrieval: {total_ms:>4} ms   (returned: {len(final)})\n"
                f"{'=' * 30}\n"
            )
            return final

        else:
            # ── Mode: hybrid_rerank ───────────────────────────────────────
            t_rerank = time.perf_counter()
            rerank_failed = False
            try:
                pairs = [(query_text, c["doc"].page_content) for c in candidates]
                if pairs:
                    reranker = _get_reranker(settings.RERANKER_MODEL)
                    raw_scores = reranker.predict(pairs)
                    for c, s in zip(candidates, raw_scores):
                        c["rerank_score"] = float(s)
                    candidates.sort(key=lambda x: x["rerank_score"], reverse=True)
                else:
                    rerank_failed = True
            except Exception as exc:
                logger.warning(
                    "Hybrid+Rerank: CrossEncoder reranker failed (%s). Falling back to RRF top-%d.",
                    exc, k
                )
                rerank_failed = True
            rerank_ms = int((time.perf_counter() - t_rerank) * 1000)

            final: list[tuple[RetrievedChunk, float]] = []
            for c in candidates[:k]:
                doc = c["doc"]
                doc.metadata["_vector_rank"] = c["v_rank"]
                doc.metadata["_bm25_rank"] = c["b_rank"]
                doc.metadata["_rrf_score"] = round(c["rrf_score"], 6)
                doc.metadata["_retrieval_mode"] = "hybrid_rerank"
                if not rerank_failed and "rerank_score" in c:
                    doc.metadata["_rerank_score"] = round(c["rerank_score"], 6)
                    score_val = c["rerank_score"]
                else:
                    doc.metadata["_rerank_score"] = None
                    score_val = c["rrf_score"]
                final.append((doc, score_val))

            total_ms = int((time.perf_counter() - t_start) * 1000)
            actual_log_mode = "hybrid_rerank" if not rerank_failed else "hybrid (rerank fallback)"
            print(
                f"\n{'=' * 30}\n"
                f"Requested Mode : hybrid_rerank\n"
                f"Actual Mode    : {actual_log_mode}\n\n"
                f"Vector Search  : {vec_ms:>4} ms   (candidates: {len(vector_results)})\n"
                f"BM25 Search    : {bm25_ms:>4} ms   (candidates: {len(bm25_results)})\n"
                f"RRF Fusion     : {rrf_ms:>4} ms   (candidate pool: {len(candidates)})\n"
                f"CrossEncoder   : {rerank_ms:>4} ms   (reranked: {len(candidates)})\n"
                f"Total Retrieval: {total_ms:>4} ms   (returned: {len(final)})\n"
                f"{'=' * 30}\n"
            )
            return final

    else:
        # BM25 failed — fall back to trimmed vector results
        trimmed = [(doc, score) for doc, score in vector_results if _use_ready_document(doc, ready_names)][:k]
        total_ms = int((time.perf_counter() - t_start) * 1000)
        print(
            f"\n{'=' * 30}\n"
            f"Requested Mode : {mode}\n"
            f"Actual Mode    : vector   [BM25 unavailable: {fallback_reason}]\n\n"
            f"Vector Search  : {vec_ms:>4} ms   (candidates: {len(vector_results)})\n"
            f"Total Retrieval: {total_ms:>4} ms   (returned: {len(trimmed)})\n"
            f"{'=' * 30}\n"
        )
        return trimmed


def build_citations(retrieved: list[tuple[RetrievedChunk, float]]) -> list[dict]:
    """
    Turns retrieve()'s output into the internal citation shape:
    [{document_id, document_name, page_number, chunk_index, chunk_text, score}, ...]

    retrieve() has already verified eligibility and attached the SQL filename.

    document_id/chunk_index are kept in this internal shape (stripped by
    to_public()) so chat_service can reconstruct a stable chunk identifier
    for query_logs without a second round-trip.
    """
    citations = []
    for doc, score_or_dist in retrieved:
        m = doc.metadata
        if m.get("_retrieval_mode") == "hybrid_rerank" and m.get("_rerank_score") is not None:
            import math
            logit = float(score_or_dist)
            score = round(1.0 / (1.0 + math.exp(-logit)), 4)
        elif m.get("_retrieval_mode") in ("hybrid", "hybrid_rerank"):
            max_rrf = 2.0 / (settings.BM25_RRF_K + 1)
            score = round(min(float(score_or_dist) / max_rrf, 1.0), 4)
        else:
            score = round(1 - float(score_or_dist), 4)   # cosine distance → similarity


        citations.append(
            {
                "document_id":   m["document_id"],
                "document_name": m["document_name"],
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
