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

## Retrieval and chat

Vector search in embedded Chroma is the configured default. Retrieval uses the
cached MiniLM model and native `Collection.query()` against the existing
per-collection cosine index. It returns ordered LangChain Core `Document`
and raw distance pairs. Optional hybrid retrieval searches independent vector
and BM25 pools, combines their original ranks with RRF, and fetches eligible
BM25-only chunks from Chroma. The optional `hybrid_rerank` mode applies a
CrossEncoder to eligible fused candidates. See
[ADR 004](decisions/004-native-chroma-vector-retrieval.md).

Chat authorizes the collection and session before retrieval. PostgreSQL verifies
which candidate documents are ready in that collection and supplies citation
filenames. Empty eligible evidence produces the existing refusal without a
provider call. Prompts use fresh evidence and up to three earlier user messages
for request context. The authorized transcript still includes assistant replies.
See [ADR 001](decisions/001-postgresql-document-eligibility.md).

## Deletion lifecycle

PostgreSQL stores deletion intent separately from document ingestion status.
Active views and retrieval exclude pending documents and collections. The API
acknowledges the recorded request and retains history. A FastAPI lifespan
worker discovers due work at startup and periodically thereafter. It removes
Chroma vectors and verified PDFs before finalizing SQL. Retry state lives in
PostgreSQL. The supported storage layout uses one application process owning
the embedded Chroma store and a per-collection OS lock on its local volume.
See [ADR 002](decisions/002-reliable-deletion-lifecycle.md).

## External services
- The LLM boundary in `app/core/llm.py` wraps the official Groq SDK in a cached, synchronous provider. It sends each prompt as one user message, requests plain text or a named function tool, and validates tool arguments with the existing Pydantic schemas. QA and comparison services own fallback order. See [ADR 005](decisions/005-native-groq-provider.md).

## Deployment
Single Render web service runs the FastAPI app, with Chroma embedded inside it (PersistentClient) rather than as a separate service — avoids Render's requirement that any service receiving private network traffic be on a paid tier. See design-decisions.md for the full reasoning and alternatives considered (paid private Chroma service, Qdrant Cloud, AWS).

- Backend + Postgres: Render
- Frontend: Vercel
- Run one FastAPI process and one replica per embedded Chroma store. The
  backend image explicitly passes `--workers 1`, including when
  `WEB_CONCURRENCY` is present. Keep that setting when overriding the command.
- Stop old writers and back up PostgreSQL, Chroma, and uploads before running
  `alembic upgrade head` against the intended database. Confirm the revision
  before starting the new process. Then check app, PostgreSQL, and Chroma
  readiness and pending cleanup. See the [deployment steps](../README.md#deployment-and-upgrades).
- The Docker build excludes local configuration and runtime artifacts. Supply
  credentials at runtime and keep backups and custom storage outside the build
  context.
- Chosen over AWS EC2 for setup speed within a 30-day timeline — see
  design-decisions.md for the full reasoning.
  Known trade-off: Chroma's embedded data isn't guaranteed to survive a redeploy unless the persist path is backed by a persistent disk. Accepted for this project — documents can be re-uploaded after a redeploy; not acceptable for a real production system with real users.

## Historical extensibility notes

These original design notes describe where proposed features would fit. The
sections above describe the current retrieval and chat behavior.

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

