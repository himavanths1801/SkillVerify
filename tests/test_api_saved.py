"""Saved results on disk: the SQLite store, restart, partial runs, the question-set cache, regenerate and retention.
Fakes and a fake clock only; each test uses its own temporary data folder."""
import asyncio
import json
import logging
import sqlite3
import time

import pytest
from test_api_questions import (JD_TEXT2, Fleet, QuestionProvider, World, clock, scan, shop_files,  # noqa: F401
                                validation)
from test_api_support import CHAIN, FakeScanner, FakeValidator, make_env  # noqa: F401 (make_env is a fixture)

from api import settings, storage
from api.models import ModelChain
from api.storage import FILE_NAME, MAX_CACHED_FILES, Store, question_evidence
from skillverify.jd.provider import LLMCallError
from skillverify_questions import config, writer

RATE = LLMCallError("Gemini rate limit reached (HTTP 429). Wait a while and try again.")
KEY = "gsk_FAKE-KEY-MUST-NEVER-BE-SAVED-123456"
RAW = "RAW-MODEL-OUTPUT-MARKER"


class Scripted(QuestionProvider):
    """A question provider whose first calls are scripted (an exception to raise, or None); a marker rides along in
    every raw answer, to prove the raw model output is never stored."""

    def __init__(self, script=()):
        super().__init__()
        self.script = list(script)

    def generate_json(self, system_prompt, user_prompt):
        if system_prompt != writer.SYSTEM_PROMPT:
            return super().generate_json(system_prompt, user_prompt)
        step = self.script.pop(0) if self.script else None
        if step is not None:
            self.question_calls += 1
            raise step
        answer = dict(super().generate_json(system_prompt, user_prompt))
        answer["debug_raw"] = RAW  # not a field of a question: the checks drop it
        return answer


class FakeDisk:
    """A clock for the store (seconds since 1970, starting now), so retention can be tested by moving it."""

    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now


def build(make_env, clock, directory, scripts=None, disk=None, entries=CHAIN):
    fleet, files = Fleet(), shop_files()
    for name, script in (scripts or {}).items():
        fleet.providers[name] = Scripted(script)
    store = Store(directory, clock=disk or FakeDisk())
    chain, qchain = ModelChain(CHAIN, clock=clock), ModelChain(entries, clock=clock)
    env = make_env(provider_factory=fleet.make, chain=chain, questions_chain=qchain, files_factory=lambda: files,
                   validator=FakeValidator(validation()), scanner=FakeScanner(scan()), store=store)
    world = World(env, fleet, files, clock, chain)
    world.store, world.qchain = store, qchain
    return world


@pytest.fixture
def folder(tmp_path):
    return tmp_path / "data"


# --------------------------------------------------------------------------
# the store itself
# --------------------------------------------------------------------------
def test_the_store_opens_lazily_and_creates_the_folder(tmp_path):
    target = tmp_path / "a" / "b"
    store = Store(target)
    assert not target.exists()  # nothing happens until the file is needed
    assert store.enabled and (target / FILE_NAME).exists()


def test_the_directory_can_be_given_by_a_function_read_only_when_needed(tmp_path):
    calls = []
    store = Store(lambda: calls.append(1) or tmp_path)
    assert calls == []
    assert store.enabled and calls == [1]


