#!/usr/bin/env python3
"""
benchmark_retrieval_chunk.py — chunk-level retrieval quality + latency benchmark.

Primary evaluation:
  - Recall@1 / Recall@3 / Recall@5   (chunk-level)
  - Precision@5 and MRR@5
  - Latency p50 / p95 / mean (ms)

Optional secondary evaluation:
  - Top-1 / Top-3 / Top-5 document hit rate when expected_documents is present.

The benchmark can run vector, hybrid (BM25+RRF), or hybrid with reranking,
or compare all three on the same labeled question set.

Pass a labeled JSON file with --questions:
    [
      {
        "question": "...",
        "collection_id": "<uuid>",
        "expected_chunks": ["<exact_chunk_id>", "..."],
        "expected_documents": ["exact_filename.pdf"]
      }
    ]

expected_chunks must contain the actual chunk IDs for the answer-bearing
chunk(s). Do not invent these IDs; obtain them from the chunks stored by the
application. expected_documents is optional.

Must be run inside the app's Python environment since it imports app.core /
app.services directly.
"""

import argparse
from contextlib import redirect_stdout
from io import StringIO
import json
import logging
import math
import re
import statistics
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# ── Chunk-ID extraction ───────────────────────────────────────────────────────

def _value_from_mapping_or_object(item, key: str):
    """Read a key from a dict-like/object result without assuming one schema."""
    if item is None:
        return None

    if isinstance(item, dict):
        return item.get(key)

    try:
        return getattr(item, key)
    except AttributeError:
        return None


def _extract_chunk_id(item) -> str | None:
    """
    Extract a chunk ID from a retrieval result or citation.

    InCite's retrieve() returns list[tuple[RetrievedChunk, float]].
    The canonical chunk identity comes from the document_id, page_number, and
    chunk_index metadata. Ingestion writes it to Chroma in this format.

        f"{document_id}_p{page_number}_c{chunk_index}"

    That same string is stored in the Postgres chunks.chroma_id column and is
    what expected_chunks values must contain.

    build_citations() returns plain dicts with document_id, page_number,
    chunk_index — we reconstruct the same chroma_id format from those too.

    Both paths produce the same canonical string so comparisons work correctly.
    """
    if item is None:
        return None

    # Path 1 uses the chunk from the (RetrievedChunk, float) retrieval pair.
    if isinstance(item, tuple):
        item = item[0]

    metadata = _value_from_mapping_or_object(item, "metadata")
    if metadata is not None:
        meta = metadata if isinstance(metadata, dict) else {}
        doc_id = meta.get("document_id")
        page = meta.get("page_number")
        chunk = meta.get("chunk_index")
        if doc_id is not None and page is not None and chunk is not None:
            return f"{doc_id}_p{page}_c{chunk}"

    # ── Path 2: citation dict from build_citations() ──────────────────────────
    # {"document_id": ..., "page_number": ..., "chunk_index": ..., ...}
    if isinstance(item, dict):
        doc_id = item.get("document_id")
        page = item.get("page_number")
        chunk = item.get("chunk_index")
        if doc_id is not None and page is not None and chunk is not None:
            return f"{doc_id}_p{page}_c{chunk}"

        # Explicit chunk_id key — kept for forward compatibility.
        for key in ("chunk_id", "chunkId", "chroma_id"):
            value = item.get(key)
            if value is not None:
                return str(value)

    # ── Path 3: object attributes (forward compatibility) ─────────────────────
    for key in ("chunk_id", "chunkId", "chroma_id"):
        value = _value_from_mapping_or_object(item, key)
        if value is not None:
            return str(value)

    return None


