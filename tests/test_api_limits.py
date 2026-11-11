"""Time limits, the repository cap and stage timings. Fakes and a fake clock only; the real .env is never read."""
import logging
import re
import threading
import time

import pytest
from test_api_support import FakeValidator, github_ok, make_env, rescored  # noqa: F401 (make_env is a fixture)

from api import errors, jobs, pipeline, repocap, settings
from api.jobs import JobManager

DEFAULT_LIMITS = {"analysis_timeout_seconds": 360, "questions_timeout_seconds": 360, "scan_max_repos": 30}


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------
def test_defaults_when_nothing_is_set(caplog):
    caplog.set_level(logging.DEBUG)
    assert settings.load_limits({}) == DEFAULT_LIMITS
    assert settings.DEFAULT_ANALYSIS_TIMEOUT_SECONDS >= 300  # never lowered below the 300 s asked for
    assert [r for r in caplog.records if r.name == "skillverify.api"] == []


def test_valid_values_are_used_including_the_edges():
    env = {"ANALYSIS_TIMEOUT_SECONDS": " 30 ", "QUESTIONS_TIMEOUT_SECONDS": "1800", "SCAN_MAX_REPOS": "200"}
    assert settings.load_limits(env) == {"analysis_timeout_seconds": 30, "questions_timeout_seconds": 1800,
                                         "scan_max_repos": 200}
    assert settings.load_limits({"SCAN_MAX_REPOS": "5"})["scan_max_repos"] == 5


@pytest.mark.parametrize("variable,value", [
    ("ANALYSIS_TIMEOUT_SECONDS", "29"), ("ANALYSIS_TIMEOUT_SECONDS", "1801"), ("QUESTIONS_TIMEOUT_SECONDS", "0"),
    ("QUESTIONS_TIMEOUT_SECONDS", "-5"), ("SCAN_MAX_REPOS", "4"), ("SCAN_MAX_REPOS", "201"),
    ("SCAN_MAX_REPOS", "lots"), ("ANALYSIS_TIMEOUT_SECONDS", "3.5"), ("SCAN_MAX_REPOS", "1e2"),
])
def test_a_bad_value_falls_back_to_the_default_with_one_log_line_that_never_shows_it(variable, value, caplog):
    caplog.set_level(logging.DEBUG)
    limits = settings.load_limits({variable: value})
    assert limits == DEFAULT_LIMITS
    lines = [r.getMessage() for r in caplog.records if r.name == "skillverify.api"]
    low, high = (5, 200) if variable == "SCAN_MAX_REPOS" else (30, 1800)
    assert lines == [f"Ignored an invalid {variable} setting (allowed {low} to {high}); using the default."]


