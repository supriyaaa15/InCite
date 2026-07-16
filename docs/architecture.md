# Architecture

## Layers
Request flow: **API routes** -> **services** (business logic) -> **repositories**
(DB access only) -> **models** (SQLAlchemy).

- **API layer** (`app/api/`): thin route handlers, no business logic. Parses
  requests, calls a service, returns the response.
- **Services** (`app/services/`): business logic lives here. auth_service,
  storage_service, ingestion_service, retrieval_service, chat_service.
- **Repositories** (`app/repositories/`): DB queries only, no business logic.
  Keeps SQLAlchemy specifics out of services — if the DB layer ever changes,
  only repositories change.
- **Models** (`app/models/`): SQLAlchemy ORM models, one file per table.
- **Core** (`app/core/`): config, security (JWT/hashing), database session.

## Data stores
- **PostgreSQL**: users, collections, documents, chunks (metadata), chat
  sessions, messages, query logs.
- **Chroma**: chunk embeddings (the actual vectors). Chunks in Postgres store
  a `chroma_id` pointing to the corresponding vector.

## External services
- **LLM API** (Google Gemini): called by chat_service to generate answers
  from retrieved chunks.

## Deployment
Single Render web service runs the FastAPI app, with Chroma embedded inside it (PersistentClient) rather than as a separate service — avoids Render's requirement that any service receiving private network traffic be on a paid tier. See design-decisions.md for the full reasoning and alternatives considered (paid private Chroma service, Qdrant Cloud, AWS).

- Backend + Postgres: Render
- Frontend: Vercel
- Chosen over AWS EC2 for setup speed within a 30-day timeline — see
  design-decisions.md for the full reasoning.
  Known trade-off: Chroma's embedded data isn't guaranteed to survive a redeploy unless the persist path is backed by a persistent disk. Accepted for this project — documents can be re-uploaded after a redeploy; not acceptable for a real production system with real users.

## Extensibility — where future phases plug in

The architecture was deliberately kept boring and layered specifically so
each planned future feature has an obvious, minimal-refactor home. None of
these are built yet — this section exists so implementing them later means
"add code in this one place," not "figure out where this belongs."

**Phase 2 — Confidence-aware RAG**: `retrieval_service.build_citations()`
already returns a `score` per chunk, and that data already reaches
`chat_service.send_message()`. Adding a confidence check is a conditional
around the existing `citations` list, plus one new config value
(e.g. `CONFIDENCE_THRESHOLD`) — no new data path required.

**Phase 2 — Retrieval Transparency**: the API already returns full
citation detail (document, page, chunk text, score) in `ChatResponse`.
This phase is almost entirely a frontend rendering task — the backend
already exposes everything needed.

**Phase 2 — Follow-up Question Suggestions**: purely additive. A new
function alongside `_generate_answer()` in `chat_service.py`, and one new
optional field on `ChatResponse`. Doesn't touch retrieval or existing
fields, so it can't break anything already working.

**Phase 3 — Cross-Document Comparison**: every chunk stored in Chroma
already carries `document_id` in its metadata (set during ingestion).
Chroma's query `where` filter can already scope retrieval to specific
document(s) within a collection — this phase needs a new
`comparison_service.py` and route, but no changes to storage or ingestion.

**Phase 3 — Document Intelligence Report**: deliberately NOT pre-adding
empty columns (reading_level, topics, ocr_status, etc.) to the documents
table now — that's speculative schema, and Alembic already makes adding
real columns a fast, low-risk migration when these fields actually have
logic behind them. The extensibility here is process (clean migration
tooling), not pre-built structure.

**Phase 4 — Hybrid Search / Reranking**: this is exactly why
`retrieval_service.py` was extracted from `chat_service.py` (see
design-decisions.md). Combining BM25 with vector search, or adding a
reranking step, both happen entirely inside `retrieval_service.retrieve()`
— every caller (chat, and later comparison) is unaffected.

**Phase 4 — OCR**: `app/utils/pdf_processing.py` is already isolated from
ingestion orchestration. An OCR fallback for pages with no extractable
text slots in there as a new function, called conditionally, without
restructuring `ingestion_service.py`. Known current gap: pages with no
extractable text are silently skipped rather than flagged — worth fixing
when OCR is actually built, tracked in design-decisions.md.

