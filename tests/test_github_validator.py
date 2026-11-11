"""Unit tests for the GitHub Validator. All HTTP calls are mocked: no real API use."""
import json
from datetime import datetime, timezone

import pytest
import requests
import responses

from skillverify import github_validator as gv
from skillverify.github_validator import GitHubConfigError, GitHubValidator

API = "https://api.github.com"
NOW = datetime(2025, 6, 1, tzinfo=timezone.utc)


def make_validator(**kwargs):
    kwargs.setdefault("backoff_seconds", 0)  # never sleep in tests
    return GitHubValidator(token="test-token", now=NOW, **kwargs)


def repo(name, fork=False, pushed="2025-05-20T00:00:00Z"):
    return {
        "name": name, "html_url": f"https://github.com/alice/{name}", "fork": fork,
        "size": 100, "pushed_at": pushed, "language": "Python",
        "stargazers_count": 1, "owner": {"login": "alice"},
    }


def commit(date):
    return {"commit": {"author": {"date": date}}}


def mock_user(public_repos=1):
    responses.add(responses.GET, f"{API}/users/alice", json={
        "name": "Alice", "html_url": "https://github.com/alice",
        "public_repos": public_repos, "created_at": "2020-01-01T00:00:00Z"})


def mock_repo_page(page, data):
    responses.add(responses.GET, f"{API}/users/alice/repos", json=data,
                  match=[responses.matchers.query_param_matcher(
                      {"type": "owner", "per_page": "100", "page": str(page)})])


def mock_repo_list(repos):
    # page 1 has the repos, page 2 is empty (end of pagination)
    mock_repo_page(1, repos)
    mock_repo_page(2, [])


def mock_repo_details(name, languages, commit_dates):
    responses.add(responses.GET, f"{API}/repos/alice/{name}/languages", json=languages)
    responses.add(responses.GET, f"{API}/repos/alice/{name}/commits",
                  json=[commit(d) for d in commit_dates])


@responses.activate
def test_normal_user_with_several_repos():
    mock_user(3)
    mock_repo_list([repo("api"), repo("site"), repo("old", pushed="2023-01-01T00:00:00Z")])
    mock_repo_details("api", {"Python": 3000, "Shell": 1000},
                      ["2025-05-30T00:00:00Z", "2025-05-01T00:00:00Z"])
    mock_repo_details("site", {"JavaScript": 5000}, ["2025-04-01T00:00:00Z"])
    mock_repo_details("old", {"Python": 500}, [])

    result = make_validator().validate("alice")

    assert result["status"] == "ok"
    assert result["profile"]["name"] == "Alice"
    s = result["summary"]
    assert s["total_public_repos"] == 3 and s["counted_repos"] == 3
    assert s["total_commits_in_window"] == 3
    assert s["active_repos_in_window"] == 2
    assert s["languages_combined"] == {"JavaScript": 5000, "Python": 3500, "Shell": 1000}
    assert s["languages_ranked"] == ["JavaScript", "Python", "Shell"]
    assert s["most_recent_activity_date"] == "2025-05-30T00:00:00Z"

    api = result["repos"][0]
    assert api["commits_in_window"] == 2
    assert api["last_commit_date"] == "2025-05-30T00:00:00Z"
    assert api["commit_recency_days"] == 2  # May 30 -> June 1
    assert api["language_percentages"] == {"Python": 75.0, "Shell": 25.0}
    # no commits in window -> falls back to pushed_at
    old = result["repos"][2]
    assert old["commits_in_window"] == 0 and old["last_commit_source"] == "pushed_at"
    json.dumps(result)  # must be JSON-serializable


@responses.activate
def test_commit_request_filters_by_author_and_window():
    mock_user()
    mock_repo_list([repo("api")])
    mock_repo_details("api", {"Python": 1}, [])
    make_validator().validate("alice")
    commits_call = [c for c in responses.calls if "/commits" in c.request.url][0]
    assert "author=alice" in commits_call.request.url
    assert "since=2024-12-03" in commits_call.request.url  # NOW minus 180 days
    assert commits_call.request.headers["Authorization"] == "Bearer test-token"


