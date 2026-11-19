"""Reading the CV and the job description: handover, waiting, pacing, the reading chain setting, the same-input cache
and the removal of e-mail addresses and phone numbers. Fake models, a fake clock and a fake sleep only."""
import copy
import json
import logging
import sqlite3
from types import SimpleNamespace

import pytest
from test_api_support import (CHAIN, CV_ANSWER, CV_TEXT, JD_ANSWER, JD_TEXT, FakeScanner, FakeValidator,  # noqa: F401
                              env, make_env, rescored)

from api import errors, pacing, pipeline, reading, retry, settings, storage
from api.models import ModelChain
from skillverify.cv import prompts as cv_prompts
from skillverify.jd import prompts as jd_prompts
from skillverify.jd.provider import LLMCallError, LLMConfigError, LLMProvider
from skillverify_questions.groq_provider import GroqKeyRejected, GroqRateLimited, GroqTooLarge

GROQ = "groq:openai/gpt-oss-120b"
LIMIT = LLMCallError("Gemini rate limit reached (HTTP 429). Wait a while and try again.")
BUSY = LLMCallError("Gemini returned HTTP 503.")
EMAIL, PHONE = "priya.sample@example.test", "+91 98765 43210"
OTHER_EMAIL, OTHER_PHONE = "p.sample@mail.example.test", "+91 91234 56789"
CV_WITH_CONTACTS = (f"Priya Sample\n{EMAIL}\n{PHONE}\nGitHub: github.com/octocat\nSkills: Python, SQL, Pandas\n")


class Model(LLMProvider):
    """One fake model. ``script`` errors are raised first (one per call), then the answers; every prompt is kept."""
    name, model = "fake", "fake-1"

    def __init__(self, jd=JD_ANSWER, cv=CV_ANSWER):
        self.jd, self.cv, self.script, self.prompts = jd, cv, [], []
        self.calls = 0

    def generate_json(self, system_prompt, user_prompt):
        self.calls += 1
        self.prompts.append((system_prompt, user_prompt))
        if self.script:
            raise self.script.pop(0)
        return self.jd if system_prompt == jd_prompts.extraction_system_prompt() else self.cv


class Rig:
    """An app over fake models: ``models`` for the Gemini names, ``groq`` for the ``groq:`` entries."""

    def __init__(self, make_env, folder, chain=CHAIN, store=True, jd_answer=JD_ANSWER):
        self.time = make_env.time
        self.models = {name: Model(jd=jd_answer) for name in chain if not name.startswith("groq:")}
        self.groq = {name[len("groq:"):]: Model(jd=jd_answer) for name in chain if name.startswith("groq:")}
        self.chain = ModelChain(chain, clock=self.time)
        self.store = storage.Store(folder) if store else None
        self.env = make_env(provider_factory=lambda model=None: self.models[model],
                            groq_factory=lambda name: self.groq[name], chain=self.chain, store=self.store)

    def all_models(self):
        return [*self.models.values(), *self.groq.values()]

    def calls(self):
        return {name: model.calls for name, model in {**self.models, **self.groq}.items()}

    def run(self, **overrides):
        return self.env.run(**overrides)

    def snapshot(self, entry):
        return next(row for row in self.chain.snapshot() if row["model"] == entry)


@pytest.fixture
def rig(make_env, tmp_path):
    return Rig(make_env, tmp_path)


def limits(monkeypatch, **values):
    monkeypatch.setattr(settings, "_limits_cache", settings.load_limits({k.upper(): str(v) for k, v in values.items()}))


def handover_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("reading ")]


def cv_file(text):
    return ("cv_priya.txt", text.encode())


