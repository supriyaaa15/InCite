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


