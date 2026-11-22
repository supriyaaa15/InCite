"""Exercise Phase 1A against migrated PostgreSQL 16 and embedded Chroma."""

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest


run_id = os.environ.get("INCITE_PHASE1A_RUN")
if not run_id:
    pytest.skip("Run scripts/run_phase1a_integration.py to provision PostgreSQL 16.", allow_module_level=True)

import chromadb
from chromadb.api.models.Collection import Collection as ChromaCollection
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
import httpx
from requests.adapters import HTTPAdapter
from sqlalchemy import event, select, text
from sqlalchemy.engine import make_url


repo = Path(__file__).resolve().parents[2]
scratch = Path.cwd()
database_url = os.environ.get("DATABASE_URL", "")
database = make_url(database_url)
system_id = os.environ.get("INCITE_PHASE1A_SYSTEM_ID", "")
if not (
    re.fullmatch(r"[0-9a-f]{32}", run_id) and system_id.isdecimal()
    and database.drivername == "postgresql+psycopg2" and database.host == "127.0.0.1"
    and database.port is not None and 1024 < database.port < 65536 and database.port != 5432
    and database.database == f"incite_phase1a_{run_id}" and database.username == "incite_test"
    and re.fullmatch(r"[0-9a-f]{48}", database.password or "")
    and dict(database.query) == {"sslmode": "disable", "connect_timeout": "5"}
    and scratch.name.startswith("incite-phase1a-") and not (scratch / ".env").exists()
    and os.environ.get("CHROMA_PERSIST_PATH") == str(scratch / "chroma")
    and not (scratch / "chroma").exists()
    and os.environ.get("UPLOAD_DIR") == str(scratch / "uploads")
    and os.environ.get("GROQ_API_KEY") == "synthetic-phase1a-key"
):
    raise RuntimeError("Integration tests require the disposable settings from the Phase 1A runner.")

from app.core import bm25_store
from app.core.chroma_client import get_collection_store, get_existing_collection_store
from app.core.collection_artifacts import collection_artifact_lock
from app.api import document_routes
from app.core.config import settings
from app.core.database import SessionLocal, engine
from app.core.security import create_access_token
from app.main import app
from app.models import ChatSession, Chunk, Collection, Document, Message, User
from app.models.chat import MessageRole
from app.models.document import DocumentStatus
from app.repositories import document_repository
from app.services import chat_service, comparison_service, deletion_cleanup, deletion_service, ingestion_service, retrieval_service


QUESTIONS = ("What do the notes explain?", "Compare alpha.pdf and beta.pdf", "Which PDF should I read?")
RECENT_USER = "I am studying for a synthetic quiz."
OLD_ASSISTANT = "Archived assistant reply about an unavailable source."


@pytest.fixture(scope="session", autouse=True)
def migrated_database():
    if settings.DATABASE_URL != database_url or engine.url != database:
        pytest.fail("The application resolved an unexpected database URL.")
    if Path(settings.CHROMA_PERSIST_PATH) != scratch / "chroma" or Path(settings.UPLOAD_DIR) != scratch / "uploads":
        pytest.fail("The application resolved unexpected storage paths.")
    with engine.connect() as connection:
        identity = connection.execute(text(
            "SELECT current_database(), current_user, current_setting('server_version_num')::int, "
            "system_identifier::text, version() FROM pg_control_system()"
        )).one()
        if not (
            identity[0] == database.database and identity[1] == database.username
            and 160000 <= identity[2] < 170000 and identity[3] == system_id
        ):
            pytest.fail("The host connection did not reach the verified PostgreSQL 16 container.")
        if connection.scalar(text("SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")) != 0:
            pytest.fail("Expected an empty database before applying migrations.")

    config = Config(str(repo / "alembic.ini"))
    config.set_main_option("script_location", str(repo / "alembic").replace("%", "%%"))
    try:
        command.upgrade(config, "b2c3d4e5f6a7")
        user_id, collection_id, doc_id = uuid4(), uuid4(), uuid4()
        with engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO users (id, email, hashed_password) VALUES (:id, 'migration@example.test', 'synthetic')"
            ), {"id": user_id})
            connection.execute(text(
                "INSERT INTO collections (id, user_id, name) VALUES (:id, :user_id, 'Existing')"
            ), {"id": collection_id, "user_id": user_id})
            connection.execute(text(
                "INSERT INTO documents (id, collection_id, filename, file_path, status) "
                "VALUES (:id, :collection_id, 'existing.pdf', 'synthetic.pdf', 'ready')"
            ), {"id": doc_id, "collection_id": collection_id})
        command.upgrade(config, "head")
        with SessionLocal() as db:
            assert document_repository.get_ready_document_names(db, collection_id, {doc_id}) == {doc_id: "existing.pdf"}
            collection = db.get(Collection, collection_id)
            assert collection.delete_requested_at is None and collection.cleanup_completed_at is None
        print(f"\nVerified {identity[4]}")
        print(f"Chroma {chromadb.__version__}: PersistentClient, temporary storage, cosine, 2D embeddings")
        yield
    finally:
        engine.dispose()


