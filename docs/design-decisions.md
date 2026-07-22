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

## [Day 20] Collections page: shared Layout component introduced

Decision: built components/Layout.jsx (header with brand + user email +
logout) now, wrapping CollectionsPage, rather than putting header markup
directly in each page.
Why: every remaining screen (document upload, chat, history) needed
the same chrome. Same principle as the backend's layered architecture —
one place owns "what does every authenticated page look like," so adding
a 6th screen later doesn't mean copy-pasting header JSX again.
Also: collection cards navigate via onClick + useNavigate rather than
plain <a> tags, since this is an SPA — a real page navigation would
trigger a full reload and lose the in-memory auth state unnecessarily.

[Day 21] Multipart upload required extending the API client's request()

Problem: the existing request() helper always set
Content-Type: application/json — fine for every prior endpoint, but wrong
for file upload, where the browser must set its own Content-Type (with
the multipart boundary) automatically.
Fix: request() now accepts an optional formData param; when present,
it's passed directly as the fetch body with no manual headers set,
instead of JSON.stringify-ing a body object.

## [Day 21] Polling design: per-document intervals, refresh-resilient, cleaned up on unmount

Decision: pollIntervals (a useRef) tracks one setInterval per
document id being polled, not a single global poller.
Why: supports uploading multiple files without their polling
interfering with each other, and each poll stops itself the moment a
document leaves "processing" status — no wasted requests once there's
nothing left to wait for.
Also handled: resuming polling for any document still "processing"
on page load (so a refresh mid-ingestion doesn't leave the UI frozen on
a stale status), and clearing every active interval in the useEffect
cleanup function (prevents state updates firing on an unmounted
component, and the memory leak that would otherwise cause).

## [Day 22] Chat UI: optimistic messages with rollback on failure

Decision: the user's message appears in the message list immediately
on submit, before the API call resolves — not after.
Why: the LLM round trip is the slowest part of this app by far
(observed 4-28s in Day 17's query_logs data); waiting for it before
showing what the user just typed would make the UI feel unresponsive.
Safety net: if the request fails, the optimistic message is rolled
back (removed from state) rather than left in place looking like it was
successfully sent and silently unanswered.
Also: reasoning is rendered as a distinct, understated line (small
italic) below the answer, separate from the citation chips — wasn't
strictly required by the Day 22 scope, but the data was already available
from the API (built Day 12-13), so surfacing it cost nothing and previews
the Retrieval Transparency feature planned for Days 28-29.

## [Day 23] Backend gap found while building history: ChatSessionResponse was missing collection_id

Problem: GET /sessions returns every session across ALL of a user's
collections (by design — chat_repository.list_sessions_by_user has no
collection filter). The frontend needs to show only "this collection's"
history in the chat sidebar, which requires filtering client-side by
collection_id — but ChatSessionResponse didn't expose that field at all.
Fix: added collection_id to ChatSessionResponse. No migration needed
— the data already existed on the ChatSession model, it just wasn't
being serialized in the response.
Known, deliberate gap: resumed conversations show citations but not
reasoning, because reasoning is only ever returned live at generation
time — it's never persisted to the messages table (only content and
citations are). Fixing this would need a new column + migration; not
worth it for a field that's supplementary context, not the answer itself.
Also: "new chat" is just local state reset (sessionId -> null,
messages -> []) — no API call needed, since a new session isn't actually
created server-side until the first message is sent to it.

## [Day 24] Bug fix: chat wasn't history-aware, answered off-topic on follow-ups

