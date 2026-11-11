"""Orchestration (choose code, read files, write, replace) and the GitHub files client. Offline, deterministic."""
import logging
import re

import pytest
import requests
from test_questions_support import (PY_ORDERS, RATE_LIMIT, FakeFiles, FakeWriter, contents_payload, github_result,
                                    good_answer, repo_record, shop_analysis, shop_files, skill_row, tech_item,
                                    tree_entry)

from skillverify_questions import config, github_files, service
from skillverify_questions.signals import Signals

SIGNALS = Signals()


def prepare(skills=None, github=None, files=None, **kwargs):
    if skills is None:
        skills, github = shop_analysis()
    elif github is None:
        github = shop_analysis()[1]
    return service.prepare(skills, github, files or shop_files(), SIGNALS, **kwargs)


SYMBOLS = {"analysis.py": ["clean_orders"], "sql/report.sql": ["monthly_revenue"], "scripts/release.sh": ["release"],
           "src/OrderList.jsx": ["OrderList"], "notebooks/explore.ipynb": ["simulate"]}


def writer_for(prepared, **overrides):
    """A fake Gemini that gives every chosen and reserve file a good answer."""
    answers = {}
    for pick in prepared.selection.picks + prepared.selection.reserve:
        path = pick.file.path
        base = path.rsplit("/", 1)[-1]
        name = SYMBOLS.get(path, ["the code"])[0]
        answers[path] = good_answer(path, SYMBOLS.get(path, []), question=(
            f"Open {base} and look at {name}. Extend it so it handles one more case, "
            f"then explain how you would check your change for {pick.need.skill}."))
    answers.update(overrides)
    return FakeWriter(answers)


# --------------------------------------------------------------------------
# choose code, read files
# --------------------------------------------------------------------------
def test_prepare_reports_the_selection_counts_and_skipped_skills():
    stages = []
    prepared = prepare(set_stage=stages.append)
    picks = [(p.need.skill, p.file.path) for p in prepared.selection.picks]
    assert picks == [("Python", "analysis.py"), ("SQL", "sql/report.sql"), ("Git", "scripts/release.sh"),
                     ("React", "src/OrderList.jsx")]
    assert prepared.counts == {"candidates_skills": 5, "repos_considered": 3, "files_fetched": 5, "files_ranked": 5}
    assert {s["skill"]: s["reason"] for s in prepared.selection.skipped} == {
        "Docker": "not_found_in_code", "Tableau": "not_checkable_from_code", "Pandas": "no_usable_file"}
    assert stages == ["Choosing code", "Reading files"]


def test_the_tree_is_listed_and_filtered_before_any_content_is_fetched():
    files = shop_files()
    prepare(files=files)
    for repo in ("orders-analysis", "release-tools", "shop-ui"):
        sequence = [kind for kind, name in files.order if name == repo]
        assert sequence[0] == "tree" and sequence.count("tree") == 1
    fetched = {path for _, path in files.fetched}
    assert fetched == {"analysis.py", "sql/report.sql", "scripts/release.sh", "src/OrderList.jsx",
                       "notebooks/explore.ipynb"}  # no README.md, no tests/, no node_modules/
    assert files.calls["default_branch"] == 0  # the scan already knew the branch


def test_a_missing_default_branch_is_looked_up_and_an_unknown_one_skips_the_repo():
    skills, github = shop_analysis()
    for item in github["tech_stack"]["per_repo"]:
        item["default_branch"] = None
    files = shop_files()
    prepare(skills, github, files)
    assert files.calls["default_branch"] == 3
    empty = FakeFiles(shop_files().trees, shop_files().contents, branch=None)
    prepared = prepare(skills, github, empty)
    assert prepared.selection.picks == [] and prepared.counts["files_fetched"] == 0 and empty.calls["tree"] == 0


