"""
Background ingestion service.

Runs as a FastAPI BackgroundTask, so the HTTP response has already been sent
by the time this executes.  Uses its own DB session (SessionLocal) because
the request-scoped session from get_db() is already closed.

Failure guarantee
-----------------
Every execution path ends with the document status set to either
`ready` or `failed`.  A `failed` document always carries an `error_code`
and a user-safe `error_message` on the DB row.

Exception hierarchy caught (in order):
1. UploadValidationError — raised by secondary checks inside iter_pages,
   e.g. if a page raises mid-stream.  Uses the error's own code/message.
2. MemoryError — Python-catchable OOM.  Maps to RESOURCE_LIMIT.
3. Exception — any other unexpected error.  Maps to INTERNAL_ERROR.

Note on OOMKilled / SIGKILL:
   Infrastructure-level kills (Kubernetes OOMKilled, exit code 137, SIGKILL)
   terminate the worker process before Python exception handling can execute.
   These cannot be caught here.  The document will remain in `processing`
   status until the operator resolves the infrastructure issue or implements
   an external watchdog / timeout sweep.

Cleanup on failure
------------------
If any chunks have already been written to Chroma or Postgres, they are
deleted before marking the document as failed.  This prevents accumulation
of partial embeddings that would corrupt retrieval results.  Cleanup errors
are logged but never re-raised — the document is always marked failed.

Logging
-------
Every failure logs:  document_id, collection_id, filename, error_code,
elapsed_time, and the full exception with stack trace (server-side only).
Stack traces are NEVER sent to the frontend.
"""

import logging
import time
import uuid

from app.core.chroma_client import get_collection_store
from app.core.config import settings
from app.core.database import SessionLocal
from app.core.embeddings import get_embedder
from app.models.document import Document, DocumentStatus
from app.repositories import chunk_repository, document_repository
from app.utils.pdf_processing import chunk_text, iter_pages
from app.utils.upload_errors import UploadErrorCode, UploadValidationError

logger = logging.getLogger(__name__)

BATCH_SIZE = 32


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _flush_batch(db, document, batch: list[dict], batch_number: int, embedder, store):
    texts = [c["content"] for c in batch]

    print(f"Encoding batch {batch_number} ({len(texts)} chunks)...")

    # -------- Embeddings --------
    t0 = time.perf_counter()
    embeddings = embedder.encode(texts).tolist()
    embedding_time = time.perf_counter() - t0

    # -------- Chroma --------
    batch_ids = [
        f"{document.id}_p{c['page_number']}_c{c['chunk_index']}"
        for c in batch
    ]

    batch_metadatas = [
        {
            "document_id": str(document.id),
            "page_number": c["page_number"],
            "chunk_index": c["chunk_index"],
        }
        for c in batch
    ]

    t0 = time.perf_counter()
    store.upsert(
        ids=batch_ids,
        embeddings=embeddings,
        documents=texts,
        metadatas=batch_metadatas,
    )
    chroma_upsert_time = time.perf_counter() - t0

    # -------- Postgres --------
    batch_rows = [
        {
            "document_id": document.id,
            "page_number": c["page_number"],
            "chunk_index": c["chunk_index"],
            "content": c["content"],
            "chroma_id": chroma_id,
        }
        for c, chroma_id in zip(batch, batch_ids)
    ]

    t0 = time.perf_counter()
    chunk_repository.bulk_create(db, batch_rows)
    postgres_insert_time = time.perf_counter() - t0

    return embedding_time, chroma_upsert_time, postgres_insert_time


