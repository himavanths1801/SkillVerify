"""Fix 1: a model's answer is read tolerantly, and every unusable answer has a safe, closed cause.
Fakes only: fake providers, ``responses`` for HTTP, no real call, no real key."""
import json
import logging

import pytest
import requests
import responses
from test_questions_support import PY_ORDERS, FakeWriter

from skillverify.jd import provider as jd_provider
from skillverify.jd.provider import GEMINI_URL_TEMPLATE, LLMCallError
from skillverify_questions import answer, config, groq_provider, select, service, writer
from skillverify_questions.gemini_questions import QuestionGemini
from skillverify_questions.groq_provider import GROQ_URL, GroqProvider

OBJECT = {"kind": "write_code", "question": "q", "nested": {"a": [1, 2, {"b": "}"}]}, "text": "a } and { inside"}
RAW = json.dumps(OBJECT)
CANDIDATE = select.FileCandidate("orders-analysis", "analysis.py", "python", PY_ORDERS, 50, 3, None)
QUESTION = ("Look at clean_orders in analysis.py. A reviewer says rows with a missing price slip through. "
            "Change the function so those rows are dropped, and explain how you would test it.")
GOOD = {"kind": "write_code", "question": QUESTION, "starter_code": "", "file": "analysis.py",
        "symbols": ["clean_orders"], "reference_notes": "Drop rows with a missing price."}


# --------------------------------------------------------------------------
# the tolerant parser
# --------------------------------------------------------------------------
@pytest.mark.parametrize("text", [
    RAW,
    f"```json\n{RAW}\n```",
    f"```\n{RAW}\n```",
    f"  ```JSON {RAW}```  ",
    f"Here is the question you asked for:\n{RAW}\nHope that helps! {{not json}}",
    f"Sure!\n```json\n{RAW}\n```\nLet me know.",
    f"<think>{{reasoning}}</think>\n{RAW}".replace("{reasoning}", "reasoning here"),
])
def test_a_fenced_or_surrounded_object_is_accepted(text):
    assert answer.parse_answer(text, "Groq") == OBJECT


def test_braces_inside_strings_do_not_confuse_the_balance_scan():
    text = 'noise {"a": "closing } brace and \\" quote {", "b": {"c": 1}} trailing'
    assert answer.parse_answer(text, "Groq") == {"a": 'closing } brace and " quote {', "b": {"c": 1}}


@pytest.mark.parametrize("text", ["[1, 2]", '[{"kind": "write_code"}]', "42", '"just a string"', "null", "true"])
def test_a_top_level_array_or_scalar_is_wrong_shape(text):
    with pytest.raises(answer.AnswerProblem) as raised:
        answer.parse_answer(text, "Groq")
    assert raised.value.cause == "wrong_shape" and "not an object" in str(raised.value)


def test_each_cause_is_produced_by_its_own_case():
    def cause(text, truncated=False):
        with pytest.raises(answer.AnswerProblem) as raised:
            answer.parse_answer(text, "Groq", truncated)
        return raised.value.cause

    assert cause("") == "empty_response" and cause("   \n") == "empty_response" and cause(None) == "empty_response"
    assert cause('{"question": "cut here and the braces never') == "cut_off"                 # unbalanced braces
    assert cause('```json\n{"question": "an unclosed fence') == "cut_off"
    assert cause("", truncated=True) == "cut_off"                                            # the provider flag: nothing left
    assert cause("I cannot do that.", truncated=True) == "cut_off"                           # the provider flag
    assert cause("I cannot do that.") == "not_json"                                          # text without an object
    assert cause("{not: json}") == "not_json"                                                # balanced, but not JSON
    assert cause("[1, 2]", truncated=True) == "wrong_shape"                                  # a complete wrong shape stays one
    assert set(config.ANSWER_CAUSES) == {"empty_response", "cut_off", "not_json", "wrong_shape"}


def test_a_complete_object_is_used_even_when_the_provider_says_the_limit_was_reached():
    assert answer.parse_answer(RAW, "Groq", truncated=True) == OBJECT