# --------------------------------------------------------------------------
# handover
# --------------------------------------------------------------------------
def test_a_limit_on_the_first_model_rests_it_and_the_same_call_goes_to_the_next(rig, caplog):
    caplog.set_level(logging.INFO)
    rig.models["model-a"].script = [LIMIT]
    job = rig.run()
    assert job["state"] == "done" and job["error"] is None
    assert rig.calls() == {"model-a": 1, "model-b": 2, "model-c": 0}
    assert job["meta"]["readers"] == {"jd": {"model": "model-b", "cached": False},
                                      "cv": {"model": "model-b", "cached": False}}
    assert job["meta"]["model"] == "model-b" and job["meta"]["fallback_used"] is True
    assert rig.snapshot("model-a")["state"] == "cooling_down" and rig.snapshot("model-b")["state"] == "ready"
    assert handover_lines(caplog) == ["reading handover doc=jd from=model-a to=model-b reason=rate_limited"]


def test_the_same_call_is_repeated_with_the_same_prompts_on_the_next_model(rig):
    rig.models["model-a"].script = [LIMIT]
    rig.run()
    assert rig.models["model-b"].prompts[0] == rig.models["model-a"].prompts[0]


def test_a_busy_model_hands_over_at_once_while_another_is_ready_and_waits_nothing(rig, make_env):
    rig.models["model-a"].script = [BUSY]
    assert rig.run()["state"] == "done"
    assert rig.models["model-a"].calls == 1  # no quiet retries on a model that has a ready neighbour
    assert rig.snapshot("model-a")["state"] == "cooling_down" and make_env.time.slept == [2.0]  # only the CV pause


def test_the_last_model_still_retries_a_temporary_failure_quietly(make_env, tmp_path, monkeypatch):
    waited = []
    monkeypatch.setattr(retry, "_sleep", waited.append)
    rig = Rig(make_env, tmp_path, chain=("model-a",))
    rig.models["model-a"].script = [BUSY]
    assert rig.run()["state"] == "done" and waited == [5]


def test_every_model_resting_with_a_short_wait_waits_and_continues(make_env, tmp_path, caplog):
    caplog.set_level(logging.INFO)
    rig = Rig(make_env, tmp_path, chain=(GROQ,))
    rig.groq["openai/gpt-oss-120b"].script = [GroqRateLimited("Groq HTTP 429", retry_after=5)]
    job = rig.run()
    assert job["state"] == "done"
    assert make_env.time.slept[0] == 5  # the wait, then the same call again
    assert rig.groq["openai/gpt-oss-120b"].calls == 3
    assert handover_lines(caplog) == ["reading waiting doc=jd seconds=5 for a model to be ready"]


def test_every_model_resting_with_a_long_wait_ends_with_the_usual_error_and_never_waits(rig, make_env):
    for model in rig.models.values():
        model.script = [LIMIT]
    job = rig.run()
    assert job["state"] == "failed" and job["result"] is None
    assert job["error"] == {"code": "ai_rate_limited", "message": errors.MESSAGES["ai_rate_limited"],
                            "retryable": True, "fallback_available": False}
    assert make_env.time.slept == []  # 120 s is more than READING_MAX_WAIT_SECONDS
    assert "Reading the job description" in job["meta"]["timings"]  # the stage that failed is still timed


def test_a_wait_that_would_pass_the_analysis_limit_is_not_taken(make_env, tmp_path, monkeypatch):
    limits(monkeypatch, analysis_timeout_seconds=30, reading_max_wait_seconds=60)
    rig = Rig(make_env, tmp_path, chain=(GROQ,))
    rig.groq["openai/gpt-oss-120b"].script = [GroqRateLimited("Groq HTTP 429", retry_after=40)]
    job = rig.run()
    assert job["state"] == "failed" and job["error"]["code"] == "ai_rate_limited" and make_env.time.slept == []


def test_the_analysis_limit_is_never_exceeded_on_the_fake_clock(make_env, tmp_path, monkeypatch):
    limits(monkeypatch, analysis_timeout_seconds=30, reading_max_wait_seconds=60)
    rig = Rig(make_env, tmp_path, chain=(GROQ,))
    rig.groq["openai/gpt-oss-120b"].script = [GroqRateLimited("Groq HTTP 429", retry_after=12)] * 20
    started = make_env.time()
    job = rig.run()
    assert job["error"]["code"] == "ai_rate_limited"
    assert make_env.time() - started <= 30 and sum(make_env.time.slept) <= 30
    assert len(make_env.time.slept) == 2  # the third wait would have passed the limit


