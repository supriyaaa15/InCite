"""Exercise the production Groq boundary and fallback policies over mock HTTP."""

from hashlib import sha256
import json
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from groq import APIConnectionError, APITimeoutError, Groq, InternalServerError
import httpx
from pydantic import ValidationError
import pytest

from app.core import llm
from app.core.config import settings
from app.services import chat_service, comparison_service


PRIMARY = "synthetic-primary"
BACKUP = "synthetic-backup"
PROMPT = "Synthetic request\n\nUser: brief context\n\nCitation: sentinel alpha"
BASE_URL = "https://example.invalid"
QA = chat_service.AnswerWithReasoning
COMPARISON = comparison_service._ComparisonResult
RECOMMENDATION = comparison_service._RecommendationResult
RESPONSES = {
    QA.__name__: {"answer": " Answer. ", "reasoning": " Evidence. ", "follow_up_questions": ["Next?", " next?"]},
    COMPARISON.__name__: {"sections": [{"title": "Summary", "content": "Comparison."}], "sources": ["alpha.pdf", "beta.pdf"]},
    RECOMMENDATION.__name__: {"summary": "Recommendation.", "rankings": [
        {"document_name": "alpha.pdf", "rank": 1, "suitability_label": "Concise", "reason": "Evidence."},
    ]},
}


def completion(message, *, empty_choices=False):
    return httpx.Response(200, json={
        "id": "synthetic", "model": "returned-provider-alias", "object": "chat.completion", "created": 1,
        "choices": [] if empty_choices else [{"index": 0, "finish_reason": "stop", "message": message}],
    })


def text_reply(content):
    return completion({"role": "assistant", "content": content})


def tool_arguments(tool_name, arguments):
    return {"id": "synthetic-tool", "type": "function", "function": {"name": tool_name, "arguments": arguments}}


def tool_reply(*tools):
    return completion({"role": "assistant", "content": None, "tool_calls": list(tools)})


def success(_request, body):
    if "tool_choice" not in body:
        return text_reply(" Plain answer. ")
    name = body["tool_choice"]["function"]["name"]
    return tool_reply(tool_arguments(name, json.dumps(RESPONSES[name])))


def error(status, **details):
    return httpx.Response(status, json={"error": details})


@pytest.fixture
def provider_http(monkeypatch):
    calls, retry_delays, fallback_delays = [], [], []
    respond = Mock(side_effect=success)

    def unexpected_transport(*args, **kwargs):
        pytest.fail("The provider test attempted real HTTP.")

    def handle(request):
        assert str(request.url) == BASE_URL + "/openai/v1/chat/completions"
        body = json.loads(request.content)
        calls.append(SimpleNamespace(
            body=body,
            retry=int(request.headers["x-stainless-retry-count"]),
            timeout=request.extensions["timeout"],
        ))
        return respond(request, body)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", unexpected_transport)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", unexpected_transport)
    monkeypatch.setenv("GROQ_API_BASE", BASE_URL)
    monkeypatch.setattr(settings, "LLM_MODEL", PRIMARY)
    monkeypatch.setattr(settings, "LLM_FALLBACK_MODELS", BACKUP)
    monkeypatch.setattr("groq._base_client.time.sleep", retry_delays.append)
    monkeypatch.setattr(chat_service, "time", SimpleNamespace(time=lambda: 0, sleep=fallback_delays.append))
    llm.get_llm.cache_clear()
    with httpx.Client(transport=httpx.MockTransport(handle), trust_env=False) as client:
        monkeypatch.setattr(llm, "Groq", lambda **kwargs: Groq(**kwargs, http_client=client))
        try:
            yield SimpleNamespace(calls=calls, respond=respond, retry_delays=retry_delays, fallback_delays=fallback_delays)
        finally:
            llm.get_llm.cache_clear()


def attempts(provider_http):
    return [(call.body["model"], "tools" in call.body, call.retry) for call in provider_http.calls]


def sdk_attempts(model, *, structured=True):
    return [(model, structured, retry) for retry in range(3)]


