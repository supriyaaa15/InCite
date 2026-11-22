# ADR 002 — Reliable deletion lifecycle

**Status** — Durable logical deletion, physical cleanup, and retry recovery
are implemented for the supported single-host deployment.

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
file paths the cleanup worker needs for retries. A nullable collection
`cleanup_completed_at` records completion. Each target stores its retry time,
attempt count, and last failed step in PostgreSQL. The migrations preserve
existing records and foreign keys.

Both DELETE routes return `202 Accepted` with `cleanup_pending` after the SQL
commit. An authorized repeat while pending keeps the original timestamp.
Purged documents return `404`, and completed collection requests return `204`. Active views
hide pending targets. The collection's sessions, messages, citations, and query
logs are retained. Owners can still read them through the history API. The
current UI has no archive browser, and archived collections are no longer in
its active list. The confirmations explain that physical cleanup is pending.

## Transaction boundaries

- A delete request locks the collection first and the document second if needed.
  Ownership is verified under the collection lock. SQL failure rolls back the
  request. The delete routes perform no PDF, Chroma, or SQL-chunk cleanup.
- Upload saves and validates the file under the artifact lock and outside the
  SQL row lock. It rechecks ownership and collection state under the SQL
  collection lock through registration.
  If a concurrent deletion rejects registration, the route makes a best-effort
  attempt to remove only the new, unregistered upload.
- Ingestion checks active state inside the artifact lock before Chroma
  creation and each batch write.
  SQL chunk registration and the final ready or failed transition recheck state
  under collection-then-document row locks. Embedding and Chroma calls run
  outside those locks. Multiple application processes follow the same SQL lock
  order. A worker that encounters deletion cannot add SQL chunks or mark ready.
- Retrieval verifies deletion state during the existing PostgreSQL eligibility
  query. New chat requests also require an active collection. A turn that has
  already passed eligibility can finish.

## Physical cleanup and retries

The worker discovers work immediately at startup and each polling pass. It takes
a per-collection, OS-backed lock in the local Chroma directory. Upload file
writes, Chroma creation and writes, and cleanup all use that lock. It survives
a PostgreSQL connection loss and is released when the owning process exits.
Lock files remain in place to keep one lock identity per collection. Deletion
intent uses SQL row locks alone and can commit while an artifact write finishes.

The worker reloads the pending target under the artifact lock, then closes the
SQL read transaction. It removes only matching canonical vector IDs from the
target document's Chroma collection and invalidates BM25, including after
partial vector deletion. It then unlinks the PDF from the directory for the
stored owner and collection. Missing artifacts are safe to retry. Unsafe paths,
linked files, and shared paths remain pending for review.

After the external steps succeed, a short SQL transaction deletes the document.
The existing foreign key cascades to its chunks. Once an archived collection
has no document rows, the worker drops its Chroma collection and removes its
upload directory if empty. An unexpected file leaves completion pending.
The last SQL transaction sets the completion time and keeps the archive and
history rows. Retrieval uses non-creating Chroma lookups.

The worker records the failed step and backs off from five seconds up to five
minutes. Each pass selects a bounded batch. Previously, 16 pending documents
in a busy collection could fill every slot on every poll and starve an
unrelated document. A busy lock now moves its target's existing
`cleanup_retry_at` four seconds ahead, without changing the failure count or
last error. The normal due-time filter excludes it during that window. Due work
keeps its existing timestamp ordering. A crash or failed SQL commit leaves
durable pending work.
The next attempt repeats the external steps and treats verified absence as
success. There is no public endpoint to run cleanup against arbitrary targets.
If PostgreSQL is unavailable, a later poll retries discovery.

## Alternatives and limitations

Synchronous deletion cannot atomically commit changes across PostgreSQL,
Chroma, and the filesystem. A separate queue could track claims and retries,
but the existing rows can hold the pending request and retry state.
Larger batches only raise the contention threshold. Blocking on the artifact
lock would stall the pass. Reusing the existing timestamp keeps the batch
bounded and makes the deferral visible across restarts.

An in-flight Chroma operation may finish after logical deletion, but cleanup
waits for its artifact lock and later batches recheck pending state under that
lock. The fence requires cooperating writers and working OS locks on the same
local files. It does not establish correctness for multiple processes sharing
embedded Chroma internals, separate hosts, independent volumes, or tools that
bypass the lock. Run one application process per embedded Chroma store.
[Chroma system constraints](https://cookbook.chromadb.dev/core/system_constraints/).

Before rollout, stop older application processes, back up PostgreSQL, Chroma
and uploads, then apply `alembic upgrade head` to the intended database.
Confirm the database revision matches the head before starting the upgraded
process. Verify readiness and that due cleanup resumes. The Docker command
explicitly sets `--workers 1`, which overrides `WEB_CONCURRENCY`. Supply runtime
configuration at deployment time. The Docker build context excludes local
configuration and runtime artifacts. See the [deployment steps](../../README.md#deployment-and-upgrades).
Set `DELETION_CLEANUP_ENABLED=false` to pause physical cleanup while retaining
ordinary logical deletion requests.

A crash between upload file write and SQL registration can leave an
unregistered file. Collection cleanup preserves it and reports the nonempty
directory. Filesystem checks assume the app controls its storage directories.
Legacy orphans without usable ownership information, media- or backup-level
erasure, and redaction of retained transcripts are outside this milestone.

## Verification

See the [engineering log](../engineering-log.md) for the unit, migration, and
disposable PostgreSQL and Chroma test results. The 17-document regression
exercises the real worker and verifies both progress for the unlocked
collection and recovery of deferred work. A Linux container verified POSIX
lock contention and release after `SIGKILL`. A separate Windows Uvicorn process
test verified recovery after both external artifacts were removed but before
SQL finalization, with neighboring artifacts intact. A full Linux application
crash and restart and a kill during a Chroma operation remain unverified.
