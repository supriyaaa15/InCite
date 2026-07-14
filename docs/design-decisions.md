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


## [Day 12-13, revisited] Extracted retrieval_service.py from chat_service.py

**Problem:** the original architecture plan called for retrieval and chat
orchestration to be separate services, but the initial chat implementation
put retrieval logic (embedding the query, calling Chroma, building
citations) as private functions directly inside `chat_service.py` — drift
from the plan.

**Why it mattered:** a formal Phase 2-4 roadmap was defined (confidence
scoring, retrieval transparency, follow-up suggestions, cross-document
comparison, document intelligence reports, OCR, hybrid search, reranking).
Hybrid Search and Reranking are both changes to *how retrieval works* —
with retrieval logic embedded inside `chat_service.py`, those features
would mean editing conversation-orchestration code to change retrieval
behavior, coupling two concerns that should be independent.

**Fix:** extracted `retrieve()` and `build_citations()` into
`services/retrieval_service.py`. `chat_service.py` now only orchestrates
(sessions, messages, calling retrieval, calling generation, logging) —
it has no knowledge of how retrieval actually works internally. Hybrid
search / reranking later only touch `retrieval_service.py`.

**Deliberately NOT done now:** did not pre-add database columns for
Phase 3's "Document Intelligence Report" (reading_level, topics,
ocr_status, etc.) — that's speculative schema before the logic exists to
populate it. Alembic migrations are already fast and low-risk, so the
extensibility there is in having clean tooling, not pre-built empty
columns. See `docs/architecture.md` "Extensibility" section for the full
mapping of each planned phase to where it will plug in.

## [Day 13, planning] Days 28-29 feature swapped: Compare Documents -> Confidence-aware RAG + Retrieval Transparency

Decision: replaced the original Days 28-29 stretch feature (Compare
Documents) with Confidence-aware RAG + Retrieval Transparency (both from
the Phase 2 roadmap).
Why: both are backend-cheap — similarity scores and retrieved-chunk
detail already exist in every chat response as of Day 12-13. They reuse
the existing chat UI (Day 22) rather than needing a new screen. They're
also a better differentiator: "chat with your PDF, with citations" is
common in RAG tutorials; a system that visibly tells you when it isn't
confident, and shows its retrieval reasoning, is a less common design
choice and a stronger technical talking point.
Compare Documents and the rest of Phase 3/4 (cross-document
comparison, document intelligence reports, OCR, hybrid search, reranking)
remain explicitly out of scope for the 30-day placement deadline — tracked
as post-placement future work, not abandoned scope creep.

## [Day 13, revisited] Reasoning-capable prompt, structured reasoning field, trimmed excerpts