@pytest.fixture(autouse=True)
def offline_adapters(monkeypatch):
    def deny_http(*args, **kwargs):
        pytest.fail("The integration test attempted external HTTP.")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", deny_http)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", deny_http)
    monkeypatch.setattr(HTTPAdapter, "send", deny_http)
    monkeypatch.setattr(
        retrieval_service, "get_langchain_embeddings",
        lambda: SimpleNamespace(embed_query=lambda query: [1.0, 0.0]),
    )


@pytest.fixture(autouse=True)
def provider(monkeypatch):
    prompts = []
    responses = {
        "AnswerWithReasoning": {"answer": "Synthetic answer.", "reasoning": "Synthetic evidence."},
        "_ComparisonResult": {"sections": [], "sources": ["alpha.pdf", "beta.pdf"]},
        "_RecommendationResult": {"summary": "Synthetic recommendation.", "rankings": []},
    }

    class Provider:
        def with_structured_output(self, schema):
            def invoke(prompt):
                prompts.append((schema.__name__, prompt))
                return schema.model_validate(responses[schema.__name__])

            return SimpleNamespace(invoke=invoke)

        def invoke(self, prompt):
            pytest.fail("Unexpected plain-text provider call.")

    for service in (chat_service, comparison_service):
        monkeypatch.setattr(service, "get_llm", lambda model: Provider())
    return prompts


@pytest.fixture
def corpus():
    owner_id, other_user_id, collection_id, foreign_id, second_owned_id, session_id = [uuid4() for _ in range(6)]
    with SessionLocal() as db:
        db.add_all([
            User(id=owner_id, email=f"{owner_id}@example.test", hashed_password="synthetic"),
            User(id=other_user_id, email=f"{other_user_id}@example.test", hashed_password="synthetic"),
        ])
        db.flush()
        db.add_all([
            Collection(id=collection_id, user_id=owner_id, name="Synthetic notes"),
            Collection(id=foreign_id, user_id=other_user_id, name="Other user's notes"),
            Collection(id=second_owned_id, user_id=owner_id, name="Other owned collection"),
        ])
        db.flush()
        db.add(ChatSession(id=session_id, user_id=owner_id, collection_id=collection_id))
        db.flush()
        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        db.add_all([
            Message(session_id=session_id, role=MessageRole.user, content=RECENT_USER, created_at=start),
            Message(session_id=session_id, role=MessageRole.assistant, content=OLD_ASSISTANT, created_at=start + timedelta(seconds=1)),
        ])
        store = get_collection_store(str(collection_id))
        assert store.metadata == {"hnsw:space": "cosine"}

        def seed(key, status, *, collection=collection_id, embedding=(1.0, 0.0), legacy=False, page=1):
            doc_id = uuid4()
            if status is not None:
                db.add(Document(
                    id=doc_id, collection_id=collection, filename=f"{key}.pdf",
                    file_path="synthetic-fixture.pdf", status=status,
                ))
                db.flush()
            metadata = {"document_id": str(doc_id), "page_number": page, "chunk_index": 0}
            if not legacy:
                metadata["document_name"] = "untrusted.pdf" if key in {"alpha", "beta"} else "alpha.pdf"
            chunk_id = f"{doc_id}_p1_c0"
            keyword = " glacier" if key in {"beta", "failed", "mismatch"} else ""
            store.upsert(
                ids=[chunk_id], embeddings=[list(embedding)],
                documents=[f"Synthetic {key} evidence.{keyword}"], metadatas=[metadata],
            )
            return doc_id, chunk_id

        ready, processing, failed = DocumentStatus.ready, DocumentStatus.processing, DocumentStatus.failed
        chunks = {}
        for key, status, options in (
            ("alpha", ready, {"legacy": True, "embedding": (1.0, 0.2)}),
            ("beta", ready, {"embedding": (1.0, 0.3)}),
            ("processing", processing, {"legacy": True}),
            ("failed", failed, {}),
            ("foreign", ready, {"collection": foreign_id}),
            ("deleted", ready, {}),
            ("orphan", None, {}),
        ):
            chunks[key] = seed(key, status, **options)
        db.delete(db.get(Document, chunks["deleted"][0]))
        db.commit()
        yield SimpleNamespace(
            db=db, store=store, seed=seed, chunks=chunks, collection_id=collection_id,
            second_owned_id=second_owned_id, session_id=session_id, owner_id=owner_id, other_user_id=other_user_id,
        )


def names(results):
    return {doc.metadata["document_name"] for doc, _ in results}


def auth(user_id):
    return {"Authorization": f"Bearer {create_access_token(str(user_id))}"}