def _ranked_chunk_ids(retrieved, citations) -> list[str]:
    """
    Build the ranked chunk-ID list.

    Prefer the raw retrieval objects because build_citations may omit internal
    chunk identifiers. Fall back to citation objects when necessary.
    """
    ranked = []

    retrieved_list = list(retrieved or [])
    citations_list = list(citations or [])

    n = max(len(retrieved_list), len(citations_list))

    for i in range(n):
        chunk_id = None

        if i < len(retrieved_list):
            chunk_id = _extract_chunk_id(retrieved_list[i])

        if chunk_id is None and i < len(citations_list):
            chunk_id = _extract_chunk_id(citations_list[i])

        if chunk_id is None:
            raise ValueError(
                "Could not extract a chunk ID from retrieval results. "
                "The benchmark requires retrieval results/citations to expose "
                "the stored chunk ID without changing the retrieval algorithm."
            )

        ranked.append(chunk_id)

    return ranked


# ── Core single-question run ─────────────────────────────────────────────────

def _binary_metrics(ranked: list[str], expected: set[str]) -> dict:
    ranked = [chunk_id.lower() for chunk_id in ranked]
    if len(ranked) != len(set(ranked)):
        raise ValueError("Retrieval returned duplicate chunk IDs.")
    first = next((i for i, chunk_id in enumerate(ranked[:5], 1) if chunk_id in expected), None)
    return {
        **{f"recall_at_{k}": len(expected.intersection(ranked[:k])) / len(expected) for k in (1, 3, 5)},
        "precision_at_5": len(expected.intersection(ranked[:5])) / 5,
        "rank_of_first_hit": first,
        "reciprocal_rank": 1.0 / first if first is not None else 0.0,
    }


def _checked_retrieve(service, collection_id, query, db, mode, top_k):
    """Reject logged failures and implicit mode changes before scoring."""
    trace, warnings = StringIO(), StringIO()
    handler = logging.StreamHandler(warnings)
    handler.setLevel(logging.WARNING)
    service.logger.addHandler(handler)
    try:
        with redirect_stdout(trace):
            results = service.retrieve(collection_id, query, db, top_k=top_k)
    finally:
        service.logger.removeHandler(handler)
    match = re.search(r"^Actual Mode\s*:\s*(.+)$", trace.getvalue(), re.MULTILINE)
    actual = match[1].strip().split("   [", 1)[0] if match else None
    # Clean early exits have no mode trace. A logged SQL error is never a miss.
    if warnings.getvalue() or (actual != mode and (results or actual is not None)):
        reason = warnings.getvalue().strip() or f"Unexpected retrieval mode: {actual!r}"
        raise RuntimeError(reason)
    return results, actual


def _run_one(question: dict, mode: str, top_k: int) -> dict:
    """
    Runs retrieval for one question in the given mode and scores it.

    MRR@5 uses the 1-based rank of the first chunk in the top five whose
    ID appears in expected_chunks.

    Recall@K is:
        (# expected chunks found in top K) / (# expected chunks)

    This supports questions with multiple answer-bearing chunks.
    """
    from app.core.chroma_client import get_existing_collection_store
    from app.core.config import settings
    from app.core.database import SessionLocal
    from app.services import retrieval_service

    original_mode = settings.RETRIEVAL_MODE
    settings.RETRIEVAL_MODE = mode

    db = SessionLocal()
    try:
        collection_id = uuid.UUID(question["collection_id"])
        query = question["question"]

        expected_chunks = {
            str(e).strip().lower()
            for e in question.get("expected_chunks", [])
            if str(e).strip()
        }

        if not expected_chunks:
            raise ValueError(
                f"Question {question.get('_id', '<unknown>')} has no "
                "expected_chunks. Add the actual answer-bearing chunk IDs "
                "before running the benchmark."
            )

        try:
            if get_existing_collection_store(str(collection_id)) is None:
                raise RuntimeError("Benchmark Chroma collection is unavailable.")
            t0 = time.perf_counter()
            retrieved, actual_mode = _checked_retrieve(retrieval_service, collection_id, query, db, mode, top_k)
        except Exception as exc:
            raise RuntimeError(f"{question.get('_id', '<unknown>')} ({mode}): {exc}") from exc
        latency_ms = (time.perf_counter() - t0) * 1000

        citations = retrieval_service.build_citations(retrieved)
        ranked_chunk_ids = _ranked_chunk_ids(retrieved, citations)

        result = {
            "question": query,
            "id": question.get("_id"),
            "mode": mode,
            "actual_mode": actual_mode,
            "status": "ok" if ranked_chunk_ids else "empty",
            "retrieved": citations,
            "retrieved_chunk_ids": ranked_chunk_ids,
            "latency_ms": latency_ms,
            **_binary_metrics(ranked_chunk_ids, expected_chunks),
            "expected_chunks": sorted(expected_chunks),
        }

        # Optional secondary document-level metrics for continuity with v2.
        expected_documents = [
            str(e).lower()
            for e in question.get("expected_documents", [])
            if str(e).strip()
        ]

        if expected_documents:
            ranked_docs = [
                str(c["document_name"]).lower()
                for c in citations
                if "document_name" in c
            ]
            expected_docs = set(expected_documents)

            doc_rank = None
            for i, document_name in enumerate(ranked_docs, start=1):
                if document_name in expected_docs:
                    doc_rank = i
                    break

            result.update({
                "document_rank_of_first_hit": doc_rank,
                "document_top1_hit": doc_rank == 1,
                "document_top3_hit": doc_rank is not None and doc_rank <= 3,
                "document_top5_hit": doc_rank is not None and doc_rank <= 5,
            })

        return result

    finally:
        db.close()
        settings.RETRIEVAL_MODE = original_mode