Problem: found via actual testing (Day 22-23) — asking "why is it used"
as a follow-up to "what is arrays" returned information about arrays,
structures, AND classes, instead of staying focused on arrays.
Root cause: _generate_answer() never received any prior conversation
turns — only the current question and whatever got retrieved for it
alone. With no history, the model had no way to resolve "it" to "arrays"
specifically, so a vague follow-up query retrieved (and then described)
everything broadly related.
Fix: send_message() now fetches prior messages via
chat_repository.list_messages_by_session() BEFORE saving the current
user message — guaranteeing history never includes the question being
answered. The prompt gained a "Conversation so far" block (capped at the
last ~3 exchanges, bounding prompt size) plus explicit instructions to
resolve pronouns/references against that history and to focus only on
the concept actually being asked about, not list every retrieved concept.
Verified: same two-question sequence re-tested — second answer
correctly stayed on-topic (multi-dimensional arrays specifically),
reasoning cited one relevant page instead of several unrelated ones.
Observation carried forward to Days 28-29: this test also showed
citations with very weak/negative scores (0.08 down to -0.01) still
displayed alongside a correctly-focused answer — confirms citation
filtering needs to happen at the display layer, not by changing how
generation already works (generation was already correctly ignoring the
weak matches; the UI just wasn't).

## [Day 24] Bug-hunt pass: four bugs found and fixed

Two found by deliberately re-reading the code with an adversarial eye
(not just "does the happy path work"), two found by actually using the
product and noticing something looked wrong.

Bug 1 — expired/invalid tokens left the user stuck (found via review).
Every page calls api/client.js's request(), but a 401 response just threw
a generic error shown in whatever error-banner happened to be on screen —
nothing logged the user out or returned them to /login. Fixed once, in
request() itself: on 401, clear the stored token and hard-redirect to
/login. Fixed at the one shared choke point rather than needing the same
check copy-pasted into every page.

Bug 2 — race condition switching chat sessions mid-request (found via
review, confirmed real by testing). Nothing stopped clicking a
different session (or "+ New chat") while a message was still in flight.
The still-pending response would resolve after the switch and silently
attach itself to whatever conversation was now being viewed, reverting
sessionId unexpectedly. Fixed by guarding loadSession() and
startNewChat() behind the same sending flag already used to disable
the input, with a visible disabled state on the sidebar buttons.

Bug 3 — LLM introduced LaTeX notation the frontend can't render (found
via real testing on a math-heavy document). Asking about SVD returned
"Σ\Sigma
Σ" and "VTV^T
VT" as literal text — even though the retrieved
source chunk itself contained a plain, correctly-extracted unicode Σ
character. The model was adding LaTeX formatting on its own initiative
during generation, a common default habit for math-aware LLMs, and
nothing in the frontend renders LaTeX. Fixed with a one-line prompt
instruction: describe math in plain text/unicode, no LaTeX syntax.
Cheaper and more consistent with the rest of the UI than adding a LaTeX
rendering library for what's a narrow use case.

Bug 4 — Markdown formatting showed as literal asterisks (found in the
same test as Bug 3). The model naturally produces Markdown (bold,
bullet lists) in longer answers, but chat-message-content was a plain

