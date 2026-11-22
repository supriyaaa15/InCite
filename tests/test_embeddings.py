from unittest.mock import Mock, call

import numpy as np

from app.core import embeddings
from app.core.config import settings


def test_query_matches_legacy_single_item_batch_and_reuses_model(monkeypatch):
    encode_results = np.arange(384, dtype=np.float32).reshape(1, 384) / 256
    model = Mock()
    model.encode.return_value = encode_results
    factory = Mock(return_value=model)
    monkeypatch.setattr(embeddings, "SentenceTransformer", factory)
    embeddings.get_embedder.cache_clear()
    queries = ["Where is the crossing?", "  ", "Καλημέρα"]
    try:
        for query in queries:
            legacy_vector = model.encode([query])[0].tolist()
            assert embeddings.embed_query(query) == legacy_vector
    finally:
        embeddings.get_embedder.cache_clear()

    factory.assert_called_once_with(settings.EMBEDDING_MODEL)
    assert model.encode.call_args_list == [call([query]) for query in queries for _ in range(2)]
