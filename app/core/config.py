from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Single source of truth for all configuration.
    Values come from environment variables (.env in dev, real env vars in prod).
    Never hardcode these values anywhere else in the app — import settings instead.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database
    DATABASE_URL: str = "postgresql://incite:incite@localhost:5432/incite"

    # Chroma — embedded (PersistentClient), not a separate server. Writes
    # to a local directory instead of talking to a network service.
    CHROMA_PERSIST_PATH: str = "./chroma_data"

    # Auth
    JWT_SECRET: str  # required — no default, app won't start without it in .env
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 1440

    # RAG tuning — change these to experiment with retrieval strategy.
    # query_logs.top_k records which value produced which answer, so you
    # can compare experiments after the fact.
    CHUNK_SIZE: int = 1000
    CHUNK_OVERLAP: int = 150
    TOP_K: int = 5
    EMBEDDING_MODEL: str = "sentence-transformers/all-MiniLM-L6-v2"

    # Hybrid retrieval (Phase 4 & Phase 6). RETRIEVAL_MODE controls whether
    # BM25 + RRF and CrossEncoder reranking are used alongside vector search.
    # "vector" = vector search only. "hybrid" = BM25 + Chroma vector + RRF.
    # "hybrid_rerank" = BM25 + Chroma vector + RRF (top-15) + CrossEncoder rerank.
    RETRIEVAL_MODE: str = "vector"   # "vector" | "hybrid" | "hybrid_rerank"
    BM25_RRF_K: int = 60
    RERANK_POOL_K: int = 15
    RERANKER_MODEL: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"

    # Confidence-aware RAG (Days 28-29, recalibrated Day 30). Two
    # boundaries, not one — see docs/design-decisions.md for why a single
    # top-score cutoff isn't reliable on its own (a real historical case
    # had the same top score for both a correct refusal and an incorrect
    # answer). Thresholds tuned against this project's actual test data.
    MIN_CITATION_SCORE: float = 0.05  # below this: filtered out as noise,
    # never shown, never sent to the LLM. If nothing survives, skip
    # generation entirely and return a deterministic "not enough info"
    # message — no reliance on the model choosing to be honest.
    MEDIUM_CONFIDENCE_THRESHOLD: float = 0.20  # best surviving score below
    # this (but >= MIN_CITATION_SCORE): confidence="low".
    HIGH_CONFIDENCE_THRESHOLD: float = 0.45  # best surviving score at or
    # above this: confidence="high". Between the two: confidence="medium".

    # LLM — Groq provider
    GROQ_API_KEY: str  # required — no default, app won't start without it in .env
    LLM_MODEL: str = "openai/gpt-oss-120b"
    # Comma-separated, tried in order if LLM_MODEL hits a quota/rate limit
    # or a transient error. Each model is attempted via chat_service._invoke_with_fallback().
    LLM_FALLBACK_MODELS: str = "openai/gpt-oss-20b,qwen/qwen3.8-27b"

    # Storage
    UPLOAD_DIR: str = "./uploads"

    # Upload validation limits — override via env vars in production.
    # MAX_UPLOAD_SIZE_MB: hard cap on raw file size before any processing.
    # MAX_PDF_PAGES: hard cap on page count, checked synchronously in the
    # route handler so we never create an orphaned "processing" DB row.
    MAX_UPLOAD_SIZE_MB: int = 20
    MAX_PDF_PAGES: int = 800

    @property
    def max_upload_size_bytes(self) -> int:
        """Derived convenience — avoids scattering * 1024 * 1024 everywhere."""
        return self.MAX_UPLOAD_SIZE_MB * 1024 * 1024

    # CORS — comma-separated list of allowed frontend origins. Defaults to
    # local Vite dev server; set to the real deployed frontend URL in
    # production (e.g. https://incite.vercel.app).
    ALLOWED_ORIGINS: str = "http://localhost:5173"


settings = Settings()
