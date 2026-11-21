from functools import lru_cache

from langchain_groq import ChatGroq

from app.core.config import settings


@lru_cache(maxsize=8)
def get_llm(model: str | None = None) -> ChatGroq:
    """
    Cached per model name — the fallback chain (chat_service.py) requests
    several different models across the primary + fallback list, and each
    one should only ever be constructed once, not per-request.
    """
    return ChatGroq(
        model=model or settings.LLM_MODEL,
        groq_api_key=settings.GROQ_API_KEY,
        temperature=0.1,
    )
