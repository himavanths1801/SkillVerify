"""API internals: retry wrapper, error objects, temp files, the job store, the persistence guard, default wiring."""
import os
import threading
from pathlib import Path

import pytest
from test_api_support import CV_TEXT, FakeProvider, JD_TEXT, env, make_env, settings  # noqa: F401

from api import errors, jobs, pipeline, retry
from api.jobs import JobManager, StoreFullError
from skillverify.cv import CVExtractError
from skillverify.jd.growth import LEARNED_PATH, QUEUE_PATH
from skillverify.jd.provider import LLMCallError, LLMConfigError

ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------
# error objects
# --------------------------------------------------------------------------
def test_error_object_shape_and_closed_set():
    assert set(errors.CODES) == {"invalid_input", "jd_unreadable", "cv_unreadable", "github_user_not_found",
                                 "github_unavailable", "ai_busy", "ai_rate_limited", "ai_all_limited", "ai_not_configured",
                                 "timeout", "internal_error", "analysis_not_ready",
                                 "questions_already_started", "no_replacement_available"}
    for code in errors.CODES:
        obj = errors.error_object(code)
        assert obj == {"code": code, "message": errors.MESSAGES[code], "retryable": errors.RETRYABLE[code]}
    assert errors.error_object("jd_unreadable", "x" * 1000)["message"] == "x" * errors.MAX_MESSAGE_CHARS
    with pytest.raises(ValueError):
        errors.error_object("made_up")


def test_no_message_is_a_hiring_verdict():
    for text in errors.MESSAGES.values():
        assert not any(word in text.lower() for word in ("hire", "reject", "fail", "pass", "suitable"))


# --------------------------------------------------------------------------
# the retry wrapper
# --------------------------------------------------------------------------
class Scripted:
    name, model = "scripted", "m-1"

    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes), 0

    def generate_json(self, system_prompt, user_prompt):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.mark.parametrize("message, expected", [
    ("Gemini returned HTTP 500.", True), ("Gemini returned HTTP 502.", True), ("Gemini returned HTTP 503.", True),
    ("Gemini returned HTTP 504.", True), ("Network problem while calling Gemini (Timeout).", True),
    ("Gemini rate limit reached (HTTP 429). Wait a while and try again.", False),
    ("Gemini rejected the API key (HTTP 401). Check GEMINI_API_KEY.", False),
    ("Gemini returned HTTP 5030.", False), ("Gemini's answer was not valid JSON.", False),
    ("Something said: Network problem", False),
])
def test_is_temporary(message, expected):
    assert retry.is_temporary(message) is expected


def test_wrapper_passes_answers_and_identity_through(monkeypatch):
    monkeypatch.setattr(retry, "_sleep", lambda s: pytest.fail("must not wait"))
    inner = Scripted({"ok": 1})
    wrapped = retry.RetryingProvider(inner)
    assert wrapped.generate_json("s", "u") == {"ok": 1}
    assert (wrapped.name, wrapped.model) == ("scripted", "m-1")
    anonymous = retry.RetryingProvider(object())
    assert (anonymous.name, anonymous.model) == ("unknown", "unknown")


def test_wrapper_waits_the_configured_delays_then_raises(monkeypatch):
    waited = []
    monkeypatch.setattr(retry, "_sleep", waited.append)
    error = LLMCallError("Gemini returned HTTP 503.")
    inner = Scripted(error, error, error, error, {"never": 1})
    with pytest.raises(LLMCallError) as caught:
        retry.RetryingProvider(inner).generate_json("s", "u")
    assert caught.value is error and waited == [5, 10, 20] and inner.calls == 4


def test_wrapper_reads_the_delays_from_settings(monkeypatch):
    waited = []
    monkeypatch.setattr(retry, "_sleep", waited.append)
    monkeypatch.setattr(settings, "GEMINI_OUTER_RETRY_DELAYS", (1,))
    inner = Scripted(LLMCallError("Gemini returned HTTP 500."), {"ok": 1})
    assert retry.RetryingProvider(inner).generate_json("s", "u") == {"ok": 1} and waited == [1]


def test_wrapper_never_retries_other_errors(monkeypatch):
    monkeypatch.setattr(retry, "_sleep", lambda s: pytest.fail("must not wait"))
    for message in ("Gemini rate limit reached (HTTP 429). Wait.", "Gemini rejected the API key (HTTP 403)."):
        inner = Scripted(LLMCallError(message))
        with pytest.raises(LLMCallError):
            retry.RetryingProvider(inner).generate_json("s", "u")
        assert inner.calls == 1


