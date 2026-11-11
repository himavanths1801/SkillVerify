"""Guarded growth of the skill taxonomy (SkillVerify, Step 3).

When a job description names a skill the taxonomy does not know, the LLM may propose
a filing for it. The LLM is NOT trusted: ``evaluate_proposal`` applies deterministic
guards in code, and only a proposal that passes every guard is added automatically.
Everything else goes to a human review queue.

Auto-added skills are NEVER written to ``skills.json`` (or any shipped file). They go
to an overlay file, ``skills_learned.json``, which is created only on the first real
auto-add. ``load_jd_taxonomy`` = extended taxonomy + that overlay.
"""
from __future__ import annotations

import contextlib
import copy
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from skillverify.taxonomy import CATEGORIES, Taxonomy, TaxonomyError, make_key
from skillverify.taxonomy_extra import load_extended_taxonomy

# --- Guard thresholds (change them here, nowhere else) -------------------
AUTO_ADD_MIN_CONFIDENCE = 0.9   # the model must be at least this sure (0-1)
MAX_AUTO_ADDS_PER_RUN = 3       # at most this many automatic additions per JD
MAX_ALIASES_PER_SKILL = 5       # extra spellings kept for a new skill
MAX_NAME_LENGTH = 60            # longest canonical name or alias

_SKILLVERIFY_DIR = Path(__file__).resolve().parent.parent
LEARNED_PATH = _SKILLVERIFY_DIR / "data" / "skills_learned.json"
QUEUE_PATH = _SKILLVERIFY_DIR.parent / "output" / "taxonomy_review_queue.json"

_NAME_RE = re.compile(r"[A-Za-z0-9 .+#\-_/&]+")
_MIN_SHARED_PREFIX = 4


class QueueError(Exception):
    """Raised when the review queue file is unreadable or corrupt (it is never overwritten)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def squash(text: str) -> str:
    """Collapse whitespace and ignore case, for 'is this text in the JD?' checks."""
    return " ".join(text.split()).casefold()


def contains_term(haystack_squashed: str, term: str) -> bool:
    """Whole-term match on already-squashed text.

    The term must not be glued to a letter, digit or underscore on either side, and must not be
    followed by '+' or '#'. So "Java" is not found in "JavaScript", "Go" not in "Google", "R" not in
    "Required", and "C" not in "C++" or "C#", while "Python" is still found in "(Python)", "python."
    and "Python/Django". Punctuation next to the term is fine; line breaks and case are ignored
    (squash has already collapsed them). A plain substring test would let the model invent skills.
    """
    needle = squash(term)
    if not needle:
        return False
    pattern = r"(?<!\w)" + re.escape(needle) + r"(?![\w+#])"
    return re.search(pattern, haystack_squashed, re.IGNORECASE) is not None


def _atomic_write_json(path: Path, data: Any) -> None:
    """Write via a temp file in the same folder + os.replace, so a crash never leaves half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle_fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp_name)
        raise


# --------------------------------------------------------------------------
# The learned-skills overlay
# --------------------------------------------------------------------------
def read_overlay(path: Optional[str | Path] = None) -> dict[str, list]:
    """The overlay file as ``{"skills": [...], "aliases": [...]}``. A missing or empty file is
    fine; a corrupt one raises ``TaxonomyError`` (it is never silently ignored)."""
    file_path = Path(path) if path is not None else LEARNED_PATH
    try:
        text = file_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"skills": [], "aliases": []}
    except (OSError, ValueError) as exc:
        raise TaxonomyError(f"Could not read learned skills file {file_path}: {exc}") from None
    if not text.strip():
        return {"skills": [], "aliases": []}
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise TaxonomyError(f"Learned skills file is not valid JSON ({file_path}): {exc}") from None
    if not isinstance(raw, dict):
        raise TaxonomyError(f"Learned skills file {file_path} must contain a JSON object.")
    skills, aliases = raw.get("skills", []), raw.get("aliases", [])
    if not isinstance(skills, list) or not isinstance(aliases, list):
        raise TaxonomyError(f"Learned skills file {file_path}: 'skills' and 'aliases' must be lists.")
    return {"skills": skills, "aliases": aliases}


def _taxonomy_from_overlay(overlay: dict[str, list]) -> Taxonomy:
    """Extended taxonomy + overlay. Only the fields the taxonomy accepts are passed on;
    provenance fields (date, source_jd ...) stay in the overlay file."""
    skills = copy.deepcopy(load_extended_taxonomy().skills)  # deep copy: never mutate shared records
    for position, entry in enumerate(overlay["skills"], start=1):
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not entry["id"].strip():
            raise TaxonomyError(f"Learned skill #{position} has no 'id'.")
        if entry.get("category") not in CATEGORIES:
            raise TaxonomyError(f"Learned skill '{entry['id']}' has an invalid category.")
        aliases = entry.get("aliases", [])
        if not isinstance(aliases, list) or not all(isinstance(a, str) for a in aliases):
            raise TaxonomyError(f"Learned skill '{entry['id']}': 'aliases' must be a list of strings.")
        skills.append({"id": entry["id"], "category": entry["category"],
                       "aliases": list(aliases), "github_languages": []})
    by_id = {s["id"]: s for s in skills}
    for position, entry in enumerate(overlay["aliases"], start=1):
        target = by_id.get(entry.get("skill")) if isinstance(entry, dict) else None
        alias = entry.get("alias") if isinstance(entry, dict) else None
        if target is None or not isinstance(alias, str) or not alias.strip():
            raise TaxonomyError(f"Learned alias #{position} is invalid (unknown target skill or empty alias).")
        target["aliases"].append(alias)
    return Taxonomy(skills)  # the constructor refuses id and alias collisions


