from functools import lru_cache

import chromadb

from app.core.config import settings


@lru_cache(maxsize=1)
def get_chroma_client() -> chromadb.ClientAPI:
    """
    Embedded Chroma (PersistentClient) — writes to a local directory
    instead of talking to a separate server process. Chosen over a
    separate Chroma service specifically to avoid Render's paid-tier
    requirement for private-network-receiving services (see
    docs/design-decisions.md). Data lives at CHROMA_PERSIST_PATH; if that
    path isn't backed by a persistent disk in production, embeddings are
    lost on redeploy — accepted trade-off for this project, documents can
    be re-uploaded.
    """
    return chromadb.PersistentClient(path=settings.CHROMA_PERSIST_PATH)


def get_collection_store(collection_id: str):
    """
    One Chroma collection per InCite Collection (not one giant shared
    collection) — this is what keeps retrieval scoped correctly. A chat in
    "College Notes" should never retrieve chunks from "Resume", and naming
    the Chroma collection after collection_id enforces that at the storage
    level, not just by filtering after the fact.

    Unchanged by the PersistentClient migration — Collection.upsert() and
    .query() are identical regardless of which client created them.
    """
    client = get_chroma_client()
    return client.get_or_create_collection(
        name=f"collection_{collection_id}", metadata={"hnsw:space": "cosine"}
    )