def test_fetch_caps_per_repo_and_overall(monkeypatch):
    skills = [skill_row("Python")]
    repos = [repo_record(f"repo{i}", f"2026-0{i + 1}-01T00:00:00Z") for i in range(6)]
    github = github_result(repos, [tech_item("Python", [r["name"] for r in repos])])
    trees = {r["name"]: [tree_entry(f"m{i}.py", 1000 + i) for i in range(20)] for r in repos}
    contents = {(name, f"m{i}.py"): PY_ORDERS for name in trees for i in range(20)}
    files = FakeFiles(trees, contents)
    prepared = service.prepare(skills, github, files, SIGNALS)
    assert prepared.counts["repos_considered"] == config.MAX_REPOS_CONSIDERED == 5
    per_repo = {name: sum(1 for repo, _ in files.fetched if repo == name) for name in trees}
    assert max(per_repo.values()) == config.MAX_FILES_PER_REPO_FETCHED == 8
    assert files.calls["file"] == 5 * 8 == 40 == config.MAX_FILE_FETCHES
    monkeypatch.setattr(config, "MAX_FILE_FETCHES", 11)
    files = FakeFiles(trees, contents)
    service.prepare(skills, github, files, SIGNALS)
    assert files.calls["file"] == 11
    monkeypatch.setattr(config, "MAX_FILE_FETCHES", 0)
    files = FakeFiles(trees, contents)
    assert service.prepare(skills, github, files, SIGNALS).selection.picks == []


def test_the_largest_code_files_are_fetched_first():
    skills = [skill_row("Python")]
    github = github_result([repo_record("r")], [tech_item("Python", ["r"])])
    tree = {"r": [tree_entry(f"f{i}.py", 500 + i * 10) for i in range(12)] + [tree_entry("big.py", 9000)]}
    files = FakeFiles(tree, {})
    service.prepare(skills, github, files, SIGNALS)
    assert [path for _, path in files.fetched] == ["big.py"] + [f"f{i}.py" for i in range(11, 4, -1)]


def test_no_candidate_skills_means_no_github_call_at_all():
    skills = [skill_row("Docker", status="not_found"), skill_row("Tableau", status="not_detectable_from_github")]
    files = shop_files()
    prepared = service.prepare(skills, shop_analysis()[1], files, SIGNALS)
    assert files.total() == 0 and prepared.selection.picks == []
    assert [s["reason"] for s in prepared.selection.skipped] == ["not_found_in_code", "not_checkable_from_code"]
    assert service.prepare("junk", None, files, SIGNALS).selection.skipped == []
    assert service.prepare([skill_row("Python")], {"repos": []}, files, SIGNALS).counts["repos_considered"] == 0


def test_github_errors_are_raised_plainly_and_never_retried():
    boom = github_files.GitHubFilesError(github_files.RATE_LIMITED, "GitHub's request limit was reached.")
    files = FakeFiles(shop_files().trees, shop_files().contents, error=boom)
    with pytest.raises(github_files.GitHubFilesError):
        prepare(files=files)
    assert files.calls["tree"] == 1  # one attempt, no retry


def test_missing_files_and_unreadable_files_are_skipped():
    files = shop_files()
    del files.contents[("orders-analysis", "analysis.py")]
    files.contents[("release-tools", "scripts/release.sh")] = "# auto-generated\n" + files.contents[
        ("release-tools", "scripts/release.sh")]
    prepared = prepare(files=files)
    assert prepared.counts["files_fetched"] == 5 and prepared.counts["files_ranked"] == 3
    assert "analysis.py" not in {p.file.path for p in prepared.selection.picks}


# --------------------------------------------------------------------------
# write and check
# --------------------------------------------------------------------------
def test_a_full_run_gives_ok_with_the_why_record_and_ids():
    prepared = prepare()
    provider = writer_for(prepared)
    stages = []
    ids = iter(f"q_{i:08x}" for i in range(9))
    result, state = service.write_all(prepared, provider, stages.append, make_id=lambda: next(ids))
    assert result["status"] == "ok" and result["message"] is None and result["limits"] == {"min": 3, "max": 5}
    assert [q["id"] for q in result["questions"]] == ["q_00000000", "q_00000001", "q_00000002", "q_00000003"]
    first = result["questions"][0]
    assert set(first) == {"id", "kind", "question", "language", "starter_code", "reference_notes", "why"}
    assert first["language"] == "python"
    assert first["why"] == {"repo": "orders-analysis", "path": "analysis.py", "skill": "Python", "tier": "must_have",
                            "claimed": True, "top_ranked": True, "symbols": ["clean_orders"], "reason": (
                                "Asked because the CV claims Python and this is the highest-ranked file in the repos "
                                "that uses it.")}
    assert result["questions"][2]["why"]["reason"].startswith("Asked because the job lists Git as a nice-to-have")
    assert result["counts"] == {"candidates_skills": 5, "repos_considered": 3, "files_fetched": 5, "files_ranked": 5,
                                "questions": 4, "dropped": 0}
    assert provider.calls == 4 and stages == ["Writing questions", "Checking questions"]
    assert state.used == {("orders-analysis", "analysis.py"), ("orders-analysis", "sql/report.sql"),
                          ("release-tools", "scripts/release.sh"), ("shop-ui", "src/OrderList.jsx")}
    assert result["generated_at"].endswith("+00:00")


