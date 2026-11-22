from functools import lru_cache

from langchain_core.embeddings import Embeddings
from sentence_transformers import SentenceTransformer

from app.core.config import settings


@lru_cache(maxsize=1)
def get_embedder() -> SentenceTransformer:
    """
    Loads the embedding model once per process, not once per request.
    lru_cache with maxsize=1 makes this a lazy singleton — first call loads
    it, every call after returns the same already-loaded instance.
    """
    return SentenceTransformer(settings.EMBEDDING_MODEL)


class SentenceTransformerEmbeddings(Embeddings):
    """Share the cached SentenceTransformer across ingestion and retrieval."""

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return get_embedder().encode(texts).tolist()

    def embed_query(self, text: str) -> list[float]:
        return get_embedder().encode([text])[0].tolist()


@lru_cache(maxsize=1)
def get_langchain_embeddings() -> SentenceTransformerEmbeddings:
    return SentenceTransformerEmbeddings()
