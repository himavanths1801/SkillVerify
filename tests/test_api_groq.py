"""Groq first, Gemini behind it: the question chain, the run fallbacks, health and the replacement route. Fakes only."""
import json
import logging

import pytest
from test_api_questions import Fleet, QuestionProvider, World, clock, shop_files, scan, validation  # noqa: F401 (clock is a fixture)
from test_api_support import CHAIN, FakeScanner, FakeValidator, make_env  # noqa: F401 (make_env is a fixture)

from api import errors, pipeline, settings
from api import models as api_models
from api.models import ModelChain
from skillverify.jd.provider import LLMCallError, LLMConfigError
from skillverify_questions.groq_provider import GroqKeyRejected, GroqRateLimited, GroqTooLarge

GROQ = "groq:openai/gpt-oss-120b"
KEY = "gsk_FAKE-GROQ-KEY-987654321"


# --------------------------------------------------------------------------
# the chain setting
# --------------------------------------------------------------------------
def test_default_chain_is_groq_then_the_current_gemini_chain_when_there_is_a_key():
    assert settings.load_questions_chain({"GROQ_API_KEY": KEY}) == (GROQ, *settings.DEFAULT_MODEL_CHAIN)
    assert settings.DEFAULT_GROQ_ENTRY == GROQ


def test_without_a_key_groq_is_skipped_silently_and_the_chain_is_exactly_todays(caplog):
    caplog.set_level(logging.DEBUG)
    for env in ({}, {"GROQ_API_KEY": ""}, {"GROQ_API_KEY": "   "}):
        assert settings.load_questions_chain(env) == settings.load_model_chain(env) == settings.DEFAULT_MODEL_CHAIN
    assert [r for r in caplog.records if r.name == "skillverify.api"] == []


def test_gemini_model_chain_still_decides_the_gemini_entries():
    env = {"GROQ_API_KEY": KEY, "GEMINI_MODEL_CHAIN": "gem-one, gem-two"}
    assert settings.load_questions_chain(env) == (GROQ, "gem-one", "gem-two")
    assert settings.load_questions_chain({"GROQ_API_KEY": KEY, "GEMINI_MODEL": "gem-x"}) == (GROQ, "gem-x", *settings.DEFAULT_MODEL_CHAIN)


def test_a_custom_chain_mixes_providers_in_the_given_order_and_removes_duplicates():
    env = {"GROQ_API_KEY": KEY, "QUESTIONS_MODEL_CHAIN": " gem-a , groq:llama/model-b,gem-a, GROQ:llama/model-b ,groq:other/c,,"}
    assert settings.load_questions_chain(env) == ("gem-a", "groq:llama/model-b", "groq:other/c")


def test_bad_entries_are_dropped_with_one_plain_log_line_that_never_shows_them(caplog):
    env = {"GROQ_API_KEY": KEY, "QUESTIONS_MODEL_CHAIN": "good-one,bad name!,groq:,groq:bad name,groq:ok/model"}
    assert settings.load_questions_chain(env) == ("good-one", "groq:ok/model")
    lines = [r.getMessage() for r in caplog.records if r.name == "skillverify.api"]
    assert lines == ["Ignored 3 invalid model name(s) in the question model settings (entry 2, 3, 4)."]
    assert "bad name" not in caplog.text


def test_a_custom_chain_of_only_groq_without_a_key_or_of_only_bad_names_falls_back_to_gemini():
    assert settings.load_questions_chain({"QUESTIONS_MODEL_CHAIN": "groq:a/b"}) == settings.DEFAULT_MODEL_CHAIN
    assert settings.load_questions_chain({"QUESTIONS_MODEL_CHAIN": "bad name!", "GROQ_API_KEY": KEY}) == (GROQ, *settings.DEFAULT_MODEL_CHAIN)[1:]


def test_the_chain_is_capped():
    names = ",".join(f"m{i}" for i in range(20))
    assert len(settings.load_questions_chain({"QUESTIONS_MODEL_CHAIN": names})) == settings.MAX_CHAIN_MODELS


def test_a_model_chain_can_read_its_entries_with_another_loader_and_only_when_needed(monkeypatch):
    monkeypatch.setattr(api_models, "load_dotenv", lambda: None)  # the real .env is never read
    monkeypatch.setenv("GROQ_API_KEY", KEY)
    monkeypatch.delenv("QUESTIONS_MODEL_CHAIN", raising=False)
    monkeypatch.delenv("GEMINI_MODEL_CHAIN", raising=False)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    chain = ModelChain(loader=settings.load_questions_chain)
    assert chain.models[0] == GROQ and len(chain.models) == 3
    assert chain.snapshot()[0] == {"model": GROQ, "state": "ready", "seconds_left": 0}


