"""Exercise Phase 1A against migrated PostgreSQL 16 and embedded Chroma."""

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re
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

from app.core.chroma_client import get_collection_store
from app.core.config import settings
from app.core.database import SessionLocal, engine
from app.core.security import create_access_token
from app.main import app
from app.models import ChatSession, Collection, Document, Message, User
from app.models.chat import MessageRole
from app.models.document import DocumentStatus
from app.services import chat_service, comparison_service, retrieval_service


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
        command.upgrade(config, "head")
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
