# ADR 005 — Use the native Groq SDK for generation

**Status** — Implemented. The native Groq provider remains in use.
[ADR 006](006-remove-langchain-core.md) superseded the remaining Core
dependencies described in this record.

## Context

ChatGroq wrapped the Groq SDK, serialized prompt strings, built named tool
schemas, and parsed responses. InCite already owned prompts, model order,
fallback policy, response schemas, and attribution. The audit matched 32
ordinary request and fallback cases and found differences in how a simple
native prototype handled malformed and multiple tool calls.

## Decision

Use the official Groq SDK through `GroqProvider.generate_text()` and
`generate_structured()`. Reuse a synchronous client per model. Keep the
single user message, temperature, completion options, named function choice,
and Pydantic tool descriptors, including existing schema descriptions. A
missing tool returns `None`. When several
tools are present, skip undecodable calls, then validate the first decodable
call against the requested schema. Malformed arguments and unexpected tool
names surface as provider parse errors. Blank plain responses retain the
existing QA fallback.

Recognize quota and transient errors through SDK exceptions, HTTP statuses,
and structured provider codes. The previous string search could mistake the
word “quota” in malformed tool JSON for provider quota exhaustion. QA now
tries plain text on the same model in that case. Comparison and recommendation
follow their ordinary parse-error path. Free-form diagnostic text alone no
longer triggers quota or transient fallback. Model order stays the same.
QA records the successful requested model, while comparison and recommendation
retain their existing primary-model attribution.

Keep two SDK retries and explicitly pass `timeout=None`, as the wrapper did.
Using the native SDK defaults would add a per-phase HTTP timeout. QA still
allows one application retry after a timeout or service-unavailable error,
and comparison and recommendation still make one application attempt for
those failures. A bounded timeout and revised attribution policy can be
reviewed separately.

## Alternatives and tradeoffs

Keeping ChatGroq would leave schema conversion and parser rules in the
wrapper. The native provider makes those rules visible in the app, which
now owns the tool schema conversion and must preserve it with tests.
LangChain Core remains for retrieval `Document` and embeddings. A different
document type or a new fallback policy would widen this change.

## Verification

Permanent tests use the production provider with mock HTTP. They check
legacy request fingerprints for the three structured schemas, error paths,
request counts and order, timeouts, attribution, and prompt boundaries.
A clean Python 3.11 install passed `pip check`, 123 unit-runner tests, and
33 PostgreSQL/Chroma integration tests.
See the [engineering log](../engineering-log.md) for clean-environment
verification and limitations.
