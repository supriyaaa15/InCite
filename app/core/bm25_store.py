"""
BM25 index store — per-collection, in-memory, lazy-loaded.

One BM25Okapi index is built per collection on first access and cached
until explicitly invalidated. The corpus is loaded from Chroma (the same
persistent collection used by vector retrieval), so there is a single
source of truth: Chroma. No Postgres query is needed when document_name
is present in the Chroma metadata (true for all documents ingested after
Phase 4). For documents ingested before Phase 4, document_name is absent
from the metadata; the caller (bm25_store.get_index) accepts an optional
db session and performs a one-time bulk Postgres lookup only for those
legacy chunks.

Invalidation contract
---------------------
invalidate(collection_id) MUST be called:
  - after every successful ingestion (new chunks added to Chroma)
  - after every document deletion (chunks removed from Chroma)

Missing an invalidation means the index silently returns stale results.
Both call sites are in ingestion_service.py and document_routes.py.

Thread safety
-------------
Module-level dict writes are GIL-protected in CPython, which is the
runtime this application uses. For other runtimes, add a threading.Lock
around cache reads/writes.
"""

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from rank_bm25 import BM25Okapi
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# ── Internal types ────────────────────────────────────────────────────────────

@dataclass
class _BM25Index:
    bm25: Optional[BM25Okapi]   # None when corpus is empty — search() returns [] immediately
    corpus: list[str]           # raw chunk texts, parallel to the two lists below
    chroma_ids: list[str]       # Chroma document IDs, one per chunk
    doc_meta: list[dict]        # {document_id, document_name, page_number, chunk_index}
    built_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ── Module-level cache ────────────────────────────────────────────────────────

_cache: dict[str, _BM25Index] = {}   # key: str(collection_id)


# ── Tokenizer ─────────────────────────────────────────────────────────────────

_STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "in", "on", "at", "of",
    "to", "for", "and", "or", "but", "what", "how", "why", "does", "do",
    "can", "it", "this", "that", "with", "as", "be",
}


def _tokenize(text: str) -> list[str]:
    """
    Lowercase word-boundary tokenisation with stopword removal.
    Kept intentionally simple: BM25 benefits from reducing vocabulary noise
    more than from sophisticated stemming at this collection size.
    """
    words = re.findall(r"\w+", text.lower())
    return [w for w in words if w not in _STOPWORDS]


# ── Public API ────────────────────────────────────────────────────────────────

def invalidate(collection_id: str | uuid.UUID) -> None:
    """
    Drop the cached BM25 index for this collection. The next call to
    get_index() will trigger a full rebuild from the current Chroma state.

    Safe to call even if no index is cached (no-op).
    """
    key = str(collection_id)
    dropped = _cache.pop(key, None)
    if dropped is not None:
        logger.debug(
            "BM25 cache invalidated | collection_id=%s built_at=%s",
            key,
            dropped.built_at.isoformat(),
        )


def get_index(
    collection_id: str | uuid.UUID,
    chroma_collection,           # chromadb.Collection — typed loosely to avoid circular import
    db: Optional[Session] = None,
) -> _BM25Index:
    """
    Return the cached BM25 index for this collection, building it first
    if it doesn't exist.

    Parameters
    ----------
    collection_id:
        The InCite collection UUID (used as the cache key).
    chroma_collection:
        The raw chromadb Collection object for this collection.
        Must support .get(include=[...]) and .count().
    db:
        Optional SQLAlchemy session. Only used to resolve document_id →
        filename for chunks ingested before Phase 4 (which lack
        document_name in Chroma metadata). For fully up-to-date collections
        this is never called.
    """
    key = str(collection_id)
    if key in _cache:
        return _cache[key]

    logger.debug("BM25 cache miss — building index | collection_id=%s", key)
    index = _build_index(collection_id, chroma_collection, db)
    _cache[key] = index
    logger.info(
        "BM25 index built | collection_id=%s chunks=%d built_at=%s",
        key,
        len(index.corpus),
        index.built_at.isoformat(),
    )
    return index


