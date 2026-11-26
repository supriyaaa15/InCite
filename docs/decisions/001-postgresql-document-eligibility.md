# ADR 001 — PostgreSQL document eligibility

**Status** — Implemented. [ADR 002](002-reliable-deletion-lifecycle.md)
extends eligibility to deletion-pending documents and collections. The
original cleanup proposal below predates that implementation. The vector
default described here is historical. See the [architecture guide](../architecture.md)
for the current default.

## Context

PostgreSQL owns each document's collection and ingestion status. Chroma stores searchable copies of chunks. Ingestion can write vectors before the document is ready, and the existing delete route can remove a SQL record while leaving its vectors. The in-memory BM25 index can also retain candidate IDs from an earlier state. All three chat intents share the same retrieval service.

## Decision

Chat authorizes the collection against the authenticated user before search. The retrieval service collects document IDs from the bounded vector and BM25 candidate pools, then runs one PostgreSQL query for matching `ready` records in that collection. It uses the filenames returned by this query for citations and prompt context, including for legacy chunks that lack filenames. The current model has `processing`, `ready`, and `failed` states.

Vector search is the configured default and uses one search for up to four times the requested result count. The optional hybrid modes use independent vector and BM25 pools, retaining the original ranks for RRF over their union. The service fetches eligible BM25-only chunks from Chroma and checks their metadata against the candidate's chunk ID. It filters candidates before the final cutoff and optional reranker, and retains the original score conversions. Missing SQL results or an eligibility query error produce no candidates. A failed query triggers a rollback. When chat persistence is available, the shared empty-evidence response requires no Groq call.

## Alternatives considered

- **Chroma status or filename checks.** These use copies that can lag the authoritative record and filenames are ambiguous across documents.
- **Filter only at prompt construction.** This would allow stale chunks to use ranking and reranker slots, and would require each consumer to remember to apply the rule.
- **Load every ready document or check each hit separately.** The candidate-scoped batch bounds SQL work to the documents that can affect the response.

## Consequences

The batch adds one SQL read when candidate IDs exist, while replacing citation-side filename resolution. Vector search can return a larger candidate payload, and hybrid can hydrate more BM25-only hits to recover from missing entries. Search can still return fewer than requested when stale candidates fill the bounded pool. PostgreSQL eligibility is a snapshot. A concurrent deletion after the check can race generation. Phase 1B will address cleanup and coordination across PostgreSQL, Chroma, the filesystem, ingestion workers, and BM25 caches. Historical messages and citations follow their existing retention behavior.

## Historical prompt context

Prior assistant replies may quote or paraphrase unavailable documents. Stored citation JSON identifies display excerpts by filename and page, without stable source IDs. Query logs link retrieved chunk IDs to the message, but log candidates rather than verified sources for each claim. Assistant text may also incorporate earlier conversation, so checking whether those logged IDs are still ready would provide an incomplete guarantee.

**Decision.** Read the last three user messages from the authorized session before saving the current turn. QA, comparison, and recommendation use those messages only to interpret the request. All three ground document claims in freshly retrieved eligible evidence. The full stored transcript remains available in the UI.

**Alternatives.** Reusing an assistant reply after verifying its sources would require reliable claim provenance and a policy for legacy messages. A model-generated summary could repeat the same unsupported or unavailable claims. A deterministic rewrite may help retrieval later, but it is unnecessary for applying the history boundary.

**Limits.** The app can resolve references present in recent user messages. It asks for clarification when comparison targets can only be inferred from an old assistant reply. The current question still drives retrieval, so short follow-ups may need an explicit topic. User-supplied text remains user context even when it repeats document text. Stored history retention and races after a fresh eligibility read remain separate concerns.
