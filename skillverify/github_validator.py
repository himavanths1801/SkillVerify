"""GitHub Validator (SkillVerify, Step 1).

Given a GitHub username, collect real, verifiable evidence of what that person
has built and return it as a JSON-serializable dictionary.

The file is organised in three layers so each can be understood (and tested)
on its own:

1. Pure data-shaping functions (no network): ``build_repo_record``,
   ``build_summary``, ``language_percentages`` ...
2. ``GitHubValidator``: talks to the GitHub REST API (network only).
3. Small helpers: ``validate_github_user`` and ``save_result_to_json``.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import requests
from dotenv import load_dotenv

API_BASE = "https://api.github.com"
DEFAULT_WINDOW_DAYS = 180          # about 6 months
DEFAULT_TIMEOUT_SECONDS = 15
DEFAULT_MAX_RETRIES = 3
DEFAULT_MAX_COMMIT_PAGES = 5       # 5 pages x 100 commits = 500 commits per repo max
MIN_RATE_LIMIT_REMAINING = 5       # stop early instead of burning the last few calls


# --------------------------------------------------------------------------
# Exceptions
# --------------------------------------------------------------------------
class GitHubConfigError(RuntimeError):
    """Raised when the validator is not configured correctly (e.g. no token)."""


class _NotFoundError(Exception):
    """Internal: GitHub returned 404."""


class _RateLimitError(Exception):
    """Internal: rate limit hit (or nearly exhausted)."""


class _ApiError(Exception):
    """Internal: network failure or unexpected HTTP status after retries."""


class _FatalApiError(_ApiError):
    """Internal: an API problem that affects the whole run (e.g. bad token)."""


# --------------------------------------------------------------------------
# Layer 1: pure data shaping (no network, easy to unit test)
# --------------------------------------------------------------------------
USERNAME_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")


def is_valid_username(username: Any) -> bool:
    """True if ``username`` looks like a GitHub login (1-39 chars, letters/digits/hyphens,
    not starting with a hyphen). fullmatch avoids the trailing-newline quirk of ``$``."""
    return isinstance(username, str) and USERNAME_PATTERN.fullmatch(username) is not None


def parse_github_date(value: str) -> datetime:
    """Parse GitHub's ISO 8601 timestamps such as ``2024-05-01T10:00:00Z``."""
    # fromisoformat() does not accept a trailing "Z" before Python 3.11.
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def language_percentages(languages: dict[str, int]) -> dict[str, float]:
    """Convert ``{language: bytes}`` into ``{language: percent}`` (2 decimals)."""
    total = sum(languages.values())
    if total == 0:
        return {}
    return {lang: round(size * 100 / total, 2) for lang, size in languages.items()}


def build_repo_record(
    repo: dict[str, Any],
    languages: dict[str, int],
    commit_dates: list[str],
    window_days: int,
    now: datetime,
    counted: bool = True,
    commits_capped: bool = False,
) -> dict[str, Any]:
    """Shape one repo's raw API data into the record SkillVerify uses.

    ``commit_dates`` are ISO timestamps of commits authored by the candidate
    inside the window, newest first (the order GitHub returns them in).
    """
    if commit_dates:
        last_commit = commit_dates[0]
        source = "user_commits"
    else:
        # No commits by the user in the window. pushed_at is the best fallback,
        # but it may include other people's pushes, so we say where it came from.
        last_commit = repo.get("pushed_at")
        source = "pushed_at"

    recency_days = None
    if last_commit:
        recency_days = max((now - parse_github_date(last_commit)).days, 0)

    return {
        "name": repo["name"],
        "url": repo["html_url"],
        "is_fork": bool(repo.get("fork", False)),
        "counted": counted,  # False => listed for transparency but not used as evidence
        "primary_language": repo.get("language"),
        "stars": repo.get("stargazers_count", 0),
        "languages": languages,
        "language_percentages": language_percentages(languages),
        "size_kb": repo.get("size", 0),
        "last_commit_date": last_commit,
        "last_commit_source": source,
        # None (not 0) for repos we deliberately did not inspect, so "unknown"
        # is never confused with "no activity".
        "commits_in_window": len(commit_dates) if counted else None,
        "commits_capped": commits_capped,
        "window_days": window_days,
        "commit_recency_days": recency_days,
    }


def _failed_repo_record(repo: dict[str, Any], window_days: int, now: datetime, message: str) -> dict[str, Any]:
    """Record for a repo whose details could not be fetched: not counted as evidence."""
    record = {
        "name": repo.get("name"),
        "url": repo.get("html_url"),
        "is_fork": bool(repo.get("fork", False)),
        "counted": False,
        "primary_language": repo.get("language"),
        "stars": repo.get("stargazers_count", 0),
        "languages": {},
        "language_percentages": {},
        "size_kb": repo.get("size", 0),
        "last_commit_date": None,
        "last_commit_source": None,
        "commits_in_window": None,
        "commits_capped": False,
        "window_days": window_days,
        "commit_recency_days": None,
    }
    record["error"] = message
    return record