# --------------------------------------------------------------------------
# temporary files
# --------------------------------------------------------------------------
def test_the_temporary_folder_is_deleted_after_a_successful_extraction(monkeypatch):
    text = CV_TEXT.encode()
    seen = []
    real = pipeline.extract_text

    def spy(path):
        seen.append(Path(path))
        assert Path(path).read_bytes() == text
        assert Path(path).name == "upload.txt"  # the uploaded name is never used as a path
        return real(path)

    monkeypatch.setattr(pipeline, "extract_text", spy)
    pipeline._extract(pipeline.UploadedFile(".txt", "x", text), "cv_unreadable")
    assert seen and not seen[0].parent.exists()


def test_the_temporary_folder_is_deleted_when_extraction_fails(monkeypatch):
    seen = []

    def failing(path):
        seen.append(Path(path))
        raise CVExtractError("unreadable_file", "The file could not be read.")

    monkeypatch.setattr(pipeline, "extract_text", failing)
    with pytest.raises(errors.PipelineError) as caught:
        pipeline._extract(pipeline.UploadedFile(".pdf", "x", b"junk"), "cv_unreadable")
    assert caught.value.error["code"] == "cv_unreadable" and caught.value.error["message"] == "The file could not be read."
    assert seen and not seen[0].parent.exists()


def test_the_temporary_folder_is_deleted_when_extraction_blows_up(monkeypatch):
    seen = []

    def exploding(path):
        seen.append(Path(path))
        raise RuntimeError("unexpected")

    monkeypatch.setattr(pipeline, "extract_text", exploding)
    with pytest.raises(RuntimeError):
        pipeline._extract(pipeline.UploadedFile(".txt", "x", b"junk"), "cv_unreadable")
    assert not seen[0].parent.exists()


def test_safe_stem():
    assert pipeline.safe_stem("My CV (1).pdf", "cv") == "My_CV__1_"
    assert pipeline.safe_stem("....pdf", "cv") == "cv" and pipeline.safe_stem("...", "job") == "job"
    assert len(pipeline.safe_stem("a" * 200 + ".pdf", "cv")) == 60


# --------------------------------------------------------------------------
# the persistence guard: nothing is written to the Phase 1 data
# --------------------------------------------------------------------------
def tree_state(folder):
    return {str(p.relative_to(ROOT)): (p.stat().st_size, p.stat().st_mtime_ns) for p in sorted(folder.rglob("*")) if p.is_file()}


def test_a_full_run_creates_and_changes_nothing_on_disk(env):
    watched = [ROOT / "output", ROOT / "skillverify" / "data"]
    before = {str(f): tree_state(f) for f in watched if f.exists()}
    queue_before = QUEUE_PATH.exists()
    learned_before = LEARNED_PATH.exists()

    assert env.run()["state"] == "done"
    assert env.run(job_name="second")["state"] == "done"  # and a second one, with an unmapped-free JD

    assert LEARNED_PATH.exists() is learned_before is False  # the overlay was never created
    assert QUEUE_PATH.exists() is queue_before  # the review queue was neither created nor removed
    assert {str(f): tree_state(f) for f in watched if f.exists()} == before  # sizes and mtimes of every file
    assert not list(ROOT.glob("*_jdparsed.json")) and not list(ROOT.glob("*_cvparsed.json"))


def test_the_parsers_run_without_persisting_or_auto_adding(env, monkeypatch):
    created = []
    for name in ("JDParser", "CVParser"):
        real = getattr(pipeline, name)

        def spy(*args, _real=real, **kwargs):
            created.append((_real.__name__, kwargs.get("persist"), kwargs.get("auto_add")))
            return _real(*args, **kwargs)

        monkeypatch.setattr(pipeline, name, spy)
    assert env.run()["state"] == "done"
    assert created == [("JDParser", False, False), ("CVParser", False, False)]


def test_no_secret_or_path_reaches_any_response(make_env):
    secret = "AIzaSySECRETKEY123456"
    env = make_env(provider=FakeProvider(always=LLMCallError(f"HTTP 503 at https://x?key={secret} C:\\Users\\a\\b.py")))
    job = env.run()
    for text in (secret, "https://", "Users", "Traceback", ".py"):
        assert text not in str(job)