def test_an_analysis_round_trips_with_only_the_small_evidence_the_question_writer_needs(folder):
    store = Store(folder)
    github = {"username": "octocat", "profile": {"name": "SECRET-NAME"}, "summary": {"x": 1},
              "repos": [{"name": "r", "counted": True, "is_fork": False, "languages": {"Python": 5}, "url": "u",
                         "last_commit_date": "2026-01-01T00:00:00Z", "commits_in_window": 99},
                        {"name": "broken", "error": "boom"}, "junk"],
              "tech_stack": {"skills": [{"skill": "Python", "repos": ["r"], "examples": ["SECRET-PATH"]}, "junk"],
                             "per_repo": [{"name": "r", "default_branch": "main", "skills": ["x"]}, "junk"]}}
    evidence = question_evidence(github)
    assert evidence == {"username": "octocat",
                        "repos": [{"name": "r", "counted": True, "is_fork": False, "languages": {"Python": 5},
                                   "last_commit_date": "2026-01-01T00:00:00Z"}, {"name": "broken", "error": True}],
                        "tech_stack": {"skills": [{"skill": "Python", "repos": ["r"]}],
                                       "per_repo": [{"name": "r", "default_branch": "main"}]}}
    assert store.save_analysis("a1", 5.0, {"status": "ok"}, {"candidate_name": "X"}, evidence)
    assert store.load_analysis("a1") == {"id": "a1", "created": 5.0, "result": {"status": "ok"},
                                         "meta": {"candidate_name": "X"}, "evidence": evidence}
    assert store.load_analysis("missing") is None
    assert question_evidence(None) is None and question_evidence({"username": 5}) is None
    assert question_evidence({"username": "u"}) == {"username": "u", "repos": [], "tech_stack": {"skills": [], "per_repo": []}}


def test_a_run_round_trips_and_a_failed_regeneration_does_not_wipe_a_finished_run(folder):
    store = Store(folder)
    assert store.save_run("a1", "ok", {"questions": [1]}, {"model": "m"}, "fp")
    row = store.load_run("a1")
    assert row["status"] == "ok" and row["fingerprint"] == "fp" and row["error"] is None and row["replacements"] == []
    assert store.save_run("a1", "partial", {"questions": []}, {}, "fp", {"code": "x"}, keep_finished=True) is False
    assert store.load_run("a1")["status"] == "ok"
    assert store.save_run("a2", "partial", {"questions": [1]}, {}, "fp", {"code": "ai_busy"}, keep_finished=True)
    assert store.save_run("a2", "partial", {"questions": [1, 2]}, {}, "fp", {"code": "ai_busy"}, keep_finished=True)
    assert store.load_run("a2")["result"] == {"questions": [1, 2]} and store.load_run("a2")["error"] == {"code": "ai_busy"}
    assert store.load_run("missing") is None


def test_replacements_are_added_to_the_saved_run(folder):
    store = Store(folder)
    assert store.add_replacement("nope", {"x": 1}) is False
    store.save_run("a1", "ok", {}, {"model": "m"}, None)
    assert store.add_replacement("a1", {"after": "q1", "question": {"id": "q9"}})
    assert store.add_replacement("a1", {"after": None, "question": {"id": "q10"}}, {"model": "n"})
    row = store.load_run("a1")
    assert [r["question"]["id"] for r in row["replacements"]] == ["q9", "q10"] and row["meta"] == {"model": "n"}


def test_the_question_set_cache_finds_the_newest_finished_run_of_another_analysis(folder):
    clock = FakeDisk()
    store = Store(folder, clock=clock)
    store.save_run("old", "ok", {"n": 1}, {}, "fp")
    clock.now += 10
    store.save_run("new", "too_few", {"n": 2}, {}, "fp")
    clock.now += 10
    store.save_run("half", "partial", {"n": 3}, {}, "fp")  # a partial run is never a cache hit
    assert store.find_cached_run("fp", exclude="x")["analysis_id"] == "new"
    assert store.find_cached_run("fp", exclude="new")["analysis_id"] == "old"
    assert store.find_cached_run("other", exclude="x") is None


def test_the_file_cache_hits_misses_and_misses_again_when_the_push_date_changes(folder):
    store = Store(folder)
    assert store.get_file("o/r", "a.py", "2026-01-01") is None
    assert store.put_file("o/r", "a.py", "2026-01-01", "print(1)")
    assert store.get_file("o/r", "a.py", "2026-01-01") == "print(1)"
    assert store.get_file("o/r", "a.py", "2026-02-02") is None  # the repo changed
    assert store.get_file("o/r", "b.py", "2026-01-01") is None and store.get_file("o/other", "a.py", "2026-01-01") is None
    assert store.put_file("o/r", "a.py", None, "no date") and store.get_file("o/r", "a.py", None) == "no date"
    assert store.get_file("o/r", "a.py", "") == "no date"  # a missing date and an empty one are the same key


