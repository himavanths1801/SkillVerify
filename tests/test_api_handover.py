"""Every model hands over to the next one, the pause between calls, and the new settings. Fakes, a fake clock and a
fake sleep: nothing is real and nothing waits."""
import logging
import re

import pytest
from test_api_support import FakeTime
from test_questions_service import prepare as prepare_run
from test_questions_service import writer_for

from api import errors, pacing, pipeline, retry, settings
from api.models import ModelChain
from api.pacing import Pacer
from api.questions import _RunProvider
from skillverify.jd.provider import LLMCallError, LLMProvider
from skillverify_questions import service, writer
from skillverify_questions.groq_provider import GroqRateLimited

GROQ = "groq:openai/gpt-oss-120b"
A, B, C = "model-a", "model-b", "model-c"
RATE = LLMCallError("Gemini rate limit reached (HTTP 429). Wait a while and try again.")
BUSY = LLMCallError("Gemini returned HTTP 503.")


def limited(seconds=None):
    return GroqRateLimited("Groq rate limit reached (HTTP 429).", seconds)


class Model(LLMProvider):
    """A model whose calls are scripted: an exception to raise, or None to answer normally."""
    name, model = "fake", "fake"

    def __init__(self, inner, script=()):
        self.inner, self.script, self.calls = inner, list(script), 0

    def generate_json(self, system_prompt, user_prompt):
        self.calls += 1
        step = self.script.pop(0) if self.script else None
        if step is not None:
            raise step
        return self.inner.generate_json(system_prompt, user_prompt)


@pytest.fixture
def clock(monkeypatch):
    """One fake clock for the chain AND for the writer's own time (a sleep moves it)."""
    fake = FakeTime()
    monkeypatch.setattr(pacing, "_clock", fake)
    monkeypatch.setattr(pacing, "_sleep", fake.sleep)
    monkeypatch.setattr(retry, "_sleep", lambda seconds: None)  # the quiet outer retries of a provider never really wait
    monkeypatch.setattr(settings, "_limits_cache", settings.load_limits({"QUESTIONS_MIN_GAP_SECONDS": "0"}))
    return fake


class Rig:
    def __init__(self, clock, entries, scripts, **limits):
        if limits:
            settings._limits_cache = settings.load_limits({k: str(v) for k, v in limits.items()})
        self.clock, self.prepared = clock, prepare_run()
        inner = writer_for(self.prepared)
        self.models = {entry: Model(inner, scripts.get(entry, ())) for entry in entries}
        self.chain = ModelChain(entries, clock=clock)
        self.deps = pipeline.Dependencies(
            provider_factory=lambda name: self.models[name], validator_factory=None, scanner_factory=None,
            chain=self.chain, questions_chain=self.chain, groq_factory=lambda name: self.models[settings.GROQ_PREFIX + name],
            pacer=Pacer())
        self.published = []

    def provider(self, deadline=None):
        return _RunProvider(self.deps, lambda model, fallback: self.published.append((model, fallback)), None, deadline)

    def run(self, deadline=None):
        provider = self.provider(deadline)
        state = service.new_state(self.prepared)
        provider.attach(state)
        result, state = service.write_all(self.prepared, provider, state=state)
        return result, state, provider


def mine(caplog):
    """The log lines of THIS test's thread (a worker thread of an earlier test may still be finishing)."""
    return [r.getMessage() for r in caplog.records if r.threadName == "MainThread"]


def states(rig):
    return [(row["model"], row["state"], row["seconds_left"]) for row in rig.chain.snapshot()]


# --------------------------------------------------------------------------
# Part 1: handover
# --------------------------------------------------------------------------
def test_the_first_model_limited_on_the_2nd_write_and_the_second_on_the_4th_hand_over_in_turn(clock, caplog):
    caplog.set_level(logging.INFO)
    rig = Rig(clock, (GROQ, A, B), {GROQ: [None, limited(60)], A: [None, None, RATE]})
    result, state, _ = rig.run()
    assert result["status"] == "ok" and len(result["questions"]) == 4
    assert state.models_used == {GROQ: 1, A: 2, B: 1} and state.fallbacks == 2
    assert rig.models[GROQ].calls == 2 and rig.models[A].calls == 3 and rig.models[B].calls == 1
    lines = [line for line in mine(caplog) if line.startswith("handover")]
    assert lines == [f"handover from={GROQ} to={A} reason=rate_limited", f"handover from={A} to={B} reason=rate_limited"]
    assert states(rig) == [(GROQ, "cooling_down", 60), (A, "cooling_down", 120), (B, "ready", 0)]  # retry-after 60 < 120