def test_messages_keep_the_words_the_writer_recognises_and_never_contain_the_answer():
    for cause in config.ANSWER_CAUSES:
        text = answer.message_for("Groq", cause)
        assert writer._ANSWER_SHAPE.search(text) and "SECRET" not in text
    secret = '{"question": "SECRET-ANSWER-TEXT'
    with pytest.raises(answer.AnswerProblem) as raised:
        answer.parse_answer(secret, "Groq")
    assert "SECRET" not in str(raised.value) and "SECRET" not in repr(raised.value)


def test_cause_of_reads_the_cause_or_the_usual_wording():
    assert answer.cause_of(answer.AnswerProblem("x", "cut_off")) == "cut_off"
    assert answer.cause_of(LLMCallError("Gemini returned no usable answer.")) == "empty_response"
    assert answer.cause_of(LLMCallError("Gemini's answer was JSON but not an object.")) == "wrong_shape"
    assert answer.cause_of(LLMCallError("Groq's answer was not valid JSON.")) == "not_json"
    assert answer.cause_of(LLMCallError("anything else")) == "not_json"


# --------------------------------------------------------------------------
# the Groq provider: request body, finish_reason, tolerant reading
# --------------------------------------------------------------------------
@pytest.fixture
def groq_env(monkeypatch):
    monkeypatch.setattr(groq_provider, "load_dotenv", lambda: None)  # the real .env is never read
    monkeypatch.setenv("GROQ_API_KEY", "gsk_FAKE-KEY-FOR-TESTS-0123456789")


def groq_reply(content, finish="stop", **extra):
    message = {"role": "assistant"}
    if content is not None:
        message["content"] = content
    responses.add(responses.POST, GROQ_URL, status=200,
                  json={"choices": [{"message": message, "finish_reason": finish}], **extra})


def groq_call(**kwargs):
    return GroqProvider(**kwargs).generate_json("system words", "user words")


@responses.activate
def test_no_output_cap_is_sent_unless_one_is_set(groq_env):
    groq_reply(RAW)
    groq_call()
    body = json.loads(responses.calls[0].request.body)
    assert "max_completion_tokens" not in body and "max_tokens" not in body and "reasoning_effort" not in body
    assert body["response_format"] == {"type": "json_object"}  # JSON mode: documented for the chosen model


@responses.activate
def test_the_configured_output_limit_is_in_the_request_body(groq_env):
    groq_reply(RAW)
    groq_reply(RAW)
    assert groq_call(max_output_tokens=3000) == OBJECT
    assert json.loads(responses.calls[0].request.body)["max_completion_tokens"] == 3000
    provider = GroqProvider()
    provider.set_max_output_tokens(1234)
    provider.generate_json("s", "u")
    assert json.loads(responses.calls[1].request.body)["max_completion_tokens"] == 1234
    provider.set_max_output_tokens(None)
    assert provider.max_output_tokens is None


@responses.activate
@pytest.mark.parametrize("content,finish,cause", [
    (None, "stop", "empty_response"), ("", "stop", "empty_response"), ("   ", "stop", "empty_response"),
    ("", "length", "cut_off"),                                  # the thinking used the whole limit: nothing is left
    ('{"question": "cut here', "length", "cut_off"),            # reported by the provider
    ('{"question": "cut here', "stop", "cut_off"),              # the braces never balance
    ("no object here", "stop", "not_json"), ("[1]", "stop", "wrong_shape"),
])
def test_every_unusable_groq_answer_has_its_cause(groq_env, content, finish, cause):
    groq_reply(content, finish)
    with pytest.raises(answer.AnswerProblem) as raised:
        groq_call()
    assert raised.value.cause == cause and writer._ANSWER_SHAPE.search(str(raised.value))


@responses.activate
def test_groq_answers_with_fences_or_text_around_the_object_are_accepted(groq_env):
    groq_reply(f"```json\n{RAW}\n```")
    groq_reply(f"Here you go: {RAW} Bye.", finish="length")
    assert groq_call() == OBJECT and groq_call() == OBJECT


