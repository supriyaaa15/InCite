"""
tests/test_reranker.py
=======================
Unit tests for Phase 6 Hybrid + CrossEncoder Reranker retrieval architecture.

What is tested
--------------
1. Reranker pool size (top-15 candidates from RRF feed into CrossEncoder).
2. Query/Document text pair formatting for CrossEncoder scoring.
3. Candidate reordering (CrossEncoder score overrides RRF score order).
4. Final top-k truncation (returns top 5 reranked candidates).
5. Metadata preservation (_vector_rank, _bm25_rank, _rrf_score, _rerank_score, _retrieval_mode).
6. Non-regression of existing modes ("vector" and "hybrid").
7. Graceful fallback on reranker loading/inference failure (returns RRF top-5).

Run locally:
    python -m unittest tests/test_reranker.py -v
"""

import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

from langchain_core.documents import Document

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import settings
from app.services import retrieval_service


def _make_doc(doc_id: str, text: str, page: int = 1, chunk: int = 0) -> Document:
    return Document(
        page_content=text,
        metadata={
            "document_id": doc_id,
            "document_name": f"doc_{doc_id}.pdf",
            "page_number": page,
            "chunk_index": chunk,
        },
    )


def _get_mock_retrieval_deps():
    """
    Sets up synthetic vector and BM25 candidate pools for 20 candidates.
    """
    collection_id = uuid.uuid4()
    query = "What is the capital requirement under Basel III?"

    vector_lookup = {}
    document_rows = []
    for i in range(1, 21):
        document_id = uuid.uuid4()
        cid = f"{document_id}_p1_c0"
        doc = _make_doc(str(document_id), f"Text content for chunk {i} discussing finance rules.")
        vector_lookup[cid] = (doc, float(i) * 0.05)
        document_rows.append((document_id, doc.metadata["document_name"]))

    bm25_results = [(cid, float(20 - i)) for i, cid in enumerate(vector_lookup)]
    db = MagicMock()
    db.execute.return_value = document_rows

    return collection_id, query, vector_lookup, bm25_results, db


class TestRerankerConfig(unittest.TestCase):
    def test_reranker_settings_defaults(self):
        self.assertEqual(settings.RERANKER_MODEL, "cross-encoder/ms-marco-MiniLM-L-6-v2")
        self.assertEqual(settings.RERANK_POOL_K, 15)


class TestHybridRerankPipeline(unittest.TestCase):
    """Tests core candidate flow, reordering, truncation, and metadata in hybrid_rerank mode."""

    def test_reranker_candidate_pool_size_and_reordering(self):
        collection_id, query, vector_lookup, bm25_results, db = _get_mock_retrieval_deps()

        mock_reranker = MagicMock()
        def mock_predict(pairs):
            return [float(len(pairs) - i) for i in range(len(pairs))]

        mock_reranker.predict.side_effect = mock_predict

        with patch.object(settings, "RETRIEVAL_MODE", "hybrid_rerank"), \
             patch("app.services.retrieval_service._vector_retrieve", return_value=([], vector_lookup)), \
             patch("app.services.retrieval_service._bm25_retrieve", return_value=bm25_results), \
             patch("app.services.retrieval_service._get_reranker", return_value=mock_reranker):

            results = retrieval_service.retrieve(collection_id, query, db, top_k=5)

            # 1. Output length truncated to top_k = 5
            self.assertEqual(len(results), 5)

            # 2. Reranker received 15 pairs (RERANK_POOL_K = 15)
            self.assertTrue(mock_reranker.predict.called)
            pairs_passed = mock_reranker.predict.call_args[0][0]
            self.assertEqual(len(pairs_passed), 15)
            self.assertEqual(pairs_passed[0][0], query)

            # 3. Check metadata preservation on final top-5
            first_doc, first_score = results[0]
            self.assertEqual(first_doc.metadata["_retrieval_mode"], "hybrid_rerank")
            self.assertIn("_vector_rank", first_doc.metadata)
            self.assertIn("_bm25_rank", first_doc.metadata)
            self.assertIn("_rrf_score", first_doc.metadata)
            self.assertIn("_rerank_score", first_doc.metadata)
            self.assertEqual(first_doc.metadata["_rerank_score"], 15.0)
            self.assertEqual(first_score, 15.0)

    def test_reranker_failure_graceful_fallback(self):
        collection_id, query, vector_lookup, bm25_results, db = _get_mock_retrieval_deps()

        with patch.object(settings, "RETRIEVAL_MODE", "hybrid_rerank"), \
             patch("app.services.retrieval_service._vector_retrieve", return_value=([], vector_lookup)), \
             patch("app.services.retrieval_service._bm25_retrieve", return_value=bm25_results), \
             patch("app.services.retrieval_service._get_reranker", side_effect=RuntimeError("Model failed to load")):

            results = retrieval_service.retrieve(collection_id, query, db, top_k=5)

            self.assertEqual(len(results), 5)
            first_doc, first_score = results[0]
            self.assertEqual(first_doc.metadata["_retrieval_mode"], "hybrid_rerank")
            self.assertIsNone(first_doc.metadata["_rerank_score"])
            self.assertIsInstance(first_score, float)


class TestExistingModesNonRegression(unittest.TestCase):
    """Verifies vector and hybrid modes remain completely unchanged."""

    def test_vector_mode_unchanged(self):
        collection_id, query, vector_lookup, _, db = _get_mock_retrieval_deps()
        mock_vec_results = [(doc, dist) for doc, dist in vector_lookup.values()][:5]

        with patch.object(settings, "RETRIEVAL_MODE", "vector"), \
             patch("app.services.retrieval_service._vector_retrieve", return_value=(mock_vec_results, vector_lookup)):

            results = retrieval_service.retrieve(collection_id, query, db, top_k=5)
            self.assertEqual(len(results), 5)
            self.assertIsNone(results[0][0].metadata.get("_retrieval_mode"))

    def test_hybrid_mode_unchanged(self):
        collection_id, query, vector_lookup, bm25_results, db = _get_mock_retrieval_deps()

        with patch.object(settings, "RETRIEVAL_MODE", "hybrid"), \
             patch("app.services.retrieval_service._vector_retrieve", return_value=([], vector_lookup)), \
             patch("app.services.retrieval_service._bm25_retrieve", return_value=bm25_results), \
             patch("app.services.retrieval_service._get_reranker") as mock_get_reranker:

            results = retrieval_service.retrieve(collection_id, query, db, top_k=5)
            self.assertEqual(len(results), 5)
            self.assertFalse(mock_get_reranker.called)
            self.assertEqual(results[0][0].metadata["_retrieval_mode"], "hybrid")
            self.assertNotIn("_rerank_score", results[0][0].metadata)


if __name__ == "__main__":
    unittest.main()