# ── Aggregation ───────────────────────────────────────────────────────────────

def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = max(0, math.ceil(len(s) * p) - 1)
    return s[idx]


def _aggregate(results: list[dict]) -> dict:
    latencies = [r["latency_ms"] for r in results]
    n = len(results)

    recall1 = (
        statistics.mean(r["recall_at_1"] for r in results) if n else 0.0
    )
    recall3 = (
        statistics.mean(r["recall_at_3"] for r in results) if n else 0.0
    )
    recall5 = (
        statistics.mean(r["recall_at_5"] for r in results) if n else 0.0
    )
    mrr = (
        statistics.mean(r["reciprocal_rank"] for r in results) if n else 0.0
    )

    agg = {
        "n": n,
        "recall1": recall1,
        "recall3": recall3,
        "recall5": recall5,
        "precision5": statistics.mean(r["precision_at_5"] for r in results) if n else 0.0,
        "mrr": mrr,
        "latency_mean": statistics.mean(latencies) if latencies else 0.0,
        "latency_p50": _percentile(latencies, 0.50),
        "latency_p95": _percentile(latencies, 0.95),
    }

    # Optional secondary document-level aggregate.
    doc_scored = [r for r in results if "document_top1_hit" in r]
    if doc_scored:
        agg.update({
            "document_top1": statistics.mean(
                bool(r["document_top1_hit"]) for r in doc_scored
            ),
            "document_top3": statistics.mean(
                bool(r["document_top3_hit"]) for r in doc_scored
            ),
            "document_top5": statistics.mean(
                bool(r["document_top5_hit"]) for r in doc_scored
            ),
            "document_n_scored": len(doc_scored),
        })

    return agg


# ── Reporting ─────────────────────────────────────────────────────────────────

def _print_summary(label: str, agg: dict) -> None:
    print(f"\n{label}  (n={agg['n']})")
    print(f"  Recall@1 : {_pct(agg['recall1'])}")
    print(f"  Recall@3 : {_pct(agg['recall3'])}")
    print(f"  Recall@5 : {_pct(agg['recall5'])}")
    print(f"  Precision@5: {_pct(agg['precision5'])}")
    print(f"  MRR@5    : {agg['mrr']:.3f}")
    print(
        f"  Latency  mean={agg['latency_mean']:.0f}ms  "
        f"p50={agg['latency_p50']:.0f}ms  "
        f"p95={agg['latency_p95']:.0f}ms"
    )

    if "document_n_scored" in agg:
        print(
            f"  Doc-level secondary  Top-1={_pct(agg['document_top1'])}  "
            f"Top-3={_pct(agg['document_top3'])}  "
            f"Top-5={_pct(agg['document_top5'])}"
        )