def test_a_file_over_the_size_limit_is_not_cached(folder):
    store = Store(folder)
    assert store.put_file("o/r", "big.py", "d", "x" * (config.MAX_FILE_BYTES + 1)) is False
    assert store.get_file("o/r", "big.py", "d") is None and store.count_files() == 0
    assert store.put_file("o/r", "ok.py", "d", "x" * config.MAX_FILE_BYTES)


def test_the_file_cache_keeps_at_most_2000_files(folder):
    assert MAX_CACHED_FILES == 2000


def test_the_file_cache_drops_the_oldest_files_first_when_it_is_full(folder, monkeypatch):
    monkeypatch.setattr(storage, "MAX_CACHED_FILES", 25)  # the same rule with a small cap, so the test stays fast
    clock = FakeDisk()
    store = Store(folder, clock=clock)
    for number in range(30):
        clock.now += 1
        store.put_file("o/r", f"f{number}.py", "d", f"text {number}")
    assert store.count_files() == 25
    assert store.get_file("o/r", "f0.py", "d") is None and store.get_file("o/r", "f4.py", "d") is None  # oldest gone
    assert store.get_file("o/r", "f5.py", "d") == "text 5" and store.get_file("o/r", "f29.py", "d") == "text 29"
    clock.now += 1
    store.put_file("o/r", "f5.py", "d", "text 5 again")  # writing a file again makes it the newest
    store.put_file("o/r", "new.py", "d", "new")
    assert store.get_file("o/r", "f5.py", "d") == "text 5 again" and store.get_file("o/r", "f6.py", "d") is None


def test_retention_deletes_saved_analyses_and_runs_older_than_the_days(folder, caplog):
    caplog.set_level(logging.INFO)
    clock = FakeDisk()
    store = Store(folder, clock=clock)
    store.save_analysis("old", clock.now, {"status": "ok"}, {}, None)
    store.save_run("old", "ok", {}, {}, None)
    clock.now += 10 * 86400
    store.save_analysis("recent", clock.now, {"status": "ok"}, {}, None)
    store.save_run("recent", "ok", {}, {}, None)
    clock.now += 5 * 86400  # "old" is 15 days old, "recent" 5
    assert store.purge(14) == (1, 1)
    assert store.load_analysis("old") is None and store.load_run("old") is None
    assert store.load_analysis("recent") is not None and store.load_run("recent") is not None
    assert store.purge(14) == (0, 0)
    assert "Saved results older than 14 days removed: analyses=1 question_runs=1" in caplog.text


def test_a_corrupt_file_switches_storage_off_with_one_plain_line_and_never_crashes(folder, caplog):
    folder.mkdir()
    (folder / FILE_NAME).write_bytes(b"this is not a sqlite database at all " * 50)
    store = Store(folder)
    caplog.set_level(logging.DEBUG)
    assert store.enabled is False
    assert store.save_analysis("a", 1.0, {}, {}, None) is False and store.load_analysis("a") is None
    assert store.save_run("a", "ok", {}, {}, None) is False and store.load_run("a") is None
    assert store.find_cached_run("fp", "x") is None and store.get_file("o/r", "a", "d") is None
    assert store.put_file("o/r", "a", "d", "t") is False and store.purge(14) == (0, 0) and store.count_files() == 0
    lines = [r.getMessage() for r in caplog.records if r.name == "skillverify.api"]
    assert lines == ["Saved results are off: the data file could not be used (DatabaseError). Everything stays in memory."]


def test_an_unusable_folder_switches_storage_off_too(tmp_path, caplog):
    occupied = tmp_path / "file"
    occupied.write_text("a file, not a folder")
    store = Store(occupied / "sub")
    caplog.set_level(logging.DEBUG)
    assert store.enabled is False and store.load_run("a") is None
    assert len([r for r in caplog.records if "Saved results are off" in r.getMessage()]) == 1


def test_a_row_that_cannot_be_read_switches_storage_off_instead_of_failing_a_request(folder, caplog):
    store = Store(folder)
    assert store.enabled
    with sqlite3.connect(str(folder / FILE_NAME)) as raw:
        raw.execute("INSERT INTO analyses (id, created, result, meta) VALUES ('bad', 1, '{not json', '{}')")
    caplog.set_level(logging.DEBUG)
    assert store.load_analysis("bad") is None and store.enabled is False
    assert [r.getMessage() for r in caplog.records].count(
        "Saved results are off: the data file could not be used (JSONDecodeError). Everything stays in memory.") == 1