def test_pick_can_skip_entries_and_a_retry_after_sets_the_cooldown_with_a_floor_and_a_cap():
    now = {"t": 0.0}
    chain = ModelChain((GROQ, "gem"), clock=lambda: now["t"])
    assert chain.pick() == GROQ and chain.pick(skip={GROQ}) == "gem" and chain.pick(skip={GROQ, "gem"}) is None
    chain.mark_rate_limited(GROQ, retry_after=7)
    assert chain.snapshot()[0] == {"model": GROQ, "state": "cooling_down", "seconds_left": 7}
    now["t"] = 7.0
    assert chain.pick() == GROQ
    chain.mark_rate_limited(GROQ, retry_after=0.2)  # a floor of one second
    assert chain.snapshot()[0]["seconds_left"] == 1
    chain.mark_rate_limited(GROQ, retry_after=10 ** 6)  # a longer wait than the default is not used
    assert chain.snapshot()[0]["seconds_left"] == settings.REPEATED_LIMIT_COOLDOWN_SECONDS
    plain = ModelChain((GROQ,), clock=lambda: 0.0)
    plain.mark_rate_limited(GROQ)  # no Retry-After: the usual cooldown
    assert plain.snapshot()[0]["seconds_left"] == settings.RATE_LIMIT_COOLDOWN_SECONDS


def test_the_real_dependencies_use_their_own_question_chain_and_tests_can_share_the_gemini_one():
    deps = pipeline.default_dependencies()
    assert deps.questions_chain is not None and deps.question_chain is deps.questions_chain
    shared = pipeline.Dependencies(provider_factory=None, validator_factory=None, scanner_factory=None)
    assert shared.question_chain is shared.chain


# --------------------------------------------------------------------------
# runs: Groq first, Gemini behind it
# --------------------------------------------------------------------------
class FakeGroq(QuestionProvider):
    """A Groq stand-in: ``script`` is one step per call (an exception to raise, or None to answer normally)."""

    def __init__(self, script=()):
        super().__init__()
        self.script, self.groq_calls = list(script), 0

    def generate_json(self, system_prompt, user_prompt):
        self.groq_calls += 1
        step = self.script.pop(0) if self.script else None
        if step is not None:
            raise step
        return super().generate_json(system_prompt, user_prompt)


def limited(seconds=7):
    return GroqRateLimited("Groq rate limit reached (HTTP 429).", seconds)


@pytest.fixture
def gw(make_env, clock):
    return build_world(make_env, clock)


def build_world(make_env, clock, entries=(GROQ, "model-a", "model-b"), script=(), factory=None):
    fleet, files = Fleet(), shop_files()
    groq = FakeGroq(script)
    chain = ModelChain(CHAIN, clock=clock)
    qchain = ModelChain(entries, clock=clock)
    built = []

    def groq_factory(name):
        built.append(name)
        if factory is not None:
            return factory(name)
        return groq

    env = make_env(provider_factory=fleet.make, chain=chain, questions_chain=qchain, groq_factory=groq_factory,
                   files_factory=lambda: files, validator=FakeValidator(validation()), scanner=FakeScanner(scan()))
    world = World(env, fleet, files, clock, chain)
    world.qchain, world.groq, world.built = qchain, groq, built
    return world


def states(world):
    return [(row["model"], row["state"], row["seconds_left"]) for row in world.qchain.snapshot()]


def test_a_groq_run_reports_the_groq_model_and_no_fallback(gw):
    status = gw.ask(gw.analysis())
    assert status["state"] == "done" and len(status["result"]["questions"]) == 3
    assert status["meta"] == {"model": GROQ, "fallback_used": False, "models_used": {GROQ: 3}, "fallbacks": 0}
    assert gw.built == ["openai/gpt-oss-120b"] and gw.groq.groq_calls == 3
    assert gw.fleet.question_calls() == {"model-a": 0, "model-b": 0, "model-c": 0}  # Gemini was not called for questions


