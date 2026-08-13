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



## [Day 30] Confidence recalibration — fixed a real miscalibration bug



Problem found through testing: a citation with similarity ~0.6 (a strong match) was still showing a "Low Confidence" badge — the original single-threshold design (LOW_CONFIDENCE_THRESHOLD=0.35) was a coarse binary split that didn't map cleanly to the actual score distribution observed across real testing. Fix: replaced the single threshold with two boundaries — MEDIUM_CONFIDENCE_THRESHOLD (0.20) and HIGH_CONFIDENCE_THRESHOLD (0.45) — giving a proper three-tier confidence display (high/medium/low) plus the existing "none" (filtered out entirely) and a new "error" state (retrieval succeeded but every LLM failed — a service issue, not a retrieval-quality one, so it gets its own distinct badge rather than being folded into "low"). Thresholds remain empirically tuned against this project's actual test data, not a universal calibration — same honest limitation noted for MIN_CITATION_SCORE on Day 29.



## [Day 30] Scope check: upload limits, validation, and Document Health Report deferred



Decision: three more requested features (upload size/count limits, malformed-PDF validation, a "Document Health Report" showing pages/chunks/OCR-readiness) were evaluated and deliberately deferred with the actual deadline in view. Why: the Document Health Report is, functionally, the Phase 3 "Document Intelligence Report" already scoped and explicitly deferred in this same log and in README.md's Future Work section — building it now, last-minute, would have quietly undone that scoping discipline rather than extend it. Upload limits/validation are reasonable, bounded hardening, but with a demo video and final bug pass still outstanding on the actual last day, shipping a stable, well-documented, working project took priority over adding more surface area right before presenting it. Not abandoned — tracked explicitly as Phase 2 (upload limits/validation, genuinely small scope, good candidate for the very next session) and Phase 3 (Document Health Report, already covered) rather than silently dropped.



## ## [Day 31] Deployment migration: Render → Railway, PDF ingestion optimization, and parser replacement



**Problem:** two independent issues remained after the Day 30 feature freeze. First, Render's free tier introduced operational friction (cold starts, memory constraints, and repeated deployment-specific workarounds) that complicated demos despite the application itself being stable. Second, large PDF ingestion was still slower than desired: a 90-page document consistently required roughly two minutes to finish chunking, embedding, and indexing.



**Decision 1 — migrated deployment from Render to Railway.**



**Why:** Railway provides a more predictable environment for this project's backend without the deployment limitations repeatedly encountered on Render's free tier. The migration removed infrastructure-specific debugging from the critical path while leaving the application architecture unchanged. No retrieval, embedding, or API logic changed — only the hosting platform.



**Decision 2 — retuned the chunking strategy from the original conservative settings to CHUNK_SIZE=1000 and CHUNK_OVERLAP=150.**



**Why:** profiling suggested the dominant cost was not the embedding model itself but the number of chunks being produced. Every chunk requires text embedding, vector insertion into Chroma, metadata persistence, and indexing. Larger chunks reduced the total chunk count substantially while still preserving enough overlap for context continuity during retrieval.



**Result:** indexing time for a real 90-page PDF dropped from roughly **119 seconds to approximately 14 seconds** (~88% reduction) with no observed degradation in retrieval quality during testing. This eliminated the earlier temptation to switch to a cloud embedding API purely for performance, preserving the original privacy design where embeddings are generated locally and document contents are not sent to a third-party embedding service.



**Decision 3 — replaced pdfplumber with PyMuPDF for PDF text extraction.**



**Why:** PyMuPDF (fitz) provides significantly faster document parsing while maintaining high extraction quality for digitally-generated PDFs. Since PDF parsing is part of every ingestion request, reducing extraction time improves end-to-end indexing latency without changing any downstream RAG logic. The rest of the ingestion pipeline (chunking → embedding → Chroma storage) remained unchanged.



**Trade-off acknowledged:** increasing chunk size means each retrieved chunk contains more surrounding context, slightly increasing prompt size sent to the LLM. Given the large reduction in indexing time and no observed retrieval regression across the project's test documents, this was accepted as the better engineering trade-off for the current system.



---



## [Day 32] Upload pipeline hardening: validation, error codes, graceful failures, and frontend UX



**Problem:** the upload pipeline had grown organically without any formal validation layer. Several production-readiness gaps had accumulated:



1. Any file type was accepted — a user could upload a `.exe` renamed to `.pdf` and it would be saved to disk, a DB row created, and the background task would spin until fitz crashed.

2. Corrupted, password-protected, and image-only PDFs produced a generic `failed` status with no explanation stored anywhere — the frontend had no way to tell the user what went wrong.

3. The `documents` table had no columns to hold an error code or message, so even if the backend computed the reason, it had nowhere to persist it.

4. The ingestion service caught all exceptions with a bare `except Exception` block, logged nothing structured, and re-raised — stack traces appeared in the raw server log but no diagnostic information reached the database or the API response.

5. `MemoryError` was implicitly treated the same as any other exception, with no special handling.

6. Partial data accumulation: if ingestion crashed mid-way, already-written Chroma vectors and Postgres chunk rows were left orphaned, silently corrupting future retrieval results for that collection.

7. No configurable limits existed — a 2 GB, 5000-page PDF would be accepted and processed with no safeguard.

8. The frontend showed a plain `"failed"` badge with no message and no recovery action, leaving users with no information and no next step.



---



### Decision 1 — add configurable upload limits via the existing `Settings` infrastructure



