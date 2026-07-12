# Design decisions log

Entries added as decisions are made, not reconstructed later.

---

## [Day 1-2] Postgres + Chroma, not Postgres + pgvector
**Decision:** separate Postgres (relational) and Chroma (vectors) instead of pgvector.
**Why:** pgvector consolidates to one database, which is the more "correct" production
choice — but it adds setup risk (extension config, Docker image, driver quirks) that
isn't worth it on a 30-day timeline for a first RAG project. Chroma has simpler setup
and better documentation for learning the fundamentals fast.
**Tradeoff acknowledged:** two data stores instead of one. Phase 2: migrate to pgvector.

## [Day 1-2] Storage abstraction (storage_service.py)
**Decision:** wrap file storage behind save_file/get_file/delete_file instead of
calling the filesystem directly from routes/services.
**Why:** local disk today, S3 later — migration touches one file, not every caller.

## [Day 1-2] Config-driven RAG tuning
**Decision:** CHUNK_SIZE, CHUNK_OVERLAP, TOP_K, EMBEDDING_MODEL all live in
core/config.py, not hardcoded in services.
**Why:** retrieval-strategy experiments (e.g. top_k=3 vs 5 vs 10) become one-line
changes, and query_logs.top_k records which value produced which answer — so past
experiments stay comparable after the fact.

## [Day 1-2] Health endpoints (/health, /health/db, /health/chroma)
**Decision:** three separate health checks instead of one generic one.
**Why:** when deployment breaks, a single /health that just says "ok" doesn't tell
you WHAT broke. Splitting by dependency (app itself / Postgres / Chroma) turns a
deploy debugging session from guesswork into a 10-second check.

## [Day 1-2] Google Gemini over Anthropic Claude for the LLM call
**Decision:** GOOGLE_API_KEY / Gemini for answer generation, not Anthropic.
**Why:** [fill in your actual reason — e.g. free-tier quota generous enough for
dev/demo usage, or existing familiarity with the Gemini API]
**Tradeoff acknowledged:** switching providers later means updating the LLM
call inside chat_service and the prompt formatting — isolated to one service
if that ever changes, since retrieval and generation are separate concerns.

---

## [Day 3-4] Fixed-size chunking with overlap
Decision: word-count-based chunks (CHUNK_SIZE=500, CHUNK_OVERLAP=50).
Why: simple to implement and reason about; overlap preserves context
across chunk boundaries.
Observed limitation: pages with leftover text just over CHUNK_SIZE
produce a small trailing chunk that's mostly duplicate content (e.g. a
501-word page yields chunk 0 with 500 words, chunk 1 with ~51 words,
50 of which repeat chunk 0's tail). Phase 2: merge undersized trailing
chunks into the previous chunk instead of keeping them separate.

## [Day 5-6] Manual RAG pipeline validated end-to-end

Decision: built the full pipeline (embed -> store in Chroma -> retrieve ->
generate) as a standalone script (scripts/test_rag.py) before touching FastAPI.
Why: confirms retrieval quality and generation correctness in isolation,
with nothing else to blame if something looks wrong later inside the API.
Result: tested against "Mastering Bitcoin" PDF, question "What is a Hash
Time Lock Contract?" — top 5 retrieved chunks were all genuinely on-topic
(pages 184, 351, 187, 358, 188, similarity 0.52 down to 0.46, sensible
descending order). Generated answer correctly described both HTLC clauses
(redemption via secret+hash, refund via timelock), named the actual opcode
(CHECKLOCKTIMEVERIFY) and script structure — traceable to the retrieved
chunks, not generic model knowledge. Confirms chunking + embeddings +
retrieval + grounded generation all work correctly together.

## [Day 5-6] google-generativeai -> google-genai SDK migration

Decision: switched from google-generativeai (import as
google.generativeai) to google-genai (import as from google import genai), using client.models.generate_content().
Why: google-generativeai is Google's legacy SDK, deprecated November
30, 2025. google-genai is the current, actively maintained SDK. It also
exposes a newer stateful client.interactions.create() API for multi-turn/
agentic use — not used here since this script only needs single-turn,
stateless calls (no conversation history to manage yet).
Also changed: default LLM_MODEL from gemini-2.0-flash (deprecated
March 2026) to gemini-2.5-flash.

## [Day 5-6] Observed: LLM call latency

