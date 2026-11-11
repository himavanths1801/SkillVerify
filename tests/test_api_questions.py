"""The question routes: start, status, retry, replace. Fake Gemini, fake GitHub, fake clock; nothing is real."""
import json
import logging
import threading
import time

import pytest
from test_api_support import (CHAIN, CV_ANSWER, CV_TEXT, FakeProvider, FakeScanner, FakeValidator, JD_TEXT, github_ok,
                              make_env, rescored, scan_item)
from test_questions_support import (NOTEBOOK, PY_ORDERS, SQL_REPORT, FakeFiles, good_answer, tree_entry)

from api import errors, jobs, retry, settings
from api import questions as api_questions
from api.models import ModelChain
from skillverify.jd.provider import LLMCallError, LLMConfigError
from skillverify.scoring import score_candidate
from skillverify_questions import github_files, service, writer

SECRET = "SECRET-KEY-VALUE-123"
JD = {"skills": [
    {"as_written": "Python", "name": "Python", "signal": "required", "evidence": "Python"},
    {"as_written": "SQL", "name": "SQL", "signal": "required", "evidence": "SQL"},
    {"as_written": "Pandas", "name": "Pandas", "signal": "preferred", "evidence": "Pandas"},
    {"as_written": "Docker", "name": "Docker", "signal": "preferred", "evidence": "Docker"},
]}
CV = {**CV_ANSWER, "skills": [{"as_written": n, "name": n, "context": "skills_section", "project": None,
                               "evidence": n} for n in ("Python", "SQL", "Pandas")]}
JD_TEXT2 = "Data Analyst\nRequired: Python and SQL. Nice to have: Pandas and Docker.\n"
PY_ETL = PY_ORDERS.replace("clean_orders", "clean_rows").replace("OrderReport", "RowReport")
SYMBOLS = {"analysis.py": "clean_orders", "etl.py": "clean_rows", "sql/report.sql": "monthly_revenue",
           "notebooks/explore.ipynb": "simulate"}


def repo(name, pushed):
    return {"name": name, "url": "u", "is_fork": False, "counted": True, "primary_language": "Python",
            "languages": {"Python": 9000}, "last_commit_date": pushed}


def validation():
    return {**github_ok(), "repos": [repo("orders-analysis", "2026-09-10T10:00:00Z")]}


def scan():
    skills = [{**scan_item(name), "repos": ["orders-analysis"]} for name in ("Python", "SQL", "Pandas")]
    return {"status": "ok", "complete": True, "repos_scanned": 1, "skills": skills,
            "per_repo": [{"name": "orders-analysis", "default_branch": "main"}]}


def shop_files(**kwargs):
    trees = {"orders-analysis": [tree_entry("analysis.py", 1600), tree_entry("etl.py", 1500),
                                 tree_entry("sql/report.sql", 800), tree_entry("notebooks/explore.ipynb", 3000),
                                 tree_entry("README.md", 900)]}
    contents = {("orders-analysis", "analysis.py"): PY_ORDERS, ("orders-analysis", "etl.py"): PY_ETL,
                ("orders-analysis", "sql/report.sql"): SQL_REPORT, ("orders-analysis", "notebooks/explore.ipynb"): NOTEBOOK}
    return FakeFiles(trees, contents, **kwargs)


class QuestionProvider(FakeProvider):
    """Answers the JD and CV calls like the analysis fake, and the question writer calls by chosen path."""

    def __init__(self, answers=None, question_error=None, **kwargs):
        super().__init__(jd=JD, cv=CV, **kwargs)
        self.answers, self.question_error, self.question_calls = answers or {}, question_error, 0
        self.question_gate = None  # holds only the question calls, never the analysis calls

    def generate_json(self, system_prompt, user_prompt):
        if system_prompt != writer.SYSTEM_PROMPT:
            return super().generate_json(system_prompt, user_prompt)
        self.question_calls += 1
        if self.question_gate is not None:
            self.question_gate.wait(10)
        if self.question_error is not None:
            raise self.question_error
        path = next(line.split(": ", 1)[1] for line in user_prompt.splitlines() if line.startswith("Chosen file path:"))
        if path in self.answers:
            return self.answers[path]
        name = SYMBOLS.get(path, "the code")
        return good_answer(path, [SYMBOLS[path]] if path in SYMBOLS else [], question=(
            f"Open {path.rsplit('/', 1)[-1]} and look at {name}. Extend it so it handles one more case, then "
            "explain how you would check the change."))