def test_a_start_with_everything_resting_ends_at_once_as_all_limited(rig):
    for name in CHAIN:
        rig.chain.mark_rate_limited(name)
    job = rig.run()
    assert job["error"]["code"] == "ai_all_limited" and rig.calls() == {"model-a": 0, "model-b": 0, "model-c": 0}


def test_a_start_with_a_short_rest_left_waits_for_it(rig, make_env):
    for name in CHAIN:
        rig.chain.mark_rate_limited(name, retry_after=8)
    assert rig.run()["state"] == "done" and make_env.time.slept[0] == 8


def test_an_answer_that_is_unusable_is_not_a_handover_and_is_not_saved(rig):
    rig.models["model-a"].jd = {"nothing": "here"}
    job = rig.run()
    assert job["error"]["code"] == "jd_unreadable"
    assert rig.calls() == {"model-a": 1, "model-b": 0, "model-c": 0}
    assert [r["state"] for r in rig.chain.snapshot()] == ["ready"] * 3
    assert rig.store.count_readings() == 0
    rig.models["model-a"].jd = JD_ANSWER  # a try again reads the document again
    assert rig.run()["state"] == "done" and rig.models["model-a"].calls == 3


def test_a_rejected_key_is_not_a_handover(rig):
    rig.models["model-a"].script = [LLMCallError("Gemini rejected the API key (HTTP 401). Check GEMINI_API_KEY.")]
    assert rig.run()["error"]["code"] == "ai_not_configured" and rig.models["model-b"].calls == 0


# --------------------------------------------------------------------------
# a reading chain that mixes Groq in (only when the owner asked for it)
# --------------------------------------------------------------------------
def provider(make_env, entries, kind=reading.CV, store=None, deadline=None, redact_first=False, **kwargs):
    """A bare ReadingProvider over fakes. ``fleet`` holds the fake models by entry."""
    fleet = {name: Model() for name in entries}
    chain = ModelChain(entries, clock=make_env.time)
    seen = []

    def gemini(model=None):
        if model in kwargs.get("broken", ()):
            raise LLMConfigError("not set")
        return fleet[model]

    def groq(name):
        if settings.GROQ_PREFIX + name in kwargs.get("broken", ()):
            raise LLMConfigError("not set")
        return fleet[settings.GROQ_PREFIX + name]

    deps = pipeline.Dependencies(provider_factory=gemini, validator_factory=lambda: None,
                                 scanner_factory=lambda v, t: None, chain=chain, groq_factory=groq, store=store)
    made = reading.ReadingProvider(deps, kind, lambda *args: seen.append(args), deadline, redact_first)
    made.fleet, made.chain, made.seen = fleet, chain, seen
    return made


def test_a_groq_entry_reads_when_it_is_first_and_a_rate_limit_hands_over_to_gemini(make_env, caplog):
    caplog.set_level(logging.INFO)
    made = provider(make_env, (GROQ, "gem-a"))
    made.fleet[GROQ].script = [GroqRateLimited("Groq HTTP 429", retry_after=7)]
    assert made.generate_json("system", "user") == CV_ANSWER
    assert made.seen == [("cv", GROQ, False), ("cv", "gem-a", False)]
    assert made.chain.snapshot()[0] == {"model": GROQ, "state": "cooling_down", "seconds_left": 7}  # Retry-After was shorter
    assert handover_lines(caplog) == [f"reading handover doc=cv from={GROQ} to=gem-a reason=rate_limited"]
    assert (made.name, made.model) == ("gemini", "gem-a")