# --------------------------------------------------------------------------
# restart: a new app object over the same data folder
# --------------------------------------------------------------------------
def test_a_finished_analysis_and_its_question_run_survive_a_restart(make_env, clock, folder):
    first = build(make_env, clock, folder)
    job_id = first.analysis()
    before = first.call("GET", job_id).json()
    run = first.ask(job_id)
    assert run["state"] == "done"
    second = build(make_env, clock, folder)  # the server was restarted
    after = second.call("GET", job_id).json()
    assert after["state"] == "done" and after["result"] == before["result"] and after["meta"] == before["meta"]
    again = second.call("GET", job_id, "/questions").json()
    assert again["state"] == "done" and again["result"] == run["result"] and again["meta"] == run["meta"]
    assert second.fleet.question_calls() == {"model-a": 0, "model-b": 0, "model-c": 0} and second.files.total() == 0


def test_running_failed_and_not_ok_analyses_are_not_saved(make_env, clock, folder):
    first = build(make_env, clock, folder)
    for provider in first.fleet.providers.values():
        provider.always = RuntimeError("boom")
    failed = first.env.run(jd_text=JD_TEXT2)
    assert failed["state"] == "failed"
    empty = make_env(validator=FakeValidator({"status": "no_public_repos", "username": "x", "repos": [], "message": ""}),
                     store=first.store)
    not_ok = empty.run(jd_text=JD_TEXT2)
    assert not_ok["state"] == "done" and not_ok["result"]["status"] == "no_github_evidence"
    second = build(make_env, clock, folder)
    assert second.call("GET", failed["id"]).status == 404 and second.call("GET", not_ok["id"]).status == 404


def test_a_restored_analysis_can_start_a_new_question_run(make_env, clock, folder):
    first = build(make_env, clock, folder)
    job_id = first.analysis()
    second = build(make_env, clock, folder)
    status = second.ask(job_id)  # the saved evidence is enough: no new analysis is needed
    assert status["state"] == "done" and len(status["result"]["questions"]) == 3


def test_a_restored_analysis_is_kept_past_the_one_hour_memory_limit(make_env, clock, folder, monkeypatch):
    first = build(make_env, clock, folder)
    job_id = first.analysis()
    second = build(make_env, clock, folder)
    assert second.call("GET", job_id).status == 200
    from api import jobs
    real = jobs._now
    monkeypatch.setattr(jobs, "_now", lambda: real() + settings.JOB_TTL_SECONDS + 100)
    assert second.call("GET", job_id).status == 200  # a saved analysis does not expire with the memory time-to-live


# --------------------------------------------------------------------------
# a partial run is saved and a retry continues from it
# --------------------------------------------------------------------------
def test_a_run_ended_by_limits_is_saved_as_partial_and_a_retry_after_a_restart_continues_from_it(make_env, clock, folder):
    scripts = {"model-a": [None, None, RATE], "model-b": [RATE], "model-c": [RATE]}  # 2 questions, then all three limited
    first = build(make_env, clock, folder, scripts=scripts)
    job_id = first.analysis()
    failed = first.ask(job_id)
    assert failed["state"] == "failed" and failed["error"]["code"] == "ai_rate_limited" and failed["result"] is None
    saved = first.store.load_run(job_id)
    assert saved["status"] == "partial" and len(saved["result"]["questions"]) == 2 and saved["error"]["retryable"] is True
    written = [q["id"] for q in saved["result"]["questions"]]
    second = build(make_env, clock, folder)  # a restart: the partial run is loaded from disk
    restored = second.call("GET", job_id, "/questions").json()
    assert restored["state"] == "failed" and restored["error"]["code"] == "ai_rate_limited" and restored["result"] is None
    clock.now += settings.RATE_LIMIT_COOLDOWN_SECONDS
    assert second.call("POST", job_id, "/questions/retry").status == 202
    done = second.wait(job_id)
    assert done["state"] == "done" and len(done["result"]["questions"]) == 3
    assert [q["id"] for q in done["result"]["questions"][:2]] == written  # the first two were kept, not written again
    assert second.fleet.question_calls() == {"model-a": 1, "model-b": 0, "model-c": 0}  # only the missing one was written
    assert second.files.calls["file"] == 0  # the file texts came from the cache: GitHub was not asked for them again
    assert first.store.load_run(job_id)["status"] == "ok"