# --------------------------------------------------------------------------
# default wiring (constructed, never called over the network)
# --------------------------------------------------------------------------
def test_default_dependencies_build_the_real_classes_offline(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token-for-test")
    from skillverify.sql_strict import StrictSqlTechScanner
    from skillverify.taxonomy_extra import load_extended_taxonomy
    deps = pipeline.default_dependencies()
    provider = deps.provider_factory()
    validator = deps.validator_factory()
    scanner = deps.scanner_factory(validator, load_extended_taxonomy())
    assert provider.name == "gemini" and isinstance(scanner, StrictSqlTechScanner)
    assert "fake-key" not in repr(provider)


def test_the_module_level_app_is_importable_and_lazy():
    import api.app
    assert api.app.app.state.jobs is not None


# --------------------------------------------------------------------------
# the job store
# --------------------------------------------------------------------------
def make_inputs():
    return pipeline.JobInputs(jd_text=JD_TEXT, jd_file=None, cv_file=pipeline.UploadedFile(".txt", "cv", CV_TEXT.encode()),
                              github_username=None, job_name="job")


def quick_runner(inputs, deps, set_stage, set_meta=None):
    set_stage("Scoring")
    return pipeline.PipelineOutput(result={"status": "ok"}, jd={}, cv={}, github=None)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def manager(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(settings, "_limits_cache", settings.load_limits({}))  # defaults; .env is never read
    monkeypatch.setattr(jobs, "_now", clock)
    made = JobManager(deps=object(), runner=quick_runner)
    made.clock = clock
    yield made
    made.shutdown()


def wait_done(manager, job_id):
    for _ in range(500):
        snap = manager.snapshot(job_id)
        if snap["state"] in ("done", "failed"):
            return snap
        threading.Event().wait(0.01)
    raise AssertionError("not finished")


def test_jobs_older_than_an_hour_are_dropped(manager):
    old = manager.create(make_inputs())
    wait_done(manager, old)
    manager.clock.now += settings.JOB_TTL_SECONDS - 1
    assert manager.snapshot(old) is not None
    manager.clock.now += 2
    assert manager.snapshot(old) is None
    assert manager.retry(old) is None
    fresh = manager.create(make_inputs())
    assert manager.snapshot(fresh)["id"] == fresh and len(manager._jobs) == 1


def test_at_most_max_jobs_are_kept_and_the_oldest_finished_one_goes(manager, monkeypatch):
    monkeypatch.setattr(settings, "MAX_JOBS", 3)
    ids = []
    for _ in range(3):
        ids.append(manager.create(make_inputs()))
        wait_done(manager, ids[-1])
        manager.clock.now += 1
    ids.append(manager.create(make_inputs()))
    assert len(manager._jobs) == 3
    assert manager.snapshot(ids[0]) is None and all(manager.snapshot(i) for i in ids[1:])


def test_when_every_kept_job_is_active_a_new_one_is_refused(monkeypatch):
    gate = threading.Event()

    def blocking(inputs, deps, set_stage, set_meta=None):
        gate.wait(10)
        return quick_runner(inputs, deps, set_stage, set_meta)

    monkeypatch.setattr(settings, "_limits_cache", settings.load_limits({}))  # defaults; .env is never read
    monkeypatch.setattr(settings, "MAX_JOBS", 2)
    monkeypatch.setattr(settings, "MAX_WORKERS", 1)
    manager = JobManager(deps=object(), runner=blocking)
    try:
        manager.create(make_inputs())
        manager.create(make_inputs())
        with pytest.raises(StoreFullError):
            manager.create(make_inputs())
    finally:
        gate.set()
        manager.shutdown()


def test_the_store_never_exceeds_two_running_jobs(monkeypatch):
    gate, lock = threading.Event(), threading.Lock()
    active = {"now": 0, "max": 0}

    def counting(inputs, deps, set_stage, set_meta=None):
        with lock:
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
        gate.wait(0.3)
        with lock:
            active["now"] -= 1
        return quick_runner(inputs, deps, set_stage, set_meta)

    monkeypatch.setattr(settings, "_limits_cache", settings.load_limits({}))  # defaults; .env is never read
    manager = JobManager(deps=object(), runner=counting)
    try:
        ids = [manager.create(make_inputs()) for _ in range(5)]
        for job_id in ids:
            wait_done(manager, job_id)
        assert active["max"] == settings.MAX_WORKERS == 2
    finally:
        manager.shutdown()


def test_a_stale_run_cannot_change_a_newer_attempt(manager):
    job_id = manager.create(make_inputs())
    wait_done(manager, job_id)
    manager._execute("missing", 0)  # unknown job: nothing happens
    manager._execute(job_id, 0)  # already finished: nothing happens
    assert manager.snapshot(job_id)["stage"] == "Done"
    assert manager.intermediates("missing") is None
    assert manager.intermediates(job_id) == {"jd": {}, "cv": {}, "github": None}


def test_a_run_whose_job_vanished_writes_nothing(monkeypatch):
    monkeypatch.setattr(settings, "_limits_cache", settings.load_limits({}))  # defaults; .env is never read
    holder = {}

    def vanishing(inputs, deps, set_stage, set_meta=None):
        with holder["manager"]._lock:
            holder["manager"]._jobs.clear()  # the job expired while the run was going
        set_stage("Scoring")
        set_meta(job_title="late")
        return pipeline.PipelineOutput(result={"status": "ok"}, jd={}, cv={}, github=None)

    manager = holder["manager"] = JobManager(deps=object(), runner=vanishing)
    try:
        job_id = manager.create(make_inputs())
        import time
        time.sleep(0.3)
        assert manager.snapshot(job_id) is None
    finally:
        manager.shutdown()
