# ADR 003 — Remove unused AI framework dependencies

## Context

InCite owns ingestion, retrieval ranking, PostgreSQL eligibility, prompts,
fallback policy, and citations. It uses LangChain adapters for Chroma vector
queries and Groq responses. The standalone Gemini smoke script was retired
earlier, and the application has no Gemini call sites.

## Decision

Remove the direct declarations for `langchain`, `langchain-community`,
`langchain-google-genai`, and `google-genai`. The source and dependency
graph audit found no project imports of those packages. The installed
`langchain-community` required `langchain`, so both declarations are removed
together. Neither retained adapter requires any of the four packages.

Keep the existing `langchain-chroma` and `langchain-groq` pins. The former
wraps vector search and the latter handles plain and structured model
responses. Both still require `langchain-core`, which also provides the
embedding interface and document type used by InCite. Core remains a
transitive dependency. The Python requirements file supplies the runtime
dependencies. The separate frontend lockfile has no related changes.

## Tradeoffs

Keeping the unused packages increases the dependency graph without serving
an application path. Replacing the active adapters at the same time would
also change the vector result and provider boundaries. Retaining them
keeps this change limited to dependency declarations and leaves native SDK
migration for a separate decision with behavior parity tests. Build size
and request latency are separate measurements.

## Verification

A clean Python 3.11 install omitted all four packages and passed
`pip check`, 83 unit tests, and 33 PostgreSQL/Chroma integration tests.
Offline adapter and utility checks passed. See the
[engineering log](../engineering-log.md) for the resolved versions and
test scope.
