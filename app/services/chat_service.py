import time
import uuid

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.llm import get_llm
from app.models.chat import ChatSession, MessageRole
from app.repositories import chat_repository, query_log_repository
from app.services import collection_service, retrieval_service


class ChatSessionNotFoundError(Exception):
    """Same error for missing and not-owned — see collection_service for
    the reasoning, it's identical here."""

    pass


class SessionCollectionMismatchError(Exception):
    """Raised if a session_id is passed for a different collection than
    the one in the URL — prevents mixing retrieval context across
    collections mid-conversation."""

    pass


class AnswerWithReasoning(BaseModel):
    """
    Schema for structured LLM output. Passed to with_structured_output()
    (Day 14-15) — LangChain handles the JSON-mode request, schema
    enforcement, and parsing that Day 12-13 did by hand with
    response_mime_type + json.loads. This is the actual "what does
    LangChain abstract" answer for this project: about 30 lines of manual
    JSON handling collapsed into one method call.
    """

    answer: str = Field(description="The answer to the question, reasoned from the given context.")
    reasoning: str = Field(
        description="One short sentence naming which page(s) and document(s) "
        "the answer draws from and what they cover."
    )


def get_owned_session(db: Session, user_id: uuid.UUID, session_id: uuid.UUID) -> ChatSession:
    session = chat_repository.get_session_by_id(db, session_id)
    if session is None or session.user_id != user_id:
        raise ChatSessionNotFoundError()
    return session


def list_sessions(db: Session, user_id: uuid.UUID) -> list[ChatSession]:
    return chat_repository.list_sessions_by_user(db, user_id)


def list_messages(db: Session, user_id: uuid.UUID, session_id: uuid.UUID) -> list:
    session = get_owned_session(db, user_id, session_id)
    return chat_repository.list_messages_by_session(db, session.id)


def delete_session(db: Session, user_id: uuid.UUID, session_id: uuid.UUID) -> None:
    session = get_owned_session(db, user_id, session_id)
    chat_repository.delete_session(db, session)


def _fallback_reasoning(citations: list[dict]) -> str:
    """Deterministic, metadata-only reasoning — used whenever the model
    doesn't return valid structured output. Never fails, unlike trusting
    the model to always format correctly."""
    if not citations:
        return "No relevant content was found in this collection."
    by_doc: dict[str, set[int]] = {}
    for c in citations:
        by_doc.setdefault(c["document_name"], set()).add(c["page_number"])
    parts = [
        f"page(s) {', '.join(str(p) for p in sorted(pages))} of {doc}"
        for doc, pages in by_doc.items()
    ]
    return "This answer was derived from " + "; ".join(parts) + "."


def _generate_answer(question: str, citations: list[dict]) -> tuple[str, str]:
    """
    Returns (answer, reasoning). The model is asked to reason and
    synthesize across the retrieved context (not just locate an exact
    matching sentence) while staying grounded in it. Structured output is
    requested via with_structured_output() — if the model's output can't
    be validated against AnswerWithReasoning, LangChain raises rather than
    silently returning something malformed, so the except block below is
    still the same safety net as Day 12-13: fall back to a plain call and
    build reasoning deterministically from citation metadata.
    """
    if not citations:
        return (
            "I couldn't find anything relevant to that question in this collection.",
            _fallback_reasoning(citations),
        )

    context = "\n\n---\n\n".join(
        f"[Page {c['page_number']}, {c['document_name']}]\n{c['chunk_text']}" for c in citations
    )
    prompt = f"""You are answering a question using ONLY the context below. You may
reason and synthesize across the provided context to form your answer —
you are not limited to quoting an exact matching sentence. Do not
introduce facts that are not supported by the context. If the context
genuinely does not contain enough information to answer, say so honestly
instead of guessing.

Context:
{context}

Question: {question}"""

    try:
        structured_llm = get_llm().with_structured_output(AnswerWithReasoning)
        result = structured_llm.invoke(prompt)
        answer = result.answer.strip()
        reasoning = result.reasoning.strip() or _fallback_reasoning(citations)
        if not answer:
            raise ValueError("model returned an empty answer")
        return answer, reasoning
    except Exception:
        # Model didn't return valid structured output, or the call failed
        # for some other reason — don't crash the request over it. Fall
        # back to a plain unstructured call for the answer, and build
        # reasoning deterministically from citation metadata rather than
        # trusting the model's formatting.
        response = get_llm().invoke(prompt)
        return response.content.strip(), _fallback_reasoning(citations)


def send_message(
    db: Session,
    user_id: uuid.UUID,
    collection_id: uuid.UUID,
    message_text: str,
    session_id: uuid.UUID | None,
) -> tuple[ChatSession, str, str, list[dict]]:
    """
    The full chat flow: verify ownership, get/create the session, retrieve
    relevant chunks, generate a grounded answer + reasoning, persist
    everything (both messages + a query log), return what the route needs.
    Retrieval itself is delegated to retrieval_service — this function only
    orchestrates the conversation around it.
    """
    collection_service.get_owned_collection(db, user_id, collection_id)

    if session_id is not None:
        session = get_owned_session(db, user_id, session_id)
        if session.collection_id != collection_id:
            raise SessionCollectionMismatchError()
    else:
        title = message_text[:60]
        session = chat_repository.create_session(db, user_id, collection_id, title=title)

    chat_repository.create_message(db, session.id, role=MessageRole.user, content=message_text)

    retrieval_start = time.time()
    retrieved = retrieval_service.retrieve(collection_id, message_text)
    retrieval_time_ms = int((time.time() - retrieval_start) * 1000)

    # Full chunk_text kept internal — only used to build the LLM prompt.
    citations = retrieval_service.build_citations(db, retrieved)

    llm_start = time.time()
    answer, reasoning = _generate_answer(message_text, citations)
    llm_time_ms = int((time.time() - llm_start) * 1000)

    # Excerpted version is what actually leaves the server — API response
    # and what gets persisted in Message.citations.
    public_citations = retrieval_service.to_public(citations)

    assistant_message = chat_repository.create_message(
        db, session.id, role=MessageRole.assistant, content=answer, citations=public_citations
    )

    # Reconstructed using the same id scheme ingestion_service used when
    # writing to Chroma (document_id_p{page}_c{chunk_index}) — retrieve()
    # returns LangChain Documents now, not raw Chroma ids, so this is
    # rebuilt from citation metadata instead of read directly off the
    # query result.
    retrieved_chunk_ids = [
        f"{c['document_id']}_p{c['page_number']}_c{c['chunk_index']}" for c in citations
    ]

    query_log_repository.create(
        db,
        message_id=assistant_message.id,
        retrieved_chunk_ids=retrieved_chunk_ids,
        similarity_scores=[c["score"] for c in citations],
        top_k=settings.TOP_K,
        response_time_ms=retrieval_time_ms + llm_time_ms,
        llm_model=settings.LLM_MODEL,
    )

    return session, answer, reasoning, public_citations