**What changed:** added `MAX_UPLOAD_SIZE_MB` (default 20) and `MAX_PDF_PAGES` (default 800) to the `Settings` class in `app/core/config.py`, with a derived `max_upload_size_bytes` property to avoid scattering `* 1024 * 1024` conversions across callsites. Both values are overridable via environment variables, consistent with every other tunable in the system. Added corresponding entries to `.env.example` with inline comments.



**Why:** hardcoding limits inside route handlers or service functions was the obvious alternative, but it would have violated the project's existing convention (established from Day 1) that all configuration lives in `Settings` and is never hardcoded. Centralising the limits also means a single env var change takes effect everywhere without a code deploy.



**Why these defaults:** 20 MB covers the vast majority of academic PDFs used in RAG workflows. 800 pages is a generous ceiling — a typical textbook runs 300–600 pages. Both values are deliberately conservative for a free-tier deployment while still being easy to raise in production.



---



### Decision 2 — introduce a dedicated error vocabulary module (`app/utils/upload_errors.py`)



**What changed:** created a new module with two exports:



- `UploadErrorCode` — a `str` enum of ten machine-readable codes: `INVALID_FILE`, `INVALID_PDF`, `PASSWORD_PROTECTED`, `EMPTY_DOCUMENT`, `NO_EXTRACTABLE_TEXT`, `FILE_TOO_LARGE`, `PAGE_LIMIT_EXCEEDED`, `RESOURCE_LIMIT`, `PROCESSING_TIMEOUT`, `INTERNAL_ERROR`.

- `UploadValidationError(Exception)` — a custom exception that carries both an `error_code` and a `user_message`, with a centralised default message per code. Callers can optionally supply a custom message (e.g. to include the actual configured limit in the text).



**Why a dedicated module rather than inline strings:** the route handler and the ingestion service both need to raise and catch the same errors. Without a shared vocabulary the codes would have been string literals duplicated across two files, guaranteed to drift. A single import resolves that. The `_USER_MESSAGES` dictionary also acts as a single place to update copy if wording changes — the strings are UI surface, and keeping them out of business logic makes that easier.



**Why a custom exception class rather than returning error codes as values:** raising an exception means any failure path — however nested — automatically propagates to the correct handler without the caller having to inspect return values. It also separates the "what went wrong" (error code) from the "what to show" (user message) in a typed, self-documenting way.



---



### Decision 3 — two-phase PDF validation (`app/utils/pdf_validator.py`)



**What changed:** introduced a new module with two functions called in sequence by the route handler:



**Phase 1 — `validate_upload(file)`** runs *before* writing to disk:

- Enforces the size limit by seeking to the end of the upload stream and seeking back.

- Checks MIME type and file extension as *hints only* — not as the authoritative gate.



**Phase 2 — `validate_pdf_file(file_path)`** runs *after* saving to disk:

- Calls `fitz.open()` — this is the single authoritative decision on whether the file is a PDF. If fitz can't open it, it's not a PDF, regardless of what the `Content-Type` header or filename says.

- Checks `pdf.is_encrypted` before accessing pages (accessing page content on a locked document raises its own exception rather than returning useful data).

- Checks `pdf.page_count == 0` (empty document).

- Checks `pdf.page_count > MAX_PDF_PAGES` (page limit exceeded).

- Iterates pages, accumulating character counts, short-circuiting as soon as any text is found. Zero total characters → `NO_EXTRACTABLE_TEXT`.



**Why fitz rather than MIME/extension as the authoritative check:** a file's MIME type is set by the client and can be spoofed trivially. A file extension is just a naming convention. A user can rename `virus.exe` to `notes.pdf` or set `Content-Type: application/pdf` on any binary payload. `fitz.open()` actually parses the PDF byte structure — it cannot be fooled by metadata. MIME and extension are kept as a first-pass *hint* to give faster, friendlier feedback for accidental wrong-file selections before the cost of a disk write.



**Why both phases rather than one:** size validation must happen before saving to disk (no point writing a 500 MB file just to reject it). Structural PDF validation (`fitz.open()`) requires a file path — it cannot operate on an in-memory `SpooledTemporaryFile` reliably across all fitz versions. The two-phase split matches the physical reality of the operation.



**Why `NO_EXTRACTABLE_TEXT` is not treated as a hard rejection of an invalid file:** a scanned PDF is a perfectly valid PDF — it just has no machine-readable text. The pipeline cannot *process* it (no OCR is implemented), but that is a pipeline limitation, not a document defect. Telling the user "invalid PDF" would be a lie. The correct message is: "This PDF contains only scanned images. OCR is not currently supported." This is a product capability boundary, not a validation failure.



**Why validation runs synchronously in the route handler:** running validation in the background task was evaluated and rejected. The key reason is orphaned rows: if validation is deferred to the background, the route handler must create the `documents` DB row before knowing whether the file is valid. A background failure then requires the client to poll to discover rejection. Running validation synchronously (in the route handler, before `document_repository.create()`) means invalid files are rejected with an immediate HTTP 422 and *no DB row is ever created*. No polling, no orphaned rows, no "processing" that immediately becomes "failed". The cost — the user waits slightly longer before getting the 422 — is measured in milliseconds (fitz validation is fast: open file, check header, check page count, spot-check for text).



---



### Decision 4 — add `error_code` and `error_message` columns to the `documents` table



**What changed:** added two nullable columns — `error_code VARCHAR` and `error_message TEXT` — to the `documents` table via a new Alembic migration (`a1b2c3d4e5f6`). Updated the `Document` SQLAlchemy model, the `DocumentResponse` Pydantic schema (so both fields are returned in the API response), and the `document_repository.update_status()` function (to accept and persist both fields).



