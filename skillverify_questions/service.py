"""Orchestration: choose code, read files, write and check questions, replace one. No logging of any content.

Stages (plain text, in this order): "Choosing code", "Reading files", "Writing questions", "Checking questions".
"""
import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from skillverify_questions import config, files, select
from skillverify_questions.reasons import reason_for
from skillverify_questions.signals import Signals
from skillverify_questions.writer import normalise_for_duplicates, write_question

log = logging.getLogger("skillverify.questions")   # codes and skill names only, never content

STAGE_CHOOSING, STAGE_READING ="Choosing code", "Reading files"
STAGE_WRITING, STAGE_CHECKING = "Writing questions", "Checking questions"
SCHEMA_VERSION = 1


@dataclass
class Prepared:
    """What the selection step found. Cached so a retry or a replacement never touches GitHub again."""
    selection: select.Selection
    trees: dict[str, set[str]]
    counts: dict[str, int]
    repo_texts: dict[str, list[str]] = field(default_factory=dict)   # every fetched file of a repo (redacted)
    excluded: list[dict] = field(default_factory=list)               # files kept out of the SQL ranking, with why


@dataclass
class RunState:
    """What one question run has used so far (files, duplicates, skipped skills, dropped attempts)."""
    questions: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    used: set[tuple[str, str]] = field(default_factory=set)
    earlier: set[str] = field(default_factory=set)
    dropped: int = 0
    drops: list[dict] = field(default_factory=list)   # metadata and reason codes only
    asked: set[str] = field(default_factory=set)      # skills that have a question


def new_id() -> str:
    return "q_" + secrets.token_hex(4)


def _noop(_: str) -> None:
    return None


# --------------------------------------------------------------------------
# choose code and read files (the only part that talks to GitHub)
# --------------------------------------------------------------------------
def prepare(score_skills: Any, github: Any, files_client: Any, signals: Signals,
            set_stage: Callable[[str], None] = _noop) -> Prepared:
    """Filter, fetch and rank. Raises whatever the files client raises (a plain GitHub error); nothing partial is kept."""
    set_stage(STAGE_CHOOSING)
    needs, skipped = select.candidate_skills(score_skills if isinstance(score_skills, list) else [])
    counts = {"candidates_skills": len(needs), "repos_considered": 0, "files_fetched": 0, "files_ranked": 0}
    trees: dict[str, set[str]] = {}
    if not needs:
        return Prepared(select.Selection(skipped=skipped), trees, counts)
    owner = github.get("username") if isinstance(github, dict) else None
    repos = select.choose_repos(needs, github, signals, config.MAX_REPOS_CONSIDERED) if isinstance(owner, str) else []
    counts["repos_considered"] = len(repos)

    set_stage(STAGE_READING)
    candidates: list[select.FileCandidate] = []
    repo_texts: dict[str, list[str]] = {}
    fetched = 0
    for repo in repos:
        branch = repo.default_branch or files_client.default_branch(owner, repo.name)
        if not branch:
            continue
        entries = files_client.tree(owner, repo.name, branch)
        trees[repo.name] = {e["path"] for e in entries if e.get("type") == "blob" and isinstance(e.get("path"), str)}
        room = min(config.MAX_FILES_PER_REPO_FETCHED, config.MAX_FILE_FETCHES - fetched)
        for path in select.fetch_order(select.filter_tree(entries), max(room, 0)):
            raw = files_client.file_text(owner, repo.name, path, branch)
            fetched += 1
            if raw is not None:  # kept for the SQL name check; nothing more is fetched later
                kept = files.notebook_code(raw) if files.extension_of(path) == ".ipynb" else raw
                repo_texts.setdefault(repo.name, []).append(files.redact(kept))
            prepared_text = select.prepare_text(path, raw) if raw is not None else None
            if prepared_text is not None:
                language, text = prepared_text
                candidates.append(select.FileCandidate(repo.name, path, language, text,
                                                       files.logic_score(text, language), 0, repo.pushed_at))
    counts.update(files_fetched=fetched, files_ranked=len(candidates))
    ranked = {need.skill: select.rank_for_skill(need.skill, candidates, signals) for need in needs}
    selection = select.select(needs, ranked)
    selection.skipped = skipped + selection.skipped
    excluded = []
    if any(need.skill == "SQL" for need in needs):
        excluded = [{"skill": "SQL", "repo": c.repo, "path": c.path, "reason": "no_real_sql_in_file"}
                    for c in candidates
                    if signals.count("SQL", c.language, c.text) == 0
                    and signals.count("SQL", c.language, c.text, gated=False) > 0]
    return Prepared(selection, trees, counts, repo_texts, excluded)


