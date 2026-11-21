import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import UUID, uuid4

from langchain_core.documents import Document as RetrievedDocument
from sqlalchemy import create_engine, delete
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import Base, ChatSession, Collection, Document, Message, User
from app.models.chat import MessageRole
from app.models.document import DocumentStatus
from app.services import chat_service, collection_service, retrieval_service


class TestDocumentEligibility(unittest.TestCase):
    def setUp(self):
        for service in (chat_service, chat_service.comparison_service):
            self.enterContext(patch.object(service, "get_llm", side_effect=AssertionError("Unexpected provider call")))
        engine = create_engine("sqlite://")
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine, tables=[
            User.__table__, Collection.__table__, Document.__table__, ChatSession.__table__, Message.__table__,
        ])
        self.db = Session(engine)
        self.addCleanup(self.db.close)
        self.user_id = uuid4()
        self.other_user_id = uuid4()
        self.collection = uuid4()
        self.other_collection = uuid4()
        self.db.add_all([
            User(id=self.user_id, email="owner@example.test", hashed_password="unused"),
            User(id=self.other_user_id, email="other@example.test", hashed_password="unused"),
            Collection(id=self.collection, user_id=self.user_id, name="Owned"),
            Collection(id=self.other_collection, user_id=self.other_user_id, name="Other"),
        ])
        self.db.commit()
        self.ready = self.make_document("ready", DocumentStatus.ready, legacy=True)
        self.second = self.make_document("another", DocumentStatus.ready)
        self.processing = self.make_document("processing", DocumentStatus.processing)
        self.failed = self.make_document("failed", DocumentStatus.failed)
        self.foreign = self.make_document("foreign", DocumentStatus.ready, self.other_collection)
        self.deleted = self.make_document("deleted", DocumentStatus.ready)
        self.db.execute(delete(Document).where(Document.id == UUID(self.deleted.metadata["document_id"])))
        self.db.commit()
        self.orphan = self.make_chunk(uuid4(), "orphan")

    def make_document(self, name, status, collection=None, legacy=False):
        document_id = uuid4()
        self.db.add(Document(
            id=document_id,
            collection_id=collection or self.collection,
            filename=f"{name}.pdf",
            file_path="synthetic.pdf",
            status=status,
        ))
        self.db.commit()
        chunk = self.make_chunk(document_id, name)
        if legacy:
            del chunk.metadata["document_name"]
        return chunk

    @staticmethod
    def make_chunk(document_id, text):
        return RetrievedDocument(page_content=text, metadata={
            "document_id": str(document_id),
            "document_name": "untrusted.pdf",
            "page_number": 1,
            "chunk_index": 0,
        })

    @staticmethod
    def chunk_id(chunk):
        return f"{chunk.metadata['document_id']}_p1_c0"

    def vector_candidates(self, *chunks):
        store = Mock()
        store.similarity_search_with_score.side_effect = (
            lambda _query, k: [(doc, i / 10) for i, doc in enumerate(chunks[:k], 1)]
        )
        self.enterContext(patch.object(retrieval_service, "_get_vectorstore", return_value=store))
        return store

    def bm25_candidates(self, *chunks, hydrated=None):
        ranked = [(self.chunk_id(doc), 10.0 - i) for i, doc in enumerate(chunks)]
        self.enterContext(patch.object(retrieval_service, "_bm25_retrieve", return_value=ranked))
        store = Mock()
        documents = hydrated if hydrated is not None else {self.chunk_id(doc): doc for doc in chunks}

        def fetch(ids, include):
            found = [chunk_id for chunk_id in ids if chunk_id in documents]
            return {
                "ids": found,
                "documents": [documents[chunk_id].page_content for chunk_id in found],
                "metadatas": [documents[chunk_id].metadata for chunk_id in found],
            }

        store.get.side_effect = fetch
        self.enterContext(patch.object(retrieval_service, "get_collection_store", return_value=store))
        return store

    def retrieve(self, mode="vector", top_k=5):
        with patch.object(settings, "RETRIEVAL_MODE", mode):
            return retrieval_service.retrieve(self.collection, "query", self.db, top_k=top_k)

    def test_vector_filters_ineligible_and_refills_in_rank_order(self):
        store = self.vector_candidates(
            self.processing, self.failed, self.deleted, self.orphan, self.foreign, self.second, self.ready,
        )
        with patch.object(self.db, "execute", wraps=self.db.execute) as execute:
            results = self.retrieve(top_k=2)
        self.assertEqual([(d.page_content, score) for d, score in results], [("another", .6), ("ready", .7)])
        self.assertEqual([d.metadata["document_name"] for d, _ in results], ["another.pdf", "ready.pdf"])
        self.assertEqual(execute.call_count, 1)
        store.similarity_search_with_score.assert_called_once_with("query", k=8)

    def test_hybrid_keeps_original_fusion_scores_and_admits_valid_bm25_only_hit(self):
        self.vector_candidates(self.deleted, self.ready)
        store = self.bm25_candidates(self.deleted, self.foreign, self.second, self.ready)
        results = self.retrieve("hybrid", top_k=2)
        self.assertEqual([d.page_content for d, _ in results], ["ready", "another"])
        self.assertAlmostEqual(results[0][1], 1 / (settings.BM25_RRF_K + 2) + 1 / (settings.BM25_RRF_K + 4))
        self.assertAlmostEqual(results[1][1], 1 / (settings.BM25_RRF_K + 3))
        self.assertEqual(store.get.call_args.kwargs["ids"], [self.chunk_id(self.second)])

    def test_hybrid_rejects_mismatched_hydrated_metadata(self):
        self.vector_candidates(self.ready)
        mismatched = self.make_chunk(UUID(self.ready.metadata["document_id"]), "wrong chunk")
        self.bm25_candidates(self.second, hydrated={self.chunk_id(self.second): mismatched})
        self.assertEqual([d.page_content for d, _ in self.retrieve("hybrid")], ["ready"])

    def test_reranker_only_receives_eligible_text(self):
        self.vector_candidates(self.processing, self.ready, self.failed, self.second)
        self.bm25_candidates(self.processing, self.ready, self.second)
        reranker = Mock()
        reranker.predict.return_value = [1.0, 3.0]
        with patch.object(retrieval_service, "_get_reranker", return_value=reranker):
            results = self.retrieve("hybrid_rerank", top_k=2)
        reranker.predict.assert_called_once_with([("query", "ready"), ("query", "another")])
        self.assertEqual([(d.page_content, score) for d, score in results], [("another", 3.0), ("ready", 1.0)])

    def test_vector_fallback_still_checks_eligibility(self):
        self.vector_candidates(self.failed, self.ready)
        with patch.object(retrieval_service, "_bm25_retrieve", side_effect=RuntimeError):
            self.assertEqual([d.page_content for d, _ in self.retrieve("hybrid")], ["ready"])

    def test_malformed_metadata_is_ignored(self):
        self.vector_candidates(RetrievedDocument(page_content="malformed"), self.ready)
        self.assertEqual([d.page_content for d, _ in self.retrieve()], ["ready"])

    def test_no_candidates_skips_eligibility_query(self):
        self.vector_candidates()
        with patch.object(self.db, "execute") as execute:
            self.assertEqual(self.retrieve(), [])
        execute.assert_not_called()

    def test_missing_database_session_skips_search(self):
        store = self.vector_candidates(self.ready)
        self.assertEqual(retrieval_service.retrieve(self.collection, "query", None), [])
        store.similarity_search_with_score.assert_not_called()

    def save_history(self, *turns):
        session_id = uuid4()
        self.db.add(ChatSession(id=session_id, user_id=self.user_id, collection_id=self.collection))
        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        for index, (role, content) in enumerate(turns):
            self.db.add(Message(
                session_id=session_id,
                role=role,
                content=content,
                created_at=start + timedelta(seconds=index),
            ))
        self.db.commit()
        return session_id

    def chat(self, question, user_id=None, mode="vector", session_id=None):
        session = SimpleNamespace(id=uuid4())
        with patch.object(settings, "RETRIEVAL_MODE", mode), \
             patch.object(chat_service.chat_repository, "create_session", return_value=session), \
             patch.object(chat_service.chat_repository, "create_message", return_value=SimpleNamespace(id=uuid4())), \
             patch.object(chat_service.query_log_repository, "create"):
            return chat_service.send_message(self.db, user_id or self.user_id, self.collection, question, session_id)

    def test_other_user_is_rejected_before_vector_search(self):
        store = self.vector_candidates(self.ready)
        with self.assertRaises(collection_service.CollectionNotFoundError):
            self.chat("What is covered?", self.other_user_id)
        store.similarity_search_with_score.assert_not_called()

    def test_all_invalid_skips_generation_for_every_intent(self):
        self.vector_candidates(self.processing, self.failed, self.deleted, self.foreign, self.orphan)
        self.bm25_candidates(self.orphan, self.deleted, self.foreign)
        session_id = self.save_history((MessageRole.assistant, "Historical excerpt: deleted"))
        questions = ("What is covered?", "Compare ready.pdf and another.pdf", "Which PDF should I read?")
        with patch.object(chat_service, "_invoke_with_fallback") as answer_provider, \
             patch.object(chat_service.comparison_service, "_llm_structured_call") as comparison_provider, \
             patch.object(retrieval_service, "_get_reranker") as reranker:
            for mode in ("vector", "hybrid", "hybrid_rerank"):
                for question in questions:
                    with self.subTest(mode=mode, question=question):
                        result = self.chat(question, mode=mode, session_id=session_id)
                        self.assertEqual(result[1], chat_service._no_confidence_answer()[0])
                        self.assertEqual(result[3], [])
            answer_provider.assert_not_called()
            comparison_provider.assert_not_called()
            reranker.assert_not_called()

    def test_failed_sql_check_refuses_and_rolls_back(self):
        self.vector_candidates(self.ready)
        failure = OperationalError("synthetic read", {}, RuntimeError())
        execute = self.db.execute

        def fail_eligibility_query(statement, *args, **kwargs):
            if [column["name"] for column in statement.column_descriptions] == ["id", "filename"]:
                raise failure
            return execute(statement, *args, **kwargs)

        with patch.object(self.db, "execute", side_effect=fail_eligibility_query), \
             patch.object(self.db, "rollback", wraps=self.db.rollback) as rollback, \
             patch.object(chat_service, "_invoke_with_fallback") as provider:
            result = self.chat("What is covered?")
        self.assertEqual(result[1], chat_service._no_confidence_answer()[0])
        rollback.assert_called_once()
        provider.assert_not_called()

    def test_qa_prompt_and_citations_only_include_eligible_chunks(self):
        self.vector_candidates(self.deleted, self.ready)
        with patch.object(chat_service, "_invoke_with_fallback", return_value=("Answer", "Sources", [], "test")) as provider:
            result = self.chat("What is covered?")
        prompt = provider.call_args.args[0]
        self.assertIn("[Page 1, ready.pdf]", prompt)
        self.assertNotIn("deleted", prompt)
        self.assertEqual([citation["document_name"] for citation in result[3]], ["ready.pdf"])

    def test_comparison_and_recommendation_prompts_only_include_eligible_chunks(self):
        self.vector_candidates(self.processing, self.ready, self.deleted, self.second, self.foreign)
        response = SimpleNamespace(summary="Summary", rankings=[], sections=[], follow_up_questions=[])
        with patch.object(chat_service.comparison_service, "_llm_structured_call", return_value=response) as provider:
            self.chat("Compare ready.pdf and another.pdf")
            self.chat("Which PDF should I read?")
        self.assertEqual(provider.call_count, 2)
        for call in provider.call_args_list:
            prompt = call.args[0]
            self.assertIn("[Page 1]: ready", prompt)
            self.assertIn("[Page 1]: another", prompt)
            for invalid_text in ("processing", "deleted", "foreign", "untrusted.pdf"):
                self.assertNotIn(invalid_text, prompt)

    def test_unavailable_assistant_history_is_excluded_from_all_three_prompts(self):
        unavailable = (self.deleted, self.processing, self.failed)
        old_answers = [f"Historical excerpt: {chunk.page_content}" for chunk in unavailable]
        prior_question = "I am studying for a networking exam."
        session_id = self.save_history(
            (MessageRole.user, prior_question),
            *((MessageRole.assistant, answer) for answer in old_answers),
        )
        self.vector_candidates(*unavailable, self.ready, self.second)
        response = SimpleNamespace(summary="Summary", rankings=[], sections=[], follow_up_questions=[])
        with patch.object(chat_service, "_invoke_with_fallback", return_value=("Answer", "Sources", [], "test")) as qa, \
             patch.object(chat_service.comparison_service, "_llm_structured_call", return_value=response) as other:
            self.chat("What is covered?", session_id=session_id)
            self.chat("Compare ready.pdf and another.pdf", session_id=session_id)
            self.chat("Which PDF should I read?", session_id=session_id)
        qa.assert_called_once()
        self.assertEqual(other.call_count, 2)
        for call in qa.call_args_list + other.call_args_list:
            prompt = call.args[0]
            self.assertIn(prior_question, prompt)
            self.assertIn("ready.pdf", prompt)
            for chunk, old_answer in zip(unavailable, old_answers):
                self.assertNotIn(old_answer, prompt)
                self.assertNotIn(f"\n{chunk.page_content}\n", prompt)
                self.assertNotIn(f"]: {chunk.page_content}\n", prompt)

    def test_qa_followup_uses_recent_user_messages_and_keeps_display_history(self):
        user_questions = ["Old topic", "What is a router?", "How does routing work?", "What is static routing?"]
        prior_answer = "A historical assistant explanation about static routing."
        session_id = self.save_history(
            *((MessageRole.user, question) for question in user_questions),
            (MessageRole.assistant, prior_answer),
        )
        self.save_history((MessageRole.user, "Unrelated session question"))
        self.vector_candidates(self.ready)
        with patch.object(chat_service, "_invoke_with_fallback", return_value=("Follow-up answer", "Sources", [], "test")) as provider:
            result = self.chat("When should I use it?", session_id=session_id)
        self.assertEqual(result[1], "Follow-up answer")
        prompt = provider.call_args.args[0]
        positions = [prompt.index(question) for question in user_questions[1:]]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(prompt.count("When should I use it?"), 1)
        for excluded in (user_questions[0], prior_answer, "Unrelated session question"):
            self.assertNotIn(excluded, prompt)
        display_history = chat_service.list_messages(self.db, self.user_id, session_id)
        self.assertEqual([message.content for message in display_history], [*user_questions, prior_answer])

    def test_comparison_followup_can_use_document_names_from_prior_user_message(self):
        session_id = self.save_history(
            (MessageRole.user, "I want to study ready.pdf and another.pdf."),
            (MessageRole.assistant, "Historical reply without document names."),
        )
        self.vector_candidates(self.ready, self.second)
        response = SimpleNamespace(sections=[], follow_up_questions=[])
        with patch.object(chat_service.comparison_service, "_llm_structured_call", return_value=response) as provider:
            self.chat("Compare both PDFs", session_id=session_id)
        provider.assert_called_once()
        prompt = provider.call_args.args[0]
        self.assertIn("Document: ready.pdf", prompt)
        self.assertIn("Document: another.pdf", prompt)
        self.assertNotIn("Historical reply without document names", prompt)

    def test_comparison_followup_clarifies_when_only_assistant_named_documents(self):
        session_id = self.save_history(
            (MessageRole.user, "I need some revision material."),
            (MessageRole.assistant, "I recommend ready.pdf and another.pdf."),
        )
        self.vector_candidates(self.ready, self.second)
        response = SimpleNamespace(sections=[], follow_up_questions=[])
        with patch.object(chat_service.comparison_service, "_llm_structured_call", return_value=response) as provider:
            result = self.chat("Compare both PDFs", session_id=session_id)
        self.assertIn("Which documents would you like me to compare?", result[1])
        provider.assert_not_called()
