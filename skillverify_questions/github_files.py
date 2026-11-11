"""Reads a repo's file tree and single file contents from GitHub. Live, plain errors, no retries (same rule as Step 1).

The token is read the same way Step 1 reads it (``GITHUB_TOKEN`` from the environment, or the .env file through
python-dotenv) and is only ever sent in a request header: it is never printed, logged or put in an error.
"""
import base64
import os
from typing import Any, Optional
from urllib.parse import quote

import requests
from dotenv import load_dotenv

API_BASE = "https://api.github.com"
TIMEOUT_SECONDS = 15

NOT_CONFIGURED, RATE_LIMITED, UNAVAILABLE = "not_configured", "rate_limited", "unavailable"


class GitHubFilesError(Exception):
    """GitHub could not be used. ``kind`` is one of NOT_CONFIGURED, RATE_LIMITED, UNAVAILABLE. The text is plain
    and safe: it never contains a token, a URL or file content."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class GitHubFiles:
    """``session`` is anything with ``get(url, params=None, headers=None, timeout=None)`` (a ``requests.Session``)."""

    def __init__(self, token: Optional[str] = None, session: Any = None, timeout: float = TIMEOUT_SECONDS) -> None:
        load_dotenv()  # real environment variables win over the .env file
        self._token = (token or os.environ.get("GITHUB_TOKEN") or "").strip()
        if not self._token:
            raise GitHubFilesError(NOT_CONFIGURED, "GITHUB_TOKEN is not set.")
        self._session = session if session is not None else requests.Session()
        self._timeout = timeout

    def __repr__(self) -> str:  # never include the token
        return "GitHubFiles()"

    def _get(self, path: str, params: Optional[dict[str, str]] = None, missing: tuple[int, ...] = (404,)) -> Any:
        """The decoded JSON, or None when the status is one of ``missing`` (a gone repo, an empty repo)."""
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/vnd.github+json",
                   "User-Agent": "SkillVerify-Questions"}
        try:
            response = self._session.get(f"{API_BASE}{path}", params=params, headers=headers, timeout=self._timeout)
        except requests.RequestException as exc:  # only the type: the text could contain the URL
            raise GitHubFilesError(UNAVAILABLE, f"GitHub could not be reached ({type(exc).__name__}).") from None
        status = response.status_code
        if status == 200:
            try:
                return response.json()
            except ValueError:
                raise GitHubFilesError(UNAVAILABLE, "GitHub sent an answer that could not be read.") from None
        if status in missing:
            return None
        if status in (403, 429):
            raise GitHubFilesError(RATE_LIMITED, "GitHub's request limit was reached.")
        if status == 401:
            raise GitHubFilesError(UNAVAILABLE, "GitHub rejected the token.")
        raise GitHubFilesError(UNAVAILABLE, f"GitHub answered HTTP {status}.")

    @staticmethod
    def _repo_path(owner: str, repo: str) -> str:
        return f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"

    def default_branch(self, owner: str, repo: str) -> Optional[str]:
        data = self._get(self._repo_path(owner, repo))
        branch = data.get("default_branch") if isinstance(data, dict) else None
        return branch if isinstance(branch, str) and branch else None

    def tree(self, owner: str, repo: str, branch: str) -> list[dict]:
        """Every entry of the repo's tree in ONE recursive call. A missing or empty repo gives an empty list."""
        data = self._get(f"{self._repo_path(owner, repo)}/git/trees/{quote(branch, safe='')}",
                         {"recursive": "1"}, missing=(404, 409))
        entries = data.get("tree") if isinstance(data, dict) else None
        return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []

    def file_text(self, owner: str, repo: str, path: str, branch: str) -> Optional[str]:
        """The decoded text of one file, or None when it is gone or not plain text."""
        data = self._get(f"{self._repo_path(owner, repo)}/contents/{quote(path, safe='/')}", {"ref": branch})
        if not isinstance(data, dict) or data.get("encoding") != "base64" or not isinstance(data.get("content"), str):
            return None
        try:
            content = data["content"].replace("\n", "")  # GitHub wraps the base64 text in lines
            return base64.b64decode(content, validate=True).decode("utf-8", errors="replace")
        except ValueError:
            return None
