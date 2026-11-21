# Engineering log

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