def test_a_longer_retry_after_than_the_default_is_ignored(make_env):
    made = provider(make_env, (GROQ, "gem-a"))
    made.fleet[GROQ].script = [GroqRateLimited("Groq HTTP 429", retry_after=900)]
    made.generate_json("system", "user")
    assert made.chain.snapshot()[0]["seconds_left"] == settings.RATE_LIMIT_COOLDOWN_SECONDS


def test_the_name_and_model_before_any_call_are_the_first_entry(make_env):
    made = provider(make_env, (GROQ, "gem-a"))
    assert (made.name, made.model) == ("gemini", GROQ)  # nothing picked yet: the first entry of the chain
    made.generate_json("system", "user")
    assert (made.name, made.model) == ("groq", GROQ)


def test_a_rejected_groq_key_sets_groq_aside_and_gemini_reads(make_env):
    made = provider(make_env, (GROQ, "gem-a"))
    made.fleet[GROQ].script = [GroqKeyRejected("Groq rejected the API key (HTTP 401).")]
    assert made.generate_json("system", "user") == CV_ANSWER and made.seen[-1] == ("cv", "gem-a", False)
    assert made.chain.snapshot()[0]["state"] == "ready"  # set aside for this document, not rested


def test_a_rejected_groq_key_with_nothing_behind_it_is_ai_not_configured(make_env):
    made = provider(make_env, (GROQ,))
    made.fleet[GROQ].script = [GroqKeyRejected("Groq rejected the API key (HTTP 401).")]
    with pytest.raises(errors.PipelineError) as raised:
        made.generate_json("system", "user")
    assert raised.value.error["code"] == "ai_not_configured"


def test_a_request_that_is_too_large_for_groq_goes_to_gemini_without_resting_groq(make_env):
    made = provider(make_env, (GROQ, "gem-a"))
    made.fleet[GROQ].script = [GroqTooLarge("Groq request too large (HTTP 413).")]
    made.generate_json("system", "user")
    assert made.seen[-1] == ("cv", "gem-a", False) and made.chain.snapshot()[0]["state"] == "ready"


def test_a_failed_groq_call_rests_groq_and_gemini_reads(make_env):
    for message in ("Groq returned HTTP 503.", "Network problem while calling Groq (ConnectionError)."):
        made = provider(make_env, (GROQ, "gem-a"))
        made.fleet[GROQ].script = [LLMCallError(message)]
        made.generate_json("system", "user")
        assert made.seen[-1] == ("cv", "gem-a", False) and made.chain.snapshot()[0]["state"] == "cooling_down"


def test_groq_is_retried_quietly_only_as_the_last_resort(make_env, monkeypatch):
    waited = []
    monkeypatch.setattr(retry, "_sleep", waited.append)
    made = provider(make_env, (GROQ,))
    made.fleet[GROQ].script = [LLMCallError("Groq returned HTTP 503.")]
    made.generate_json("system", "user")
    assert waited == list(settings.GROQ_OUTER_RETRY_DELAYS)[:1]


def test_a_groq_entry_without_a_key_is_skipped_and_gemini_reads(make_env):
    made = provider(make_env, (GROQ, "gem-a"), broken=(GROQ,))
    assert made.generate_json("system", "user") == CV_ANSWER and made.seen == [("cv", "gem-a", False)]


def test_a_gemini_entry_that_cannot_be_built_is_ai_not_configured(make_env):
    made = provider(make_env, ("gem-a",), broken=("gem-a",))
    with pytest.raises(errors.PipelineError) as raised:
        made.generate_json("system", "user")
    assert raised.value.error["code"] == "ai_not_configured"


def test_a_missing_key_during_a_call_is_ai_not_configured(make_env):
    made = provider(make_env, ("gem-a",))
    made.fleet["gem-a"].script = [LLMConfigError("not set")]
    with pytest.raises(errors.PipelineError) as raised:
        made.generate_json("system", "user")
    assert raised.value.error["code"] == "ai_not_configured"


