import json

import pytest

from scripts.validate_retrieval_pilot import main, make_report, validate_questions, validate_reviews


DOC = "00000000-0000-0000-0000-000000000001"
FIRST = f"{DOC}_p1_c0"
SECOND = f"{DOC}_p1_c1"
ALTERNATIVE = f"{DOC}_p2_c0"


def question(**changes):
    return {
        "_id": "pilot-1",
        "question": "How does this work?",
        "collection_id": DOC,
        "expected_documents": ["fixture.pdf"],
        "expected_chunks": [FIRST, SECOND],
        **changes,
    }


def judgment(chunk_id=FIRST, **changes):
    return {
        "question_id": "pilot-1",
        "document": "fixture.pdf",
        "chunk_id": chunk_id,
        "evidence_ref": "fixture page 1",
        "judgment": "relevant",
        "manually_verified": True,
        "notes": "Supports the answer.",
        **changes,
    }


def review(*entries):
    return {"corpus_id": "fixture-v1", "index_id": "fixture-build-1", "judgments": list(entries)}


def test_valid_question_with_multiple_labels():
    assert validate_questions([question()]) == ({"pilot-1": question()}, [])


@pytest.mark.parametrize(("changes", "message"), [
    ({"_id": None}, "missing or invalid _id"),
    ({"collection_id": "bad"}, "invalid collection_id"),
    ({"expected_documents": []}, "expected_documents"),
    ({"expected_chunks": []}, "at least one"),
    ({"expected_chunks": [f"{DOC}_p0_c1"]}, "malformed canonical"),
    ({"expected_chunks": [FIRST, FIRST]}, "duplicate chunk label"),
])
def test_invalid_question_fields(changes, message):
    assert message in validate_questions([question(**changes)])[1][0]


def test_duplicate_id_and_normalized_text():
    _, issues = validate_questions([question(), question(question="HOW does this work!")])
    assert any("duplicate _id" in issue for issue in issues)
    assert any("duplicate normalized" in issue for issue in issues)


def test_reviews_include_alternative_and_count_assessed_originals():
    records, _ = validate_questions([question()])
    entries = review(judgment(), judgment(SECOND, judgment="irrelevant"), judgment(ALTERNATIVE))
    assert validate_reviews(entries, records) == (1, [])


def test_unverified_and_uncertain_do_not_complete_review():
    records, _ = validate_questions([question()])
    entries = review(judgment(), judgment(SECOND, judgment="uncertain", manually_verified=False))
    assert validate_reviews(entries, records) == (0, [])


@pytest.mark.parametrize("changes", [
    {"notes": ""},
    {"evidence_ref": None},
    {"document": ""},
    {"judgment": []},
    {"judgment": "uncertain"},
    {"manually_verified": "yes"},
    {"question_id": "unknown"},
])
def test_invalid_review_entry(changes):
    assert validate_reviews(review(judgment(**changes)), {"pilot-1": question()})[1]


def test_incomplete_review_identity():
    assert validate_reviews({"judgments": []}, {})[1] == [
        "Review is missing corpus_id.", "Review is missing index_id."
    ]


def test_report_omits_raw_values():
    report, failed = make_report([question()], None, include_reviews=False)
    assert not failed
    assert "Dataset structure: PASS" in report
    assert "2: 1" in report
    assert "fixture.pdf" not in report and FIRST not in report


def test_cli_writes_separate_report_and_cannot_overwrite_input(tmp_path, monkeypatch):
    source = tmp_path / "questions.json"
    source.write_text(json.dumps([question()]), encoding="utf-8")
    original = source.read_bytes()
    report = tmp_path / "report.md"
    monkeypatch.setattr("sys.argv", ["validator", "--questions", str(source), "--report", str(report)])
    assert main() == 0
    assert "Dataset structure: PASS" in report.read_text(encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["validator", "--questions", str(source), "--report", str(source)])
    with pytest.raises(SystemExit, match="2"):
        main()
    assert source.read_bytes() == original