@responses.activate
@pytest.mark.parametrize("payload", [{"no": "choices"}, {"choices": []}, {"choices": [None]}, {"choices": ["x"]}])
def test_a_groq_reply_without_a_usable_choice_is_an_empty_response(groq_env, payload):
    responses.add(responses.POST, GROQ_URL, status=200, json=payload)
    with pytest.raises(answer.AnswerProblem) as raised:
        groq_call()
    assert raised.value.cause == "empty_response"


@responses.activate
def test_a_groq_reply_that_is_not_json_at_all_is_not_json(groq_env):
    responses.add(responses.POST, GROQ_URL, status=200, body="<html>")
    with pytest.raises(answer.AnswerProblem) as raised:
        groq_call()
    assert raised.value.cause == "not_json" and "not valid JSON" in str(raised.value)


# --------------------------------------------------------------------------
# the Gemini provider the question writer uses
# --------------------------------------------------------------------------
@pytest.fixture
def gemini_env(monkeypatch):
    monkeypatch.setattr(jd_provider, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(jd_provider, "_sleep", lambda seconds: None)
    monkeypatch.setenv("GEMINI_API_KEY", "SECRET-KEY-123")


def gemini_reply(text=None, finish=None, **payload):
    candidate = {}
    if text is not None:
        candidate["content"] = {"parts": [{"text": text}]}
    if finish is not None:
        candidate["finishReason"] = finish
    responses.add(responses.POST, GEMINI_URL_TEMPLATE.format(model="gem-q"), status=200,
                  json=payload or {"candidates": [candidate]})


def gemini_call():
    return QuestionGemini(model="gem-q").generate_json("system", "user")


@responses.activate
def test_gemini_for_questions_reads_fences_and_surrounding_text(gemini_env):
    gemini_reply(f"```json\n{RAW}\n```")
    gemini_reply(f"Question: {RAW}")
    gemini_reply(RAW)
    assert gemini_call() == OBJECT and gemini_call() == OBJECT and gemini_call() == OBJECT
    body = json.loads(responses.calls[0].request.body)
    assert body["generationConfig"] == {"temperature": 0, "responseMimeType": "application/json"}  # request unchanged


@responses.activate
@pytest.mark.parametrize("text,finish,payload,cause", [
    (None, None, None, "empty_response"),
    (None, "MAX_TOKENS", None, "cut_off"),
    ('{"question": "cut', "MAX_TOKENS", None, "cut_off"),
    ('{"question": "cut', "STOP", None, "cut_off"),
    ("words only", "STOP", None, "not_json"),
    ('["x"]', "STOP", None, "wrong_shape"),
    (None, None, {"promptFeedback": {"blockReason": "SAFETY"}}, "empty_response"),
    (None, None, {"unexpected": True}, "empty_response"),
])
def test_every_unusable_gemini_answer_has_its_cause(gemini_env, text, finish, payload, cause):
    gemini_reply(text, finish, **(payload or {}))
    with pytest.raises(answer.AnswerProblem) as raised:
        gemini_call()
    assert raised.value.cause == cause and "SECRET-KEY-123" not in str(raised.value)
    assert writer._ANSWER_SHAPE.search(str(raised.value))


@responses.activate
def test_gemini_keeps_the_blocked_reason_in_its_message_and_a_non_json_reply_is_not_json(gemini_env):
    gemini_reply(promptFeedback={"blockReason": "SAFETY"})
    with pytest.raises(answer.AnswerProblem) as raised:
        gemini_call()
    assert "blocked: SAFETY" in str(raised.value)
    responses.add(responses.POST, GEMINI_URL_TEMPLATE.format(model="gem-q"), status=200, body="<html>")
    with pytest.raises(answer.AnswerProblem) as raised:
        gemini_call()
    assert raised.value.cause == "not_json"


# --------------------------------------------------------------------------
# the writer: causes of the final and the first attempt, no duplicates, nothing but codes in the data and the logs
# --------------------------------------------------------------------------
def problem(cause):
    return answer.AnswerProblem(answer.message_for("Groq", cause), cause)


def write(*steps):
    provider = FakeWriter({"analysis.py": list(steps) if len(steps) > 1 else steps[0]})
    return writer.write_question(provider, CANDIDATE, "Python", {"analysis.py"}, set()), provider


def test_causes_list_the_final_attempt_then_the_first_without_duplicates():
    written, _ = write(problem("cut_off"), problem("not_json"))
    assert written.question is None and written.reasons == ["bad_json"]
    assert written.causes == ["not_json", "cut_off"]
    same, _ = write(problem("cut_off"), problem("cut_off"))
    assert same.causes == ["cut_off"]
    assert same.final_causes == ["cut_off"] and same.first_causes == ["cut_off"]


def test_a_wrong_shaped_answer_object_is_wrong_shape():
    written, _ = write({"kind": "nonsense", **{k: v for k, v in GOOD.items() if k != "kind"}}, ["not", "a", "dict"])
    assert written.reasons == ["bad_json"] and written.causes == ["wrong_shape"]
    missing, _ = write({k: v for k, v in GOOD.items() if k != "question"})
    assert missing.causes == ["wrong_shape"]


def test_a_drop_for_another_reason_has_no_causes_and_a_repaired_answer_has_none_either():
    other, _ = write({**GOOD, "file": "elsewhere.py"})
    assert other.reasons == ["wrong_file"] and other.causes == []
    repaired, provider = write(problem("empty_response"), GOOD)
    assert repaired.question is not None and repaired.causes == [] and repaired.calls == 2


def test_a_first_bad_json_then_a_different_failure_keeps_the_first_cause():
    written, _ = write(problem("cut_off"), {**GOOD, "file": "elsewhere.py"})
    assert written.reasons == ["wrong_file", "bad_json"] and written.causes == ["cut_off"]


def test_a_plain_error_with_the_usual_wording_gets_the_cause_its_words_mean():
    written, _ = write(LLMCallError("Gemini returned no usable answer."), LLMCallError("Gemini's answer was not valid JSON."))
    assert written.causes == ["not_json", "empty_response"]


def run_with(answers, caplog):
    caplog.set_level(logging.DEBUG)
    selection = select.Selection(picks=[select.Pick(select.Need("Python", "must_have", True), CANDIDATE, True)])
    prepared = service.Prepared(selection, {"orders-analysis": {"analysis.py"}}, {"files_ranked": 1})
    return service.write_all(prepared, FakeWriter({"analysis.py": answers}))[0]


def test_dropped_entries_carry_the_causes_and_the_log_line_only_codes(caplog):
    result = run_with([problem("cut_off"), problem("empty_response")], caplog)
    assert result["dropped"] == [{"skill": "Python", "tier": "must_have", "repo": "orders-analysis", "path": "analysis.py",
                                  "attempts": 2, "reasons": ["bad_json"], "causes": ["empty_response", "cut_off"],
                                  "shape_fields": []}]
    assert set(result["dropped"][0]["causes"]) <= set(config.ANSWER_CAUSES)
    assert result["schema_version"] == 1
    lines = [r.getMessage() for r in caplog.records if r.name == "skillverify.questions"]
    assert lines == ["question dropped skill=Python reasons=bad_json causes=empty_response,cut_off shape_fields=none"]


def test_no_answer_text_reaches_the_result_or_the_logs(caplog):
    leaky = LLMCallError("Groq's answer was not valid JSON. {'question': 'SECRET-ANSWER-TEXT'}")
    result = run_with([leaky, leaky], caplog)
    assert result["dropped"][0]["causes"] == ["not_json"]
    assert "SECRET" in str(leaky)  # the fake error is deliberately careless; the writer must still never copy it
    assert "SECRET-ANSWER-TEXT" not in json.dumps(result) and "SECRET-ANSWER-TEXT" not in caplog.text


def test_a_provider_failure_that_is_not_an_answer_problem_is_still_raised():
    with pytest.raises(LLMCallError):
        write(LLMCallError("Groq rate limit reached (HTTP 429)."))


def test_requests_exceptions_are_not_confused_with_answers(groq_env):
    with responses.RequestsMock() as mock:
        mock.add(responses.POST, GROQ_URL, body=requests.ConnectionError("x"))
        with pytest.raises(LLMCallError) as raised:
            groq_call()
    assert not isinstance(raised.value, answer.AnswerProblem)
