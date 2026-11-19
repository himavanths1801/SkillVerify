"""The Groq provider: request shape, error mapping, answer parsing. HTTP is faked with ``responses``; nothing real."""
import json
import logging

import pytest
import requests
import responses

from skillverify.jd.provider import REQUEST_TIMEOUT_SECONDS, LLMCallError, LLMConfigError
from skillverify_questions import groq_provider
from skillverify_questions.groq_provider import (DEFAULT_GROQ_MODEL, GROQ_URL, GroqKeyRejected, GroqProvider,
                                                 GroqRateLimited, GroqTooLarge, parse_retry_after)
from skillverify_questions.writer import _ANSWER_SHAPE

KEY = "gsk_FAKE-KEY-FOR-TESTS-0123456789"
ANSWER = {"kind": "write_code", "question": "q"}


@pytest.fixture(autouse=True)
def groq_env(monkeypatch):
    monkeypatch.setattr(groq_provider, "load_dotenv", lambda: None)  # the real .env is never read
    monkeypatch.setenv("GROQ_API_KEY", KEY)


def reply(content=None, **kwargs):
    kwargs.setdefault("status", 200)
    if content is not None:
        kwargs["json"] = {"choices": [{"message": {"role": "assistant", "content": content}}]}
    responses.add(responses.POST, GROQ_URL, **kwargs)


def call(model=None):
    return GroqProvider(model).generate_json("system words about JSON", "user words")


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------
def test_the_default_model_is_the_documented_one():
    assert DEFAULT_GROQ_MODEL == "openai/gpt-oss-120b"
    assert GroqProvider().model == "openai/gpt-oss-120b"


def test_no_key_is_a_config_error_and_a_bad_model_name_too(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "   ")
    with pytest.raises(LLMConfigError) as raised:
        GroqProvider()
    assert "GROQ_API_KEY" in str(raised.value)
    monkeypatch.setenv("GROQ_API_KEY", KEY)
    with pytest.raises(LLMConfigError):
        GroqProvider("not a model!")
    assert GroqProvider("meta-llama/llama-3.3-70b-versatile").model == "meta-llama/llama-3.3-70b-versatile"


def test_repr_never_shows_the_key():
    assert KEY not in repr(GroqProvider()) and "gpt-oss-120b" in repr(GroqProvider())


# --------------------------------------------------------------------------
# a good call
# --------------------------------------------------------------------------
@responses.activate
def test_the_request_is_openai_compatible_json_mode_with_a_bearer_key():
    reply(json.dumps(ANSWER))
    assert call() == ANSWER
    sent = responses.calls[0].request
    body = json.loads(sent.body)
    assert sent.url == "https://api.groq.com/openai/v1/chat/completions"
    assert sent.headers["Authorization"] == f"Bearer {KEY}"
    assert body["model"] == "openai/gpt-oss-120b" and body["temperature"] == 0
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"] == [{"role": "system", "content": "system words about JSON"},
                                {"role": "user", "content": "user words"}]


@responses.activate
def test_the_timeout_is_the_same_as_geminis(monkeypatch):
    seen = {}
    real = requests.post

    def spy(url, **kwargs):
        seen.update(kwargs)
        return real(url, **kwargs)

    monkeypatch.setattr(requests, "post", spy)
    reply(json.dumps(ANSWER))
    call()
    assert seen["timeout"] == REQUEST_TIMEOUT_SECONDS == 60


# --------------------------------------------------------------------------
# rate limits, size, keys, outages
# --------------------------------------------------------------------------
@responses.activate
def test_429_is_a_rate_limit_with_the_retry_after_seconds():
    reply(status=429, json={"error": {"message": "slow down"}}, headers={"Retry-After": "7"})
    with pytest.raises(GroqRateLimited) as raised:
        call()
    assert raised.value.retry_after == 7.0 and "HTTP 429" in str(raised.value)


@responses.activate
@pytest.mark.parametrize("header", [None, "soon", "0", "-3", ""])
def test_a_missing_or_useless_retry_after_is_none(header):
    reply(status=429, json={"error": {"message": "x"}}, headers={"Retry-After": header} if header is not None else {})
    with pytest.raises(GroqRateLimited) as raised:
        call()
    assert raised.value.retry_after is None


