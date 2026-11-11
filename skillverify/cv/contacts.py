"""Deterministic contact extraction for the CV parser (SkillVerify, Step 4). No LLM.

Emails, phone numbers and the GitHub username are found by CODE, not by the model: that is more
reliable, and the model then never needs to see the personal contact details (``redact_contacts``
replaces them before any text is sent out).

This module never calls the GitHub API. Whether an account exists, or is an organization rather
than a person, is for Step 1 / Step 5 to check.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional

from skillverify.github_validator import is_valid_username

MAX_REPOS_MENTIONED = 20
MAX_OTHER_LINKS = 20

# GitHub paths that are pages of the site, not user names.
RESERVED_PATHS = frozenset((
    "about features pricing login join settings marketplace sponsors orgs topics collections explore "
    "notifications pulls issues enterprise security apps readme site contact customer-stories team "
    "trending new codespaces copilot solutions resources actions pages projects desktop").split())

# Words that can follow "GitHub:" without being a user name ("GitHub: GitHub", "GitHub: link").
# It also holds the names of other sites and fields ("GitHub | LinkedIn"), which are labels, not users.
LABEL_STOPWORDS = frozenset((
    "github link profile url here website page account repo repository repositories "
    "username user handle site portfolio linkedin twitter x facebook instagram youtube medium kaggle leetcode "
    "hackerrank hackerearth codeforces codechef geeksforgeeks gitlab bitbucket stackoverflow behance dribbble "
    "email mail phone mobile contact resume cv projects repos none na n/a").split())

# --------------------------------------------------------------------------
# email
# --------------------------------------------------------------------------
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")
_NOT_AN_EMAIL = {"git@github.com"}  # the ssh clone form, not a person


def find_emails(text: str, links: Optional[Iterable[str]] = None) -> list[str]:
    """Lowercase, de-duplicated email addresses from the text and from ``mailto:`` links."""
    found: list[str] = []

    def add(address: str) -> None:
        address = address.lower()
        if address not in _NOT_AN_EMAIL and address not in found:
            found.append(address)

    for match in _EMAIL.finditer(text):
        add(match.group(0))
    for link in links or ():
        if isinstance(link, str) and link.lower().startswith("mailto:"):
            address = link[7:].split("?", 1)[0].strip()
            if _EMAIL.fullmatch(address):
                add(address)
    return found


# --------------------------------------------------------------------------
# phone
# --------------------------------------------------------------------------
# Optional "+", then digits with spaces, dots, dashes and parentheses between them. The look-behind
# keeps us out of URLs and identifiers (".../919876543210", "id=123...", "abc123...").
_PHONE = re.compile(r"(?<![\w/=?#.@\-])(\+\s?)?((?:\(\d{1,5}\)|\d)[\d\s().\-]{6,22}\d)(?!\w)")
_YEAR = re.compile(r"(?:19|20)\d{2}")
_DATE = re.compile(r"\d{1,2}[-./ ]\d{1,2}[-./ ]\d{2,4}|\d{4}[-./ ]\d{1,2}[-./ ]\d{1,2}")
MIN_PHONE_DIGITS, MAX_PHONE_DIGITS = 8, 15


def _looks_like_a_phone(candidate: str, plus: bool) -> bool:
    digits = re.sub(r"\D", "", candidate)
    if not MIN_PHONE_DIGITS <= len(digits) <= MAX_PHONE_DIGITS:
        return False
    groups = re.findall(r"\d+", candidate)
    if all(_YEAR.fullmatch(g) for g in groups):  # "2021 - 2025", "2019 2023"
        return False
    if _DATE.fullmatch(candidate.strip()):  # "15-08-2023", "2023-08-15"
        return False
    separators = re.sub(r"[\d\s]", "", candidate)
    if separators and set(separators) == {"."} and len(groups) >= 4:  # a dotted version / address
        return False
    if not plus and re.fullmatch(r"\d+", candidate):  # a bare run of digits: only phone-sized ones
        return 10 <= len(digits) <= 12
    return True


def find_phones(text: str, links: Optional[Iterable[str]] = None) -> list[dict[str, str]]:
    """Phone numbers as ``{"as_written", "normalized"}`` (normalized = digits with an optional leading +)."""
    found: list[dict[str, str]] = []

    def add(as_written: str, plus: bool) -> None:
        normalized = ("+" if plus else "") + re.sub(r"\D", "", as_written)
        if all(entry["normalized"] != normalized for entry in found):
            found.append({"as_written": as_written, "normalized": normalized})

    for match in _PHONE.finditer(text):
        plus, body = bool(match.group(1)), match.group(2)
        if _looks_like_a_phone(body, plus):
            add(match.group(0).strip(), plus)
    for link in links or ():
        if isinstance(link, str) and link.lower().startswith("tel:"):
            number = link[4:].strip()
            digits = re.sub(r"\D", "", number)
            if MIN_PHONE_DIGITS <= len(digits) <= MAX_PHONE_DIGITS:
                add(number, number.startswith("+"))
    return found


def redact_contacts(text: str, emails: Iterable[str], phones: Iterable[dict[str, str]]) -> str:
    """Replace every found email with [EMAIL] and every found phone with [PHONE].

    The redacted text is the only CV text ever sent to the LLM."""
    for address in emails:
        text = re.sub(re.escape(address), "[EMAIL]", text, flags=re.IGNORECASE)
    for phone in phones:
        text = text.replace(phone["as_written"], "[PHONE]")
    return text


# --------------------------------------------------------------------------
# GitHub
# --------------------------------------------------------------------------
_SEGMENT = r"[^\s/?#<>()\[\]{}\"'`,;|]+"
_URL = re.compile(r"(?<![\w.@\-])(?:https?://)?(?:www\.)?github\.com[/:](" + _SEGMENT + r")(?:/(" + _SEGMENT + r"))?",
                  re.IGNORECASE)
_SSH = re.compile(r"git@github\.com:(" + _SEGMENT + r")(?:/(" + _SEGMENT + r"))?", re.IGNORECASE)
_GIST = re.compile(r"(?<![\w.\-])(?:https?://)?gist\.github\.com(?:/" + _SEGMENT + r")*", re.IGNORECASE)
_PAGES = re.compile(r"(?<![\w.\-])(?:https?://)?[A-Za-z0-9\-]+\.github\.io(?:/" + _SEGMENT + r")*", re.IGNORECASE)
# "GitHub: janedoe", "GitHub - @janedoe", "GitHub | janedoe": a colon, dash, pipe or @ is REQUIRED, and the
# name must end the field, so "GitHub Actions experience" or "GitHub - Version control" give nothing.
_LABEL = re.compile(
    r"\bgithub\b(?:[ \t]+(?:profile|username|id|handle|account))?[ \t]*(?:[:|\-–—]|@)[ \t]*@?"
    r"([A-Za-z0-9][A-Za-z0-9\-]*)(?![A-Za-z0-9./_\-])(?![ \t]*:)(?=[ \t]*(?:$|[|,;•·)\]]))",
    re.IGNORECASE | re.MULTILINE)
_TRAILING = ".,;:!?)]}>'\"`"


def _clean_segment(segment: str) -> str:
    return segment.rstrip(_TRAILING)


def _clean_repo(segment: Optional[str]) -> Optional[str]:
    if not segment:
        return None
    repo = _clean_segment(segment)
    repo = repo[:-4] if repo.lower().endswith(".git") else repo
    return repo if re.fullmatch(r"[A-Za-z0-9._\-]{1,100}", repo) else None


def _empty_github(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"status": "none", "username": None, "profile_url": None, "source": None,
                            "repos_mentioned": [], "candidates": [], "other_links": []}
    base.update(over)
    return base


def find_github(text: str, links: Optional[Iterable[str]] = None) -> dict[str, Any]:
    """The candidate's GitHub username, found by code. See the module docstring and docs/CV_PARSER.md."""
    links = [link for link in (links or ()) if isinstance(link, str)]
    # (position, username, repo or None, source, is_profile_like)
    occurrences: list[tuple[int, str, Optional[str], str, bool]] = []

    def record(position: int, raw_user: str, raw_repo: Optional[str], source: str) -> None:
        user = _clean_segment(raw_user)
        if user.lower() in RESERVED_PATHS or not is_valid_username(user):
            return
        repo = _clean_repo(raw_repo)
        occurrences.append((position, user.lower(), repo, source, repo is None))

    # Visible text first (document order), then the hyperlink targets.
    for pattern in (_URL, _SSH):
        for match in pattern.finditer(text):
            record(match.start(), match.group(1), match.group(2), "text")
    for match in _LABEL.finditer(text):
        user = match.group(1)
        if user.lower() not in RESERVED_PATHS | LABEL_STOPWORDS and is_valid_username(user):
            occurrences.append((match.start(), user.lower(), None, "label", True))
    base = len(text) + 1
    for index, link in enumerate(links):
        for pattern in (_URL, _SSH):
            for match in pattern.finditer(link):
                record(base + index, match.group(1), match.group(2), "link")

    other: list[str] = []
    for source_text in [text, *links]:
        for pattern in (_GIST, _PAGES):
            for match in pattern.finditer(source_text):
                value = match.group(0).rstrip(_TRAILING)
                if value not in other and len(other) < MAX_OTHER_LINKS:
                    other.append(value)

    if any(o[3] != "label" and o[4] for o in occurrences):
        # Strong evidence (a profile-only github.com URL, in a hyperlink or in the text) beats a guessed
        # label: the weak label candidates are ignored completely (they are not even listed). A repo-only
        # URL (someone's project link) is not a profile claim, so it does not silence an explicit label.
        occurrences = [o for o in occurrences if o[3] != "label"]
    if not occurrences:
        return _empty_github(other_links=other)
    occurrences.sort(key=lambda o: o[0])
    candidates: list[str] = []
    for _, user, _, _, _ in occurrences:
        if user not in candidates:
            candidates.append(user)

    def rank(user: str) -> tuple[int, int, int]:
        mine = [o for o in occurrences if o[1] == user]
        has_profile = any(o[4] for o in mine)  # a profile-only URL (or label) beats a repo URL
        return (0 if has_profile else 1, -len(mine), candidates.index(user))

    chosen = min(candidates, key=rank)
    mine = [o for o in occurrences if o[1] == chosen]
    profile_like = [o for o in mine if o[4]] or mine
    sources = {o[3] for o in profile_like}
    source = next(s for s in ("link", "text", "label") if s in sources)  # a real hyperlink is the best evidence
    repos: list[str] = []
    for _, _, repo, _, _ in mine:
        if repo and repo not in repos and len(repos) < MAX_REPOS_MENTIONED:
            repos.append(repo)
    return {"status": "found" if len(candidates) == 1 else "ambiguous", "username": chosen,
            "profile_url": f"https://github.com/{chosen}", "source": source, "repos_mentioned": repos,
            "candidates": candidates, "other_links": other}
