"""API routes: health, validation, the happy path, the retry route, CORS and privacy. Offline, fakes only."""
import asyncio
import threading
import time

import pytest
from test_api_support import (CV_TEXT, FakeProvider, FakeValidator, JD_TEXT, env, github_ok, make_env,  # noqa: F401
                              rescored, settings)

import api.app
from api import pipeline
from skillverify.scoring import score_candidate

MB = 1024 * 1024


# --------------------------------------------------------------------------
# health
# --------------------------------------------------------------------------
def test_health_reports_booleans_only(env, monkeypatch):
    monkeypatch.setattr(api.app, "load_dotenv", lambda: None)
    monkeypatch.setenv("GEMINI_API_KEY", "sk-secret-value-123")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secrettoken456")
    response = env.client.request("GET", "/api/health")
    assert response.status == 200
    body = response.json()
    assert {k: body[k] for k in ("status", "gemini_key_set", "github_token_set")} == {
        "status": "ok", "gemini_key_set": True, "github_token_set": True}
    assert "secret" not in response.text


def test_health_when_nothing_is_set(env, monkeypatch):
    monkeypatch.setattr(api.app, "load_dotenv", lambda: None)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "   ")
    body = env.client.request("GET", "/api/health").json()
    assert {k: body[k] for k in ("status", "gemini_key_set", "github_token_set")} == {
        "status": "ok", "gemini_key_set": False, "github_token_set": False}


# --------------------------------------------------------------------------
# the happy path
# --------------------------------------------------------------------------
def test_happy_path_result_is_exactly_score_candidate(env):
    response = env.submit()
    assert response.status == 202
    job_id = response.json()["id"]
    assert set(response.json()) == {"id"}
    job = env.wait(job_id)
    assert set(job) == {"id", "state", "stage", "result", "error", "meta"}  # the intermediates are NOT returned
    assert (job["id"], job["state"], job["stage"], job["error"]) == (job_id, "done", "Done", None)

    kept = env.manager.intermediates(job_id)  # kept in memory for the later question generator
    assert set(kept) == {"jd", "cv", "github"}
    assert job["result"] == rescored(job["result"], kept)
    assert job["result"]["status"] == "ok"
    assert job["result"]["score"]["repo_fingerprint"] == 64.3
    assert job["result"]["job_name"] == "job" and job["result"]["cv_label"] == "cv_priya"
    assert env.validator.calls == ["octocat"]  # the username came from the CV
    assert env.scanner.calls == 1


def test_stages_queued_then_running_then_done(make_env, monkeypatch):
    monkeypatch.setattr(settings, "MAX_WORKERS", 1)
    gate = threading.Event()
    env = make_env(provider=FakeProvider(gate=gate))
    first = env.submit().json()["id"]
    second = env.submit().json()["id"]
    deadline = time.time() + 5
    while time.time() < deadline and env.client.request("GET", f"/api/analyses/{first}").json()["state"] != "running":
        time.sleep(0.01)
    running = env.client.request("GET", f"/api/analyses/{first}").json()
    waiting = env.client.request("GET", f"/api/analyses/{second}").json()
    assert (running["state"], running["stage"]) == ("running", "Reading the job description")
    assert (waiting["state"], waiting["stage"]) == ("queued", "Analyzing, this can take a moment.")
    assert running["result"] is None and running["error"] is None
    gate.set()
    assert env.wait(first)["state"] == "done"
    assert env.wait(second)["state"] == "done"


def test_the_stage_follows_the_pipeline(make_env):
    seen = []
    env = make_env()
    original = pipeline.run_pipeline

    def recording(inputs, deps, set_stage, set_meta=None):
        return original(inputs, deps, lambda text: (seen.append(text), set_stage(text)), set_meta)

    env.manager._runner = recording
    assert env.run()["state"] == "done"
    assert seen == ["Reading the job description", "Reading the CV", "Checking GitHub", "Scanning repositories",
                    "Scoring"]


def test_a_jd_file_and_a_typed_username_and_job_name(env):
    job = env.run(jd_text=None, jd_file=("data_analyst.txt", JD_TEXT.encode()), github_username="  torvalds ",
                  job_name="my_job")
    assert job["state"] == "done"
    assert env.validator.calls == ["torvalds"]  # the typed username wins over the CV's
    assert job["result"]["job_name"] == "my_job"
    assert "github_username_mismatch" in job["result"]["flags"]  # a human chose it; the score says so
    assert job["result"]["status"] == "ok"


