# HTTP API

The FastAPI backend serves local interactive documentation at `/docs` and its machine-readable specification at `/openapi.json`. Resource IDs are UUIDs. POST bodies are JSON except for the PDF upload.

## Health

All three probes accept unauthenticated `GET` and `HEAD` requests. Use `GET` to check the JSON response.

| Path | Successful response | Check |
| --- | --- | --- |
| `/health` | `{"status":"ok"}` | Application process responds |
| `/health/db` | `{"status":"ok","database":"connected"}` | PostgreSQL runs `SELECT 1` |
| `/health/chroma` | `{"status":"ok","chroma":"connected"}` | Embedded Chroma responds to its heartbeat |

A caught database or Chroma probe error returns HTTP 200 with `status: "error"`, the dependency marked `"unreachable"`, and a `detail` string. Check the response body for readiness. The dependency probes can disclose diagnostic details and do not check the Groq provider or cleanup backlog.

## Authentication

| Method and path | Request | Success |
| --- | --- | --- |
| `POST /auth/register` | `email`, `password` (at least eight characters) | 201 with `id`, `email`, `created_at` |
| `POST /auth/login` | `email`, `password` | 200 with `access_token` and `token_type: "bearer"` |
| `GET /auth/me` | Bearer token | 200 with `id`, `email`, `created_at` |

Register and login are public. Register returns 409 for an existing email. Login returns 401 for invalid credentials. Send `Authorization: Bearer <token>` to `/auth/me` and the resource routes below. With the pinned FastAPI version, missing or non-Bearer authorization returns 403. An invalid or expired Bearer token returns 401. Invalid request fields or path UUIDs can return 422.

## Collections

| Method and path | Request or behavior | Success |
| --- | --- | --- |
| `POST /collections` | `name` (1–200 characters) | 201 with a collection |
| `GET /collections` | List the caller's active collections | 200 with an array of collections |
| `GET /collections/{collection_id}` | Read an active collection | 200 with a collection |
| `DELETE /collections/{collection_id}` | Record a deletion request | 202 while pending, 204 after cleanup |

A collection response contains `id`, `name`, and `created_at`. Active routes return 404 for missing, foreign, or archived collections.

## Documents

| Method and path | Request or behavior | Success |
| --- | --- | --- |
| `POST /collections/{collection_id}/documents` | `multipart/form-data`, PDF field `file` | 201 with a document in `processing` |
| `GET /collections/{collection_id}/documents` | List visible documents in the active collection | 200 with an array of documents |
| `GET /documents/{document_id}` | Read or poll ingestion status | 200 with a document |
| `DELETE /documents/{document_id}` | Record a deletion request | 202 while pending |

A document response contains `id`, `filename`, `status`, `page_count`, `error_code`, `error_message`, and `created_at`. Status is `processing`, `ready`, or `failed`. Page count and error fields can be null. Rejected uploads return 422 with validation details. Failures during background ingestion are reported through the document's `failed` status and error fields. Missing, foreign, and deletion-pending documents return 404 from the read route.

### Deletion and history

A 202 response has the body `{"status":"cleanup_pending"}`. The acknowledgement means PostgreSQL recorded the request. The pending document or collection is excluded from active use and new retrieval eligibility checks. A worker removes the files and vectors and finalizes PostgreSQL cleanup. An authorized retry while pending preserves the original request time. A repeat collection deletion returns 204 with an empty body after cleanup. A physically removed document returns 404 on repeat deletion. Foreign and missing IDs also return 404. Archived collection chats remain accessible through the owner-only session endpoints.

## Chat and sessions

`POST /collections/{collection_id}/chat` takes a nonempty `message` and an optional `session_id`. Omitting the ID or providing null starts a session. A missing or unowned session, or an unavailable collection, returns 404. An owned session from a different collection returns 400.

The route chooses question answering, comparison, or recommendation from the request text. A successful response is HTTP 200 with:

- `session_id`, `message` (the assistant reply), and `reasoning`.
- `citations`, with `document_name`, `page_number`, `excerpt`, and `score` for each citation. Scores measure retrieval relevance and depend on the retrieval mode.
- `confidence`, which can be `none`, `low`, `medium`, `high`, `error`, or null. `none` signals insufficient evidence. `error` signals that QA generation exhausted its model attempts. Comparison and recommendation with eligible evidence use null and return an empty citation array.
- `follow_up_questions`, an array of strings that can be empty.
- `comparison_sections` with `title` and `content`, and `recommended_documents` with `document_name`, `rank`, `suitability_label`, and `reason`. The arrays are empty when the corresponding results are unavailable.

All three intents share ready-only retrieval. The insufficient-evidence response uses no provider call. Up to three earlier user messages help interpret the request. The full transcript remains available to the session owner.

| Method and path | Behavior | Success |
| --- | --- | --- |
| `GET /sessions` | List the caller's sessions, including sessions for archived collections | 200 with `id`, `collection_id`, `title`, `created_at` per session |
| `GET /sessions/{session_id}/messages` | Read the transcript in chronological order | 200 with an array of messages |
| `DELETE /sessions/{session_id}` | Delete an owned session and its messages | 204 with an empty body |

History messages contain `id`, `role` (`user` or `assistant`), `content`, `citations`, `recommended_documents`, `comparison_sections`, and `created_at`. The three structured fields can be null. An unavailable or unowned session returns 404 from the messages and delete routes.

See the [architecture guide](architecture.md) and [database reference](database.md) for storage and lifecycle details.