@responses.activate
def test_user_with_only_forks_excluded_by_default():
    mock_user(2)
    mock_repo_list([repo("f1", fork=True), repo("f2", fork=True)])
    result = make_validator().validate("alice")  # no languages/commits mocks: must not be called
    assert result["status"] == "ok"
    assert result["summary"]["counted_repos"] == 0
    assert result["summary"]["forked_repos_excluded"] == 2
    assert all(r["is_fork"] and not r["counted"] for r in result["repos"])
    assert result["summary"]["languages_combined"] == {}
    assert "forks" in result["message"]


@responses.activate
def test_include_forks_option_counts_them():
    mock_user()
    mock_repo_list([repo("f1", fork=True)])
    mock_repo_details("f1", {"Go": 10}, ["2025-05-01T00:00:00Z"])
    result = make_validator(include_forks=True).validate("alice")
    assert result["summary"]["counted_repos"] == 1
    assert result["summary"]["languages_ranked"] == ["Go"]


@responses.activate
def test_nonexistent_user_404():
    responses.add(responses.GET, f"{API}/users/ghost", status=404, json={"message": "Not Found"})
    result = make_validator().validate("ghost")
    assert result["status"] == "user_not_found"
    assert result["repos"] == []


@responses.activate
def test_user_with_zero_repos():
    mock_user(0)
    mock_repo_list([])
    result = make_validator().validate("alice")
    assert result["status"] == "no_public_repos"
    assert result["repos"] == []


@responses.activate
def test_rate_limit_response():
    responses.add(responses.GET, f"{API}/users/alice", status=403,
                  headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1760000000"},
                  json={"message": "rate limit exceeded"})
    result = make_validator().validate("alice")
    assert result["status"] == "rate_limited"
    assert "2025-10-09" in result["message"]  # reset time from header, human readable


@responses.activate
def test_stops_early_when_rate_limit_nearly_exhausted():
    responses.add(responses.GET, f"{API}/users/alice", json={"public_repos": 1},
                  headers={"X-RateLimit-Remaining": "2", "X-RateLimit-Reset": "1760000000"})
    result = make_validator().validate("alice")
    assert result["status"] == "rate_limited"
    assert len(responses.calls) == 1  # did not make the repo-list call


@responses.activate
def test_pagination_across_two_pages():
    mock_user(101)
    mock_repo_page(1, [repo(f"r{i}") for i in range(100)])
    mock_repo_page(2, [repo("last")])
    mock_repo_page(3, [])
    assert len(make_validator().fetch_repos("alice")) == 101


@responses.activate
def test_commit_pagination_and_cap():
    # Every page returns 100 commits, so we must stop at max_commit_pages.
    responses.add(responses.GET, f"{API}/repos/alice/big/commits",
                  json=[commit("2025-05-01T00:00:00Z")] * 100)
    dates, capped = make_validator(max_commit_pages=3).fetch_user_commit_dates("alice", "big", "alice")
    assert len(dates) == 300 and capped is True


@responses.activate
def test_empty_repo_409_is_valid():
    mock_user()
    mock_repo_list([repo("empty")])
    responses.add(responses.GET, f"{API}/repos/alice/empty/languages", json={})
    responses.add(responses.GET, f"{API}/repos/alice/empty/commits", status=409,
                  json={"message": "Git Repository is empty."})
    result = make_validator().validate("alice")
    assert result["status"] == "ok"
    rec = result["repos"][0]
    assert rec["commits_in_window"] == 0 and rec["languages"] == {}
    assert rec["language_percentages"] == {}


@responses.activate
def test_network_error_retries_then_returns_error():
    responses.add(responses.GET, f"{API}/users/alice", body=requests.ConnectionError("boom"))
    result = make_validator(max_retries=2).validate("alice")
    assert result["status"] == "error"
    assert len(responses.calls) == 3  # 1 try + 2 retries


@responses.activate
def test_retry_recovers_after_server_error():
    responses.add(responses.GET, f"{API}/users/alice", status=502)
    mock_user()
    mock_repo_list([])
    result = make_validator().validate("alice")
    assert result["status"] == "no_public_repos"