def test_groq_rate_limited_falls_to_gemini_in_the_same_run_and_rests_groq_for_the_retry_after(make_env, clock):
    world = build_world(make_env, clock, script=[limited(7)])
    status = world.ask(world.analysis())
    assert status["state"] == "done" and len(status["result"]["questions"]) == 3
    assert status["meta"] == {"model": "model-a", "fallback_used": True, "models_used": {"model-a": 3}, "fallbacks": 1}
    assert world.groq.groq_calls == 1 and world.fleet.question_calls()["model-a"] == 3
    assert states(world)[0] == (GROQ, "cooling_down", 7)
    second = world.analysis()
    assert world.ask(second)["meta"]["model"] == "model-a" and world.groq.groq_calls == 1  # Groq still resting
    clock.now += 7
    assert world.qchain.pick() == GROQ


def test_a_missing_retry_after_uses_the_usual_cooldown_and_a_longer_one_is_not_used(make_env, clock):
    world = build_world(make_env, clock, script=[GroqRateLimited("Groq rate limit reached (HTTP 429).", None)])
    world.ask(world.analysis())
    assert states(world)[0][2] == settings.RATE_LIMIT_COOLDOWN_SECONDS
    huge = build_world(make_env, clock, script=[limited(10 ** 7)])
    huge.ask(huge.analysis())
    assert states(huge)[0][2] == settings.RATE_LIMIT_COOLDOWN_SECONDS  # only a SHORTER retry-after replaces the default


def test_groq_401_skips_groq_for_the_rest_of_the_run_without_resting_it_and_without_failing(make_env, clock):
    world = build_world(make_env, clock, script=[GroqKeyRejected("Groq rejected the API key (HTTP 401). Check GROQ_API_KEY.")])
    status = world.ask(world.analysis())
    assert status["state"] == "done" and len(status["result"]["questions"]) == 3
    assert world.groq.groq_calls == 1  # one refused call, then Groq was not asked again in this run
    assert world.fleet.question_calls()["model-a"] == 3
    assert status["meta"] == {"model": "model-a", "fallback_used": True, "models_used": {"model-a": 3}, "fallbacks": 1}
    assert states(world)[0] == (GROQ, "ready", 0)  # not rested: a fixed key works on the next run


def test_groq_413_moves_this_request_to_gemini_without_cooling_groq(make_env, clock):
    world = build_world(make_env, clock, script=[GroqTooLarge("Groq request too large for this model (HTTP 413).")])
    status = world.ask(world.analysis())
    assert status["state"] == "done" and len(status["result"]["questions"]) == 3
    assert world.fleet.question_calls()["model-a"] == 1  # only the oversized request went to Gemini
    assert world.groq.groq_calls == 3  # the refused one, then the other two requests back on Groq
    assert states(world)[0] == (GROQ, "ready", 0)
    assert status["meta"]["model"] == GROQ and status["meta"]["fallback_used"] is False


def test_groq_413_when_gemini_cannot_help_is_a_plain_failure_not_a_hang(make_env, clock):
    world = build_world(make_env, clock, entries=(GROQ,), script=[GroqTooLarge("Groq request too large for this model (HTTP 413).")])
    status = world.ask(world.analysis())
    assert status["state"] == "failed" and status["error"]["code"] == "internal_error"
    assert states(world)[0] == (GROQ, "ready", 0)


def test_a_temporary_groq_failure_is_retried_once_then_groq_is_set_aside_for_the_run(make_env, clock):
    world = build_world(make_env, clock, script=[LLMCallError("Groq returned HTTP 503."), LLMCallError("Groq returned HTTP 503.")])
    status = world.ask(world.analysis())
    assert status["state"] == "done" and status["meta"]["model"] == "model-a" and status["meta"]["fallback_used"] is True
    assert world.groq.groq_calls == 2  # the first call and the one quiet retry
    assert states(world)[0] == (GROQ, "cooling_down", settings.RATE_LIMIT_COOLDOWN_SECONDS)  # a failed call rests it now


def test_a_network_problem_with_groq_falls_to_gemini_too(make_env, clock):
    error = LLMCallError("Network problem while calling Groq (Timeout).")
    world = build_world(make_env, clock, script=[error, error])
    assert world.ask(world.analysis())["meta"]["model"] == "model-a"


def test_when_groq_is_the_only_entry_a_rate_limit_is_the_usual_error_and_is_rested_once(make_env, clock):
    world = build_world(make_env, clock, entries=(GROQ,), script=[limited(5)])
    status = world.ask(world.analysis())
    assert status["state"] == "failed"
    assert status["error"] == {"code": "ai_rate_limited", "message": errors.MESSAGES["ai_rate_limited"], "retryable": True,
                               "fallback_available": False}
    assert states(world)[0] == (GROQ, "cooling_down", 5)  # marked once: not twice (no 30 minute penalty)


