"""
Regression tests for classify_intent().

These tests encode the exact boundary that the intent classifier must respect:

  COMPARE fires ONLY when a compare-verb AND a document-reference signal
  are both present in the message. Concept-comparison questions must remain
  QUESTION intent regardless of compare-sounding words.

Run with:
    pytest tests/test_intent_classifier.py -v
"""

import sys
from pathlib import Path

# Make the project root importable without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.chat_service import IntentType, classify_intent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _q(message: str) -> IntentType:
    """Classify a message with an empty conversation history."""
    return classify_intent(message, history=[])


# ---------------------------------------------------------------------------
# COMPARE — should fire (compare-verb + document reference)
# ---------------------------------------------------------------------------

class TestCompareTrue:
    """Messages that explicitly compare documents and must → COMPARE."""

    def test_compare_named_pdf(self):
        assert _q("compare Notes.pdf and SystemDesign.pdf") == IntentType.COMPARE

    def test_compare_the_documents(self):
        assert _q("compare the documents") == IntentType.COMPARE

    def test_compare_these_documents(self):
        assert _q("compare these documents") == IntentType.COMPARE

    def test_compare_both_files(self):
        assert _q("compare both files") == IntentType.COMPARE

    def test_compare_first_two_documents(self):
        assert _q("compare the first two documents") == IntentType.COMPARE

    def test_compare_the_two_pdfs(self):
        assert _q("compare the two PDFs") == IntentType.COMPARE

    def test_vs_with_pdf(self):
        assert _q("ML_Notes.pdf vs SystemDesign.pdf — which is better?") == IntentType.COMPARE

    def test_versus_document(self):
        assert _q("Notes versus the other document") == IntentType.COMPARE

    def test_contrast_these_files(self):
        assert _q("contrast these files") == IntentType.COMPARE

    def test_side_by_side_pdfs(self):
        assert _q("show me a side by side comparison of the PDFs") == IntentType.COMPARE

    def test_compare_uploaded(self):
        assert _q("compare the uploaded files") == IntentType.COMPARE

    def test_compare_both_docs(self):
        assert _q("compare both docs") == IntentType.COMPARE

    def test_compare_which_file(self):
        assert _q("compare and tell me which file is better") == IntentType.COMPARE

    def test_compare_which_doc(self):
        assert _q("compare and tell me which doc covers this better") == IntentType.COMPARE


# ---------------------------------------------------------------------------
# COMPARE — must NOT fire (concept comparisons stay QUESTION)
# ---------------------------------------------------------------------------

class TestCompareDoesNotFire:
    """Concept-comparison questions that must remain QUESTION intent."""

    def test_difference_between_concepts(self):
        assert _q("What is the difference between supervised and unsupervised learning?") == IntentType.QUESTION

    def test_how_does_x_differ_from_y(self):
        assert _q("How does logistic regression differ from linear regression?") == IntentType.QUESTION

    def test_differ_word_alone(self):
        assert _q("How do neural networks differ from decision trees?") == IntentType.QUESTION

    def test_versus_concepts(self):
        assert _q("What is bagging versus boosting?") == IntentType.QUESTION

    def test_contrast_concepts(self):
        assert _q("Contrast deep learning and traditional ML approaches") == IntentType.QUESTION

    def test_similarities_and_differences_concepts(self):
        assert _q("What are the similarities and differences between SVM and logistic regression?") == IntentType.QUESTION

    def test_compare_two_algorithms(self):
        # "compare" is present but "algorithms" is not a document-reference signal
        assert _q("compare gradient descent and stochastic gradient descent") == IntentType.QUESTION

    def test_which_is_better_concepts(self):
        assert _q("Which is better between overfitting and underfitting?") == IntentType.QUESTION

    def test_vs_without_doc_reference(self):
        # " vs " alone without a document signal → QUESTION
        assert _q("precision vs recall — what should I optimize for?") == IntentType.QUESTION

    def test_compare_approaches(self):
        assert _q("compare the two approaches to regularization") == IntentType.QUESTION

    def test_how_do_they_differ_concepts(self):
        assert _q("How do transformers and RNNs differ?") == IntentType.QUESTION

    def test_difference_between_methods(self):
        assert _q("Explain the difference between batch and online learning") == IntentType.QUESTION


# ---------------------------------------------------------------------------
# RECOMMEND — positive cases (must not be confused with COMPARE)
# ---------------------------------------------------------------------------

class TestRecommend:
    """Messages that should → RECOMMEND, not COMPARE or QUESTION."""

    def test_which_document(self):
        assert _q("Which document should I read for interview prep?") == IntentType.RECOMMEND

    def test_recommend_keyword(self):
        assert _q("Can you recommend a document for quick revision?") == IntentType.RECOMMEND

    def test_which_pdf_to_study(self):
        assert _q("Which PDF is best for beginners?") == IntentType.RECOMMEND

    def test_which_file(self):
        assert _q("Which file should I start with?") == IntentType.RECOMMEND

    def test_best_for(self):
        assert _q("What is best for exam prep?") == IntentType.RECOMMEND

    def test_quick_revision(self):
        assert _q("What should I read for quick revision?") == IntentType.RECOMMEND


# ---------------------------------------------------------------------------
# QUESTION — normal factual queries
# ---------------------------------------------------------------------------

class TestQuestion:
    """Normal questions that must remain QUESTION."""

    def test_what_is_ml(self):
        assert _q("What is machine learning?") == IntentType.QUESTION

    def test_explain_concept(self):
        assert _q("Explain backpropagation in neural networks") == IntentType.QUESTION

    def test_how_does_work(self):
        assert _q("How does gradient descent work?") == IntentType.QUESTION

    def test_what_are_examples(self):
        assert _q("Give me examples of unsupervised learning algorithms") == IntentType.QUESTION

    def test_define(self):
        assert _q("Define overfitting and how to prevent it") == IntentType.QUESTION
