"""
comparison_service.py

Handles document recommendation and cross-document comparison.

Does NOT call retrieval_service.retrieve() — retrieval is send_message's
responsibility.  This service receives pre-retrieved citations and focuses on:

  - grouping chunks by document (_group_by_document)
  - per-document chunk capping for fair coverage (_cap_per_document)
  - resolving which documents to compare (_resolve_target_documents)
  - building a structured LLM prompt per intent
  - making exactly one LLM call per request (_llm_structured_call)

Public API
----------
recommend(question, citations, recent_user_messages, available_document_names)
  → tuple[str, list[RecommendedDocument], list[str]]

compare(question, citations, recent_user_messages, all_document_names)
  → tuple[str, list[ComparisonSection], list[str]]

Both functions return ("", [], []) on any failure — never raises, so chat
responses are never blocked by recommendation/comparison errors.
"""

import logging

from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.llm import get_llm, is_quota_error
from app.schemas.chat import ComparisonSection, RecommendedDocument

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal LLM output schemas
# ---------------------------------------------------------------------------

class _DocumentRanking(BaseModel):
    """Internal schema — not exposed in the API. Translated to RecommendedDocument."""

    document_name: str = Field(
        description="Exact filename as provided in the context above."
    )
    rank: int = Field(
        description="1 = best match for the user's goal. Must be a unique integer per document."
    )
    suitability_label: str = Field(
        description=(
            "A short label that PRECISELY reflects the user's stated goal. "
            "Do NOT use a fixed label set — infer from the request. "
            "Examples: 'Best examples', 'Most theoretical', 'Best interview prep', "
            "'Best for quick revision', 'Most comprehensive', 'Best for depth', "
            "'Most practical', 'Best beginner guide'."
        )
    )
    reason: str = Field(
        description=(
            "1-2 sentences grounded ONLY in the document's retrieved content "
            "explaining why this document matches the user's goal."
        )
    )


class _RecommendationResult(BaseModel):
    summary: str = Field(
        description=(
            "A natural, conversational introductory sentence addressing the user directly "
            "(e.g. 'For a 15-minute quick revision, I recommend:' or 'Based on your goal of preparing for interviews:'). "
            "NEVER use third-person phrasing like 'The user needs...' or 'The user is looking for...'."
        )
    )
    rankings: list[_DocumentRanking] = Field(
        description="Ranked documents, best first. Maximum 3 entries."
    )
    follow_up_questions: list[str] = Field(
        default_factory=list,
        description=(
            "Up to 3 follow-up questions to help the user explore further. "
            "Ground every question in the retrieved content."
        ),
    )


class _ComparisonResult(BaseModel):
    # ComparisonSection (from app.schemas.chat) is reused directly as the
    # LLM output type — it has the same two fields the LLM needs to fill.
    sections: list[ComparisonSection] = Field(
        description=(
            "Exactly 5 sections in this order: Summary, Common Concepts, "
            "Key Differences, Strengths of Each Document, Recommendation."
        )
    )
    sources: list[str] = Field(
        description="The exact filenames of the documents being compared."
    )
    follow_up_questions: list[str] = Field(
        default_factory=list,
        description="Up to 3 follow-up questions grounded in the comparison.",
    )


# ---------------------------------------------------------------------------
# Shared private helpers
# ---------------------------------------------------------------------------

def _group_by_document(citations: list[dict]) -> dict[str, list[dict]]:
    """
    Groups citation dicts by document_name, preserving within-group order.
    Used by both recommend() and compare() to avoid duplicating this logic.
    """
    grouped: dict[str, list[dict]] = {}
    for c in citations:
        grouped.setdefault(c["document_name"], []).append(c)
    return grouped


def _cap_per_document(grouped: dict[str, list[dict]], n: int = 5) -> dict[str, list[dict]]:
    """
    Keeps the top N chunks per document by similarity score.

    Without this cap, a single dominant document can occupy the majority of
    the retrieved set (e.g. 25 of 30 slots) when retrieval is global, leaving
    other documents with almost no representation in the LLM prompt.  Capping
    after grouping guarantees every document gets equal context regardless of
    how cosine scores happen to distribute.

    Only used for RECOMMEND (where all documents compete in one retrieval
    call).  COMPARE filters to target documents instead.
    """
    return {
        doc_name: sorted(chunks, key=lambda c: c["score"], reverse=True)[:n]
        for doc_name, chunks in grouped.items()
    }


