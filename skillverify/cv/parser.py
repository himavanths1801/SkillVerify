"""CV parser (SkillVerify, Step 4).

Reads a CV (PDF, Word or text), and returns the candidate's name, contacts, GitHub username,
education, CLAIMED skills (mapped to the shared taxonomy, with verbatim evidence and where each
was claimed) and projects. A CV makes claims, not proofs: skills carry no tier and no weight, and
nothing is scored here (Step 5 compares claims with GitHub evidence).

The LLM is NOT trusted. Code decides everything that matters:
* email, phone and GitHub username come from deterministic code (contacts.py) and the model never
  overrides them; contacts are replaced by [EMAIL] / [PHONE] before any text is sent out;
* every name, institution, skill, project and snippet must be found in the CV text, or it is dropped;
* skills are matched by the deterministic taxonomy; unknown ones go through the guarded growth of Step 3;
* at most two LLM calls are made per CV.
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from skillverify.cv import prompts as cv_prompts
from skillverify.cv.contacts import find_emails, find_github, find_phones, redact_contacts
from skillverify.cv.extract import MAX_CV_CHARS, CVExtractError, extract_text
from skillverify.jd import prompts as jd_prompts
from skillverify.jd.growth import (
    LEARNED_PATH,
    QUEUE_PATH,
    QueueError,
    add_to_queue,
    apply_auto_add,
    contains_term,
    evaluate_proposal,
    load_jd_taxonomy,
    squash,
)
from skillverify.jd.provider import LLMCallError, LLMProvider
from skillverify.taxonomy import Taxonomy, make_key

# --- Limits (change them here, nowhere else) -----------------------------
MAX_SKILLS = 80              # most distinct skills + unmapped terms kept
MAX_EDUCATION = 6            # education entries kept
MAX_PROJECTS = 10            # projects kept
MAX_EVIDENCE_CHARS = 400     # longest evidence snippet kept
MAX_EVIDENCE_ITEMS = 3       # evidence snippets kept per skill
MAX_DESCRIPTION_CHARS = 400  # longest project description kept
STUFFING_MIN_SKILLS = 40     # more distinct skills than this ...
STUFFING_MIN_CONTEXT = 0.25  # ... with less than this share having real context: keyword stuffing

_LABEL = re.compile(r"[A-Za-z0-9._-]{1,60}")
_BRACKETS = re.compile(r"\s*\([^)]*\)")  # "Python (advanced)" -> "Python" (taxonomy limitation)
_SPLIT = re.compile(r"[;,]")
_CONTEXTS = ("skills_section", "project", "experience", "education", "other")
_PLAIN_CONTEXTS = ("skills_section", "other")
_SKILL_KEYS = ("skill", "category", "claimed_in", "evidence", "claimed_as", "match")


def label_from_path(path: Any) -> str:
    """A safe default label from a file name: invalid characters become '_'."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", Path(str(path)).stem)[:60] or "cv"


def _copy_if_exists(source: Path, destination: Path) -> Path:
    if source.is_file():
        shutil.copyfile(source, destination)
    return destination


def _text_field(value: Any, squashed_text: str, limit: Optional[int] = None) -> Optional[str]:
    """A verbatim-checked free text field: kept only if it is found in the CV, else None."""
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()[:limit].strip() if limit else value.strip()
    return value if squash(value) in squashed_text else None


class _Claims:
    """Skill claims collected from the model's answer (before and after growth)."""

    def __init__(self) -> None:
        self.found: dict[str, dict[str, Any]] = {}
        self.unmapped: dict[str, dict[str, Any]] = {}
        self.rejected: list[dict[str, str]] = []


