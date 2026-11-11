"""The two essential error rules (Gemini: quiet retries then an error; GitHub: plain error, no retries),
plus parser problems and unexpected failures. Offline, fakes only."""
import threading
import time

import pytest
from test_api_support import (FakeProvider, FakeScanner, FakeValidator, TECH_OK, env, github_ok, make_env,  # noqa: F401
                              scan_item, settings)

from api import errors, pipeline, retry
from skillverify import GitHubConfigError
from skillverify.jd.provider import LLMCallError, LLMConfigError
from skillverify.scoring import ScoringConfigError

CLOSED_SET = {"invalid_input", "jd_unreadable", "cv_unreadable", "github_user_not_found", "github_unavailable",
              "ai_busy", "ai_rate_limited", "ai_all_limited", "ai_not_configured", "timeout", "internal_error"}
DELAYS = (5, 10, 20)


@pytest.fixture
def sleeps(monkeypatch):
    """Record the outer retry waits instead of sleeping."""
    waited = []
    monkeypatch.setattr(retry, "_sleep", waited.append)
    return waited


def failed_with(job, code, retryable):
    assert job["state"] == "failed" and job["result"] is None  # never a score from partial data
    assert job["error"]["code"] == code in CLOSED_SET
    assert job["error"]["retryable"] is retryable
    extra = {"fallback_available"} if code == "ai_rate_limited" else set()  # only a rate limit says this
    if code == "timeout":
        extra = {"stage"}  # only a timeout names the stage that ran out of time
    assert set(job["error"]) == {"code", "message", "retryable"} | extra
    return job["error"]


# --------------------------------------------------------------------------
# Gemini: quiet retries, then the error
# --------------------------------------------------------------------------
def test_the_outer_delays_are_one_constant():
    assert settings.GEMINI_OUTER_RETRY_DELAYS == DELAYS


def test_two_temporary_failures_then_success_is_done(make_env, sleeps):
    provider = FakeProvider(failures=[LLMCallError("Gemini returned HTTP 503."), LLMCallError("Gemini returned HTTP 503.")])
    env = make_env(provider=provider)
    job = env.run()
    assert job["state"] == "done" and job["error"] is None
    assert sleeps == [5, 10]  # exactly the first two outer delays
    assert provider.calls == 4  # 3 JD attempts + 1 CV call


@pytest.mark.parametrize("message", ["Gemini returned HTTP 500.", "Gemini returned HTTP 502.", "Gemini returned HTTP 504.",
                                     "Network problem while calling Gemini (ConnectionError)."])
def test_other_temporary_failures_are_retried_too(make_env, sleeps, message):
    env = make_env(provider=FakeProvider(failures=[LLMCallError(message)]))
    assert env.run()["state"] == "done" and sleeps == [5]


def test_the_user_sees_nothing_while_it_retries(make_env, monkeypatch):
    env = make_env(provider=FakeProvider(failures=[LLMCallError("Gemini returned HTTP 503.")]))
    seen = []

    def watch(seconds):
        job = next(iter(env.manager._jobs.values()))
        seen.append((job.state, job.stage, job.error))

    monkeypatch.setattr(retry, "_sleep", watch)
    env.run()
    assert seen == [("running", "Reading the job description", None)]


def test_a_service_that_stays_busy_ends_as_ai_busy(make_env, sleeps):
    provider = FakeProvider(always=LLMCallError("Gemini returned HTTP 503."))
    env = make_env(provider=provider)
    error = failed_with(env.run(), "ai_busy", True)
    assert error["message"] == "The AI service is busy right now. Please try again."
    assert sleeps == [5, 10, 20]  # exactly 3 outer retries
    assert provider.calls == 4


def test_a_busy_service_on_the_cv_call_is_the_same_error(make_env, sleeps):
    env = make_env(provider=FakeProvider(cv_error=LLMCallError("Gemini returned HTTP 502.")))
    failed_with(env.run(), "ai_busy", True)
    assert sleeps == [5, 10, 20]