Observation: the Gemini API call itself took ~11.6s for a single answer,
noticeably slower than the retrieval step (112ms). Not fixed now — noted for
later. Once query_logs (Day 17) is storing response_time_ms per request,
this becomes something to track systematically rather than eyeball once.
Possible causes to investigate later: context size (5 chunks in the prompt),
model choice (flash vs a faster/lighter variant), network latency to the API.

## [Day 7-8] JWT auth working end-to-end

Decision: layered auth flow — auth_routes.py (HTTP) -> auth_service.py
(business rules) -> user_repository.py (DB queries) -> models/user.py.
get_current_user in core/deps.py is the single shared dependency every
future protected route will use.
Result: verified via Swagger UI — POST /auth/register creates a user
with a bcrypt-hashed password, POST /auth/login returns a valid JWT,
GET /auth/me (protected) correctly resolves the token back into the
authenticated user's id/email/created_at. Full chain confirmed working.
Security decision: login returns the same error for "no such user" and
"wrong password" — revealing which one it was allows attackers to enumerate
valid registered emails.

## [Day 7-8] Fixed: passlib + bcrypt version incompatibility

Problem: POST /auth/register failed with ValueError: password cannot be longer than 72 bytes even for short passwords. Root cause: passlib
1.7.4's internal self-test (detect_wrap_bug) is incompatible with bcrypt
4.1+'s changed API — a known compatibility issue between the two libraries,
not a bug in application code.
Fix: pinned bcrypt==4.0.1 explicitly in requirements.txt alongside
passlib[bcrypt]==1.7.4.

## [Day 7-8] Docker build speed: pip cache mount

Problem: every requirements.txt change forced a full reinstall of all
dependencies from scratch (including large packages like torch and
chromadb), since Docker invalidates the whole layer on any change to that
file and pip had no memory of previous downloads.
Fix: changed the Dockerfile's pip install to use a BuildKit cache
mount (--mount=type=cache,target=/root/.cache/pip), so downloaded
packages persist across builds even when the layer itself is invalidated.
Also: learned to only use docker compose up --build when
requirements.txt or the Dockerfile change — plain Python code edits are
picked up live via the mounted volume + uvicorn --reload, no rebuild needed.

## [Day 9-11] Collections + document upload working end-to-end

Decision: promoted the Day 3-4/5-6 script logic (chunking, embedding)
into real app code — app/utils/pdf_processing.py is now the single source
of truth, imported by both ingestion_service.py (production) and the test
scripts (manual sanity checks). No more duplicated logic between "test"
and "real" code paths.
Result: verified via Swagger — created a collection, uploaded a 5-page
PDF, response returned immediately with status="processing", polled
GET /documents/{id} until it flipped to status="ready" with page_count=5.
Confirmed directly in Postgres (SELECT against chunks table) that all 5
chunks were stored with correct page_number/chunk_index and real extracted
content — not just that the API reported success.

## [Day 9-11] One Chroma collection per InCite collection

Decision: each Collection gets its own Chroma collection, named
collection_{collection_id}, rather than one shared Chroma collection
for the whole app.
Why: this is what actually enforces multi-tenant isolation at the
storage level. A chat inside "Resume" physically cannot retrieve chunks
from "College Notes" — they live in different Chroma collections, not
just filtered apart after a shared query. Filtering after the fact is one
missed WHERE clause away from a data leak between users; separate storage
isn't.

## [Day 9-11] Ownership checks return the same 404 either way

Decision: get_owned_collection() (and the equivalent document check)
raises the same "not found" error whether a collection/document doesn't
exist at all, or exists but belongs to a different user.
Why: distinguishing "doesn't exist" from "exists but isn't yours"
would let a user probe IDs to discover what other users have — same
reasoning as the login error from Day 7-8.

## [Day 9-11] Upload returns immediately; ingestion runs as a background task

Decision: POST /collections/{id}/documents saves the file and creates
a Document row (status=processing) synchronously, then hands off chunking/
embedding/storage to a FastAPI BackgroundTask that runs after the response
is already sent.
Why: chunking + embedding a large PDF can take real time (tens of
seconds) — making the client wait for all of that inside one HTTP request
is bad UX and risks timeouts. The frontend instead polls GET /documents/{id}
until status flips to ready or failed.
Known gap (Phase 2): deleting a document removes it from Postgres but
does not yet remove the corresponding vectors from Chroma — orphaned
vectors accumulate until this is addressed.