**Why nullable with no default:** existing rows remain valid with `NULL` in both columns, which the application treats as "no error recorded". The migration is additive and backward-safe — zero downtime, no data loss.



**Why store the message on the DB row rather than computing it at query time:** the error message can depend on runtime context (e.g. the actual page count vs. the configured limit) that isn't reconstructable later from just an error code. Storing the full message at failure time ensures the frontend always shows exactly the right text, even if the configuration changes between failure and the next API poll.



---



### Decision 5 — structured exception hierarchy in the ingestion service



**What changed:** replaced the bare `except Exception: ... raise` block with three explicit handlers:



1. `except UploadValidationError` — for secondary validation failures that surface during ingestion (e.g. a PDF that passes pre-flight but yields zero chunks). Uses the error's own `error_code` and `user_message` directly.

2. `except MemoryError` — maps to `RESOURCE_LIMIT` with a friendly message about resource constraints.

3. `except Exception` — catch-all for truly unexpected failures, maps to `INTERNAL_ERROR`. Logs the full stack trace via `logger.exception()` (server-side only).



**Why separate `MemoryError` from `Exception`:** in Python, `MemoryError` is a subclass of `BaseException`, not `Exception`, in some versions — but more importantly, it signals a qualitatively different failure: the worker ran out of heap memory, not a logic error or external service failure. Separating it lets the code give a specific, honest user message ("resource constraints") rather than the generic "unexpected error" message, and lets monitoring systems distinguish OOM events from bugs.



**Why not try to catch OOMKilled / SIGKILL:** infrastructure-level process kills (Kubernetes OOMKilled, Docker kill, `kill -9`, exit code 137) terminate the OS process before any Python exception handler executes. The Python runtime is simply gone — there is no `except` block that runs. Attempting to "handle" these with Python code is not possible. The only mitigations are infrastructure-level (resource limits, watchdog processes, external health checks). The code documents this clearly: "Infrastructure-level kills may terminate the worker before exception handling executes." Overpromising in the code (e.g. `except BaseException` with a comment "catches OOM kills") would be technically incorrect and misleading.



---



### Decision 6 — cleanup of partial data on ingestion failure



**What changed:** introduced `_cleanup_partial_data(db, document, store)`, called by the central `_mark_failed()` helper before writing the failed status. The function:



1. Queries Chroma for all vectors with `document_id` matching the failed document and deletes them.

2. Deletes all Postgres chunk rows for the document via `chunk_repository.delete_by_document()`.

3. Logs cleanup results at `INFO` level (Chroma) or catches and logs any cleanup exceptions, then continues — cleanup errors are never re-raised.



**Why cleanup is necessary:** ingestion processes documents in batches. A crash on batch 7 of 12 means batches 1–6 have already been written to both Chroma and Postgres. Without cleanup, these partial embeddings remain in the collection's Chroma store and will be retrieved by future queries, silently mixing valid content with an incomplete document's fragments. This corrupts retrieval quality for the whole collection without any visible signal.



**Why cleanup errors are suppressed rather than raised:** the primary obligation of the failure path is to mark the document as `failed` so the frontend can stop polling. If cleanup itself fails (e.g. Chroma is temporarily unavailable), raising that exception would prevent the `failed` status from being written, leaving the document permanently stuck in `processing`. Logging the cleanup failure and continuing is the correct trade-off: the operator is alerted, the user is not stuck.



---



### Decision 7 — structured failure logging fields



**What changed:** every failure log call (at `WARNING`, `ERROR`, or `EXCEPTION` level) now includes: `document_id`, `collection_id`, `filename`, `error_code`, and `elapsed_time`. Success logging (the timings block) remained as temporary `print()` statements to preserve the existing Docker terminal profiling workflow during active development.



**Why elapsed time in failure logs:** knowing *how long* a job ran before failing is diagnostically valuable. A job that fails in 0.03 seconds failed in pre-processing (e.g. file open). A job that fails after 45 seconds failed mid-embedding or mid-Chroma write. This immediately narrows the search space when reading logs.



**Why keep `print()` for the success timings block:** the ingestion pipeline is still under active profiling — embedding, chunking, Chroma upsert, and Postgres insert times are being measured in the Docker terminal. `print()` output appears immediately without log-level configuration. These are explicitly temporary debugging/profiling statements, not production logs. The decision was made to preserve them during the active development phase and convert to structured `logger.*` calls once profiling is complete. This was a deliberate product decision, not an oversight.



**Why stack traces never reach the API response or the database `error_message` column:** exposing stack traces to users is a security and UX problem simultaneously. Internally, a full stack trace contains file paths, variable names, and sometimes data values that should never leave the server. Externally, a raw Python traceback is meaningless to a user and creates distrust. The pattern used here — `logger.exception()` on the server, a static friendly string in `error_message` — is the standard production approach for this exact situation.



---



### Decision 8 — frontend failure UX



**What changed:**



- The `"failed"` status badge now shows alongside the backend's `error_message` displayed inline below the filename — e.g. *"This PDF is password-protected. Remove the password from the PDF and try uploading it again."*

- Failed document rows get a red left-border CSS accent (`border-left: 3px solid var(--error)`) to make them visually distinct at a glance.

- An **"Upload another file"** button appears directly on each failed row, allowing immediate retry without scrolling to the page header.

- The processing status badge now includes an animated CSS spinner, making it visually clear that the document is actively being processed rather than stalled.

