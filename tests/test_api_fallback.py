"""Gemini model fallback chain: runs, retries, errors, health. Fake providers and a fake clock; no network."""
import json
import logging

import pytest
from test_api_support import CHAIN, FakeProvider, env, make_env, rescored, settings  # noqa: F401

from api import errors, retry
from api import settings as api_settings
from api.models import ModelChain
from skillverify.jd.provider import LLMCallError
from skillverify.scoring import score_candidate

LIMIT = LLMCallError("Gemini rate limit reached (HTTP 429). Wait a while and try again.")
NOT_FOUND = LLMCallError("Gemini model 'x' was not found (HTTP 404). Set GEMINI_MODEL to a current model name.")
BUSY = LLMCallError("Gemini returned HTTP 503.")
BAD_KEY = LLMCallError("Gemini rejected the API key (HTTP 401). Check GEMINI_API_KEY.")
SECRET = "SECRET-KEY-VALUE-123"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Fleet:
    """One fake provider per model. ``fleet.make(model)`` is the provider factory the pipeline calls."""

    def __init__(self):
        self.providers = {name: FakeProvider() for name in CHAIN}
        self.built = []  # the model of every provider built, in order

    def make(self, model):
        self.built.append(model)
        return self.providers[model]

    def fail(self, model, error=None, **kwargs):
        self.providers[model].always = error
        for key, value in kwargs.items():
            setattr(self.providers[model], key, value)

    def calls(self):
        return {name: provider.calls for name, provider in self.providers.items()}


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def fleet():
    return Fleet()


@pytest.fixture
def world(make_env, fleet, clock, monkeypatch):
    monkeypatch.setattr(retry, "_sleep", lambda seconds: None)
    chain = ModelChain(CHAIN, clock=clock)
    made = make_env(provider_factory=fleet.make, chain=chain)
    made.chain, made.fleet, made.clock = chain, fleet, clock
    return made


def retry_job(world, job_id):
    response = world.client.request("POST", f"/api/analyses/{job_id}/retry")
    assert response.status == 202, response.text
    return world.wait(job_id)


def state(world, model):
    return next(row for row in world.chain.snapshot() if row["model"] == model)["state"]


# --------------------------------------------------------------------------
# the next run uses the next model (retry button AND analyze again)
# --------------------------------------------------------------------------
def test_a_rate_limit_marks_the_model_and_retry_uses_the_next_one(world, fleet):
    fleet.fail("model-a", LIMIT)
    first = world.run()
    assert first["state"] == "failed" and first["error"]["code"] == "ai_rate_limited"
    assert first["error"] == {"code": "ai_rate_limited", "message": errors.MESSAGES["ai_rate_limited"],
                              "retryable": True, "fallback_available": True}
    assert first["meta"]["model"] == "model-a" and first["meta"]["fallback_used"] is False
    assert state(world, "model-a") == "cooling_down"

    second = retry_job(world, first["id"])
    assert second["state"] == "done" and second["error"] is None
    assert second["meta"]["model"] == "model-b" and second["meta"]["fallback_used"] is True
    assert fleet.built == ["model-a", "model-b"]


def test_analyzing_again_behaves_the_same_as_retry(world, fleet):
    fleet.fail("model-a", LIMIT)
    assert world.run()["error"]["code"] == "ai_rate_limited"
    fresh = world.run()  # a new POST, not the retry route
    assert fresh["state"] == "done"
    assert fresh["meta"]["model"] == "model-b" and fresh["meta"]["fallback_used"] is True


def test_each_press_moves_further_along_the_chain(world, fleet):
    fleet.fail("model-a", LIMIT)
    fleet.fail("model-b", LIMIT)
    one = world.run()
    two = retry_job(world, one["id"])
    assert two["error"]["code"] == "ai_rate_limited" and two["error"]["fallback_available"] is True
    assert two["meta"]["model"] == "model-b"
    three = retry_job(world, one["id"])
    assert three["state"] == "done" and three["meta"]["model"] == "model-c"
    assert fleet.built == ["model-a", "model-b", "model-c"]


def test_the_last_model_limited_says_no_backup_is_left(world, fleet):
    for name in CHAIN:
        fleet.fail(name, LIMIT)
    runs = [world.run() for _ in range(3)]
    assert [r["error"]["fallback_available"] for r in runs] == [True, True, False]