def test_default_ids_look_like_q_plus_8_hex_characters():
    assert re.fullmatch(r"q_[0-9a-f]{8}", service.new_id()) and service.new_id() != service.new_id()


def test_a_dropped_question_is_replaced_from_the_reserve_and_recorded_honestly():
    prepared = prepare()
    bad = good_answer("sql/report.sql", ["monthly_revenue"], file="wrong.sql")
    provider = writer_for(prepared, **{"sql/report.sql": [bad, bad]})
    result, _ = service.write_all(prepared, provider)
    asked = [(q["why"]["skill"], q["why"]["path"]) for q in result["questions"]]
    assert not any(skill == "SQL" for skill, _ in asked) and ("Python", "analysis.py") in asked and len(asked) == 4
    assert asked.count(("Python", "notebooks/explore.ipynb")) == 1  # the next reserve candidate filled the gap
    assert {"skill": "SQL", "tier": "must_have", "reason": "question_failed"} in result["skipped_skills"]
    assert result["counts"]["dropped"] == 1 and provider.calls == 6


def test_a_skill_that_failed_once_but_got_a_question_from_another_file_is_not_listed_as_skipped():
    prepared = prepare()
    bad = good_answer("analysis.py", ["clean_orders"], file="wrong.py")
    provider = writer_for(prepared, **{"analysis.py": [bad, bad]})
    result, _ = service.write_all(prepared, provider)
    python = [q for q in result["questions"] if q["why"]["skill"] == "Python"]
    assert [q["why"]["path"] for q in python] == ["notebooks/explore.ipynb"]
    assert not any(s["skill"] == "Python" for s in result["skipped_skills"])


def test_too_few_and_none_are_reported_honestly_not_padded():
    github = github_result([repo_record("orders-analysis")], [tech_item("Python", ["orders-analysis"]),
                                                              tech_item("SQL", ["orders-analysis"])])
    prepared = service.prepare([skill_row("Python"), skill_row("SQL")], github, shop_files(), SIGNALS)
    result, _ = service.write_all(prepared, writer_for(prepared))
    assert result["status"] == "too_few" and len(result["questions"]) == 2
    assert result["message"] == ("Only 2 question(s) could be written from the candidate's code. "
                                 "A session needs at least 3.")
    nothing = service.prepare([skill_row("Docker", status="not_found")], github, shop_files(), SIGNALS)
    empty, _ = service.write_all(nothing, FakeWriter())
    assert empty["status"] == "none" and empty["questions"] == []
    assert empty["message"] == "No code was found that shows the job's skills, so no questions could be written."
    one = service.prepare([skill_row("Python")], github, shop_files(), SIGNALS)
    result, _ = service.write_all(one, writer_for(one))
    assert result["message"].startswith("Only 1 question(s)")


def test_at_most_five_questions_and_the_attempt_cap():
    names = ["Python", "SQL", "Git", "React", "Pandas", "NumPy"]
    prepared = prepare([skill_row(n) for n in names], shop_analysis()[1])
    assert len(prepared.selection.picks) <= config.MAX_QUESTIONS
    result, _ = service.write_all(prepared, writer_for(prepared))
    assert len(result["questions"]) <= config.MAX_QUESTIONS
    # every answer fails: no more than MAX_QUESTIONS + MAX_REPLACEMENT_CANDIDATES questions are attempted
    failing = FakeWriter(default=good_answer("zzz", []))
    result, _ = service.write_all(prepared, failing)
    attempted = failing.calls // config.MAX_QUESTION_ATTEMPTS
    assert attempted == min(len(prepared.selection.picks) + len(prepared.selection.reserve),
                            config.MAX_QUESTIONS + config.MAX_REPLACEMENT_CANDIDATES)
    assert result["status"] == "none" and result["counts"]["dropped"] == attempted