- The upload-error banner (shown on immediate 422 rejection) includes an inline **"Try another file"** button instead of requiring the user to dismiss the error and manually click the upload button again.

- Polling already stopped correctly on `"failed"` status (the condition was `status !== "processing"`), so no change was needed there — the frontend never gets stuck indefinitely.



**Why show the backend message verbatim rather than map codes to frontend strings:** the backend already computes the correct, contextual message (e.g. including the actual page count vs. the limit). Duplicating that mapping in the frontend would mean maintaining two copies of the same strings. The `error_message` column was added precisely to be the canonical source of user-facing copy — the frontend just renders it.



**Why include a retry CTA on the failed row itself (not just the header):** in a list with multiple documents, the user's attention is on the failed row. Making them scroll up to click "Upload PDF" again adds unnecessary friction. A local retry button on the failed row removes that indirection.



---



## [Day 33] Context-aware follow-up question suggestions



**Problem:** after receiving an answer, users often don't know what to ask next. The chat interface was passive — it answered what was asked, but gave no guidance on what else the documents contained or how to explore further. This is a discovery problem: the documents may cover five interesting topics but the user only knows to ask about one.



---



### Decision 1 — follow-up generation is a separate function, not an extension of `AnswerWithReasoning`



**What changed:** added `_generate_followup_questions(question, answer, citations, confidence)` as a private function in `chat_service.py`. It is called by `send_message()` after the main answer has been generated and persisted, and it returns `list[str]`.



**The alternative considered — extend `AnswerWithReasoning`:**

The existing answer generation already uses `with_structured_output(AnswerWithReasoning)` (a LangChain structured output call). Adding `follow_up_questions: list[str]` to that Pydantic model would have put everything in one LLM call and zero extra latency.



**Why that was rejected:**



1. **Failure isolation.** The requirement says "never fail the chat request because follow-up generation failed." With a shared schema, any malformed output in the `follow_up_questions` field can cause `with_structured_output()` to fall back to a plain LLM call, which loses *both* the reasoning field and the follow-up questions simultaneously. With a separate call, a follow-up failure touches nothing — it's wrapped in `try/except` that returns `[]` and logs the exception.



2. **Single-responsibility.** `AnswerWithReasoning` is the contract between the answer generation prompt and the response parser. Adding follow-up questions to it couples two concerns that are otherwise independent: answering the question, and suggesting next questions. If follow-up generation is later removed, changed (e.g. server-side rendering vs. client-side), or replaced with a different mechanism, `AnswerWithReasoning` must change too.



3. **Prompt clarity.** One prompt with two jobs — generate an answer AND generate follow-up questions — is harder to tune. The answer prompt is carefully worded around grounding, mathematical notation, and historical reasoning about retrieval behaviour. Adding follow-up generation instructions to it would make the prompt longer and harder to reason about.



4. **Extensibility.** A separate function is easy to replace: mock it in tests, A/B test a different prompt, swap it for a rule-based generator, or run it asynchronously in the future — none of those changes touch answer generation.



**Why the extra latency is acceptable:** follow-up questions are displayed below the completed response. By the time they render, the user is already reading the answer. The additional call (typically 0.5–1.5 seconds) is not on the critical path of the answer. If it times out or fails, the user still has a complete answer; they just don't see suggestions.



---



### Decision 2 — plain text response, not structured output, for follow-up generation



**What changed:** `_generate_followup_questions` calls `_invoke_with_fallback(prompt)` directly, which returns plain text. The response is then parsed by splitting on newlines and stripping list prefixes (`1.`, `1)`, `-`, `•`, etc.).



**Why not `with_structured_output()`:** the structured output mechanism adds schema enforcement overhead and is primarily valuable when the output has multiple named fields with types. A list of three short strings doesn't benefit from that. A plain prompt instruction ("Return ONLY the 3 questions, numbered 1. 2. 3.") is reliable enough for this case, and plain-text parsing is trivially robust to minor formatting variations (numbered vs. bulleted vs. dash-prefixed lists).



---



### Decision 3 — confidence-aware generation with two distinct paths



**What changed:** `_generate_followup_questions` checks the `confidence` parameter before doing anything:



- `confidence == "none"` (nothing cleared the noise filter — the LLM was never called): returns `_RECOVERY_QUESTIONS`, a fixed list of three generic discovery questions ("What topics are covered?", "Summarize the uploaded documents.", "What are the main concepts?"). No LLM call.

- All other confidence levels: makes the full LLM call with the actual question, answer, and document excerpts.



**Why different paths:** when `confidence == "none"`, the retrieval system found no relevant content. Asking the LLM to generate "grounded" follow-up questions in this situation is contradictory — there is no grounding. The LLM would either hallucinate topics that don't exist in the documents, or produce generic questions identical to the recovery list anyway. The deterministic path is both faster and more honest.



**Why `_RECOVERY_QUESTIONS` are copy-of-slice (`[:]`):** the list is a module-level constant. Returning a copy prevents any caller from accidentally mutating the shared list (unlikely in practice, but correct by construction).



---



### Decision 4 — follow-up generation runs AFTER the assistant message is committed to the database



**What changed:** in `send_message()`, the call order is:



```

_generate_answer()          ← answer + reasoning + model_used

to_public()                 ← public citations

create_message(assistant)   ← DB commit

query_log_repository.create ← DB commit

_generate_followup_questions() ← AFTER both commits

return (..., follow_up_questions)

```