def _resolve_target_documents(
    message: str,
    recent_user_messages: list[str],
    grouped: dict[str, list[dict]],
) -> list[str] | None:
    """
    Returns the list of document names to compare, or None if they cannot
    be resolved with confidence.  The caller is responsible for asking the
    user for clarification when None is returned — this function never guesses.

    Priority
    --------
    1. Explicit names in the message: look for filename substrings
       (case-insensitive, with and without extension).
    2. Implicit reference ("the first two", "both", "them"): look for names
       in recent user messages, starting with the newest.
    3. Unresolvable → return None.
    """
    document_names = [(name, name.lower(), name.lower().rsplit(".", 1)[0]) for name in grouped]

    def mentioned_documents(text: str) -> list[str]:
        text = text.lower()
        return [name for name, filename, stem in document_names if filename in text or stem in text]

    # 1. Explicit — filename substring present in the message
    named = mentioned_documents(message)
    if len(named) >= 2:
        return named

    # 2. Implicit — use an earlier user message that names the documents.
    _IMPLICIT_MARKERS = frozenset(
        ["first", "second", "third", "both", "them", "those", "these", "the two"]
    )
    msg_lower = message.lower()
    if any(marker in msg_lower for marker in _IMPLICIT_MARKERS):
        for user_message in reversed(recent_user_messages):
            mentioned = mentioned_documents(user_message)
            if len(mentioned) >= 2:
                return mentioned[:2]

    # 3. Cannot determine with confidence
    return None


def _llm_structured_call(prompt: str, schema: type):
    """Advance to the next model on a quota error or a missing response tool."""
    models = [settings.LLM_MODEL] + [
        m.strip() for m in settings.LLM_FALLBACK_MODELS.split(",") if m.strip()
    ]
    last_exc: Exception | None = None
    for model_name in models:
        try:
            llm = get_llm(model_name)
            raw = llm.generate_structured(prompt, schema)

            if raw is None:
                logger.warning(
                    "Model %s returned no response tool for %s. Trying the next model.",
                    model_name, schema.__name__
                )
                last_exc = ValueError(
                    f"{model_name} returned None for {schema.__name__} "
                    "(structured output parse failure)"
                )
                continue  # try the next model

            return raw

        except Exception as e:
            last_exc = e
            if is_quota_error(e):
                logger.warning("Model %s hit quota — trying next model", model_name)
                continue
            raise  # non-quota errors propagate immediately

    raise last_exc or RuntimeError("All configured LLM models are unavailable")


def _build_history_block(recent_user_messages: list[str]) -> str:
    """Format prior user requests separately from retrieved evidence."""
    if not recent_user_messages:
        return ""
    turns = "\n".join(f"User: {message}" for message in recent_user_messages)
    return f"Earlier user messages:\n{turns}\n\n"


def _validate_followups(raw: list[str]) -> list[str]:
    """Strip, deduplicate, and cap follow-up questions. Mirrors chat_service."""
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


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def recommend(
    question: str,
    citations: list[dict],
    recent_user_messages: list[str],
    available_document_names: list[str],
) -> tuple[str, list[RecommendedDocument], list[str]]:
    """
    Generates ranked document recommendations tailored to the user's goal.

    Parameters
    ----------
    citations
        Pre-retrieved citations from retrieval_service (top_k=30).  Capping
        to N chunks per document happens here before the LLM call.
    available_document_names
        All ready documents in the collection.  Used to provide context to
        the LLM even if some documents returned no high-scoring chunks.
    recent_user_messages
        Earlier user requests for resolving preferences or references.

    Returns (summary_message, ranked_documents, follow_up_questions).
    Returns ("", [], []) on any exception — never raises.
    """
    try:
        grouped = _group_by_document(citations)
        capped = _cap_per_document(grouped, n=5)

        doc_blocks = []
        for doc_name, chunks in capped.items():
            chunk_text = "\n".join(
                f"  [Page {c['page_number']}]: {c['chunk_text'][:300]}"
                for c in chunks
            )
            doc_blocks.append(f"Document: {doc_name}\n{chunk_text}")
        context = "\n\n---\n\n".join(doc_blocks)

        history_block = _build_history_block(recent_user_messages)

        prompt = f"""{history_block}The user is asking for a document recommendation.

User request: "{question}"

Use earlier user messages only to understand the request and resolve references.
Below are excerpts from each document in this collection. Use ONLY this \
content to evaluate and rank the documents.

{context}

Task:
1. Infer the user's specific goal from their request.
2. Provide a summary sentence that is natural, conversational, and addresses the user directly (e.g. 'For a 15-minute quick revision, I recommend:' or 'Based on your goal of preparing for interviews:'). NEVER use third-person phrasing like 'The user needs...' or 'The user is looking for...'.
3. For each document, assign a suitability_label that PRECISELY reflects the user's goal — do NOT use a fixed label set.

   Examples of how labels should adapt to the user's goal:
   - User asks "I want examples"   → "Best examples", "Most theoretical"
   - User asks "interview prep"    → "Best interview prep", "Best quick revision"
   - User asks "detailed"          → "Most comprehensive", "Best for depth"
   - User asks "beginner"          → "Best beginner guide", "Most accessible"

4. Write 1-2 sentences explaining why each document matches (or doesn't match) the user's goal, grounded strictly in the retrieved content above.
5. Rank documents best-first. Return at most 3. If fewer are relevant, return fewer."""

        result: _RecommendationResult = _llm_structured_call(prompt, _RecommendationResult)

        # Defensive guard — _llm_structured_call now raises on None, but
        # kept here as a belt-and-suspenders safety net.
        print(f"[comparison_service] recommend result: {result}")
        print(f"[comparison_service] recommend result type: {type(result)}")
        if result is None:
            logger.error(
                "recommend(): _llm_structured_call returned None — "
                "this should not happen after the None-guard fix. "
                "Returning empty recommendation."
            )
            return "", [], []

        public_rankings = [
            RecommendedDocument(
                document_name=r.document_name,
                rank=r.rank,
                suitability_label=r.suitability_label,
                reason=r.reason,
            )
            for r in result.rankings[:3]
        ]

        return result.summary, public_rankings, _validate_followups(result.follow_up_questions)

    except Exception:
        logger.exception(
            "recommend(): generation failed. "
            "Check the print output above for the raw result and its type."
        )
        return "", [], []