def _cleanup_partial_data(db, document: Document, store) -> None:
    """
    Delete any chunks already written for this document from both Chroma and
    Postgres.  Called when ingestion fails mid-way to prevent partial/corrupt
    embeddings from poisoning future retrieval results.

    Errors inside cleanup are logged and suppressed — the caller is already
    handling a failure and must always mark the document as failed regardless
    of whether cleanup succeeds.
    """
    # --- Chroma ---
    if store is not None:
        try:
            existing = store.get(where={"document_id": str(document.id)})
            ids_to_delete = existing.get("ids", [])
            if ids_to_delete:
                store.delete(ids=ids_to_delete)
                logger.info(
                    "Cleanup: deleted %d Chroma vectors | document_id=%s",
                    len(ids_to_delete),
                    document.id,
                )
        except Exception:
            logger.exception(
                "Cleanup: failed to delete Chroma vectors | document_id=%s",
                document.id,
            )

    # --- Postgres ---
    try:
        chunk_repository.delete_by_document(db, document.id)
        db.commit()
        logger.info(
            "Cleanup: deleted Postgres chunks | document_id=%s",
            document.id,
        )
    except Exception:
        logger.exception(
            "Cleanup: failed to delete Postgres chunks | document_id=%s",
            document.id,
        )
        try:
            db.rollback()
        except Exception:
            pass


