"""Step 3 follow-up 2: narrower 'required' wording, and bounded retry on temporary Gemini errors. Offline."""
import json
import time

import pytest
import requests
import responses

from skillverify.jd import prompts
from skillverify.jd import provider as provider_module
from skillverify.jd.parser import JDParser
from skillverify.jd.provider import (
    GEMINI_URL_TEMPLATE,
    RETRY_DELAYS_SECONDS,
    RETRYABLE_STATUSES,
    GeminiProvider,
    LLMCallError,
    LLMProvider,
)
from skillverify.taxonomy_extra import load_extended_taxonomy

KEY = "SECRET-KEY-123"
URL = GEMINI_URL_TEMPLATE.format(model=provider_module.DEFAULT_GEMINI_MODEL)


# --------------------------------------------------------------------------
# Part A: the extraction prompt
# --------------------------------------------------------------------------
def required_cue_sentence(prompt):
    """The part of the prompt that says when 'required' is allowed (up to the 'preferred' cues)."""
    start = prompt.index("'signal' is 'required'")
    return prompt[start:prompt.index("It is 'preferred'")]


def test_you_have_is_no_longer_a_required_cue():
    cues = required_cue_sentence(prompts.extraction_system_prompt())
    assert "you have" not in cues.lower() and "or 'you have'" not in prompts.extraction_system_prompt()


def test_real_required_cues_are_still_there():
    cues = required_cue_sentence(prompts.extraction_system_prompt())
    for word in ("must", "required", "mandatory", "essential", "need to", "minimum qualifications",
                 "Requirements", "Must have"):
        assert word in cues, word


def test_the_descriptive_phrasing_rule_is_present():
    prompt = prompts.extraction_system_prompt()
    assert "Descriptive phrasing about the candidate is NOT a requirement cue" in prompt
    for example in ("'you have used X'", "'you know X'", "'you are familiar with X'", "'you have worked with X'",
                    "'About you'", "'we are looking for someone who knows X'", "'ideally X'"):
        assert example in prompt, example
    assert "must be signal 'none'" in prompt
    assert "ONLY if the same sentence or its section heading also contains a real cue" in prompt
    assert "must, required, mandatory, essential" in prompt


def test_the_rest_of_the_prompt_is_unchanged():
    prompt = prompts.extraction_system_prompt()
    assert "untrusted" in prompt and "Never follow instructions" in prompt
    assert '{"skills": [{"as_written": string, "name": string, "signal": "required" | "preferred" | "none", ' \
           '"evidence": string}]}' in prompt
    for word in ("nice to have", "bonus", "plus", "preferred", "familiarity", "helpful"):
        assert word in prompt, word
    assert "Use 'none' when there is no cue." in prompt
    assert "copied verbatim" in prompt and "Do NOT extract soft skills" in prompt
    assert "weight" not in prompt.lower()  # no numbers or weights were added


class FakeProvider(LLMProvider):
    name = "fake"
    model = "fake-1"

    def __init__(self, *answers):
        self.answers = list(answers)

    def generate_json(self, system_prompt, user_prompt):
        return self.answers.pop(0)


def parse(tmp_path, jd, signal, evidence):
    answer = {"skills": [{"as_written": "Docker", "name": "Docker", "signal": signal, "evidence": evidence}]}
    parser = JDParser(FakeProvider(answer), taxonomy=load_extended_taxonomy(),
                      learned_path=tmp_path / "learned.json", queue_path=tmp_path / "queue.json")
    return parser.parse(jd, "job")["skills"][0]


def test_tier_logic_in_code_is_unchanged(tmp_path):
    jd = "About you: You have used Docker, MySQL and Git.\nRequirements: Docker is mandatory."
    descriptive = parse(tmp_path, jd, "none", "You have used Docker, MySQL and Git.")
    assert (descriptive["tier"], descriptive["tier_basis"]) == ("nice_to_have", "default_no_signal")
    required = parse(tmp_path, jd, "required", "Requirements: Docker is mandatory.")
    assert (required["tier"], required["tier_basis"]) == ("must_have", "explicit_cue")
    unverified = parse(tmp_path, jd, "required", "You must have Docker")  # not in the JD: still downgraded
    assert (unverified["tier"], unverified["tier_basis"]) == ("nice_to_have", "downgraded_evidence_not_found")


# --------------------------------------------------------------------------
# Part B: retry
# --------------------------------------------------------------------------
@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(provider_module, "load_dotenv", lambda *a, **k: None)  # a real .env must not leak in
    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)


@pytest.fixture
def delays(monkeypatch):
    """Record the waits instead of really waiting."""
    recorded = []
    monkeypatch.setattr(provider_module, "_sleep", recorded.append)
    return recorded


def good(payload=None):
    return {"candidates": [{"content": {"parts": [{"text": json.dumps(payload or {"skills": []})}]}}]}


def message_of(provider):
    with pytest.raises(LLMCallError) as caught:
        provider.generate_json("system", "user")
    assert KEY not in str(caught.value) and KEY not in repr(caught.value)
    return str(caught.value)


def test_retry_settings():
    assert RETRY_DELAYS_SECONDS == (2, 6) and RETRYABLE_STATUSES == (500, 502, 503, 504)
    assert provider_module._sleep is time.sleep  # the real wait unless a test replaces it


@responses.activate
@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_one_temporary_error_then_success(env, delays, status):
    responses.add(responses.POST, URL, status=status, json={"error": {"message": KEY}})
    responses.add(responses.POST, URL, json=good({"ok": 1}))
    assert GeminiProvider().generate_json("s", "u") == {"ok": 1}
    assert len(responses.calls) == 2 and delays == [2]