def compare(
    question: str,
    citations: list[dict],
    recent_user_messages: list[str],
    all_document_names: list[str],
) -> tuple[str, list[ComparisonSection], list[str]]:
    """
    Generates a structured comparison between two documents.

    Returns (message, comparison_sections, follow_up_questions).
    message is a plain-text summary always shown in the chat bubble.
    comparison_sections is the structured breakdown shown as formatted cards.

    When target documents cannot be resolved from current or prior user messages,
    returns a clarification request with an empty sections list.
    Returns ("", [], []) on any other exception — never raises.
    """
    try:
        grouped = _group_by_document(citations)

        # Resolve which documents the user wants to compare
        target_docs = _resolve_target_documents(question, recent_user_messages, grouped)

        if target_docs is None:
            # Cannot determine with confidence — ask for clarification
            doc_list = ", ".join(all_document_names) or "none found"
            clarification = (
                f"Which documents would you like me to compare? "
                f"Available in this collection: {doc_list}."
            )
            return clarification, [], []

        # Filter grouped to only target documents and cap chunks
        target_grouped = {
            doc: sorted(grouped[doc], key=lambda c: c["score"], reverse=True)[:5]
            for doc in target_docs
            if doc in grouped and grouped[doc]
        }

        if len(target_grouped) < 2:
            return (
                "I couldn't find enough retrieved content for both documents to generate "
                "a meaningful comparison. Try phrasing your question to mention the "
                "topic you'd like compared.",
                [],
                [],
            )

        # Build per-document context blocks
        doc_blocks = []
        for doc_name in target_docs:
            if doc_name not in target_grouped:
                continue
            chunk_text = "\n".join(
                f"  [Page {c['page_number']}]: {c['chunk_text'][:400]}"
                for c in target_grouped[doc_name]
            )
            doc_blocks.append(f"Document: {doc_name}\n{chunk_text}")
        context = "\n\n---\n\n".join(doc_blocks)

        history_block = _build_history_block(recent_user_messages)
        doc_labels = " and ".join(target_docs)

        prompt = f"""{history_block}The user wants a comparison between documents.

User request: "{question}"

Use earlier user messages only to understand the request and resolve references.
Documents being compared:
{context}

Generate a structured comparison with EXACTLY these 5 sections in this order:
1. Summary — what each document covers on this topic
2. Common Concepts — ideas and explanations both documents share
3. Key Differences — how the explanations differ in depth, style, and examples
4. Strengths of Each Document — what each does better than the other
5. Recommendation — which document to read first and why, considering the user's context

Requirements:
- Each section must be a single prose paragraph
- Ground every claim strictly in the retrieved content above
- Do not introduce information not present in the excerpts
- The sources list must contain exactly: {list(target_docs)}"""

        result: _ComparisonResult = _llm_structured_call(prompt, _ComparisonResult)

        message = f"Here is a comparison of {doc_labels}."
        return message, result.sections, _validate_followups(result.follow_up_questions)

    except Exception:
        logger.exception("Comparison generation failed — returning empty result")
        return "", [], []