def search(index: _BM25Index, query: str, top_n: int) -> list[tuple[str, float]]:
    """
    Run a BM25 query against the index.

    Returns
    -------
    list of (chroma_id, bm25_score) sorted best-first, length ≤ top_n.
    Chunks with score 0 are excluded (they matched no query terms and
    contribute nothing useful to RRF).
    """
    if not index.corpus or index.bm25 is None:
        # Empty collection or empty corpus — nothing to score.
        return []

    tokens = _tokenize(query)
    if not tokens:
        return []

    scores = index.bm25.get_scores(tokens)

    # Pair each score with its chroma_id, filter zeros, sort descending
    pairs = [
        (index.chroma_ids[i], float(scores[i]))
        for i in range(len(scores))
        if scores[i] > 0.0
    ]
    pairs.sort(key=lambda x: x[1], reverse=True)
    return pairs[:top_n]


# ── Private ───────────────────────────────────────────────────────────────────

def _build_index(
    collection_id: str | uuid.UUID,
    chroma_collection,
    db: Optional[Session],
) -> _BM25Index:
    """
    Load all chunks from Chroma and build a BM25Okapi index.

    document_name is taken from Chroma metadata when available (all docs
    ingested in Phase 4+). For older chunks missing the field, a single
    bulk Postgres query resolves the gaps if a db session is provided;
    otherwise "unknown document" is used as a safe fallback.
    """
    result = chroma_collection.get(include=["documents", "metadatas"])

    ids: list[str] = result["ids"]
    texts: list[str] = result["documents"] or []
    metadatas: list[dict] = result["metadatas"] or []

    if not ids:
        # Empty collection — return a sentinel index. search() returns [] for it.
        # We do NOT instantiate BM25Okapi here: BM25Okapi([[]])
        # raises ZeroDivisionError inside _calc_idf (average_idf = sum/0
        # when the vocabulary is empty).
        return _BM25Index(
            bm25=None,
            corpus=[],
            chroma_ids=[],
            doc_meta=[],
        )

    # ── Resolve document_name for legacy chunks ───────────────────────────
    # Chunks ingested before Phase 4 lack "document_name" in their metadata.
    # Collect their document_ids and resolve via a single Postgres query.
    needs_resolution: set[str] = {
        m["document_id"]
        for m in metadatas
        if not m.get("document_name")
    }
    filename_by_id: dict[str, str] = {}

    if needs_resolution and db is not None:
        try:
            from app.repositories import document_repository
            doc_uuids = [uuid.UUID(did) for did in needs_resolution]
            docs = document_repository.get_by_ids(db, doc_uuids)
            filename_by_id = {str(d.id): d.filename for d in docs}
        except Exception:
            logger.exception(
                "BM25 build: Postgres fallback for document names failed | "
                "collection_id=%s — 'unknown document' will be used for %d chunk(s)",
                collection_id,
                len(needs_resolution),
            )

    # ── Assemble parallel arrays ──────────────────────────────────────────
    corpus: list[str] = []
    chroma_ids: list[str] = []
    doc_meta: list[dict] = []

    for chroma_id, text, meta in zip(ids, texts, metadatas):
        if not text:
            continue   # skip empty chunks (shouldn't happen, but be defensive)

        doc_name = (
            meta.get("document_name")
            or filename_by_id.get(meta.get("document_id", ""), "unknown document")
        )

        corpus.append(text)
        chroma_ids.append(chroma_id)
        doc_meta.append({
            "document_id":   meta.get("document_id", ""),
            "document_name": doc_name,
            "page_number":   meta.get("page_number", 0),
            "chunk_index":   meta.get("chunk_index", 0),
        })

    if not corpus:
        return _BM25Index(
            bm25=None,
            corpus=[],
            chroma_ids=[],
            doc_meta=[],
        )

    tokenized_corpus = [_tokenize(t) for t in corpus]
    bm25 = BM25Okapi(tokenized_corpus)

    return _BM25Index(
        bm25=bm25,
        corpus=corpus,
        chroma_ids=chroma_ids,
        doc_meta=doc_meta,
    )
