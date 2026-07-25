import logging
import time
import uuid

from app.core.chroma_client import get_collection_store
from app.core.config import settings
from app.core.database import SessionLocal
from app.core.embeddings import get_embedder
from app.models.document import DocumentStatus
from app.repositories import chunk_repository, document_repository
from app.utils.pdf_processing import chunk_text, iter_pages

logger = logging.getLogger(__name__)

BATCH_SIZE = 32


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

def ingest_document(document_id: str) -> None:

    logger.info("INGESTION STARTED")
    print("INGESTION STARTED")
    """
    Runs as a FastAPI background task, AFTER the upload response has
    already been sent to the client — so it needs its own DB session,
    since the request-scoped one from get_db() is already closed by then.

    On success: document status -> ready, page_count set, chunks stored
    in both Postgres (metadata) and Chroma (vectors).
    On failure: document status -> failed, so the frontend can show that
    instead of leaving it stuck on "processing" forever.

    Timing instrumentation: wraps each stage with time.perf_counter() and
    logs a readable summary via the standard logger, backend-only (never
    part of the API response) — for diagnosing exactly which stage is
    slow, e.g. local Docker vs. the deployed free-tier instance. Only
    logged on successful completion, per the early-return paths below.
    """
    db = SessionLocal() 
    document = None
    total_start = time.perf_counter()
    try:
        document = document_repository.get_by_id(db, uuid.UUID(document_id))
        if document is None:
            return  # deleted before ingestion ran — nothing to do

        pdf_loading_time = 0.0
        chunking_time = 0.0
        embedding_time = 0.0
        chroma_upsert_time = 0.0
        postgres_insert_time = 0.0
        page_count = 0
        chunk_count = 0
        batch_buffer: list[dict] = []
        embedder = None
        store = None
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
                    batch_embedding_time, batch_chroma_time, batch_postgres_time = _flush_batch(
                        db, document, current_batch, batch_number, embedder, store
                    )
                    embedding_time += batch_embedding_time
                    chroma_upsert_time += batch_chroma_time
                    postgres_insert_time += batch_postgres_time

        print("1. Loaded PDF")
        print("2. Chunked")

        if chunk_count == 0:
            document_repository.update_status(db, document, DocumentStatus.failed)
            return

        if batch_buffer:
            if embedder is None:
                print("Getting embedder...")
                embedder = get_embedder()
                print("Embedder loaded")
                store = get_collection_store(str(document.collection_id))

            batch_number += 1
            batch_embedding_time, batch_chroma_time, batch_postgres_time = _flush_batch(
                db, document, batch_buffer, batch_number, embedder, store
            )
            embedding_time += batch_embedding_time
            chroma_upsert_time += batch_chroma_time
            postgres_insert_time += batch_postgres_time

        print("Embeddings generated")
        print("4. Chroma done")
        print("5. Postgres done")

        stage_start = time.perf_counter()
        document_repository.update_status(
            db, document, DocumentStatus.ready, page_count=page_count
        )
        print("6. Status updated")
        status_update_time = time.perf_counter() - stage_start

        print("DONE")

        total_time = time.perf_counter() - total_start
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
        # logger.info(
        #     "\n========== INGESTION TIMINGS ==========\n"
        #     f"PDF Loading       : {pdf_loading_time:.2f}s\n"
        #     f"Chunking          : {chunking_time:.2f}s\n"
        #     f"Embeddings        : {embedding_time:.2f}s\n"
        #     f"Chroma Upsert     : {chroma_upsert_time:.2f}s\n"
        #     f"Postgres Chunks   : {postgres_insert_time:.2f}s\n"
        #     f"Status Update     : {status_update_time:.2f}s\n"
        #     "---------------------------------------\n"
        #     f"Total             : {total_time:.2f}s\n"
        #     "========================================"
        # )

    except Exception:

        db.rollback()
        # Mark the document failed so the frontend has something concrete
        # to show instead of a permanently stuck "processing" status.
        # Re-raised so the traceback still shows up in the app logs.
        if document is not None:
            document_repository.update_status(db, document, DocumentStatus.failed)
        raise
    finally:
        db.close()