def _write_markdown_report(
    path: Path,
    top_k: int,
    vec_agg: dict | None,
    hyb_agg: dict | None,
    rerank_agg: dict | None,
    n_questions: int,
) -> None:
    from app.core.config import settings

    lines = []
    lines.append("# InCite Retrieval Benchmark Report")
    lines.append("")
    lines.append(
        f"- Generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}"
    )
    lines.append(f"- Question set size: {n_questions}")
    lines.append(f"- Retrieval top_k: {top_k}")
    lines.append("- Primary ground truth: exact answer-bearing chunk IDs")
    lines.append(f"- Embedding model: `{settings.EMBEDDING_MODEL}`")
    lines.append(f"- BM25 RRF k: {settings.BM25_RRF_K}")
    lines.append(f"- Reranker model: `{settings.RERANKER_MODEL}`")
    lines.append(f"- Reranker pool k: {settings.RERANK_POOL_K}")
    lines.append("")
    lines.append("## Primary: Chunk-level retrieval")
    lines.append("")

    if vec_agg is not None and hyb_agg is not None and rerank_agg is not None:
        lines.append("| Metric | Vector-only | Hybrid (BM25+RRF) | Hybrid + Reranker | Delta (Rerank vs Hybrid) |")
        lines.append("|---|---:|---:|---:|---:|")
        lines.append(
            f"| Recall@1 | {_pct(vec_agg['recall1'])} | "
            f"{_pct(hyb_agg['recall1'])} | {_pct(rerank_agg['recall1'])} | "
            f"{rerank_agg['recall1'] - hyb_agg['recall1']:+.1%} |"
        )
        lines.append(
            f"| Recall@3 | {_pct(vec_agg['recall3'])} | "
            f"{_pct(hyb_agg['recall3'])} | {_pct(rerank_agg['recall3'])} | "
            f"{rerank_agg['recall3'] - hyb_agg['recall3']:+.1%} |"
        )
        lines.append(
            f"| Recall@5 | {_pct(vec_agg['recall5'])} | "
            f"{_pct(hyb_agg['recall5'])} | {_pct(rerank_agg['recall5'])} | "
            f"{rerank_agg['recall5'] - hyb_agg['recall5']:+.1%} |"
        )
        lines.append(
            f"| Precision@5 | {_pct(vec_agg['precision5'])} | {_pct(hyb_agg['precision5'])} | "
            f"{_pct(rerank_agg['precision5'])} | {rerank_agg['precision5'] - hyb_agg['precision5']:+.1%} |"
        )
        lines.append(
            f"| MRR@5 | {vec_agg['mrr']:.3f} | {hyb_agg['mrr']:.3f} | {rerank_agg['mrr']:.3f} | "
            f"{rerank_agg['mrr'] - hyb_agg['mrr']:+.3f} |"
        )
        lines.append(
            f"| Latency mean (ms) | {vec_agg['latency_mean']:.0f} | "
            f"{hyb_agg['latency_mean']:.0f} | {rerank_agg['latency_mean']:.0f} | "
            f"{rerank_agg['latency_mean'] - hyb_agg['latency_mean']:+.0f} |"
        )
        lines.append(
            f"| Latency p50 (ms) | {vec_agg['latency_p50']:.0f} | "
            f"{hyb_agg['latency_p50']:.0f} | {rerank_agg['latency_p50']:.0f} | "
            f"{rerank_agg['latency_p50'] - hyb_agg['latency_p50']:+.0f} |"
        )
        lines.append(
            f"| Latency p95 (ms) | {vec_agg['latency_p95']:.0f} | "
            f"{hyb_agg['latency_p95']:.0f} | {rerank_agg['latency_p95']:.0f} | "
            f"{rerank_agg['latency_p95'] - hyb_agg['latency_p95']:+.0f} |"
        )
    elif vec_agg is not None and hyb_agg is not None:
        lines.append("| Metric | Vector-only | Hybrid (BM25+RRF) | Delta |")
        lines.append("|---|---:|---:|---:|")
        lines.append(
            f"| Recall@1 | {_pct(vec_agg['recall1'])} | "
            f"{_pct(hyb_agg['recall1'])} | "
            f"{hyb_agg['recall1'] - vec_agg['recall1']:+.1%} |"
        )
        lines.append(
            f"| Recall@3 | {_pct(vec_agg['recall3'])} | "
            f"{_pct(hyb_agg['recall3'])} | "
            f"{hyb_agg['recall3'] - vec_agg['recall3']:+.1%} |"
        )
        lines.append(
            f"| Recall@5 | {_pct(vec_agg['recall5'])} | "
            f"{_pct(hyb_agg['recall5'])} | "
            f"{hyb_agg['recall5'] - vec_agg['recall5']:+.1%} |"
        )
        lines.append(
            f"| Precision@5 | {_pct(vec_agg['precision5'])} | {_pct(hyb_agg['precision5'])} | "
            f"{hyb_agg['precision5'] - vec_agg['precision5']:+.1%} |"
        )
        lines.append(
            f"| MRR@5 | {vec_agg['mrr']:.3f} | {hyb_agg['mrr']:.3f} | "
            f"{hyb_agg['mrr'] - vec_agg['mrr']:+.3f} |"
        )
        lines.append(
            f"| Latency mean (ms) | {vec_agg['latency_mean']:.0f} | "
            f"{hyb_agg['latency_mean']:.0f} | "
            f"{hyb_agg['latency_mean'] - vec_agg['latency_mean']:+.0f} |"
        )
        lines.append(
            f"| Latency p50 (ms) | {vec_agg['latency_p50']:.0f} | "
            f"{hyb_agg['latency_p50']:.0f} | "
            f"{hyb_agg['latency_p50'] - vec_agg['latency_p50']:+.0f} |"
        )
        lines.append(
            f"| Latency p95 (ms) | {vec_agg['latency_p95']:.0f} | "
            f"{hyb_agg['latency_p95']:.0f} | "
            f"{hyb_agg['latency_p95'] - vec_agg['latency_p95']:+.0f} |"
        )
    else:
        agg = vec_agg or hyb_agg or rerank_agg
        if agg is not None:
            lines.append("| Metric | Result |")
            lines.append("|---|---:|")
            lines.append(f"| Recall@1 | {_pct(agg['recall1'])} |")
            lines.append(f"| Recall@3 | {_pct(agg['recall3'])} |")
            lines.append(f"| Recall@5 | {_pct(agg['recall5'])} |")
            lines.append(f"| Precision@5 | {_pct(agg['precision5'])} |")
            lines.append(f"| MRR@5 | {agg['mrr']:.3f} |")
            lines.append(f"| Latency mean (ms) | {agg['latency_mean']:.0f} |")
            lines.append(f"| Latency p50 (ms) | {agg['latency_p50']:.0f} |")
            lines.append(f"| Latency p95 (ms) | {agg['latency_p95']:.0f} |")

    lines.append("")
    lines.append("## Notes")
    lines.append("")
    lines.append(
        "- Recall@K measures the fraction of labeled answer-bearing chunks "
        "retrieved within the first K results."
    )
    lines.append(
        "- Precision@5 divides binary hits by five, including any unfilled slots. "
        "MRR@5 uses the first relevant rank within the top five."
    )
    lines.append(
        "- The same questions and expected chunk IDs are used for all retrieval modes, so comparisons are paired."
    )
    lines.append(
        "- Re-run the benchmark after retrieval, chunking, or embedding-model "
        "changes and keep the labeled question set fixed."
    )
    lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


