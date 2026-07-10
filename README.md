# InCite

Chat with your documents — with every answer cited to a document, page, and chunk.

A full-stack RAG application: upload PDFs into collections, ask questions, get
answers grounded in your own documents instead of the model's memory.

## Stack
- **Backend:** FastAPI, PostgreSQL, SQLAlchemy, Alembic
- **Retrieval:** manual RAG pipeline (built from scratch first), then migrated
  to LangChain; Chroma for vector storage
- **Auth:** JWT
- **Frontend:** React (Vite)
- **Deployment:** Render (backend + Postgres), Vercel (frontend)

## Why "InCite"
Every answer comes with in-line citations — document name, page number, and
the exact retrieved chunk — so you can verify anything the model says instead
of trusting it blindly.

## Status
🚧 In active development — Day 1-2 of a 30-day build.

## Local setup
```bash
cp .env.example .env          # fill in GOOGLE_API_KEY and JWT_SECRET
docker compose up --build
```

Then check:
- `GET http://localhost:8000/health`
- `GET http://localhost:8000/health/db`
- `GET http://localhost:8000/health/chroma`

Run migrations:
```bash
docker compose exec app alembic revision --autogenerate -m "initial schema"
docker compose exec app alembic upgrade head
```

## Docs
- [`docs/architecture.md`](docs/architecture.md) — layered architecture, data flow
- [`docs/database.md`](docs/database.md) — schema and why each table exists
- [`docs/api.md`](docs/api.md) — endpoint reference
- [`docs/design-decisions.md`](docs/design-decisions.md) — every major design
  choice and the reasoning behind it

## Roadmap
See `docs/design-decisions.md` for the running log. Core build order: manual
RAG -> API + auth -> collections/upload -> LangChain migration -> citations +
logging -> frontend -> deployment -> polish.