def test_http_429_is_not_retried(make_env, sleeps):
    provider = FakeProvider(always=LLMCallError("Gemini rate limit reached (HTTP 429). Wait a while and try again."))
    env = make_env(provider=provider)
    error = failed_with(env.run(), "ai_rate_limited", True)
    assert error["message"] == "The AI service's request limit was reached. Wait a little while and try again."
    assert sleeps == [] and provider.calls == 1


@pytest.mark.parametrize("message", [
    "Gemini rejected the API key (HTTP 401). Check GEMINI_API_KEY.",
    "Gemini rejected the API key (HTTP 403). Check GEMINI_API_KEY.",
])
def test_key_and_model_problems_are_ai_not_configured(make_env, sleeps, message):
    provider = FakeProvider(always=LLMCallError(message))
    env = make_env(provider=provider)
    job = env.run()
    error = failed_with(job, "ai_not_configured", False)
    assert error["message"] == "The AI service is not set up on this server. Check the server configuration."
    assert "GEMINI" not in str(job) and "key" not in error["message"].lower().replace("configuration", "")
    assert sleeps == [] and provider.calls == 1


def test_a_missing_key_is_ai_not_configured_and_never_shows_key_text(make_env):
    def no_key(model=None):
        raise LLMConfigError("GEMINI_API_KEY is not set. Put GEMINI_API_KEY=your_key in .env")

    job = make_env(provider_factory=no_key).run()
    failed_with(job, "ai_not_configured", False)
    assert "GEMINI_API_KEY" not in str(job) and "your_key" not in str(job)


def test_a_config_error_raised_during_a_call_is_the_same(make_env):
    failed_with(make_env(provider=FakeProvider(always=LLMConfigError("GEMINI_API_KEY"))).run(),
                "ai_not_configured", False)


def test_an_unclassified_gemini_problem_is_an_internal_error(make_env, sleeps):
    env = make_env(provider=FakeProvider(always=LLMCallError("Gemini's answer was not valid JSON.")))
    failed_with(env.run(), "internal_error", True)
    assert sleeps == []


# --------------------------------------------------------------------------
# GitHub: plain errors, no retries, no fallback
# --------------------------------------------------------------------------
def test_user_not_found(make_env, sleeps):
    env = make_env(validator=FakeValidator(result={"status": "user_not_found", "message": "x"}))
    error = failed_with(env.run(), "github_user_not_found", False)
    assert error["message"] == "That GitHub username was not found. Check the spelling and try again."
    assert env.validator.calls == ["octocat"] and env.scanner.calls == 0 and sleeps == []


@pytest.mark.parametrize("result", [
    {"status": "rate_limited", "message": "limit"},
    {"status": "error", "message": "boom"},
    {"status": "something_new"},
    "not a dict",
])
def test_github_unavailable(make_env, sleeps, result):
    validator = FakeValidator(result=result) if isinstance(result, dict) else FakeValidator()
    if not isinstance(result, dict):
        validator.validate = lambda username: result
    env = make_env(validator=validator)
    error = failed_with(env.run(), "github_unavailable", True)
    assert error["message"] == ("GitHub could not be reached or its request limit was reached. "
                                "Wait a few minutes and try again.")
    assert env.scanner.calls == 0 and sleeps == []  # no retries, no fallback


def test_a_validator_that_raises_is_github_unavailable(make_env):
    validator = FakeValidator(raises=RuntimeError("token ghp_abc at C:\\secret"))
    job = make_env(validator=validator).run()
    failed_with(job, "github_unavailable", True)
    assert "ghp_abc" not in str(job) and "secret" not in str(job) and len(validator.calls) == 1


@pytest.mark.parametrize("scanner", [
    FakeScanner(raises=RuntimeError("boom")),
    FakeScanner(tech={"status": "error", "message": "rate limit"}),
    FakeScanner(tech={"status": "skipped"}),
    FakeScanner(tech="not a dict"),
])
def test_a_failed_scan_is_github_unavailable_and_no_score_is_returned(make_env, scanner):
    env = make_env(scanner=scanner)
    failed_with(env.run(), "github_unavailable", True)
    assert len(env.validator.calls) == 1


def test_a_partial_scan_is_not_a_failure(make_env):
    partial = {"status": "partial", "complete": False, "repos_scanned": 2, "skills": [scan_item("Python")],
               "incomplete_reasons": ["rate_limited"]}
    job = make_env(scanner=FakeScanner(tech=partial)).run()
    assert job["state"] == "done" and job["result"]["github"]["scan_complete"] is False


