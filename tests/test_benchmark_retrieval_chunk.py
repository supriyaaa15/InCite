import logging
from types import SimpleNamespace

import pytest

from app.core.retrieved_chunk import RetrievedChunk
from scripts import benchmark_retrieval_chunk as benchmark


def test_metrics_count_only_binary_hits_at_each_cutoff():
    result = benchmark._binary_metrics(["other", "a", "b", "x", "y", "c"], {"a", "b", "c"})
    assert result["recall_at_1"] == 0
    assert result["recall_at_3"] == result["recall_at_5"] == pytest.approx(2 / 3)
    assert result["precision_at_5"] == 0.4
    assert result["reciprocal_rank"] == 0.5


def test_empty_short_and_out_of_cutoff_results():
    assert benchmark._binary_metrics([], {"a"})["reciprocal_rank"] == 0
    assert benchmark._binary_metrics(["a"], {"a"})["precision_at_5"] == 0.2
    assert benchmark._binary_metrics(["x", "y", "z", "b", "c", "a"], {"a"})["reciprocal_rank"] == 0


def test_duplicate_results_fail():
    with pytest.raises(ValueError, match="duplicate"):
        benchmark._binary_metrics(["a", "A"], {"a"})


def test_current_result_type_uses_canonical_metadata():
    chunk = RetrievedChunk("evidence", metadata={"document_id": "test", "page_number": 2, "chunk_index": 0})
    assert benchmark._ranked_chunk_ids([(chunk, 0.1)], []) == ["test_p2_c0"]


@pytest.mark.parametrize(("trace", "warning", "results", "fails"), [
    ("", "", [], False),
    ("Actual Mode : hybrid", "", [object()], False),
    ("Actual Mode : vector   [BM25 unavailable: error]", "", [object()], True),
    ("", "Document eligibility query failed", [], True),
    ("", "", [object()], True),
])
def test_mode_and_error_guard(trace, warning, results, fails):
    logger = logging.getLogger("test.benchmark.retrieval")

    def retrieve(*args, **kwargs):
        if warning:
            logger.warning(warning)
        print(trace)
        return results

    service = SimpleNamespace(retrieve=retrieve, logger=logger)
    if fails:
        with pytest.raises(RuntimeError):
            benchmark._checked_retrieve(service, None, "query", None, "hybrid", 5)
    else:
        assert benchmark._checked_retrieve(service, None, "query", None, "hybrid", 5)[0] == results


def test_cli_rejects_depth_under_five(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["benchmark", "--questions", "unused", "--compare", "--top-k", "4"])
    with pytest.raises(SystemExit, match="2"):
        benchmark.main()
    assert "at least 5" in capsys.readouterr().err


def test_latency_uses_nearest_rank():
    assert benchmark._percentile(list(range(1, 19)), .5) == 9
    assert benchmark._percentile(list(range(1, 19)), .95) == 18