def test_a_gemini_rate_limit_stops_the_run_and_keeps_nothing():
    prepared = prepare()
    provider = writer_for(prepared, **{"sql/report.sql": [RATE_LIMIT]})
    with pytest.raises(type(RATE_LIMIT)):
        service.write_all(prepared, provider)
    assert provider.calls == 2  # the first question, then the one that was limited


def test_the_same_inputs_give_the_same_questions_in_the_same_order():
    runs = []
    for _ in range(3):
        prepared = prepare()
        result, _ = service.write_all(prepared, writer_for(prepared), make_id=lambda: "q_x")
        runs.append([(q["why"]["skill"], q["why"]["path"]) for q in result["questions"]])
    assert runs[0] == runs[1] == runs[2]


def test_prompts_carry_only_redacted_file_text():
    files = shop_files()
    files.contents[("orders-analysis", "analysis.py")] = PY_ORDERS + 'API_KEY = "abcd1234efgh5678"\n'
    prepared = prepare(files=files)
    provider = writer_for(prepared)
    service.write_all(prepared, provider)
    sent = "\n".join(user for _, user in provider.prompts)
    assert "abcd1234efgh5678" not in sent and "[REDACTED]" in sent


# --------------------------------------------------------------------------
# replacement
# --------------------------------------------------------------------------
def test_replacement_uses_the_next_unused_reserve_file_and_never_a_used_one():
    prepared = prepare()
    provider = writer_for(prepared)
    _, state = service.write_all(prepared, provider)
    question, skipped = service.replace_one(prepared, state, provider)
    assert question["why"]["path"] == "notebooks/explore.ipynb" and question["why"]["skill"] == "Python"
    assert ("shop-ui", "notebooks/explore.ipynb") in state.used and isinstance(skipped, list)
    assert service.replace_one(prepared, state, provider) is None  # nothing is left


def test_replacement_tries_up_to_three_files_and_gives_up(monkeypatch):
    skills = [skill_row("Python")]
    github = github_result([repo_record("r")], [tech_item("Python", ["r"])])
    trees = {"r": [tree_entry(f"m{i}.py", 1000 + i) for i in range(6)]}
    contents = {("r", f"m{i}.py"): PY_ORDERS.replace("clean_orders", f"clean_{i}") for i in range(6)}
    monkeypatch.setattr(config, "TOP_FILES_PER_SKILL", 6)
    prepared = service.prepare(skills, github, FakeFiles(trees, contents), SIGNALS)
    assert len(prepared.selection.reserve) == 5
    state = service.RunState(used={prepared.selection.picks[0].file.key})
    bad = FakeWriter(default=good_answer("zzz", []))
    assert service.replace_one(prepared, state, bad) is None
    assert bad.calls == config.MAX_REPLACEMENT_CANDIDATES * config.MAX_QUESTION_ATTEMPTS
    assert state.dropped == 3 and [s["reason"] for s in state.skipped] == ["question_failed"]


# --------------------------------------------------------------------------
# privacy
# --------------------------------------------------------------------------
def test_nothing_is_logged_by_the_question_code(caplog):
    caplog.set_level(logging.DEBUG)
    prepared = prepare()
    result, state = service.write_all(prepared, writer_for(prepared))
    service.replace_one(prepared, state, writer_for(prepared))
    assert caplog.records == [] and result["questions"]


# --------------------------------------------------------------------------
# the GitHub files client
# --------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status=200, data=None, bad_json=False):
        self.status_code, self._data, self._bad = status, data, bad_json

    def json(self):
        if self._bad:
            raise ValueError("x")
        return self._data