def test_when_groq_is_rate_limited_and_the_gemini_entry_is_resting_too_the_run_fails_as_rate_limited(make_env, clock):
    world = build_world(make_env, clock, entries=(GROQ, "model-a"), script=[limited(30)])
    world.qchain.mark_rate_limited("model-a")
    status = world.ask(world.analysis())
    assert status["state"] == "failed" and status["error"]["code"] == "ai_rate_limited"  # Groq answered first, then nothing was left
    assert status["error"]["fallback_available"] is False
    next_run = world.ask(world.analysis())  # now every entry is resting: no call is made at all
    assert next_run["state"] == "failed" and next_run["error"]["code"] == "ai_all_limited"


def test_a_groq_only_chain_with_a_refused_key_is_ai_not_configured(make_env, clock):
    world = build_world(make_env, clock, entries=(GROQ,), script=[GroqKeyRejected("Groq rejected the API key (HTTP 403).")])
    status = world.ask(world.analysis())
    assert status["state"] == "failed" and status["error"]["code"] == "ai_not_configured"


def test_groq_without_a_usable_key_is_skipped_silently_when_the_provider_cannot_be_built(make_env, clock):
    def no_key(name):
        raise LLMConfigError("GROQ_API_KEY is not set.")

    world = build_world(make_env, clock, factory=no_key)
    status = world.ask(world.analysis())
    assert status["state"] == "done" and status["meta"]["model"] == "model-a" and status["meta"]["fallbacks"] == 0
    assert world.groq.groq_calls == 0
    only = build_world(make_env, clock, entries=(GROQ,), factory=no_key)
    failed = only.ask(only.analysis())
    assert failed["state"] == "failed" and failed["error"]["code"] == "ai_all_limited"


def test_a_gemini_entry_without_a_key_is_still_ai_not_configured(make_env, clock):
    world = build_world(make_env, clock, entries=("model-a",))
    world.fleet.providers["model-a"].question_error = LLMConfigError("no key")
    status = world.ask(world.analysis())
    assert status["state"] == "failed" and status["error"]["code"] == "ai_not_configured"


def test_groq_limited_and_the_gemini_fallback_has_no_key_is_ai_not_configured(make_env, clock):
    class NoKey(dict):
        def __missing__(self, name):
            raise LLMConfigError("GEMINI_API_KEY is not set.")

    world = build_world(make_env, clock, entries=(GROQ, "model-z"), script=[limited(30)])
    world.fleet.providers = NoKey(world.fleet.providers)
    status = world.ask(world.analysis())
    assert status["state"] == "failed" and status["error"]["code"] == "ai_not_configured"


def test_gemini_behind_groq_hands_over_too(make_env, clock):
    """Groq limited, then Gemini answers 429: the run is not ended, the next Gemini entry takes over."""
    world = build_world(make_env, clock, script=[limited(30)])
    world.fleet.providers["model-a"].question_error = LLMCallError("Gemini rate limit reached (HTTP 429). Wait a while and try again.")
    status = world.ask(world.analysis())
    assert status["state"] == "done" and status["meta"]["model"] == "model-b" and status["meta"]["fallbacks"] == 2
    assert [s[1] for s in states(world)] == ["cooling_down", "cooling_down", "ready"]


def test_gemini_404_still_sets_the_model_aside_and_moves_on_behind_a_groq_failure(make_env, clock):
    world = build_world(make_env, clock, script=[limited(30)])
    world.fleet.providers["model-a"].question_error = LLMCallError("Gemini model 'model-a' was not found (HTTP 404).")
    # the replacement of the broken provider: model-b answers
    status = world.ask(world.analysis())
    assert status["state"] == "done" and status["meta"]["model"] == "model-b"
    assert states(world)[1][1] == "unavailable"


# -- bad_json and the one-repair rule ---------------------------------------------------------------------------
BAD_JSON = LLMCallError("Groq's answer was not valid JSON.")


def test_an_unusable_groq_answer_counts_as_bad_json_and_its_one_repair_attempt_goes_to_gemini(make_env, clock):
    world = build_world(make_env, clock, script=[BAD_JSON])
    status = world.ask(world.analysis())
    result = status["result"]
    assert status["state"] == "done" and len(result["questions"]) == 3 and result["dropped"] == []
    assert world.groq.groq_calls == 3  # the repair of the unusable answer went to another ready model, not to Groq
    assert world.fleet.question_calls()["model-a"] == 1
    # a repair elsewhere is not a handover (no failed call): Groq still wrote two, Gemini wrote the repaired one
    assert status["meta"] == {"model": GROQ, "fallback_used": False, "models_used": {GROQ: 2, "model-a": 1}, "fallbacks": 0}


