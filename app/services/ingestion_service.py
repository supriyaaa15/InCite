import uuid

from app.core.chroma_client import get_collection_store
from app.core.config import settings
from app.core.database import SessionLocal
from app.core.embeddings import get_embedder
from app.models.document import DocumentStatus
from app.repositories import chunk_repository, document_repository
from app.utils.pdf_processing import chunk_text, load_pages


def ingest_document(document_id: str) -> None:
    """
    Runs as a FastAPI background task, AFTER the upload response has
    already been sent to the client — so it needs its own DB session,
    since the request-scoped one from get_db() is already closed by then.

    On success: document status -> ready, page_count set, chunks stored
    in both Postgres (metadata) and Chroma (vectors).
    On failure: document status -> failed, so the frontend can show that
    instead of leaving it stuck on "processing" forever.
    """
    db = SessionLocal()
    document = None
    try:
        document = document_repository.get_by_id(db, uuid.UUID(document_id))
        if document is None:
            return  # deleted before ingestion ran — nothing to do

        pages = load_pages(document.file_path)

        all_chunks = []
        for page_number, page_text in pages:
            page_chunks = chunk_text(page_text, settings.CHUNK_SIZE, settings.CHUNK_OVERLAP)
            for chunk_index, content in enumerate(page_chunks):
                all_chunks.append(
                    {"page_number": page_number, "chunk_index": chunk_index, "content": content}
                )

        if not all_chunks:
            document_repository.update_status(db, document, DocumentStatus.failed)
            return

        embedder = get_embedder()
        texts = [c["content"] for c in all_chunks]
        embeddings = embedder.encode(texts).tolist()

        store = get_collection_store(str(document.collection_id))
        ids = [f"{document.id}_p{c['page_number']}_c{c['chunk_index']}" for c in all_chunks]
        metadatas = [
            {
                "document_id": str(document.id),
                "page_number": c["page_number"],
                "chunk_index": c["chunk_index"],
            }
            for c in all_chunks
        ]
        store.upsert(ids=ids, embeddings=embeddings, documents=texts, metadatas=metadatas)

        chunk_rows = [
            {
                "document_id": document.id,
                "page_number": c["page_number"],
                "chunk_index": c["chunk_index"],
                "content": c["content"],
                "chroma_id": chroma_id,
            }
            for c, chroma_id in zip(all_chunks, ids)
        ]
        chunk_repository.bulk_create(db, chunk_rows)

        document_repository.update_status(
            db, document, DocumentStatus.ready, page_count=len(pages)
        )

    except Exception:
        # Mark the document failed so the frontend has something concrete
        # to show instead of a permanently stuck "processing" status.
        # Re-raised so the traceback still shows up in the app logs.
        if document is not None:
            document_repository.update_status(db, document, DocumentStatus.failed)
        raise
    finally:
        db.close()
