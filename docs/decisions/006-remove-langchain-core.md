# ADR 006 — Remove LangChain Core

**Status** — Implemented.

## Context

Native Chroma handles vector search and the official Groq SDK handles
generation. LangChain Core remained for retrieval documents and a query
embedding wrapper. No retained runtime package requires it.

## Decision

Use a local Pydantic-backed `RetrievedChunk` dataclass for both vector hits
and BM25 hydration. A plain dataclass accepted invalid text and shared the
caller's metadata dictionary in the audit. The validated type requires a
string, creates independent default metadata, shallow-copies provided
metadata, and permits downstream mutation and value equality. The optional
ID defaults to `None` and retains numeric-to-string coercion. Hits and
their lookup entries share the same chunk object. Stored vectors with no
text still raise a validation error.

The result type omits LangChain's serialization methods and type tag.
The API already serializes citation dictionaries. Omitting the required
text argument now raises a Pydantic validation error during construction.
Existing retrieval constructors always supply the text argument.

Use `embed_query(text)` to call `get_embedder().encode([text])[0].tolist()`.
This keeps the same one-element batch, model cache, device selection, and
model normalization. Ingestion keeps its existing batch encoding. The
stored vectors, distance metric, canonical IDs, eligibility, ranking, and
citation formulas stay the same.

## Compatibility and verification

The tracked chunk benchmark still reads canonical identity from metadata.
The untracked `scripts/phase4_integration_check.py` stays untouched. Its
Core import on line 47 prevents it from importing in a clean environment
without Core. A future local update should import
`RetrievedChunk` from `app.core.retrieved_chunk` and use that class for the
two type checks on lines 248 and 454.

The regression suite exercises the local constructor, shared object
identity, query call shape, Chroma distances and order, and the existing
retrieval and provider behavior. The [engineering log](../engineering-log.md)
records the MiniLM comparison, test counts, and clean dependency resolution.