def test_limits_are_read_lazily_once_and_can_be_reset(monkeypatch):
    monkeypatch.setattr(settings, "_limits_cache", None)
    calls = []
    monkeypatch.setattr(settings, "load_dotenv", lambda: calls.append(1))  # the .env file is not opened
    monkeypatch.setenv("SCAN_MAX_REPOS", "12")
    monkeypatch.delenv("ANALYSIS_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("QUESTIONS_TIMEOUT_SECONDS", raising=False)
    assert settings.scan_max_repos() == 12 and settings.analysis_timeout_seconds() == 360
    assert settings.questions_timeout_seconds() == 360 and calls == [1]  # loaded once
    monkeypatch.setenv("SCAN_MAX_REPOS", "40")
    assert settings.scan_max_repos() == 12
    settings.reset_limits()
    assert settings.scan_max_repos() == 40 and calls == [1, 1]


def test_health_shows_the_limits_as_numbers_and_no_key_text(make_env, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "KEY-TEXT-123")
    monkeypatch.setenv("GITHUB_TOKEN", "TOKEN-TEXT-456")
    env = make_env()
    response = env.client.request("GET", "/api/health")
    body = response.json()
    assert body["limits"] == DEFAULT_LIMITS and all(isinstance(v, int) for v in body["limits"].values())
    assert "KEY-TEXT-123" not in response.text and "TOKEN-TEXT-456" not in response.text


# --------------------------------------------------------------------------
# stage timings and a timeout that names its stage
# --------------------------------------------------------------------------
class FakeClock:
    now = 100.0

    def __call__(self):
        return self.now


def make_inputs():
    return pipeline.JobInputs(jd_text="x", jd_file=None, cv_file=pipeline.UploadedFile(".txt", "cv", b"x"),
                              github_username=None, job_name="job")


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(jobs, "_clock", fake)
    monkeypatch.setattr(settings, "_limits_cache", settings.load_limits({}))
    return fake


def wait_for(manager, job_id, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = manager.snapshot(job_id)
        if snap["state"] in ("done", "failed"):
            return snap
        time.sleep(0.01)
    raise AssertionError("not finished")


def test_every_finished_stage_gets_a_time_and_one_log_line(clock, caplog):
    caplog.set_level(logging.INFO)

    def runner(inputs, deps, set_stage, set_meta=None):
        set_stage("Checking GitHub")
        clock.now += 5.04
        set_stage("Scanning repositories")
        clock.now += 41.26
        set_stage("Scoring")
        clock.now += 0.04
        return pipeline.PipelineOutput(result={"status": "ok"}, jd={}, cv={}, github=None)

    manager = JobManager(deps=object(), runner=runner)
    try:
        done = wait_for(manager, manager.create(make_inputs()))
    finally:
        manager.shutdown()
    assert done["state"] == "done"
    assert done["meta"]["timings"] == {"Checking GitHub": 5.0, "Scanning repositories": 41.3, "Scoring": 0.0}
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("stage=")]
    assert lines == ['stage="Checking GitHub" seconds=5.0', 'stage="Scanning repositories" seconds=41.3',
                     'stage="Scoring" seconds=0.0']


def test_a_slow_stage_times_out_at_the_limit_and_the_error_names_it(clock, monkeypatch):
    monkeypatch.setattr(settings, "analysis_timeout_seconds", lambda: 0.2)
    gate = threading.Event()

    def runner(inputs, deps, set_stage, set_meta=None):
        set_stage("Checking GitHub")
        clock.now += 12.0
        set_stage("Scanning repositories")
        clock.now += 300.04
        gate.wait(10)
        return pipeline.PipelineOutput(result={"status": "ok"}, jd={}, cv={}, github=None)

    manager = JobManager(deps=object(), runner=runner)
    try:
        failed = wait_for(manager, manager.create(make_inputs()))
        assert failed["error"] == {"code": "timeout", "message": errors.MESSAGES["timeout"], "retryable": True,
                                   "stage": "Scanning repositories"}
        assert failed["meta"]["timings"] == {"Checking GitHub": 12.0, "Scanning repositories": 300.0}
        gate.set()
        time.sleep(0.2)
        assert manager.snapshot(failed["id"]) == failed  # the abandoned run changes nothing
    finally:
        gate.set()
        manager.shutdown()


def test_a_timeout_before_any_stage_has_no_stage_field(clock, monkeypatch):
    monkeypatch.setattr(settings, "analysis_timeout_seconds", lambda: 0.1)
    gate = threading.Event()
    manager = JobManager(deps=object(), runner=lambda *a, **k: gate.wait(10))
    try:
        failed = wait_for(manager, manager.create(make_inputs()))
        assert failed["error"]["code"] == "timeout" and "stage" not in failed["error"]
        assert failed["meta"]["timings"] == {}
    finally:
        gate.set()
        manager.shutdown()


def test_retry_after_a_timeout_works_and_starts_clean(clock, monkeypatch):
    monkeypatch.setattr(settings, "analysis_timeout_seconds", lambda: 0.2)
    gate = threading.Event()
    runs = []

    def runner(inputs, deps, set_stage, set_meta=None):
        runs.append(1)
        set_stage("Scanning repositories")
        if len(runs) == 1:
            gate.wait(10)
        clock.now += 2.0
        return pipeline.PipelineOutput(result={"status": "ok"}, jd={}, cv={}, github=None)

    manager = JobManager(deps=object(), runner=runner)
    try:
        job_id = manager.create(make_inputs())
        assert wait_for(manager, job_id)["error"]["code"] == "timeout"
        monkeypatch.setattr(settings, "analysis_timeout_seconds", lambda: 5)
        assert manager.retry(job_id) is True
        done = wait_for(manager, job_id)
        assert done["state"] == "done" and done["error"] is None
        assert done["meta"]["timings"] == {"Scanning repositories": 2.0} and done["meta"]["scan"] is None
    finally:
        gate.set()
        manager.shutdown()


def test_a_real_pipeline_timeout_names_the_stage_over_http(make_env, monkeypatch):
    from test_api_support import FakeProvider
    monkeypatch.setattr(settings, "analysis_timeout_seconds", lambda: 0.2)
    gate = threading.Event()
    env = make_env(provider=FakeProvider(gate=gate))
    job = env.run()
    gate.set()
    assert job["error"]["code"] == "timeout" and job["error"]["stage"] == pipeline.STAGE_JD
    assert "too long" in job["error"]["message"] and job["meta"]["timings"] == {pipeline.STAGE_JD: job["meta"]["timings"][pipeline.STAGE_JD]}


def test_the_error_object_only_adds_stage_when_given():
    assert errors.error_object("timeout") == {"code": "timeout", "message": errors.MESSAGES["timeout"], "retryable": True}
    assert errors.error_object("timeout", stage="Scoring")["stage"] == "Scoring"


# --------------------------------------------------------------------------
# the repository cap
# --------------------------------------------------------------------------
def build_repos():
    """120 fake repos: every 7th is a fork, every 11th is empty, dates repeat so ties happen, a few have no date."""
    repos = []
    for i in range(120):
        repos.append({"name": f"repo-{i:03d}", "fork": i % 7 == 0, "size": 0 if i % 11 == 0 else 50 + i,
                      "pushed_at": None if i % 37 == 5 else f"2026-0{1 + (i % 9)}-1{i % 3}T10:00:00Z",
                      "owner": {"login": "someone"}})
    return repos


class ListingValidator(FakeValidator):
    """A fake validator with a listing call, a pass-through helper and a validate() that uses the listing."""

    def __init__(self, repos):
        super().__init__()
        self.repos, self.listed, self.other_calls = repos, [], 0

    def fetch_repos(self, username):
        self.listed.append(username)
        return list(self.repos)

    def _get(self, path):
        self.other_calls += 1
        return path

    def validate(self, username):
        self.seen = self.fetch_repos(username)
        return dict(github_ok(), username=username)


def expected_newest(repos, cap):
    eligible = [r for r in repos if not r["fork"] and r["size"] != 0]
    eligible.sort(key=lambda r: r["name"])
    eligible.sort(key=lambda r: r["pushed_at"] or "", reverse=True)
    return eligible[:cap]


def test_a_120_repo_account_is_cut_to_the_newest_30_eligible_in_a_stable_order():
    repos = build_repos()
    validator = ListingValidator(repos)
    listing = repocap.limit_listing(validator, 30)
    seen = validator.fetch_repos("someone")
    eligible_total = len([r for r in repos if not r["fork"] and r["size"] != 0])
    assert [r["name"] for r in seen] == [r["name"] for r in expected_newest(repos, 30)] and len(seen) == 30
    assert not any(r["fork"] or r["size"] == 0 for r in seen)
    assert [r["name"] for r in validator.fetch_repos("someone")] == [r["name"] for r in seen]  # deterministic
    dates = [r["pushed_at"] or "" for r in seen]
    assert dates == sorted(dates, reverse=True)
    for earlier, later in zip(seen, seen[1:]):  # equal dates: name order
        if earlier["pushed_at"] == later["pushed_at"]:
            assert earlier["name"] < later["name"]
    assert listing.meta() == {"repo_cap": 30, "repos_total": eligible_total, "repos_used": 30, "capped": True}
    assert eligible_total > 30


def test_everything_else_passes_through_unchanged():
    validator = ListingValidator(build_repos())
    repocap.limit_listing(validator, 30)
    assert validator._get("/x") == "/x" and validator.other_calls == 1
    assert validator.listed == []  # wrapping lists nothing by itself
    assert validator.validate("someone")["username"] == "someone" and validator.listed == ["someone"]
    assert validator.calls == [] and len(validator.seen) == 30  # validate() works from the capped listing


def test_a_cap_larger_than_the_account_changes_nothing():
    repos = build_repos()
    validator = ListingValidator(repos)
    listing = repocap.limit_listing(validator, 200)
    assert validator.fetch_repos("someone") == repos  # forks and empty repos stay: the listing is untouched
    total = len([r for r in repos if not r["fork"] and r["size"] != 0])
    assert listing.meta() == {"repo_cap": 200, "repos_total": total, "repos_used": total, "capped": False}


def test_an_account_exactly_at_the_cap_is_not_capped():
    repos = [{"name": f"r{i}", "fork": False, "size": 5, "pushed_at": "2026-01-01T00:00:00Z"} for i in range(30)]
    validator = ListingValidator(repos)
    listing = repocap.limit_listing(validator, 30)
    assert validator.fetch_repos("u") == repos and listing.meta()["capped"] is False


def test_a_validator_without_a_listing_call_is_left_alone():
    validator = FakeValidator()
    listing = repocap.limit_listing(validator, 30)
    assert listing.meta() == {"repo_cap": 30, "repos_total": None, "repos_used": None, "capped": False}
    assert not hasattr(validator, "fetch_repos")


def test_the_cap_does_not_change_the_inputs():
    repos = build_repos()
    snapshot = [dict(r) for r in repos]
    validator = ListingValidator(repos)
    repocap.limit_listing(validator, 30)
    validator.fetch_repos("someone")
    assert repos == snapshot


# -- meta.scan through the pipeline ---------------------------------------------------------------------------
def test_meta_scan_is_null_until_the_scan_stage_and_filled_for_a_capped_account(make_env, monkeypatch):
    monkeypatch.setattr(settings, "_limits_cache", settings.load_limits({"SCAN_MAX_REPOS": "30"}))
    repos = build_repos()
    env = make_env(validator_factory=lambda: ListingValidator(repos))
    job = env.run()
    total = len([r for r in repos if not r["fork"] and r["size"] != 0])
    assert job["state"] == "done"
    assert job["meta"]["scan"] == {"repo_cap": 30, "repos_total": total, "repos_used": 30, "capped": True}
    assert {"Checking GitHub", "Scanning repositories", "Scoring"} <= set(job["meta"]["timings"])
    kept = env.manager.intermediates(job["id"])
    assert job["result"] == rescored(job["result"], kept)  # the result is exactly what score_candidate returns
    assert "scan" not in job["result"]


def test_meta_scan_for_an_uncapped_account_and_for_a_validator_that_cannot_report(make_env, monkeypatch):
    monkeypatch.setattr(settings, "_limits_cache", settings.load_limits({"SCAN_MAX_REPOS": "200"}))
    repos = build_repos()
    env = make_env(validator_factory=lambda: ListingValidator(repos))
    total = len([r for r in repos if not r["fork"] and r["size"] != 0])
    assert env.run()["meta"]["scan"] == {"repo_cap": 200, "repos_total": total, "repos_used": total, "capped": False}
    plain = make_env()
    assert plain.run()["meta"]["scan"] == {"repo_cap": settings.scan_max_repos(),
                                           "repos_total": None, "repos_used": None, "capped": False}


def test_meta_scan_stays_null_when_the_scan_stage_never_runs(make_env):
    env = make_env(validator=FakeValidator({"username": "octocat", "status": "no_public_repos", "message": "",
                                            "repos": [], "summary": {}}))
    job = env.run()
    assert job["state"] == "done" and job["meta"]["scan"] is None


def test_the_scanner_gets_the_configured_repo_cap(monkeypatch):
    seen = {}

    class FakeScanner:
        def __init__(self, validator, taxonomy=None, max_repos=None):
            seen["max_repos"] = max_repos

    monkeypatch.setattr(pipeline, "StrictSqlTechScanner", FakeScanner)
    monkeypatch.setattr(settings, "_limits_cache", settings.load_limits({"SCAN_MAX_REPOS": "17"}))
    pipeline.default_dependencies().scanner_factory(object(), object())
    assert seen["max_repos"] == 17


# --------------------------------------------------------------------------
# logs carry only stage names, numbers and codes
# --------------------------------------------------------------------------
STAGE_NAMES = [pipeline.STAGE_JD, pipeline.STAGE_CV, pipeline.STAGE_GITHUB, pipeline.STAGE_SCAN, pipeline.STAGE_SCORING]
ALLOWED_LINES = [
    re.compile(r"job [0-9a-f-]{36} stage: (" + "|".join(map(re.escape, STAGE_NAMES)) + ")"),
    re.compile(r'stage="(' + "|".join(map(re.escape, STAGE_NAMES)) + r')" seconds=\d+\.\d'),
    re.compile(r"job [0-9a-f-]{36} (done|failed: [a-z_]+)"),
]


def test_logs_have_only_stage_names_numbers_and_codes(make_env, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(settings, "_limits_cache", settings.load_limits({"SCAN_MAX_REPOS": "30"}))
    env = make_env(validator_factory=lambda: ListingValidator(build_repos()))
    assert env.run()["state"] == "done"
    lines = [r.getMessage() for r in caplog.records if r.name == "skillverify.api"]
    assert lines and all(any(p.fullmatch(line) for p in ALLOWED_LINES) for line in lines), lines
    assert "repo-0" not in caplog.text and "someone" not in caplog.text and "Priya" not in caplog.text