def test_waiting_with_no_deadline_given_still_works(make_env):
    made = provider(make_env, ("gem-a",))
    made.chain.mark_rate_limited("gem-a", retry_after=4)
    made.generate_json("system", "user")
    assert make_env.time.slept == [4]


# --------------------------------------------------------------------------
# pacing
# --------------------------------------------------------------------------
def test_two_calls_to_the_same_model_are_two_seconds_apart(rig, make_env):
    assert rig.run()["state"] == "done"
    assert make_env.time.slept == [2.0]  # the JD call at once, the CV call after the gap


def test_calls_to_different_models_do_not_wait_for_each_other(rig, make_env):
    rig.models["model-a"].script = [LIMIT]
    rig.run()
    assert make_env.time.slept == [2.0]  # only the CV call (the JD call moved to model-b, which was not used yet)


def test_the_gap_is_a_setting_and_zero_means_no_pause(make_env, tmp_path, monkeypatch):
    limits(monkeypatch, reading_min_gap_seconds=0)
    Rig(make_env, tmp_path / "zero").run()
    assert make_env.time.slept == []
    limits(monkeypatch, reading_min_gap_seconds=30)
    Rig(make_env, tmp_path / "thirty").run()
    assert make_env.time.slept[-1] > 2  # the CV call waited for the longer gap (part of it already passed)


def test_reading_and_the_question_writer_share_one_pacer(rig, make_env):
    rig.env.manager._deps.pacer.wait("model-a", 2)  # the question writer just called model-a
    rig.run()
    assert make_env.time.slept[0] == 2.0  # the first reading call waited for it


# --------------------------------------------------------------------------
# the chain setting
# --------------------------------------------------------------------------
def test_the_default_reading_chain_is_the_gemini_chain_and_never_has_groq():
    assert settings.load_reading_chain({}) == settings.DEFAULT_MODEL_CHAIN
    assert settings.load_reading_chain({"GROQ_API_KEY": "gsk_fake"}) == settings.DEFAULT_MODEL_CHAIN  # a key is not enough
    assert settings.load_reading_chain({"GROQ_API_KEY": "gsk_fake", "GEMINI_MODEL_CHAIN": "gem-one,gem-two"}) == ("gem-one", "gem-two")
    assert settings.load_questions_chain({"GROQ_API_KEY": "gsk_fake"})[0] == settings.DEFAULT_GROQ_ENTRY  # unchanged


def test_a_custom_reading_chain_can_put_groq_in_and_needs_a_key():
    env = {"READING_MODEL_CHAIN": f"{GROQ}, gem-a", "GROQ_API_KEY": "gsk_fake"}
    assert settings.load_reading_chain(env) == (GROQ, "gem-a")
    assert settings.load_reading_chain({"READING_MODEL_CHAIN": f"{GROQ},gem-a"}) == ("gem-a",)  # no key: skipped


def test_invalid_reading_entries_are_dropped_with_one_line_that_never_shows_them(caplog):
    assert settings.load_reading_chain({"READING_MODEL_CHAIN": "good-one,bad name!"}) == ("good-one",)
    lines = [r.getMessage() for r in caplog.records if r.name == "skillverify.api"]
    assert lines == ["Ignored 1 invalid model name(s) in the reading model settings (entry 2)."]
    assert "bad name" not in caplog.text
    assert settings.load_reading_chain({"READING_MODEL_CHAIN": "bad name!"}) == settings.DEFAULT_MODEL_CHAIN


def test_the_real_dependencies_read_the_chain_lazily_from_the_environment(monkeypatch):
    monkeypatch.setattr("api.models.load_dotenv", lambda: None)
    monkeypatch.setenv("GROQ_API_KEY", "gsk_fake")
    monkeypatch.delenv("READING_MODEL_CHAIN", raising=False)
    monkeypatch.delenv("GEMINI_MODEL_CHAIN", raising=False)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    assert pipeline.default_dependencies().chain.models == settings.DEFAULT_MODEL_CHAIN
    monkeypatch.setenv("READING_MODEL_CHAIN", GROQ)
    assert pipeline.default_dependencies().chain.models == (GROQ,)