def test_a_partial_run_keeps_its_model_counts_across_the_retry(make_env, clock, folder):
    first = build(make_env, clock, folder, scripts={"model-a": [None, None, RATE], "model-b": [RATE], "model-c": [RATE]})
    job_id = first.analysis()
    failed = first.ask(job_id)
    assert failed["meta"]["models_used"] == {"model-a": 2} and failed["meta"]["fallbacks"] == 2
    clock.now += settings.RATE_LIMIT_COOLDOWN_SECONDS
    first.call("POST", job_id, "/questions/retry")
    done = first.wait(job_id)
    assert done["meta"]["models_used"] == {"model-a": 2, "model-b": 1} or sum(done["meta"]["models_used"].values()) == 3
    assert done["meta"]["fallbacks"] >= 2


def test_a_run_that_wrote_nothing_saves_nothing(make_env, clock, folder):
    first = build(make_env, clock, folder, scripts={name: [RATE] for name in CHAIN})
    job_id = first.analysis()
    assert first.ask(job_id)["state"] == "failed"
    assert first.store.load_run(job_id) is None


# --------------------------------------------------------------------------
# the question-set cache
# --------------------------------------------------------------------------
def test_a_second_analysis_of_the_same_account_gets_the_saved_set_with_no_model_or_github_call(make_env, clock, folder):
    world = build(make_env, clock, folder)
    first_id = world.analysis()
    first = world.ask(first_id)
    calls = world.fleet.question_calls(), dict(world.files.calls)
    second_id = world.analysis()
    assert second_id != first_id
    started = world.call("POST", second_id, "/questions")
    assert started.status == 202 and started.json()["state"] == "done"  # at once
    cached = world.call("GET", second_id, "/questions").json()
    assert cached["result"] == first["result"] and cached["meta"]["cached"] is True
    assert cached["meta"]["cached_from"] == first_id and cached["result"]["schema_version"] == 1
    assert (world.fleet.question_calls(), dict(world.files.calls)) == calls  # asserted on the fakes: nothing was called
    assert world.store.load_run(second_id)["meta"]["cached_from"] == first_id  # and it is saved for the restart


def test_the_cached_set_is_restored_after_a_restart_and_can_still_write_replacements(make_env, clock, folder):
    world = build(make_env, clock, folder)
    first_id = world.analysis()
    world.ask(first_id)
    second_id = world.analysis()
    world.call("POST", second_id, "/questions")
    restarted = build(make_env, clock, folder)
    status = restarted.call("GET", second_id, "/questions").json()
    assert status["state"] == "done" and status["meta"]["cached"] is True
    response = restarted.call("POST", second_id, "/questions/replace")  # prepared again, from the file cache
    assert response.status == 200 and response.json()["question"]["why"]["path"] == "notebooks/explore.ipynb"


def test_a_different_set_of_asked_skills_is_a_miss(make_env, clock, folder):
    from test_api_questions import JD
    world = build(make_env, clock, folder)
    world.ask(world.analysis())
    for provider in world.fleet.providers.values():
        provider.jd = {"skills": JD["skills"][:2]}  # the next job description asks about Python and SQL only
    second = world.env.run(jd_text=JD_TEXT2 + "Also: a note about the team.\n")  # other text: a new reading
    assert second["state"] == "done"
    calls = sum(world.fleet.question_calls().values())
    status = world.ask(second["id"])
    assert "cached" not in status["meta"] and sum(world.fleet.question_calls().values()) > calls