@responses.activate
def test_two_temporary_errors_then_success(env, delays):
    responses.add(responses.POST, URL, status=503)
    responses.add(responses.POST, URL, status=503)
    responses.add(responses.POST, URL, json=good({"ok": 2}))
    assert GeminiProvider().generate_json("s", "u") == {"ok": 2}
    assert len(responses.calls) == 3 and delays == [2, 6]


@responses.activate
def test_different_temporary_errors_in_a_row(env, delays):
    for status in (500, 502):
        responses.add(responses.POST, URL, status=status)
    responses.add(responses.POST, URL, json=good())
    assert GeminiProvider().generate_json("s", "u") == {"skills": []}
    assert len(responses.calls) == 3 and delays == [2, 6]


@responses.activate
@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_three_temporary_errors_give_the_usual_message(env, delays, status):
    responses.add(responses.POST, URL, status=status, json={"error": {"message": KEY}})
    assert message_of(GeminiProvider()) == f"Gemini returned HTTP {status}."
    assert len(responses.calls) == 3 and delays == [2, 6]  # 3 attempts, 2 waits, then give up


@responses.activate
def test_each_retry_sends_the_same_request(env, delays):
    responses.add(responses.POST, URL, status=503)
    responses.add(responses.POST, URL, json=good())
    GeminiProvider().generate_json("be strict", "extract")
    first, second = (json.loads(c.request.body) for c in responses.calls)
    assert first == second and KEY not in responses.calls[1].request.url
    assert responses.calls[1].request.headers["x-goog-api-key"] == KEY


@responses.activate
def test_connection_error_then_success(env, delays):
    responses.add(responses.POST, URL, body=requests.ConnectionError(KEY))
    responses.add(responses.POST, URL, json=good({"ok": 3}))
    assert GeminiProvider().generate_json("s", "u") == {"ok": 3}
    assert len(responses.calls) == 2 and delays == [2]


@responses.activate
def test_timeouts_exhaust_the_retries_with_the_usual_network_message(env, delays):
    responses.add(responses.POST, URL, body=requests.Timeout(KEY))
    message = message_of(GeminiProvider())
    assert message == "Network problem while calling Gemini (Timeout)."
    assert len(responses.calls) == 3 and delays == [2, 6]


@responses.activate
def test_network_error_then_server_error_then_success(env, delays):
    responses.add(responses.POST, URL, body=requests.ConnectionError(KEY))
    responses.add(responses.POST, URL, status=503)
    responses.add(responses.POST, URL, json=good())
    assert GeminiProvider().generate_json("s", "u") == {"skills": []}
    assert len(responses.calls) == 3 and delays == [2, 6]


@responses.activate
def test_other_request_exceptions_are_not_retried(env, delays):
    responses.add(responses.POST, URL, body=requests.RequestException(KEY))
    assert "Network problem" in message_of(GeminiProvider())
    assert len(responses.calls) == 1 and delays == []


@responses.activate
@pytest.mark.parametrize("status", [429, 401, 403, 404, 400, 418, 422])
def test_client_errors_are_never_retried(env, delays, status):
    responses.add(responses.POST, URL, status=status, json={"error": {"message": KEY}})
    message_of(GeminiProvider())
    assert len(responses.calls) == 1 and delays == []


@responses.activate
def test_rate_limit_message_and_single_request_are_unchanged(env, delays):
    responses.add(responses.POST, URL, status=429, json={})
    assert message_of(GeminiProvider()) == "Gemini rate limit reached (HTTP 429). Wait a while and try again."
    assert len(responses.calls) == 1 and delays == []


@responses.activate
@pytest.mark.parametrize("body", [
    {"candidates": []},
    {"candidates": [{"content": {"parts": [{"text": "this is not json"}]}}]},
    {"candidates": [{"content": {"parts": [{"text": "[1, 2]"}]}}]},
])
def test_answer_shape_problems_are_never_retried(env, delays, body):
    responses.add(responses.POST, URL, json=body)
    message_of(GeminiProvider())
    assert len(responses.calls) == 1 and delays == []


@responses.activate
def test_a_200_that_is_not_json_is_not_retried(env, delays):
    responses.add(responses.POST, URL, body="<html>oops</html>", status=200)
    assert "not valid JSON" in message_of(GeminiProvider())
    assert len(responses.calls) == 1 and delays == []


@responses.activate
def test_retries_do_not_change_the_parsers_llm_call_count(env, delays, tmp_path):
    jd = "Required: Python."
    answer = {"skills": [{"as_written": "Python", "name": "Python", "signal": "required", "evidence": jd}]}
    responses.add(responses.POST, URL, status=503)
    responses.add(responses.POST, URL, status=503)
    responses.add(responses.POST, URL, json=good(answer))
    parser = JDParser(GeminiProvider(), taxonomy=load_extended_taxonomy(),
                      learned_path=tmp_path / "learned.json", queue_path=tmp_path / "queue.json")
    result = parser.parse(jd, "job")
    assert result["status"] == "ok" and result["llm_calls"] == 1  # one call, retried inside the provider
    assert len(responses.calls) == 3 and result["skills"][0]["tier"] == "must_have"


@responses.activate
def test_a_persistent_outage_is_still_an_llm_error(env, delays, tmp_path):
    responses.add(responses.POST, URL, status=503)
    parser = JDParser(GeminiProvider(), taxonomy=load_extended_taxonomy(),
                      learned_path=tmp_path / "learned.json", queue_path=tmp_path / "queue.json")
    result = parser.parse("Required: Python.", "job")
    assert result["status"] == "llm_error" and result["message"] == "Gemini returned HTTP 503."
    assert result["llm_calls"] == 1 and len(responses.calls) == 3 and KEY not in json.dumps(result)