@pytest.mark.parametrize("variable, value, low, high", [("READING_MIN_GAP_SECONDS", "31", 0, 30),
                                                          ("READING_MIN_GAP_SECONDS", "-1", 0, 30),
                                                          ("READING_MAX_WAIT_SECONDS", "61", 0, 60),
                                                          ("READING_MAX_WAIT_SECONDS", "soon", 0, 60)])
def test_a_bad_reading_setting_falls_back_with_one_line_that_never_shows_the_value(caplog, variable, value, low, high):
    loaded = settings.load_limits({variable: value})
    assert loaded["reading_min_gap_seconds"] == 2 and loaded["reading_max_wait_seconds"] == 20
    lines = [r.getMessage() for r in caplog.records if r.name == "skillverify.api"]
    assert lines == [f"Ignored an invalid {variable} setting (allowed {low} to {high}); using the default."]
    assert value not in caplog.text.replace(f"{low} to {high}", "")


def test_the_reading_numbers_are_valid_at_their_edges():
    loaded = settings.load_limits({"READING_MIN_GAP_SECONDS": "0", "READING_MAX_WAIT_SECONDS": "60"})
    assert (loaded["reading_min_gap_seconds"], loaded["reading_max_wait_seconds"]) == (0, 60)
    assert settings.reading_min_gap_seconds() == 2 and settings.reading_max_wait_seconds() == 20


def test_health_shows_the_reading_chain_its_states_and_both_numbers(rig):
    rig.models["model-a"].script = [LIMIT]
    rig.run()
    body = rig.env.client.request("GET", "/api/health").json()
    assert body["reading_model_chain"] == list(CHAIN) == body["model_chain"]
    assert [(r["model"], r["state"]) for r in body["reading_models"]] == [
        ("model-a", "cooling_down"), ("model-b", "ready"), ("model-c", "ready")]
    assert body["limits"]["reading_min_gap_seconds"] == 2 and body["limits"]["reading_max_wait_seconds"] == 20


# --------------------------------------------------------------------------
# the same input gives the same reading, with no model call
# --------------------------------------------------------------------------
def same_but_the_clock(a, b):
    return {**a, "generated_at": ""} == {**b, "generated_at": ""}


def test_a_repeat_of_the_same_documents_is_served_from_the_saved_readings_with_no_model_call(rig):
    first = rig.run()
    calls = rig.calls()
    second = rig.run()
    assert rig.calls() == calls  # not one model call
    assert second["meta"]["readers"] == {"jd": {"model": "model-a", "cached": True},
                                         "cv": {"model": "model-a", "cached": True}}
    assert first["meta"]["readers"]["jd"]["cached"] is False
    assert same_but_the_clock(first["result"], second["result"])  # the same score
    kept = rig.env.manager.intermediates(second["id"])
    assert second["result"] == rescored(second["result"], kept)  # still exactly what score_candidate gives


def test_a_cached_reading_remembers_which_model_read_it_so_the_backup_note_still_works(rig):
    rig.models["model-a"].script = [LIMIT]
    first = rig.run()
    assert first["meta"]["fallback_used"] is True
    again = rig.run()
    assert again["meta"]["readers"]["cv"] == {"model": "model-b", "cached": True}
    assert again["meta"]["model"] == "model-b" and again["meta"]["fallback_used"] is True


def test_a_cached_reading_survives_a_restart(make_env, tmp_path):
    first = Rig(make_env, tmp_path)
    first.run()
    second = Rig(make_env, tmp_path)  # a new server over the same folder
    job = second.run()
    assert second.calls() == {"model-a": 0, "model-b": 0, "model-c": 0}
    assert job["meta"]["readers"]["jd"]["cached"] is True and job["state"] == "done"