class CVParser:
    """Parse CVs into claimed skills, projects, education and contacts.

    ``persist=False`` is for dry runs: the learned-skills file and the review queue are copied to a
    temporary folder and used there, so nothing real is written. Call ``close()`` (or use ``with``)
    to remove that folder.
    """

    def __init__(
        self,
        provider: LLMProvider,
        taxonomy: Optional[Taxonomy] = None,
        learned_path: Optional[str | Path] = None,
        queue_path: Optional[str | Path] = None,
        auto_add: bool = True,
        persist: bool = True,
    ) -> None:
        self.provider = provider
        self.auto_add = auto_add
        self._scratch: Optional[tempfile.TemporaryDirectory] = None
        learned = Path(learned_path) if learned_path is not None else LEARNED_PATH
        queue = Path(queue_path) if queue_path is not None else QUEUE_PATH
        if not persist:
            self._scratch = tempfile.TemporaryDirectory(prefix="skillverify_cv_dryrun_")
            folder = Path(self._scratch.name)
            learned = _copy_if_exists(learned, folder / "skills_learned.json")
            queue = _copy_if_exists(queue, folder / "taxonomy_review_queue.json")
        self.learned_path = learned
        self.queue_path = queue
        self.taxonomy = taxonomy if taxonomy is not None else load_jd_taxonomy(self.learned_path)

    def close(self) -> None:
        if self._scratch is not None:
            self._scratch.cleanup()
            self._scratch = None

    def __enter__(self) -> "CVParser":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # -- output skeleton ---------------------------------------------------
    def _blank(self, label: Any) -> dict[str, Any]:
        valid = isinstance(label, str) and _LABEL.fullmatch(label) is not None
        return {
            "schema_version": 1, "status": "ok", "message": "", "label": label if valid else "",
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "provider": {"name": getattr(self.provider, "name", "unknown"),
                         "model": getattr(self.provider, "model", "unknown")},
            "source": {"format": "unknown", "pages": None, "characters": 0, "truncated": False, "links_found": 0},
            "llm_calls": 0,
            "candidate": {"name": None, "name_candidate": None, "emails": [], "phones": []},
            "github": {"status": "none", "username": None, "profile_url": None, "source": None,
                       "repos_mentioned": [], "candidates": [], "other_links": []},
            "education": [], "skills": [], "projects": [], "unmapped": [], "rejected": [],
            "taxonomy_changes": {"auto_added": [], "queued": []}, "warnings": [], "counts": {},
        }

    @staticmethod
    def _finish(result: dict[str, Any], status: str = "ok", message: str = "") -> dict[str, Any]:
        result["status"] = status
        if message:
            result["message"] = message
        changes = result["taxonomy_changes"]
        result["counts"] = {
            "skills": len(result["skills"]), "projects": len(result["projects"]),
            "education": len(result["education"]), "unmapped": len(result["unmapped"]),
            "rejected": len(result["rejected"]), "auto_added": len(changes["auto_added"]),
            "queued": len(changes["queued"]),
        }
        return result

    def _call(self, result: dict[str, Any], system_prompt: str, user_prompt: str) -> Any:
        result["llm_calls"] += 1
        return self.provider.generate_json(system_prompt, user_prompt)

    # -- entry points -------------------------------------------------------
    def parse_file(self, path: Any, label: Optional[str] = None) -> dict[str, Any]:
        """Extract the text of a PDF / DOCX / TXT file and parse it."""
        if not isinstance(path, (str, os.PathLike)):
            return self._finish(self._blank(label), "invalid_input", "The CV path must be a string or a path.")
        label = label if label is not None else label_from_path(path)
        result = self._blank(label)
        if not result["label"]:
            return self._finish(result, "invalid_input", self._label_message())
        try:
            extracted = extract_text(path)
        except CVExtractError as exc:
            suffix = Path(str(path)).suffix.lower().lstrip(".")
            result["source"]["format"] = suffix if suffix in ("pdf", "docx", "txt") else "unknown"
            return self._finish(result, exc.code, exc.message)
        source = {"format": extracted["format"], "pages": extracted["pages"], "characters": extracted["characters"],
                  "truncated": extracted["truncated"], "links_found": len(extracted["links"])}
        return self.parse_text(extracted["text"], label, extracted["links"], source)

    @staticmethod
    def _label_message() -> str:
        return "label must be 1-60 characters: letters, digits, '.', '_' or '-'."

    def parse_text(self, text: Any, label: Any = "cv", links: Optional[list[str]] = None,
                   source: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Parse CV text. Never raises for LLM or data problems (only ``LLMConfigError`` may propagate)."""
        result = self._blank(label)
        if not result["label"]:
            return self._finish(result, "invalid_input", self._label_message())
        if not isinstance(text, str) or not text.strip():
            return self._finish(result, "invalid_input", "The CV text must be non-empty text.")
        links = [link for link in (links or []) if isinstance(link, str)]
        truncated = len(text) > MAX_CV_CHARS or bool(source and source.get("truncated"))
        text = text[:MAX_CV_CHARS]
        result["source"] = {
            "format": source["format"] if source else "text", "pages": source["pages"] if source else None,
            "characters": len(text), "truncated": truncated, "links_found": len(links)}

        # Contacts and GitHub come from code only; the model never sees the raw contact details.
        emails, phones = find_emails(text, links), find_phones(text, links)
        github = find_github(text, links)
        result["candidate"].update(emails=emails, phones=phones)
        result["github"] = github
        warnings = result["warnings"]
        if github["status"] == "none":
            warnings.append("no_github_found")
        elif github["status"] == "ambiguous":
            warnings.append("github_ambiguous")
        if not emails:
            warnings.append("no_email_found")
        if not phones:
            warnings.append("no_phone_found")
        if truncated:
            warnings.append("truncated")

        redacted = redact_contacts(text, emails, phones)
        try:
            answer = self._call(result, cv_prompts.extraction_system_prompt(), cv_prompts.extraction_user_prompt(redacted))
        except LLMCallError as exc:
            return self._finish(result, "llm_error", str(exc))  # the code-derived parts are still returned
        if not isinstance(answer, dict) or not isinstance(answer.get("skills"), list):
            return self._finish(result, "parse_error", "The model's answer had no 'skills' list.")

        squashed = squash(redacted)
        self._read_name(answer, squashed, result)
        self._read_education(answer.get("education"), squashed, result)
        claims = _Claims()
        self._read_skills(answer["skills"], squashed, claims)  # the skills section first, then the projects
        projects, tech_terms = self._read_projects(answer.get("projects"), squashed, claims, result)
        resolved = self._resolve_unmapped(result, redacted, label, claims)
        self._assemble(result, claims, projects, tech_terms, resolved)
        return self._finish(result)

    # -- verified fields ------------------------------------------------------
    @staticmethod
    def _read_name(answer: dict[str, Any], squashed: str, result: dict[str, Any]) -> None:
        guess = answer.get("name")
        if not isinstance(guess, str) or not guess.strip():
            return
        guess = guess.strip()[:100]
        if contains_term(squashed, guess):
            result["candidate"]["name"] = guess
        else:
            result["candidate"]["name_candidate"] = guess  # kept as a hint, never trusted
            result["warnings"].append("name_not_verified")

    @staticmethod
    def _read_education(items: Any, squashed: str, result: dict[str, Any]) -> None:
        for item in items if isinstance(items, list) else []:
            institution = item.get("institution") if isinstance(item, dict) else None
            if not isinstance(institution, str) or not contains_term(squashed, institution):
                term = institution if isinstance(institution, str) else str(item)[:100]
                result["rejected"].append({"term": term, "reason": "not_in_cv"})
                continue
            if len(result["education"]) >= MAX_EDUCATION:
                result["rejected"].append({"term": institution, "reason": "over_education_cap"})
                continue
            result["education"].append({
                "institution": institution.strip(),
                **{field: _text_field(item.get(field), squashed) for field in ("degree", "field", "start", "end")}})

    # -- skills ------------------------------------------------------------
    def _claim(self, claims: _Claims, wording: str, claimed_in: str, snippets: list[str],
               hint: Optional[str]) -> tuple[list[str], list[str]]:
        """Run one claimed wording through the taxonomy. Returns (matched skill ids, unknown terms)."""
        matched: list[str] = []
        unknown: list[str] = []
        pieces = [p.strip() for p in _SPLIT.split(_BRACKETS.sub("", wording)) if p.strip()]
        if not pieces:
            claims.rejected.append({"term": wording, "reason": "empty_term"})
        for piece in pieces:
            outcome = self.taxonomy.normalize_many([piece])  # also splits "HTML/CSS" when every part matches
            for match in outcome["matched"]:
                if self._room_for(claims, match["skill"], claims.found, wording):
                    self._merge(claims.found, match["skill"], {
                        "skill": match["skill"], "category": match["category"], "claimed_in": [claimed_in],
                        "evidence": list(snippets), "claimed_as": [wording], "match": "taxonomy"})
                    matched.append(match["skill"])
            for item in outcome["unknown"]:
                term = str(item["input"])
                key = make_key(term)
                if self._room_for(claims, key, claims.unmapped, wording):
                    self._merge(claims.unmapped, key, {
                        "term": term, "key": key, "claimed_in": [claimed_in], "evidence": list(snippets),
                        "claimed_as": [wording], "suggested_name": hint})
                    unknown.append(term)
            for _bad in outcome["invalid"]:
                claims.rejected.append({"term": piece, "reason": "invalid_term"})
        return matched, unknown

    @staticmethod
    def _room_for(claims: _Claims, key: str, table: dict, wording: str) -> bool:
        if key not in table and len(claims.found) + len(claims.unmapped) >= MAX_SKILLS:
            claims.rejected.append({"term": wording, "reason": "over_skill_cap"})
            return False
        return True

    @staticmethod
    def _merge(table: dict[str, dict[str, Any]], key: str, fresh: dict[str, Any]) -> None:
        """Add a mention; a repeated skill unites its contexts, evidence (max 3) and wordings."""
        current = table.get(key)
        if current is None:
            table[key] = fresh
            return
        for field, limit in (("claimed_in", None), ("evidence", MAX_EVIDENCE_ITEMS), ("claimed_as", None)):
            for value in fresh[field]:
                if value not in current[field] and (limit is None or len(current[field]) < limit):
                    current[field].append(value)

    def _read_skills(self, items: list[Any], squashed: str, claims: _Claims) -> None:
        for item in items:
            wording = item.get("as_written") if isinstance(item, dict) else None
            if not isinstance(wording, str) or not wording.strip():
                claims.rejected.append({"term": str(item)[:100], "reason": "malformed_item"})
                continue
            wording = wording.strip()
            if not contains_term(squashed, wording):
                claims.rejected.append({"term": wording, "reason": "not_in_cv"})  # the model made it up
                continue
            context = item.get("context") if item.get("context") in _CONTEXTS else "other"
            claimed_in = context
            if context == "project":
                project = item.get("project")
                claimed_in = (f"project:{project.strip()}" if isinstance(project, str) and project.strip()
                              and contains_term(squashed, project) else "other")
            evidence = item.get("evidence")
            snippets = ([evidence.strip()[:MAX_EVIDENCE_CHARS]]
                        if isinstance(evidence, str) and evidence.strip() and squash(evidence) in squashed else [])
            hint = item.get("name") if isinstance(item.get("name"), str) else None
            self._claim(claims, wording, claimed_in, snippets, hint)

    # -- projects ------------------------------------------------------------
    def _read_projects(self, items: Any, squashed: str, claims: _Claims, result: dict[str, Any]
                       ) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
        projects: list[dict[str, Any]] = []
        tech_terms: dict[str, list[str]] = {}  # project name -> unknown technology terms (resolved later)
        for item in items if isinstance(items, list) else []:
            name = item.get("name") if isinstance(item, dict) else None
            if not isinstance(name, str) or not contains_term(squashed, name):
                result["rejected"].append({"term": name if isinstance(name, str) else str(item)[:100],
                                           "reason": "not_in_cv"})
                continue
            name = name.strip()
            if any(p["name"] == name for p in projects):
                result["rejected"].append({"term": name, "reason": "duplicate_project"})
                continue
            if len(projects) >= MAX_PROJECTS:
                result["rejected"].append({"term": name, "reason": "over_project_cap"})
                continue
            description = _text_field(item.get("description"), squashed, MAX_DESCRIPTION_CHARS)
            if description is None:
                description = ""
                result["warnings"].append("project_description_not_verified")
            project = {"name": name, "description": description, "technologies": [], "technologies_unmapped": []}
            projects.append(project)
            tech_terms[name] = []
            technologies = item.get("technologies")
            for tech in technologies if isinstance(technologies, list) else []:
                if not isinstance(tech, str) or not contains_term(squashed, tech):
                    result["rejected"].append({"term": tech if isinstance(tech, str) else str(tech)[:100],
                                               "reason": "not_in_cv"})
                    continue
                snippets = [description] if description and contains_term(squash(description), tech) else []
                matched, unknown = self._claim(claims, tech.strip(), f"project:{name}", snippets, None)
                for skill_id in matched:
                    if skill_id not in project["technologies"]:
                        project["technologies"].append(skill_id)
                tech_terms[name].extend(unknown)
        return projects, tech_terms

    # -- unknown skills: guarded growth (one batched LLM call) --------------------
    def _resolve_unmapped(self, result: dict[str, Any], redacted: str, label: str, claims: _Claims) -> dict[str, str]:
        """Returns {term key: new skill id} for terms that were auto-added."""
        resolved: dict[str, str] = {}
        if not claims.unmapped:
            return resolved
        terms = list(claims.unmapped.values())
        proposals: dict[str, Any] = {}
        blanket: Optional[str] = None
        if not self.auto_add:
            blanket = "auto-add disabled"
        else:
            try:
                answer = self._call(result, jd_prompts.proposal_system_prompt(),
                                    jd_prompts.proposal_user_prompt([t["term"] for t in terms]))
            except LLMCallError:
                blanket = "proposal call failed"
            else:
                listing = answer.get("proposals") if isinstance(answer, dict) else None
                if isinstance(listing, list):
                    for proposal in listing:
                        if isinstance(proposal, dict) and isinstance(proposal.get("term"), str):
                            proposals.setdefault(make_key(proposal["term"]), proposal)
                else:
                    blanket = "proposal answer was unusable"

        changes = result["taxonomy_changes"]
        queued: list[dict[str, Any]] = []
        for entry in terms:
            if blanket is not None:
                decision = {"action": "queue", "reason": blanket, "suggested_name": entry["suggested_name"],
                            "suggested_category": None}
            elif entry["key"] not in proposals:
                decision = {"action": "queue", "reason": "no proposal returned for this term",
                            "suggested_name": entry["suggested_name"], "suggested_category": None}
            else:
                decision = evaluate_proposal(proposals[entry["key"]], entry["term"], redacted, self.taxonomy,
                                             len(changes["auto_added"]))
            if decision["action"] != "queue":
                # The provenance carries only the term and a label: no sentence from the CV, no personal data.
                outcome = apply_auto_add(decision, self.learned_path,
                                         {"source_jd": f"cv:{label}", "evidence": entry["term"]})
                if outcome["applied"]:
                    self.taxonomy = load_jd_taxonomy(self.learned_path)  # later terms see the addition
                    resolved[entry["key"]] = self._record_auto_add(changes, claims, entry, decision)
                    continue
                decision = {"action": "queue", "reason": outcome["reason"], "suggested_name": decision.get("name"),
                            "suggested_category": decision.get("category")}
            queued.append({"term": entry["term"], "reason": decision["reason"],
                           "suggested_name": decision.get("suggested_name") or entry["suggested_name"],
                           "suggested_category": decision.get("suggested_category")})
            result["unmapped"].append({
                "term": entry["term"], "claimed_in": entry["claimed_in"], "evidence": entry["evidence"],
                "disposition": "queued", "reason": decision["reason"]})
        changes["queued"] = [{"term": q["term"], "reason": q["reason"]} for q in queued]
        if queued:
            try:
                add_to_queue([{**q, "source_jd": f"cv:{label}"} for q in queued], self.queue_path)
            except (QueueError, OSError) as exc:
                result["message"] = f"The review queue could not be updated: {exc}"
        return resolved

    def _record_auto_add(self, changes: dict[str, list], claims: _Claims, entry: dict[str, Any],
                         decision: dict[str, Any]) -> str:
        is_skill = decision["action"] == "auto_add_skill"
        skill_id = decision["name"] if is_skill else decision["skill"]
        self._merge(claims.found, skill_id, {
            "skill": skill_id, "category": self.taxonomy.get(skill_id)["category"],
            "claimed_in": list(entry["claimed_in"]), "evidence": list(entry["evidence"]),
            "claimed_as": list(entry["claimed_as"]), "match": "auto_added"})
        change = {"type": "skill" if is_skill else "alias", "skill": skill_id, "term": entry["term"],
                  "confidence": decision["confidence"]}
        if is_skill:
            change.update(category=decision["category"], aliases=decision["aliases"])
        else:
            change["alias"] = decision["alias"]
        changes["auto_added"].append(change)
        return skill_id

    # -- final assembly --------------------------------------------------------
    @staticmethod
    def _assemble(result: dict[str, Any], claims: _Claims, projects: list[dict[str, Any]],
                  tech_terms: dict[str, list[str]], resolved: dict[str, str]) -> None:
        for project in projects:
            for term in tech_terms[project["name"]]:
                skill_id = resolved.get(make_key(term))
                if skill_id is not None:
                    if skill_id not in project["technologies"]:
                        project["technologies"].append(skill_id)
                elif term not in project["technologies_unmapped"]:
                    project["technologies_unmapped"].append(term)
        result["projects"] = projects
        result["skills"] = [{key: skill[key] for key in _SKILL_KEYS} for skill in claims.found.values()]
        result["rejected"].extend(claims.rejected)

        skills = result["skills"]
        with_context = sum(1 for s in skills if any(c not in _PLAIN_CONTEXTS for c in s["claimed_in"]))
        warnings = result["warnings"]
        if not skills:
            warnings.append("no_skills_found")
        if not projects:
            warnings.append("no_projects_found")
        if not result["education"]:
            warnings.append("no_education_found")
        if len(skills) > STUFFING_MIN_SKILLS and with_context / len(skills) < STUFFING_MIN_CONTEXT:
            warnings.append("many_skills_little_context")  # a keyword-stuffing signal for Step 5; not penalized here
        result["warnings"] = list(dict.fromkeys(warnings))