<p> tag with no Markdown parsing — "**High Variance:**" rendered as
literal asterisks instead of bold text. Fixed by adding react-markdown
and rendering assistant messages through it (user-typed messages
deliberately stay plain text — no reason to Markdown-parse what someone
just typed, and it avoids surprising reformatting of a user's own
literal asterisks/underscores).
Pattern worth noting: Bugs 3 and 4 are opposite fixes for a similar
symptom (model output containing special syntax the UI didn't handle) —
one suppressed the syntax at the source (prompt instruction), the other
rendered it properly at the destination (Markdown parser). Worth
thinking about which lever to pull case by case: suppress at generation
when the syntax adds no value to the reader (LaTeX with no renderer),
render properly when it does (Markdown structure genuinely aids
readability).

## [Day 24] Found: LLM inconsistently follows the "admit when you don't know" instruction

Observation: asked the identical question ("what is curse of
dimensionality") in two different collections, both with weak retrieval
(no score above 0.23, no retrieved chunk actually defining the term).
Collection "new" correctly refused ("the provided context does not
contain information about..."). Collection "new2" instead generated a
full, plausible-sounding definition — almost certainly from the model's
general training knowledge, not the retrieved context, despite the
prompt explicitly instructing it to say so honestly when context is
insufficient.
Why this happens: LLMs sample probabilistically; instruction-
following is a strong tendency, not a guarantee, even with explicit,
well-written prompt instructions. The same weak-grounding scenario can
go either way run to run. This is a structural property of how these
models generate text, not something more/better prompt wording can fully
eliminate.
Decision: do not chase this with more prompt engineering. The real
fix is deterministic and code-level, not linguistic — check retrieval
scores BEFORE calling the LLM at all; if all scores fall below a
threshold, return the "not enough information" message directly from
Python, without ever asking the model to make that judgment call. This
is precisely the planned Confidence-aware RAG feature (Days 28-29) —
this observation is now the concrete, reproducible motivating evidence
for it, not a hypothetical use case.
Also observed: the two collections had different document sets
("new" had 3 mixed-topic PDFs, "new2" appeared to have only
viva_questions_v2.pdf), which affected which specific chunks got
retrieved — but the core finding (inconsistent honesty about weak
grounding) held regardless of exactly which chunks came back in each
case.

## [Day 25] Bug: deleting a collection crashed with a NOT NULL constraint violation

Problem: DELETE /collections/{id} (first time ever actually exercised
end-to-end — built Day 9-13, only verified by code review until now)
threw sqlalchemy.exc.IntegrityError: null value in column "message_id"
of relation "query_logs" violates not-null constraint.
Root cause: two separate cascade mechanisms exist and look similar
but aren't: the database-level ON DELETE CASCADE (set via
ForeignKey(ondelete="CASCADE")) only fires if a raw DELETE reaches
Postgres directly. But db.delete(some_object) in SQLAlchemy's ORM walks
the Python object graph first — Collection -> ChatSession -> Message —
and for any relationship without an explicit ORM-level cascade
(cascade="all, delete-orphan"), its default behavior is to try to
DISASSOCIATE the child (UPDATE ... SET foreign_key = NULL) rather than
delete it. Message.query_log had no such cascade set, so the ORM tried
to null out query_logs.message_id before deleting the message — which
correctly failed, since that column is (deliberately) NOT NULL.
Fix: added cascade="all, delete-orphan" to Message.query_log,
matching the pattern already used correctly on every other parent-owns-
children relationship in the schema.
Verification step taken: grepped every relationship() definition
across all models to confirm this was an isolated gap, not a systemic
pattern — every other cascade (Collection.documents, Collection.
chat_sessions, Document.chunks, User.collections, User.chat_sessions,
ChatSession.messages) already had cascade="all, delete-orphan" set
correctly. Worth doing this audit whenever one instance of a pattern-bug
is found — checking whether it's isolated or systemic is cheap and
prevents finding the same bug five more times later.
Lesson: DB-level and ORM-level cascade are not the same guarantee.
Setting ondelete="CASCADE" on a ForeignKey column is necessary but not
sufficient when deletes go through db.delete() on a loaded object graph
— the ORM-level relationship() cascade needs to be configured too, or
SQLAlchemy's default "try to disassociate, not delete" behavior applies.

## [Day 26] Deployment architecture: embedded Chroma (PersistentClient), not a separate service

Problem: Render (chosen for setup speed) requires any service that
receives private network traffic to be on a paid tier — free services
can send private requests but not receive them, confirmed directly
against Render's own docs. Since Chroma's OSS version has no built-in
authentication, running it as a public service was ruled out on security
grounds, and running it as a private service meant it needed to receive
from the FastAPI backend — which forced a paid tier (~$7/month minimum)
purely for Chroma.

Options considered, in order:


Separate paid private Chroma service on Render (~$7/mo) — lowest
code risk, deploys exactly what was already tested locally. Real cost,
zero migration.
AWS (EC2/RDS/S3) — ruled out. As of a 2025 policy change, new AWS
accounts get $100-200 in credits with a 6-month auto-close clock, not
the old 12-months-free model. Requires a credit card, has well-
documented surprise-billing traps (NAT gateways, idle Elastic IPs,
EBS on stopped instances), and solves nothing Render doesn't already
solve — more risk for no clear benefit at this stage.
Managed vector DB (Qdrant Cloud) — genuinely free forever (1GB,
no card, confirmed via multiple sources as a permanent tier, unlike
Pinecone's more limited free tier or Weaviate Cloud's 14-day-trial-
then-$25/mo). Real architectural upside: sidesteps private networking
entirely, since it's a public API reached by key, same pattern as the
Gemini calls already in use. Real cost: requires migrating BOTH
retrieval_service.py (to langchain-qdrant) AND ingestion_service.py
(which uses the raw chromadb client directly, not LangChain) — meaning
re-verifying the entire upload -> chunk -> embed -> store -> retrieve
-> generate -> cite pipeline again, days before the deadline.
Embedded Chroma (PersistentClient) — chosen. Before committing,
precisely audited every file touching Chroma to confirm the real
scope: chroma_client.py, config.py, and health_routes.py needed
changes (3 files); retrieval_service.py and ingestion_service.py
needed ZERO changes, verified directly (grepped both for
HttpClient/CHROMA_HOST/CHROMA_PORT references: zero matches in
either) rather than assumed. This is possible specifically because
chromadb.Collection's upsert()/query() methods are client-agnostic —
they don't know or care whether HttpClient or PersistentClient
created them.


Why embedded won: lowest risk, genuinely free (single Render
service, no paid tier needed), and the actual code change was smaller
than initially estimated once precisely scoped — 5 files, all mechanical
config/client-construction changes, zero changes to tested retrieval or
ingestion logic.

Trade-off explicitly accepted: PersistentClient's data isn't
guaranteed to survive a redeploy unless CHROMA_PERSIST_PATH is backed by
a Render persistent disk (availability on the free tier unconfirmed at
decision time). Accepted for a portfolio project — documents can be
re-uploaded after a redeploy. Explicitly NOT acceptable for a real
production system with real user data; documented here specifically so
this isn't mistaken for a universally-good pattern rather than a
context-specific trade-off.

Also fixed while touching this code: app/api/health_routes.py's
/health/chroma endpoint was constructing its own separate HttpClient
instance instead of reusing the shared get_chroma_client() singleton
that every other part of the app went through — a pre-existing
inconsistency, found only because this migration required touching
every file that referenced the old client type directly.

## [Day 26] Embedded Chroma migration verified end-to-end

Verification: after wiping the old (incompatible, server-format)
local chroma_data volume, ran the full pipeline fresh: GET /health/chroma
returned {"status": "ok", "chroma": "connected"} confirming
PersistentClient initialized correctly; uploaded a new PDF and confirmed
it reached status="ready"; sent a chat question and got a correctly
grounded answer with accurate reasoning and relevant citations
(scores 0.21 down to 0.03, sensible descending order, same pattern as
every prior test throughout the project).
Confirms: the "zero changes to retrieval_service.py and
ingestion_service.py" claim held under actual testing, not just static
code review — both files' Chroma-touching code paths (upsert during
ingestion, similarity_search_with_score during retrieval) worked
correctly against PersistentClient with no modification, exactly as
predicted from chromadb's client-agnostic Collection API.
One real hiccup during this migration, unrelated to Chroma itself:
after removing the chroma service from docker-compose.yml, docker compose down failed with "Network incite_default: Resource is still in
use" — the old chroma container had become an orphan (no longer
defined in the compose file, but still running and holding the network
open). Fixed with docker compose down --remove-orphans, which
specifically targets containers left over from a removed service
definition. Worth remembering: removing a service from docker-compose.yml
doesn't automatically clean up its already-running container.