# --------------------------------------------------------------------------
# every model resting
# --------------------------------------------------------------------------
def test_all_models_limited_fails_at_once_without_any_gemini_call(world, fleet):
    for name in CHAIN:
        fleet.fail(name, LIMIT)
    for _ in range(3):
        world.run()
    before = fleet.calls()
    built = list(fleet.built)
    job = world.run()
    assert job["state"] == "failed" and job["result"] is None
    assert job["error"] == {"code": "ai_all_limited", "message": errors.MESSAGES["ai_all_limited"],
                            "retryable": True}
    assert job["error"]["message"] == ("All available AI models have reached their limits. "
                                       "Wait a few minutes and try again.")
    assert fleet.calls() == before and fleet.built == built  # nothing was even built
    assert job["meta"]["model"] is None and job["meta"]["fallback_used"] is False


def test_a_recovered_main_model_is_used_again(world, fleet, clock):
    fleet.fail("model-a", LIMIT)
    world.run()
    fleet.fail("model-a", None)  # Gemini is happy again
    assert world.run()["meta"]["model"] == "model-b"  # still cooling
    clock.now += api_settings.RATE_LIMIT_COOLDOWN_SECONDS
    job = world.run()
    assert job["state"] == "done" and job["meta"]["model"] == "model-a" and job["meta"]["fallback_used"] is False


def test_a_second_limit_without_a_success_rests_longer_and_success_resets(world, fleet, clock):
    fleet.fail("model-a", LIMIT)
    world.run()
    clock.now += api_settings.RATE_LIMIT_COOLDOWN_SECONDS
    world.run()  # limited again, nothing in between
    clock.now += api_settings.RATE_LIMIT_COOLDOWN_SECONDS
    assert world.chain.pick() == "model-b"
    clock.now += api_settings.REPEATED_LIMIT_COOLDOWN_SECONDS
    fleet.fail("model-a", None)
    assert world.run()["meta"]["model"] == "model-a"  # success clears the count
    fleet.fail("model-a", LIMIT)
    world.run()
    clock.now += api_settings.RATE_LIMIT_COOLDOWN_SECONDS
    assert world.chain.pick() == "model-a"  # short cooldown again


# --------------------------------------------------------------------------
# one model per run
# --------------------------------------------------------------------------
def test_one_run_uses_one_model_for_both_parsers(world, fleet):
    fleet.fail("model-a", LIMIT)
    world.run()
    job = world.run()
    assert fleet.calls() == {"model-a": 1, "model-b": 2, "model-c": 0}  # JD and CV both went to model-b
    assert fleet.built.count("model-b") == 1 and job["meta"]["model"] == "model-b"


def test_a_limit_on_the_cv_call_still_counts_for_the_run_model(world, fleet):
    fleet.fail("model-a", None, cv_error=LIMIT)
    job = world.run()
    assert job["error"]["code"] == "ai_rate_limited" and state(world, "model-a") == "cooling_down"


# --------------------------------------------------------------------------
# an unknown model name is a configuration problem: switch silently inside the same run
# --------------------------------------------------------------------------
def test_a_404_on_the_first_call_switches_models_within_the_run(world, fleet):
    fleet.fail("model-a", NOT_FOUND)
    job = world.run()
    assert job["state"] == "done" and job["error"] is None
    assert job["meta"]["model"] == "model-b" and job["meta"]["fallback_used"] is True
    assert fleet.built == ["model-a", "model-b"] and fleet.calls() == {"model-a": 1, "model-b": 2, "model-c": 0}
    assert state(world, "model-a") == "unavailable"
    assert world.chain.pick() == "model-b"


def test_when_every_model_is_unknown_the_run_fails_with_all_limited_after_one_try_each(world, fleet):
    for name in CHAIN:
        fleet.fail(name, NOT_FOUND)
    job = world.run()
    assert job["error"]["code"] == "ai_all_limited" and job["error"]["retryable"] is True
    assert fleet.calls() == {"model-a": 1, "model-b": 1, "model-c": 1}  # never more than the chain length
    assert [row["state"] for row in world.chain.snapshot()] == ["unavailable"] * 3


def test_a_404_after_the_first_call_is_still_the_old_configuration_error(world, fleet):
    fleet.fail("model-a", None, cv_error=NOT_FOUND)
    job = world.run()
    assert job["error"]["code"] == "ai_not_configured"
    assert [row["state"] for row in world.chain.snapshot()] == ["ready"] * 3


def test_a_429_after_a_404_switch_marks_the_model_that_was_limited(world, fleet):
    fleet.fail("model-a", NOT_FOUND)
    fleet.fail("model-b", LIMIT)
    job = world.run()
    assert job["error"]["code"] == "ai_rate_limited" and job["meta"]["model"] == "model-b"
    assert [row["state"] for row in world.chain.snapshot()] == ["unavailable", "cooling_down", "ready"]
    assert job["error"]["fallback_available"] is True


