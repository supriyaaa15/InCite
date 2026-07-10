# Database schema

## Tables

**users** — id, email (unique), hashed_password, created_at

**collections** — id, user_id (FK), name, created_at
Belongs to a user. Documents and chat sessions belong to a collection.

**documents** — id, collection_id (FK), filename, file_path, page_count, status
(processing | ready | failed), uploaded_at
`file_path` points to wherever storage_service saved the raw PDF — lets you
reprocess embeddings or regenerate chunks without asking the user to re-upload.

**chunks** — id, document_id (FK), page_number, chunk_index, content, chroma_id
`chunk_index` is the chunk's position within the document (e.g. page 4, chunk 7)
— makes debugging a bad retrieval precise instead of "somewhere on page 4".
`chroma_id` is the bridge to the actual embedding vector, which lives in Chroma,
not Postgres.

**chat_sessions** — id, user_id (FK), collection_id (FK), title, created_at

**messages** — id, session_id (FK), role (user | assistant), content,
citations (JSONB), created_at
Citations are stored inline as JSONB rather than a separate table because
they're immutable once generated and never queried independently — no benefit
to normalizing them out.

**query_logs** — id, message_id (FK), retrieved_chunk_ids, similarity_scores,
top_k, response_time_ms, llm_model, created_at
Kept separate from messages because it's debugging/observability data — you
could delete every row in this table without breaking anything a user sees.

## Cascade behavior
All foreign keys cascade on delete: deleting a user removes their collections,
documents, chunks, chat sessions, and messages. No orphaned rows.