def test_two_unusable_answers_drop_the_question_with_the_bad_json_code(make_env, clock):
    world = build_world(make_env, clock, script=[BAD_JSON])  # Groq's first attempt; the repair on Gemini is unusable too
    world.fleet.providers["model-a"].question_error = LLMCallError("Gemini's answer was not valid JSON.")
    result = world.ask(world.analysis())["result"]
    assert [d["reasons"] for d in result["dropped"]] == [["bad_json"]] and result["dropped"][0]["attempts"] == 2
    assert result["dropped"][0]["causes"] == ["not_json"]
    assert result["counts"]["dropped"] == 1


# -- the replacement route ----------------------------------------------------------------------------------------
def test_the_replacement_route_uses_the_new_chain(gw):
    job_id = gw.analysis()
    gw.ask(job_id)
    calls = gw.groq.groq_calls
    response = gw.call("POST", job_id, "/questions/replace")
    assert response.status == 200 and response.json()["question"]["id"].startswith("q_")
    assert gw.groq.groq_calls == calls + 1  # Groq wrote the replacement


def test_the_replacement_falls_back_to_gemini_when_groq_is_limited(make_env, clock):
    world = build_world(make_env, clock)
    job_id = world.analysis()
    world.ask(job_id)
    world.groq.script = [limited(20)]
    response = world.call("POST", job_id, "/questions/replace")
    assert response.status == 200
    assert world.fleet.question_calls()["model-a"] == 1
    assert states(world)[0] == (GROQ, "cooling_down", 20)


def test_a_replacement_with_groq_as_the_only_entry_and_a_rate_limit_is_a_503_error_object(make_env, clock):
    world = build_world(make_env, clock, entries=(GROQ,))
    job_id = world.analysis()
    world.ask(job_id)
    world.groq.script = [limited(20)]
    response = world.call("POST", job_id, "/questions/replace")
    assert response.status == 503 and response.json()["error"]["code"] == "ai_rate_limited"
    assert response.json()["error"]["fallback_available"] is False
    assert states(world)[0] == (GROQ, "cooling_down", 20)


# -- health ---------------------------------------------------------------------------------------------------------
def test_health_shows_groq_key_set_without_the_key_and_the_question_chain_with_states(gw, monkeypatch):
    monkeypatch.setattr("api.app.load_dotenv", lambda: None)
    monkeypatch.setenv("GROQ_API_KEY", KEY)
    gw.qchain.mark_rate_limited(GROQ, retry_after=9)
    response = gw.client.request("GET", "/api/health")
    body = response.json()
    assert body["groq_key_set"] is True and KEY not in response.text
    assert body["questions_model_chain"] == [GROQ, "model-a", "model-b"]
    assert [(r["model"], r["state"], r["seconds_left"]) for r in body["questions_models"]] == [
        (GROQ, "cooling_down", 9), ("model-a", "ready", 0), ("model-b", "ready", 0)]
    assert body["model_chain"] == list(CHAIN)  # the analysis chain (JD and CV) is unchanged
    monkeypatch.setenv("GROQ_API_KEY", "")
    assert gw.client.request("GET", "/api/health").json()["groq_key_set"] is False


def test_health_without_a_separate_question_chain_shows_the_shared_chain(make_env, monkeypatch):
    monkeypatch.setattr("api.app.load_dotenv", lambda: None)
    env = make_env()
    body = env.client.request("GET", "/api/health").json()
    assert body["questions_model_chain"] == body["model_chain"] == list(CHAIN)


# -- no key material anywhere ---------------------------------------------------------------------------------------
def test_the_key_never_appears_in_responses_errors_or_logs(make_env, clock, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr("api.app.load_dotenv", lambda: None)
    monkeypatch.setenv("GROQ_API_KEY", KEY)
    world = build_world(make_env, clock, script=[GroqKeyRejected(f"Groq rejected the API key (HTTP 401). Check GROQ_API_KEY."), ])
    status = world.ask(world.analysis())
    other = build_world(make_env, clock, script=[limited(5)])
    status2 = other.ask(other.analysis())
    texts = [json.dumps(status), json.dumps(status2), world.client.request("GET", "/api/health").text, caplog.text]
    assert all(KEY not in text for text in texts)
