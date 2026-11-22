# ADR 004 — Use native Chroma for vector retrieval

**Status** — Implemented. Native Chroma remains in use.
[ADR 006](006-remove-langchain-core.md) superseded the original result type
and embedding wrapper described here.

## Context

Ingestion, BM25 hydration, and deletion already use the native Chroma SDK.
The vector adapter queried the same embedded cosine index and converted the
first query batch into documents and distances. The preceding experiment
compared both paths using identical MiniLM vectors. All 19 returned rows
matched in order, text, normalized metadata, and canonical identity. The
maximum absolute distance difference was zero.

## Decision

Reuse the native read-only collection lookup. Embed the query with the
existing cached MiniLM model, call `Collection.query` with the explicit
embedding, and convert the first response batch to `(Document, distance)`
pairs. Chroma determines row order and cosine distances. The vector lookup
keeps canonical chunk IDs derived from metadata. Missing collections remain
absent and return empty results. Null metadata becomes an empty dictionary.
A stored vector with no text still raises the existing `Document` validation
error. Eligibility, BM25, RRF, reranking, and citation formulas stay the same.

Retain LangChain Core `Document` and `Embeddings` for the current callers and
benchmark utilities. Keep `langchain-groq` for model calls. Removing
`langchain-chroma` eliminates one installed distribution at the tested
dependency versions. NumPy and LangChain Core are already used at runtime.
Their previous compatibility bounds are now explicit in `requirements.txt`
so removing the adapter does not widen the resolver's choices.

## Alternatives and tradeoffs

Keeping the adapter would preserve the existing call through a separate
package. Calling the SDK directly makes lookup and result conversion visible
in one function. Converting native rows is a small amount of application
code. A new result type would widen the change and affect utilities that
check for `Document`. Raw Chroma IDs are available, but changing which ID
is authoritative would alter retrieval behavior and needs a separate review.

## Verification

Permanent tests exercise the production function against temporary Chroma
with deterministic vectors, including distance ties, oversized result limits,
empty or missing collections, filtering, null metadata, and missing text.
See the [engineering log](../engineering-log.md) for the adapter comparison,
fresh install, and regression results. No performance or live provider
comparison was made.