## [Day 27] Deployment bugs: OOM on Render free tier, then Neon connection drops

Bug 1 — deploy failed with "Ran out of memory (used over 512MB)":
first deploy attempt crashed before the app even finished starting.
Root cause: on Linux, PyPI's default torch wheel (a sentence-transformers
dependency) bundles full CUDA/cuDNN support even though Render's
instances have no GPU — confirmed by log lines showing onnxruntime
uselessly probing for GPU devices at startup. That bundled CUDA weight
alone was enough to exceed the free tier's 512MB cap. Windows/Mac get
CPU-only torch by default from PyPI; Linux doesn't.
Fix: pinned torch==2.4.1+cpu via --extra-index-url https://download.pytorch.org/whl/cpu in requirements.txt. Verified fix
by watching the next deploy succeed ("Your service is live"). Benefits
local builds too (smaller image, faster installs) — no reason not to use
the CPU build everywhere given this app never needs GPU acceleration.
Also fixed while touching this file: requirements.txt had drifted
from the corrected, verified-compatible pins settled during the Day
14-15 LangChain migration (chromadb, langchain-chroma,
langchain-google-genai had reverted to the old open-ended ranges that
originally caused that conflict) — resynced to the known-working set.

Bug 2 — /health/db returned "SSL connection has been closed
unexpectedly" intermittently: never seen locally, only after
deploying against Neon (serverless Postgres). Root cause: Neon can
close idle connections server-side as part of how it scales down —
SQLAlchemy's default connection pool doesn't know that happened and
tries to reuse what it thinks is a still-valid cached connection.
Fix: added pool_pre_ping=True (tests each connection before handing
it out, transparently reconnects if dead) and pool_recycle=300 (proactively
recycles connections every 5 minutes, under any idle-timeout window a
managed provider might enforce) to the engine in core/database.py.
Lesson: several of this project's real bugs only surfaced once real
infrastructure (Render's actual memory limits, Neon's actual connection
behavior) replaced local Docker's much more forgiving defaults — a
reminder that "works locally" and "works in the actual deployment
environment" are genuinely different claims, not the same claim tested
twice.