class FakeSession:
    def __init__(self, *responses):
        self.responses, self.requests = list(responses), []

    def get(self, url, params=None, headers=None, timeout=None):
        self.requests.append((url, params, headers, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def client(*responses, token="ghp_SECRETTOKEN1234567890"):
    session = FakeSession(*responses)
    return github_files.GitHubFiles(token=token, session=session), session


def test_the_token_comes_from_the_environment_like_step_1_and_is_never_shown(monkeypatch):
    monkeypatch.setattr(github_files, "load_dotenv", lambda: None)
    monkeypatch.setenv("GITHUB_TOKEN", " env-token-value ")
    session = FakeSession(FakeResponse(data={"default_branch": "main"}))
    made = github_files.GitHubFiles(session=session)
    assert made.default_branch("o", "r") == "main"
    assert session.requests[0][2]["Authorization"] == "Bearer env-token-value"
    assert "env-token-value" not in repr(made) and "env-token-value" not in session.requests[0][0]
    monkeypatch.delenv("GITHUB_TOKEN")
    with pytest.raises(github_files.GitHubFilesError) as raised:
        github_files.GitHubFiles(session=session)
    assert raised.value.kind == github_files.NOT_CONFIGURED


def test_the_default_session_is_a_requests_session(monkeypatch):
    monkeypatch.setattr(github_files, "load_dotenv", lambda: None)
    assert isinstance(github_files.GitHubFiles(token="t")._session, requests.Session)


def test_tree_is_one_recursive_call_and_empty_or_gone_repos_give_nothing():
    made, session = client(FakeResponse(data={"tree": [{"path": "a.py", "type": "blob"}, "junk"]}),
                           FakeResponse(status=409), FakeResponse(status=404), FakeResponse(data={"tree": "x"}))
    assert made.tree("o", "r", "main") == [{"path": "a.py", "type": "blob"}]
    assert session.requests[0][0].endswith("/repos/o/r/git/trees/main") and session.requests[0][1] == {"recursive": "1"}
    assert made.tree("o", "r", "main") == [] and made.tree("o", "r", "main") == [] and made.tree("o", "r", "m") == []


def test_file_text_decodes_and_returns_none_for_gone_or_unreadable_files():
    made, session = client(FakeResponse(data=contents_payload("print('é')\n")), FakeResponse(status=404),
                           FakeResponse(data={"encoding": "none"}), FakeResponse(data=["x"]),
                           FakeResponse(data={"encoding": "base64", "content": "%%%"}))
    assert made.file_text("o", "r", "dir/a b.py", "main") == "print('é')\n"
    assert session.requests[0][0].endswith("/contents/dir/a%20b.py") and session.requests[0][1] == {"ref": "main"}
    assert [made.file_text("o", "r", "x.py", "main") for _ in range(4)] == [None, None, None, None]


def test_default_branch_missing_cases():
    made, _ = client(FakeResponse(status=404), FakeResponse(data={"default_branch": ""}), FakeResponse(data=[]))
    assert [made.default_branch("o", "r") for _ in range(3)] == [None, None, None]


@pytest.mark.parametrize("response, kind, text", [
    (FakeResponse(status=403), github_files.RATE_LIMITED, "request limit"),
    (FakeResponse(status=429), github_files.RATE_LIMITED, "request limit"),
    (FakeResponse(status=401), github_files.UNAVAILABLE, "rejected the token"),
    (FakeResponse(status=500), github_files.UNAVAILABLE, "HTTP 500"),
    (FakeResponse(bad_json=True), github_files.UNAVAILABLE, "could not be read"),
    (requests.ConnectionError("https://api.github.com/x?token=ghp_SECRETTOKEN1234567890"),
     github_files.UNAVAILABLE, "ConnectionError"),
])
def test_github_failures_are_plain_errors_with_no_retry_and_no_secrets(response, kind, text):
    made, session = client(response)
    with pytest.raises(github_files.GitHubFilesError) as raised:
        made.tree("o", "r", "main")
    assert raised.value.kind == kind and text in str(raised.value)
    assert "SECRET" not in str(raised.value) and len(session.requests) == 1


def test_a_gemini_failure_during_a_replacement_leaves_the_file_unused():
    prepared = prepare()
    provider = writer_for(prepared)
    _, state = service.write_all(prepared, provider)
    with pytest.raises(type(RATE_LIMIT)):
        service.replace_one(prepared, state, FakeWriter(default=RATE_LIMIT))
    assert ("shop-ui", "notebooks/explore.ipynb") not in state.used
    assert service.replace_one(prepared, state, provider) is not None