def _cleanup_fixture(corpus, key="alpha"):
    document_id, chunk_id = corpus.chunks[key]
    pdf = scratch / "uploads" / str(corpus.owner_id) / str(corpus.collection_id) / f"{document_id}.pdf"
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(b"synthetic PDF artifact")
    corpus.db.get(Document, document_id).file_path = str(pdf)
    corpus.db.add(Chunk(document_id=document_id, page_number=1, chunk_index=0,
                        content="Synthetic chunk", chroma_id=chunk_id))
    corpus.db.commit()
    return document_id, chunk_id, pdf


def _assert_cleaned(corpus, document_id, chunk_id, pdf):
    with SessionLocal() as db:
        assert db.get(Document, document_id) is None
        assert db.scalar(select(Chunk.id).where(Chunk.document_id == document_id)) is None
    assert not corpus.store.get(ids=[chunk_id])["ids"]
    assert not pdf.exists()


def test_busy_collection_does_not_starve_later_document(corpus, monkeypatch):
    blocked = []
    for index in range(16):
        key = f"busy-{index}"
        corpus.chunks[key] = corpus.seed(key, DocumentStatus.ready)
        target = _cleanup_fixture(corpus, key)
        deletion_service.request_deletion(corpus.db, corpus.owner_id, document_id=target[0])
        blocked.append(target)

    later_id = uuid4()
    later_chunk = f"{later_id}_p1_c0"
    later_pdf = scratch / "uploads" / str(corpus.owner_id) / str(corpus.second_owned_id) / f"{later_id}.pdf"
    later_pdf.parent.mkdir(parents=True)
    later_pdf.write_bytes(b"synthetic PDF")
    later_store = get_collection_store(str(corpus.second_owned_id))
    later_store.upsert(ids=[later_chunk], embeddings=[[1.0, 0.0]],
                       metadatas=[{"document_id": str(later_id)}], documents=["Synthetic later document"])
    corpus.db.add(Document(id=later_id, collection_id=corpus.second_owned_id, filename="later.pdf",
                           file_path=str(later_pdf), status=DocumentStatus.ready))
    corpus.db.flush()
    corpus.db.add(Chunk(document_id=later_id, page_number=1, chunk_index=0,
                        content="Synthetic later document", chroma_id=later_chunk))
    corpus.db.commit()
    deletion_service.request_deletion(corpus.db, corpus.owner_id, document_id=later_id)

    now = datetime.now(timezone.utc)
    monkeypatch.setattr(deletion_cleanup, "_now", lambda: now)
    with collection_artifact_lock(corpus.collection_id):
        deletion_cleanup.run_cleanup_pass()
        with SessionLocal() as db:
            assert db.get(Document, later_id) is not None
        now += timedelta(seconds=deletion_cleanup.POLL_SECONDS)
        deletion_cleanup.run_cleanup_pass()
        with SessionLocal() as db:
            assert db.get(Document, later_id) is None
            assert db.scalar(select(Chunk.id).where(Chunk.document_id == later_id)) is None
            for document_id, chunk_id, pdf in blocked:
                document = db.get(Document, document_id)
                assert document.cleanup_retry_at > now
                assert document.cleanup_attempts == 0 and document.cleanup_error is None
                assert pdf.exists()
                assert corpus.store.get(ids=[chunk_id])["ids"] == [chunk_id]
        assert not later_pdf.exists()
        assert not later_store.get(ids=[later_chunk])["ids"]

    deletion_cleanup.run_cleanup_pass()
    with SessionLocal() as db:
        assert all(db.get(Document, document_id) is not None for document_id, _, _ in blocked)
    now += timedelta(seconds=deletion_cleanup.POLL_SECONDS)
    deletion_cleanup.run_cleanup_pass()
    for target in blocked:
        _assert_cleaned(corpus, *target)


def test_cleanup_is_scoped_and_evicts_bm25(corpus):
    document_id, chunk_id, pdf = _cleanup_fixture(corpus)
    _, neighbor_id, neighbor_pdf = _cleanup_fixture(corpus, "beta")
    neighbor_store = get_collection_store(str(corpus.second_owned_id))
    neighbor_store.upsert(ids=[chunk_id], embeddings=[[1.0, 0.0]], documents=["Another collection"])
    owner_headers = auth(corpus.owner_id)
    with TestClient(app) as client:
        assert client.delete(f"/documents/{document_id}", headers=auth(corpus.other_user_id)).status_code == 404
        deletion_cleanup._cleanup_target(corpus.collection_id, document_id)
        assert pdf.exists()
        assert client.delete(f"/documents/{document_id}", headers=owner_headers).status_code == 202
        assert chunk_id in bm25_store.get_index(corpus.collection_id, corpus.store).chroma_ids
        deletion_cleanup._cleanup_target(corpus.second_owned_id, document_id)
        assert pdf.exists()
        deletion_cleanup._cleanup_target(corpus.collection_id, document_id)
        deletion_cleanup._cleanup_target(corpus.collection_id, document_id)
        assert client.delete(f"/documents/{document_id}", headers=owner_headers).status_code == 404
    _assert_cleaned(corpus, document_id, chunk_id, pdf)
    assert neighbor_pdf.exists() and corpus.store.get(ids=[neighbor_id])["ids"] == [neighbor_id]
    assert neighbor_store.get(ids=[chunk_id])["ids"] == [chunk_id]
    assert chunk_id not in bm25_store.get_index(corpus.collection_id, corpus.store).chroma_ids


