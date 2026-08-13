# InCite

**Chat with your documents — every answer cited, every low-confidence answer flagged.**

Upload PDFs into collections, ask questions, and get answers grounded in your own
documents — not the model's training data — with every claim traceable to a specific
document, page, and passage.

🔗 **Live app:** [in-cite.vercel.app](https://in-cite.vercel.app)
🔗 **API:** [incite-0vgr.onrender.com/docs](https://incite-0vgr.onrender.com/docs)

---

## The problem this solves

Plain LLMs either don't know your private documents or hallucinate confident-sounding
answers with no way to verify them. InCite answers only from documents you uploaded,
cites the exact source for every claim, and — when retrieval is weak — says so
honestly in code, before the LLM is ever called. See
[`docs/design-decisions.md`](docs/design-decisions.md) for why prompt instructions
alone aren't sufficient.

## Key features

| Feature | Detail |
|---|---|
| **Auth & multi-tenancy** | JWT accounts; collections, docs, and chats are isolated at both the DB and vector-store level |
| **Collections** | Group documents by topic and chat within that scope only |
| **PDF upload** | Background ingestion (chunk → embed → store); frontend polls until ready |
| **Grounded chat + citations** | Every answer shows document, page number, and the most relevant passage |
| **Confidence-aware answers** | Irrelevant retrieval is filtered before the LLM sees it; borderline answers are flagged "low confidence" |
| **Follow-up suggestions** | Each response surfaces 3 grounded follow-up questions |
| **Document recommendation** | Ask which document to read for a goal; the system ranks and recommends |
| **Cross-document comparison** | Compare documents on a topic; retrieval runs per-document and results are synthesised |
| **Hybrid retrieval** | BM25 + vector search fused with Reciprocal Rank Fusion (RRF) — improves accuracy on exact-match and technical queries |
| **Retrieval benchmarking** | CLI tool measuring Top-1/Top-3 accuracy and latency across retrieval modes |
| **Conversation memory** | Follow-ups resolve correctly against prior context |
| **Observability** | Every query logs retrieved chunks, scores, response time, and model |

---

## Architecture

```
Vercel (React frontend)
        │
        ▼
Render — FastAPI backend + embedded Chroma (PersistentClient)
        │
        ▼
Neon (managed Postgres)
```

Backend is layered: routes are thin, business logic lives in services, DB access in
repositories. Full breakdown: [`docs/architecture.md`](docs/architecture.md)

## Tech stack

| Layer | Technology |
|---|---|
| Frontend | React (Vite), React Router |
| Backend | FastAPI, SQLAlchemy, Alembic |
| Database | PostgreSQL (Neon) |
| Vector store | Chroma (embedded, `PersistentClient`) |
| Lexical retrieval | `rank-bm25` (Okapi BM25, in-memory, per-collection) |
| Retrieval fusion | Reciprocal Rank Fusion (RRF) |
| Orchestration | LangChain (retrieval + generation) |
| LLM | Google Gemini |
| Embeddings | `sentence-transformers` (local, CPU) |
| Auth | JWT |
| Deployment | Render (backend), Vercel (frontend), Neon (Postgres) |

Retrieval was built manually first — raw chunking, embeddings, cosine similarity — then
migrated to LangChain. Every abstraction can be explained from first principles, not
treated as a black box.

---

## Notable engineering decisions

Full reasoning for 40+ decisions is in [`docs/design-decisions.md`](docs/design-decisions.md).

- **Confidence isn't one threshold, it's two.** A hard filter removes clearly irrelevant
  retrieval before it reaches the LLM (blocking hallucination from noise); a separate,
  lower bar flags borderline answers as "low confidence" rather than pretending a single
  cutoff can cleanly separate right from wrong. A real test case produced the *same*
  top similarity score for both a correct refusal and an incorrect answer — that's why
  this isn't one blunt number.

- **Embedded Chroma over a separate service.** Chroma runs in-process via
  `PersistentClient` rather than as its own server — a deliberate trade-off to stay on
  free-tier hosting (a private Render service costs ~$7/month; a public Chroma server
  has no built-in auth). The honest cost: embedded data doesn't survive restarts without
  a persistent disk. Acceptable for a portfolio project; documented explicitly so it
  isn't mistaken for a universally good pattern.

- **Hybrid retrieval fuses rankings, not scores.** Vector similarity and BM25 scores
  live on incompatible scales — averaging them is meaningless. Instead, both result lists
  are ranked independently and fused with Reciprocal Rank Fusion
  (`score = Σ 1/(k + rank_i)`, `k=60`). The RRF score is then normalised to `[0, 1]`
  so the existing confidence thresholds and citation filters work without any changes.
  `RETRIEVAL_MODE=vector` preserves the vector-only baseline; `RETRIEVAL_MODE=hybrid`
  enables BM25 + vector + RRF. BM25 indexes are per-collection, in-memory, and lazily
  built from the same Chroma corpus — no separate search server, no extra storage.

## Known limitations

- Embedded Chroma data doesn't survive Render restarts without a persistent disk
  (a keep-alive ping prevents idle spin-downs, but deploy-triggered restarts still
  wipe it — not production-grade)
- `reasoning` and `confidence` aren't persisted with historical messages; generated
  live, shown once
- Similarity thresholds are tuned against this project's test documents, not
  universally calibrated

## Future work

- Cross-Encoder reranking (score-based reranking after RRF fusion)
- OCR for scanned PDFs
- Further retrieval evaluation and threshold calibration
- Document intelligence report (reading level, topics, processing metadata)

---

## Local setup

```bash
git clone <this-repo>
cd incite
cp .env.example .env          # fill in GOOGLE_API_KEY and JWT_SECRET
docker compose up --build
docker compose exec app alembic upgrade head   # first time only
```

Health checks: `GET localhost:8000/health`, `/health/db`, `/health/chroma`

Frontend (separate terminal):
```bash
cd frontend && npm install && npm run dev      # http://localhost:5173
```

> **PostgreSQL port:** Docker maps the container's `5432` to `localhost:5433` to avoid
> conflicts with a locally-running Postgres instance. Migration commands use the
> container network and work unchanged; external tools (psql, TablePlus) connect on `5433`.

### Retrieval modes

```
RETRIEVAL_MODE=vector   # default — vector-only baseline
RETRIEVAL_MODE=hybrid   # BM25 + vector + Reciprocal Rank Fusion
BM25_RRF_K=60           # RRF k constant (TREC standard)
```

Switch freely — no rebuild required.

## Benchmarking

```bash
docker compose exec app python scripts/benchmark_retrieval.py --mode vector
docker compose exec app python scripts/benchmark_retrieval.py --mode hybrid
docker compose exec app python scripts/benchmark_retrieval.py --compare   # side-by-side delta
```

Each run reports **Top-1 accuracy**, **Top-3 accuracy**, and **average latency** across
questions defined in `scripts/questions.json`. Edit that file with real collection UUIDs
and expected document filenames to measure against your own data.

## Docs

- [`docs/architecture.md`](docs/architecture.md) — layered architecture, data flow, deployment shape
- [`docs/database.md`](docs/database.md) — schema and why each table exists
- [`docs/api.md`](docs/api.md) — endpoint reference
- [`docs/design-decisions.md`](docs/design-decisions.md) — every major decision and bug fix,
  with reasoning, written as the project happened