def test_every_entry_but_the_last_limited_still_finishes_the_run(clock):
    rig = Rig(clock, (GROQ, A, B), {GROQ: [limited()], A: [RATE]})
    result, state, _ = rig.run()
    assert len(result["questions"]) == 4 and state.models_used == {B: 4} and state.fallbacks == 2
    assert [s[1] for s in states(rig)] == ["cooling_down", "cooling_down", "ready"]


def test_a_temporary_failure_rests_the_entry_and_hands_over_too(clock, caplog):
    caplog.set_level(logging.INFO)
    rig = Rig(clock, (A, B), {A: [BUSY, BUSY, BUSY, BUSY]})  # the outer retry of Gemini gives up, then it hands over
    result, state, _ = rig.run()
    assert len(result["questions"]) == 4 and state.models_used == {B: 4}
    assert "handover from=model-a to=model-b reason=temporary" in mine(caplog)
    assert states(rig)[0] == (A, "cooling_down", settings.RATE_LIMIT_COOLDOWN_SECONDS)


def test_a_model_that_gemini_does_not_know_is_set_aside_and_counts_as_a_handover(clock, caplog):
    caplog.set_level(logging.INFO)
    rig = Rig(clock, (A, B), {A: [LLMCallError("Gemini model 'model-a' was not found (HTTP 404).")]})
    _, state, _ = rig.run()
    assert state.models_used == {B: 4} and "handover from=model-a to=model-b reason=not_found" in mine(caplog)
    assert states(rig)[0][1] == "unavailable"


def test_the_run_ends_only_when_every_entry_is_resting_and_keeps_what_it_wrote(clock):
    rig = Rig(clock, (A, B), {A: [None, RATE], B: [None, RATE]})  # A writes 1, B writes 1, both limited on the 3rd
    provider = rig.provider()
    state = service.new_state(rig.prepared)
    provider.attach(state)
    with pytest.raises(LLMCallError) as raised:
        service.write_all(rig.prepared, provider, state=state)
    assert "HTTP 429" in str(raised.value)
    assert len(state.questions) == 2 and state.models_used == {A: 1, B: 1}  # kept: a retry continues from them
    assert [s[1] for s in states(rig)] == ["cooling_down", "cooling_down"]
    assert rig.models[A].calls == 2 and rig.models[B].calls == 2


def test_when_every_entry_is_resting_at_the_start_the_run_ends_with_ai_all_limited(clock):
    rig = Rig(clock, (A, B), {})
    rig.chain.mark_rate_limited(A)
    rig.chain.mark_rate_limited(B)
    with pytest.raises(errors.PipelineError) as raised:
        rig.provider()
    assert raised.value.error["code"] == "ai_all_limited"
    assert rig.models[A].calls == rig.models[B].calls == 0


def test_a_short_cooldown_is_waited_for_then_the_run_continues(clock, caplog):
    caplog.set_level(logging.INFO)
    rig = Rig(clock, (A, B), {A: [limited(5)], B: [limited(8)]})  # both limited with a short retry-after
    result, state, _ = rig.run()
    assert len(result["questions"]) == 4 and clock.slept == [5]  # waited for the shortest: A is ready after 5 s
    assert state.models_used == {A: 4} and state.fallbacks == 2
    assert "waiting seconds=5 for a model to be ready" in mine(caplog)


@pytest.mark.parametrize("limits,retry_after", [
    ({"QUESTIONS_MAX_WAIT_SECONDS": 10}, 12),   # the shortest cooldown is above the maximum wait
    ({"QUESTIONS_MAX_WAIT_SECONDS": 0}, 5),     # waiting is switched off
])
def test_a_cooldown_above_the_maximum_wait_ends_the_run_without_waiting(clock, limits, retry_after):
    rig = Rig(clock, (A, B), {A: [limited(retry_after)], B: [limited(retry_after + 20)]}, **limits)
    provider = rig.provider()
    state = service.new_state(rig.prepared)
    provider.attach(state)
    with pytest.raises(LLMCallError):
        service.write_all(rig.prepared, provider, state=state)
    assert clock.slept == []