class World:
    def __init__(self, env, fleet, files, clock, chain):
        self.env, self.fleet, self.files, self.clock, self.chain = env, fleet, files, clock, chain
        self.client = env.client

    def analysis(self):
        job = self.env.run(jd_text=JD_TEXT2)
        assert job["state"] == "done" and job["result"]["status"] == "ok", job
        return job["id"]

    def call(self, method, job_id, suffix="", **kwargs):
        return self.client.request(method, f"/api/analyses/{job_id}{suffix}", **kwargs)

    def wait(self, job_id, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = self.call("GET", job_id, "/questions").json()
            if status["state"] in ("done", "failed"):
                return status
            time.sleep(0.01)
        raise AssertionError("questions not finished")

    def ask(self, job_id):
        response = self.call("POST", job_id, "/questions")
        assert response.status == 202, response.text
        return self.wait(job_id)


class Fleet:
    def __init__(self, **kwargs):
        self.providers = {name: QuestionProvider(**kwargs) for name in CHAIN}
        self.built = []

    def make(self, model):
        self.built.append(model)
        return self.providers[model]

    def question_calls(self):
        return {name: p.question_calls for name, p in self.providers.items()}


@pytest.fixture
def clock():
    class Clock:
        now = 1000.0

        def __call__(self):
            return self.now
    return Clock()


@pytest.fixture
def world(make_env, clock, monkeypatch):
    fleet, files = Fleet(), shop_files()
    chain = ModelChain(CHAIN, clock=clock)
    env = make_env(provider_factory=fleet.make, chain=chain, files_factory=lambda: files,
                   validator=FakeValidator(validation()), scanner=FakeScanner(scan()))
    return World(env, fleet, files, clock, chain)


# --------------------------------------------------------------------------
# the happy path
# --------------------------------------------------------------------------
def test_questions_are_idle_until_started_and_the_analysis_result_never_changes(world):
    job_id = world.analysis()
    before = json.dumps(world.call("GET", job_id).json(), sort_keys=True)
    idle = world.call("GET", job_id, "/questions").json()
    assert idle == {"id": job_id, "state": "idle", "stage": None, "result": None, "error": None,
                    "meta": {"model": None, "fallback_used": False}}
    started = world.call("POST", job_id, "/questions")
    assert started.status == 202
    body = started.json()
    assert set(body) == {"id", "state", "stage", "result", "error", "meta"} and body["id"] == job_id
    done = world.wait(job_id)
    assert done["state"] == "done" and done["stage"] == "Checking questions" and done["error"] is None
    assert json.dumps(world.call("GET", job_id).json(), sort_keys=True) == before  # byte for byte
    kept = world.env.manager.intermediates(job_id)
    result = world.call("GET", job_id).json()["result"]
    assert result == rescored(result, kept)
    assert "questions" not in world.call("GET", job_id).json()["result"]


def test_the_finished_run_has_the_documented_shape(world):
    result = world.ask(world.analysis())["result"]
    assert set(result) == {"schema_version", "status", "message", "generated_at", "limits", "questions",
                           "skipped_skills", "dropped", "counts"}
    assert result["dropped"] == []
    assert result["status"] == "ok" and result["message"] is None and result["limits"] == {"min": 3, "max": 5}
    assert [(q["why"]["skill"], q["why"]["path"]) for q in result["questions"]] == [
        ("Python", "analysis.py"), ("SQL", "sql/report.sql"), ("Pandas", "etl.py")]
    question = result["questions"][0]
    assert question["id"].startswith("q_") and len(question["id"]) == 10 and question["language"] == "python"
    assert question["why"]["tier"] == "must_have" and question["why"]["claimed"] is True
    assert result["skipped_skills"] == [{"skill": "Docker", "tier": "nice_to_have", "reason": "not_found_in_code"}]
    assert result["counts"] == {"candidates_skills": 3, "repos_considered": 1, "files_fetched": 4, "files_ranked": 4,
                                "questions": 3, "dropped": 0}
    assert world.files.calls["tree"] == 1 and world.files.calls["file"] == 4  # one tree, only the files needed


def test_the_run_uses_the_main_model_and_reports_it(world):
    status = world.ask(world.analysis())
    assert status["meta"] == {"model": "model-a", "fallback_used": False}
    assert world.fleet.question_calls() == {"model-a": 3, "model-b": 0, "model-c": 0}


# --------------------------------------------------------------------------
# 409s and 404s
# --------------------------------------------------------------------------
def test_questions_need_a_finished_analysis_with_a_usable_score(world, make_env):
    unknown = world.call("POST", "no-such-id", "/questions")
    assert unknown.status == 409 and unknown.json()["error"]["code"] == "analysis_not_ready"
    assert unknown.json()["error"] == {"code": "analysis_not_ready", "message": errors.MESSAGES["analysis_not_ready"],
                                       "retryable": False}
    gate = threading.Event()
    slow = make_env(provider=FakeProvider(gate=gate), files_factory=lambda: shop_files())
    running = slow.submit(jd_text=JD_TEXT2).json()["id"]
    assert slow.client.request("POST", f"/api/analyses/{running}/questions").status == 409
    gate.set()
    slow.wait(running)
    empty = make_env(validator=FakeValidator({"status": "no_public_repos", "username": "x", "repos": [],
                                              "message": ""}), files_factory=lambda: shop_files())
    job = empty.run(jd_text=JD_TEXT2)
    assert job["result"]["status"] == "no_github_evidence"
    refused = empty.client.request("POST", f"/api/analyses/{job['id']}/questions")
    assert refused.status == 409 and refused.json()["error"]["code"] == "analysis_not_ready"


def test_a_second_start_is_refused_whatever_state_the_first_is_in(world):
    gate = threading.Event()
    world.fleet.providers["model-a"].question_gate = gate
    job_id = world.analysis()
    first = world.call("POST", job_id, "/questions")
    assert first.status == 202
    again = world.call("POST", job_id, "/questions")
    assert again.status == 409 and again.json()["error"]["code"] == "questions_already_started"
    assert again.json()["error"]["retryable"] is False
    gate.set()
    world.wait(job_id)
    assert world.call("POST", job_id, "/questions").status == 409  # done: GET instead


def test_unknown_ids_give_404_for_get_retry_and_replace(world):
    for method, suffix in (("GET", "/questions"), ("POST", "/questions/retry"), ("POST", "/questions/replace")):
        response = world.call(method, "no-such-id", suffix)
        assert response.status == 404 and "not found" in response.json()["message"]


def test_the_question_job_disappears_with_its_analysis(world, monkeypatch):
    job_id = world.analysis()
    world.ask(job_id)
    monkeypatch.setattr(jobs, "_now", lambda: time.time() + settings.JOB_TTL_SECONDS + 5)
    assert world.call("GET", job_id, "/questions").status == 404
    assert job_id not in world.env.questions._jobs


# --------------------------------------------------------------------------
# failures and retry
# --------------------------------------------------------------------------
def test_gemini_outage_fails_with_ai_busy_and_retry_reuses_the_cached_selection(world, monkeypatch):
    monkeypatch.setattr(retry, "_sleep", lambda seconds: None)
    provider = world.fleet.providers["model-a"]
    job_id = world.analysis()
    provider.question_error = LLMCallError("Gemini returned HTTP 503.")
    failed = world.ask(job_id)
    assert failed["state"] == "failed" and failed["result"] is None and failed["error"]["code"] == "ai_busy"
    assert failed["error"]["retryable"] is True and "fallback_available" not in failed["error"]
    github_calls = dict(world.files.calls)
    assert github_calls["tree"] == 1
    provider.question_error = None
    retried = world.call("POST", job_id, "/questions/retry")
    assert retried.status == 202 and retried.json()["state"] in ("queued", "running")
    done = world.wait(job_id)
    assert done["state"] == "done" and done["result"]["status"] == "ok"
    assert world.files.calls == github_calls  # not one more GitHub call
    assert world.call("POST", job_id, "/questions/retry").status == 409  # done: nothing to retry


def test_retry_is_refused_unless_failed_and_retryable(world):
    job_id = world.analysis()
    assert world.call("POST", job_id, "/questions/retry").status == 409  # never started
    world.fleet.providers["model-a"].question_error = LLMCallError("Gemini rejected the API key (HTTP 401).")
    failed = world.ask(job_id)
    assert failed["error"]["code"] == "ai_not_configured" and failed["error"]["retryable"] is False
    refused = world.call("POST", job_id, "/questions/retry")
    assert refused.status == 409 and "retryable" in refused.json()["message"]


def test_a_429_marks_the_model_and_the_retry_uses_the_next_one(world):
    job_id = world.analysis()
    world.fleet.providers["model-a"].question_error = LLMCallError("Gemini rate limit reached (HTTP 429).")
    failed = world.ask(job_id)
    assert failed["error"] == {"code": "ai_rate_limited", "message": errors.MESSAGES["ai_rate_limited"],
                               "retryable": True, "fallback_available": True}
    assert failed["result"] is None and failed["meta"]["model"] == "model-a"
    assert [row["state"] for row in world.chain.snapshot()] == ["cooling_down", "ready", "ready"]
    world.call("POST", job_id, "/questions/retry")
    done = world.wait(job_id)
    assert done["state"] == "done" and done["meta"] == {"model": "model-b", "fallback_used": True}
    assert world.fleet.question_calls()["model-b"] == 3


def test_all_models_limited_fails_at_once_without_a_gemini_or_github_call(world):
    job_id = world.analysis()
    for name in CHAIN:
        world.chain.mark_rate_limited(name)
    before = world.fleet.question_calls(), dict(world.files.calls), list(world.fleet.built)
    failed = world.ask(job_id)
    assert failed["error"]["code"] == "ai_all_limited" and failed["error"]["retryable"] is True
    assert (world.fleet.question_calls(), dict(world.files.calls), list(world.fleet.built)) == before
    assert failed["meta"] == {"model": None, "fallback_used": False}
    world.clock.now += settings.RATE_LIMIT_COOLDOWN_SECONDS
    world.call("POST", job_id, "/questions/retry")
    assert world.wait(job_id)["state"] == "done"


def test_the_last_model_limited_says_no_backup_is_left(world):
    job_id = world.analysis()
    for name in CHAIN[:2]:
        world.chain.mark_rate_limited(name)
    world.fleet.providers["model-c"].question_error = LLMCallError("Gemini rate limit reached (HTTP 429).")
    assert world.ask(job_id)["error"]["fallback_available"] is False


def test_an_unknown_model_is_set_aside_and_the_next_one_used_within_the_run(world):
    job_id = world.analysis()
    world.fleet.providers["model-a"].question_error = LLMCallError(
        "Gemini model 'x' was not found (HTTP 404). Set GEMINI_MODEL.")
    done = world.ask(job_id)
    assert done["state"] == "done" and done["meta"] == {"model": "model-b", "fallback_used": True}
    assert [row["state"] for row in world.chain.snapshot()][0] == "unavailable"
    assert world.fleet.question_calls() == {"model-a": 1, "model-b": 3, "model-c": 0}


def test_when_every_model_is_unknown_the_run_fails_with_ai_all_limited(world):
    job_id = world.analysis()
    for provider in world.fleet.providers.values():
        provider.question_error = LLMCallError("Gemini model 'x' was not found (HTTP 404). Set GEMINI_MODEL.")
    failed = world.ask(job_id)
    assert failed["error"]["code"] == "ai_all_limited"
    assert world.fleet.question_calls() == {"model-a": 1, "model-b": 1, "model-c": 1}


def test_a_404_after_the_first_call_is_a_configuration_error_and_changes_nothing(world):
    job_id = world.analysis()
    provider = world.fleet.providers["model-a"]

    def late_404(path):
        if provider.question_calls > 1:
            raise LLMCallError("Gemini model 'x' was not found (HTTP 404).")
        return good_answer(path, [SYMBOLS[path]], question=(
            f"Open {path.rsplit('/', 1)[-1]} and look at {SYMBOLS[path]}. Extend it so it handles one more case."
            " Then explain how you would check the change."))
    provider.question_calls = 0
    original = provider.generate_json

    def generate(system_prompt, user_prompt):
        if system_prompt == writer.SYSTEM_PROMPT and provider.question_calls >= 1:
            provider.question_calls += 1
            raise LLMCallError("Gemini model 'x' was not found (HTTP 404).")
        return original(system_prompt, user_prompt)
    provider.generate_json = generate
    failed = world.ask(job_id)
    assert failed["error"]["code"] == "ai_not_configured"
    assert [row["state"] for row in world.chain.snapshot()] == ["ready"] * 3


def test_a_missing_gemini_key_is_ai_not_configured(make_env):
    def no_key(model):
        raise LLMConfigError("GEMINI_API_KEY is not set.")
    env = make_env(validator=FakeValidator(validation()), scanner=FakeScanner(scan()), files_factory=lambda: shop_files())
    done = env.run(jd_text=JD_TEXT2)
    env.manager._deps.provider_factory = no_key
    body = env.client.request("POST", f"/api/analyses/{done['id']}/questions")
    assert body.status == 202
    status = wait_questions(env, done["id"])
    assert status["error"]["code"] == "ai_not_configured" and "GEMINI" not in json.dumps(status)


def test_a_config_error_raised_during_a_call_is_ai_not_configured(world):
    job_id = world.analysis()
    world.fleet.providers["model-a"].question_error = LLMConfigError("GEMINI_API_KEY")
    status = world.ask(job_id)
    assert status["error"]["code"] == "ai_not_configured" and "GEMINI" not in json.dumps(status)


def wait_questions(env, job_id, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = env.client.request("GET", f"/api/analyses/{job_id}/questions").json()
        if status["state"] in ("done", "failed"):
            return status
        time.sleep(0.01)
    raise AssertionError("not finished")


def test_github_failures_are_github_unavailable_and_a_retry_reads_github_again(world):
    job_id = world.analysis()
    world.files.error = github_files.GitHubFilesError(github_files.RATE_LIMITED, "GitHub's request limit was reached.")
    failed = world.ask(job_id)
    assert failed["error"]["code"] == "github_unavailable" and failed["error"]["retryable"] is True
    assert world.fleet.question_calls() == {"model-a": 0, "model-b": 0, "model-c": 0}
    assert world.files.calls["tree"] == 1  # one attempt, no retries
    world.files.error = None
    world.call("POST", job_id, "/questions/retry")
    assert world.wait(job_id)["state"] == "done" and world.files.calls["tree"] == 2


def test_a_github_token_that_is_not_set_is_an_internal_error(make_env):
    def no_token():
        raise github_files.GitHubFilesError(github_files.NOT_CONFIGURED, "GITHUB_TOKEN is not set.")
    env = make_env(validator=FakeValidator(validation()), scanner=FakeScanner(scan()), files_factory=no_token,
                   provider=QuestionProvider())
    job = env.run(jd_text=JD_TEXT2)
    env.client.request("POST", f"/api/analyses/{job['id']}/questions")
    status = wait_questions(env, job["id"])
    assert status["error"]["code"] == "internal_error" and "GITHUB_TOKEN" not in json.dumps(status)


def test_unexpected_errors_become_a_fixed_internal_error_without_any_text(world):
    job_id = world.analysis()
    world.files.error = RuntimeError("secret detail: " + SECRET)
    failed = world.ask(job_id)
    assert failed["error"] == {"code": "internal_error", "message": errors.MESSAGES["internal_error"],
                               "retryable": True}
    assert SECRET not in json.dumps(failed)


def test_a_stuck_run_times_out(world, monkeypatch):
    monkeypatch.setattr(settings, "questions_timeout_seconds", lambda: 0.2)
    gate = threading.Event()
    world.fleet.providers["model-a"].question_gate = gate
    job_id = world.analysis()
    failed = world.ask(job_id)
    assert failed["error"]["code"] == "timeout" and failed["error"]["retryable"] is True
    assert failed["error"]["stage"] == "Writing questions"  # the stage that ran out of time
    gate.set()
    time.sleep(0.1)
    assert world.call("GET", job_id, "/questions").json()["state"] == "failed"  # the late result is ignored


def test_too_few_questions_are_reported_honestly(make_env):
    files = shop_files()
    files.trees["orders-analysis"] = [tree_entry("analysis.py", 1600)]
    env = make_env(provider=QuestionProvider(), validator=FakeValidator(validation()), scanner=FakeScanner(scan()),
                   files_factory=lambda: files)
    job = env.run(jd_text=JD_TEXT2)
    env.client.request("POST", f"/api/analyses/{job['id']}/questions")
    result = wait_questions(env, job["id"])["result"]
    assert result["status"] == "too_few" and len(result["questions"]) == 1
    assert result["message"] == ("Only 1 question(s) could be written from the candidate's code. "
                                 "A session needs at least 3.")


# --------------------------------------------------------------------------
# replace
# --------------------------------------------------------------------------
def test_replace_writes_one_question_from_the_next_unused_file_then_runs_out(world):
    job_id = world.analysis()
    run = world.ask(job_id)["result"]
    used = {q["why"]["path"] for q in run["questions"]}
    response = world.call("POST", job_id, "/questions/replace", raw_body=json.dumps({"after": "q_12345678"}).encode(),
                          content_type="application/json")
    assert response.status == 200, response.text
    body = response.json()
    assert set(body) == {"question", "skipped_skills", "dropped"} and body["dropped"] == []
    assert body["question"]["why"]["path"] == "notebooks/explore.ipynb" not in used
    assert body["question"]["id"].startswith("q_") and body["question"]["why"]["skill"] == "Python"
    empty = world.call("POST", job_id, "/questions/replace")
    assert empty.status == 409 and empty.json()["error"] == {
        "code": "no_replacement_available", "message": "There is no other code in the candidate's repos to ask about.",
        "retryable": False}


def test_replace_needs_finished_questions_and_a_valid_body(world):
    job_id = world.analysis()
    early = world.call("POST", job_id, "/questions/replace")
    assert early.status == 409 and early.json()["error"]["code"] == "analysis_not_ready"
    world.ask(job_id)
    for raw in (b"not json", b"[1]", json.dumps({"after": 5}).encode(), json.dumps({"after": "x" * 65}).encode()):
        bad = world.call("POST", job_id, "/questions/replace", raw_body=raw, content_type="application/json")
        assert bad.status == 422 and bad.json()["error"]["code"] == "invalid_input"
    assert world.call("POST", job_id, "/questions/replace", raw_body=b"{}", content_type="application/json").status == 200


def test_replace_429_marks_the_model_and_reports_the_error(world):
    job_id = world.analysis()
    world.ask(job_id)
    world.fleet.providers["model-a"].question_error = LLMCallError("Gemini rate limit reached (HTTP 429).")
    response = world.call("POST", job_id, "/questions/replace")
    assert response.status == 503
    assert response.json()["error"] == {"code": "ai_rate_limited", "message": errors.MESSAGES["ai_rate_limited"],
                                        "retryable": True, "fallback_available": True}
    assert [row["state"] for row in world.chain.snapshot()][0] == "cooling_down"
    world.fleet.providers["model-a"].question_error = None
    assert world.call("POST", job_id, "/questions/replace").status == 200  # now model-b writes it


def test_replace_with_every_model_resting_makes_no_gemini_call(world):
    job_id = world.analysis()
    world.ask(job_id)
    for name in CHAIN:
        world.chain.mark_rate_limited(name)
    calls = world.fleet.question_calls()
    response = world.call("POST", job_id, "/questions/replace")
    assert response.status == 503 and response.json()["error"]["code"] == "ai_all_limited"
    assert world.fleet.question_calls() == calls


def test_two_replacements_at_once_are_refused(world):
    job_id = world.analysis()
    world.ask(job_id)
    job = world.env.questions._jobs[job_id]
    assert job.busy.acquire(blocking=False)
    try:
        busy = world.call("POST", job_id, "/questions/replace")
    finally:
        job.busy.release()
    assert busy.status == 409 and busy.json()["error"]["code"] == "questions_already_started"


# --------------------------------------------------------------------------
# privacy
# --------------------------------------------------------------------------
def test_no_response_or_log_line_contains_file_text_question_text_or_key_material(world, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    monkeypatch.setenv("GITHUB_TOKEN", SECRET)
    job_id = world.analysis()
    status = world.ask(job_id)
    world.call("POST", job_id, "/questions/replace")
    health = world.client.request("GET", "/api/health").text
    texts = [r.getMessage() for r in caplog.records] + [health]
    assert texts, "the run should have logged something (ids and stages)"
    sensitive = ["clean_orders", "monthly_revenue", "simulate", "Extend it so it handles", "import pandas",
                 "SELECT", SECRET] + [q["question"] for q in status["result"]["questions"]]
    for text in texts:
        assert not any(word in text for word in sensitive), text
    assert any("questions" in text and job_id in text for text in texts)  # ids and stages only
    assert SECRET not in json.dumps(status) and SECRET not in health


def test_the_student_visible_fields_are_separate_from_the_recruiter_only_ones(world):
    question = world.ask(world.analysis())["result"]["questions"][0]
    student = {k: question[k] for k in ("question", "language", "starter_code")}
    recruiter_only = {"why", "reference_notes"}
    assert not recruiter_only & set(student) and recruiter_only <= set(question)
    assert api_questions.QuestionError(409, {"code": "x"}).status == 409
    assert service.SCHEMA_VERSION == 1


def test_a_stale_or_unknown_run_does_nothing(world):
    job_id = world.analysis()
    world.ask(job_id)
    before = world.call("GET", job_id, "/questions").json()
    world.env.questions._execute("no-such-id", 0)  # unknown
    world.env.questions._execute(job_id, 7)        # not the current attempt
    world.env.questions._execute(job_id, 0)        # no longer queued
    assert world.call("GET", job_id, "/questions").json() == before


def test_a_failed_replacement_reports_its_dropped_attempts_with_codes_only(world):
    job_id = world.analysis()
    world.ask(job_id)
    bad = good_answer("notebooks/explore.ipynb", ["simulate"], file="somewhere-else.py", reference_notes=SECRET)
    for provider in world.fleet.providers.values():
        provider.answers["notebooks/explore.ipynb"] = bad
    response = world.call("POST", job_id, "/questions/replace")
    assert response.status == 409
    error = response.json()["error"]
    assert error["code"] == "no_replacement_available"
    assert error["dropped"] == [{"skill": "Python", "tier": "must_have", "repo": "orders-analysis",
                                 "path": "notebooks/explore.ipynb", "attempts": 2, "reasons": ["wrong_file"]}]
    assert SECRET not in response.text
