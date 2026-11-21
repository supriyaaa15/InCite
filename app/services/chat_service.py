import enum
import time
import uuid

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.llm import get_llm
from app.models.chat import ChatSession, MessageRole
from app.models.document import DocumentStatus
from app.repositories import chat_repository, document_repository, query_log_repository
from app.services import collection_service, comparison_service, retrieval_service

import logging

logger = logging.getLogger(__name__)


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
    Schema for structured LLM output.  Passed to with_structured_output()
    (Day 14-15) — LangChain handles the JSON-mode request, schema
    enforcement, and parsing that Day 12-13 did by hand with
    response_mime_type + json.loads. This is the actual "what does
    LangChain abstract" answer for this project: about 30 lines of manual
    JSON handling collapsed into one method call.

    follow_up_questions was added (Day 33 refactor) to collapse what was
    previously a separate second LLM call into the same structured
    response.  The field is optional with an empty-list default so that:
    - existing callers that don't read it are unaffected
    - the plain-text fallback path in _try_model can return [] without
      breaking the schema contract
    - a model that omits or misformats the field degrades gracefully to
      no suggestions rather than crashing
    """

    answer: str = Field(description="The answer to the question, reasoned from the given context.")
    reasoning: str = Field(
        description="One short sentence naming which page(s) and document(s) "
        "the answer draws from and what they cover."
    )
    follow_up_questions: list[str] = Field(
        default_factory=list,
        description=(
            "Exactly 3 concise follow-up questions that help the user explore the documents further. "
            "Ground every question strictly in the retrieved context — do not hallucinate topics. "
            "Prefer questions that encourage deeper understanding, comparisons between concepts, "
            "summaries of related sections, or practical applications of what was discussed. "
            "Avoid repeating the user's question and avoid generic questions like 'Tell me more'. "
            "Return an empty list if no meaningful grounded questions can be generated."
        ),
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


def _try_model(model_name: str, prompt: str) -> tuple[str, str, list[str]]:
    """
    One attempt against one model. Tries structured output first; if that
    fails to parse (not necessarily an API error — could just be the
    model not returning valid JSON), falls back to a plain call for this
    same model before giving up on it. Whatever exception surfaces here
    is what _invoke_with_fallback classifies as quota/transient/other.

    Returns (answer, reasoning, follow_up_questions).
    follow_up_questions is [] on the plain-text fallback path — there's
    no reliable way to extract a list from unstructured text, so we
    degrade gracefully to no suggestions rather than parsing heuristically.
    """
    llm = get_llm(model_name)
    try:
        structured_llm = llm.with_structured_output(AnswerWithReasoning)
        result = structured_llm.invoke(prompt)
        answer = result.answer.strip()
        reasoning = result.reasoning.strip()
        if not answer:
            raise ValueError("model returned an empty answer")
        follow_up_questions = _validate_followups(result.follow_up_questions)
        return answer, reasoning, follow_up_questions
    except Exception as structured_error:
        if _is_quota_error(structured_error) or _is_transient_error(structured_error):
            raise  # a real API problem — let the fallback chain handle it, don't mask it with a retry
        response = llm.invoke(prompt)
        content = response.content.strip()
        if not content:
            raise ValueError("model returned an empty response")
        return content, "", []  # no structured reasoning or follow-ups from a plain call


def _validate_followups(raw: list[str]) -> list[str]:
    """
    Normalise the follow_up_questions list returned by the structured LLM
    call: strip whitespace, discard empty strings, deduplicate (preserving
    order), cap at 3.  Never raises — called from _try_model where any
    exception surfaces to the fallback chain.
    """
    seen: set[str] = set()
    result: list[str] = []
    for q in raw:
        q = q.strip()
        if not q:
            continue
        lower = q.lower()
        if lower not in seen:
            seen.add(lower)
            result.append(q)
    return result[:3]


def _invoke_with_fallback(prompt: str) -> tuple[str | None, str | None, list[str], str]:
    """
    Tries LLM_MODEL first, then each of LLM_FALLBACK_MODELS in order.
    One retry per model for transient errors (503, timeout) with a short
    backoff; quota errors (429/RESOURCE_EXHAUSTED) skip straight to the
    next model, since retrying the same exhausted model can't help.

    Returns (answer, reasoning, follow_up_questions, model_used).
    answer is None only if every model in the chain failed — the caller
    is responsible for turning that into a user-facing message, this
    function never raises for an exhausted chain.
    follow_up_questions is [] on any failure or plain-text fallback.
    """
    models_to_try = [settings.LLM_MODEL] + [
        m.strip() for m in settings.LLM_FALLBACK_MODELS.split(",") if m.strip()
    ]

    for model_name in models_to_try:
        logger.warning(f"Trying model: {model_name}")
        for attempt in range(2):
            try:
                answer, reasoning, follow_up_questions = _try_model(model_name, prompt)
                return answer, reasoning, follow_up_questions, model_name
            except Exception as e:
                logger.warning(f"{model_name} failed: {e}")
                if _is_quota_error(e):
                    break  # this model is exhausted — no point retrying, try the next one
                if _is_transient_error(e) and attempt == 0:
                    time.sleep(1.5)
                    continue
                break  # unrecognized error, or out of retries — try the next model anyway

    return None, None, [], ""


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
    question: str, citations: list[dict], recent_user_messages: list[str]
) -> tuple[str, str, list[str], str]:
    """
    Returns (answer, reasoning, follow_up_questions, model_used).

    citations here are already filtered (filter_citations() has run in
    send_message before this is called) — every chunk weak enough to be
    noise has already been removed, so everything reaching the prompt is
    something worth answering from.

    Recent user messages help resolve references in follow-up questions.

    follow_up_questions is populated from the same structured LLM call as
    the answer — no second API request.  See design-decisions.md (Day 33
    refactor) for why this collapsed from two calls to one.
    """
    history_block = ""
    if recent_user_messages:
        turns = "\n".join(f"User: {message}" for message in recent_user_messages)
        history_block = f"Earlier user messages:\n{turns}\n\n"

    context = "\n\n---\n\n".join(
        f"[Page {c['page_number']}, {c['document_name']}]\n{c['chunk_text']}" for c in citations
    )
    prompt = f"""{history_block}You are answering the user's latest question using ONLY the context below.