**Why after the commit:** if follow-up generation fails or raises an exception, the assistant message and query log are already safely written. There is no transaction to roll back. The follow-up questions are ephemeral — they are returned in the API response but not persisted to the database (they are regenerated on each new request). If they fail, the user receives the full answer with an empty `follow_up_questions` list.



**Why not persist follow-up questions:** the questions are context-specific to the moment they were generated (the specific answer, the specific retrieved chunks). Persisting them and re-displaying them when a session is resumed would show stale questions — the user's context has changed. The cleaner model is: suggestions are live-only, generated fresh with each response.



---



### Decision 5 — the prompt design



The follow-up prompt instructs the model to:

- Stay grounded in what the documents "actually contain" (not what the model generally knows about the topic)

- Be specific, not generic (explicitly gives a bad example: "Can you explain more?")

- Not repeat the original question

- Not introduce topics not present in the documents

- Return exactly three numbered questions with no preamble



The context sent to the model is limited to the first 200 characters of each of the top 5 retrieved chunks (`citations[:5]`). This is intentional: the full chunk text is used for the answer prompt (where the model needs the complete context), but for follow-up suggestions, a brief excerpt per chunk is sufficient to anchor the questions in document content without bloating the prompt.



---



### Decision 6 — validation and cleanup of the parsed response



**What changed:** after splitting the LLM response into lines and stripping prefixes, the code:

1. Removes empty lines.

2. Deduplicates (case-insensitive, preserving order).

3. Takes at most 3 (`unique[:3]`).