def build_summary(
    records: list[dict[str, Any]], total_public_repos: int
) -> dict[str, Any]:
    """Aggregate across the counted (non-excluded) repos.

    Note on ``most_recent_activity_date``: it can mix the candidate's own commit
    dates with the ``pushed_at`` fallback, and ``pushed_at`` may include other
    people's pushes. For scoring, only trust a repo's last commit date when
    ``last_commit_source == "user_commits"``.
    """
    counted = [r for r in records if r["counted"]]

    combined: dict[str, int] = {}
    for record in counted:
        for lang, size in record["languages"].items():
            combined[lang] = combined.get(lang, 0) + size
    # Sort biggest first so "top language" is simply the first entry.
    combined = dict(sorted(combined.items(), key=lambda item: item[1], reverse=True))

    dates = [r["last_commit_date"] for r in counted if r["last_commit_date"]]
    most_recent = max(dates, key=parse_github_date) if dates else None

    return {
        "total_public_repos": total_public_repos,
        "counted_repos": len(counted),
        "forked_repos_excluded": sum(
            1 for r in records if r["is_fork"] and not r["counted"] and "error" not in r
        ),
        "repos_failed": sum(1 for r in records if "error" in r),
        "languages_combined": combined,
        "languages_ranked": list(combined.keys()),
        "most_recent_activity_date": most_recent,
        "total_commits_in_window": sum(r["commits_in_window"] or 0 for r in counted),
        "active_repos_in_window": sum(1 for r in counted if (r["commits_in_window"] or 0) > 0),
    }


def _empty_summary(total_public_repos: int = 0) -> dict[str, Any]:
    return build_summary([], total_public_repos)