# ── Entry point ───────────────────────────────────────────────────────────────

def _load_questions(path: Path) -> list[dict]:
    try:
        questions = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise ValueError(f"questions file not found: {path}") from exc
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"cannot read questions file {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {path} at line {exc.lineno}, column {exc.colno}: {exc.msg}") from exc

    if not isinstance(questions, list) or not questions:
        raise ValueError("questions file must contain a non-empty JSON array of question objects")

    for index, row in enumerate(questions, start=1):
        label = f"question #{index}"
        if not isinstance(row, dict):
            raise ValueError(f"{label} must be a JSON object")
        question_text = row.get("question")
        if not isinstance(question_text, str) or not question_text.strip():
            raise ValueError(f"{label} must have non-empty 'question' text")
        collection_id = row.get("collection_id")
        if not isinstance(collection_id, str):
            raise ValueError(f"{label} must have a valid 'collection_id' UUID")
        try:
            uuid.UUID(collection_id)
        except ValueError as exc:
            raise ValueError(f"{label} must have a valid 'collection_id' UUID") from exc
        chunks = row.get("expected_chunks")
        if not isinstance(chunks, list) or not chunks or any(
            not isinstance(chunk, str) or not chunk.strip() for chunk in chunks
        ):
            raise ValueError(f"{label} must have a non-empty 'expected_chunks' array of exact chunk IDs")
        documents = row.get("expected_documents", [])
        if not isinstance(documents, list) or any(
            not isinstance(name, str) or not name.strip() for name in documents
        ):
            raise ValueError(f"{label} 'expected_documents' must be an array of non-empty filenames")

    return questions


