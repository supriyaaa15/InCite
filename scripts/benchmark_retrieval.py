#!/usr/bin/env python3
"""
Retrieval benchmark — compares vector vs hybrid retrieval quality.

Usage
-----
Single mode:
    python scripts/benchmark_retrieval.py --mode vector
    python scripts/benchmark_retrieval.py --mode hybrid

Side-by-side comparison (runs each question twice):
    python scripts/benchmark_retrieval.py --compare

Optional arguments:
    --questions PATH   Path to questions JSON file (default: scripts/questions.json)
    --top-k N          Number of results to retrieve (default: 5)

questions.json format:
    [
      {
        "question": "What is consistent hashing?",
        "collection_id": "uuid-of-collection",
        "expected_documents": ["SystemDesign.pdf"]
      }
    ]

expected_documents is a list — Top-1 / Top-3 is satisfied when ANY of
the expected documents appears in the respective position.

Requires the app to be importable (run inside the Docker container or with
the same Python environment as the app).
"""

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

# ── Bootstrap: make the project root importable ───────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("RETRIEVAL_MODE", "vector")   # overridden by --mode

from app.core.config import settings
from app.core.database import SessionLocal
from app.services import retrieval_service


# ── Helpers ───────────────────────────────────────────────────────────────────

def _run_one(question: dict, mode: str, top_k: int) -> dict:
    """
    Run retrieval for a single question in the given mode.

    Returns a result dict with keys:
        question, mode, retrieved, latency_ms, top1_hit, top3_hit
    """
    # Temporarily override RETRIEVAL_MODE for this call.
    # (Settings are read once inside retrieve() so we patch the object.)
    original_mode = settings.RETRIEVAL_MODE
    settings.RETRIEVAL_MODE = mode

    db = SessionLocal()
    try:
        collection_id = uuid.UUID(question["collection_id"])
        query = question["question"]
        expected = [e.lower() for e in question.get("expected_documents", [])]

        t0 = time.perf_counter()
        retrieved = retrieval_service.retrieve(collection_id, query, db, top_k=top_k)
        latency_ms = int((time.perf_counter() - t0) * 1000)

        citations = retrieval_service.build_citations(db, retrieved)

        ranked_docs = [c["document_name"].lower() for c in citations]

        top1_hit = bool(ranked_docs) and (not expected or ranked_docs[0] in expected)
        top3_hit = bool(expected) and any(d in expected for d in ranked_docs[:3])
        if not expected:
            top1_hit = top3_hit = None   # no ground truth — mark as unknown

        return {
            "question":    query,
            "mode":        mode,
            "retrieved":   citations,
            "latency_ms":  latency_ms,
            "top1_hit":    top1_hit,
            "top3_hit":    top3_hit,
            "expected":    question.get("expected_documents", []),
        }
    finally:
        db.close()
        settings.RETRIEVAL_MODE = original_mode


def _print_question_result(result: dict, show_mode: bool = True) -> None:
    mode_line = f"  Mode     : {result['mode']}\n" if show_mode else ""
    print(f"\nQ: \"{result['question']}\"")
    print(mode_line, end="")
    print(f"  Latency  : {result['latency_ms']} ms")
    if result["expected"]:
        t1 = "✓" if result["top1_hit"] else "✗"
        t3 = "✓" if result["top3_hit"] else "✗"
        print(f"  Expected : {', '.join(result['expected'])}  →  Top-1: {t1}  Top-3: {t3}")
    else:
        print("  Expected : (no ground truth)")
    print("  Retrieved:")
    for i, c in enumerate(result["retrieved"][:5], 1):
        vr = c.get("_vector_rank", "-") if "_vector_rank" in c else "-"
        br = c.get("_bm25_rank",   "-") if "_bm25_rank"   in c else "-"
        extras = (
            f"  vector_rank={vr}  bm25_rank={br}"
            if result["mode"] == "hybrid" else ""
        )
        print(
            f"    #{i}  {c['document_name']}  p.{c['page_number']}"
            f"  score={c['score']:.4f}{extras}"
        )


