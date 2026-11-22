# ADR 002 — Reliable deletion lifecycle

**Status** — Partially implemented. Durable logical deletion is in place.
Physical cleanup and restart recovery are pending.

## Problem

Document deletion previously removed the PDF before committing SQL deletion
and left Chroma vectors behind. Collection deletion cascaded through SQL and
removed chat history while leaving the PDFs and vectors. A failed step or a
process exit could leave the stores inconsistent with no durable deletion request.

## Decision

Add nullable `delete_requested_at` timestamps to documents and collections.
Existing rows stay active. A document is pending if it or its parent collection
has a deletion request. The `processing`, `ready`, and `failed` values continue
to describe ingestion. The request time and existing rows retain the IDs and
file paths a later worker will need for retries. A nullable collection
`cleanup_completed_at` records the future completion state and remains unset
in this milestone. The forward-only migration preserves existing records and
foreign keys.

Both DELETE routes return `202 Accepted` with `cleanup_pending` after the SQL
commit. An authorized repeat keeps the original request timestamp. Active views
hide pending targets. The collection's sessions, messages, citations, and query
logs are retained. Owners can still read them through the history API. The
current UI has no archive browser, and archived collections are no longer in
its active list. The confirmations explain that physical cleanup is pending.

## Transaction boundaries

- A delete request locks the collection first and the document second if needed.
  Ownership is verified under the collection lock. SQL failure rolls back the
  request. The delete routes perform no PDF, Chroma, or SQL-chunk cleanup.
- Upload saves and validates the file outside the lock, then rechecks ownership
  and collection state while holding the collection lock through registration.
  If a concurrent deletion rejects registration, the route makes a best-effort
  attempt to remove only the new, unregistered upload.
- Ingestion checks active state before Chroma creation and batch writes.
  SQL chunk registration and the final ready or failed transition recheck state
  under collection-then-document row locks. Embedding and Chroma calls run
  outside those locks. Multiple application processes follow the same SQL lock
  order. A worker that encounters deletion cannot add SQL chunks or mark ready.
- Retrieval verifies deletion state during the existing PostgreSQL eligibility
  query. New chat requests also require an active collection. A turn that has
  already passed eligibility can finish.

## Alternatives and limitations

Synchronous deletion cannot atomically commit changes across PostgreSQL,
Chroma, and the filesystem. A separate queue could track claims and retries,
but the existing rows retain enough information for this foundation.

An in-flight Chroma operation can finish after the deletion request commits.
Its output remains ineligible, and its physical vectors need a later cleanup
pass. The app schedules one ingestion task per upload. This milestone does not
add claims or fencing for a duplicated ingestion task. A cleanup worker will
need a write fence that covers Chroma operations and late writers before it
can report physical completion. A process restart retains deletion intent.
Polling, backoff, startup recovery, vector removal, and PDF removal are future
work. A process crash between the upload file write and SQL registration can
also leave an unregistered file.

## Verification

See the [engineering log](../engineering-log.md) for the unit, migration, and
disposable PostgreSQL and Chroma test results.
