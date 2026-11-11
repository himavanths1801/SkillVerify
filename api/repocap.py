"""Limit how many repositories one analysis inspects, without touching the frozen validator or scanner.

The validator lists ALL of an account's repos and then makes at least two GitHub calls for every counted one, one
after the other; the scanner then makes many more per repo. On an account with over a hundred repos that alone can
use the whole time limit. ``limit_listing`` wraps the ``fetch_repos`` method of the validator instance so the
listing it works from holds only the newest ``cap`` non-fork, non-empty repos. Everything else passes through.
"""
from typing import Any, Optional


def is_eligible(repo: Any) -> bool:
    """A repo that can count as evidence: not a fork, and not empty (GitHub reports ``size`` 0 for an empty repo)."""
    return isinstance(repo, dict) and not repo.get("fork") and repo.get("size") != 0


def newest_first(repos: list[dict], cap: int) -> list[dict]:
    """The ``cap`` newest repos by ``pushed_at`` (a missing date is oldest); equal dates are ordered by name."""
    ordered = sorted(repos, key=lambda r: (str(r.get("name") or "").lower(), str(r.get("name") or "")))
    ordered.sort(key=lambda r: str(r.get("pushed_at") or ""), reverse=True)  # stable: ties keep the name order
    return ordered[:cap]


class Listing:
    """What the wrapped listing call saw. Numbers only."""

    def __init__(self, cap: int) -> None:
        self.cap = cap
        self.total: Optional[int] = None   # eligible repos before the cap
        self.used: Optional[int] = None    # eligible repos the scan received

    def meta(self) -> dict:
        capped = self.total is not None and self.used is not None and self.total > self.used
        return {"repo_cap": self.cap, "repos_total": self.total, "repos_used": self.used, "capped": capped}


def limit_listing(validator: Any, cap: int) -> Listing:
    """Wrap ``validator.fetch_repos`` (when it has one). An account within the cap is listed exactly as before; a
    bigger one is cut to its newest ``cap`` eligible repos. Returns the record the wrapper fills in."""
    listing = Listing(cap)
    original = getattr(validator, "fetch_repos", None)
    if not callable(original):
        return listing

    def fetch_repos(*args: Any, **kwargs: Any) -> list[dict]:
        raw = original(*args, **kwargs)
        eligible = [repo for repo in raw if is_eligible(repo)]
        listing.total = len(eligible)
        if len(eligible) <= cap:
            listing.used = len(eligible)
            return raw
        kept = newest_first(eligible, cap)
        listing.used = len(kept)
        return kept

    validator.fetch_repos = fetch_repos
    return listing