def test_collection_cleanup_removes_documents_and_retains_history(corpus):
    artifacts = [_cleanup_fixture(corpus, key) for key in ("alpha", "beta", "processing", "failed")]
    neighbor = get_collection_store(str(corpus.second_owned_id))
    headers = auth(corpus.owner_id)
    url = f"/collections/{corpus.collection_id}"
    with TestClient(app) as client:
        assert client.delete(url, headers=headers).status_code == 202
        deletion_cleanup._cleanup_target(corpus.collection_id, None)
        assert get_existing_collection_store(str(corpus.collection_id)) is not None
        for document_id, chunk_id, pdf in artifacts:
            deletion_cleanup._cleanup_target(corpus.collection_id, document_id)
            _assert_cleaned(corpus, document_id, chunk_id, pdf)
        # The orphan vector belongs to this archived Chroma collection.
        assert corpus.store.get(ids=[corpus.chunks["orphan"][1]])["ids"]
        deletion_cleanup._cleanup_target(corpus.collection_id, None)
        deletion_cleanup._cleanup_target(corpus.collection_id, None)
        assert client.delete(url, headers=headers).status_code == 204
        assert client.get(url, headers=headers).status_code == 404
        history = f"/sessions/{corpus.session_id}/messages"
        assert [row["content"] for row in client.get(history, headers=headers).json()] == [RECENT_USER, OLD_ASSISTANT]
        assert client.get(history, headers=auth(corpus.other_user_id)).status_code == 404
    with SessionLocal() as db:
        assert db.get(Collection, corpus.collection_id).cleanup_completed_at is not None
        assert retrieval_service.retrieve(corpus.collection_id, "notes", db) == []
    assert get_existing_collection_store(str(corpus.collection_id)) is None
    assert get_existing_collection_store(str(corpus.second_owned_id)).name == neighbor.name
    assert not artifacts[0][2].parent.exists()


