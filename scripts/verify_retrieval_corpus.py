"""Verify pilot labels against explicit SQL and Chroma evidence exports."""

import argparse
from collections import defaultdict
from pathlib import Path
import re
from uuid import UUID

if __package__:
    from .validate_retrieval_pilot import read_json, validate_questions, validate_reviews
else:
    from validate_retrieval_pilot import read_json, validate_questions, validate_reviews


def uuid_text(value):
    try:
        return str(UUID(value)) == value
    except (ValueError, AttributeError, TypeError):
        return False


def rows_by_id(rows):
    if not isinstance(rows, list):
        raise ValueError("Export rows must be an array.")
    indexed = defaultdict(list)
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            raise ValueError("Export row is missing its ID.")
        indexed[row["id"]].append(row)
    return indexed


def verify(manifest, sql, chroma, questions, review=None):
    """Return sanitized (question row, label number, finding) triples."""
    keys = ("corpus_id", "index_id", "collection_id")
    if not all(isinstance(item, dict) for item in (manifest, sql, chroma)):
        raise ValueError("Manifest and exports must be JSON objects.")
    if not all(isinstance(manifest.get(key), str) and manifest[key].strip() for key in keys):
        raise ValueError("Manifest corpus, index and collection identities are required.")
    if not all(uuid_text(manifest.get(key)) for key in ("collection_id", "owner_id")):
        raise ValueError("Manifest collection_id and owner_id must be canonical UUIDs.")
    for export in (sql, chroma):
        if export.get("status") != "ok":
            raise ValueError("Store export failed or collection is unavailable.")
        if any(export.get(key) != manifest[key] for key in keys):
            raise ValueError("Store export does not match the selected corpus, index or collection.")
    if sql.get("owner_id") != manifest["owner_id"] or sql.get("collection_pending") is not False:
        raise ValueError("SQL collection is unowned, unavailable or pending deletion.")
    if review is not None and any(review.get(key) != manifest[key] for key in ("corpus_id", "index_id")):
        raise ValueError("Review belongs to a different corpus or index.")
    sql_rows = rows_by_id(sql.get("rows"))
    vectors = rows_by_id(chroma.get("rows"))
    alternatives = defaultdict(dict)
    if review is not None:
        for row in review["judgments"]:
            alternatives[row["question_id"]][row["chunk_id"]] = row["document"]

    count, findings = 0, []
    for row_number, question in enumerate(questions, 1):
        if str(UUID(question["collection_id"])) != manifest["collection_id"]:
            findings.append((row_number, 0, "question collection does not match manifest"))
            continue
        extra = alternatives[question["_id"].strip()]
        labels = dict.fromkeys(question["expected_chunks"], question["expected_documents"])
        for chunk_id, document in extra.items():
            originals = labels.get(chunk_id)
            labels[chunk_id] = [document] if originals is None or document in originals else []
        for label_number, (chunk_id, names) in enumerate(labels.items(), 1):
            count += 1
            problem = compare_chunk(chunk_id, names, sql_rows.get(chunk_id, []), vectors.get(chunk_id, []), manifest)
            if problem:
                findings.append((row_number, label_number, problem))
    return count, findings


def compare_chunk(chunk_id, names, sql, vectors, manifest):
    if len(sql) > 1 or len(vectors) > 1:
        return "duplicate store ID"
    if not sql:
        return "stale Chroma vector without SQL chunk" if vectors else "missing SQL chunk"
    record = sql[0]
    if record.get("collection_id") != manifest["collection_id"] or record.get("owner_id") != manifest["owner_id"]:
        return "wrong document collection or owner"
    if record.get("document_status") != "ready" or record.get("document_pending") is not False:
        return "document is ineligible"
    if record.get("filename") not in names:
        return "document name does not match expected evidence"
    doc_id, suffix = chunk_id.split("_p", 1)
    page, index = map(int, suffix.split("_c"))
    identity = {"document_id": doc_id, "page_number": page, "chunk_index": index}
    if any(type(record.get(key)) is not type(value) or record[key] != value for key, value in identity.items()):
        return "SQL chunk identity mismatch"
    if not vectors or vectors[0].get("vector_present") is not True:
        return "missing Chroma vector"
    vector = vectors[0]
    if any(type(vector.get(key)) is not type(value) or vector[key] != value for key, value in identity.items()):
        return "Chroma chunk identity mismatch"
    for row in (record, vector):
        digest = row.get("text_sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            return "store text hash unavailable or malformed"
    if record["text_sha256"] != vector["text_sha256"]:
        return "SQL and Chroma text mismatch"
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("questions", "manifest", "sql-export", "chroma-export", "report"):
        parser.add_argument(f"--{name}", required=True, type=Path)
    parser.add_argument("--review", type=Path)
    args = parser.parse_args()
    try:
        questions = read_json(args.questions)
        question_map, errors = validate_questions(questions)
        review = read_json(args.review) if args.review else None
        if args.review:
            errors += validate_reviews(review, question_map)[1]
        if errors:
            raise ValueError("Question or review structure failed. Run the pilot validator first.")
        count, findings = verify(
            read_json(args.manifest), read_json(args.sql_export), read_json(args.chroma_export), questions, review,
        )
        lines = [
            "# Corpus verification",
            "",
            f"- Labels checked: {count}",
            f"- Unresolved findings: {len(findings)}",
            "- Semantic relevance requires human review.",
            "",
        ]
        for row, label, finding in findings:
            location = f"Question row {row}" + (f", label {label}" if label else "")
            lines.append(f"- {location}: {finding}")
        with args.report.open("x", encoding="utf-8") as output:
            output.write("\n".join(lines) + "\n")
    except ValueError as error:
        parser.error(str(error))
    except OSError:
        parser.error("Could not read an input or create a new report. Existing files are preserved.")
    return int(bool(findings))


if __name__ == "__main__":
    raise SystemExit(main())
