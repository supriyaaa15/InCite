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


# Substring matches against the exception's own message — deliberately
# string-based rather than importing specific exception classes, since
# the exact exception types vary across google-genai/langchain-google-genai
# SDK versions and this project has already been bitten twice by assuming
# a specific version's API shape. Matching on message content is more
# resilient to that kind of drift.
_QUOTA_ERROR_MARKERS = ("429", "resource_exhausted", "quota")
_TRANSIENT_ERROR_MARKERS = ("503", "timeout", "timed out", "unavailable", "deadline exceeded")


def _is_quota_error(exc: Exception) -> bool:
    return any(marker in str(exc).lower() for marker in _QUOTA_ERROR_MARKERS)


def _is_transient_error(exc: Exception) -> bool:
    return any(marker in str(exc).lower() for marker in _TRANSIENT_ERROR_MARKERS)


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


def _try_model(model_name: str, prompt: str) -> tuple[str, str]:
    """
    One attempt against one model. Tries structured output first; if that
    fails to parse (not necessarily an API error — could just be the
    model not returning valid JSON), falls back to a plain call for this
    same model before giving up on it. Whatever exception surfaces here
    is what _invoke_with_fallback classifies as quota/transient/other.
    """
    llm = get_llm(model_name)
    try:
        structured_llm = llm.with_structured_output(AnswerWithReasoning)
        result = structured_llm.invoke(prompt)
        answer = result.answer.strip()
        reasoning = result.reasoning.strip()
        if not answer:
            raise ValueError("model returned an empty answer")
        return answer, reasoning
    except Exception as structured_error:
        if _is_quota_error(structured_error) or _is_transient_error(structured_error):
            raise  # a real API problem — let the fallback chain handle it, don't mask it with a retry
        response = llm.invoke(prompt)
        content = response.content.strip()
        if not content:
            raise ValueError("model returned an empty response")
        return content, ""  # no structured reasoning from a plain call


def _invoke_with_fallback(prompt: str) -> tuple[str | None, str | None, str]:
    """
    Tries LLM_MODEL first, then each of LLM_FALLBACK_MODELS in order.
    One retry per model for transient errors (503, timeout) with a short
    backoff; quota errors (429/RESOURCE_EXHAUSTED) skip straight to the
    next model, since retrying the same exhausted model can't help.

    Returns (answer, reasoning, model_used). answer is None only if every
    model in the chain failed — the caller is responsible for turning
    that into a user-facing message, this function never raises for an
    exhausted chain.
    """
    models_to_try = [settings.LLM_MODEL] + [
        m.strip() for m in settings.LLM_FALLBACK_MODELS.split(",") if m.strip()
    ]

    for model_name in models_to_try:
        for attempt in range(2):
            try:
                answer, reasoning = _try_model(model_name, prompt)
                return answer, reasoning, model_name
            except Exception as e:
                if _is_quota_error(e):
                    break  # this model is exhausted — no point retrying, try the next one
                if _is_transient_error(e) and attempt == 0:
                    time.sleep(1.5)
                    continue
                break  # unrecognized error, or out of retries — try the next model anyway

    return None, None, ""


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


def _no_confidence_answer() -> tuple[str, str]:
    """
    Deterministic — no LLM call involved. Used whenever nothing survives
    filter_citations(), i.e. every retrieved chunk was below
    MIN_CITATION_SCORE. This is the fix for a real bug found during
    testing (Day 24): asking the LLM to "be honest when unsure" isn't
    reliable — the same weak-retrieval scenario answered correctly one
    time and hallucinated the next. Removing the LLM from this decision
    entirely, rather than asking it to make the right call, is what
    actually fixes it.
    """
    return (
        "I couldn't find enough information in the uploaded documents to "
        "answer that. Try asking differently, or upload a document that "
        "covers this topic.",
        "No relevant content was found in this collection.",
    )


