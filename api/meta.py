"""Display values for the Score screen header: the candidate's name and the job title. Plain code, no LLM.

Both are short plain strings or None. They are never logged and never placed in an error.
"""
import re
from typing import Any, Optional

MAX_LENGTH = 80
MAX_TITLE_WORDS = 10
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_SEPARATORS = (" - ", " – ", " — ", " | ")
_TRAILING = " \t.,:;!-–—|"
_REJECTED_STARTS = ("about", "we ", "our ", "the ", "job description", "overview", "description")


def _clean(value: str, cap: Optional[int] = MAX_LENGTH) -> str:
    return " ".join(_CONTROL.sub(" ", value).split())[:cap].strip()


def candidate_name(cv_result: Any) -> Optional[str]:
    """The verified name from the CV parse (never the unverified ``name_candidate`` hint)."""
    candidate = cv_result.get("candidate") if isinstance(cv_result, dict) else None
    name = candidate.get("name") if isinstance(candidate, dict) else None
    if not isinstance(name, str):
        return None
    name = _clean(name)
    if not name:
        return None
    return name.title() if name.isupper() else name


def job_title(jd_text: Any) -> Optional[str]:
    """The first line of the job description, cut at a separator, if it looks like a title."""
    if not isinstance(jd_text, str):
        return None
    line = next((ln.strip() for ln in jd_text.splitlines() if ln.strip()), "")
    cuts = [line.index(sep) for sep in _SEPARATORS if sep in line]
    if cuts:
        line = line[:min(cuts)]
    title = _clean(line, None).rstrip(_TRAILING)  # too long means no title, not a cut one
    if not title or len(title) > MAX_LENGTH or len(title.split()) > MAX_TITLE_WORDS or "." in title:
        return None
    if title.lower().startswith(_REJECTED_STARTS):
        return None
    return title