def test_the_job_name_defaults_to_the_jd_file_name(env):
    job = env.run(jd_text=None, jd_file=("My JD (final).txt", JD_TEXT.encode()))
    assert job["result"]["job_name"] == "My_JD__final_"


def test_an_unusable_jd_file_name_falls_back_to_job(env):
    assert env.run(jd_text=None, jd_file=("...txt", JD_TEXT.encode()))["result"]["job_name"] == "job"


def test_an_empty_jd_text_field_is_ignored_when_a_file_is_given(env):
    assert env.run(jd_text="", jd_file=("jd.txt", JD_TEXT.encode()))["state"] == "done"


def test_a_directory_in_the_file_name_is_never_used(env):
    job = env.run(cv_file=("..\\..\\evil/cv one.txt", CV_TEXT.encode()))
    assert job["result"]["cv_label"] == "cv_one"


def test_without_any_github_username_the_score_still_runs(env):
    job = env.run(cv_file=("cv.txt", b"Priya Sample\nSkills: Python, SQL, Pandas\n"))
    assert job["state"] == "done"
    assert env.validator.calls == []
    assert job["result"]["status"] == "no_github_evidence"  # exactly what score_candidate reports


# --------------------------------------------------------------------------
# validation (422, plain messages)
# --------------------------------------------------------------------------
def invalid(response, fragment):
    assert response.status == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_input" and error["retryable"] is False
    assert fragment in error["message"]
    assert "Traceback" not in response.text


@pytest.mark.parametrize("overrides, fragment", [
    ({"jd_file": ("jd.txt", b"Python")}, "not both"),
    ({"jd_text": None}, "as text or as a file"),
    ({"jd_text": "   \n"}, "text is empty"),
    ({"cv_file": None}, "CV file is required"),
    ({"cv_file": ("cv.exe", b"x")}, "CV file must be a .pdf, .docx or .txt"),
    ({"cv_file": ("cv", b"x")}, "CV file must be a .pdf, .docx or .txt"),
    ({"cv_file": ("cv.txt", b"")}, "CV file is empty"),
    ({"cv_file": ("cv.txt", b"x" * (5 * MB + 1))}, "larger than 5 MB"),
    ({"jd_text": None, "jd_file": ("jd.doc", b"x")}, "job description file must be"),
    ({"jd_text": None, "jd_file": ("jd.txt", b"x" * (5 * MB + 1))}, "larger than 5 MB"),
    ({"jd_text": "x" * (MB + 1)}, "at most 1 MB"),
    ({"github_username": "not a user!"}, "GitHub username"),
    ({"github_username": "-leading"}, "GitHub username"),
    ({"job_name": "bad name/../"}, "job name"),
])
def test_validation_errors(env, overrides, fragment):
    invalid(env.submit(**overrides), fragment)
    assert env.manager._jobs == {}  # nothing was created


def test_a_blank_file_part_is_a_plain_422(env):
    response = env.submit(cv_file=("", b"x"))
    assert response.status == 422 and "Traceback" not in response.text


def test_a_broken_multipart_body_is_a_plain_error(env):
    response = env.client.request("POST", "/api/analyses", raw_body=b"garbage", content_type="multipart/form-data; boundary=x")
    invalid(response, "could not be read")


def test_a_file_field_sent_as_plain_text_is_a_plain_422(env):
    response = env.client.request("POST", "/api/analyses", {"jd_text": JD_TEXT, "cv_file": "just text"})
    invalid(response, "could not be understood")


def test_other_http_errors_stay_plain(env):
    response = env.client.request("GET", "/api/nothing-here")
    assert response.status == 404 and "Traceback" not in response.text
    assert env.client.request("DELETE", "/api/health").status == 405


def test_a_request_without_a_form_is_a_plain_422(env):
    response = env.client.request("POST", "/api/analyses", raw_body=b"{}", content_type="application/json")
    assert response.status == 422 and "Traceback" not in response.text


# --------------------------------------------------------------------------
# job lookup
# --------------------------------------------------------------------------
def test_an_unknown_job_is_a_plain_404(env):
    response = env.client.request("GET", "/api/analyses/does-not-exist")
    assert response.status == 404 and "not found" in response.json()["message"]
    assert env.client.request("POST", "/api/analyses/does-not-exist/retry").status == 404


