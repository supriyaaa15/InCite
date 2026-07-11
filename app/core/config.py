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

    # Chroma
    CHROMA_HOST: str = "localhost"
    CHROMA_PORT: int = 8000

    # Auth
    JWT_SECRET: str 
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
    GOOGLE_API_KEY: str 
    LLM_MODEL: str = "gemini-3-flash-preview"

    # Storage
    UPLOAD_DIR: str = "./uploads"


settings = Settings()
