"""Check pilot labels and optional human review records without opening a store."""

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import unicodedata
from uuid import UUID


CHUNK_ID = re.compile(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}_p[1-9][0-9]*_c(?:0|[1-9][0-9]*)")
JUDGMENTS = {"relevant", "partially relevant", "irrelevant", "uncertain"}


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def string_list(value):
    return isinstance(value, list) and bool(value) and all(map(nonempty, value))


def validate_questions(data):
    issues, records = [], {}
    seen_text = set()
    if not isinstance(data, list) or not data:
        return records, ["Dataset must be a non-empty JSON array."]
    for number, row in enumerate(data, 1):
        where = f"Question row {number}"
        if not isinstance(row, dict):
            issues.append(f"{where}: expected an object.")
            continue
        qid = row.get("_id")
        if not nonempty(qid):
            issues.append(f"{where}: missing or invalid _id.")
        elif qid.strip() in records:
            issues.append(f"{where}: duplicate _id.")
        else:
            records[qid.strip()] = row
        question = row.get("question")
        normalized = ""
        if nonempty(question):
            normalized = " ".join(re.findall(r"\w+", unicodedata.normalize("NFKC", question).casefold()))
        if not normalized:
            issues.append(f"{where}: missing or invalid question text.")
        elif normalized in seen_text:
            issues.append(f"{where}: duplicate normalized question text.")
        seen_text.add(normalized)
        try:
            UUID(row.get("collection_id", ""))
        except (ValueError, TypeError, AttributeError):
            issues.append(f"{where}: invalid collection_id.")
        if not string_list(row.get("expected_documents")):
            issues.append(f"{where}: missing or invalid expected_documents.")
        labels = row.get("expected_chunks")
        if not string_list(labels):
            issues.append(f"{where}: expected_chunks must contain at least one chunk ID.")
        elif any(not CHUNK_ID.fullmatch(label) for label in labels):
            issues.append(f"{where}: malformed canonical chunk ID.")
        elif len(set(labels)) != len(labels):
            issues.append(f"{where}: duplicate chunk label.")
    return records, issues


def validate_reviews(data, questions):
    """Check recorded judgments and count questions with all original labels reviewed."""
    issues, seen, reviewed = [], set(), set()
    if not isinstance(data, dict):
        return 0, ["Review must be a JSON object."]
    for field in ("corpus_id", "index_id"):
        if not nonempty(data.get(field)):
            issues.append(f"Review is missing {field}.")
    entries = data.get("judgments")
    if not isinstance(entries, list):
        return 0, [*issues, "Review judgments must be an array."]
    for number, row in enumerate(entries, 1):
        where = f"Judgment row {number}"
        if not isinstance(row, dict):
            issues.append(f"{where}: expected an object.")
            continue
        qid, chunk = row.get("question_id"), row.get("chunk_id")
        if not isinstance(qid, str) or qid not in questions:
            issues.append(f"{where}: unknown question_id.")
            continue
        if not isinstance(chunk, str) or not CHUNK_ID.fullmatch(chunk):
            issues.append(f"{where}: malformed canonical chunk ID.")
            continue
        key = (qid, chunk)
        if key in seen:
            issues.append(f"{where}: duplicate question/chunk judgment.")
            continue
        seen.add(key)
        if not all(nonempty(row.get(field)) for field in ("document", "evidence_ref", "notes")):
            issues.append(f"{where}: document, evidence_ref and notes are required.")
            continue
        judgment, verified = row.get("judgment"), row.get("manually_verified")
        if not isinstance(judgment, str) or judgment not in JUDGMENTS or not isinstance(verified, bool):
            issues.append(f"{where}: invalid judgment or manually_verified flag.")
            continue
        if verified and judgment == "uncertain":
            issues.append(f"{where}: uncertain evidence cannot be marked verified.")
        elif verified:
            reviewed.add(key)
    complete = sum(
        string_list(row.get("expected_chunks"))
        and all((qid, chunk) in reviewed for chunk in row["expected_chunks"])
        for qid, row in questions.items()
    )
    return complete, issues


def make_report(questions, reviews, *, include_reviews):
    records, dataset_issues = validate_questions(questions)
    complete, review_issues = validate_reviews(reviews, records) if include_reviews else (0, [])
    documents, counts = Counter(), Counter()
    rows = [row for row in questions if isinstance(row, dict)] if isinstance(questions, list) else []
    for row in rows:
        names, labels = row.get("expected_documents"), row.get("expected_chunks")
        if string_list(names):
            documents.update(set(names))
        counts[len(labels) if string_list(labels) else 0] += 1
    lines = [
        "# Pilot validation report",
        "",
        f"- Question rows: {len(questions) if isinstance(questions, list) else 0}",
        f"- Unique question IDs: {len(records)}",
        f"- Distinct expected document references: {len(documents)}",
        "- Per-document question counts (descending): "
        + (", ".join(str(count) for _, count in documents.most_common()) or "none"),
        "- Label counts (labels: questions): "
        + (", ".join(f"{count}: {total}" for count, total in sorted(counts.items())) or "none"),
        f"- Dataset structure: {'FAIL' if dataset_issues else 'PASS'}",
    ]
    if include_reviews:
        lines += [
            f"- Review structure: {'FAIL' if review_issues else 'PASS'}",
            f"- Questions with judgments marked verified for all original labels: {complete}",
        ]
    else:
        lines.append("- Human review file: not supplied")
    lines += ["- Corpus/index checks: not performed", "- Semantic relevance: requires human judgment"]
    issues = dataset_issues + review_issues
    if issues:
        lines += ["", "## Issues", "", *[f"- {issue}" for issue in issues]]
    return "\n".join(lines) + "\n", bool(issues)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", required=True, type=Path)
    parser.add_argument("--review", type=Path)
    parser.add_argument("--report", required=True, type=Path, help="New Markdown report path")
    args = parser.parse_args()
    try:
        questions = read_json(args.questions)
        reviews = read_json(args.review) if args.review else None
        report, errors = make_report(questions, reviews, include_reviews=args.review is not None)
        with args.report.open("x", encoding="utf-8") as output:
            output.write(report)
    except FileExistsError:
        parser.error("Report path already exists. Choose a new file.")
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print("Validation report written.")
    return int(errors)


if __name__ == "__main__":
    raise SystemExit(main())
