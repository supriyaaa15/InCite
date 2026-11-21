"""
tests/test_rrf_fusion.py
========================
Unit tests for Phase 3 independent candidate-pool Hybrid Retrieval.

What is tested
--------------
These tests validate the correctness of _rrf_fuse(), which is the only
function whose semantics changed in Phase 3 (the other change, fetching
BM25-only Documents from Chroma, is integration-tested separately via
the phase3_sanity_check script because it requires a live Chroma instance).

Each test is named with the correctness property it protects so the
connection between test and design decision is explicit.

Run with:
    python -m unittest tests/test_rrf_fusion.py -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langchain_core.documents import Document

from app.services.retrieval_service import _rrf_fuse


def _doc(text: str = "content", **meta) -> Document:
    """Build a minimal LangChain Document for test vectors."""
    return Document(page_content=text, metadata=meta)


def _make_vector_lookup(*chroma_ids: str) -> dict:
    """
    Build a vector_lookup dict in rank order (first arg = rank 1).
    dict insertion order is the rank signal used by _rrf_fuse().
    """
    return {cid: (_doc(), float(i)) for i, cid in enumerate(chroma_ids)}


def _make_bm25_results(*chroma_ids: str) -> list:
    """
    Build a bm25_results list in rank order (first arg = rank 1).
    Scores are synthetic — only the order matters for RRF.
    """
    return [(cid, float(len(chroma_ids) - i)) for i, cid in enumerate(chroma_ids)]


RRF_K = 60   # matches settings.BM25_RRF_K


class TestT1VectorOnlySurvives(unittest.TestCase):
    def test_vector_only_candidate_in_output(self):
        vec = _make_vector_lookup("cid_v1", "cid_v2")
        bm25 = _make_bm25_results("cid_b1")   # entirely different ID

        result = _rrf_fuse(vec, bm25, k=RRF_K, top_k=5)
        result_ids = [r[0] for r in result]

        self.assertIn("cid_v1", result_ids)
        self.assertIn("cid_v2", result_ids)

    def test_vector_only_rank_is_populated(self):
        vec = _make_vector_lookup("cid_v1")
        bm25 = _make_bm25_results("cid_b1")

        result = _rrf_fuse(vec, bm25, k=RRF_K, top_k=5)
        v_entry = next(r for r in result if r[0] == "cid_v1")
        _cid, _rrf, v_rank, b_rank = v_entry

        self.assertEqual(v_rank, 1)
        self.assertIsNone(b_rank)

    def test_vector_only_rrf_score_is_correct(self):
        vec = _make_vector_lookup("cid_v1")
        bm25 = []

        result = _rrf_fuse(vec, bm25, k=RRF_K, top_k=5)
        _cid, rrf_score, v_rank, b_rank = result[0]

        expected = 1.0 / (RRF_K + 1)
        self.assertAlmostEqual(rrf_score, expected, places=7)


class TestT2BM25OnlySurvives(unittest.TestCase):
    def test_bm25_only_candidate_in_output(self):
        vec = _make_vector_lookup("cid_v1")
        bm25 = _make_bm25_results("cid_b1")

        result = _rrf_fuse(vec, bm25, k=RRF_K, top_k=5)
        result_ids = [r[0] for r in result]

        self.assertIn("cid_b1", result_ids)

    def test_bm25_only_rank_is_populated(self):
        vec = _make_vector_lookup("cid_v1")
        bm25 = _make_bm25_results("cid_b1")

        result = _rrf_fuse(vec, bm25, k=RRF_K, top_k=5)
        b_entry = next(r for r in result if r[0] == "cid_b1")
        _cid, _rrf, v_rank, b_rank = b_entry

        self.assertEqual(b_rank, 1)
        self.assertIsNone(v_rank)

    def test_bm25_only_rrf_score_is_correct(self):
        bm25 = _make_bm25_results("cid_b1")

        result = _rrf_fuse({}, bm25, k=RRF_K, top_k=5)
        _cid, rrf_score, _, _ = result[0]

        expected = 1.0 / (RRF_K + 1)
        self.assertAlmostEqual(rrf_score, expected, places=7)

    def test_bm25_only_can_outscore_vector_only(self):
        vec = _make_vector_lookup(*[f"cid_v{i}" for i in range(1, 21)])
        bm25 = _make_bm25_results("cid_b1")

        result = _rrf_fuse(vec, bm25, k=RRF_K, top_k=21)
        scores = {r[0]: r[1] for r in result}

        bm25_score = scores["cid_b1"]
        vec_last_score = scores["cid_v20"]

        self.assertGreater(bm25_score, vec_last_score)


class TestT3DualContribution(unittest.TestCase):
    def test_dual_candidate_outscores_single_list(self):
        common_id = "cid_common"
        vec = _make_vector_lookup(common_id, "cid_v2")
        bm25 = _make_bm25_results(common_id, "cid_b2")

        result = _rrf_fuse(vec, bm25, k=RRF_K, top_k=5)
        scores = {r[0]: r[1] for r in result}

        dual_score = scores[common_id]
        single_v_score = scores.get("cid_v2", 0.0)
        single_b_score = scores.get("cid_b2", 0.0)

        self.assertGreater(dual_score, single_v_score)
        self.assertGreater(dual_score, single_b_score)

    def test_dual_candidate_score_equals_sum(self):
        common_id = "cid_common"
        vec = _make_vector_lookup(common_id)
        bm25 = _make_bm25_results(common_id)

        result = _rrf_fuse(vec, bm25, k=RRF_K, top_k=5)
        _cid, rrf_score, v_rank, b_rank = result[0]

        expected = 1.0 / (RRF_K + v_rank) + 1.0 / (RRF_K + b_rank)
        self.assertAlmostEqual(rrf_score, expected, places=7)

    def test_dual_candidate_is_top_ranked(self):
        common_id = "cid_common"
        vec = _make_vector_lookup(common_id, "cid_v2", "cid_v3")
        bm25 = _make_bm25_results(common_id, "cid_b2", "cid_b3")

        result = _rrf_fuse(vec, bm25, k=RRF_K, top_k=5)
        self.assertEqual(result[0][0], common_id)


class TestT4NoDuplicates(unittest.TestCase):
    def test_shared_id_appears_once(self):
        common_id = "cid_shared"
        vec = _make_vector_lookup(common_id, "cid_v2")
        bm25 = _make_bm25_results(common_id, "cid_b2")

        result = _rrf_fuse(vec, bm25, k=RRF_K, top_k=10)
        ids = [r[0] for r in result]

        self.assertEqual(ids.count(common_id), 1)

    def test_all_output_ids_are_unique(self):
        vec = _make_vector_lookup("a", "b", "c")
        bm25 = _make_bm25_results("b", "c", "d")

        result = _rrf_fuse(vec, bm25, k=RRF_K, top_k=10)
        ids = [r[0] for r in result]

        self.assertEqual(len(ids), len(set(ids)))


class TestT5TopKBound(unittest.TestCase):
    def test_output_length_capped_at_top_k(self):
        vec = _make_vector_lookup(*[f"v{i}" for i in range(10)])
        bm25 = _make_bm25_results(*[f"b{i}" for i in range(10)])

        result = _rrf_fuse(vec, bm25, k=RRF_K, top_k=5)
        self.assertEqual(len(result), 5)

    def test_output_length_equals_union_when_smaller_than_top_k(self):
        vec = _make_vector_lookup("v1")
        bm25 = _make_bm25_results("b1")

        result = _rrf_fuse(vec, bm25, k=RRF_K, top_k=5)
        self.assertEqual(len(result), 2)

    def test_output_is_empty_when_both_inputs_are_empty(self):
        result = _rrf_fuse({}, [], k=RRF_K, top_k=5)
        self.assertEqual(result, [])

    def test_output_sorted_descending_by_rrf_score(self):
        vec = _make_vector_lookup(*[f"v{i}" for i in range(5)])
        bm25 = _make_bm25_results(*[f"b{i}" for i in range(5)])

        result = _rrf_fuse(vec, bm25, k=RRF_K, top_k=10)
        scores = [r[1] for r in result]

        self.assertEqual(scores, sorted(scores, reverse=True))


class TestT6BM25OnlyDocumentShape(unittest.TestCase):
    def _simulate_chroma_get_response(self, cid: str) -> dict:
        return {
            "ids": [cid],
            "documents": ["Blockchain is a distributed ledger technology."],
            "metadatas": [{
                "document_id": "deadbeef-0000-0000-0000-000000000001",
                "document_name": "B1_mastering-bitcoin.pdf",
                "page_number": 7,
                "chunk_index": 3,
            }],
        }

    def test_document_constructed_from_chroma_get_has_required_keys(self):
        fetched = self._simulate_chroma_get_response("some_chroma_id")
        doc = Document(page_content=fetched["documents"][0], metadata=fetched["metadatas"][0])

        for required_key in ("document_id", "page_number", "chunk_index"):
            self.assertIn(required_key, doc.metadata)

    def test_document_page_content_matches_chunk_text(self):
        fetched = self._simulate_chroma_get_response("some_chroma_id")
        doc = Document(
            page_content=fetched["documents"][0],
            metadata=fetched["metadatas"][0],
        )
        self.assertEqual(doc.page_content, "Blockchain is a distributed ledger technology.")

    def test_document_name_carried_through_from_chroma_metadata(self):
        fetched = self._simulate_chroma_get_response("some_chroma_id")
        doc = Document(
            page_content=fetched["documents"][0],
            metadata=fetched["metadatas"][0],
        )
        self.assertEqual(doc.metadata.get("document_name"), "B1_mastering-bitcoin.pdf")


class TestT7VectorFallback(unittest.TestCase):
    def test_rrf_with_empty_bm25_produces_vector_results(self):
        vec = _make_vector_lookup("v1", "v2", "v3")
        bm25 = []

        result = _rrf_fuse(vec, bm25, k=RRF_K, top_k=5)
        result_ids = [r[0] for r in result]

        self.assertEqual(set(result_ids), {"v1", "v2", "v3"})

    def test_rrf_with_empty_vector_produces_bm25_results(self):
        vec = {}
        bm25 = _make_bm25_results("b1", "b2")

        result = _rrf_fuse(vec, bm25, k=RRF_K, top_k=5)
        result_ids = [r[0] for r in result]

        self.assertEqual(set(result_ids), {"b1", "b2"})

    def test_rrf_both_empty_returns_empty(self):
        result = _rrf_fuse({}, [], k=RRF_K, top_k=5)
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
