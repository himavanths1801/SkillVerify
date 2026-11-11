"""The `meta` field of a job: verified candidate name and a job title taken from the JD text. Offline, fakes only."""
import threading
import time

import pytest
from test_api_support import (CV_ANSWER, CV_TEXT, FakeProvider, JD_TEXT, env, make_env, rescored,
                              settings)  # noqa: F401

from api import jobs, meta
from skillverify.scoring import score_candidate


# --------------------------------------------------------------------------
# candidate name
# --------------------------------------------------------------------------
def cv_with(name, hint=None):
    return {"candidate": {"name": name, "name_candidate": hint}}


@pytest.mark.parametrize("cv, expected", [
    (cv_with("PRIYA RAMAN"), "Priya Raman"),            # all capitals -> title case
    (cv_with("Priya Raman"), "Priya Raman"),
    (cv_with("Priya McDonald"), "Priya McDonald"),      # mixed case is left alone
    (cv_with("priya raman"), "priya raman"),            # lower case is not all capitals
    (cv_with(None, hint="Someone Else"), None),         # the unverified hint is never used
    (cv_with("", hint="Someone Else"), None),
    (cv_with("   "), None),
    (cv_with(42), None),
    ({"candidate": None}, None),
    ({}, None),
    (None, None),
])
def test_candidate_name(cv, expected):
    assert meta.candidate_name(cv) == expected


def test_candidate_name_is_plain_and_capped():
    assert meta.candidate_name(cv_with("Priya\x00 \x07Raman\n")) == "Priya Raman"
    assert len(meta.candidate_name(cv_with("A" * 200))) == 80


# --------------------------------------------------------------------------
# job title
# --------------------------------------------------------------------------
@pytest.mark.parametrize("text, expected", [
    ("Junior Data Analyst - Retail Analytics Team\nWe are hiring.", "Junior Data Analyst"),
    ("Data Analyst, Customer Insights\nMore text.", "Data Analyst, Customer Insights"),
    ("Backend Developer (Python)\n", "Backend Developer (Python)"),
    ("\n\n   Data Engineer \u2013 Remote", "Data Engineer"),
    ("Data Engineer \u2014 Remote", "Data Engineer"),
    ("Data Engineer | Remote | Full time", "Data Engineer"),
    ("Data Engineer:", "Data Engineer"),
    ("Data\x00 Engineer", "Data Engineer"),
])
def test_job_title_accepted(text, expected):
    assert meta.job_title(text) == expected


@pytest.mark.parametrize("text", [
    "",                                              # empty file
    "   \n \n",
    "A" * 81,                                        # too long
    "One two three four five six seven eight nine ten eleven",   # too many words
    "About the role\nBackend work.",
    "We are looking for an analyst",
    "Our team builds things",
    "The Data Team",
    "Job Description\nAnalyst",
    "Overview",
    "Description of duties",
    "Sr. Data Analyst",                              # a full stop inside the line
    "Backend analytics role. Required: Python and SQL.",
    None,
])
def test_job_title_rejected(text):
    assert meta.job_title(text) is None


def test_job_title_boundaries():
    assert meta.job_title("A" * 80) == "A" * 80
    ten = " ".join(["Word"] * 10)
    assert meta.job_title(ten) == ten


# --------------------------------------------------------------------------
# through the API
# --------------------------------------------------------------------------
def test_meta_is_null_while_running_and_filled_after_done(make_env):
    gate = threading.Event()
    env = make_env(provider=FakeProvider(gate=gate))
    job_id = env.submit(jd_text="Data Analyst - Team A\nRequired: Python and SQL").json()["id"]
    deadline = time.time() + 5
    while time.time() < deadline and env.client.request("GET", f"/api/analyses/{job_id}").json()["state"] != "running":
        time.sleep(0.01)
    running = env.client.request("GET", f"/api/analyses/{job_id}").json()
    assert running["meta"] == {"candidate_name": None, "job_title": None, "model": "model-a", "fallback_used": False,
                               "scan": None, "timings": {}}
    gate.set()
    done = env.wait(job_id)
    assert done["state"] == "done"
    assert {k: v for k, v in done["meta"].items() if k not in ("scan", "timings")} == {
        "candidate_name": "Priya Sample", "job_title": "Data Analyst", "model": "model-a", "fallback_used": False}
    assert "Scoring" in done["meta"]["timings"]


def test_meta_title_is_null_when_the_jd_line_is_not_a_title(env):
    job = env.run()  # JD_TEXT's first line has a full stop in the middle
    assert job["meta"]["candidate_name"] == "Priya Sample" and job["meta"]["job_title"] is None


def test_meta_is_kept_on_a_failed_job_up_to_the_failing_step(make_env):
    env = make_env(provider=FakeProvider(cv_error=RuntimeError("boom")))
    job = env.run(jd_text="Data Analyst\nRequired: Python and SQL")
    assert job["state"] == "failed"
    assert (job["meta"]["candidate_name"], job["meta"]["job_title"]) == (None, "Data Analyst")


def test_result_is_still_exactly_score_candidate(env):
    job = env.run(jd_text="Data Analyst\nRequired: Python and SQL")
    kept = env.manager.intermediates(job["id"])
    assert job["result"] == rescored(job["result"], kept)
    assert "meta" not in job["result"]
    assert "Priya" not in str(job["result"].get("flags"))


def test_unverified_name_hint_is_not_used_end_to_end(make_env):
    cv = {**CV_ANSWER, "name": "Someone Who Is Not In The Text"}
    env = make_env(provider=FakeProvider(cv=cv))
    assert env.run()["meta"]["candidate_name"] is None


def test_meta_is_reset_by_a_retry_and_ignored_for_a_stale_attempt(make_env):
    env = make_env(provider=FakeProvider(always=RuntimeError("x")))
    job = env.run()
    manager = env.manager
    snap = manager.snapshot(job["id"])
    assert {k: v for k, v in snap["meta"].items() if k != "timings"} == {
        "candidate_name": None, "job_title": None, "model": "model-a", "fallback_used": False, "scan": None}
    # a late write from an attempt that is no longer running changes nothing
    with manager._lock:
        stored = manager._jobs[job["id"]]
        stored.state = jobs.FAILED
        stored.meta["job_title"] = "Old"
        stored.error = {"code": "ai_busy", "message": "m", "retryable": True}
    assert manager.retry(job["id"]) is True
    assert manager.snapshot(job["id"])["meta"]["job_title"] is None
