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
- Backend + Postgres: Render
- Frontend: Vercel
- Chosen over AWS EC2 for setup speed within a 30-day timeline — see
  design-decisions.md for the full reasoning.