def test_only_the_changed_document_is_read_again(rig):
    rig.run()
    job = rig.run(jd_text=JD_TEXT + "Also: Docker.\n")
    assert job["meta"]["readers"] == {"jd": {"model": "model-a", "cached": False},
                                      "cv": {"model": "model-a", "cached": True}}
    assert rig.calls()["model-a"] == 3


def test_a_changed_cv_is_a_miss(rig):
    rig.run()
    job = rig.run(cv_file=cv_file(CV_TEXT + "Hobby: chess\n"))
    assert job["meta"]["readers"]["cv"]["cached"] is False and job["meta"]["readers"]["jd"]["cached"] is True


def test_a_new_prompt_version_is_a_miss(rig, monkeypatch):
    rig.run()
    monkeypatch.setattr(reading, "READING_PROMPT_VERSION", reading.READING_PROMPT_VERSION + 1)
    job = rig.run()
    assert job["meta"]["readers"]["jd"]["cached"] is False and rig.calls()["model-a"] == 4


def test_without_saved_results_every_run_reads_again(make_env, tmp_path):
    rig = Rig(make_env, tmp_path, store=False)
    rig.run()
    job = rig.run()
    assert rig.calls()["model-a"] == 4 and job["meta"]["readers"]["jd"] == {"model": "model-a", "cached": False}


def test_the_key_depends_on_the_prompt_the_text_and_the_version(monkeypatch):
    base = reading.reading_key("system", "text")
    assert base == reading.reading_key("system", "text") and len(base) == 64
    assert len({base, reading.reading_key("other", "text"), reading.reading_key("system", "other"),
                reading.reading_key("systemtext", ""), reading.reading_key("", "systemtext")}) == 5
    monkeypatch.setattr(reading, "READING_PROMPT_VERSION", 99)
    assert reading.reading_key("system", "text") != base


def test_a_saved_answer_is_handed_out_as_a_copy(make_env, tmp_path):
    store = storage.Store(tmp_path)
    store.put_reading(reading.reading_key("s", "u"), "gem-a", CV_ANSWER)
    made = provider(make_env, ("gem-a",), store=store)
    first = made.generate_json("s", "u")
    first["skills"].clear()
    assert made.generate_json("s", "u") == CV_ANSWER and made.fleet["gem-a"].calls == 0


# --------------------------------------------------------------------------
# privacy: e-mail addresses and phone numbers never leave
# --------------------------------------------------------------------------
def test_no_email_or_phone_reaches_a_model_a_log_the_saved_readings_or_a_response(rig, caplog, tmp_path):
    caplog.set_level(logging.DEBUG)
    rig.models["model-a"].script = [LIMIT]  # include a handover in the run
    job = rig.run(cv_file=cv_file(CV_WITH_CONTACTS))
    assert job["state"] == "done"
    sent = "\n".join(p[0] + p[1] for model in rig.all_models() for p in model.prompts)
    assert "[EMAIL]" in sent and "[PHONE]" in sent  # the fixed placeholders
    for secret in (EMAIL, "98765", "43210"):
        assert secret not in sent
    texts = [json.dumps(job), rig.env.client.request("GET", "/api/health").text,
             json.dumps(rig.env.manager.snapshot(job["id"])), caplog.text]
    with sqlite3.connect(str(tmp_path / storage.FILE_NAME)) as raw:
        texts.append("\n".join(raw.iterdump()))
    for text in texts:
        for secret in (EMAIL, "98765", "43210", "priya.sample"):
            assert secret not in text


def test_the_name_and_the_links_still_reach_the_parser(rig):
    job = rig.run(cv_file=cv_file(CV_WITH_CONTACTS))
    sent = rig.models["model-a"].prompts[1][1]  # the CV call
    assert "Priya Sample" in sent and "github.com/octocat" in sent and "Python, SQL, Pandas" in sent
    assert job["meta"]["candidate_name"] == "Priya Sample"
    assert rig.env.manager.intermediates(job["id"])["cv"]["github"]["username"] == "octocat"