def test_parse_retry_after():
    assert parse_retry_after("2.5") == 2.5 and parse_retry_after(" 30 ") == 30.0
    assert parse_retry_after(None) is None and parse_retry_after("x") is None and parse_retry_after("0") is None


@responses.activate
@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_key_is_its_own_error(status):
    reply(status=status, json={"error": {"message": "bad key"}})
    with pytest.raises(GroqKeyRejected) as raised:
        call()
    assert f"HTTP {status}" in str(raised.value) and KEY not in str(raised.value)


@responses.activate
@pytest.mark.parametrize("status,text", [
    (413, "anything"), (429, "Request too large for model `openai/gpt-oss-120b` ... tokens per minute"),
    (400, "Please reduce your message size and try again."),
])
def test_a_request_that_is_too_large_is_not_a_rate_limit(status, text):
    reply(status=status, json={"error": {"message": text}})
    with pytest.raises(GroqTooLarge):
        call()


@responses.activate
def test_a_413_without_a_readable_body_is_still_too_large():
    reply(status=413, body="<html>nope</html>")
    with pytest.raises(GroqTooLarge):
        call()


@responses.activate
@pytest.mark.parametrize("body", ["not json at all", json.dumps({"error": "plain string"}), json.dumps({"error": {"message": 5}})])
def test_a_plain_429_with_an_odd_body_is_a_normal_rate_limit(body):
    reply(status=429, body=body)
    with pytest.raises(GroqRateLimited):
        call()


@responses.activate
@pytest.mark.parametrize("status", [500, 502, 503, 504, 404, 422])
def test_other_statuses_are_a_failed_call_with_the_status_in_the_message(status):
    reply(status=status, json={"error": {"message": "SECRET-BODY"}})
    with pytest.raises(LLMCallError) as raised:
        call()
    assert str(raised.value) == f"Groq returned HTTP {status}." and "SECRET-BODY" not in str(raised.value)
    assert not isinstance(raised.value, (GroqRateLimited, GroqKeyRejected, GroqTooLarge))


@responses.activate
@pytest.mark.parametrize("error", [requests.ConnectionError("https://api.groq.com/?key=SECRET"), requests.Timeout("slow")])
def test_network_problems_say_only_the_exception_type(error):
    responses.add(responses.POST, GROQ_URL, body=error)
    with pytest.raises(LLMCallError) as raised:
        call()
    assert str(raised.value).startswith("Network problem while calling Groq (") and "SECRET" not in str(raised.value)


# --------------------------------------------------------------------------
# answers that are not usable: the writer's repair rule applies, as for Gemini
# --------------------------------------------------------------------------
@responses.activate
@pytest.mark.parametrize("make", [
    lambda: reply("   "),                                   # empty text
    lambda: reply("this is not json"),                      # not JSON
    lambda: reply("[1, 2]"),                                # JSON, not an object
    lambda: reply(status=200, json={"choices": []}),        # no choice
    lambda: reply(status=200, json={"nothing": True}),      # no choices key
    lambda: reply(status=200, json={"choices": [{"message": {"content": None}}]}),
    lambda: reply(status=200, body="<html>"),               # the response itself is not JSON
])
def test_an_unusable_answer_matches_the_writers_bad_json_pattern(make):
    make()
    with pytest.raises(LLMCallError) as raised:
        call()
    assert _ANSWER_SHAPE.search(str(raised.value))
    assert not isinstance(raised.value, (GroqRateLimited, GroqKeyRejected, GroqTooLarge))


# --------------------------------------------------------------------------
# nothing secret leaks
# --------------------------------------------------------------------------
@responses.activate
def test_the_key_never_appears_in_errors_or_logs(caplog):
    caplog.set_level(logging.DEBUG)
    texts = []
    for status in (401, 429, 413, 500):
        responses.replace(responses.POST, GROQ_URL, status=status, json={"error": {"message": KEY}}) if responses.calls else \
            responses.add(responses.POST, GROQ_URL, status=status, json={"error": {"message": KEY}})
        with pytest.raises(LLMCallError) as raised:
            call()
        texts += [str(raised.value), repr(raised.value)]
    responses.replace(responses.POST, GROQ_URL, body=requests.ConnectionError(KEY))
    with pytest.raises(LLMCallError) as raised:
        call()
    texts.append(str(raised.value))
    assert all(KEY not in text for text in texts) and KEY not in caplog.text