# --------------------------------------------------------------------------
# Layer 2: the validator (network access)
# --------------------------------------------------------------------------
class GitHubValidator:
    """Fetches evidence of a user's real GitHub work.

    Usage::

        validator = GitHubValidator()          # reads GITHUB_TOKEN
        result = validator.validate("octocat")
    """

    def __init__(
        self,
        token: Optional[str] = None,
        window_days: int = DEFAULT_WINDOW_DAYS,
        include_forks: bool = False,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_seconds: float = 1.0,
        max_commit_pages: int = DEFAULT_MAX_COMMIT_PAGES,
        now: Optional[datetime] = None,
    ) -> None:
        # Load .env first so a developer's local file works without exporting
        # anything. Real environment variables take priority (override=False).
        load_dotenv()
        self._token = token or os.environ.get("GITHUB_TOKEN")
        if not self._token:
            # Fail loudly: silently going unauthenticated would cap us at
            # ~60 requests/hour and cause confusing "random" failures.
            raise GitHubConfigError(
                "GITHUB_TOKEN is not set. Create a personal access token at "
                "https://github.com/settings/tokens, then either set the "
                "GITHUB_TOKEN environment variable or put "
                "GITHUB_TOKEN=your_token in a .env file (see .env.example)."
            )

        self.window_days = window_days
        self.include_forks = include_forks
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self.max_commit_pages = max_commit_pages
        self._fixed_now = now  # lets tests freeze time

        # One shared Session reuses the TCP connection, which is much faster.
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "SkillVerify-GitHubValidator",
            }
        )
        self._rate_remaining: Optional[int] = None
        self._rate_reset: Optional[str] = None

    # -- low-level HTTP ----------------------------------------------------
    def _now(self) -> datetime:
        return self._fixed_now or datetime.now(timezone.utc)

    def _format_reset(self) -> str:
        """Human-readable reset time from the X-RateLimit-Reset header."""
        if self._rate_reset and self._rate_reset.isdigit():
            reset = datetime.fromtimestamp(int(self._rate_reset), tz=timezone.utc)
            return reset.strftime("%Y-%m-%d %H:%M:%S UTC")
        return "unknown time"

    def _record_rate_limit(self, response: requests.Response) -> None:
        remaining = response.headers.get("X-RateLimit-Remaining")
        if remaining is not None and remaining.isdigit():
            self._rate_remaining = int(remaining)
        self._rate_reset = response.headers.get("X-RateLimit-Reset", self._rate_reset)

    def _get(
        self,
        path: str,
        params: Optional[dict[str, Any]] = None,
        allowed_statuses: tuple[int, ...] = (),
    ) -> requests.Response:
        """GET with timeout, retry/backoff and rate-limit awareness.

        Statuses in ``allowed_statuses`` are returned to the caller instead of
        raising (used for 409 = empty repository).
        """
        # Stop early rather than hammering the API once we are nearly out.
        if self._rate_remaining is not None and self._rate_remaining < MIN_RATE_LIMIT_REMAINING:
            raise _RateLimitError(
                f"Only {self._rate_remaining} GitHub API calls left; stopping early. "
                f"Limit resets at {self._format_reset()}."
            )

        url = f"{API_BASE}{path}"
        last_problem = "unknown error"
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as exc:
                # Deliberately do not include headers/URL params that could hold secrets.
                last_problem = f"network error ({type(exc).__name__})"
                self._sleep_backoff(attempt)
                continue
            except requests.RequestException as exc:
                # Anything else (bad URL, too many redirects ...) will not fix itself.
                raise _ApiError(f"Request failed ({type(exc).__name__}).") from None

            self._record_rate_limit(response)
            status = response.status_code

            if status in allowed_statuses or status == 200:
                return response
            if status == 404:
                raise _NotFoundError(path)
            if status == 401:
                raise _FatalApiError(
                    "GitHub rejected the token (invalid or expired). "
                    "Create a new token and update GITHUB_TOKEN."
                )
            if status in (403, 429):
                if self._rate_remaining == 0:  # primary rate limit
                    raise _RateLimitError(
                        f"GitHub API rate limit reached. Limit resets at {self._format_reset()}."
                    )
                secondary = self._secondary_limit_message(response)
                if secondary:
                    raise _RateLimitError(secondary)
                if status == 429:
                    # GitHub uses 429 only for "too many requests", so it is always
                    # a rate limit, even with no Retry-After header or body hint.
                    raise _RateLimitError(
                        "GitHub is rate limiting requests (HTTP 429). Wait about a minute and try again."
                    )
            if status >= 500:
                last_problem = f"GitHub server error (HTTP {status})"
                self._sleep_backoff(attempt)
                continue
            # 401 (bad token), 422, other 403 ... retrying will not help.
            raise _ApiError(f"Unexpected HTTP {status} from GitHub for {path}.")

        raise _ApiError(f"Giving up after {self.max_retries + 1} attempts: {last_problem}.")

    @staticmethod
    def _secondary_limit_message(response: requests.Response) -> Optional[str]:
        """Message if this 403/429 is a secondary (abuse) rate limit, else None.

        Signals: a Retry-After header, or a body message that mentions it.
        We only report the wait; sleeping here could block for minutes.
        """
        retry_after = response.headers.get("Retry-After")
        try:
            body_message = str(response.json().get("message", "")).lower()
        except (ValueError, AttributeError):
            body_message = ""
        if retry_after is None and "secondary" not in body_message and "abuse" not in body_message:
            return None
        if retry_after and retry_after.strip().isdigit():
            return f"GitHub secondary rate limit hit. Wait {retry_after.strip()} seconds before retrying."
        return "GitHub secondary rate limit hit. Wait a few minutes before retrying."

    def _sleep_backoff(self, attempt: int) -> None:
        if attempt < self.max_retries and self.backoff_seconds > 0:
            time.sleep(self.backoff_seconds * (2**attempt))

    @staticmethod
    def _json(response: requests.Response) -> Any:
        """Decode a JSON body; a bad body is an API error and is never retried."""
        try:
            return response.json()
        except ValueError:  # requests' JSONDecodeError is a ValueError
            raise _ApiError("GitHub returned a response that is not valid JSON.") from None

    # -- endpoint wrappers -------------------------------------------------
    def fetch_profile(self, username: str) -> dict[str, Any]:
        return self._json(self._get(f"/users/{username}"))

    def fetch_repos(self, username: str) -> list[dict[str, Any]]:
        """All public repos owned by the user, following pagination."""
        repos: list[dict[str, Any]] = []
        page = 1
        while True:
            batch = self._json(self._get(
                f"/users/{username}/repos",
                params={"type": "owner", "per_page": 100, "page": page},
            ))
            if not batch:  # an empty page means we have everything
                break
            repos.extend(batch)
            page += 1
        return repos

    def fetch_languages(self, owner: str, repo: str) -> dict[str, int]:
        return self._json(self._get(f"/repos/{owner}/{repo}/languages")) or {}

    def fetch_user_commit_dates(self, owner: str, repo: str, username: str) -> tuple[list[str], bool]:
        """Dates of commits authored by ``username`` in the window, newest first.

        Returns ``(dates, capped)``; ``capped`` is True if we stopped at the
        page limit and there may be more commits.
        """
        since = (self._now() - timedelta(days=self.window_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        dates: list[str] = []
        for page in range(1, self.max_commit_pages + 1):
            response = self._get(
                f"/repos/{owner}/{repo}/commits",
                # `author` matches the GitHub login, which is our ownership signal.
                params={"author": username, "since": since, "per_page": 100, "page": page},
                allowed_statuses=(409,),
            )
            if response.status_code == 409:  # empty repository: valid, zero commits
                return [], False
            batch = self._json(response)
            dates.extend(c["commit"]["author"]["date"] for c in batch)
            if len(batch) < 100:  # last page reached
                return dates, False
        return dates, True

    # -- main entry point --------------------------------------------------
    def validate(self, username: str) -> dict[str, Any]:
        """Run the full validation. Never raises for API-level problems;
        inspect ``result["status"]`` instead."""
        generated_at = self._now().isoformat()
        result: dict[str, Any] = {
            "username": username if isinstance(username, str) else repr(username),
            "status": "ok",
            "message": "",
            "profile": None,
            "repos": [],
            "summary": _empty_summary(),
            "generated_at": generated_at,
            "window_days": self.window_days,
        }

        # Usernames will come from untrusted CV text, so check before the name
        # goes anywhere near a URL. No API call is made for a bad name.
        if not is_valid_username(username):
            result["status"] = "error"
            result["message"] = f"Invalid GitHub username: {str(username)[:50]!r}."
            return result

        try:
            profile = self.fetch_profile(username)
            result["profile"] = {
                "name": profile.get("name"),
                "url": profile.get("html_url"),
                "public_repos": profile.get("public_repos", 0),
                "created_at": profile.get("created_at"),
            }

            raw_repos = self.fetch_repos(username)
            if not raw_repos:
                result["status"] = "no_public_repos"
                result["message"] = f"{username} has no public repositories."
                return result

            records = [self._inspect_repo(username, repo) for repo in raw_repos]
            result["repos"] = records
            result["summary"] = build_summary(records, total_public_repos=len(raw_repos))
            if result["summary"]["counted_repos"] == 0:
                if result["summary"]["repos_failed"] > 0:
                    result["message"] = "No repository could be inspected; see the 'error' field on each repo."
                else:
                    result["message"] = (
                        "All public repositories are forks, so none count as evidence "
                        "(set include_forks=True to override)."
                    )

        except _NotFoundError:
            result["status"] = "user_not_found"
            result["message"] = f"GitHub user '{username}' does not exist."
            result["profile"] = None
        except _RateLimitError as exc:
            result["status"] = "rate_limited"
            result["message"] = str(exc)
            result["repos"] = []  # partial data could mislead scoring
            result["summary"] = _empty_summary()
        except _ApiError as exc:
            result["status"] = "error"
            result["message"] = str(exc)
            result["repos"] = []
            result["summary"] = _empty_summary()
        except Exception as exc:  # noqa: BLE001 - last-resort safety net
            # Contract: validate() never raises for API/data problems.
            result["status"] = "error"
            result["message"] = f"Unexpected error while processing GitHub data ({type(exc).__name__})."
            result["repos"] = []
            result["summary"] = _empty_summary()
        return result

    def _inspect_repo(self, username: str, repo: dict[str, Any]) -> dict[str, Any]:
        """Fetch languages + commits for one repo and shape the record."""
        is_fork = bool(repo.get("fork", False))
        counted = self.include_forks or not is_fork
        if not counted:
            # Skip extra API calls for forks we will not count: saves rate limit.
            return build_repo_record(repo, {}, [], self.window_days, self._now(), counted=False)

        try:
            owner = repo.get("owner", {}).get("login", username)
            languages = self.fetch_languages(owner, repo["name"])
            dates, capped = self.fetch_user_commit_dates(owner, repo["name"], username)
            return build_repo_record(
                repo, languages, dates, self.window_days, self._now(), counted=True, commits_capped=capped
            )
        except (_RateLimitError, _FatalApiError):
            raise  # global problems (rate limit, bad token) must stop the whole run
        except _NotFoundError:
            return _failed_repo_record(repo, self.window_days, self._now(), "repository not found (HTTP 404)")
        except _ApiError as exc:
            return _failed_repo_record(repo, self.window_days, self._now(), str(exc))
        except Exception as exc:  # noqa: BLE001 - one odd repo must not kill the run
            return _failed_repo_record(
                repo, self.window_days, self._now(), f"unexpected error ({type(exc).__name__})"
            )


# --------------------------------------------------------------------------
# Layer 3: helpers
# --------------------------------------------------------------------------
def validate_github_user(username: str, **options: Any) -> dict[str, Any]:
    """Convenience wrapper: ``validate_github_user("octocat", window_days=90)``."""
    return GitHubValidator(**options).validate(username)


def save_result_to_json(result: dict[str, Any], path: str | Path) -> Path:
    """Write a validator result to a JSON file (creating folders if needed)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2, ensure_ascii=False)
    return path
