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
    """
    LangChain's vectorstore integrations expect an object implementing its
    Embeddings interface — just embed_documents() and embed_query(). This
    wraps our existing get_embedder() singleton instead of letting
    LangChain load its own separate copy of the model, so ingestion (raw
    sentence-transformers calls) and retrieval (through this wrapper)
    always produce identical embeddings from the exact same weights.
    """

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return get_embedder().encode(texts).tolist()

    def embed_query(self, text: str) -> list[float]:
        return get_embedder().encode([text])[0].tolist()


@lru_cache(maxsize=1)
def get_langchain_embeddings() -> SentenceTransformerEmbeddings:
    return SentenceTransformerEmbeddings()