def test_plain_request_and_legacy_timeout(provider_http):
    assert llm.get_llm(PRIMARY) is llm.get_llm(PRIMARY)
    assert llm.get_llm(PRIMARY) is not llm.get_llm(BACKUP)
    assert llm.get_llm().generate_text(PROMPT) == " Plain answer. "
    [call] = provider_http.calls
    assert call.body == {
        "model": PRIMARY, "messages": [{"role": "user", "content": PROMPT}],
        "temperature": 0.1, "n": 1, "stop": None, "stream": False,
    }
    assert call.timeout == {"connect": None, "read": None, "write": None, "pool": None}


@pytest.mark.parametrize(("schema", "legacy_digest"), [
    (QA, "fa62cbe262925fd996a54e4c8ef773c5892901b5152c373ad2053d5dd802aa75"),
    (COMPARISON, "a77a99a49e8f3f309fc12c00d64bb9bb7aa96de2ffb131d3d6a801ed0137b0a2"),
    (RECOMMENDATION, "59cbe2e59f2de6457130ba42caf877741231dab965ca9d3ccafdbcfd9a6d10d5"),
])
def test_structured_request_matches_full_legacy_body(provider_http, schema, legacy_digest):
    result = llm.get_llm(PRIMARY).generate_structured(PROMPT, schema)
    assert result == schema.model_validate(RESPONSES[schema.__name__])
    [call] = provider_http.calls
    # Fingerprints cover the complete sorted JSON from the old 0.3.2 wrapper,
    # including tool and property descriptions, defaults, refs, and named choice.
    encoded = json.dumps(call.body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    assert sha256(encoded).hexdigest() == legacy_digest


@pytest.mark.parametrize("arguments", ["{bad", '{"quota": invalid}', ""])
def test_invalid_tool_json_is_a_parse_error(provider_http, arguments):
    provider_http.respond.side_effect = None
    provider_http.respond.return_value = tool_reply(tool_arguments(QA.__name__, arguments))
    with pytest.raises(llm.StructuredResponseError) as exc:
        llm.get_llm(PRIMARY).generate_structured(PROMPT, QA)
    assert not llm.is_quota_error(exc.value)
    assert attempts(provider_http) == [(PRIMARY, True, 0)]


def test_invalid_tool_can_precede_valid_tool(provider_http):
    provider_http.respond.side_effect = None
    provider_http.respond.return_value = tool_reply(
        tool_arguments(QA.__name__, "{bad"), tool_arguments(QA.__name__, json.dumps(RESPONSES[QA.__name__])),
    )
    assert llm.get_llm(PRIMARY).generate_structured(PROMPT, QA) == QA.model_validate(RESPONSES[QA.__name__])
    assert attempts(provider_http) == [(PRIMARY, True, 0)]


def test_tool_without_arguments_is_a_parse_error(provider_http):
    no_arguments = tool_arguments(QA.__name__, "")
    del no_arguments["function"]["arguments"]
    provider_http.respond.side_effect = None
    provider_http.respond.return_value = tool_reply(no_arguments)
    with pytest.raises(llm.StructuredResponseError):
        llm.get_llm(PRIMARY).generate_structured(PROMPT, QA)


@pytest.mark.parametrize("arguments", ["{}", '{"answer":[],"reasoning":[]}'])
def test_pydantic_validates_first_decodable_tool(provider_http, arguments):
    provider_http.respond.side_effect = None
    provider_http.respond.return_value = tool_reply(
        tool_arguments(QA.__name__, arguments), tool_arguments(QA.__name__, json.dumps(RESPONSES[QA.__name__])),
    )
    with pytest.raises(ValidationError):
        llm.get_llm(PRIMARY).generate_structured(PROMPT, QA)


def test_unexpected_tool_name_is_a_parse_error(provider_http):
    provider_http.respond.side_effect = None
    provider_http.respond.return_value = tool_reply(tool_arguments("unrequested_tool", "{}"))
    with pytest.raises(llm.StructuredResponseError, match="unexpected response tool"):
        llm.get_llm(PRIMARY).generate_structured(PROMPT, QA)


def test_no_tool_and_null_text_keep_their_response_contract(provider_http):
    provider_http.respond.side_effect = None
    provider_http.respond.return_value = text_reply(None)
    provider = llm.get_llm(PRIMARY)
    assert provider.generate_structured(PROMPT, QA) is None
    assert provider.generate_text(PROMPT) == ""


def test_empty_choices_still_raise(provider_http):
    provider_http.respond.side_effect = None
    provider_http.respond.return_value = completion(None, empty_choices=True)
    with pytest.raises(IndexError):
        llm.get_llm(PRIMARY).generate_text(PROMPT)


def provider_failure(request, failure):
    if failure == "timeout":
        raise httpx.ReadTimeout("Synthetic read timeout", request=request)
    if failure == "connection":
        raise httpx.ConnectError("Synthetic connection failure", request=request)
    return error(failure, message="Synthetic provider failure")


@pytest.mark.parametrize(("failure", "app_delay", "plain_fallback"), [
    (429, False, False), (503, True, False), ("timeout", True, False), ("connection", False, True),
])
def test_qa_retry_and_fallback_attempt_order(provider_http, failure, app_delay, plain_fallback):
    def reply(request, body):
        if body["model"] == PRIMARY:
            return provider_failure(request, failure)
        return success(request, body)

    provider_http.respond.side_effect = reply
    assert chat_service._invoke_with_fallback(PROMPT) == ("Answer.", "Evidence.", ["Next?"], BACKUP)
    expected = sdk_attempts(PRIMARY)
    if app_delay:
        expected += sdk_attempts(PRIMARY)
    if plain_fallback:
        expected += sdk_attempts(PRIMARY, structured=False)
    assert attempts(provider_http) == expected + [(BACKUP, True, 0)]
    assert len(provider_http.retry_delays) == 2 * (len(expected) // 3)
    assert provider_http.fallback_delays == ([1.5] if app_delay else [])


@pytest.mark.parametrize(("failure", "exception"), [
    (503, InternalServerError), ("timeout", APITimeoutError), ("connection", APIConnectionError),
])
def test_comparison_does_not_advance_on_transient_failure(provider_http, failure, exception):
    provider_http.respond.side_effect = lambda request, _body: provider_failure(request, failure)
    with pytest.raises(exception):
        comparison_service._llm_structured_call(PROMPT, COMPARISON)
    assert attempts(provider_http) == sdk_attempts(PRIMARY)
    assert len(provider_http.retry_delays) == 2


@pytest.mark.parametrize(("status", "details", "primary_count"), [
    (429, {"message": "rate limit"}, 3),
    (400, {"code": "resource_exhausted", "message": "capacity"}, 1),
])
def test_comparison_advances_on_provider_quota(provider_http, status, details, primary_count):
    def reply(request, body):
        if body["model"] == PRIMARY:
            return error(status, **details)
        return success(request, body)

    provider_http.respond.side_effect = reply
    assert comparison_service._llm_structured_call(PROMPT, COMPARISON).sources == ["alpha.pdf", "beta.pdf"]
    assert attempts(provider_http) == sdk_attempts(PRIMARY)[:primary_count] + [(BACKUP, True, 0)]


@pytest.mark.parametrize("response_kind", ["no tool", "invalid JSON with quota", "blank structured answer"])
def test_qa_tries_same_model_as_plain_text(provider_http, response_kind):
    def reply(request, body):
        if "tools" not in body:
            return success(request, body)
        if response_kind == "no tool":
            return text_reply("No tool")
        arguments = '{"quota": invalid}' if response_kind == "invalid JSON with quota" else '{"answer":" ","reasoning":""}'
        return tool_reply(tool_arguments(QA.__name__, arguments))

    provider_http.respond.side_effect = reply
    assert chat_service._invoke_with_fallback(PROMPT) == ("Plain answer.", "", [], PRIMARY)
    assert attempts(provider_http) == [(PRIMARY, True, 0), (PRIMARY, False, 0)]


def test_qa_moves_to_backup_after_blank_plain_response(provider_http):
    def reply(request, body):
        if body["model"] == PRIMARY:
            return text_reply("   ")
        return success(request, body)

    provider_http.respond.side_effect = reply
    assert chat_service._invoke_with_fallback(PROMPT)[-1] == BACKUP
    assert attempts(provider_http) == [(PRIMARY, True, 0), (PRIMARY, False, 0), (BACKUP, True, 0)]


def test_comparison_advances_after_missing_tool(provider_http):
    def reply(request, body):
        return text_reply(None) if body["model"] == PRIMARY else success(request, body)

    provider_http.respond.side_effect = reply
    assert comparison_service._llm_structured_call(PROMPT, COMPARISON).sources == ["alpha.pdf", "beta.pdf"]
    assert attempts(provider_http) == [(PRIMARY, True, 0), (BACKUP, True, 0)]


@pytest.mark.parametrize(("arguments", "exception"), [
    ('{"quota": invalid}', llm.StructuredResponseError), ("{}", ValidationError),
])
def test_comparison_parse_errors_never_masquerade_as_quota(provider_http, arguments, exception):
    provider_http.respond.side_effect = None
    provider_http.respond.return_value = tool_reply(tool_arguments(COMPARISON.__name__, arguments))
    with pytest.raises(exception):
        comparison_service._llm_structured_call(PROMPT, COMPARISON)
    assert attempts(provider_http) == [(PRIMARY, True, 0)]


@pytest.mark.parametrize(("question", "schema", "logged_model"), [
    ("What do the notes explain?", QA, BACKUP),
    ("Compare alpha.pdf and beta.pdf.", COMPARISON, PRIMARY),
    ("Which PDF should I read?", RECOMMENDATION, PRIMARY),
    ("What do the notes explain?", None, PRIMARY),
])
def test_chat_prompt_boundaries_and_model_attribution(provider_http, monkeypatch, question, schema, logged_model):
    user_id, collection_id, session_id = uuid4(), uuid4(), uuid4()
    filenames = ["alpha.pdf", "beta.pdf"]
    citations = [
        {"document_id": str(uuid4()), "document_name": name, "page_number": 1, "chunk_index": 0,
         "chunk_text": f"evidence-from-{name}", "score": 0.8}
        for name in filenames
    ] if schema else []
    monkeypatch.setattr(chat_service.collection_service, "get_owned_collection", Mock())
    monkeypatch.setattr(chat_service, "get_owned_session", Mock(return_value=SimpleNamespace(
        id=session_id, collection_id=collection_id, user_id=user_id,
    )))
    monkeypatch.setattr(chat_service.chat_repository, "list_recent_user_messages", Mock(return_value=["user-history-marker"]))
    transcript = Mock(return_value=[SimpleNamespace(role=chat_service.MessageRole.assistant, content="old-assistant-marker")])
    monkeypatch.setattr(chat_service.chat_repository, "list_messages_by_session", transcript)
    monkeypatch.setattr(chat_service.chat_repository, "create_message", Mock(return_value=SimpleNamespace(id=uuid4())))
    monkeypatch.setattr(chat_service.document_repository, "list_by_collection", Mock(return_value=[
        SimpleNamespace(filename=name, status=chat_service.DocumentStatus.ready) for name in filenames
    ]))
    monkeypatch.setattr(chat_service.retrieval_service, "retrieve", Mock(return_value=[]))
    monkeypatch.setattr(chat_service.retrieval_service, "build_citations", Mock(return_value=citations))
    query_log = Mock()
    monkeypatch.setattr(chat_service.query_log_repository, "create", query_log)

    def reply(request, body):
        return error(429, message="quota") if body["model"] == PRIMARY else success(request, body)

    provider_http.respond.side_effect = reply
    chat_service.send_message(object(), user_id, collection_id, question, session_id)

    assert query_log.call_args.kwargs["llm_model"] == logged_model
    transcript.assert_not_called()
    if not schema:
        assert not provider_http.calls
        return
    assert attempts(provider_http) == sdk_attempts(PRIMARY) + [(BACKUP, True, 0)]
    for call in provider_http.calls:
        assert call.body["tool_choice"]["function"]["name"] == schema.__name__
        [message] = call.body["messages"]
        assert message["role"] == "user"
        assert all(marker in message["content"] for marker in (
            question, "user-history-marker", "evidence-from-alpha.pdf", "evidence-from-beta.pdf",
        ))
        assert "old-assistant-marker" not in message["content"]
