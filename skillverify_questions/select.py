"""Candidates, ranking, selection and reserve. Pure: no network, no Gemini, no randomness.

The same inputs always give the same selection. Files are chosen here, by plain code; Gemini never chooses a file.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from skillverify.github_validator import parse_github_date
from skillverify_questions import config, files
from skillverify_questions.reasons import MUST_HAVE
from skillverify_questions.signals import Signals

_OLDEST = datetime.min.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class Need:
    """A JD skill that GitHub confirmed, in priority order."""
    skill: str
    tier: str
    claimed: bool


@dataclass(frozen=True)
class Repo:
    name: str
    pushed_at: Optional[str]
    default_branch: Optional[str]
    evidences: frozenset  # the needed skills the existing scan found in this repo


@dataclass(frozen=True)
class FileCandidate:
    repo: str
    path: str
    language: str
    text: str            # redacted
    logic_score: int
    signal_lines: int
    pushed_at: Optional[str]

    @property
    def key(self) -> tuple[str, str]:
        return self.repo, self.path


@dataclass(frozen=True)
class Pick:
    need: Need
    file: FileCandidate
    top_ranked: bool = False   # True only for the rank-1 file of the skill; every reserve pick is False


@dataclass
class Selection:
    picks: list[Pick] = field(default_factory=list)
    reserve: list[Pick] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)


def _time(value: Optional[str]) -> datetime:
    try:
        return parse_github_date(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, AttributeError):
        return _OLDEST


# --------------------------------------------------------------------------
# candidate skills
# --------------------------------------------------------------------------
def candidate_skills(score_skills: Iterable[dict[str, Any]]) -> tuple[list[Need], list[dict]]:
    """(needs, skipped). Must-haves first (heavier weight first, then the API order), then nice-to-haves.

    Only skills with ``github_status == "found"`` can be asked about; every other JD skill is recorded as skipped.
    """
    rows = [row for row in score_skills if isinstance(row, dict) and isinstance(row.get("skill"), str)]
    order = {id(row): index for index, row in enumerate(rows)}

    def priority(row: dict[str, Any]) -> tuple[int, float, int]:
        weight = row.get("weight") if isinstance(row.get("weight"), (int, float)) else 0
        return (0 if row.get("tier") == MUST_HAVE else 1, -float(weight), order[id(row)])

    needs: list[Need] = []
    skipped: list[dict] = []
    for row in sorted(rows, key=priority):
        if row.get("github_status") == "found":
            needs.append(Need(row["skill"], str(row.get("tier")), row.get("claimed") is True))
        else:
            reason = (config.NOT_CHECKABLE_FROM_CODE if row.get("github_status") == "not_detectable_from_github"
                      else config.NOT_FOUND_IN_CODE)
            skipped.append({"skill": row["skill"], "tier": str(row.get("tier")), "reason": reason})
    return needs, skipped


# --------------------------------------------------------------------------
# repos
# --------------------------------------------------------------------------
def choose_repos(needs: list[Need], github: Any, signals: Signals, limit: int) -> list[Repo]:
    """At most ``limit`` counted, non-fork repos: those evidencing the most needed skills first, then the most
    recently pushed, then by name. The evidence comes from the scan the analysis already ran (nothing is re-scanned)."""
    github = github if isinstance(github, dict) else {}
    tech = github.get("tech_stack") if isinstance(github.get("tech_stack"), dict) else {}
    wanted = {need.skill for need in needs}
    evidence: dict[str, set[str]] = {}
    for item in tech.get("skills", []) if isinstance(tech.get("skills"), list) else []:
        if isinstance(item, dict) and item.get("skill") in wanted and isinstance(item.get("repos"), list):
            for name in item["repos"]:
                evidence.setdefault(str(name), set()).add(item["skill"])
    branches = {p.get("name"): p.get("default_branch") for p in tech.get("per_repo", [])
                if isinstance(p, dict)} if isinstance(tech.get("per_repo"), list) else {}
    repos: list[Repo] = []
    for record in github.get("repos", []) if isinstance(github.get("repos"), list) else []:
        if not (isinstance(record, dict) and record.get("counted") and "error" not in record
                and not record.get("is_fork") and isinstance(record.get("name"), str)):
            continue
        languages = record.get("languages") if isinstance(record.get("languages"), dict) else {}
        shown = set(evidence.get(record["name"], set()))
        for need in needs:  # a skill written in a language is evidenced by that language in the repo
            wanted_languages = {k.lower() for k in languages}
            if any(name.lower() in wanted_languages
                   for name in (signals.taxonomy.get(need.skill) or {}).get("github_languages", [])):
                shown.add(need.skill)
        repos.append(Repo(record["name"], record.get("last_commit_date"), branches.get(record["name"]),
                          frozenset(shown)))
    repos.sort(key=lambda r: (-len(r.evidences), -_time(r.pushed_at).timestamp(), r.name))
    return repos[:limit]


# --------------------------------------------------------------------------
# files
# --------------------------------------------------------------------------
def filter_tree(entries: Iterable[Any]) -> list[tuple[str, int]]:
    """The (path, size) of every file in a tree that survives the path and size filter. Nothing is fetched here."""
    survivors = []
    for entry in entries:
        if isinstance(entry, dict) and entry.get("type") == "blob" and isinstance(entry.get("path"), str):
            size = entry.get("size") if isinstance(entry.get("size"), int) else 0
            if files.skip_reason(entry["path"], size) is None:
                survivors.append((entry["path"], size))
    return survivors


def fetch_order(survivors: list[tuple[str, int]], limit: int) -> list[str]:
    """Which files to fetch from one repo: larger code files first, path alphabetical as the tie-break."""
    return [path for path, _ in sorted(survivors, key=lambda item: (-item[1], item[0]))[:limit]]


def prepare_text(path: str, raw: str) -> Optional[tuple[str, str]]:
    """(language, redacted text) for a fetched file, or None when it is generated or has too little logic."""
    language = files.language_of(path)
    if language is None:
        return None
    text = files.notebook_code(raw) if files.extension_of(path) == ".ipynb" else raw
    if not text.strip() or files.is_generated(text):
        return None
    text = files.redact(text)
    if files.logic_lines(text, language) < config.MIN_LOGIC_LINES:
        return None
    return language, text


def rank_for_skill(skill: str, candidates: Iterable[FileCandidate], signals: Signals) -> list[FileCandidate]:
    """The best files for one skill, best first. Tie-break order: higher logic score, more skill-signal lines,
    repo pushed more recently, repo name and path alphabetical."""
    scored = []
    for candidate in candidates:
        lines = signals.count(skill, candidate.language, candidate.text)
        if lines > 0:
            scored.append(FileCandidate(candidate.repo, candidate.path, candidate.language, candidate.text,
                                        candidate.logic_score, lines, candidate.pushed_at))
    scored.sort(key=lambda c: (-c.logic_score, -c.signal_lines, -_time(c.pushed_at).timestamp(), c.repo, c.path))
    return scored[:config.TOP_FILES_PER_SKILL]


def select(needs: list[Need], ranked: dict[str, list[FileCandidate]]) -> Selection:
    """Each skill, in priority order, takes its best file that no earlier skill took, up to MAX_QUESTIONS."""
    result = Selection()
    used: set[tuple[str, str]] = set()
    unpicked: list[Need] = []
    for need in needs:
        options = ranked.get(need.skill, [])
        free = next((f for f in options if f.key not in used), None)
        if free is None:
            result.skipped.append({"skill": need.skill, "tier": need.tier, "reason": config.NO_USABLE_FILE})
        elif len(result.picks) >= config.MAX_QUESTIONS:
            unpicked.append(need)
            result.skipped.append({"skill": need.skill, "tier": need.tier, "reason": config.NOT_ENOUGH_ROOM})
        else:
            result.picks.append(Pick(need, free, options[0] is free))
            used.add(free.key)
    # Reserve, in this order: the best unused file of each skill that got no question; then each asked skill's
    # next-best files; then the other files of the skills that got no question.
    taken = set(used)
    for need in unpicked:
        first = next((f for f in ranked.get(need.skill, []) if f.key not in taken), None)
        if first is not None:
            result.reserve.append(Pick(need, first))
            taken.add(first.key)
    for need in [pick.need for pick in result.picks] + unpicked:
        for option in ranked.get(need.skill, []):
            if option.key not in taken:
                result.reserve.append(Pick(need, option))
                taken.add(option.key)
    return result