## [Day 27] Deployed live — Vercel + Render + Neon, full pipeline verified

Stack: Vercel (frontend), Render free web service (FastAPI + embedded
Chroma), Neon (Postgres, free, no fixed expiry — chosen over Render's own
Postgres specifically because it auto-pauses on inactivity instead of
hard-deleting after a fixed 30-90 day window).

Bug: 404 on any direct navigation to a client-side route.
React Router routes (/login, /collections/{id}, etc.) only exist
client-side — Vercel's static host returned a real 404 for any URL that
wasn't the root, since no matching file exists there until the React app
itself loads and takes over routing. Fixed with a vercel.json rewrite
rule sending every non-asset request to index.html, letting React Router
handle the actual path once the JS loads. Classic, well-known SPA
deployment gotcha — would have hit every refresh, bookmark, and shared
link to a specific collection/chat, not just login.

Non-issue, cost real debugging time anyway: local WiFi network blocking
Vercel's IPs specifically. ping succeeded, TCP:443 failed — isolated
via Test-NetConnection and a mobile-data test to confirm it was the local
router/ISP, not the deployment (Vercel's own dashboard showed the
deployment as healthy and "Ready" throughout). Worth the reminder: when
something fails identically across every browser AND the platform's own
dashboard shows it healthy, the problem is probably local network, not
the code — check that early next time rather than deep in browser
DevTools first.

Verified end-to-end on live infrastructure (not just health checks):
register, login, create collection, upload a document through to
status=ready, send a chat message and get a grounded answer with correct
citations and reasoning — the full pipeline, on Render's free tier, Neon,
and Vercel, working together.

## [Day 27] Found: free-tier spin-down (not just redeploys) wipes embedded Chroma data