def test_a_new_writer_settings_version_is_a_miss(make_env, clock, folder, monkeypatch):
    world = build(make_env, clock, folder)
    world.ask(world.analysis())
    monkeypatch.setattr(config, "WRITER_SETTINGS_VERSION", config.WRITER_SETTINGS_VERSION + 1)
    calls = sum(world.fleet.question_calls().values())
    status = world.ask(world.analysis())
    assert "cached" not in status["meta"] and sum(world.fleet.question_calls().values()) > calls


def test_a_different_account_is_a_miss(make_env, clock, folder):
    world = build(make_env, clock, folder)
    world.ask(world.analysis())
    calls = sum(world.fleet.question_calls().values())
    other = world.env.run(jd_text=JD_TEXT2, github_username="someone-else")
    assert other["state"] == "done"
    status = world.ask(other["id"])
    assert "cached" not in status["meta"] and sum(world.fleet.question_calls().values()) > calls


# --------------------------------------------------------------------------
# regenerate
# --------------------------------------------------------------------------
def test_regenerate_skips_the_set_cache_but_uses_the_file_cache_and_overwrites_the_saved_run(make_env, clock, folder):
    world = build(make_env, clock, folder)
    first_id = world.analysis()
    world.ask(first_id)
    second_id = world.analysis()
    world.call("POST", second_id, "/questions")  # served from the cache
    files_before, calls_before = dict(world.files.calls), sum(world.fleet.question_calls().values())
    response = world.call("POST", second_id, "/questions", raw_body=json.dumps({"regenerate": True}).encode(),
                          content_type="application/json")
    assert response.status == 202 and response.json()["state"] in ("queued", "running")
    done = world.wait(second_id)
    assert done["state"] == "done" and "cached" not in done["meta"]
    assert sum(world.fleet.question_calls().values()) == calls_before + 3  # the model wrote them again
    assert world.files.calls["file"] == files_before["file"]  # but the files came from the cache
    saved = world.store.load_run(second_id)
    assert "cached" not in saved["meta"] and saved["result"]["questions"][0]["id"] == done["result"]["questions"][0]["id"]
    assert saved["result"]["questions"][0]["id"] != world.store.load_run(first_id)["result"]["questions"][0]["id"]


def test_regenerate_works_after_a_restart_and_without_it_a_saved_run_is_not_started_again(make_env, clock, folder):
    world = build(make_env, clock, folder)
    job_id = world.analysis()
    first = world.ask(job_id)
    restarted = build(make_env, clock, folder)
    plain = restarted.call("POST", job_id, "/questions")
    assert plain.status == 409 and plain.json()["error"]["code"] == "questions_already_started"
    assert restarted.call("GET", job_id, "/questions").json()["result"] == first["result"]  # returned as it was saved
    response = restarted.call("POST", job_id, "/questions", raw_body=b'{"regenerate": true}', content_type="application/json")
    assert response.status == 202
    done = restarted.wait(job_id)
    assert done["state"] == "done" and done["result"]["questions"][0]["id"] != first["result"]["questions"][0]["id"]


def test_a_failed_regeneration_does_not_wipe_a_good_saved_run(make_env, clock, folder):
    world = build(make_env, clock, folder, scripts={name: [] for name in CHAIN})
    job_id = world.analysis()
    first = world.ask(job_id)
    for provider in world.fleet.providers.values():
        provider.script = [RATE, RATE, RATE]  # every model limited on its first call
    world.call("POST", job_id, "/questions", raw_body=b'{"regenerate": true}', content_type="application/json")
    assert world.wait(job_id)["state"] == "failed"
    assert world.store.load_run(job_id)["status"] == "ok"  # the good saved run is still there
    assert world.store.load_run(job_id)["result"]["questions"] == first["result"]["questions"]


def test_regenerate_is_refused_while_a_run_is_going(make_env, clock, folder):
    import threading
    world = build(make_env, clock, folder)
    gate = threading.Event()
    world.fleet.providers["model-a"].question_gate = gate
    job_id = world.analysis()
    assert world.call("POST", job_id, "/questions").status == 202
    refused = world.call("POST", job_id, "/questions", raw_body=b'{"regenerate": true}', content_type="application/json")
    assert refused.status == 409 and refused.json()["error"]["code"] == "questions_already_started"
    gate.set()
    assert world.wait(job_id)["state"] == "done"


