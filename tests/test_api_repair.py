"""Fix 2 (the repair goes to a different model), Fix 4 (the message follows the effective count) and the new
output-token setting. Fake providers, fake GitHub, fake clock and fake sleep; nothing real."""
import logging

import pytest
from test_api_groq import GROQ
from test_api_questions import (SYMBOLS, Fleet, QuestionProvider, World, clock, scan, shop_files,  # noqa: F401
                                validation)
from test_api_saved import build, folder  # noqa: F401 (folder is a fixture)
from test_api_support import CHAIN, FakeScanner, FakeValidator, make_env  # noqa: F401 (make_env is a fixture)
from test_questions_support import good_answer

from api import pipeline, settings
from api.models import ModelChain
from skillverify.jd import provider as jd_provider
from skillverify.jd.provider import LLMCallError, LLMConfigError
from skillverify_questions import config, service
from skillverify_questions.gemini_questions import QuestionGemini


class FirstBad(QuestionProvider):
    """Answers every file's FIRST attempt with an answer that fails a check (wrong file); later attempts are good."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.seen = set()

    def generate_json(self, system_prompt, user_prompt):
        answer = super().generate_json(system_prompt, user_prompt)
        if system_prompt == service.write_question.__globals__["SYSTEM_PROMPT"]:
            path = next(l.split(": ", 1)[1] for l in user_prompt.splitlines() if l.startswith("Chosen file path:"))
            if path not in self.seen:
                self.seen.add(path)
                return {**answer, "file": "somewhere-else.py"}
        return answer


def rig(make_env, clock, providers, entries=("model-a", "model-b"), groq_factory=None):
    fleet, files = Fleet(), shop_files()
    fleet.providers.update(providers)
    chain, qchain = ModelChain(CHAIN, clock=clock), ModelChain(entries, clock=clock)
    kwargs = {"groq_factory": groq_factory} if groq_factory else {}
    env = make_env(provider_factory=fleet.make, chain=chain, questions_chain=qchain, files_factory=lambda: files,
                   validator=FakeValidator(validation()), scanner=FakeScanner(scan()), **kwargs)
    world = World(env, fleet, files, clock, chain)
    world.qchain = qchain
    return world


# --------------------------------------------------------------------------
# Fix 2: the repair attempt goes to a different ready model
# --------------------------------------------------------------------------
def test_the_repair_goes_to_the_other_ready_model_and_the_next_question_starts_on_the_first_again(make_env, clock, caplog):
    caplog.set_level(logging.DEBUG)
    world = rig(make_env, clock, {"model-a": FirstBad()})
    status = world.ask(world.analysis())
    questions = status["result"]["questions"]
    assert status["state"] == "done" and len(questions) == 3 and status["result"]["dropped"] == []
    calls = world.fleet.question_calls()
    assert (calls["model-a"], calls["model-b"], calls["model-c"]) == (3, 3, 0)  # every first attempt on a, every repair on b
    meta = status["meta"]
    assert meta["models_used"] == {"model-b": 3}  # the model that wrote the final accepted question
    assert meta["model"] == "model-b" and meta["fallback_used"] is True
    assert meta["fallbacks"] == 0  # a repair elsewhere is not a handover: nothing failed
    lines = [r.getMessage() for r in caplog.records if r.name == "skillverify.api"]
    assert lines.count("repair on another model from=model-a to=model-b") == 3
    assert not any("handover" in line for line in lines)


def test_a_question_that_passes_the_first_time_stays_on_the_first_model(make_env, clock):
    world = rig(make_env, clock, {})
    status = world.ask(world.analysis())
    calls = world.fleet.question_calls()
    assert (calls["model-a"], calls["model-b"]) == (3, 0)
    assert status["meta"]["models_used"] == {"model-a": 3} and status["meta"]["fallback_used"] is False


def test_the_same_model_repairs_only_when_no_other_one_is_ready(make_env, clock, caplog):
    caplog.set_level(logging.DEBUG)
    world = rig(make_env, clock, {"model-a": FirstBad()})
    world.qchain.mark_rate_limited("model-b")  # resting
    status = world.ask(world.analysis())
    assert status["state"] == "done" and status["result"]["dropped"] == []
    calls = world.fleet.question_calls()
    assert (calls["model-a"], calls["model-b"]) == (6, 0)
    assert status["meta"]["models_used"] == {"model-a": 3} and status["meta"]["fallback_used"] is False
    assert "repair on another model" not in caplog.text


def test_a_model_that_hits_a_limit_during_the_repair_hands_over_as_usual(make_env, clock):
    limited = QuestionProvider(question_error=LLMCallError("Gemini rate limit reached (HTTP 429). Wait a while."))
    world = rig(make_env, clock, {"model-a": FirstBad(), "model-b": limited})
    status = world.ask(world.analysis())
    assert status["state"] == "done" and status["result"]["dropped"] == [] and len(status["result"]["questions"]) == 3
    rows = {row["model"]: row["state"] for row in world.qchain.snapshot()}
    assert rows["model-b"] == "cooling_down"           # the rest from the previous build still applies
    assert status["meta"]["fallbacks"] >= 1             # the failed repair call handed back to model-a
    assert status["meta"]["models_used"] == {"model-a": 3}
    assert world.fleet.question_calls()["model-b"] == 1  # rested after its first limit, never asked again


def test_a_second_entry_that_cannot_be_built_leaves_the_repair_on_the_first(make_env, clock):
    def groq_factory(name):
        raise LLMConfigError("GROQ_API_KEY is not set.")

    world = rig(make_env, clock, {"model-a": FirstBad()}, entries=("model-a", GROQ), groq_factory=groq_factory)
    status = world.ask(world.analysis())
    assert status["state"] == "done" and status["result"]["dropped"] == []
    assert world.fleet.question_calls()["model-a"] == 6
    assert GROQ not in {row["model"] for row in world.qchain.snapshot() if row["state"] != "ready"}  # not rested: set aside


def test_a_plain_provider_without_the_hook_still_works(make_env, clock):
    class Plain:  # what the writer is given in other tests: no ``repair_elsewhere``
        def generate_json(self, system, user):
            raise AssertionError("not called")

    assert getattr(Plain(), "repair_elsewhere", None) is None


# --------------------------------------------------------------------------
# the output-token setting
# --------------------------------------------------------------------------
def test_the_setting_defaults_to_no_cap_and_accepts_the_documented_range(caplog):
    caplog.set_level(logging.DEBUG)
    assert settings.load_limits({})["questions_max_output_tokens"] == settings.DEFAULT_QUESTIONS_MAX_OUTPUT_TOKENS == 0
    assert [r for r in caplog.records if r.name == "skillverify.api"] == []
    for value in ("500", " 2500 ", "8000"):
        assert settings.load_limits({"QUESTIONS_MAX_OUTPUT_TOKENS": value})["questions_max_output_tokens"] == int(value)
    assert settings.load_limits({"QUESTIONS_MAX_OUTPUT_TOKENS": ""})["questions_max_output_tokens"] == 0


@pytest.mark.parametrize("value", ["499", "8001", "0", "-1", "lots", "1.5", "4e3"])
def test_a_bad_value_is_the_default_with_one_plain_log_line_that_never_shows_it(value, caplog):
    caplog.set_level(logging.DEBUG)
    assert settings.load_limits({"QUESTIONS_MAX_OUTPUT_TOKENS": value})["questions_max_output_tokens"] == 0
    lines = [r.getMessage() for r in caplog.records if r.name == "skillverify.api"]
    assert lines == ["Ignored an invalid QUESTIONS_MAX_OUTPUT_TOKENS setting (allowed 500 to 8000); using the default."]
    assert "lots" not in caplog.text and "4e3" not in caplog.text  # the value itself is never printed


def test_the_setting_is_shown_by_health(make_env):
    env = make_env()
    assert env.client.request("GET", "/api/health").json()["limits"]["questions_max_output_tokens"] == 0


class RecordingGroq(QuestionProvider):
    def __init__(self):
        super().__init__()
        self.caps = []

    def set_max_output_tokens(self, tokens):
        self.caps.append(tokens)


@pytest.mark.parametrize("env_value,expected", [("2500", [2500]), ("", [])])
def test_the_configured_cap_is_given_to_the_groq_provider_of_a_run(make_env, clock, monkeypatch, env_value, expected):
    groq = RecordingGroq()
    world = rig(make_env, clock, {}, entries=(GROQ, "model-a"), groq_factory=lambda name: groq)
    monkeypatch.setattr(settings, "_limits_cache",
                        settings.load_limits({"QUESTIONS_MAX_OUTPUT_TOKENS": env_value} if env_value else {}))
    assert world.ask(world.analysis())["state"] == "done"
    assert set(groq.caps) == set(expected) and (bool(groq.caps) == bool(expected))


def test_the_question_writer_gets_its_own_gemini_provider_and_the_reading_chain_keeps_the_plain_one(monkeypatch):
    monkeypatch.setattr(jd_provider, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("GEMINI_API_KEY", "SECRET-KEY-123")
    deps = pipeline.default_dependencies()
    assert isinstance(deps.question_provider_factory("gem-q"), QuestionGemini)
    assert type(deps.provider_factory("gem-r")) is jd_provider.GeminiProvider
    plain = pipeline.Dependencies(provider_factory=None, validator_factory=None, scanner_factory=None)
    assert plain.question_provider_factory is None


def test_a_test_factory_for_the_question_provider_is_used_for_gemini_entries(make_env, clock):
    fleet = Fleet()
    used = []
    world = rig(make_env, clock, {})
    world.env.questions._deps.question_provider_factory = lambda model: used.append(model) or fleet.providers[model]
    assert world.ask(world.analysis())["state"] == "done"
    assert used and set(used) <= set(CHAIN)


# --------------------------------------------------------------------------
# Fix 4: the message, the count and the status follow what the screen shows
# --------------------------------------------------------------------------
def effective(status, count_message):
    return status["result"]["status"], status["result"]["message"], status["result"]["counts"]["questions"], count_message


def too_few(n, minimum):
    return config.MESSAGE_TOO_FEW.format(n=n, min=minimum)


def test_effective_count_with_0_1_and_2_replacements():
    one = {"status": "too_few", "message": too_few(1, 3), "counts": {"questions": 1, "dropped": 7},
           "questions": [{"id": "q_1"}], "dropped": [{"reasons": ["bad_json"]}] * 7, "schema_version": 1}
    assert service.with_replacements(one, 0) is one
    first = service.with_replacements(one, 1)
    assert first["status"] == "too_few" and first["message"] == too_few(2, 3)
    assert first["counts"] == {"questions": 2, "dropped": 7}
    second = service.with_replacements(one, 2)
    assert second["status"] == "ok" and second["message"] is None and second["counts"]["questions"] == 3
    assert one["status"] == "too_few" and one["counts"]["questions"] == 1 and one["message"] == too_few(1, 3)  # untouched
    assert second["questions"] == one["questions"] and second["dropped"] == one["dropped"] and second["schema_version"] == 1


def test_with_replacements_leaves_odd_results_alone():
    for odd in ({}, {"questions": "x"}, None):
        assert service.with_replacements(odd, 2) is odd
    bare = service.with_replacements({"questions": [{}], "status": "too_few"}, 1)
    assert bare["counts"] == {"questions": 2}


def test_the_status_changes_at_the_minimum(monkeypatch):
    base = {"status": "too_few", "message": "old", "counts": {"questions": 3}, "questions": [{}, {}, {}]}
    monkeypatch.setattr(config, "MIN_QUESTIONS", 5)
    assert service.with_replacements(base, 1)["status"] == "too_few"
    assert service.with_replacements(base, 1)["message"] == too_few(4, 5)
    assert service.with_replacements(base, 2)["status"] == "ok"
    none_before = {"status": "none", "message": config.MESSAGE_NONE, "counts": {"questions": 0}, "questions": []}
    assert service.with_replacements(none_before, 1)["message"] == too_few(1, 5)


def result_of(world, job_id):
    return world.call("GET", job_id, "/questions").json()["result"]


def test_a_replacement_corrects_the_message_the_count_and_the_status(make_env, clock, folder, monkeypatch):
    monkeypatch.setattr(config, "MIN_QUESTIONS", 4)
    world = build(make_env, clock, folder)
    job_id = world.analysis()
    first = world.ask(job_id)["result"]
    assert (first["status"], first["message"], first["counts"]["questions"]) == ("too_few", too_few(3, 4), 3)
    response = world.call("POST", job_id, "/questions/replace")
    assert response.status == 200
    after = result_of(world, job_id)
    assert (after["status"], after["message"], after["counts"]["questions"]) == ("ok", None, 4)
    assert after["questions"] == first["questions"]  # the run's own questions are as they were; the screen adds the replacement
    saved = world.store.load_run(job_id)["result"]  # the saved original is not rewritten
    assert (saved["status"], saved["message"], saved["counts"]["questions"]) == ("too_few", too_few(3, 4), 3)
    restarted = build(make_env, clock, folder)  # a restart keeps the corrected message
    again = result_of(restarted, job_id)
    assert (again["status"], again["message"], again["counts"]["questions"]) == ("ok", None, 4)
    assert restarted.store.load_run(job_id)["result"]["status"] == "too_few"


def test_the_message_keeps_its_wording_and_only_n_changes_while_still_below_the_minimum(make_env, clock, monkeypatch):
    monkeypatch.setattr(config, "MIN_QUESTIONS", 6)
    world = rig(make_env, clock, {})
    job_id = world.analysis()
    first = world.ask(job_id)["result"]
    assert first["message"] == "Only 3 question(s) could be written from the candidate's code. A session needs at least 6."
    assert world.call("POST", job_id, "/questions/replace").status == 200
    now = result_of(world, job_id)
    assert now["message"] == "Only 4 question(s) could be written from the candidate's code. A session needs at least 6."
    assert now["status"] == "too_few" and now["counts"]["questions"] == 4
    assert result_of(world, job_id)["counts"]["dropped"] == first["counts"]["dropped"]


def test_a_finished_run_without_replacements_is_returned_as_it_is(make_env, clock):
    world = rig(make_env, clock, {})
    job_id = world.analysis()
    status = world.ask(job_id)
    assert result_of(world, job_id) == status["result"] and status["result"]["status"] == "ok"


def test_regenerate_forgets_the_replacements_of_the_old_run(make_env, clock, folder, monkeypatch):
    monkeypatch.setattr(config, "MIN_QUESTIONS", 4)
    world = build(make_env, clock, folder)
    job_id = world.analysis()
    world.ask(job_id)
    world.call("POST", job_id, "/questions/replace")
    assert result_of(world, job_id)["counts"]["questions"] == 4
    assert world.call("POST", job_id, "/questions", raw_body=b'{"regenerate": true}',
                      content_type="application/json").status == 202
    fresh = world.wait(job_id)["result"]
    assert fresh["counts"]["questions"] == 3 and fresh["status"] == "too_few" and fresh["message"] == too_few(3, 4)
