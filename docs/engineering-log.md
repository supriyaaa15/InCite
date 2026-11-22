# Engineering log

This log records decisions, experiments, and verification as they happened.
Test counts and deployment observations apply to the milestone where they
appear. See the [architecture guide](architecture.md) for the current design.

## Milestones

- [Ready-only retrieval and chat history](#phase-1a--ready-only-retrieval)
- [Disposable PostgreSQL and Chroma verification](#phase-1a--postgresql-and-chroma-verification)
- [Durable deletion and recovery](#reliable-deletion-foundation), merged in PR #1
- [Unused AI dependency cleanup](#unused-ai-dependency-cleanup), merged in PR #2
- [Native Chroma vector retrieval](#native-chroma-vector-retrieval), merged in PR #3
- [Native Groq provider](#native-groq-provider), merged in PR #4
- [LangChain Core removal](#remove-langchain-core), merged in PR #5 with 131 unit and 33 integration tests passing

## Phase 1A — Ready-only retrieval

Stale Chroma entries could supply text after their SQL document had been deleted or before ingestion finished. All chat intents now receive evidence from a shared PostgreSQL eligibility check. [ADR 001](decisions/001-postgresql-document-eligibility.md) explains the decision and the alternatives.

- `app/repositories/document_repository.py` queries candidate IDs, collection, and ready status in one SQL statement.
- `app/services/retrieval_service.py` gates all retrieval modes, refills from bounded candidates, and uses verified filenames for citations.
- `app/services/chat_service.py` reuses the existing insufficient-context response across intents. `scripts/benchmark_retrieval_chunk.py` uses the revised citation builder.
- `tests/test_document_eligibility.py` adds 13 regression tests. `tests/test_reranker.py` supplies eligible document rows to the existing ranking fixtures.

**Verification.** Before the fix, the initial 11 tests exposed deleted text reaching QA and processing text reaching reranking. After the fix, all 77 tests completed with zero failures and zero skips in an offline runner. That count includes 13 new tests and 64 existing tests. The eligibility tests use actual SQLAlchemy queries on disposable in-memory SQLite and mocked search and model calls. The local runner supplied import stand-ins for missing `pydantic-settings`, Chroma, LangChain Chroma, and Sentence Transformers. Native focused discovery stopped on the missing `pydantic-settings` import. No live PostgreSQL, Chroma, or provider integration was exercised. In a configured environment, the focused command is `python -m unittest discover -s tests -p test_document_eligibility.py`.

**Cost and limits.** The eligibility batch adds one SQL read for a nonempty candidate pool. Overfetch can increase the vector payload and BM25 hydration work. This phase measured no search latency or production load. The bounded pool can be exhausted by stale hits, and an eligibility read can race later deletion. Complete database outages can also prevent chat persistence.

**Phase 1B.** Define when a document becomes unavailable, coordinate with in-flight ingestion, and make PostgreSQL, Chroma, filesystem, and BM25 cleanup retriable. Define how historical messages and stored citations should be handled after deletion.

### Follow-up — Historical prompt context

Stored assistant replies could reach all three prompt builders independently of ready-only retrieval. Citations contain display fields, and query logs record retrieved candidates. Those records cannot establish the provenance of all claims in an old reply.

- `app/repositories/chat_repository.py` reads only the three most recent user messages for prompt context, in chronological order.
- `app/services/chat_service.py` supplies that context after session ownership and collection checks. `app/services/comparison_service.py` uses it for recommendations and to resolve comparison targets among currently retrieved documents. The UI transcript still reads both roles.
- `tests/test_document_eligibility.py` exercises stored history with SQLite and mocked model calls. Four regressions cover deleted, processing, and failed evidence across all three intents, bounded and session-scoped user context, full transcript access, and comparison references from user versus assistant messages.

**Initial verification.** The four new regressions failed before the fix. After the fix, the offline runner passed all 81 tests, including 17 eligibility tests and 64 existing tests, using the same import stand-ins described above. No Groq call was made. At that point pytest and `pydantic-settings` were missing, blocking native runs. The next section records native and integration verification.

## Phase 1A — PostgreSQL and Chroma verification

The Python 3.11.18 project `.venv` ran native pytest 8.3.5 on Windows. From the repository in PowerShell, run the unit suite with `--unit` and the integration suite with the default invocation.

```powershell
.\.venv\Scripts\python.exe -B -s scripts/run_phase1a_integration.py --unit
.\.venv\Scripts\python.exe -B -s scripts/run_phase1a_integration.py
```

The runner prints the full pytest command and provisions a fresh database per integration run. Use `--pull` if the local Docker Desktop Linux engine lacks `postgres:16`. The verified run used PostgreSQL **16.14** (Debian `16.14-1.pgdg13+1`), from image ID `sha256:be01cf82fc7dbba824acf0a82e150b4b360f3ff93c6631d7844af431e841a95c`. The runner verified the unique container, image, temporary storage, and loopback-only port. The test process matched the database name, user, major version, and PostgreSQL cluster identifier against the container before applying `alembic upgrade head` to the empty database.

The integration tests use Chroma **0.5.15** through the application’s embedded `PersistentClient` in a fresh temporary directory. The application creates per-collection cosine indexes. The fixtures seed two-dimensional vectors and use a deterministic query embedding. PostgreSQL and Chroma reads and writes, BM25 ranking and hydration, JWT authorization, and the FastAPI chat and history routes run through the application. Fake model adapters record structured Groq inputs and reranker inputs. The test environment uses synthetic credentials and blocks outbound HTTP through the application’s HTTP clients.

- **Unit suite** collected and passed all **81** tests with zero failures, errors, or skips. Pytest reported two Pydantic deprecation warnings.
- **Integration suite** collected and passed all **11** cases with zero failures, errors, or skips. Pytest reported five Pydantic deprecation warnings and one Starlette/AnyIO deprecation warning. An initial collection attempt stopped because Chroma needed a home directory. The runner now provides one inside its scratch directory.
- The suite verified ready-only vector and hybrid retrieval, eligible BM25-only hydration, rejection of mismatched Chroma metadata, eligible reranker inputs, authorized prompts and history, and refusal across QA, comparison, and recommendation when all evidence is invalid. PostgreSQL returned `22012` for a deliberately injected division by zero on the eligibility read. The application rolled back, persisted the refusal, and made no model call.
- Each run removed its disposable container and scratch directory. The model adapters, PDF ingestion pipeline, and races after the eligibility read remain outside this verification.

**Tradeoffs.** A follow-up can refer to what the user previously said when current retrieval returns eligible evidence. If only the assistant named the comparison targets, the app asks for clarification. Retrieval still uses the current question as submitted. User text can contain assertions or copied excerpts, so prompts label it as request context and use freshly retrieved excerpts for document claims. No schema change or summarization call is required. See [ADR 001](decisions/001-postgresql-document-eligibility.md#historical-prompt-context) for the alternatives.

## Phase 1A — Pre-commit cleanup

The benchmark CLI now requires `--questions` with a labeled JSON file and validates it before loading application settings or opening a database session. Its single-mode runs, paired comparison, metrics, and Markdown report use the existing evaluation path. Historical benchmark datasets and measurements were kept separate from Phase 1A verification.

The standalone Gemini and remote-Chroma smoke script was retired because the project now uses Groq and embedded Chroma. The legacy Gemini dependencies remain pending a separate dependency review. Vector search remains the configured default, with independent hybrid RRF and CrossEncoder reranking available as options.

The final rerun passed all 81 native unit tests and all 11 PostgreSQL 16.14 and Chroma 0.5.15 integration tests, with zero failures, errors, or skips. Pytest reported the same Pydantic and Starlette/AnyIO deprecation warnings. Benchmark import and help worked without application credentials. Twelve invalid CLI inputs exited with usage errors. A synthetic retrieval adapter exercised the benchmark's single-mode run, three-mode comparison, scoring, and report. No live benchmark corpus or Groq endpoint was used.

## Reliable deletion foundation

Document and collection DELETE now commit a durable request and return
`202 Accepted` with `cleanup_pending`. Pending documents and collections
are excluded from retrieval. Archived collections reject uploads and new
chat turns. Owner-only chat history is retained. Phase 1A's prompt and
retrieval safety checks still apply. Physical cleanup remains pending.
See [ADR 002](decisions/002-reliable-deletion-lifecycle.md) for the state
and transaction boundaries.

The disposable PostgreSQL 16.14 and Chroma 0.5.15 runner passed 82 native
unit tests (81 previous, one new) and 21 integration cases (11 previous,
10 new) with zero failures or skips. The migration upgraded a database
seeded at revision `b2c3d4e5f6a7` and preserved the ready fixture. The tests
covered authorization, duplicates, failed SQL, all retrieval modes,
retained history, and deletion during upload registration and ingestion.
Only synthetic data and fake model adapters were used. Pytest reported
two unit and six integration dependency deprecation warnings.

Both edited frontend pages passed JSX parsing. The full frontend build
could not run because no Node.js version was configured.

## Physical deletion and recovery

The FastAPI worker discovers pending requests in PostgreSQL on startup and
polls every two seconds. It removes matching Chroma vectors, invalidates
BM25, and removes the original PDF from the verified upload directory.
Only then does a short PostgreSQL transaction delete the document and its
chunk rows. Collection cleanup waits for all document rows to be removed,
then removes the collection's Chroma index and empty upload directory.
Archived collection metadata and authorized chat history remain available.

The migration adds durable attempt counts, retry times, and failed-step
labels. Retry delays start at five seconds and cap at five minutes. A
restart resumes pending work, including requests interrupted after an
external deletion. Missing artifacts count as success. Ambiguous paths,
conflicting vector identities, and unregistered files remain for manual
review. The worker does not hold a database transaction during Chroma or
filesystem I/O.

Upload, ingestion, and cleanup share a per-collection OS file lock. An
ingestion write can finish after DELETE is accepted, but cleanup cannot
finish while that writer holds the lock. The embedded Chroma deployment
supports one application process per persistent store. Before rollout,
stop old writers and apply `alembic upgrade head` before starting the
upgraded application. See [ADR 002](decisions/002-reliable-deletion-lifecycle.md)
for transaction boundaries, retry behavior, and deployment limits.

Run both suites from the repository root in PowerShell.

```powershell
.\.venv\Scripts\python.exe -B scripts/run_phase1a_integration.py --unit
.\.venv\Scripts\python.exe -B scripts/run_phase1a_integration.py
```

The final run passed **83 unit tests** and **32 integration cases** with
zero failures, errors, or skips. Pytest reported two unit and six
integration dependency deprecation warnings. The integration runner used
disposable PostgreSQL 16.14, Chroma 0.5.15 with temporary persistence,
temporary uploaded PDFs, synthetic records, and fake model adapters. It
removed the disposable container and scratch data after testing.

The tests checked PostgreSQL, Chroma, and file contents after deletion,
including neighboring collection isolation, missing artifacts, retry after
errors before and after external operations, and a failed PostgreSQL
finalization. They exercised a Chroma write finishing after DELETE was
accepted, followed by cleanup, and recovery during a fresh FastAPI
lifespan. A separate Windows subprocess test verified OS lock release on
abrupt process exit. Full application restart and POSIX lock verification
were still open at that point.

## Cleanup scheduling and deployment verification

A busy collection reproduced starvation by occupying all 16 document slots
on repeated passes. The worker now defers busy targets for four seconds
using the existing retry timestamp. It keeps their failure count and last
error. The regression runs the actual worker with PostgreSQL, Chroma, and
temporary PDFs. The seventeenth document finishes while the original 16
remain locked. After release and expiry, the worker cleans all 16.
Against an isolated copy of `a19fdb3`, the same test failed at the expected
assertion because the seventeenth document was still pending on the next pass.

The PowerShell commands above passed **83 unit tests** and **33 integration
tests**, with zero failures, errors, or skips. Pytest reported two unit and
six integration dependency deprecation warnings. The disposable stack used
PostgreSQL 16.14 and Chroma 0.5.15.

The existing cross-process lock test passed in a Linux Docker container.
The previous Linux check also verified release after `SIGKILL`. The previous
Windows Uvicorn crash test terminated the app after vector and PDF removal
and before SQL finalization. Restart completed cleanup against the same
disposable stores and preserved neighboring artifacts. Graceful shutdown
waited for its active pass. Full Linux application crash recovery and a
kill during a Chroma operation remain unverified.

The image command pins one worker even with `WEB_CONCURRENCY=4`. A BuildKit
probe copied a synthetic context and confirmed 25 private or generated paths
were excluded and 11 intended paths were retained. The full backend image
dependency build was outside this check. The deployment guide now places
backup and migration confirmation before startup, followed by readiness and
cleanup verification. No existing database was migrated.

## Unused AI dependency cleanup

Removed four unused requirements: `langchain`, `langchain-community`,
`langchain-google-genai`, and `google-genai`. No active import used those
packages. The installed Community package depended on the LangChain
metapackage, and neither is required by the Chroma or Groq adapter.
The existing adapter pins and their shared transitive `langchain-core`
remain in place. The runtime declaration count fell from 24 to 20.
The Python requirements have no lockfile to regenerate. See
[ADR 003](decisions/003-remove-unused-ai-dependencies.md).

A fresh Windows Python 3.11.18 environment resolved and installed 135
runtime distributions from those 20 declarations, excluding the virtual
environment's bootstrap tools and the separate pytest install. The four
removed packages were absent. The install retained `langchain-chroma`
0.1.4 and `langchain-groq` 0.3.2 and resolved `langchain-core` 0.3.86
and `groq` 0.37.1. Both `pip check` runs passed, before and after
installing pytest 8.3.5. The uncached Windows install took about 29 minutes
on this machine. No comparable baseline install was timed.

Offline smoke checks exercised the Chroma vector adapter and plain and
structured Groq adapter calls with fake inputs and an in-memory HTTP
transport. All eight utility modules present in the checkout imported,
including the local benchmark tools. The chunk benchmark's `--help` ran
successfully. Dataset workflows and live model calls were outside this
verification.

The two project runner commands above passed **83 unit tests** and
**33 integration tests** in the clean environment, with zero failures,
errors, or skips. Pytest reported two unit and six integration dependency
deprecation warnings. The integration runner used PostgreSQL 16.14 and
Chroma 0.5.15 in disposable storage and removed its container.

## Native Chroma vector retrieval

The preceding MiniLM comparison used identical query vectors against the same
temporary cosine collection. All 19 rows matched in order, text, normalized
metadata, canonical chunk IDs, and raw distances. The maximum distance
difference was zero. The production vector query now uses the same native
SDK as ingestion and BM25 hydration. It calls the existing read-only
collection lookup, obtains a query vector from the shared MiniLM model, and
converts the first query batch to the existing `(Document, distance)` pairs.

The retrieval service is five source lines shorter, including comments.
The refactor removes the adapter factory and makes the native result conversion
explicit. It keeps the canonical metadata-derived ID, the existing
missing-collection and malformed-text behavior, and the ranking, eligibility,
and citation logic. Core's `Document` and embedding interface remain available.
The benchmark still reconstructs the same IDs, and local scripts that check
`isinstance(result, Document)` retain that type contract. The Groq adapter is
unchanged. See [ADR 004](decisions/004-native-chroma-vector-retrieval.md).

Removing `langchain-chroma` drops one installed distribution. NumPy and
LangChain Core already existed in the dependency graph. Their previous
compatibility bounds are now explicit so removing the Chroma adapter does
not widen the version ranges. In the previous Windows environment, the
removed adapter accounted for 45,858 bytes of recorded non-bytecode package
files. No application latency or image-size comparison was performed.

Six permanent tests exercise the production function against disposable
Chroma with deterministic vectors. A separate side-by-side check ran the
production function and the old 0.1.4 adapter against the same real index.
The tied and oversized results, canonical IDs, null metadata, empty and
filtered results, and missing-collection behavior matched. Both paths
raised a `Document` validation error for a vector without stored text.
The eligibility tests also check citation scores in all three modes.

A clean Windows Python 3.11.18 install resolved 134 runtime distributions
from 21 direct declarations, excluding bootstrap and pytest packages.
The preceding clean install resolved 135 from 20 declarations. Core and
NumPy are now declared directly, and `langchain-chroma` is absent. The
resolved versions were Chroma 0.5.15, LangChain Core 0.3.86,
`langchain-groq` 0.3.2, Groq 0.37.1, NumPy 1.26.4, Torch 2.4.1+cpu, and
Sentence Transformers 3.2.0. `pip check` passed with pytest 8.3.5 installed.
Offline smoke checks verified the Torch-to-NumPy conversion, plain and
structured Groq responses with a mock HTTP transport, benchmark chunk IDs,
and import of the local Phase 4 utility. No live provider calls were made.

The clean environment passed **89 unit-runner tests**, including the six
native Chroma cases, and **33 integration tests**, with zero failures,
errors, or skips. Pytest reported two unit-runner and six integration
dependency deprecation warnings. The integration suite used disposable
PostgreSQL 16.14 and Chroma 0.5.15 with deterministic embeddings. It exercised
vector and hybrid retrieval, BM25-only hydration, optional reranking, SQL
eligibility and rollback, deletion-pending exclusion, and citation contracts.
The runner removed its PostgreSQL container after the suite.

## Native Groq provider

Replaced ChatGroq with a cached, synchronous provider over the official Groq
SDK. The provider owns request serialization and Pydantic tool parsing.
Chat and comparison services still own prompts, retry order, user-facing
fallbacks, and model attribution. LangChain Core still supplies retrieval
documents and the embedding interface. See
[ADR 005](decisions/005-native-groq-provider.md).

The audit matched 32 ordinary mock cases and four chat orchestration cases.
The production regression tests compare exact request bodies against the
legacy plain request and all three named tool schemas. They cover malformed
JSON, missing arguments, and an invalid tool call preceding a valid one.
Typed provider errors, HTTP status, and structured codes drive quota and
timeout handling. Malformed JSON containing “quota” now takes the ordinary
parse-error path. QA can try plain text on that model. Comparison and
recommendation keep their existing parse-failure responses.

The provider explicitly retains two SDK retries and `timeout=None`. The audit
observed that omitting the timeout argument would use the Groq SDK defaults
of five seconds for connect and sixty seconds for the other HTTP phases.
Mock HTTP tests measure request order without waiting on real retry delays.
QA can make six primary HTTP attempts for an exhausted 503 or read timeout
before fallback. Comparison and recommendation stop after the SDK's three
attempts for those errors.

A clean Windows Python 3.11.18 install resolved 133 runtime distributions
from 21 direct declarations, excluding bootstrap and pytest packages.
The previous clean install resolved 134. Groq 0.37.1 is a direct dependency
and `langchain-groq` is absent. The install retained LangChain Core 0.3.86,
Chroma 0.5.15, NumPy 1.26.4, Torch 2.4.1+cpu, and Sentence Transformers 3.2.0.
Pydantic resolved to 2.9.2 and HTTPX to 0.28.1. `pip check` passed with
pytest 8.3.5 installed.

The clean environment passed **123 unit-runner tests**, including **34**
native provider cases, and **33 PostgreSQL/Chroma integration tests**.
There were zero failures, errors, or skips. The unit runner reported two
Pydantic class-based configuration deprecation warnings. Integration
reported five warnings of that type and one Starlette warning about the
deprecated AnyIO `BlockingPortal` alias. The runner used disposable
PostgreSQL 16.14 and Chroma 0.5.15 and removed its container. Offline checks
also verified client reuse, benchmark chunk IDs, and import of the local
Phase 4 utility. Verification used mock HTTP or fake providers throughout.
No live model behavior or latency comparison was measured.

## Remove LangChain Core

The retrieval service now constructs local Pydantic-backed
`RetrievedChunk` values for vector hits and BM25-only hydration.
The type validates text and metadata, copies the outer metadata dictionary,
retains mutable fields and value equality, and defaults the optional ID to
`None`. Numeric ID coercion remains available. The vector hit list and
lookup use the same chunk object. The API continues to serialize citation
dictionaries. See [ADR 006](decisions/006-remove-langchain-core.md).

The direct query helper uses the cached SentenceTransformer and the same
one-element input batch. An offline comparison against the embedding module
exported from `774573a` used the same public MiniLM revision and cached
CPU model instance. All four 384-dimensional query vectors matched bit for
bit, including Unicode and whitespace inputs. The three-document batch
also matched exactly. A disposable cosine collection returned identical
order, canonical IDs, and raw distances. No GPU run or performance
comparison was made.

Before removing the requirement, the migrated code passed 131 unit-runner
tests and 33 PostgreSQL/Chroma integration tests. Tests cover constructor
validation, metadata copying and mutation, shared result identity, model
caching, retrieval, SQL eligibility, citation scores, and benchmark IDs.
The 34 native Groq mock-provider cases remain in the unit runner.

The requirements installed successfully in a fresh Windows Python 3.11.18
environment. The clean baseline had 21 direct and 133 total runtime
packages. The new install has 20 direct and 122 total runtime packages,
counted before installing pytest. No LangChain distribution was installed.
Removing Core's `packaging` bound changed the resolved version from 25.0
to 26.3. `pip check` reported no broken requirements with pytest 8.3.5
installed. Chroma 0.5.15, Groq 0.37.1, SentenceTransformers 3.2.0, and
CPU Torch 2.4.1 remained installed.

The clean install passed all 131 unit tests and all 33 integration tests
with zero failures or skips. The integration runner used disposable
PostgreSQL 16.14 and temporary Chroma 0.5.15 storage, then removed the
container. The unit run logged two Pydantic configuration deprecations.
The integration run logged five of those and one Starlette/AnyIO
`BlockingPortal` deprecation. Groq tests used mocked responses.

The untracked Phase 4 utility remains unchanged. Its Core import on line 47
and its type checks on lines 248 and 454 need a future local update to
`app.core.retrieved_chunk.RetrievedChunk`. A clean-environment import check
confirmed that the utility raises `ModuleNotFoundError` for `langchain_core`.
The tracked benchmark imported and returned the same canonical ID from a
retrieval result and a citation mapping.