def _aggregate(results: list[dict]) -> dict:
    latencies   = [r["latency_ms"] for r in results]
    top1_known  = [r for r in results if r["top1_hit"] is not None]
    top3_known  = [r for r in results if r["top3_hit"] is not None]
    top1_hits   = sum(1 for r in top1_known if r["top1_hit"])
    top3_hits   = sum(1 for r in top3_known if r["top3_hit"])
    n           = len(results)
    return {
        "n":            n,
        "top1_hits":    top1_hits,
        "top1_total":   len(top1_known),
        "top3_hits":    top3_hits,
        "top3_total":   len(top3_known),
        "avg_latency":  int(sum(latencies) / n) if n else 0,
    }


def _print_aggregate(agg: dict, label: str) -> None:
    t = agg["top1_total"]
    print(f"\n{label}")
    if t:
        pct1 = int(100 * agg["top1_hits"] / agg["top1_total"])
        pct3 = int(100 * agg["top3_hits"] / agg["top3_total"])
        print(f"  Top-1   : {pct1:>3}%  ({agg['top1_hits']}/{agg['top1_total']})")
        print(f"  Top-3   : {pct3:>3}%  ({agg['top3_hits']}/{agg['top3_total']})")
    else:
        print("  Top-1   : n/a  (no ground truth in questions.json)")
        print("  Top-3   : n/a")
    print(f"  Latency : {agg['avg_latency']} ms")


def _print_delta(vec: dict, hyb: dict) -> None:
    print("\nDelta (Hybrid − Vector)")
    if vec["top1_total"] and hyb["top1_total"]:
        d1 = int(100 * hyb["top1_hits"] / hyb["top1_total"]) - int(100 * vec["top1_hits"] / vec["top1_total"])
        d3 = int(100 * hyb["top3_hits"] / hyb["top3_total"]) - int(100 * vec["top3_hits"] / vec["top3_total"])
        sign = lambda x: f"+{x}" if x >= 0 else str(x)
        print(f"  Top-1   : {sign(d1)}%")
        print(f"  Top-3   : {sign(d3)}%")
    else:
        print("  Top-1   : n/a")
        print("  Top-3   : n/a")
    d_lat = hyb["avg_latency"] - vec["avg_latency"]
    sign = "+" if d_lat >= 0 else ""
    print(f"  Latency : {sign}{d_lat} ms")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="InCite retrieval benchmark")
    parser.add_argument(
        "--questions",
        default=str(PROJECT_ROOT / "scripts" / "questions.json"),
        help="Path to questions JSON file",
    )
    parser.add_argument("--top-k", type=int, default=5, help="Number of results to retrieve")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--mode", choices=["vector", "hybrid"], help="Run in a single mode")
    group.add_argument("--compare", action="store_true", help="Run both modes and show delta")
    args = parser.parse_args()

    questions_path = Path(args.questions)
    if not questions_path.exists():
        print(f"ERROR: questions file not found: {questions_path}")
        sys.exit(1)

    questions = json.loads(questions_path.read_text(encoding="utf-8"))
    if not questions:
        print("ERROR: questions.json is empty.")
        sys.exit(1)

    top_k = args.top_k

    if args.compare:
        # ── Compare mode ──────────────────────────────────────────────────
        print(f"\nRunning {len(questions)} question(s) in VECTOR mode...")
        vec_results = [_run_one(q, "vector", top_k) for q in questions]

        print(f"\nRunning {len(questions)} question(s) in HYBRID mode...")
        hyb_results = [_run_one(q, "hybrid", top_k) for q in questions]

        print(f"\n{'=' * 30}")
        print("Vector")
        _print_aggregate(_aggregate(vec_results), "")
        print("\nHybrid")
        _print_aggregate(_aggregate(hyb_results), "")
        _print_delta(_aggregate(vec_results), _aggregate(hyb_results))
        print(f"{'=' * 30}\n")

    else:
        # ── Single mode ───────────────────────────────────────────────────
        mode = args.mode
        print(f"\nRunning {len(questions)} question(s) in {mode.upper()} mode...\n")

        results = []
        for q in questions:
            r = _run_one(q, mode, top_k)
            _print_question_result(r, show_mode=False)
            results.append(r)

        agg = _aggregate(results)
        print(f"\n{'─' * 30}")
        _print_aggregate(agg, f"Summary ({mode})")
        print()


if __name__ == "__main__":
    main()
