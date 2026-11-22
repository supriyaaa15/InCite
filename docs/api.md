# API endpoints

## Health
- `GET /health` — is the app process alive
- `GET /health/db` — can the app reach Postgres
- `GET /health/chroma` — can the app reach Chroma

## Auth
- `POST /auth/register` — {email, password} -> {id, email}
- `POST /auth/login` — {email, password} -> {access_token}

## Collections
- `POST /collections` — {name} -> Collection
- `GET /collections` — list current user's collections
- `GET /collections/{id}` — get one
- `DELETE /collections/{id}` — record logical deletion and return `202 Accepted`
  with `{"status":"cleanup_pending"}`. The collection becomes unavailable to
  new uploads and chat. Owner-only history endpoints still serve its existing chats.

## Documents
- `POST /collections/{id}/documents` — multipart upload, kicks off background
  ingestion (chunk + embed + store), returns Document with status=processing
- `GET /collections/{id}/documents` — list docs in a collection
- `GET /documents/{id}` — single doc, for the frontend to poll status
- `DELETE /documents/{id}` — record logical deletion and return `202 Accepted`
  with `{"status":"cleanup_pending"}`. The document disappears from active views
  and retrieval. The PDF, SQL chunks, and vectors remain pending cleanup.

Repeating an authorized deletion request returns the same pending status and
preserves the original request time. Missing and foreign IDs return `404`.

## Chat
- `POST /collections/{id}/chat` — {message, session_id?} -> {answer, citations}
- `GET /sessions` — list current user's chat sessions
- `GET /sessions/{id}/messages` — full history for a session
- `DELETE /sessions/{id}`

## Phase 2
- `POST /collections/{id}/compare` — compare two documents (e.g. resume vs JD)

All routes except /health, /auth/register, /auth/login require a valid JWT in
the Authorization header: `Bearer <token>`.