def _generate_answer(
    question: str, citations: list[dict], history: list[dict]
) -> tuple[str, str, str]:
    """
    Returns (answer, reasoning, model_used). citations here are already
    filtered (filter_citations() has run in send_message before this is
    called) — every chunk weak enough to be noise has already been
    removed, so everything reaching the prompt is something worth
    answering from. history is prior turns in this session
    ([{role, content}, ...], oldest first, current question NOT
    included) — without it, a follow-up like "why is it used" has no way
    to resolve what "it" refers to.
    """
    history_block = ""
    if history:
        recent = history[-6:]  # last ~3 exchanges — enough context, bounded prompt size
        turns = "\n".join(f"{h['role'].capitalize()}: {h['content']}" for h in recent)
        history_block = f"Conversation so far:\n{turns}\n\n"

    context = "\n\n---\n\n".join(
        f"[Page {c['page_number']}, {c['document_name']}]\n{c['chunk_text']}" for c in citations
    )
    prompt = f"""{history_block}You are answering the user's latest question using ONLY the context below.

If there is conversation history above, use it to understand what the
current question is really asking — resolve pronouns and references like
"it" or "that" to the specific concept actually being discussed, not just
anything vaguely related.

The retrieved context may contain multiple different concepts. Focus your
answer ONLY on the concept the user is actually asking about. Do not list
or describe unrelated concepts just because they happened to be
retrieved — only bring one in if it's genuinely necessary to answer the
question.

You may reason and synthesize across the relevant parts of the context to
form your answer — you are not limited to quoting an exact matching
sentence. Do not introduce facts that are not supported by the context.
If the context genuinely does not contain enough information to answer,
say so honestly instead of guessing.

Write any mathematical notation in plain, readable text or standard
unicode symbols (e.g. "U, V transpose, and a diagonal matrix Σ") — do NOT
use LaTeX syntax (no dollar signs, no backslash-commands like \\Sigma or
\\sqrt). The person reading this cannot render LaTeX.

Context:
{context}

Question: {question}"""

    answer, reasoning, model_used = _invoke_with_fallback(prompt)

    if answer is None:
        # Every model in the fallback chain failed — most likely every
        # configured model has hit its quota. A crash here would show the
        # user a raw "Failed to fetch"; this is the friendly alternative.
        return (
            "The AI service has reached its daily quota. Please try again later.",
            "No answer could be generated — every configured AI model is "
            "currently unavailable or has reached its quota.",
            "",
        )

    if not reasoning:
        reasoning = _fallback_reasoning(citations)
    return answer, reasoning, model_used


def send_message(
    db: Session,
    user_id: uuid.UUID,
    collection_id: uuid.UUID,
    message_text: str,
    session_id: uuid.UUID | None,
) -> tuple[ChatSession, str, str, list[dict], str]:
    """
    The full chat flow: verify ownership, get/create the session, retrieve
    relevant chunks, generate a grounded answer + reasoning, persist
    everything (both messages + a query log), return what the route needs.
    Retrieval itself is delegated to retrieval_service — this function only
    orchestrates the conversation around it.

    Returns (session, answer, reasoning, public_citations, confidence).
    confidence is "none" (nothing cleared the noise filter, LLM never
    called), "error" (retrieval found something, but every configured LLM
    failed/hit quota), "low" or "medium" or "high" (based on the best
    surviving citation score against MEDIUM_CONFIDENCE_THRESHOLD and
    HIGH_CONFIDENCE_THRESHOLD).
    """
    collection_service.get_owned_collection(db, user_id, collection_id)

    if session_id is not None:
        session = get_owned_session(db, user_id, session_id)
        if session.collection_id != collection_id:
            raise SessionCollectionMismatchError()
    else:
        title = message_text[:60]
        session = chat_repository.create_session(db, user_id, collection_id, title=title)

    # Fetched BEFORE the current user message is saved below — this is
    # prior turns only, current question excluded, which is exactly what
    # _generate_answer needs to resolve follow-ups against.
    prior_messages = chat_repository.list_messages_by_session(db, session.id)
    history = [{"role": m.role.value, "content": m.content} for m in prior_messages]

    chat_repository.create_message(db, session.id, role=MessageRole.user, content=message_text)

    retrieval_start = time.time()
    retrieved = retrieval_service.retrieve(collection_id, message_text)
    retrieval_time_ms = int((time.time() - retrieval_start) * 1000)

    # Full chunk_text kept internal — only used to build the LLM prompt.
    # Unfiltered — kept as-is for query_logs below, so debugging retrieval
    # quality later sees everything that came back, not just what passed.
    citations = retrieval_service.build_citations(db, retrieved)
    filtered_citations = retrieval_service.filter_citations(citations, settings.MIN_CITATION_SCORE)

    llm_start = time.time()
    model_used = ""
    if not filtered_citations:
        answer, reasoning = _no_confidence_answer()
        confidence = "none"
    else:
        answer, reasoning, model_used = _generate_answer(message_text, filtered_citations, history)
        top_score = max(c["score"] for c in filtered_citations)
        if not model_used:
            # retrieval succeeded but every LLM in the chain failed —
            # distinct from a retrieval-quality problem, so it gets its
            # own confidence value rather than being folded into "low"
            confidence = "error"
        elif top_score >= settings.HIGH_CONFIDENCE_THRESHOLD:
            confidence = "high"
        elif top_score >= settings.MEDIUM_CONFIDENCE_THRESHOLD:
            confidence = "medium"
        else:
            confidence = "low"
    llm_time_ms = int((time.time() - llm_start) * 1000)

    # Excerpted version is what actually leaves the server — API response
    # and what gets persisted in Message.citations. Built from the
    # filtered list — a citation too weak to inform the answer shouldn't
    # be shown as if it did.
    public_citations = retrieval_service.to_public(filtered_citations, message_text)

    assistant_message = chat_repository.create_message(
        db, session.id, role=MessageRole.assistant, content=answer, citations=public_citations
    )

    # Logged from the ORIGINAL unfiltered citations, deliberately — this
    # table exists for debugging retrieval quality, so it should show
    # everything that came back, not just what survived filtering.
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
        llm_model=model_used or settings.LLM_MODEL,  # log which model actually
        # answered, not just the configured primary — this is the whole
        # point of the fallback chain being observable, not a black box
    )

    return session, answer, reasoning, public_citations, confidence