# --------------------------------------------------------------------------
# other errors leave the chain alone
# --------------------------------------------------------------------------
@pytest.mark.parametrize("error, code", [(BUSY, "ai_busy"), (BAD_KEY, "ai_not_configured"),
                                          (LLMCallError("Gemini's answer was not valid JSON."), "internal_error")])
def test_other_errors_do_not_change_any_model_state(world, fleet, error, code):
    fleet.fail("model-a", error)
    job = world.run()
    assert job["error"]["code"] == code and "fallback_available" not in job["error"]
    assert [row["state"] for row in world.chain.snapshot()] == ["ready"] * 3
    assert fleet.built == ["model-a"]  # and no silent switch either


def test_a_failure_after_the_ai_steps_does_not_change_model_state(world, fleet):
    world.validator.raises = RuntimeError("github down")
    job = world.run()
    assert job["error"]["code"] == "github_unavailable"
    assert [row["state"] for row in world.chain.snapshot()] == ["ready"] * 3
    assert world.chain._limits_in_a_row.get("model-a", 0) == 0


def test_a_missing_key_changes_nothing(make_env, clock):
    from skillverify.jd.provider import LLMConfigError

    def no_key(model):
        raise LLMConfigError("GEMINI_API_KEY is not set.")
    chain = ModelChain(CHAIN, clock=clock)
    job = make_env(provider_factory=no_key, chain=chain).run()
    assert job["error"]["code"] == "ai_not_configured"
    assert [row["state"] for row in chain.snapshot()] == ["ready"] * 3


# --------------------------------------------------------------------------
# result and privacy
# --------------------------------------------------------------------------
def test_result_is_exactly_score_candidate_whatever_model_ran(world, fleet):
    fleet.fail("model-a", LIMIT)
    world.run()
    job = world.run()
    kept = world.manager.intermediates(job["id"])
    assert json.dumps(job["result"], sort_keys=True) == json.dumps(rescored(job["result"], kept), sort_keys=True)
    assert "model" not in job["result"] and "fallback_used" not in job["result"]


def test_meta_keeps_the_model_after_done_and_is_replaced_by_a_retry(world, fleet):
    fleet.fail("model-a", LIMIT)
    first = world.run()
    assert world.client.request("GET", f"/api/analyses/{first['id']}").json()["meta"]["model"] == "model-a"
    second = retry_job(world, first["id"])
    assert second["meta"]["model"] == "model-b"
    again = world.client.request("GET", f"/api/analyses/{first['id']}").json()
    assert again["meta"]["model"] == "model-b" and again["meta"]["fallback_used"] is True


def test_health_shows_the_chain_and_states_and_no_key_text(world, fleet, monkeypatch):
    monkeypatch.setattr("api.app.load_dotenv", lambda: None)
    monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    fleet.fail("model-a", LIMIT)
    world.run()
    response = world.client.request("GET", "/api/health")
    body = response.json()
    assert body["model_chain"] == list(CHAIN)
    assert [(row["model"], row["state"]) for row in body["models"]] == [
        ("model-a", "cooling_down"), ("model-b", "ready"), ("model-c", "ready")]
    assert body["models"][0]["seconds_left"] == api_settings.RATE_LIMIT_COOLDOWN_SECONDS
    assert body["gemini_key_set"] is True and SECRET not in response.text


def test_no_response_or_log_line_contains_key_material(world, fleet, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    fleet.fail("model-a", LIMIT)
    texts = []
    for _ in range(3):
        job = world.run()
        texts.append(json.dumps(job))
    texts.append(world.client.request("GET", "/api/health").text)
    texts.extend(r.getMessage() for r in caplog.records)
    assert not any(SECRET in text for text in texts)


def test_error_object_carries_fallback_available_only_when_given():
    assert "fallback_available" not in errors.error_object("ai_rate_limited")
    assert errors.error_object("ai_rate_limited", fallback_available=False)["fallback_available"] is False


def test_a_config_error_raised_during_the_cv_call_is_ai_not_configured(world, fleet):
    from skillverify.jd.provider import LLMConfigError
    fleet.fail("model-a", None, cv_error=LLMConfigError("GEMINI_API_KEY"))
    job = world.run()
    assert job["error"]["code"] == "ai_not_configured"
    assert [row["state"] for row in world.chain.snapshot()] == ["ready"] * 3