Symptom: mid-conversation, a question about content that had been
successfully retrieved just one message earlier ("gradient descent",
visible in an earlier citation chip) suddenly returned zero citations
and the "nothing relevant found" fallback. Coincided with two 502
responses on the chat endpoint (one 9.96s, one 74ms) around the same
time.
Root cause: the accepted trade-off documented earlier ("embedded
Chroma data isn't guaranteed to survive a redeploy") was understood too
narrowly — it also applies to Render's automatic free-tier spin-down
after ~15 minutes of inactivity, which restarts the container fresh.
That's a far more frequent event than a code-triggered redeploy — it can
happen several times a day, including mid-demo during any pause. On
restart, CHROMA_PERSIST_PATH starts empty (no persistent disk was ever
attached — only env vars were configured when the Render service was
created), so every previously uploaded document's vectors vanish, even
though Postgres (Neon) still correctly shows the document as
status=ready with the right page count — the two data stores silently
disagree after a spin-down.
Fix: set up a free UptimeRobot monitor pinging /health every 5
minutes, keeping the container active so the 15-minute idle threshold
never triggers. Confirmed as a legitimate, extremely common pattern for
Render's free tier via multiple independent sources — Render's own
position is that a paid tier is the "clean" solution, but an external
keep-alive ping is a normal, widely-used workaround, not against any
terms.
Honest limitation: this stops inactivity-based spin-downs, not
genuine crashes or Render-side maintenance restarts — a real persistent
disk remains the fully robust fix if zero risk is needed right before an
actual interview.
Lesson: re-read an accepted trade-off's actual scope carefully —
"lost on redeploy" and "lost on this specific free-tier platform
behavior that happens far more often than redeploys" are different
claims, and the gap between them caused real confusion mid-testing.

## [Day 27] Deployment stability confirmed: HEAD fix + keep-alive holding together

Verification: after deploying the HEAD-support fix and confirming
UptimeRobot showed no new incidents for 15+ minutes, re-uploaded a fresh
test document and re-ran the exact query that failed before ("what is
array"). Clean result: grounded answer, correct reasoning citing the
right pages, citations properly ordered by score (0.65 -> 0.53 -> 0.36).
Closes out the spin-down data-loss issue from earlier — the
combination of (1) a real persistent-disk-free architecture decision
(embedded Chroma, accepted trade-off) with (2) a keep-alive ping
preventing the free-tier inactivity spin-down that would otherwise
trigger data loss, is now a stable, working setup for continued
testing and demos.
Operational note for future self: any code push triggers a Render
redeploy, which still wipes embedded Chroma data (this is a genuinely
different trigger than the inactivity spin-down the keep-alive fixes —
keep-alive only prevents idle-triggered restarts, not deploy-triggered
ones). Re-upload test documents after every deploy going forward, not
just periodically.

## [Day 29] Retuned MIN_CITATION_SCORE from 0.15 to 0.05 based on a real false negative

Problem found through testing: the original threshold (0.15) was
chosen from limited prior evidence and turned out too aggressive —
re-testing the exact "curse of dimensionality" scenario from Day 24
showed the correct, relevant chunk (score ~0.13) was being filtered out
before it ever reached the LLM. The system then answered from only
loosely related chunks or reported insufficient information, even though
the right content had actually been retrieved.
Fix: lowered MIN_CITATION_SCORE to 0.05. Confirmed this still
reliably excludes clearly-irrelevant chunks (the earlier observed
negative-score struct-definition chunk for an arrays question, for
example) while now correctly admitting genuinely relevant but
weaker-scored content.
Why this doesn't undermine the two-threshold design: the system
still does its job correctly at 0.05 — the curse-of-dimensionality
answer now comes back correct AND still shows the "low confidence"
badge, since 0.13 is well below LOW_CONFIDENCE_THRESHOLD (0.35). This is
the intended behavior: genuinely relevant-but-weak content gets used,
with the uncertainty honestly surfaced, rather than either hidden
entirely or presented with false confidence.
Honest limitation: a fixed cosine similarity threshold's "meaningful"
range shifts depending on how homogeneous or diverse a collection's
content is — 0.05 is tuned against this project's specific test
documents, not a universal constant. A more robust version would
calibrate thresholds per-collection or use a learned/relative measure
instead of an absolute cutoff — noted as a genuine Phase 3+ improvement,
not something to chase further under the current timeline.