def test_no_public_repos_goes_to_the_score_unchanged(make_env):
    env = make_env(validator=FakeValidator(result={"status": "no_public_repos", "username": "octocat"}))
    job = env.run()
    assert job["state"] == "done" and job["result"]["status"] == "no_github_evidence"
    assert env.scanner.calls == 0


def test_a_github_token_problem_is_an_internal_error(make_env):
    def no_token():
        raise GitHubConfigError("GITHUB_TOKEN is not set ... your_token")

    job = make_env(validator_factory=no_token).run()
    failed_with(job, "internal_error", True)
    assert "GITHUB_TOKEN" not in str(job) and "your_token" not in str(job)


# --------------------------------------------------------------------------
# parser problems and unreadable files
# --------------------------------------------------------------------------
def test_a_jd_the_parser_cannot_use_is_jd_unreadable(make_env):
    error = failed_with(make_env(provider=FakeProvider(jd={"nothing": []})).run(), "jd_unreadable", False)
    assert error["message"].startswith("The job description could not be used.") and "'skills' list" in error["message"]


def test_a_cv_the_parser_cannot_use_is_cv_unreadable(make_env):
    error = failed_with(make_env(provider=FakeProvider(cv={"nothing": []})).run(), "cv_unreadable", False)
    assert error["message"].startswith("The CV could not be used.")


def test_an_unreadable_cv_file_is_cv_unreadable(env):
    error = failed_with(env.run(cv_file=("cv.pdf", b"this is not a pdf")), "cv_unreadable", False)
    assert error["message"]  # the extractor's own friendly message
    assert env.provider.calls == 1  # the JD was read, the CV never reached Gemini


def test_an_unreadable_jd_file_is_jd_unreadable(env):
    failed_with(env.run(jd_text=None, jd_file=("jd.docx", b"not a docx")), "jd_unreadable", False)
    assert env.provider.calls == 0


def test_a_cv_file_with_no_text_is_cv_unreadable(env):
    failed_with(env.run(cv_file=("cv.txt", b"   \n\n  ")), "cv_unreadable", False)


# --------------------------------------------------------------------------
# anything unexpected
# --------------------------------------------------------------------------
def test_an_unexpected_exception_never_leaks(make_env):
    provider = FakeProvider(always=RuntimeError("secret-key-xyz in C:\\Users\\someone\\x.py Traceback"))
    job = make_env(provider=provider).run()
    error = failed_with(job, "internal_error", True)
    assert error["message"] == "Something went wrong on the server. Please try again."
    for text in ("secret-key", "Users", "Traceback", "RuntimeError"):
        assert text not in str(job)


def test_score_problems_are_internal_errors(make_env, monkeypatch):
    def broken(*args, **kwargs):
        raise ScoringConfigError("weights")

    monkeypatch.setattr(pipeline, "score_candidate", broken)
    failed_with(make_env().run(), "internal_error", True)
    monkeypatch.setattr(pipeline, "score_candidate", lambda *a, **k: {"status": "invalid_input"})
    failed_with(make_env().run(), "internal_error", True)


def test_a_non_dict_parser_result_is_an_internal_error():
    with pytest.raises(errors.PipelineError) as caught:
        pipeline._check_parsed(None, errors.JD_UNREADABLE, "job description")
    assert caught.value.error["code"] == "internal_error"


# --------------------------------------------------------------------------
# the overall timeout
# --------------------------------------------------------------------------
def test_a_job_that_takes_too_long_fails_with_timeout_and_late_work_is_ignored(make_env, monkeypatch):
    monkeypatch.setattr(settings, "analysis_timeout_seconds", lambda: 0.2)
    gate = threading.Event()
    env = make_env(provider=FakeProvider(gate=gate))
    job = env.run()
    error = failed_with(job, "timeout", True)
    assert "too long" in error["message"]
    gate.set()  # the abandoned run finishes later: it must not change the job
    time.sleep(0.3)
    assert env.client.request("GET", f"/api/analyses/{job['id']}").json() == job
