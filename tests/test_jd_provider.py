"""Tests for the LLM provider interface and the Gemini connector. All HTTP is mocked."""
import json

import pytest
import requests
import responses

from skillverify.jd import provider as provider_module
from skillverify.jd.provider import (
    DEFAULT_GEMINI_MODEL,
    GEMINI_URL_TEMPLATE,
    GeminiProvider,
    LLMCallError,
    LLMConfigError,
    LLMProvider,
)

KEY = "SECRET-KEY-123"


@pytest.fixture(autouse=True)
def no_real_waiting(monkeypatch):
    """500/502/503/504 and network errors are now retried with short waits: never really wait in tests."""
    monkeypatch.setattr(provider_module, "_sleep", lambda seconds: None)


@pytest.fixture
def env(monkeypatch):
    """A key in the environment, and no way for a real .env file to leak in."""
    monkeypatch.setattr(provider_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)


def url_for(model=DEFAULT_GEMINI_MODEL):
    return GEMINI_URL_TEMPLATE.format(model=model)


def answer(payload):
    return {"candidates": [{"content": {"parts": [{"text": json.dumps(payload)}]}}]}


def call_and_catch(provider):
    with pytest.raises(LLMCallError) as caught:
        provider.generate_json("system", "user")
    assert KEY not in str(caught.value) and KEY not in repr(caught.value)
    return str(caught.value)


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------
@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_or_blank_key_is_a_friendly_config_error(monkeypatch, value):
    monkeypatch.setattr(provider_module, "load_dotenv", lambda *a, **k: None)  # a real .env must not leak in
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    if value is not None:
        monkeypatch.setenv("GEMINI_API_KEY", value)
    with pytest.raises(LLMConfigError, match="GEMINI_API_KEY is not set"):
        GeminiProvider()


def test_dotenv_is_loaded_before_the_key_is_read(monkeypatch):
    calls = []
    monkeypatch.setattr(provider_module, "load_dotenv", lambda *a, **k: calls.append(1))
    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    GeminiProvider()
    assert calls == [1]


def test_model_default_env_and_argument(env, monkeypatch):
    assert GeminiProvider().model == DEFAULT_GEMINI_MODEL == "gemini-3.6-flash"
    monkeypatch.setenv("GEMINI_MODEL", "gemini-custom")
    assert GeminiProvider().model == "gemini-custom"
    assert GeminiProvider(model="from-argument").model == "from-argument"
    assert GeminiProvider().name == "gemini"


@pytest.mark.parametrize("bad", ["a/b", "a b", "../x", "x?y=1", "a" * 101])
def test_invalid_model_names_are_refused(env, bad):
    with pytest.raises(LLMConfigError, match="Invalid Gemini model name"):
        GeminiProvider(model=bad)


def test_repr_never_contains_the_key(env):
    assert KEY not in repr(GeminiProvider()) and "gemini-3.6-flash" in repr(GeminiProvider())


def test_the_interface_is_abstract():
    with pytest.raises(TypeError):
        LLMProvider()

    class Tiny(LLMProvider):
        name, model = "tiny", "t-1"

        def generate_json(self, system_prompt, user_prompt):
            return {"echo": user_prompt}

    assert Tiny().generate_json("s", "u") == {"echo": "u"}


# --------------------------------------------------------------------------
# the request
# --------------------------------------------------------------------------
@responses.activate
def test_success_and_request_shape(env):
    responses.add(responses.POST, url_for(), json=answer({"skills": []}))
    result = GeminiProvider().generate_json("be strict", "extract these")
    assert result == {"skills": []}
    request = responses.calls[0].request
    assert request.url == url_for() and KEY not in request.url      # the key is not in the URL
    assert request.headers["x-goog-api-key"] == KEY
    body = json.loads(request.body)
    assert body["systemInstruction"]["parts"][0]["text"] == "be strict"
    assert body["contents"][0]["parts"][0]["text"] == "extract these"
    assert body["generationConfig"] == {"temperature": 0, "responseMimeType": "application/json"}


@responses.activate
def test_model_name_is_in_the_url(env):
    responses.add(responses.POST, url_for("my-model"), json=answer({"ok": True}))
    assert GeminiProvider(model="my-model").generate_json("s", "u") == {"ok": True}
    assert "models/my-model:generateContent" in responses.calls[0].request.url


# --------------------------------------------------------------------------
# errors (and the key never leaks)
# --------------------------------------------------------------------------
@responses.activate
@pytest.mark.parametrize("status, expected", [
    (401, "rejected the API key"), (403, "rejected the API key"), (404, "model names change"),
    (429, "rate limit"), (500, "HTTP 500"), (503, "HTTP 503"),
])
def test_http_errors(env, status, expected):
    responses.add(responses.POST, url_for(), status=status, json={"error": {"message": KEY}})
    message = call_and_catch(GeminiProvider())
    assert expected in message


@responses.activate
def test_rate_limit_is_not_retried(env):
    responses.add(responses.POST, url_for(), status=429, json={})
    call_and_catch(GeminiProvider())
    assert len(responses.calls) == 1


@responses.activate
def test_404_names_the_model(env):
    responses.add(responses.POST, url_for("old-model"), status=404, json={})
    assert "old-model" in call_and_catch(GeminiProvider(model="old-model"))


@responses.activate
@pytest.mark.parametrize("exc", [requests.ConnectionError(KEY), requests.Timeout(KEY), requests.RequestException(KEY)])
def test_network_problems_never_leak_details(env, exc):
    responses.add(responses.POST, url_for(), body=exc)
    message = call_and_catch(GeminiProvider())
    assert "Network problem" in message and type(exc).__name__ in message


@responses.activate
@pytest.mark.parametrize("body", [
    {"candidates": []}, {}, {"candidates": [{}]}, {"candidates": [{"content": {"parts": []}}]},
    {"candidates": [{"content": {"parts": [{"text": 5}]}}]}, [1, 2], "text", {"candidates": "x"},
])
def test_no_usable_candidates(env, body):
    responses.add(responses.POST, url_for(), json=body)
    assert "no usable answer" in call_and_catch(GeminiProvider())


@responses.activate
def test_blocked_prompt_reports_the_reason(env):
    responses.add(responses.POST, url_for(), json={"promptFeedback": {"blockReason": "SAFETY"}})
    assert "blocked: SAFETY" in call_and_catch(GeminiProvider())


@responses.activate
def test_response_that_is_not_json(env):
    responses.add(responses.POST, url_for(), body="<html>oops</html>", status=200)
    assert "not valid JSON" in call_and_catch(GeminiProvider())


@responses.activate
def test_answer_text_that_is_not_json(env):
    responses.add(responses.POST, url_for(),
                  json={"candidates": [{"content": {"parts": [{"text": "this is prose, not JSON"}]}}]})
    assert "answer was not valid JSON" in call_and_catch(GeminiProvider())


@responses.activate
@pytest.mark.parametrize("payload", [[1, 2, 3], "text", 5, None])
def test_answer_json_that_is_not_an_object(env, payload):
    responses.add(responses.POST, url_for(), json=answer(payload))
    assert "not an object" in call_and_catch(GeminiProvider())