def test_the_parser_still_finds_the_contacts_itself_so_the_score_does_not_change(rig):
    with_contacts = rig.run(cv_file=cv_file(CV_WITH_CONTACTS))
    kept = rig.env.manager.intermediates(with_contacts["id"])["cv"]
    assert kept["candidate"]["emails"] == [EMAIL] and "no_email_found" not in kept["warnings"]  # in memory only
    assert "no_email_found" not in with_contacts["result"]["cv_warnings"]
    without = rig.run()  # the plain CV has no contacts: the same warnings as before this change
    assert "no_email_found" in without["result"]["cv_warnings"] and "no_phone_found" in without["result"]["cv_warnings"]


def test_a_cv_that_differs_only_in_its_contacts_is_the_same_reading(rig):
    rig.run(cv_file=cv_file(CV_WITH_CONTACTS))
    calls = rig.calls()
    job = rig.run(cv_file=cv_file(CV_WITH_CONTACTS.replace(EMAIL, OTHER_EMAIL).replace(PHONE, OTHER_PHONE)))
    assert rig.calls() == calls and job["meta"]["readers"]["cv"]["cached"] is True  # the key is made after redaction


def test_the_wrapper_removes_contacts_from_text_even_if_the_parser_had_not(make_env):
    made = provider(make_env, ("gem-a",), redact_first=True)
    made.generate_json("system", f"Contact {EMAIL} or {PHONE}. Name: Priya Sample https://github.com/octocat")
    system, user = made.fleet["gem-a"].prompts[0]
    assert user == "Contact [EMAIL] or [PHONE]. Name: Priya Sample https://github.com/octocat"
    assert reading.redact(user) == user  # nothing left to remove


def test_a_job_description_is_not_redacted(make_env):
    made = provider(make_env, ("gem-a",), kind=reading.JD)
    text = "Send your CV to jobs@example.test or call 555 0100 200."
    made.generate_json("system", text)
    assert made.fleet["gem-a"].prompts[0][1] == text


# --------------------------------------------------------------------------
# the saved readings (storage)
# --------------------------------------------------------------------------
def test_a_saved_reading_is_the_model_and_the_answer_and_not_the_text(tmp_path):
    store = storage.Store(tmp_path)
    key = reading.reading_key("s", "Priya Sample, a very recognisable sentence")
    assert store.get_reading(key) is None
    assert store.put_reading(key, "gem-a", CV_ANSWER) is True
    assert store.get_reading(key) == {"model": "gem-a", "answer": CV_ANSWER}
    with sqlite3.connect(str(tmp_path / storage.FILE_NAME)) as raw:
        assert "recognisable" not in "\n".join(raw.iterdump())


def test_only_the_newest_readings_are_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "MAX_CACHED_READINGS", 3)
    now = [1000.0]
    store = storage.Store(tmp_path, clock=lambda: now[0])
    for number in range(5):
        now[0] += 1
        store.put_reading(f"key-{number}", "gem-a", copy.deepcopy(CV_ANSWER))
    assert store.count_readings() == 3
    assert store.get_reading("key-0") is None and store.get_reading("key-4") is not None


def test_old_readings_are_deleted_with_the_other_saved_results(tmp_path):
    now = [1_000_000.0]
    store = storage.Store(tmp_path, clock=lambda: now[0])
    store.put_reading("old", "gem-a", CV_ANSWER)
    now[0] += 10 * storage.SECONDS_PER_DAY
    store.put_reading("new", "gem-a", CV_ANSWER)
    now[0] += 5 * storage.SECONDS_PER_DAY
    store.purge(14)
    assert store.get_reading("old") is None and store.get_reading("new") is not None


def test_an_unusable_data_folder_means_no_saved_readings_and_no_failure(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    store = storage.Store(blocker / "inside")  # a folder that cannot be made
    assert store.get_reading("k") is None and store.put_reading("k", "gem-a", CV_ANSWER) is False
    assert store.count_readings() == 0
