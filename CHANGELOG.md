# Changelog

## Unreleased

### Deletion lifecycle foundation

- Document and collection deletion now records a durable request in PostgreSQL and returns `202 Accepted` with `cleanup_pending`. Pending targets disappear from active views and retrieval. Archived collection chats remain available through owner-only history endpoints.
- Upload registration and ingestion's SQL writes recheck deletion state under PostgreSQL row locks. Physical cleanup, scheduling, and restart recovery remain pending.

### Changed

- Added independent vector and BM25 candidate pools, RRF over their union, and Chroma hydration for BM25-only hits. Hybrid retrieval and CrossEncoder reranking are optional. Vector search remains the configured default.
- Switched chat generation from Gemini to Groq via `ChatGroq`, with a configurable model fallback chain.
- The chunk benchmark replaces the document-level benchmark and requires an explicit labeled JSON file via `--questions`.

### Removed

- Retired the original standalone Gemini and remote-Chroma RAG smoke script. The Phase 1A suite exercises the current application paths with disposable stores and fake model responses.

### Tests

- Added an opt-in Phase 1A suite that checks document eligibility and chat history against disposable PostgreSQL 16 and embedded Chroma.

### Fixed

- Phase 1A restricts new chat evidence to ready documents in the authorized collection. Vector search, hybrid search, and reranking exclude chunks whose documents fail the PostgreSQL check. QA, comparison, and recommendation reuse the existing insufficient-context response when nothing eligible remains.
- QA, comparison, and recommendation now use up to three earlier user messages to resolve follow-ups. Stored assistant replies stay in the visible transcript and are excluded from model prompts because their sources cannot be verified reliably.

See [the engineering log](docs/engineering-log.md) and [ADR 001](docs/decisions/001-postgresql-document-eligibility.md).