@pytest.mark.parametrize("body", [b"not json", b"[1]", b'{"regenerate": "yes"}', b'{"regenerate": 1}'])
def test_a_bad_start_body_is_a_422(make_env, clock, folder, body):
    world = build(make_env, clock, folder)
    job_id = world.analysis()
    response = world.call("POST", job_id, "/questions", raw_body=body, content_type="application/json")
    assert response.status == 422 and response.json()["error"]["code"] == "invalid_input"


def test_regenerate_false_or_an_empty_object_is_a_normal_start(make_env, clock, folder):
    world = build(make_env, clock, folder)
    job_id = world.analysis()
    assert world.call("POST", job_id, "/questions", raw_body=b'{"regenerate": false}', content_type="application/json").status == 202
    assert world.wait(job_id)["state"] == "done"


# --------------------------------------------------------------------------
# the replace route updates the saved run
# --------------------------------------------------------------------------
def test_a_replacement_is_saved_with_the_run_and_its_file_stays_used_after_a_restart(make_env, clock, folder):
    world = build(make_env, clock, folder)
    job_id = world.analysis()
    first = world.ask(job_id)
    asked = first["result"]["questions"][0]["id"]
    response = world.call("POST", job_id, "/questions/replace", raw_body=json.dumps({"after": asked}).encode(),
                          content_type="application/json")
    assert response.status == 200
    row = world.store.load_run(job_id)
    assert [(r["after"], r["question"]["id"]) for r in row["replacements"]] == [(asked, response.json()["question"]["id"])]
    assert row["meta"]["models_used"] == {"model-a": 4}
    assert row["result"]["questions"] == first["result"]["questions"]  # the saved run itself is not rewritten
    restarted = build(make_env, clock, folder)
    again = restarted.call("POST", job_id, "/questions/replace")
    assert again.status == 409 and again.json()["error"]["code"] == "no_replacement_available"  # no file is used twice


def test_replace_needs_a_finished_run_even_after_a_restart(make_env, clock, folder):
    world = build(make_env, clock, folder)
    job_id = world.analysis()
    restarted = build(make_env, clock, folder)
    assert restarted.call("POST", job_id, "/questions/replace").status == 409


# --------------------------------------------------------------------------
# retention, health and fall-back
# --------------------------------------------------------------------------
def test_old_saved_results_are_deleted_when_the_server_starts(make_env, clock, folder):
    disk = FakeDisk()
    world = build(make_env, clock, folder, disk=disk)
    job_id = world.analysis()
    world.ask(job_id)
    disk.now += 15 * 86400  # 15 days later: older than the 14 days of SAVED_RESULTS_DAYS
    restarted = build(make_env, clock, folder, disk=disk)

    async def start_and_stop():
        async with restarted.env.app.router.lifespan_context(restarted.env.app):
            pass

    asyncio.run(start_and_stop())
    assert restarted.call("GET", job_id).status == 404
    assert restarted.store.load_run(job_id) is None


def test_recent_saved_results_are_kept_at_start(make_env, clock, folder):
    disk = FakeDisk()
    world = build(make_env, clock, folder, disk=disk)
    job_id = world.analysis()
    disk.now += 13 * 86400
    restarted = build(make_env, clock, folder, disk=disk)

    async def start_and_stop():
        async with restarted.env.app.router.lifespan_context(restarted.env.app):
            pass

    asyncio.run(start_and_stop())
    assert restarted.call("GET", job_id).status == 200


def test_health_says_whether_storage_is_on(make_env, clock, folder, monkeypatch):
    monkeypatch.setattr("api.app.load_dotenv", lambda: None)
    world = build(make_env, clock, folder)
    assert world.client.request("GET", "/api/health").json()["storage"] == {"enabled": True}
    assert make_env().client.request("GET", "/api/health").json()["storage"] == {"enabled": False}  # no store: memory only


