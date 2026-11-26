# Retrieval evaluation pilot

The 30 human-authored questions are a local pilot for testing the evaluation process. The checked-in `scripts/questions.json` is a template in a clean checkout. Use an approved local copy of the labeled dataset. Keep corpus identifiers, questions, excerpts, and review records outside published results unless cleared for sharing.

## Validate the dataset

From the repository root in PowerShell, choose a new report filename for each run.

```powershell
.\.venv\Scripts\python.exe -B scripts/validate_retrieval_pilot.py --questions scripts/questions.json --report "$env:TEMP\incite-pilot-structure.md"
```

The pilot requires a unique `_id`, question text, a collection UUID, expected document filenames, and at least one exact `expected_chunks` value. Each chunk ID has the form `{document_uuid}_p{page_number}_c{chunk_index}`. Pages start at one and chunk indices start at zero. Filenames are review hints. Verify document UUIDs, because filenames can repeat. The retrieval benchmark accepts the same core fields and allows expected document filenames to be omitted. This pilot requires them for review.

The validator opens no stores. It reports structural issues by row number, label counts, and document coverage counts. It returns 0 for structurally valid inputs, 1 for validation findings, and 2 for input or output errors. Reports use exclusive creation, so an existing report or input cannot be overwritten.

## Review relevance

Read each question by ID in the dataset. Open the candidate evidence using a stable local reference. Save judgments in a separate JSON file. The question ID joins each judgment to the original question text. Corpus and index IDs identify one frozen manifest and one index build.

```json
{
  "corpus_id": "synthetic-corpus-v1",
  "index_id": "synthetic-index-build-1",
  "judgments": [
    {
      "question_id": "fixture-question-1",
      "document": "fixture.pdf",
      "chunk_id": "00000000-0000-0000-0000-000000000001_p1_c0",
      "evidence_ref": "fixture.pdf, page 1, chunk 0",
      "judgment": "uncertain",
      "manually_verified": false,
      "notes": "Check whether the rest of the page answers the question."
    }
  ]
}
```

Use `relevant` for direct support, `partially relevant` for incomplete support, `irrelevant` for no useful support, and `uncertain` when evidence is unavailable or ambiguous. Mark a resolved judgment verified after reading the evidence. Add an extra row for each alternative valid chunk. A verified original label can be judged irrelevant. A structural pass and review coverage count provide no relevance guarantee.

```powershell
.\.venv\Scripts\python.exe -B scripts/validate_retrieval_pilot.py --questions scripts/questions.json --review "$env:TEMP\incite-pilot-review.json" --report "$env:TEMP\incite-pilot-review-report.md"
```

Pool candidates from several retrieval modes, hide their originating mode and rank, and let reviewers add evidence outside that pool. Resolve uncertain judgments before scoring. Synthetic questions often echo their source chunk. The originating chunk and the BM25 proposal list are starting points for review.

## Verify corpus and index before scoring

Prepare a frozen, approved disposable corpus and record document content hashes, ownership and collection mapping, chunk settings, embedding and reranker revisions, and the index build identity. Keep stable evaluation references with an explicit mapping to store UUIDs. Re-ingestion may assign different IDs or change chunk boundaries.

### Verify approved exports

Use exports from the same frozen, authorized build. The verifier accepts local JSON only and has no database or Chroma connection options. Export only from an approved disposable corpus. Existing application stores require separate authorization.

Each JSON object has `corpus_id`, `index_id`, and `collection_id`. Use the same values in all three files. The manifest also specifies the expected `owner_id` independently of the exports.

- **SQL export.** Set `status` to `ok` only after verifying the collection exists and the read succeeds. Include the collection's actual `owner_id`, boolean `collection_pending`, and a `rows` array. In a read-only PostgreSQL transaction, join chunks to documents and collections for all original and additional label IDs. Preserve duplicate rows and rows with wrong ownership or deletion state for diagnosis. Each row contains `id` (the stored `chroma_id`), `document_id`, `collection_id`, `owner_id`, `page_number`, `chunk_index`, `filename`, `document_status`, boolean `document_pending`, and `text_sha256`.
- **Chroma export.** Use existing-collection lookup on a disposable copy, then `get(ids=..., include=["documents", "metadatas", "embeddings"])`. Set `status` to `ok` only if the intended collection exists and the read succeeds. Each entry in `rows` contains the returned `id`, metadata `document_id`, `page_number`, `chunk_index`, boolean `vector_present` derived from the returned embedding, and `text_sha256`. A missing ID stays absent. Record failed or unavailable reads with a non-`ok` status.
- Compute each hash over the exact stored text's UTF-8 bytes. Keep the manifest and exports local. The verifier trusts their provenance and does not authenticate or generate them. The disposable integration test shows how to extract and hash the fields using real stores.

```powershell
.\.venv\Scripts\python.exe -B scripts/verify_retrieval_corpus.py --questions scripts/questions.json `
  --manifest "$env:TEMP\pilot-manifest.json" --sql-export "$env:TEMP\pilot-sql.json" `
  --chroma-export "$env:TEMP\pilot-chroma.json" --report "$env:TEMP\pilot-corpus-report.md"
```

Add `--review PATH` to include alternative evidence from the review file. Choose a new report filename on each run. Exit 0 means the checked labels matched the exports. Exit 1 means the report lists unresolved labels by question row and label position. Positions start at one, with original labels first and then additional judgments in review order. Exit 2 means an input, output, store-export, or corpus identity error prevented verification. Reports omit source text and store identifiers. Resolve every finding and complete human review before scoring.

Run the synthetic verification through the PostgreSQL 16 Docker runner. It provisions its own database and Chroma storage.

```powershell
.\.venv\Scripts\python.exe -B scripts/run_phase1a_integration.py
```

The original three pilot PDFs and matching index manifest were not found in the approved repository files. A clean synthetic verification exercises the workflow. It cannot establish relevance, original source identity, or index alignment for the 30 local pilot questions.

## Future benchmark

Freeze the reviewed labels and use the same held-out questions and result depth for vector, hybrid, and hybrid with reranking. Report Recall@1/3/5, Precision@5, and MRR@5 using a declared binary relevance rule. Add nDCG@5 if reviewers agree on graded relevance. Record cold and warm latency separately, log mode fallbacks and failures, and keep human and synthetic results separate. The pilot tests the protocol before a larger held-out evaluation.