Use earlier user messages only to understand the request and resolve references
like "it" or "that". Treat the retrieved excerpts as the sole source of
document claims.

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

After answering, also generate follow_up_questions: exactly 3 questions
that help the user explore the uploaded documents further. Prefer questions
that encourage deeper understanding, comparisons between concepts,
summaries of related topics, or practical applications. Avoid repeating the
user's question, avoid generic questions like "Tell me more", and do not
suggest questions about information absent from the retrieved context.

Context:
{context}

Question: {question}"""

    answer, reasoning, follow_up_questions, model_used = _invoke_with_fallback(prompt)

    if answer is None:
        # Every model in the fallback chain failed — most likely every
        # configured model has hit its quota. A crash here would show the
        # user a raw "Failed to fetch"; this is the friendly alternative.
        return (
            "The AI service has reached its daily quota. Please try again later.",
            "No answer could be generated — every configured AI model is "
            "currently unavailable or has reached its quota.",
            [],
            "",
        )

    if not reasoning:
        reasoning = _fallback_reasoning(citations)
    return answer, reasoning, follow_up_questions, model_used


# Recovery suggestions for the low-confidence path (confidence == "none"):
# retrieval found nothing to ground the LLM on, so there's no answer to
# build grounded follow-ups from.  These static questions help the user
# discover what's in their collection instead.
_RECOVERY_QUESTIONS = [
    "What topics are covered in the uploaded documents?",
    "Can you summarize what these documents are about?",
    "What are the main concepts discussed in these documents?",
]

# Shown when a RECOMMEND or COMPARE request arrives but the collection
# has fewer than 2 ready documents.
_SINGLE_DOC_RECOMMEND_MSG = (
    "Only one document is currently ready in this collection. "
    "Upload additional documents to receive recommendations across multiple sources."
)
_SINGLE_DOC_COMPARE_MSG = (
    "Comparing documents requires at least two ready documents in this collection. "
    "Upload another document to use this feature."
)


# ---------------------------------------------------------------------------
# Intent classification
# ---------------------------------------------------------------------------

class IntentType(enum.Enum):
    QUESTION = "question"
    RECOMMEND = "recommend"
    COMPARE = "compare"


# ── COMPARE detection ─────────────────────────────────────────────────────────
#
# Design rule (from user feedback):
#   COMPARE fires ONLY when the message explicitly refers to documents/files.
#   "What is the difference between supervised and unsupervised learning?"
#   must remain QUESTION even though it contains a compare-verb.
#
# Two-part check:
#   1. A compare-verb is present (compare, contrast, vs, versus, side by side).
#   2. A document-reference signal is present (pdf, document, file, "these docs",
#      ordinal phrases like "first two", "both documents", etc.).
#
# Both conditions must be satisfied simultaneously for COMPARE intent.

_COMPARE_VERBS = frozenset([
    "compare", "contrast", " vs ", " vs.", "versus", "side by side",
    "side-by-side",
])

# Words/patterns that indicate the user is referring to uploaded files, not
# to abstract concepts. These are anchors — they must co-occur with a compare-
# verb to trigger COMPARE.
_DOCUMENT_REFERENCE_SIGNALS = frozenset([
    ".pdf", "document", "documents", "file", "files", "pdf", "pdfs",
    "notes", "my notes", "these docs", "those docs", "both docs",
    "the docs", "first document", "second document", "third document",
    "first two", "both documents", "these files", "those files",
    "first file", "second file", "uploaded", "which doc", "which pdf",
    "which file",
])


def _has_compare_verb(msg: str) -> bool:
    return any(phrase in msg for phrase in _COMPARE_VERBS)


def _has_document_reference(msg: str) -> bool:
    return any(signal in msg for signal in _DOCUMENT_REFERENCE_SIGNALS)


# Phrases that signal RECOMMEND intent.
# Kept specific to avoid false positives on common words like "examples"
# appearing in normal questions ("Give me examples of...").
_RECOMMEND_PHRASES = frozenset([
    "which document", "which file", "which pdf", "best document",
    "which one should", "which should i", "recommend",
    "quick revision", "revision notes", "most detailed",
    "should i read", "interview prep", "before interview",
    "for beginners", "beginner friendly", "which has more",
    "which has better", "better for", "best for",
])


def classify_intent(message: str, history: list[str]) -> IntentType:  # noqa: ARG001
    """
    Rule-based intent classification. O(n) in keyword count, 0 API calls.
    QUESTION is the safe default — misclassification degrades to a normal
    answer rather than an error.

    COMPARE requires BOTH a compare-verb AND a document-reference signal.
    This prevents concept-comparison questions ("difference between supervised
    and unsupervised learning") from being misclassified as COMPARE.

    history is accepted for future use (e.g. resolving follow-up intents
    in a multi-turn conversation) but classification currently uses only
    the current message.
    """
    msg_lower = message.lower()

    # COMPARE: compare-verb + document reference must both be present.
    # Checked before RECOMMEND so "compare these two PDFs, which is better"
    # is classified as COMPARE rather than RECOMMEND.
    if _has_compare_verb(msg_lower) and _has_document_reference(msg_lower):
        return IntentType.COMPARE

    if any(phrase in msg_lower for phrase in _RECOMMEND_PHRASES):
        return IntentType.RECOMMEND

    return IntentType.QUESTION



def send_message(
    db: Session,
    user_id: uuid.UUID,
    collection_id: uuid.UUID,
    message_text: str,
    session_id: uuid.UUID | None,
) -> tuple[ChatSession, str, str, list[dict], str | None, list[str], list, list]:
    """
    Full chat orchestration: classify intent, retrieve, branch to the right
    generator (QUESTION → _generate_answer, RECOMMEND/COMPARE → comparison_service),
    persist, and return.

    Returns (session, answer, reasoning, public_citations, confidence,
    follow_up_questions, recommended_documents, comparison_sections).

    Confidence is None for RECOMMEND and COMPARE with eligible evidence.
    An empty result uses the same refusal for all intents.
    recommended_documents and comparison_sections are [] unless the
    corresponding intent was detected.
    """
    collection_service.get_owned_collection(db, user_id, collection_id)

    if session_id is not None:
        session = get_owned_session(db, user_id, session_id)
        if session.collection_id != collection_id:
            raise SessionCollectionMismatchError()
        # Stored assistant answers lack reliable document provenance.
        recent_user_messages = chat_repository.list_recent_user_messages(db, session.id)
    else:
        title = message_text[:60]
        session = chat_repository.create_session(db, user_id, collection_id, title=title)
        recent_user_messages = []

    chat_repository.create_message(db, session.id, role=MessageRole.user, content=message_text)

    # ── Intent classification ───────────────────────────────────────────
    intent = classify_intent(message_text, recent_user_messages)

    # ── Retrieval ─────────────────────────────────────────────────
    # RECOMMEND and COMPARE use a broader top_k so every document has a
    # realistic chance of appearing in the result set before per-document
    # capping (comparison_service._cap_per_document) redistributes coverage.
    retrieval_top_k = settings.TOP_K if intent == IntentType.QUESTION else 30

    retrieval_start = time.time()
    retrieved = retrieval_service.retrieve(collection_id, message_text, db, top_k=retrieval_top_k)
    retrieval_time_ms = int((time.time() - retrieval_start) * 1000)

    # Full chunk_text kept internal — only used to build LLM prompts.
    # Unfiltered — kept as-is for query_logs so debugging retrieval quality
    # later sees everything that came back, not just what passed.
    citations = retrieval_service.build_citations(retrieved)

    # ── Single-document guard for RECOMMEND / COMPARE ────────────────
    if citations and intent in (IntentType.RECOMMEND, IntentType.COMPARE):
        all_docs = document_repository.list_by_collection(db, collection_id)
        ready_doc_names = [
            d.filename for d in all_docs if d.status == DocumentStatus.ready
        ]
        if len(ready_doc_names) < 2:
            guard_msg = (
                _SINGLE_DOC_RECOMMEND_MSG
                if intent == IntentType.RECOMMEND
                else _SINGLE_DOC_COMPARE_MSG
            )
            asst = chat_repository.create_message(
                db, session.id, role=MessageRole.assistant, content=guard_msg, citations=[]
            )
            query_log_repository.create(
                db,
                message_id=asst.id,
                retrieved_chunk_ids=[],
                similarity_scores=[],
                top_k=retrieval_top_k,
                response_time_ms=retrieval_time_ms,
                llm_model=settings.LLM_MODEL,
            )
            return session, guard_msg, "", [], None, [], [], []
    else:
        ready_doc_names = []  # not needed for QUESTION intent

    # ── Intent-specific generation ─────────────────────────────────
    llm_start = time.time()
    model_used = ""

    if citations and intent == IntentType.RECOMMEND:
        summary, recommended_documents, follow_up_questions = comparison_service.recommend(
            question=message_text,
            citations=citations,
            recent_user_messages=recent_user_messages,
            available_document_names=ready_doc_names,
        )
        answer = summary or "Here are my document recommendations based on your request."
        reasoning = ""
        confidence = None
        comparison_sections: list = []
        public_citations: list = []
        model_used = settings.LLM_MODEL  # best-effort log; comparison_service handles fallback

    elif citations and intent == IntentType.COMPARE:
        comp_message, comparison_sections, follow_up_questions = comparison_service.compare(
            question=message_text,
            citations=citations,
            recent_user_messages=recent_user_messages,
            all_document_names=ready_doc_names,
        )
        answer = comp_message or "Here is the document comparison."
        reasoning = ""
        confidence = None
        recommended_documents: list = []
        public_citations = []
        model_used = settings.LLM_MODEL

    else:  # Questions and the shared no-evidence response
        filtered_citations = retrieval_service.filter_citations(
            citations, settings.MIN_CITATION_SCORE
        )
        if not filtered_citations:
            answer, reasoning = _no_confidence_answer()
            confidence = "none"
            follow_up_questions = _RECOVERY_QUESTIONS[:]
        else:
            answer, reasoning, follow_up_questions, model_used = _generate_answer(
                message_text, filtered_citations, recent_user_messages
            )
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
        # Excerpted version is what actually leaves the server — API response
        # and what gets persisted in Message.citations. Built from the
        # filtered list — a citation too weak to inform the answer shouldn't
        # be shown as if it did.
        public_citations = retrieval_service.to_public(
            filtered_citations if filtered_citations else [], message_text
        )
        recommended_documents = []
        comparison_sections = []

    llm_time_ms = int((time.time() - llm_start) * 1000)

    rec_docs_data = [d.model_dump() for d in recommended_documents] if recommended_documents else None
    comp_secs_data = [s.model_dump() for s in comparison_sections] if comparison_sections else None

    assistant_message = chat_repository.create_message(
        db,
        session.id,
        role=MessageRole.assistant,
        content=answer,
        citations=public_citations,
        recommended_documents=rec_docs_data,
        comparison_sections=comp_secs_data,
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
        top_k=retrieval_top_k,
        response_time_ms=retrieval_time_ms + llm_time_ms,
        llm_model=model_used or settings.LLM_MODEL,  # log which model actually answered
    )

    return (
        session,
        answer,
        reasoning,
        public_citations,
        confidence,
        follow_up_questions,
        recommended_documents,
        comparison_sections,
    )
