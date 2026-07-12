from functools import lru_cache

import chromadb

from app.core.config import settings


@lru_cache(maxsize=1)
def get_chroma_client() -> chromadb.HttpClient:
    return chromadb.HttpClient(host=settings.CHROMA_HOST, port=settings.CHROMA_PORT)


def get_collection_store(collection_id: str):
    """
    One Chroma collection per InCite Collection (not one giant shared
    collection) — this is what keeps retrieval scoped correctly. A chat in
    "College Notes" should never retrieve chunks from "Resume", and naming
    the Chroma collection after collection_id enforces that at the storage
    level, not just by filtering after the fact.
    """
    client = get_chroma_client()
    return client.get_or_create_collection(
        name=f"collection_{collection_id}", metadata={"hnsw:space": "cosine"}
    )
