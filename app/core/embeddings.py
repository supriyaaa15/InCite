from functools import lru_cache

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
