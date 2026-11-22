# PostgreSQL schema

Alembic manages the schema. All seven tables have a UUID primary key `id` and a timezone-aware `created_at` column. The table below lists the other application fields.

| Table | Other fields |
| --- | --- |
| `users` | `email` (unique), `hashed_password` |
| `collections` | `user_id` → users, `name`, `delete_requested_at`, `cleanup_completed_at`, `cleanup_attempts`, `cleanup_retry_at`, `cleanup_error` |
| `documents` | `collection_id` → collections, `filename`, `file_path`, `page_count`, `status`, `error_code`, `error_message`, `delete_requested_at`, `cleanup_attempts`, `cleanup_retry_at`, `cleanup_error` |
| `chunks` | `document_id` → documents, `page_number`, `chunk_index`, `content`, `chroma_id` |
| `chat_sessions` | `user_id` → users, `collection_id` → collections, `title` |
| `messages` | `session_id` → chat sessions, `role`, `content`, `citations`, `recommended_documents`, `comparison_sections` |
| `query_logs` | `message_id` → messages, `retrieved_chunk_ids`, `similarity_scores`, `top_k`, `response_time_ms`, `llm_model` |

Documents use `processing`, `ready`, and `failed` for ingestion. Their `file_path` points to the original PDF on disk. Chunks store text in PostgreSQL and link to the searchable copy in Chroma using `chroma_id`, formatted as `{document_id}_p{page_number}_c{chunk_index}`. Page numbers start at one and chunk indices start at zero on each page.

Message roles are `user` and `assistant`. Citations, recommendation results, and comparison sections are nullable PostgreSQL `JSON` fields. Query logs store candidate IDs and scores for debugging. They do not establish which source supports an individual claim.

## Deletion lifecycle and retention

A non-null `delete_requested_at` excludes a document or collection from active use. A collection's request also applies to its documents. Cleanup retry fields record the failure count, next eligible time, and most recent failure step and error type. A busy collection lock can move the next eligible time without recording a failure.

The database foreign keys use `ON DELETE CASCADE`. The worker uses that cascade to remove chunks when purging a document row. The collection HTTP delete route archives its row, and the worker sets `cleanup_completed_at` after cleanup. The collection row, sessions, messages, citations, and query logs remain in PostgreSQL. Deleting a session removes its messages and their query logs. SQL cascades alone do not clean up the filesystem or Chroma. See [ADR 002](decisions/002-reliable-deletion-lifecycle.md).

Apply and verify Alembic migrations before starting upgraded application processes. See the [upgrade procedure](../README.md#deployment-and-upgrades).
