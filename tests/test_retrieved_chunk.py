from pydantic import ValidationError
import pytest

from app.core.retrieved_chunk import RetrievedChunk


@pytest.mark.parametrize("text", [None, 42])
def test_chunk_rejects_invalid_text(text):
    with pytest.raises(ValidationError, match="page_content"):
        RetrievedChunk(page_content=text)


def test_chunk_requires_text():
    with pytest.raises(ValidationError, match="page_content"):
        RetrievedChunk()


@pytest.mark.parametrize("metadata", [None, [("page", 2)]])
def test_chunk_requires_mapping_metadata(metadata):
    with pytest.raises(ValidationError, match="metadata"):
        RetrievedChunk("text", metadata=metadata)


def test_chunk_metadata_is_copied_and_mutable():
    metadata = {"page_number": 2}
    chunk = RetrievedChunk("text", metadata=metadata)
    equal_chunk = RetrievedChunk("text", metadata=metadata)
    assert chunk == equal_chunk and chunk is not equal_chunk
    assert chunk.metadata is not metadata

    metadata["page_number"] = 8
    chunk.metadata["document_name"] = "from-sql.pdf"
    assert chunk.metadata == {"page_number": 2, "document_name": "from-sql.pdf"}
    assert equal_chunk.metadata == {"page_number": 2}
    assert chunk != equal_chunk


def test_chunk_defaults_and_numeric_id():
    first, second = RetrievedChunk("text"), RetrievedChunk("text")
    first.metadata["page_number"] = 2
    assert first.id is None and second.id is None
    assert second.metadata == {}
    assert RetrievedChunk("text", id=17).id == "17"