def test_no_wait_when_the_run_has_no_time_left(clock):
    rig = Rig(clock, (A, B), {A: [limited(5)], B: [limited(8)]})
    provider = rig.provider(deadline=clock.now + 4)  # a 5 s wait would pass the time limit
    state = service.new_state(rig.prepared)
    provider.attach(state)
    with pytest.raises(LLMCallError):
        service.write_all(rig.prepared, provider, state=state)
    assert clock.slept == []


def test_the_wait_is_tried_a_few_times_only_so_a_stuck_clock_cannot_loop_forever(clock, monkeypatch):
    rig = Rig(clock, (A,), {A: [limited(5)]})
    monkeypatch.setattr(pacing, "_sleep", lambda seconds: None)  # a sleep that does not move the clock
    provider = rig.provider()
    with pytest.raises(GroqRateLimited):
        provider.generate_json(writer.SYSTEM_PROMPT, "Chosen file path: analysis.py")


def test_a_shorter_retry_after_is_used_and_a_longer_one_is_not(clock):
    rig = Rig(clock, (GROQ, A), {GROQ: [limited(7)]})
    rig.run()
    assert states(rig)[0] == (GROQ, "cooling_down", 7)
    long_rig = Rig(clock, (GROQ, A), {GROQ: [limited(3600)]})
    long_rig.run()
    assert states(long_rig)[0] == (GROQ, "cooling_down", settings.RATE_LIMIT_COOLDOWN_SECONDS)


def test_a_handover_does_not_use_up_the_repair_attempt(clock):
    rig = Rig(clock, (A, B), {A: [RATE]})
    provider = rig.provider()
    candidate = rig.prepared.selection.picks[0].file
    written = writer.write_question(provider, candidate, "Python", rig.prepared.trees[candidate.repo], set())
    assert written.question is not None and written.calls == 1  # the writer made ONE call: the handover was invisible
    assert rig.models[A].calls == 1 and rig.models[B].calls == 1


def test_the_repair_attempt_is_still_there_after_a_handover(clock):
    rig = Rig(clock, (A, B), {A: [RATE]})
    candidate = rig.prepared.selection.picks[0].file
    answers = rig.models[B].inner.answers
    bad = dict(answers[candidate.path], file="wrong.py")
    answers[candidate.path] = [bad, dict(answers[candidate.path])]  # B's first answer fails a check, its second is fine
    provider = rig.provider()
    written = writer.write_question(provider, candidate, "Python", rig.prepared.trees[candidate.repo], set())
    assert written.question is not None and written.calls == 2  # the handover did not use it up: the repair worked


def test_the_credit_goes_to_the_model_that_answered_and_handover_log_lines_hold_only_codes(clock, caplog):
    caplog.set_level(logging.DEBUG)
    rig = Rig(clock, (GROQ, A), {GROQ: [None, limited(30)]})
    _, state, _ = rig.run()
    assert state.models_used == {GROQ: 1, A: 3}
    handovers = [line for line in mine(caplog) if "handover" in line]
    assert handovers and all(re.fullmatch(r"handover from=\S+ to=\S+ reason=[a-z_]+", line) for line in handovers)
    for secret in ("question", "Open ", "analysis.py"):
        assert not any(secret in line for line in handovers)


def test_a_provider_without_a_state_still_counts_and_publishes(clock):
    rig = Rig(clock, (A,), {})
    seen = []
    provider = _RunProvider(rig.deps, lambda model, fallback: None, lambda used, fallbacks: seen.append((used, fallbacks)))
    provider.note_question()  # nothing answered yet: nothing is credited
    assert seen == []
    provider.generate_json(writer.SYSTEM_PROMPT, "Chosen file path: analysis.py")
    provider.note_question()
    assert seen[-1] == ({A: 1}, 0)


# --------------------------------------------------------------------------
# Part 2: pacing
# --------------------------------------------------------------------------
def test_the_pacer_keeps_the_gap_per_model_on_a_fake_clock(clock):
    pacer = Pacer()
    assert pacer.wait(A, 3) == 0  # the first call to a model never waits
    assert pacer.wait(A, 3) == 3 and clock.slept == [3]
    assert pacer.wait(B, 3) == 0  # a different model has its own clock
    clock.now += 10
    assert pacer.wait(A, 3) == 0  # enough time has passed
    assert pacer.wait(A, 0) == 0  # a gap of 0 switches pacing off


def test_two_quick_calls_after_each_other_queue_up_behind_the_reserved_slot(clock):
    pacer = Pacer()
    pacer.wait(A, 3)
    assert [pacer.wait(A, 3), pacer.wait(A, 3)] == [3, 3]
    assert clock.slept == [3, 3]