def test_a_corrupt_data_file_leaves_the_app_working_in_memory_with_one_log_line(make_env, clock, folder, caplog):
    folder.mkdir()
    (folder / FILE_NAME).write_bytes(b"garbage " * 200)
    caplog.set_level(logging.DEBUG)
    world = build(make_env, clock, folder)
    job_id = world.analysis()
    status = world.ask(job_id)
    assert status["state"] == "done" and len(status["result"]["questions"]) == 3
    replace = world.call("POST", job_id, "/questions/replace")
    assert replace.status == 200
    assert world.client.request("GET", "/api/health").json()["storage"] == {"enabled": False}
    assert len([r for r in caplog.records if "Saved results are off" in r.getMessage()]) == 1


# --------------------------------------------------------------------------
# privacy: only what the API already returns, and the file texts
# --------------------------------------------------------------------------
def test_nothing_sensitive_is_in_the_database(make_env, clock, folder, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    monkeypatch.setenv("GROQ_API_KEY", KEY)
    monkeypatch.setenv("GITHUB_TOKEN", KEY)
    world = build(make_env, clock, folder)
    job_id = world.analysis()
    world.ask(job_id)
    world.call("POST", job_id, "/questions/replace")
    with sqlite3.connect(str(folder / FILE_NAME)) as raw:
        dump = "\n".join(raw.iterdump())
    assert KEY not in dump and "Bearer" not in dump and "Authorization" not in dump  # no key, token or header
    assert RAW not in dump  # the raw model answers are never stored
    assert "SECRET" not in dump
    tables = {row[0] for row in sqlite3.connect(str(folder / FILE_NAME)).execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert tables == {"analyses", "question_runs", "file_cache", "reading_cache"}  # nothing else is kept
    with sqlite3.connect(str(folder / FILE_NAME)) as raw:
        columns = [row[1] for row in raw.execute("PRAGMA table_info(analyses)")]
    assert columns == ["id", "created", "result", "meta", "evidence"]  # no CV text, no job description text
    stored = json.loads(sqlite3.connect(str(folder / FILE_NAME)).execute("SELECT result FROM analyses").fetchone()[0])
    assert "Priya Sample" not in json.dumps(stored.get("flags", [])) and set(stored) == set(world.call("GET", job_id).json()["result"])


def test_a_secret_in_a_candidate_file_is_redacted_before_it_reaches_the_file_cache(make_env, clock, folder):
    world = build(make_env, clock, folder)
    secret = "AKIA" + "ABCDEFGHIJKLMNOP"  # an AWS-style key id inside a fetched file
    for key in list(world.files.contents):
        world.files.contents[key] = world.files.contents[key] + f"\nAWS_ACCESS_KEY = '{secret}'\n"
    world.ask(world.analysis())
    with sqlite3.connect(str(folder / FILE_NAME)) as raw:
        dump = "\n".join(raw.iterdump())
    assert secret not in dump and "file_cache" in dump
    assert storage.Store(folder).count_files() > 0


# --------------------------------------------------------------------------
# small branches
# --------------------------------------------------------------------------
def test_an_analysis_without_an_account_has_no_fingerprint(make_env, clock, folder):
    world = build(make_env, clock, folder)
    manager = world.env.questions
    assert manager._fingerprint({}) is None and manager._fingerprint({"github": None}) is None
    assert manager._fingerprint({"github": {"username": None, "cv_username": None}}) is None
    assert manager._fingerprint({"github": {"username": "A"}, "skills": "x", "score": []}) == manager._fingerprint(
        {"github": {"cv_username": "a"}})  # lower case, and the CV's username is the fallback


def test_losing_a_race_to_start_the_same_run_is_the_usual_409(make_env, clock, folder, monkeypatch):
    world = build(make_env, clock, folder)
    job_id = world.analysis()
    assert world.call("POST", job_id, "/questions").status == 202
    monkeypatch.setattr(world.env.questions, "_job", lambda analysis_id: None)  # the other request has not been seen yet
    refused = world.call("POST", job_id, "/questions")
    assert refused.status == 409 and refused.json()["error"]["code"] == "questions_already_started"


def test_switching_off_survives_a_connection_that_cannot_be_closed(folder):
    store = Store(folder)
    assert store.enabled

    class Stuck:
        def close(self):
            raise sqlite3.Error("cannot close")

    store._conn = Stuck()
    store._switch_off(ValueError("x"))
    assert store.enabled is False