def _mark_failed(
    db,
    document: Document | None,
    error_code: UploadErrorCode,
    user_message: str,
    store,
    elapsed: float,
) -> None:
    """
    Central failure handler: cleans up partial data, then writes the failed
    status + error info to the document row.
    """
    if document is None:
        return  # deleted before ingestion ran — nothing to do

    _cleanup_partial_data(db, document, store)

    try:
        document_repository.update_status(
            db,
            document,
            DocumentStatus.failed,
            error_code=error_code.value,
            error_message=user_message,
        )
    except Exception:
        logger.exception(
            "Failed to persist failed status | document_id=%s",
            document.id,
        )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def ingest_document(document_id: str) -> None:
    """
    Runs as a FastAPI background task, AFTER the upload response has already
    been sent to the client.

    On success: document status → ready, page_count set, chunks stored in
    both Postgres (metadata) and Chroma (vectors).

    On failure: document status → failed, error_code and error_message stored,
    partial data cleaned up.
    """
    logger.info("INGESTION STARTED")
    print("INGESTION STARTED")

    db = SessionLocal()
    document: Document | None = None
    store = None
    total_start = time.perf_counter()

    try:
        document = document_repository.get_by_id(db, uuid.UUID(document_id))
        if document is None:
            logger.warning(
                "Ingestion aborted: document not found (deleted?) | document_id=%s",
                document_id,
            )
            return

        logger.info(
            "Ingestion initialised | document_id=%s collection_id=%s filename=%r",
            document.id,
            document.collection_id,
            document.filename,
        )

        # ------------------------------------------------------------------
        # Processing pipeline
        # ------------------------------------------------------------------
        pdf_loading_time = 0.0
        chunking_time = 0.0
        embedding_time = 0.0
        chroma_upsert_time = 0.0
        postgres_insert_time = 0.0
        page_count = 0
        chunk_count = 0
        batch_buffer: list[dict] = []
        embedder = None
        batch_number = 0

        page_iter = iter_pages(document.file_path)
        while True:
            t0 = time.perf_counter()
            try:
                page_number, page_text = next(page_iter)
            except StopIteration:
                pdf_loading_time += time.perf_counter() - t0
                break
            pdf_loading_time += time.perf_counter() - t0

            t0 = time.perf_counter()
            page_chunks = chunk_text(page_text, settings.CHUNK_SIZE, settings.CHUNK_OVERLAP)
            chunking_time += time.perf_counter() - t0

            page_count += 1
            for chunk_index, content in enumerate(page_chunks):
                chunk_count += 1
                batch_buffer.append(
                    {"page_number": page_number, "chunk_index": chunk_index, "content": content}
                )

                while len(batch_buffer) >= BATCH_SIZE:
                    current_batch = batch_buffer[:BATCH_SIZE]
                    del batch_buffer[:BATCH_SIZE]

                    if embedder is None:
                        print("Getting embedder...")
                        embedder = get_embedder()
                        print("Embedder loaded")
                        store = get_collection_store(str(document.collection_id))

                    batch_number += 1
                    be, bc, bp = _flush_batch(
                        db, document, current_batch, batch_number, embedder, store
                    )
                    embedding_time += be
                    chroma_upsert_time += bc
                    postgres_insert_time += bp

        if chunk_count == 0:
            # The file passed pre-flight validation but yielded no chunks —
            # this can happen with a PDF whose pages are all whitespace.
            elapsed = time.perf_counter() - total_start
            logger.warning(
                "Ingestion failed: no chunks produced | document_id=%s collection_id=%s "
                "filename=%r error_code=%s elapsed=%.2fs",
                document.id,
                document.collection_id,
                document.filename,
                UploadErrorCode.NO_EXTRACTABLE_TEXT,
                elapsed,
            )
            _mark_failed(
                db,
                document,
                UploadErrorCode.NO_EXTRACTABLE_TEXT,
                "This PDF contains only scanned images. OCR is not currently supported.",
                store,
                elapsed,
            )
            return

        print("1. Loaded PDF")
        print("2. Chunked")

        # Flush any remaining batch
        if batch_buffer:
            if embedder is None:
                print("Getting embedder...")
                embedder = get_embedder()
                print("Embedder loaded")
                store = get_collection_store(str(document.collection_id))

            batch_number += 1
            be, bc, bp = _flush_batch(
                db, document, batch_buffer, batch_number, embedder, store
            )
            embedding_time += be
            chroma_upsert_time += bc
            postgres_insert_time += bp

        print("Embeddings generated")
        print("4. Chroma done")
        print("5. Postgres done")

        # ------------------------------------------------------------------
        # Success
        # ------------------------------------------------------------------
        stage_start = time.perf_counter()
        document_repository.update_status(
            db, document, DocumentStatus.ready, page_count=page_count
        )
        print("6. Status updated")
        status_update_time = time.perf_counter() - stage_start
        total_time = time.perf_counter() - total_start

        print("DONE")
        print(f"""
        ========== INGESTION TIMINGS ==========
        PDF Loading      : {pdf_loading_time:.2f}s
        Chunking         : {chunking_time:.2f}s
        Embeddings       : {embedding_time:.2f}s
        Chroma Upsert    : {chroma_upsert_time:.2f}s
        Postgres Insert  : {postgres_insert_time:.2f}s
        Status Update    : {status_update_time:.2f}s
        Total            : {total_time:.2f}s
        ======================================
        """)

    # ------------------------------------------------------------------
    # Structured failure handling
    # ------------------------------------------------------------------
    except UploadValidationError as exc:
        elapsed = time.perf_counter() - total_start
        logger.warning(
            "Ingestion failed: validation error | document_id=%s collection_id=%s "
            "filename=%r error_code=%s elapsed=%.2fs",
            getattr(document, "id", document_id),
            getattr(document, "collection_id", "unknown"),
            getattr(document, "filename", "unknown"),
            exc.error_code,
            elapsed,
        )
        db.rollback()
        _mark_failed(db, document, exc.error_code, exc.user_message, store, elapsed)

    except MemoryError:
        # Python-catchable OOM.  Infrastructure-level OOMKilled (SIGKILL /
        # exit 137) cannot be caught — the process is already dead by then.
        elapsed = time.perf_counter() - total_start
        logger.error(
            "Ingestion failed: MemoryError | document_id=%s collection_id=%s "
            "filename=%r error_code=%s elapsed=%.2fs",
            getattr(document, "id", document_id),
            getattr(document, "collection_id", "unknown"),
            getattr(document, "filename", "unknown"),
            UploadErrorCode.RESOURCE_LIMIT,
            elapsed,
            exc_info=True,
        )
        db.rollback()
        _mark_failed(
            db,
            document,
            UploadErrorCode.RESOURCE_LIMIT,
            "Processing could not complete due to resource constraints. "
            "Please try again or upload a smaller document.",
            store,
            elapsed,
        )

    except Exception:
        elapsed = time.perf_counter() - total_start
        logger.exception(
            "Ingestion failed: unexpected error | document_id=%s collection_id=%s "
            "filename=%r error_code=%s elapsed=%.2fs",
            getattr(document, "id", document_id),
            getattr(document, "collection_id", "unknown"),
            getattr(document, "filename", "unknown"),
            UploadErrorCode.INTERNAL_ERROR,
            elapsed,
        )
        db.rollback()
        _mark_failed(
            db,
            document,
            UploadErrorCode.INTERNAL_ERROR,
            "An unexpected error occurred while processing your document. "
            "Please try again. If the problem persists, contact support.",
            store,
            elapsed,
        )

    finally:
        db.close()