def load_jd_taxonomy(learned_path: Optional[str | Path] = None) -> Taxonomy:
    """Extended taxonomy (165 base + 3 extra) plus any learned skills and aliases."""
    return _taxonomy_from_overlay(read_overlay(learned_path))


# --------------------------------------------------------------------------
# Proposal evaluation (all guards are deterministic code)
# --------------------------------------------------------------------------
def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _sane_name(text: Any) -> bool:
    """Guard 4: non-empty, short, only letters/digits/space and . + # - _ / &."""
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    return (0 < len(stripped) <= MAX_NAME_LENGTH and _NAME_RE.fullmatch(stripped) is not None
            and any(c.isalnum() for c in stripped))


def _names_overlap(a: str, b: str) -> bool:
    """Keys overlap if one contains the other or they share a prefix of 4+ characters."""
    ka, kb = make_key(a), make_key(b)
    if not ka or not kb:
        return False
    return ka in kb or kb in ka or len(os.path.commonprefix([ka, kb])) >= _MIN_SHARED_PREFIX


def _queue_decision(reason: str, proposal: Any = None) -> dict[str, Any]:
    name = category = None
    if isinstance(proposal, dict):
        if isinstance(proposal.get("canonical_name"), str):
            name = proposal["canonical_name"].strip()[:MAX_NAME_LENGTH] or None
        if proposal.get("category") in CATEGORIES:
            category = proposal["category"]
    return {"action": "queue", "reason": reason, "suggested_name": name, "suggested_category": category}


def _clean_aliases(raw: Any, name: str, term: str, taxonomy: Taxonomy) -> list[str]:
    """Guard 8: the JD wording first, then the model's aliases; invalid, duplicate or
    colliding ones are dropped (not fatal); at most MAX_ALIASES_PER_SKILL are kept."""
    candidates = [term] + (list(raw) if isinstance(raw, list) else [])
    seen = {make_key(name)}
    kept: list[str] = []
    for candidate in candidates:
        if not _sane_name(candidate):
            continue
        text = candidate.strip()
        key = make_key(text)
        if not key or key in seen or taxonomy.normalize(text)["status"] == "matched":
            continue
        seen.add(key)
        kept.append(text)
        if len(kept) >= MAX_ALIASES_PER_SKILL:
            break
    return kept


def evaluate_proposal(proposal: Any, term: str, jd_text: str, taxonomy: Taxonomy,
                      auto_adds_so_far: int) -> dict[str, Any]:
    """Decide what to do with the model's proposal for an unmapped ``term``.

    Returns ``{"action": "auto_add_skill" | "auto_add_alias" | "queue", "reason": str, ...}``.
    Any failed guard means ``queue``, with a reason that names the guard.
    """
    if not isinstance(proposal, dict):
        return _queue_decision("malformed proposal")
    if proposal.get("is_technical_skill") is not True:
        return _queue_decision("guard 1 failed: the model did not confirm a technical skill", proposal)
    confidence = proposal.get("confidence")
    if not _is_number(confidence) or not 0 <= confidence <= 1:
        return _queue_decision("guard 2 failed: confidence is not a number between 0 and 1", proposal)
    if confidence < AUTO_ADD_MIN_CONFIDENCE:
        return _queue_decision(
            f"guard 2 failed: confidence {confidence} is below {AUTO_ADD_MIN_CONFIDENCE}", proposal)
    if not contains_term(squash(jd_text), term):
        return _queue_decision("guard 5 failed: the term was not found in the job description", proposal)
    if auto_adds_so_far >= MAX_AUTO_ADDS_PER_RUN:
        return _queue_decision(f"guard 9: per-run cap of {MAX_AUTO_ADDS_PER_RUN} automatic additions reached",
                               proposal)

    target_name = proposal.get("alias_of")
    if target_name not in (None, ""):
        return _evaluate_alias(proposal, target_name, term, taxonomy, confidence)

    if proposal.get("category") not in CATEGORIES:
        return _queue_decision("guard 3 failed: the category is not one of the taxonomy categories", proposal)
    name = proposal.get("canonical_name")
    if not _sane_name(name):
        return _queue_decision("guard 4 failed: the canonical name is empty, too long or has odd characters",
                               proposal)
    name = name.strip()
    if not make_key(name) or taxonomy.normalize(name)["status"] == "matched":
        return _queue_decision("guard 6 failed: the name collides with an existing skill or alias", proposal)
    for text in (name, term):
        suggestions = taxonomy.normalize(text).get("suggestions")
        if suggestions:
            return _queue_decision(
                f"guard 7 failed: '{text}' is a near-duplicate of existing skill(s): {', '.join(suggestions)}",
                proposal)
    return {"action": "auto_add_skill", "reason": "all guards passed", "name": name,
            "category": proposal["category"], "aliases": _clean_aliases(proposal.get("aliases"), name, term, taxonomy),
            "confidence": confidence}


