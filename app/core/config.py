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
    CHUNK_SIZE: int = 500
    CHUNK_OVERLAP: int = 50
    TOP_K: int = 5
    EMBEDDING_MODEL: str = "sentence-transformers/all-MiniLM-L6-v2"

    # LLM
    GOOGLE_API_KEY: str  # required — no default, app won't start without it in .env
    LLM_MODEL: str = "gemini-2.5-flash"

    # Storage
    UPLOAD_DIR: str = "./uploads"

    # CORS — comma-separated list of allowed frontend origins. Defaults to
    # local Vite dev server; set to the real deployed frontend URL in
    # production (e.g. https://incite.vercel.app).
    ALLOWED_ORIGINS: str = "http://localhost:5173"


settings = Settings()
