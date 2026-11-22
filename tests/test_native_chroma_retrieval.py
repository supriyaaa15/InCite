"""Lock down the old vector adapter contract against disposable Chroma."""

from types import SimpleNamespace
from uuid import uuid4

import chromadb
from chromadb.api.models.Collection import Collection
from langchain_core.documents import Document
from pydantic import ValidationError
import pytest

from app.core import chroma_client
from app.services import retrieval_service
from scripts.benchmark_retrieval_chunk import _extract_chunk_id


QUERY_VECTOR = [1.0, 0.0]


@pytest.fixture
def chroma(monkeypatch, tmp_path):
    client = chromadb.PersistentClient(path=str(tmp_path / "chroma"))
    monkeypatch.setattr(chroma_client, "get_chroma_client", lambda: client)
    monkeypatch.setattr(
        retrieval_service, "get_langchain_embeddings",
        lambda: SimpleNamespace(embed_query=lambda _: QUERY_VECTOR),
    )
    return client


def test_vector_query_preserves_order_distances_and_canonical_ids(chroma):
    collection_id = uuid4()
    store = chroma_client.get_collection_store(str(collection_id))
    assert store.metadata == {"hnsw:space": "cosine"}
    texts = ["first tied chunk", "second tied chunk", "orthogonal chunk"]
    metadatas = [
        {"document_id": str(uuid4()), "page_number": 2, "chunk_index": index}
        for index in range(len(texts))
    ]
    canonical_ids = [
        f"{metadata['document_id']}_p2_c{index}" for index, metadata in enumerate(metadatas)
    ]
    stored_ids = ["legacy-storage-id", *canonical_ids[1:]]
    store.upsert(
        ids=stored_ids, documents=texts, metadatas=metadatas,
        embeddings=[QUERY_VECTOR, QUERY_VECTOR, [0.0, 1.0]],
    )
    query_args = {
        "query_embeddings": [QUERY_VECTOR],
        "n_results": 10,
        "include": ["documents", "metadatas", "distances"],
    }
    # The old adapter copied the SDK's first result batch in SDK order.
    # Chroma can choose the order of tied rows, so avoid a platform-specific tie-break.
    raw = store.query(**query_args)

    results, lookup = retrieval_service._vector_retrieve(collection_id, "query", 10)

    assert [distance for _, distance in results] == raw["distances"][0] == [0.0, 0.0, 1.0]
    assert [doc.page_content for doc, _ in results] == raw["documents"][0]
    assert [doc.metadata for doc, _ in results] == raw["metadatas"][0]
    assert all(isinstance(doc, Document) and doc.id is None for doc, _ in results)
    expected_ids = [canonical_ids[stored_ids.index(raw_id)] for raw_id in raw["ids"][0]]
    assert list(lookup) == [_extract_chunk_id(result) for result in results] == expected_ids


def test_missing_collection_is_not_created(chroma):
    assert retrieval_service._vector_retrieve(uuid4(), "query", 4) == ([], {})
    assert chroma.count_collections() == 0


def test_existing_empty_collection_returns_no_hits(chroma):
    collection_id = uuid4()
    chroma_client.get_collection_store(str(collection_id))
    assert retrieval_service._vector_retrieve(collection_id, "query", 4) == ([], {})
    assert chroma.count_collections() == 1


def test_vector_query_handles_no_filter_matches(chroma, monkeypatch):
    collection_id = uuid4()
    store = chroma_client.get_collection_store(str(collection_id))
    store.upsert(
        ids=["stored"], documents=["synthetic"], embeddings=[QUERY_VECTOR], metadatas=[{"tag": "present"}],
    )
    native_query = Collection.query

    def no_matches(collection, **kwargs):
        # Exercise a real Chroma empty result without adding a production filter option.
        return native_query(collection, **kwargs, where={"tag": "absent"})

    monkeypatch.setattr(Collection, "query", no_matches)
    assert retrieval_service._vector_retrieve(collection_id, "query", 4) == ([], {})
    assert store.count() == 1


def test_native_null_metadata_becomes_an_empty_document_mapping(chroma):
    collection_id = uuid4()
    store = chroma_client.get_collection_store(str(collection_id))
    store.upsert(ids=["no-metadata"], documents=["synthetic"], embeddings=[QUERY_VECTOR])
    results, lookup = retrieval_service._vector_retrieve(collection_id, "query", 4)
    assert len(results) == 1 and results[0][0].metadata == {}
    assert lookup == {}


def test_vector_with_missing_text_still_raises_document_validation_error(chroma):
    collection_id = uuid4()
    store = chroma_client.get_collection_store(str(collection_id))
    store.upsert(ids=["no-text"], embeddings=[QUERY_VECTOR])
    with pytest.raises(ValidationError, match="page_content"):
        retrieval_service._vector_retrieve(collection_id, "query", 4)
