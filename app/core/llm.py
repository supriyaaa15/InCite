from functools import lru_cache

from langchain_google_genai import ChatGoogleGenerativeAI

from app.core.config import settings


@lru_cache(maxsize=8)
def get_llm(model: str | None = None) -> ChatGoogleGenerativeAI:
    """
    Cached per model name — the fallback chain (chat_service.py) requests
    several different models across the primary + fallback list, and each
    one should only ever be constructed once, not per-request.
    """
    return ChatGoogleGenerativeAI(model=model or settings.LLM_MODEL, google_api_key=settings.GOOGLE_API_KEY)