def main() -> None:
    parser = argparse.ArgumentParser(
        description="InCite chunk-level retrieval benchmark (Recall@K + MRR + latency)"
    )
    parser.add_argument(
        "--questions",
        required=True,
        type=Path,
        help="Labeled JSON file with question text, collection UUIDs, and expected chunk IDs",
    )
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument(
        "--report",
        default=None,
        help="Write a markdown report to this path",
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--mode", choices=["vector", "hybrid", "hybrid_rerank"])
    group.add_argument("--compare", action="store_true")
    args = parser.parse_args()
    if args.top_k < 5:
        parser.error("--top-k must be at least 5 for the reported cutoffs.")

    try:
        questions = _load_questions(args.questions)
    except ValueError as exc:
        parser.error(str(exc))

    top_k = args.top_k
    vec_agg = hyb_agg = rerank_agg = None

    if args.compare:
        print(f"\nRunning {len(questions)} question(s) in VECTOR mode...")
        vec_results = [_run_one(q, "vector", top_k) for q in questions]
        vec_agg = _aggregate(vec_results)

        print(f"Running {len(questions)} question(s) in HYBRID mode...")
        hyb_results = [_run_one(q, "hybrid", top_k) for q in questions]
        hyb_agg = _aggregate(hyb_results)

        print(f"Running {len(questions)} question(s) in HYBRID_RERANK mode...")
        rerank_results = [_run_one(q, "hybrid_rerank", top_k) for q in questions]
        rerank_agg = _aggregate(rerank_results)

        _print_summary("Vector-only", vec_agg)
        _print_summary("Hybrid (BM25+RRF)", hyb_agg)
        _print_summary("Hybrid + Reranker", rerank_agg)

        print(
            f"\nDelta (Hybrid+Rerank − Hybrid): "
            f"Recall@3 {rerank_agg['recall3'] - hyb_agg['recall3']:+.1%}, "
            f"MRR@5 {rerank_agg['mrr'] - hyb_agg['mrr']:+.3f}, "
            f"p95 latency "
            f"{rerank_agg['latency_p95'] - hyb_agg['latency_p95']:+.0f}ms"
        )
    else:
        mode = args.mode
        print(
            f"\nRunning {len(questions)} question(s) in {mode.upper()} mode..."
        )
        results = [_run_one(q, mode, top_k) for q in questions]
        agg = _aggregate(results)
        _print_summary(mode.replace("_", " ").capitalize(), agg)

        if mode == "vector":
            vec_agg = agg
        elif mode == "hybrid":
            hyb_agg = agg
        else:
            rerank_agg = agg

    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        _write_markdown_report(
            report_path,
            top_k,
            vec_agg,
            hyb_agg,
            rerank_agg,
            len(questions),
        )
        print(f"\nMarkdown report written to {report_path}")


if __name__ == "__main__":
    main()