Problem 1 — prompt was too literal: the original prompt ("answer using
ONLY the context, if it doesn't contain the answer say so") caused the
model to refuse questions like "why can't arrays contain different data
types?" even when the context clearly supported an inferred answer (arrays
store one type -> therefore can't hold different types). It was matching
for an exact sentence instead of reasoning over what was stated.
Fix: rewrote the prompt to explicitly allow reasoning and synthesis
across the retrieved context, while still prohibiting facts not supported
by it. Still fully grounded — just no longer requires a literal sentence
match.

Problem 2 — no visibility into why an answer was generated: citations
showed sources, but nothing summarized which pages actually drove the
answer.
Fix: added a reasoning field to ChatResponse. Generated via
structured JSON output from the LLM (response_mime_type: application/json


response_schema with answer and reasoning keys) — confirmed this is
supported by the current google-genai SDK before implementing. Structured
output isn't 100% reliable from any LLM, so there's a deterministic
fallback: if JSON parsing fails, the raw text becomes the answer and
reasoning is built from citation metadata directly (grouping pages by
document) rather than trusting the model's formatting. Reasoning is
dependable even when the model's compliance isn't.


Problem 3 — citations shipped full chunk text (800+ chars) to the API:
fine for debugging, unnecessarily large and unpolished for real use.
Fix: retrieval_service.build_citations() still returns full
chunk_text internally (needed to build the LLM prompt). A new
to_public() function trims it to a ~180-character excerpt, cut at a word
boundary, for anything that leaves the server — both the API response and
what gets persisted in Message.citations. Full chunk content remains
recoverable from the chunks table (via document_id/page_number) if ever
needed later — nothing is actually lost, just not shipped by default.

## [Day 13, revisited] Bug: orphaned function body during a file edit

Problem: AttributeError: module 'retrieval_service' has no attribute 'build_citations' — but only at request time, not at container startup,
which made it confusing initially.
Root cause: a prior edit inserted the new _excerpt()/to_public()
functions but accidentally deleted the line def build_citations(...):
itself, leaving its docstring and body as dead, unreachable code hanging
off the end of to_public(). Python treated this as a syntactically valid
(but useless) string literal followed by orphaned statements — no import-
time error, since there was no syntax error, just a missing function
definition.
Why it didn't fail at startup: Python doesn't check that every
function "looks complete" — it only errors when something tries to call a
name that doesn't exist. build_citations simply didn't exist as an
attribute on the module until something tried to call it mid-request.
Fix: restored the missing def build_citations(db, retrieved): line
so its body was reattached to a real function signature.
Lesson: after any edit that reorganizes multiple functions in one
file, verify with grep -n "^def " (or equivalent) that every expected
function name is actually present as a definition — not just "the file
compiles," since orphaned code like this doesn't cause a compile error.

## [Day 14-15] LangChain migration complete and verified

Decision: migrated retrieval (retrieval_service.py) and generation
(chat_service.py) to LangChain — langchain_chroma.Chroma wraps the same
per-collection Chroma collections ingestion already writes to;
ChatGoogleGenerativeAI.with_structured_output() replaces the hand-rolled
response_mime_type + json.loads + try/except from Day 12-13.
Result: verified via Swagger — same question style as Day 12-13,
correct grounded answer, reasoning correctly named the source pages,
citations correct. Confirms the migration didn't regress anything built
in Days 12-13.
Observation, not a bug: one retrieved chunk (page 4, a struct
definition) came back with a negative similarity score (-0.2752) for an
"what is array" query — correctly signaling irrelevance, but still
returned because TOP_K always returns exactly k results regardless of
whether they're all actually relevant. Concrete real example motivating
Phase 2's Confidence-aware RAG feature — worth citing specifically rather
than describing the feature hypothetically.

## [Day 14-15] Dependency resolution saga: three real conflicts, in order

Conflict 1 — langchain-chroma vs pinned chromadb==0.5.15: initial
attempt used langchain-chroma>=0.1.4 (open range) which let pip try newer
versions (up to 1.1.0) requiring chromadb>=1.0.9+ — incompatible with the
old pin. Fix attempt: bumped chromadb client to >=1.3.5 — but this then
required also bumping the Chroma server image in docker-compose.yml to
match (client/server versions must be pinned together, always — a
protocol-level requirement, not just a Python dependency one).
Conflict 2 — client/server mismatch, in both directions: first hit
when the client was upgraded to 1.3.5+ but the server docker-compose
image was still on an old pin. Later hit AGAIN in the opposite direction
when downgrading the client back to chromadb==0.5.15 (to satisfy
langchain-chroma==0.1.4's actual requirement, confirmed via its published
metadata: chromadb!=0.5.4,!=0.5.5,<0.6.0,>=0.4.0) without also reverting
the server image, which had been bumped to 1.3.5. Symptom both times:
ValueError: Could not connect to tenant default_tenant — Chroma's
tenant/database protocol isn't compatible across major client/server
version gaps. Fix: keep both pinned to the exact same version, always.
Conflict 3 — langchain-google-genai vs langchain==0.3.4:
langchain-google-genai's 4.x line requires langchain-core>=1.2.5,<2.0.0;
langchain==0.3.4/langchain-community==0.3.2 pull in the old 0.3.x
langchain-core line. These cannot coexist. Fix: downgraded to
langchain-google-genai==2.1.5, confirmed compatible with langchain-core
0.3.x via a real working combination reported by another developer.
Also removed the explicit method="json_schema" argument from
with_structured_output() since that parameter isn't confirmed to exist
at this older version — let the library use its own default instead.
Final pinned set: langchain==0.3.4, langchain-community==0.3.2,
langchain-chroma==0.1.4, langchain-google-genai==2.1.5, chromadb==0.5.15,
chroma server image chromadb/chroma:0.5.15 (matched to the client).
Lesson for interviews: "how did you handle a broken dependency
resolution" now has three genuinely different, specific answers instead
of one — an open version range letting a resolver wander into
incompatible territory, a protocol-level client/server pairing
requirement that isn't visible in any single package's metadata, and a
transitive shared-dependency conflict (langchain-core) between two
otherwise-unrelated packages.

## [Day 17] Query logging verified with real data

Verification: query_logs was built early (Day 12-13, alongside chat)
and assumed working since chat requests returned 200 OK with no errors —
but never directly confirmed with real rows, unlike chunks which was
checked directly after Day 9-11. Ran a SELECT against query_logs after
the Day 14-15 LangChain migration to close that gap properly.
Result: confirmed real rows — top_k=5 on every row (matches config),
num_chunks=5 (every retrieval actually returned the expected count),
correct llm_model, and response_time_ms populated.
Observation: response_time_ms varied widely across requests using the
identical model and top_k — from ~4.3s up to ~28.6s, nearly a 7x spread.
Not investigated/fixed now, but this is exactly the kind of pattern
query_logs exists to surface — worth revisiting if response time ever
becomes a focus (e.g. candidate causes to check later: cold-start on the
LLM API side, prompt/context size varying by question, network variance).

## [Day 19] Frontend login/register working end-to-end

Result: React (Vite) frontend built — login/register form, AuthContext
holding JWT + user state (localStorage, re-validated via /auth/me on
load), ProtectedRoute guard, design tokens (citation-themed palette:
archival green accent, serif headings, mono for metadata — deliberate
choice over generic SaaS blue, tied to the product's citation identity).
Verified: register through the UI created a real user in Postgres, login
issued a working JWT, refresh preserved the session, placeholder home
screen correctly showed the authenticated user's email.

## [Day 19] Debugging: ERR_CONNECTION_RESET was Docker Desktop's WSL2 port forwarding, not the app

Problem: frontend showed "Failed to fetch"; a direct browser request to
localhost:8000/health also failed with net::ERR_CONNECTION_RESET — even
though docker compose logs showed the app, Postgres, and Chroma all
starting cleanly with no errors.
Why this was confusing: every previous connection issue in this
project (crashed containers, missing imports, dependency conflicts) showed
up clearly in docker compose logs. This one didn't — logs were clean,
because the container itself was genuinely fine. The break was in
Windows/Docker Desktop's WSL2 port-forwarding layer between localhost and
the container's network namespace, which is invisible to container logs
entirely.
Diagnosis path: confirmed via DevTools Network tab that BOTH the
actual fetch request AND its CORS preflight (OPTIONS) failed identically
with a connection reset — ruling out a CORS policy issue (which would
show a proper CORS error, not a reset) and pointing at something breaking
the raw connection before any HTTP response could form.
Fix: fully quit and restart Docker Desktop (not just docker compose restart — the app-level containers were never the problem, so restarting
them changed nothing on earlier attempts).
Lesson: when a connection fails at a level below HTTP (resets,
refused, not a proper error response) and container logs are completely
clean, the problem often isn't in the app at all — it's in the host
platform's networking layer connecting to the containers. Worth checking
container logs first (rules out app bugs) but not stopping there if logs
show nothing wrong.