def test_the_pacer_skips_the_wait_when_it_would_pass_the_time_budget(clock):
    pacer = Pacer()
    pacer.wait(A, 3)
    assert pacer.wait(A, 3, deadline=clock.now + 2) == 0 and clock.slept == []  # 3 s would pass the limit
    assert pacer.wait(A, 3, deadline=clock.now + 100) == 3


def test_a_run_waits_the_gap_between_writes_to_the_same_model_but_not_after_a_handover(clock):
    rig = Rig(clock, (A, B), {A: [None, RATE]}, QUESTIONS_MIN_GAP_SECONDS=3)
    result, state, _ = rig.run()
    assert len(result["questions"]) == 4 and state.models_used == {A: 1, B: 3}
    # writes: A (no wait), A again (limited, waited 3), B (a different model: no wait), B, B (3 s each)
    assert clock.slept == [3, 3, 3]


def test_the_gap_never_pushes_a_run_past_its_time_limit(clock):
    rig = Rig(clock, (A,), {}, QUESTIONS_MIN_GAP_SECONDS=3)
    result, _, _ = rig.run(deadline=clock.now + 4)  # room for one 3 s gap only
    assert len(result["questions"]) == 4 and clock.slept == [3]


@pytest.mark.parametrize("variable,value,low,high", [
    ("QUESTIONS_MIN_GAP_SECONDS", "31", 0, 30), ("QUESTIONS_MIN_GAP_SECONDS", "-1", 0, 30),
    ("QUESTIONS_MIN_GAP_SECONDS", "often", 0, 30), ("QUESTIONS_MAX_WAIT_SECONDS", "121", 0, 120),
    ("QUESTIONS_MAX_WAIT_SECONDS", "1.5", 0, 120), ("SAVED_RESULTS_DAYS", "0", 1, 90), ("SAVED_RESULTS_DAYS", "91", 1, 90),
])
def test_bad_new_settings_fall_back_with_one_log_line_that_never_shows_the_value(variable, value, low, high, caplog):
    caplog.set_level(logging.DEBUG)
    assert settings.load_limits({variable: value}) == settings.load_limits({})
    lines = [r.getMessage() for r in caplog.records if r.name == "skillverify.api"]
    assert lines == [f"Ignored an invalid {variable} setting (allowed {low} to {high}); using the default."]


def test_the_new_settings_have_their_documented_defaults_and_edges():
    assert settings.load_limits({}) | {} == settings.load_limits({})
    defaults = settings.load_limits({})
    assert (defaults["questions_min_gap_seconds"], defaults["questions_max_wait_seconds"],
            defaults["saved_results_days"]) == (3, 25, 14)
    edges = settings.load_limits({"QUESTIONS_MIN_GAP_SECONDS": "0", "QUESTIONS_MAX_WAIT_SECONDS": "120",
                                  "SAVED_RESULTS_DAYS": "90"})
    assert (edges["questions_min_gap_seconds"], edges["questions_max_wait_seconds"], edges["saved_results_days"]) == (0, 120, 90)


def test_the_new_setting_accessors_read_the_loaded_values(monkeypatch):
    monkeypatch.setattr(settings, "_limits_cache", settings.load_limits({"QUESTIONS_MIN_GAP_SECONDS": "7",
                                                                        "QUESTIONS_MAX_WAIT_SECONDS": "9",
                                                                        "SAVED_RESULTS_DAYS": "30"}))
    assert (settings.questions_min_gap_seconds(), settings.questions_max_wait_seconds(), settings.saved_results_days()) == (7, 9, 30)


def test_the_data_dir_comes_from_the_setting_or_defaults_to_api_data(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "load_dotenv", lambda: None)  # the real .env is never read
    monkeypatch.delenv("SKILLVERIFY_DATA_DIR", raising=False)
    assert settings.data_dir().parts[-2:] == ("api", "_data")
    monkeypatch.setenv("SKILLVERIFY_DATA_DIR", str(tmp_path))
    assert settings.data_dir() == tmp_path


def test_health_shows_the_two_new_limits_as_numbers(make_env):
    env = make_env()
    limits = env.client.request("GET", "/api/health").json()["limits"]
    assert limits["questions_min_gap_seconds"] == 3 and limits["questions_max_wait_seconds"] == 25
    assert "saved_results_days" not in limits and all(isinstance(v, int) for v in limits.values())


from test_api_support import make_env  # noqa: E402,F401 (a fixture)
