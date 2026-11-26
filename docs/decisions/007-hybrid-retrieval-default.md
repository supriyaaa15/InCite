# ADR 007 — Use hybrid retrieval by default

**Status** — Implemented. Vector and hybrid with CrossEncoder reranking remain explicit settings.

## Decision

Fuse independent vector and BM25 candidate lists with reciprocal rank fusion. Vector search can recover paraphrases, and lexical search can recover exact terms. Fusing their union lets eligible lexical-only hits compete even when the vector search misses them. The [architecture guide](../architecture.md#ingestion-and-retrieval) describes the current flow. [ADR 001](001-postgresql-document-eligibility.md) covers eligibility, and [ADR 004](004-native-chroma-vector-retrieval.md) covers native Chroma.

## Evidence and tradeoffs

In the 18-question provisional pilot, hybrid Recall@5 was 89.8%, compared with 87.6% for vector and 82.4% with reranking. Hybrid found relevant top-five evidence for all 18 questions. All 54 runs completed without fallback. Vector had higher Precision@5 than hybrid (38.9% versus 36.7%).

On 12 separate questions over the same three PDFs, hybrid found a relevant top-five hit for 12 questions, compared with 9 for vector. Recall@5 was 91.7% versus 70.8%, and MRR@5 was 0.871 versus 0.625. This comparison used saved vector and hybrid rankings.

The pilot's CPU reranker raised p95 retrieval latency from about 92 ms to 3.86 s and slightly improved MRR@5, from 0.883 to 0.889. A targeted replay reproduced a miss where the reranker moved relevant evidence from rank five to eight. A separate tokenizer check found answer evidence beyond the 512-token input limit. The reranker returns up to 15 eligible chunks by default, including on RRF fallback. That can limit document coverage when comparison or recommendation requests up to 30. These tradeoffs support keeping reranking optional. Historical local replacement trials produced no comparable quality results. One candidate was slow on the tested CPU and another failed to load in the tested environment.

## Limits

Both comparisons use pooled AI-assisted provisional judgments. The 12 questions were authored from PDF evidence before retrieval and held out at the question level. Independent human judgments and document-level generalization remain unverified. The held-out run had no separate retrieval warmup, so its p95 cannot establish a tail-latency advantage. See the [benchmark protocol](../retrieval-evaluation.md#benchmark-protocol).