def test_missing_token_raises_friendly_error(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(gv, "load_dotenv", lambda *a, **k: None)  # ignore a real .env
    with pytest.raises(GitHubConfigError, match="GITHUB_TOKEN"):
        GitHubValidator()


def test_save_result_to_json(tmp_path):
    out = gv.save_result_to_json({"username": "alice"}, tmp_path / "sub" / "alice.json")
    assert json.loads(out.read_text(encoding="utf-8")) == {"username": "alice"}


# ---------------------------------------------------------------------------
# Follow-up fixes
# ---------------------------------------------------------------------------
import importlib.util
from pathlib import Path


@responses.activate
@pytest.mark.parametrize("bad", ["../../escape", "a b", "", "a" * 40, "-leading", "ok\n"])
def test_invalid_usernames_make_no_http_calls(bad):
    result = make_validator().validate(bad)
    assert result["status"] == "error"
    assert result["message"].startswith("Invalid GitHub username")
    assert result["repos"] == [] and result["summary"]["counted_repos"] == 0
    assert len(responses.calls) == 0


def test_valid_usernames_pass():
    assert gv.is_valid_username("alice") and gv.is_valid_username("a-b-1") and gv.is_valid_username("a" * 39)
    assert not gv.is_valid_username(None)


@responses.activate
def test_one_failing_repo_does_not_kill_run():
    mock_user(2)
    mock_repo_list([repo("good"), repo("blocked")])
    mock_repo_details("good", {"Python": 100}, ["2025-05-01T00:00:00Z"])
    responses.add(responses.GET, f"{API}/repos/alice/blocked/languages", json={"Go": 5})
    responses.add(responses.GET, f"{API}/repos/alice/blocked/commits", status=451,
                  json={"message": "Repository access blocked"})
    result = make_validator().validate("alice")
    assert result["status"] == "ok"
    blocked = result["repos"][1]
    assert blocked["counted"] is False and "451" in blocked["error"]
    s = result["summary"]
    assert s["repos_failed"] == 1 and s["counted_repos"] == 1
    assert s["languages_combined"] == {"Python": 100}  # Go from the failed repo is not included
    assert s["forked_repos_excluded"] == 0
    assert s["total_public_repos"] == 2


@responses.activate
def test_rate_limit_during_repo_details_stops_run():
    mock_user(2)
    mock_repo_list([repo("a"), repo("b")])
    responses.add(responses.GET, f"{API}/repos/alice/a/languages", status=403,
                  headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1760000000"},
                  json={"message": "rate limit exceeded"})
    result = make_validator().validate("alice")
    assert result["status"] == "rate_limited"
    assert result["repos"] == []


@responses.activate
def test_non_json_200_body_returns_error():
    responses.add(responses.GET, f"{API}/users/alice", body="<html>oops</html>", status=200)
    result = make_validator().validate("alice")
    assert result["status"] == "error" and "JSON" in result["message"]
    assert len(responses.calls) == 1  # decode errors are not retried


@responses.activate
def test_other_request_exception_returns_error():
    responses.add(responses.GET, f"{API}/users/alice", body=requests.exceptions.TooManyRedirects("loop"))
    result = make_validator().validate("alice")
    assert result["status"] == "error"
    assert len(responses.calls) == 1  # not retried


@responses.activate
def test_401_gives_token_message_without_retry():
    responses.add(responses.GET, f"{API}/users/alice", status=401, json={"message": "Bad credentials"})
    result = make_validator().validate("alice")
    assert result["status"] == "error"
    assert result["message"] == ("GitHub rejected the token (invalid or expired). "
                                 "Create a new token and update GITHUB_TOKEN.")
    assert len(responses.calls) == 1


@responses.activate
@pytest.mark.parametrize("code", [403, 429])
def test_secondary_rate_limit_with_retry_after(code):
    responses.add(responses.GET, f"{API}/users/alice", status=code,
                  headers={"Retry-After": "60"}, json={"message": "Slow down"})
    result = make_validator().validate("alice")
    assert result["status"] == "rate_limited"
    assert "60 seconds" in result["message"]
    assert len(responses.calls) == 1


@responses.activate
def test_secondary_rate_limit_detected_from_body():
    responses.add(responses.GET, f"{API}/users/alice", status=403,
                  json={"message": "You have exceeded a secondary rate limit."})
    assert make_validator().validate("alice")["status"] == "rate_limited"


@responses.activate
def test_plain_403_is_a_normal_error_with_status_code():
    responses.add(responses.GET, f"{API}/users/alice", status=403, json={"message": "Forbidden"})
    result = make_validator().validate("alice")
    assert result["status"] == "error" and "403" in result["message"]


def _load_example_script():
    path = Path(__file__).resolve().parent.parent / "examples" / "run_validator.py"
    spec = importlib.util.spec_from_file_location("run_validator", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_example_script_filename_safety(tmp_path, monkeypatch, capsys):
    script = _load_example_script()
    assert script.output_path_for("../../escape") is None
    assert script.output_path_for("a/b") is None
    assert script.output_path_for("alice") == Path("output") / "alice.json"
    # Running main() with a bad name writes nothing and exits 1.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["run_validator.py", "../../escape"])
    assert script.main() == 1
    assert list(tmp_path.iterdir()) == []
    assert "Invalid GitHub username" in capsys.readouterr().out


@responses.activate
def test_all_repos_failing_still_ok_with_clear_message():
    mock_user()
    mock_repo_list([repo("gone")])
    responses.add(responses.GET, f"{API}/repos/alice/gone/languages", status=404, json={})
    result = make_validator().validate("alice")
    assert result["status"] == "ok"
    assert result["summary"]["repos_failed"] == 1
    assert "404" in result["repos"][0]["error"]
    assert "No repository could be inspected" in result["message"]


@responses.activate
def test_malformed_repo_data_is_isolated_per_repo():
    mock_user(2)
    bad = repo("bad")
    del bad["html_url"]  # build_repo_record will raise KeyError
    mock_repo_list([bad, repo("good")])
    mock_repo_details("bad", {"Go": 1}, [])
    mock_repo_details("good", {"Python": 1}, [])
    result = make_validator().validate("alice")
    assert result["status"] == "ok"
    assert "unexpected error" in result["repos"][0]["error"]
    assert result["summary"]["counted_repos"] == 1


@responses.activate
def test_unexpected_profile_shape_returns_error_not_exception():
    responses.add(responses.GET, f"{API}/users/alice", json=["not", "a", "dict"])
    result = make_validator().validate("alice")
    assert result["status"] == "error" and "Unexpected error" in result["message"]


@responses.activate
def test_secondary_limit_wording_without_retry_after_header():
    responses.add(responses.GET, f"{API}/users/alice", status=403,
                  json={"message": "abuse detection mechanism triggered"})
    result = make_validator().validate("alice")
    assert result["status"] == "rate_limited" and "few minutes" in result["message"]


@responses.activate
def test_non_json_403_without_signals_is_plain_error():
    responses.add(responses.GET, f"{API}/users/alice", status=403, body="nope")
    result = make_validator().validate("alice")
    assert result["status"] == "error" and "403" in result["message"]


@responses.activate
def test_backoff_sleeps_between_retries(monkeypatch):
    slept = []
    monkeypatch.setattr(gv.time, "sleep", slept.append)
    responses.add(responses.GET, f"{API}/users/alice", status=503)
    make_validator(max_retries=2, backoff_seconds=1.0).validate("alice")
    assert slept == [1.0, 2.0]


@responses.activate
def test_rate_limit_without_reset_header_and_wrapper(monkeypatch):
    responses.add(responses.GET, f"{API}/users/alice", status=403,
                  headers={"X-RateLimit-Remaining": "0"}, json={"message": "x"})
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    result = gv.validate_github_user("alice", backoff_seconds=0)
    assert result["status"] == "rate_limited" and "unknown time" in result["message"]


@responses.activate
def test_bare_429_is_rate_limited():
    responses.add(responses.GET, f"{API}/users/alice", status=429, json={"message": "Too many"})
    result = make_validator().validate("alice")
    assert result["status"] == "rate_limited"
    assert result["message"] == ("GitHub is rate limiting requests (HTTP 429). "
                                 "Wait about a minute and try again.")
    assert len(responses.calls) == 1


@responses.activate
def test_429_with_retry_after_45_mentions_seconds():
    responses.add(responses.GET, f"{API}/users/alice", status=429, headers={"Retry-After": "45"})
    result = make_validator().validate("alice")
    assert result["status"] == "rate_limited" and "45 seconds" in result["message"]


@responses.activate
def test_bare_429_during_repo_details_stops_run():
    mock_user(2)
    mock_repo_list([repo("a"), repo("b")])
    responses.add(responses.GET, f"{API}/repos/alice/a/languages", status=429, body="")
    result = make_validator().validate("alice")
    assert result["status"] == "rate_limited"
    assert result["repos"] == []
    assert not any("/repos/alice/b/" in c.request.url for c in responses.calls)