def _evaluate_alias(proposal: dict, target_name: Any, term: str, taxonomy: Taxonomy,
                    confidence: float) -> dict[str, Any]:
    """The term is another spelling of an existing skill: only the term is added, as an alias."""
    target = taxonomy.get(target_name) if isinstance(target_name, str) else None
    if target is None:
        return _queue_decision("alias_of does not name an existing canonical skill", proposal)
    alias = term.strip()
    if not _sane_name(alias):
        return _queue_decision("guard 4 failed: the alias text is empty, too long or has odd characters", proposal)
    if taxonomy.normalize(alias)["status"] == "matched":
        return _queue_decision("guard 6 failed: the alias already matches an existing skill", proposal)
    if not _names_overlap(alias, target["id"]):
        return _queue_decision("alias target unrelated by name", proposal)
    return {"action": "auto_add_alias", "reason": "all guards passed", "skill": target["id"],
            "alias": alias, "confidence": confidence}


def apply_auto_add(decision: dict[str, Any], learned_path: Optional[str | Path],
                   meta: dict[str, Any]) -> dict[str, Any]:
    """Add an approved decision to the overlay. The new taxonomy is built in memory FIRST;
    only if that works is the file written (atomically). Returns ``{"applied": bool, "reason": str}``;
    on any failure nothing is left half-written and the caller queues the proposal instead."""
    path = Path(learned_path) if learned_path is not None else LEARNED_PATH
    provenance = {"added_by": "jd_parser", "date": meta.get("date") or _now(),
                  "source_jd": meta.get("source_jd"), "confidence": decision.get("confidence"),
                  "evidence": meta.get("evidence")}
    try:
        overlay = read_overlay(path)
        if decision.get("action") == "auto_add_skill":
            overlay["skills"].append({"id": decision["name"], "category": decision["category"],
                                      "aliases": list(decision["aliases"]), "github_languages": [], **provenance})
        elif decision.get("action") == "auto_add_alias":
            overlay["aliases"].append({"skill": decision["skill"], "alias": decision["alias"], **provenance})
        else:
            return {"applied": False, "reason": "the decision is not an automatic addition"}
        _taxonomy_from_overlay(overlay)  # must build before anything is written
        _atomic_write_json(path, overlay)
    except (TaxonomyError, OSError, KeyError, TypeError, ValueError) as exc:
        return {"applied": False, "reason": f"automatic addition failed: {exc}"}
    return {"applied": True, "reason": "added"}


# --------------------------------------------------------------------------
# The review queue
# --------------------------------------------------------------------------
def load_queue(path: Optional[str | Path] = None) -> list[dict[str, Any]]:
    """Entries of the review queue. A missing or empty file is an empty queue; a corrupt one
    raises ``QueueError`` (and is never overwritten)."""
    file_path = Path(path) if path is not None else QUEUE_PATH
    try:
        text = file_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        raise QueueError(f"Could not read review queue {file_path}: {exc}") from None
    if not text.strip():
        return []
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise QueueError(f"Review queue {file_path} is not valid JSON: {exc}") from None
    entries = raw.get("entries") if isinstance(raw, dict) else None
    if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
        raise QueueError(f"Review queue {file_path} does not have the expected shape.")
    return entries


def add_to_queue(entries: list[dict[str, Any]], path: Optional[str | Path] = None,
                 now: Optional[str] = None) -> list[dict[str, Any]]:
    """Add terms to the one accumulating queue, deduplicated by ``make_key(term)``.

    Re-queueing a term increments ``seen_count`` and merges ``source_jds``."""
    file_path = Path(path) if path is not None else QUEUE_PATH
    queue = load_queue(file_path)  # raises before anything is written if the file is corrupt
    by_key = {e.get("key"): e for e in queue}
    stamp = now or _now()
    for item in entries:
        term = item.get("term")
        key = make_key(term) if isinstance(term, str) else ""
        if not key:
            continue
        current = by_key.get(key)
        if current is None:
            current = {"term": term, "key": key, "suggested_category": None, "suggested_name": None,
                       "reason": "", "seen_count": 0, "source_jds": [], "first_seen": stamp, "last_seen": stamp}
            queue.append(current)
            by_key[key] = current
        current["seen_count"] = int(current.get("seen_count", 0)) + 1
        source = item.get("source_jd")
        jds = current.setdefault("source_jds", [])
        if source and source not in jds:
            jds.append(source)
        current["last_seen"] = stamp
        current["reason"] = item.get("reason") or current.get("reason", "")
        for field in ("suggested_category", "suggested_name"):
            if item.get(field) and not current.get(field):
                current[field] = item[field]
    _atomic_write_json(file_path, {"version": 1, "entries": queue})
    return queue
