"""Retry physical deletion from the durable PostgreSQL request records."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import logging
import re

from sqlalchemy import delete, func, or_, select

from app.core import bm25_store
from app.core.chroma_client import get_chroma_client, get_existing_collection_store
from app.core.collection_artifacts import collection_artifact_lock
from app.core.config import settings
from app.core.database import SessionLocal
from app.models import Chunk, Collection, Document
from app.repositories import collection_repository, document_repository
from app.services.storage_service import storage_service


logger = logging.getLogger(__name__)
POLL_SECONDS = 2
BATCH_SIZE = 16
VECTOR_BATCH_SIZE = 256


def _now():
    return datetime.now(timezone.utc)


def _due_targets(now):
    documents = (
        select(Document.collection_id, Document.id)
        .join(Collection)
        .where(
            or_(Document.delete_requested_at.is_not(None), Collection.delete_requested_at.is_not(None)),
            Collection.cleanup_completed_at.is_(None),
            or_(Document.cleanup_retry_at.is_(None), Document.cleanup_retry_at <= now),
        )
        .order_by(func.coalesce(Document.cleanup_retry_at, Document.delete_requested_at, Collection.delete_requested_at),
                  Document.id)
        .limit(BATCH_SIZE)
    )
    collections = (
        select(Collection.id)
        .where(
            Collection.delete_requested_at.is_not(None), Collection.cleanup_completed_at.is_(None),
            or_(Collection.cleanup_retry_at.is_(None), Collection.cleanup_retry_at <= now),
            ~select(Document.id).where(Document.collection_id == Collection.id).exists(),
        )
        .order_by(func.coalesce(Collection.cleanup_retry_at, Collection.delete_requested_at), Collection.id)
        .limit(BATCH_SIZE)
    )
    with SessionLocal() as db:
        targets = list(db.execute(documents).tuples())
        targets.extend((key, None) for key in db.scalars(collections))
        return targets


def _pending_target(db, collection_id, document_id, now, *, for_update=False):
    if for_update:
        collection = collection_repository.lock_by_id(db, collection_id)
    else:
        collection = db.get(Collection, collection_id)
    if collection is None or collection.cleanup_completed_at is not None:
        return None
    if document_id is None:
        if collection.delete_requested_at is None or db.scalar(
            select(Document.id).where(Document.collection_id == collection_id).limit(1)
        ) is not None:
            return None
        target = collection
    else:
        if for_update:
            target = document_repository.lock_by_id(db, document_id, collection_id)
        else:
            target = db.get(Document, document_id)
        if target is None or target.collection_id != collection_id or (
            target.delete_requested_at is None and collection.delete_requested_at is None
        ):
            return None
    if target.cleanup_retry_at is not None and target.cleanup_retry_at > now:
        return None
    return collection, target


def _delete_document_vectors(collection_id, document_id, known_ids):
    store = get_existing_collection_store(str(collection_id))
    if store is None:
        return
    pattern = re.compile(rf"{document_id}_p[0-9]+_c[0-9]+")
    selected = store.get(where={"document_id": str(document_id)}, include=[])["ids"]
    ids = sorted(set(selected).union(known_ids))
    if any(not pattern.fullmatch(chunk_id) for chunk_id in ids):
        raise ValueError("Ambiguous Chroma chunk identity. Manual review required.")
    for start in range(0, len(ids), VECTOR_BATCH_SIZE):
        batch = ids[start:start + VECTOR_BATCH_SIZE]
        found = store.get(ids=batch, include=["metadatas"])
        if any((metadata or {}).get("document_id", str(document_id)) != str(document_id)
               for metadata in found["metadatas"]):
            raise ValueError("Conflicting Chroma document metadata. Manual review required.")
        if found["ids"]:
            store.delete(ids=found["ids"])
            if store.get(ids=found["ids"], include=[])["ids"]:
                raise RuntimeError("Chroma still contains deleted chunks.")


def _delete_collection_vectors(collection_id):
    if get_existing_collection_store(str(collection_id)) is not None:
        get_chroma_client().delete_collection(name=f"collection_{collection_id}")


def _record_failure(collection_id, document_id, step, error):
    now = _now()
    with SessionLocal.begin() as db:
        pending = _pending_target(db, collection_id, document_id, now, for_update=True)
        if pending is None:
            return
        _, target = pending
        target.cleanup_attempts += 1
        delay = min(300, 5 * 2 ** min(target.cleanup_attempts - 1, 6))
        target.cleanup_retry_at = now + timedelta(seconds=delay)
        target.cleanup_error = f"{step}: {type(error).__name__}"


def _cleanup_target(collection_id, document_id):
    with collection_artifact_lock(collection_id, wait=False) as acquired:
        if not acquired:
            return
        step = "postgres"
        try:
            with SessionLocal() as db:
                pending = _pending_target(db, collection_id, document_id, _now())
                if pending is None:
                    return
                collection, target = pending
                owner_id = collection.user_id
                if document_id is not None:
                    file_path = target.file_path
                    known_ids = list(db.scalars(select(Chunk.chroma_id).where(Chunk.document_id == document_id)))
                    other_paths = list(db.scalars(select(Document.file_path).where(
                        Document.id != document_id,
                        or_(Document.collection_id == collection_id, Document.file_path == file_path),
                    )))

            step = "chroma"
            # A partial deletion can still change the corpus. Evict even on error.
            try:
                if document_id is None:
                    _delete_collection_vectors(collection_id)
                else:
                    _delete_document_vectors(collection_id, document_id, known_ids)
            finally:
                bm25_store.invalidate(collection_id)

            step = "filesystem"
            if document_id is None:
                storage_service.remove_empty_collection_dir(owner_id, collection_id)
            else:
                storage_service.delete_document_pdf(owner_id, collection_id, file_path, other_paths)

            step = "postgres"
            with SessionLocal.begin() as db:
                pending = _pending_target(db, collection_id, document_id, _now(), for_update=True)
                if pending is None:
                    return
                collection, target = pending
                if collection.user_id != owner_id or (document_id is not None and target.file_path != file_path):
                    raise RuntimeError("Cleanup record changed during the attempt.")
                if document_id is None:
                    collection.cleanup_completed_at = _now()
                    collection.cleanup_retry_at = None
                    collection.cleanup_error = None
                else:
                    db.execute(delete(Document).where(Document.id == document_id))
        except Exception as exc:
            logger.exception("Deletion cleanup failed at %s | collection=%s document=%s",
                             step, collection_id, document_id)
            _record_failure(collection_id, document_id, step, exc)


def run_cleanup_pass():
    for collection_id, document_id in _due_targets(_now()):
        try:
            _cleanup_target(collection_id, document_id)
        except Exception as exc:
            logger.exception("Could not run or record cleanup | collection=%s document=%s",
                             collection_id, document_id)
            try:
                _record_failure(collection_id, document_id, "worker", exc)
            except Exception:
                logger.exception("PostgreSQL could not record the cleanup retry")


async def _run_worker(stop):
    while not stop.is_set():
        try:
            await asyncio.to_thread(run_cleanup_pass)
        except Exception:
            logger.exception("Could not discover pending deletion cleanup")
        try:
            await asyncio.wait_for(stop.wait(), timeout=POLL_SECONDS)
        except TimeoutError:
            pass


@asynccontextmanager
async def cleanup_lifespan(app):
    if not settings.DELETION_CLEANUP_ENABLED:
        yield
        return
    stop = asyncio.Event()
    worker = asyncio.create_task(_run_worker(stop))
    try:
        yield
    finally:
        stop.set()
        await worker