# --------------------------------------------------------------------------
# the retry route
# --------------------------------------------------------------------------
def test_retry_reruns_a_retryable_failure_from_the_stored_inputs(env):
    env.validator.result = {"status": "rate_limited", "message": "limit"}
    job = env.run(github_username="octocat")
    assert job["state"] == "failed" and job["error"]["code"] == "github_unavailable"
    assert env.validator.calls == ["octocat"]

    env.validator.result = github_ok()  # GitHub is back
    response = env.client.request("POST", f"/api/analyses/{job['id']}/retry")
    assert response.status == 202 and response.json() == {"id": job["id"]}
    again = env.wait(job["id"])
    assert again["state"] == "done" and again["error"] is None and again["result"]["status"] == "ok"
    assert env.validator.calls == ["octocat", "octocat"]  # the same stored inputs were used again


def test_retry_is_refused_for_everything_else(env):
    done = env.run()
    assert env.client.request("POST", f"/api/analyses/{done['id']}/retry").status == 409

    env.validator.result = {"status": "user_not_found", "message": "no"}
    not_retryable = env.run()
    assert not_retryable["error"]["retryable"] is False
    response = env.client.request("POST", f"/api/analyses/{not_retryable['id']}/retry")
    assert response.status == 409 and "retryable" in response.json()["message"]


def test_retry_is_refused_while_a_job_is_still_running(make_env):
    gate = threading.Event()
    env = make_env(provider=FakeProvider(gate=gate))
    job_id = env.submit().json()["id"]
    assert env.client.request("POST", f"/api/analyses/{job_id}/retry").status == 409
    gate.set()
    env.wait(job_id)


# --------------------------------------------------------------------------
# a full store
# --------------------------------------------------------------------------
def test_a_full_store_of_active_jobs_answers_503(make_env, monkeypatch):
    monkeypatch.setattr(settings, "MAX_JOBS", 2)
    monkeypatch.setattr(settings, "MAX_WORKERS", 1)
    gate = threading.Event()
    env = make_env(provider=FakeProvider(gate=gate))
    ids = [env.submit().json()["id"] for _ in range(2)]
    response = env.submit()
    assert response.status == 503
    assert response.json()["error"]["code"] == "internal_error" and "Too many" in response.json()["error"]["message"]
    gate.set()
    for job_id in ids:
        env.wait(job_id)
    assert env.submit().status == 202  # room again: the oldest finished job is dropped


# --------------------------------------------------------------------------
# CORS and privacy
# --------------------------------------------------------------------------
@pytest.mark.parametrize("origin", ["http://localhost:5173", "http://127.0.0.1:5173"])
def test_cors_allows_the_two_dev_origins(env, origin):
    response = env.client.request("GET", "/api/health", headers={"Origin": origin})
    assert response.headers["access-control-allow-origin"] == origin
    assert "access-control-allow-credentials" not in response.headers


def test_cors_is_absent_for_other_origins(env):
    for origin in ("https://evil.example", "http://localhost:3000", "http://localhost:5173.evil.example"):
        response = env.client.request("GET", "/api/health", headers={"Origin": origin})
        assert "access-control-allow-origin" not in response.headers


def test_cors_preflight(env):
    allowed = env.client.request("OPTIONS", "/api/analyses", headers={
        "Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST"})
    assert allowed.status == 200 and allowed.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert set(allowed.headers["access-control-allow-methods"].replace(" ", "").split(",")) == {"GET", "POST"}
    refused = env.client.request("OPTIONS", "/api/analyses", headers={
        "Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in refused.headers


def test_an_unexpected_server_error_is_a_plain_500(env, monkeypatch):
    def boom(job_id):
        raise RuntimeError("secret-token-abc at C:\\Users\\someone\\file.py")

    monkeypatch.setattr(env.manager, "snapshot", boom)
    response = env.client.request("GET", "/api/analyses/x")
    assert response.status == 500
    assert response.json()["error"]["code"] == "internal_error"
    for text in ("secret-token", "Users", "Traceback", "RuntimeError"):
        assert text not in response.text


def test_the_lifespan_shuts_the_job_store_down(env):
    async def cycle():
        async with env.app.router.lifespan_context(env.app):
            pass

    asyncio.run(cycle())
    with pytest.raises(RuntimeError):  # the pool no longer accepts work
        env.manager._pool.submit(lambda: None)
