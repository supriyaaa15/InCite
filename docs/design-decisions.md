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

<!-- Add new entries below as each milestone is reached. -->