@pytest.mark.parametrize("step,when", [
    ("chroma", "before"), ("chroma", "after"),
    ("filesystem", "before"), ("filesystem", "after"), ("postgres", "before"),
])
def test_cleanup_retries_partial_work(corpus, monkeypatch, step, when):
    document_id, chunk_id, pdf = _cleanup_fixture(corpus)
    deletion_service.request_deletion(corpus.db, corpus.owner_id, document_id=document_id)
    calls = []

    def inject(original):
        def fail(*args, **kwargs):
            calls.append(step)
            if when == "after":
                original(*args, **kwargs)
            raise OSError("synthetic interruption")
        return fail

    def fail_sql(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("DELETE FROM documents WHERE"):
            calls.append(step)
            return "SELECT 1 / 0", {}
        return statement, parameters

    with monkeypatch.context() as overrides:
        if step == "chroma":
            overrides.setattr(deletion_cleanup, "_delete_document_vectors", inject(deletion_cleanup._delete_document_vectors))
        elif step == "filesystem":
            storage = deletion_cleanup.storage_service
            overrides.setattr(storage, "delete_document_pdf", inject(storage.delete_document_pdf))
        else:
            event.listen(engine, "before_cursor_execute", fail_sql, retval=True)
        try:
            deletion_cleanup._cleanup_target(corpus.collection_id, document_id)
            deletion_cleanup._cleanup_target(corpus.collection_id, document_id)
        finally:
            if step == "postgres":
                event.remove(engine, "before_cursor_execute", fail_sql)
    assert calls == [step]  # The immediate retry was deferred.
    assert pdf.exists() == (step == "chroma" or (step, when) == ("filesystem", "before"))
    assert bool(corpus.store.get(ids=[chunk_id])["ids"]) == ((step, when) == ("chroma", "before"))
    with SessionLocal.begin() as db:
        pending = db.get(Document, document_id)
        assert pending.delete_requested_at is not None and pending.cleanup_attempts == 1
        assert pending.cleanup_error.startswith(f"{step}:")
        assert pending.cleanup_retry_at > datetime.now(timezone.utc)
        pending.cleanup_retry_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    deletion_cleanup._cleanup_target(corpus.collection_id, document_id)
    _assert_cleaned(corpus, document_id, chunk_id, pdf)


def test_cleanup_accepts_already_missing_artifacts(corpus):
    document_id, chunk_id, pdf = _cleanup_fixture(corpus)
    pdf.unlink()
    corpus.store.delete(ids=[chunk_id])
    deletion_service.request_deletion(corpus.db, corpus.owner_id, document_id=document_id)
    deletion_cleanup._cleanup_target(corpus.collection_id, document_id)
    _assert_cleaned(corpus, document_id, chunk_id, pdf)


def test_cleanup_preserves_an_outside_path(corpus):
    document_id, _, pdf = _cleanup_fixture(corpus)
    outside = scratch / f"outside-{document_id}.pdf"
    outside.write_bytes(b"outside configured upload directory")
    corpus.db.get(Document, document_id).file_path = str(outside)
    corpus.db.commit()
    deletion_service.request_deletion(corpus.db, corpus.owner_id, document_id=document_id)
    deletion_cleanup._cleanup_target(corpus.collection_id, document_id)
    assert outside.read_bytes() == b"outside configured upload directory" and pdf.exists()
    with SessionLocal() as db:
        assert db.get(Document, document_id).cleanup_error == "filesystem: UnsafeUploadPath"


def test_collection_cleanup_preserves_unregistered_files(corpus):
    collection_id = corpus.second_owned_id
    leftover = scratch / "uploads" / str(corpus.owner_id) / str(collection_id) / "unregistered.pdf"
    leftover.parent.mkdir(parents=True, exist_ok=True)
    leftover.write_bytes(b"synthetic unregistered upload")
    deletion_service.request_deletion(corpus.db, corpus.owner_id, collection_id=collection_id)
    deletion_cleanup._cleanup_target(collection_id, None)
    assert leftover.read_bytes() == b"synthetic unregistered upload"
    with SessionLocal() as db:
        collection = db.get(Collection, collection_id)
        assert collection.cleanup_completed_at is None
        assert collection.cleanup_error.startswith("filesystem:")
        assert collection.cleanup_retry_at is not None


def test_cleanup_resumes_on_application_start(corpus, monkeypatch):
    document_id, chunk_id, pdf = _cleanup_fixture(corpus)
    with TestClient(app) as client:
        assert client.delete(f"/documents/{document_id}", headers=auth(corpus.owner_id)).status_code == 202
    assert pdf.exists()
    monkeypatch.setattr(settings, "DELETION_CLEANUP_ENABLED", True)
    with TestClient(app):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with SessionLocal() as db:
                if db.get(Document, document_id) is None:
                    break
            time.sleep(0.05)
        else:
            pytest.fail("Startup worker did not discover the persisted request.")
    _assert_cleaned(corpus, document_id, chunk_id, pdf)


def test_document_deletion_is_owned_durable_and_repeatable(corpus):
    document_id, chunk_id = corpus.chunks["alpha"]
    pdf = scratch / "uploads" / f"{document_id}.pdf"
    pdf.parent.mkdir(exist_ok=True)
    pdf.write_bytes(b"synthetic PDF artifact")
    document = corpus.db.get(Document, document_id)
    document.file_path = str(pdf)
    chunk = Chunk(document_id=document_id, page_number=1, chunk_index=0,
                  content="Synthetic chunk", chroma_id=chunk_id)
    corpus.db.add(chunk)
    corpus.db.commit()
    chunk_row_id = chunk.id
    url = f"/documents/{document_id}"
    headers = auth(corpus.owner_id)
    with TestClient(app) as client:
        assert client.delete(url, headers=auth(corpus.other_user_id)).status_code == 404
        corpus.db.refresh(document)
        assert document.delete_requested_at is None
        result = client.delete(url, headers=headers)
        assert result.status_code == 202 and result.json() == {"status": "cleanup_pending"}
        corpus.db.refresh(document)
        original_request = document.delete_requested_at
        assert original_request is not None
        assert client.delete(url, headers=headers).json() == result.json()
        assert client.get(url, headers=headers).status_code == 404
        listed = client.get(f"/collections/{corpus.collection_id}/documents", headers=headers).json()
        assert str(document_id) not in {row["id"] for row in listed}
    corpus.db.refresh(document)
    assert document.delete_requested_at == original_request
    assert document.status == DocumentStatus.ready
    assert pdf.exists() and corpus.store.get(ids=[chunk_id])["ids"] == [chunk_id]
    assert corpus.db.get(Chunk, chunk_row_id) is not None


@pytest.mark.parametrize("mode", ["vector", "hybrid", "hybrid_rerank"])
def test_pending_document_never_supplies_evidence(corpus, monkeypatch, mode):
    document_id, chunk_id = corpus.chunks["alpha"]
    deletion_service.request_deletion(corpus.db, corpus.owner_id, document_id=document_id)
    monkeypatch.setattr(settings, "RETRIEVAL_MODE", mode)
    ranked = []

    def predict(pairs):
        ranked.extend(pairs)
        return [1.0] * len(pairs)

    monkeypatch.setattr(retrieval_service, "_get_reranker", lambda model: SimpleNamespace(predict=predict))
    result = retrieval_service.retrieve(corpus.collection_id, "glacier", corpus.db, top_k=5)
    assert names(result) == {"beta.pdf"}
    assert corpus.store.get(ids=[chunk_id])["ids"] == [chunk_id]
    if mode == "hybrid_rerank":
        assert ranked == [("glacier", "Synthetic beta evidence. glacier")]


def test_pending_collection_rejects_new_work_and_preserves_history(corpus, provider):
    url = f"/collections/{corpus.collection_id}"
    headers = auth(corpus.owner_id)
    with TestClient(app) as client:
        assert client.delete(url, headers=auth(corpus.other_user_id)).status_code == 404
        first = client.delete(url, headers=headers)
        assert first.status_code == 202 and first.json() == {"status": "cleanup_pending"}
        with SessionLocal() as fresh:
            collection = fresh.get(Collection, corpus.collection_id)
            requested_at = collection.delete_requested_at
            assert requested_at is not None and collection.cleanup_completed_at is None
        assert client.delete(url, headers=headers).status_code == 202
        assert client.get(url, headers=headers).status_code == 404
        listed = client.get("/collections", headers=headers).json()
        assert {row["id"] for row in listed} == {str(corpus.second_owned_id)}
        assert client.post(f"{url}/chat", headers=headers, json={"message": QUESTIONS[0]}).status_code == 404
        omitted_upload = client.post(f"{url}/documents", headers=headers, files={"file": ("dummy.pdf", b"dummy")})
        assert omitted_upload.status_code == 404
        history = f"/sessions/{corpus.session_id}/messages"
        assert [row["content"] for row in client.get(history, headers=headers).json()] == [RECENT_USER, OLD_ASSISTANT]
        assert client.get(history, headers=auth(corpus.other_user_id)).status_code == 404
    with SessionLocal() as fresh:
        assert fresh.get(Collection, corpus.collection_id).delete_requested_at == requested_at
        assert fresh.get(Document, corpus.chunks["alpha"][0]).delete_requested_at is None
        assert retrieval_service.retrieve(corpus.collection_id, "glacier", fresh) == []
    assert provider == []


def test_failed_deletion_transaction_leaves_document_active(corpus):
    document_id, _ = corpus.chunks["alpha"]

    def fail_update(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("UPDATE documents SET delete_requested_at"):
            return "SELECT 1 / 0", {}
        return statement, parameters

    event.listen(engine, "before_cursor_execute", fail_update, retval=True)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.delete(f"/documents/{document_id}", headers=auth(corpus.owner_id))
            assert response.status_code == 500
    finally:
        event.remove(engine, "before_cursor_execute", fail_update)
    with SessionLocal() as fresh:
        assert fresh.get(Document, document_id).delete_requested_at is None
        assert document_repository.get_ready_document_names(fresh, corpus.collection_id, {document_id}) == {
            document_id: "alpha.pdf"
        }


@pytest.mark.parametrize("target", ["document", "collection"])
def test_stale_worker_cannot_publish_ready(corpus, target):
    document_id, _ = corpus.chunks["processing"]
    document = corpus.db.get(Document, document_id)
    args = {"document_id": document_id} if target == "document" else {"collection_id": corpus.collection_id}
    with SessionLocal() as separate:
        deletion_service.request_deletion(separate, corpus.owner_id, **args)
    assert document.status == DocumentStatus.processing
    assert not document_repository.finish_processing(corpus.db, document, DocumentStatus.ready, page_count=1)
    with SessionLocal() as fresh:
        assert fresh.get(Document, document_id).status == DocumentStatus.processing


def test_in_flight_chroma_batch_cannot_register_sql_chunks_after_delete(corpus, monkeypatch):
    document_id, chunk_id = corpus.chunks["processing"]
    pdf = scratch / "uploads" / str(corpus.owner_id) / str(corpus.collection_id) / f"{document_id}.pdf"
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(b"synthetic PDF artifact")
    corpus.db.get(Document, document_id).file_path = str(pdf)
    corpus.db.commit()
    calls = []
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(deletion_cleanup, "_now", lambda: now)

    def upsert(**kwargs):
        calls.extend(kwargs["ids"])
        with SessionLocal() as separate:
            separate.execute(text("SET LOCAL lock_timeout = '2s'"))
            deletion_service.request_deletion(separate, corpus.owner_id, document_id=document_id)
        deletion_cleanup._cleanup_target(corpus.collection_id, document_id)
        with SessionLocal() as fresh:
            assert fresh.get(Document, document_id) is not None
        assert pdf.exists() and corpus.store.get(ids=kwargs["ids"])["ids"] == kwargs["ids"]
        corpus.store.upsert(**kwargs)  # Complete the in-flight write after DELETE.

    def pages(path):
        yield 1, "An embedding batch written while deletion is requested."

    monkeypatch.setattr(ingestion_service, "get_collection_store", lambda key: SimpleNamespace(upsert=upsert))
    monkeypatch.setattr(ingestion_service, "iter_pages", pages)
    monkeypatch.setattr(ingestion_service, "get_embedder", lambda: SimpleNamespace(
        encode=lambda texts: SimpleNamespace(tolist=lambda: [[1.0, 0.0] for _ in texts]),
    ))
    ingestion_service.ingest_document(str(document_id))
    assert calls and corpus.store.get(ids=calls)["ids"] == calls
    with SessionLocal() as fresh:
        document = fresh.get(Document, document_id)
        assert document.delete_requested_at is not None and document.status == DocumentStatus.processing
        assert fresh.scalar(select(Chunk.id).where(Chunk.document_id == document_id)) is None
        now = document.cleanup_retry_at
    deletion_cleanup._cleanup_target(corpus.collection_id, document_id)
    _assert_cleaned(corpus, document_id, chunk_id, pdf)
    calls_before = calls.copy()
    ingestion_service.ingest_document(str(document_id))
    assert calls == calls_before


def test_upload_rechecks_collection_after_validation(corpus, monkeypatch):
    pdf = scratch / "uploads" / f"{uuid4()}.pdf"

    def save_file(*args):
        pdf.parent.mkdir(exist_ok=True)
        pdf.write_bytes(b"unregistered synthetic upload")
        return str(pdf)

    def validate(path):
        with SessionLocal() as separate:
            separate.execute(text("SET LOCAL lock_timeout = '2s'"))
            deletion_service.request_deletion(separate, corpus.owner_id, collection_id=corpus.collection_id)

    monkeypatch.setattr(document_routes.storage_service, "save_file", save_file)
    monkeypatch.setattr(document_routes, "validate_upload", lambda file: None)
    monkeypatch.setattr(document_routes, "validate_pdf_file", validate)
    with TestClient(app) as client:
        result = client.post(f"/collections/{corpus.collection_id}/documents", headers=auth(corpus.owner_id),
                             files={"file": ("racing.pdf", b"synthetic")})
    assert result.status_code == 404
    assert not pdf.exists()
    with SessionLocal() as fresh:
        assert fresh.scalar(select(Document.id).where(
            Document.collection_id == corpus.collection_id, Document.filename == "racing.pdf"
        )) is None


def test_vector_filters_stale_and_cross_collection_chunks(corpus, monkeypatch):
    monkeypatch.setattr(settings, "RETRIEVAL_MODE", "vector")
    _, raw = retrieval_service._vector_retrieve(corpus.collection_id, "notes", 8)
    assert set(raw) == {chunk_id for _, chunk_id in corpus.chunks.values()}
    assert "document_name" not in raw[corpus.chunks["alpha"][1]][0].metadata
    assert raw[corpus.chunks["beta"][1]][0].metadata["document_name"] == "untrusted.pdf"
    assert raw[corpus.chunks["foreign"][1]][0].metadata["document_name"] == "alpha.pdf"
    results = retrieval_service.retrieve(corpus.collection_id, "notes", corpus.db, top_k=2)
    assert [(doc.page_content, doc.metadata["document_name"]) for doc, _ in results] == [
        ("Synthetic alpha evidence.", "alpha.pdf"), ("Synthetic beta evidence. glacier", "beta.pdf"),
    ]
    assert (scratch / "chroma" / "chroma.sqlite3").is_file()


@pytest.mark.parametrize("mode", ["hybrid", "hybrid_rerank"])
def test_hybrid_checks_bm25_only_ids_and_hydrated_metadata(corpus, monkeypatch, mode):
    monkeypatch.setattr(settings, "RETRIEVAL_MODE", mode)
    _, beta = corpus.chunks["beta"]
    _, failed = corpus.chunks["failed"]
    corpus.store.update(ids=[beta, failed], embeddings=[[-1.0, 0.0], [-1.0, 0.1]])
    _, mismatch = corpus.seed("mismatch", DocumentStatus.ready, embedding=(-1.0, -0.1), page=2)
    for index in range(3):
        corpus.seed(f"filler{index}", None)
    corpus.db.commit()
    _, vector = retrieval_service._vector_retrieve(corpus.collection_id, "glacier", 8)
    bm25 = dict(retrieval_service._bm25_retrieve(corpus.collection_id, "glacier", 8, None))
    assert {beta, failed, mismatch} <= bm25.keys() - vector.keys()
    assert corpus.chunks["alpha"][1] in vector

    hydrated = []
    original_get = ChromaCollection.get

    def track_get(store, *args, **kwargs):
        hydrated.extend(kwargs.get("ids") or [])
        return original_get(store, *args, **kwargs)

    pairs = []

    def predict(candidates):
        pairs.extend(candidates)
        return [1.0] * len(candidates)

    monkeypatch.setattr(ChromaCollection, "get", track_get)
    monkeypatch.setattr(retrieval_service, "_get_reranker", lambda model: SimpleNamespace(predict=predict))
    results = retrieval_service.retrieve(corpus.collection_id, "glacier", corpus.db, top_k=2)
    assert names(results) == {"alpha.pdf", "beta.pdf"}
    assert set(hydrated) == {beta, mismatch}
    hydrated_beta = next(doc for doc, _ in results if doc.metadata["document_name"] == "beta.pdf")
    assert hydrated_beta.metadata["_vector_rank"] is None
    assert hydrated_beta.metadata["_bm25_rank"] is not None
    if mode == "hybrid_rerank":
        assert set(pairs) == {("glacier", "Synthetic alpha evidence."), ("glacier", "Synthetic beta evidence. glacier")}


def test_http_authorization_and_session_binding(corpus, monkeypatch, provider):
    def unexpected_search(*args, **kwargs):
        pytest.fail("An unauthorized or mismatched chat reached retrieval.")

    monkeypatch.setattr(retrieval_service, "retrieve", unexpected_search)
    data = {"message": QUESTIONS[0], "session_id": str(corpus.session_id)}
    with TestClient(app) as client:
        denied = client.post(f"/collections/{corpus.collection_id}/chat", json=data, headers=auth(corpus.other_user_id))
        assert denied.status_code == 404
        assert denied.json()["detail"] == "Collection not found"
        response = client.post(f"/collections/{corpus.second_owned_id}/chat", json=data, headers=auth(corpus.owner_id))
        assert response.status_code == 400
        assert response.json()["detail"] == "session_id belongs to a different collection"
        assert client.get(f"/sessions/{corpus.session_id}/messages", headers=auth(corpus.other_user_id)).status_code == 404
    assert provider == []


@pytest.mark.parametrize("question,schema", zip(QUESTIONS, ("AnswerWithReasoning", "_ComparisonResult", "_RecommendationResult")))
def test_http_prompts_use_fresh_evidence_and_user_history(corpus, provider, monkeypatch, question, schema):
    monkeypatch.setattr(settings, "RETRIEVAL_MODE", "vector")
    context_inputs = []
    build_citations = retrieval_service.build_citations

    def record_context(retrieved):
        context_inputs.append(names(retrieved))
        return build_citations(retrieved)

    monkeypatch.setattr(retrieval_service, "build_citations", record_context)
    headers = auth(corpus.owner_id)
    with TestClient(app) as client:
        response = client.post(
            f"/collections/{corpus.collection_id}/chat", headers=headers,
            json={"message": question, "session_id": str(corpus.session_id)},
        )
        assert response.status_code == 200, response.text
        history = client.get(f"/sessions/{corpus.session_id}/messages", headers=headers)
    assert context_inputs == [{"alpha.pdf", "beta.pdf"}]
    assert [name for name, _ in provider] == [schema]
    _, prompt = provider[0]
    for allowed in (RECENT_USER, "alpha.pdf", "beta.pdf", "Synthetic alpha evidence.", "Synthetic beta evidence."):
        assert allowed in prompt
    assert OLD_ASSISTANT not in prompt
    assert "untrusted.pdf" not in prompt
    for key in ("processing", "failed", "foreign", "deleted", "orphan"):
        assert f"Synthetic {key} evidence." not in prompt
    body = response.json()
    if schema == "AnswerWithReasoning":
        assert {citation["document_name"] for citation in body["citations"]} == {"alpha.pdf", "beta.pdf"}
    assert history.status_code == 200
    assert [entry["content"] for entry in history.json()] == [RECENT_USER, OLD_ASSISTANT, question, body["message"]]


@pytest.mark.parametrize("question", QUESTIONS)
def test_empty_evidence_refuses_without_provider(corpus, monkeypatch, provider, question):
    monkeypatch.setattr(settings, "RETRIEVAL_MODE", "hybrid")
    beta = corpus.chunks["beta"][1]
    assert beta in dict(retrieval_service._bm25_retrieve(corpus.collection_id, "glacier", 8, None))
    for key in ("alpha", "beta"):
        corpus.db.get(Document, corpus.chunks[key][0]).status = DocumentStatus.failed
    corpus.db.commit()
    assert corpus.store.count() == len(corpus.chunks)
    result = chat_service.send_message(corpus.db, corpus.owner_id, corpus.collection_id, question, corpus.session_id)
    assert result[1] == chat_service._no_confidence_answer()[0]
    assert result[3] == []
    assert provider == []


def test_postgres_eligibility_error_rolls_back_and_refuses(corpus, monkeypatch, provider):
    monkeypatch.setattr(settings, "RETRIEVAL_MODE", "vector")
    errors = []

    def fail_eligibility(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT documents.id, documents.filename "):
            return "SELECT 1 / 0", {}
        return statement, parameters

    def observe_error(context):
        errors.append(context.original_exception.pgcode)

    event.listen(engine, "before_cursor_execute", fail_eligibility, retval=True)
    event.listen(engine, "handle_error", observe_error)
    try:
        result = chat_service.send_message(corpus.db, corpus.owner_id, corpus.collection_id, QUESTIONS[0], corpus.session_id)
    finally:
        event.remove(engine, "handle_error", observe_error)
        event.remove(engine, "before_cursor_execute", fail_eligibility)
    assert errors == ["22012"]  # PostgreSQL division_by_zero put the transaction in an aborted state.
    assert result[1] == chat_service._no_confidence_answer()[0]
    assert result[3] == []
    assert provider == []
    assert corpus.db.scalar(select(Message.content).where(Message.session_id == corpus.session_id).order_by(Message.created_at.desc()).limit(1)) == result[1]