# --------------------------------------------------------------------------
# write and check
# --------------------------------------------------------------------------
def _question_object(pick: select.Pick, written: dict, make_id: Callable[[], str]) -> dict:
    need, file = pick.need, pick.file
    extra = {"failing_example": written["failing_example"]} if "failing_example" in written else {}
    return {"id": make_id(), "kind": written["kind"], "question": written["question"],
            "language": written["language"], "starter_code": written["starter_code"],
            **extra, "reference_notes": written["reference_notes"],
            "why": {"repo": file.repo, "path": file.path, "skill": need.skill, "tier": need.tier,
                    "claimed": need.claimed, "top_ranked": pick.top_ranked,
                    "reason": reason_for(need.skill, need.tier, need.claimed, pick.top_ranked),
                    "symbols": written["symbols"]}}


def _settle(state: RunState, pick: select.Pick, ok: bool) -> None:
    """Keep ``skipped`` honest: only skills with ZERO questions are listed, so a skill that has a question is never
    skipped, even when another attempt for it failed."""
    skill = pick.need.skill
    if ok:
        state.asked.add(skill)
        state.skipped = [s for s in state.skipped if s["skill"] != skill]
    elif skill in state.asked:
        return
    elif not any(s["skill"] == skill and s["reason"] == config.QUESTION_FAILED for s in state.skipped):
        state.skipped = [s for s in state.skipped if not (s["skill"] == skill and s["reason"] == config.NOT_ENOUGH_ROOM)]
        state.skipped.append({"skill": skill, "tier": pick.need.tier, "reason": config.QUESTION_FAILED})


def _try(prepared: Prepared, state: RunState, pick: select.Pick, provider: Any,
         make_id: Callable[[], str]) -> Optional[dict]:
    state.used.add(pick.file.key)
    try:
        written = write_question(provider, pick.file, pick.need.skill, prepared.trees.get(pick.file.repo, set()),
                                 state.earlier, prepared.repo_texts.get(pick.file.repo, ()))
    except BaseException:  # a Gemini outage says nothing about the file: it stays available for the next try
        state.used.discard(pick.file.key)
        raise
    if written.question is None:
        state.dropped += 1
        reasons = written.reasons
        state.drops.append({"skill": pick.need.skill, "tier": pick.need.tier, "repo": pick.file.repo,
                            "path": pick.file.path, "attempts": written.calls, "reasons": reasons})
        log.info("question dropped skill=%s reasons=%s", pick.need.skill, ",".join(reasons))
        _settle(state, pick, False)
        return None
    state.earlier.add(normalise_for_duplicates(written.question["question"]))
    _settle(state, pick, True)
    return _question_object(pick, written.question, make_id)


def write_all(prepared: Prepared, provider: Any, set_stage: Callable[[str], None] = _noop,
              make_id: Callable[[], str] = new_id,
              now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> tuple[dict, RunState]:
    """Write the questions for the selected files; a dropped question is replaced from the reserve."""
    state = RunState(skipped=[dict(s) for s in prepared.selection.skipped])
    target = len(prepared.selection.picks)
    queue = prepared.selection.picks + prepared.selection.reserve
    set_stage(STAGE_WRITING)
    attempted = 0
    for pick in queue:
        if len(state.questions) >= target or attempted >= config.MAX_QUESTIONS + config.MAX_REPLACEMENT_CANDIDATES:
            break
        attempted += 1
        question = _try(prepared, state, pick, provider, make_id)
        if question is not None:
            state.questions.append(question)
    set_stage(STAGE_CHECKING)
    count = len(state.questions)
    if count >= config.MIN_QUESTIONS:
        status, message = "ok", None
    elif count == 0:
        status, message = "none", config.MESSAGE_NONE
    else:
        status, message = "too_few", config.MESSAGE_TOO_FEW.format(n=count, min=config.MIN_QUESTIONS)
    result = {"schema_version": SCHEMA_VERSION, "status": status, "message": message,
              "generated_at": now().isoformat(timespec="seconds"),
              "limits": {"min": config.MIN_QUESTIONS, "max": config.MAX_QUESTIONS},
              "questions": state.questions, "skipped_skills": state.skipped,
              "dropped": [dict(d) for d in state.drops],
              "counts": {**prepared.counts, "questions": count, "dropped": len(state.drops)}}
    return result, state


def replace_one(prepared: Prepared, state: RunState, provider: Any,
                make_id: Callable[[], str] = new_id) -> Optional[tuple[dict, list[dict]]]:
    """One new question from the next unused reserve file, or None when nothing is left.

    Never uses a file already used in this run (the server does not know which questions the recruiter dropped, so
    every file used so far stays used). Up to MAX_REPLACEMENT_CANDIDATES files are tried.
    """
    tries = 0
    for pick in prepared.selection.reserve:
        if pick.file.key in state.used:
            continue
        if tries >= config.MAX_REPLACEMENT_CANDIDATES:
            break
        tries += 1
        question = _try(prepared, state, pick, provider, make_id)
        if question is not None:
            return question, state.skipped
    return None