4. Logs a warning if fewer than 3 survive (doesn't raise).



**Why not enforce exactly 3 by padding:** if the model returns 2 questions instead of 3, padding with a fake third question would be worse than showing 2 good ones. The frontend renders however many arrive (0–3). A warning log is the right signal: it tells the operator the model didn't follow the instruction, without degrading the user's experience with a synthetic question.



---



### Decision 7 — frontend: suggestion chips trigger immediate send, not just input fill



**What changed:** `handleSuggestionClick(question)` directly runs the full send flow — it does not just populate the input box and wait for the user to press Enter. This mirrors the requirement: "behave exactly like the user typed the question."



**Why immediate send:** filling the input and waiting requires the user to perform one more action (press Send), which adds friction and breaks the "one click to explore" pattern. The immediate send is more consistent with how suggestion chips work in search engines and chat assistants.



**Why `handleSuggestionClick` is a standalone function rather than calling `handleSubmit`:** `handleSubmit` reads from the React `input` state, which is asynchronous — `setInput(question)` followed immediately by `handleSubmit()` would read the old input value, not the new one. The standalone function passes the question text directly as a local variable, avoiding the state timing problem.



---



### Extensibility notes (updated after Day 33 refactor)



- **Conversation memory:** follow-up generation now runs inside `_generate_answer`, which already receives `history`. Multi-turn-aware suggestions can be added by referencing `history` inside the follow-up instruction block in the prompt — no architectural change needed.

- **Agentic workflows:** the suggestion chips remain an entry point for agent-style "what to explore next" reasoning. Logic to filter suggestions based on topics already covered in the session can be added in `_validate_followups()` or in `send_message()` without touching retrieval or the LLM call itself.

- **Async generation:** the single-call architecture makes async generation harder to do selectively (the answer and follow-ups come back together), but also means the async complexity is no longer necessary — the follow-up latency is zero because it's part of the same call.



---



## [Day 33 — Architecture Revision] Collapse follow-up generation into a single LLM call



**Problem:** the Day 33 implementation made two sequential LLM calls per chat message — one for the answer + reasoning, one for the follow-up questions. On the Gemini free tier, this doubled quota consumption. After several chat exchanges, the follow-up generation call started hitting 429 RESOURCE_EXHAUSTED errors because the primary answer calls had already consumed the per-minute token budget. Users saw the answer rendered correctly but suggestions silently disappeared — a confusing experience.



The root cause was an architectural constraint from the original isolation principle ("follow-up generation failure must never affect the answer"), which was correctly stated but over-implemented: the isolation only required that a failed follow-up call not propagate as an error to the user, not that it had to be a completely separate API request.



---



### Decision — merge follow-up generation into the existing structured output call



**What changed:**



1. **`AnswerWithReasoning`** gained a third field:

   ```python

   follow_up_questions: list[str] = Field(

       default_factory=list,

       description="Exactly 3 concise follow-up questions..."

   )

   ```

   The field is `default_factory=list` so it degrades to `[]` if the model omits or misformats it — no schema rejection.



2. **The answer prompt** now includes a follow-up instruction block after the answer instructions:

   ```

   After answering, also generate follow_up_questions: exactly 3 questions

   that help the user explore the uploaded documents further. Prefer deeper

   understanding, comparisons, summaries, practical applications. Avoid

   repeating the question. Return an empty list if no grounded questions

   can be generated.

   ```

   The instruction lives at the end of the prompt so it doesn't interfere with the answer quality.



3. **`_try_model()`** now returns `tuple[str, str, list[str]]` — answer, reasoning, follow_up_questions — all from the same `result` object. The plain-text fallback path returns `[]` for follow-up questions (no reliable way to extract a list from unstructured text).



4. **`_invoke_with_fallback()`** return type expanded to `tuple[str | None, str | None, list[str], str]`.



5. **`_generate_answer()`** return type expanded to `tuple[str, str, list[str], str]`.



6. **`send_message()`** reads follow-up questions directly from `_generate_answer()` instead of calling a separate function after the DB commit.



**Deleted entirely:**

- `FollowUpQuestions` Pydantic model (separate schema for the second call)

- `_generate_followup_questions()` function (second LLM call + validation logic)

- The post-commit follow-up call block in `send_message()`

- The `low_confidence: bool` parameter and call-site translation `(confidence == "none")`



The `_RECOVERY_QUESTIONS` list and the confidence-check for the "none" path are retained, now handled inline in `send_message()` in the `if not filtered_citations:` branch.



**`_validate_followups()`** is a new small helper that the `_try_model` path calls to normalise the follow-up list: strip whitespace, discard empty strings, deduplicate (preserving order), cap at 3.  Extracted from what was the body of `_generate_followup_questions` so it's reusable without carrying the rest of the function.



---



### Why this doesn't compromise the isolation guarantee



The original isolation requirement was: "follow-up generation failure must never fail the chat request." That guarantee is still upheld:



- `_validate_followups()` never raises — it only iterates a list and returns a filtered copy.

- If the model omits `follow_up_questions` entirely, Pydantic sets it to `[]` via `default_factory`.

- If the structured output call fails entirely (`_try_model` falls back to the plain-text path), follow-up questions come back as `[]` — the answer is still returned, the session is still persisted, the user never sees an error.

- The confidence == "none" path bypasses the LLM entirely and still returns recovery questions — no change from before.



The only thing that changed is where follow-up generation happens: inside the existing LLM call, not after it.



---



### Impact



| Metric | Before (two calls) | After (one call) |

|---|---|---|

| API requests per chat message | 2 | 1 |

| Quota consumption | 2× | 1× |

| Follow-up latency | Answer latency + follow-up latency | Answer latency only (parallel) |

| Failure modes | Answer can succeed while follow-up fails | Both succeed or both fail together |

| Code surface | ~90 lines across 3 functions + 1 class | ~15 lines across `_validate_followups` + field |



The ~50% reduction in API requests directly addresses the Gemini free-tier quota exhaustion that caused follow-up suggestions to silently disappear after several chat exchanges.



---



## [Day 34] Document Recommendation & Cross-Document Comparison



### Goal



Transform the assistant from a document Q&A chatbot into an intelligent study assistant that can answer questions, recommend the best document for a given learning goal, and compare explanations across documents — all through the same chat interface, without the user switching modes.



---



### Architecture: intent-first branching inside `send_message`



```

classify_intent()   ->   QUESTION | RECOMMEND | COMPARE

      |

retrieval_service.retrieve()   (top_k varies by intent)

      |

QUESTION  -> _generate_answer()        (existing path, unchanged)

RECOMMEND -> comparison_service.recommend()

COMPARE   -> comparison_service.compare()

      |

ONE structured LLM call (regardless of intent)

      |

ChatResponse

```



This preserves the quota budget: exactly 1 LLM call per user message for all three intents, consistent with the Day 33 follow-up question refactor.



---



### Why `comparison_service.py` is isolated



1. **Cognitive separation.** `chat_service` already has ~200 lines of LLM orchestration. Adding per-document grouping, chunk capping, document name resolution, and two distinct prompt templates into the same file would make it unmaintainable.



2. **Testability.** `comparison_service` is a pure function module — it takes pre-retrieved citations and returns structured results. No dependency on session state, DB writes, or the HTTP layer.



3. **Future extensibility.** Reranking, hybrid search, or an LLM-based intent classifier can be added without touching `comparison_service`. The retrieval contract (`citations: list[dict]`) is stable.



`comparison_service` does NOT call `retrieval_service.retrieve()` — retrieval remains in `send_message`, which passes pre-retrieved citations down, the same way `_generate_answer` receives pre-retrieved citations rather than fetching its own.



---



### Intent classification: rule-based, 0 API calls



An LLM intent classifier adds 1 API call to every user message — ~80% of which are plain questions. Rule-based keyword matching is O(n) in keyword count, instant, and sufficient for the concrete intent signals this feature targets.



COMPARE is checked before RECOMMEND to handle phrases like "which is better between X and Y". QUESTION is the safe default — misclassification degrades to a normal RAG answer, not an error.



`classify_intent` accepts `history` for future use (e.g. follow-up intent detection in multi-turn conversations) but currently classifies from the current message only.



---



### Retrieval fairness for RECOMMEND: `_cap_per_document`



RECOMMEND and COMPARE use `top_k = 30` for broader coverage. However, a single dominant document can still occupy most of the top-30 result set (e.g. 25 of 30 slots), leaving other documents with almost no representation in the LLM prompt.



Fix: `_group_by_document()` groups chunks by document, then `_cap_per_document(n=5)` keeps the top 5 chunks per document by score. Every document gets up to 5 representative chunks in the LLM prompt regardless of how cosine scores distribute across the full result set. COMPARE does not need this cap — it filters to target documents explicitly.



---



### Dynamic suitability labels (not a fixed taxonomy)



Fixed labels ("Best for learning", "Best for interview prep") assume the user's goal fits a predefined category. Instead, the LLM is instructed to infer the user's specific goal and derive a label that directly reflects it:



- "I want examples" -> "Best examples", "Most theoretical"

- "interview prep" -> "Best interview prep", "Best quick revision"

- "detailed" -> "Most comprehensive", "Best for depth"



The `suitability_label` field description explicitly says "Do NOT use a fixed label set — infer from the request." This is enforced at the prompt level.



---



### Conversation context for COMPARE: explicit > implicit > clarification



`_resolve_target_documents` implements a three-priority resolution:



1. **Explicit**: filename substrings present in the message.

2. **Implicit**: message contains a reference word ("both", "the first two") AND the last assistant message named >=2 distinct documents.

3. **Unresolvable**: return None -> clarification message listing available documents.



The function never guesses. A wrong comparison produces a confident-sounding but irrelevant structured response — worse than a clarification request.



---



### Confidence field: `str | None = None`



The existing confidence vocabulary (`"none"`, `"low"`, `"medium"`, `"high"`, `"error"`) represents retrieval similarity score quality. For RECOMMEND and COMPARE, no per-chunk score threshold is computed. Setting `confidence = "high"` would mislead; inventing `"n/a"` pollutes a field with a specific existing vocabulary.



Resolution: `confidence: str | None = None` on `ChatResponse`. `None` is falsy in JavaScript — the existing frontend guard `{m.confidence && m.confidence !== "high" && ...}` already short-circuits with no code change needed.



---



### Single-document collections: natural message, not silent fallback



When the collection has fewer than 2 ready documents, a natural explanation is returned:



- RECOMMEND: "Only one document is currently ready in this collection. Upload additional documents to receive recommendations across multiple sources."

- COMPARE: "Comparing documents requires at least two ready documents in this collection. Upload another document to use this feature."



Falling back to QUESTION would produce a confusing answer when the user explicitly asked for a recommendation or comparison.



---



### API: additive only



Two new optional fields on `ChatResponse`:

- `recommended_documents: list[RecommendedDocument] = []`

- `comparison_sections: list[ComparisonSection] = []`



Both default to `[]`. `message` is always populated as a plain-text summary regardless of intent — the chat thread always renders readable text even if a client does not process the structured fields.



---



### Future extension points



- **Hybrid Search / Reranking**: plugging in to `retrieval_service.retrieve()` benefits all three intents automatically.

- **OCR**: scanned documents will contribute to recommendations and comparisons once OCR is added to ingestion — no comparison_service changes needed.

- **More intents**: adding `SUMMARIZE`, `QUIZ`, or `OUTLINE` requires a new phrase set, enum value, generator function, and branch in `send_message`. The scaffold does not change.

- **LLM intent classifier**: `classify_intent` can be replaced without changing any other code — the function signature and return type are stable.

- **Agentic workflows**: the typed `recommended_documents` and `comparison_sections` fields are structured entry points for a future agent that could act on recommendations rather than just displaying them.



---

---

## [Day 35] Phase 4 — Hybrid Retrieval (BM25 + Vector + Reciprocal Rank Fusion) & Intent Classifier Refinement

### Goal

Improve retrieval quality for technical terminology, exact keyword matches, function names, and spec numbers without degrading semantic search accuracy, increasing database round-trips, or exceeding free-tier cloud deployment constraints. Simultaneously refine intent classification to prevent conceptual comparison questions from being misclassified as document comparison tasks.

---

### Decision 1 — Scope Isolation: `retrieve()` & `build_citations()` Separation Preserved

**Architecture Contract:**
The retrieval and post-processing pipeline remains strictly separated:
```
chat_service.send_message()
    retrieval_service.retrieve(collection_id, query, db, top_k)
        → list[tuple[Document, float]]   (LangChain type, normalized score)
    retrieval_service.build_citations(db, retrieved)
        → list[dict]                     (internal citation shape)
    retrieval_service.filter_citations(citations, min_score)
    retrieval_service.to_public(citations, query)
```

- `retrieve()` always returns `list[tuple[Document, float]]` regardless of whether `RETRIEVAL_MODE` is `"vector"` or `"hybrid"`. In hybrid mode, `Document` objects are constructed from the Chroma candidate pool with RRF scores attached, ensuring `build_citations()`, `filter_citations()`, and `to_public()` remain 100% backward compatible.
- `chat_service.py` only updates a single line to pass the `db` session into `retrieve()`: `retrieval_service.retrieve(collection_id, message_text, db, top_k=retrieval_top_k)`.
- `comparison_service.py` is isolated and never invokes retrieval directly; it operates on pre-retrieved citations passed down from `send_message()`.

---

### Decision 2 — In-Memory BM25 Store (`bm25_store.py`) with Chroma Single Source of Truth

**Why in-memory Okapi BM25:** `rank-bm25` provides a lightweight pure-Python Okapi BM25 implementation without requiring a separate search server or additional ML model. For study-aid collections of typical size (hundreds of chunks per collection), the in-memory index is expected to have a small memory footprint and low search latency.

**Chroma as Single Source of Truth:** `chroma.get(include=["documents", "metadatas"])` returns all stored chunk text (`documents`), IDs, and metadata in one call. `bm25_store.py` builds its index lazily on first access directly from Chroma:
```python
_cache: dict[str, _BM25Index] = {}   # collection_id (str) → index
```

**Invalidation Contract & Sync Correctness:**
- After document ingestion completion (success or failure cleanup): `bm25_store.invalidate(collection_id)` drops the cache entry.
- After document deletion (`delete_document` route): `bm25_store.invalidate(collection_id)` drops the cache entry.
- The next query triggers `bm25_store.get_index()` → lazy rebuild from current Chroma state. Single source of truth in Chroma prevents cache drift.

**Empty Corpus Defense:** Instantiating `BM25Okapi([[]])` on an empty collection raises `ZeroDivisionError` inside `_calc_idf()` (`sum(idf) / len(idf)` when `len(idf) == 0`). `bm25_store` sets `bm25 = None` for empty collections and short-circuits `search()` to return `[]` immediately.

---

### Decision 3 — Single-Pass Candidate Fetching & Dictionary RRF Fusion (Zero Second Chroma Request)

**Candidate Pool Multiplier (`top_k * 4`):**
In hybrid mode, vector search retrieves `top_k * 4` candidates (20 candidates for default `TOP_K = 5`).

**Zero Second Chroma Request:**
1. `_vector_retrieve()` fetches candidate documents and constructs a `{chroma_id: (Document, distance)}` lookup dict in a single pass.
2. `_bm25_retrieve()` searches the in-memory BM25 index over the full collection corpus and returns candidate ranks.
3. `_rrf_fuse()` computes Reciprocal Rank Fusion scores:
   $$RRF\_score(chunk) = \sum_{i} \frac{1}{k + rank_i}$$
   with $k = 60$ (`BM25_RRF_K`, TREC standard).
4. Results are looked up directly from the in-memory dictionary. Chunks present in BM25 search that fell outside the top `top_k * 4` vector pool are skipped. With a 4× candidate multiplier, this eliminates a second Chroma database round-trip (get(ids=...)) while keeping the candidate pool sufficiently broad for the current collections. BM25-only candidates that fall outside the vector candidate pool are excluded; this tradeoff can be revisited if benchmark results show recall degradation.

---

### Decision 4 — `document_name` in Chroma Metadata for Zero-Postgres Retrieval

**Optimization:** During ingestion, `document_name` (filename) is included in Chroma metadata alongside `document_id`, `page_number`, and `chunk_index`:
```python
batch_metadatas = [
    {
        "document_id": str(document.id),
        "document_name": document.filename,
        "page_number": c["page_number"],
        "chunk_index": c["chunk_index"],
    }
    for c in batch
]
```

`build_citations()` extracts `document_name` directly from Chroma metadata, eliminating database queries during retrieval for all documents ingested in Phase 4+. A single bulk PostgreSQL fallback query (`get_by_ids`) is retained exclusively for legacy chunks ingested before Phase 4.

---

### Decision 5 — RRF Score Normalization to `[0, 1]` Similarity Space

**Problem:** With two result lists and $k=60$, the theoretical maximum RRF score is $2 / (60 + 1) \approx 0.0328$, achieved when a chunk is ranked first in both vector and BM25 results. Chunks appearing in only one list receive only that list's contribution.
In contrast, `filter_citations()` applies `MIN_CITATION_SCORE = 0.05` expecting cosine similarity values in `[0, 1]`. Unnormalized RRF scores caused `filter_citations()` to discard *all* retrieved chunks in hybrid mode, resulting in false "not found in your documents" responses.

**Fix:** RRF scores are normalized into `[0, 1]` similarity space by dividing by the theoretical maximum score for two result lists $max\_rrf = \frac{2}{\text{BM25\_RRF\_K} + 1}$:
$$\text{score} = \min\left(\frac{\text{rrf\_score}}{\text{max\_rrf}}, 1.0\right)$$

This maps the maximum two-list RRF score to 1.0, allowing the existing downstream filtering and confidence thresholds (MIN_CITATION_SCORE, HIGH_CONFIDENCE_THRESHOLD, MEDIUM_CONFIDENCE_THRESHOLD) to continue operating without configuration changes. The resulting value is a normalized retrieval/ranking score, not a calibrated probability or confidence percentage.

---

### Decision 6 — Internal Debug Metadata & Fault-Tolerant Terminal Profiling

**Internal Debug Fields:** Each retrieved document in hybrid mode carries internal metadata (`_vector_rank`, `_bm25_rank`, `_rrf_score`, `_retrieval_mode`). `to_public()` strips these fields before returning, keeping API output clean.

**Fault Tolerance:** If BM25 search encounters an error, `retrieve()` catches the exception, logs it, gracefully falls back to trimmed vector search, and outputs profiling status:
```
==========================
Requested Mode : hybrid
Actual Mode    : vector   [BM25 unavailable: <exception message>]

Vector Search  :  39 ms   (candidates: 5)
Total Retrieval:  39 ms   (returned: 5)
==========================
```

---

### Decision 7 — Document-Anchored Intent Classification (COMPARE vs Concept Questions)

**Problem:** The initial intent classifier triggered `COMPARE` whenever keywords like `"difference"`, `"differ"`, `"versus"`, or `"contrast"` were detected. Factual conceptual questions such as *"What is the difference between supervised and unsupervised learning?"* were misclassified as `COMPARE`, leading to multi-document comparison prompts and fallback warnings when single concepts were queried.

**Fix:** Intent classification for `COMPARE` now requires **two-gate co-occurrence**:
1. **Compare Verb:** (`compare`, `contrast`, `vs`, `versus`, `side by side`, etc.)
2. **Document Reference Signal:** (`pdf`, `document`, `file`, `notes`, `these docs`, `both files`, `first two`, etc.)

Conceptual queries lacking document references default to QUESTION intent, while document-focused requests such as "Compare these two PDFs" remain eligible for COMPARE. Tested against a 24+ case regression suite (tests/test_intent_classifier.py).

---

### Decision 8 — Standalone Benchmark & Evaluation Tool (`scripts/benchmark_retrieval.py`)

A standalone CLI tool evaluates Top-1 and Top-3 retrieval precision and average latency across a JSON dataset of target questions (`scripts/questions.json`). Features `--mode vector|hybrid` and `--compare` mode, outputting a side-by-side signed delta table (`Top-1`, `Top-3`, `Latency`) to quantitatively evaluate retrieval accuracy changes before deployment.
