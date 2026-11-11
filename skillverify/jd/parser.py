"""JD parser (SkillVerify, Step 3).

Reads the raw text of a job description, asks an LLM to extract the technical skills
and say how strongly each is required, then maps them onto the skill taxonomy.

The LLM is NOT trusted. Code decides everything that matters:
* a skill is accepted only if its wording really appears in the JD (anti-hallucination);
* "must have" needs evidence copied verbatim from the JD, otherwise it is downgraded;
* matching uses the deterministic taxonomy; unknown terms go through guarded growth;
* at most two LLM calls are made per JD.

It reports tiers only (must_have / nice_to_have). Numeric weights belong to Step 5.
"""
from __future__ import annotations

import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from skillverify.jd import prompts
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

MAX_JD_CHARS = 30000      # longer text is cut here and flagged jd.truncated
MAX_SKILLS = 60           # most distinct skills / unmapped terms kept
MAX_EVIDENCE_CHARS = 400  # longest evidence snippet kept
MAX_EVIDENCE_ITEMS = 3    # evidence snippets kept per skill

_JOB_NAME = re.compile(r"[A-Za-z0-9._-]{1,60}")
_BRACKETS = re.compile(r"\s*\([^)]*\)")  # "Python (advanced)" -> "Python" (taxonomy limitation)
_SPLIT = re.compile(r"[;,]")
_SKILL_KEYS = ("skill", "category", "tier", "tier_basis", "evidence", "claimed_as", "match")


def _rank(tier: str, basis: str) -> int:
    """Higher wins when the same skill is mentioned twice."""
    if tier == "must_have":
        return 3
    return {"explicit_cue": 2, "downgraded_evidence_not_found": 1}.get(basis, 0)


def _copy_if_exists(source: Path, destination: Path) -> Path:
    if source.is_file():
        shutil.copyfile(source, destination)
    return destination


class JDParser:
    """Parse job descriptions into taxonomy-mapped, tiered skill lists.

    ``persist=False`` is for dry runs: the learned-skills file and the review queue are
    copied to a temporary folder and used there, so nothing real is written. Call
    ``close()`` (or use ``with``) to remove that folder.
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
            self._scratch = tempfile.TemporaryDirectory(prefix="skillverify_dryrun_")
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

    def __enter__(self) -> "JDParser":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # -- output skeleton ---------------------------------------------------
    def _blank(self, job_name: Any) -> dict[str, Any]:
        valid = isinstance(job_name, str) and _JOB_NAME.fullmatch(job_name) is not None
        return {
            "schema_version": 1, "status": "ok", "message": "",
            "job_name": job_name if valid else "",
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "provider": {"name": getattr(self.provider, "name", "unknown"),
                         "model": getattr(self.provider, "model", "unknown")},
            "jd": {"characters": 0, "truncated": False},
            "llm_calls": 0, "skills": [], "unmapped": [], "rejected": [],
            "taxonomy_changes": {"auto_added": [], "queued": []},
            "counts": {},
        }

    @staticmethod
    def _finish(result: dict[str, Any], status: str = "ok", message: str = "") -> dict[str, Any]:
        result["status"] = status
        if message:
            result["message"] = message
        skills = result["skills"]
        result["counts"] = {
            "skills": len(skills),
            "must_have": sum(1 for s in skills if s["tier"] == "must_have"),
            "nice_to_have": sum(1 for s in skills if s["tier"] == "nice_to_have"),
            "unmapped": len(result["unmapped"]), "rejected": len(result["rejected"]),
            "auto_added": len(result["taxonomy_changes"]["auto_added"]),
            "queued": len(result["taxonomy_changes"]["queued"]),
        }
        return result

    def _call(self, result: dict[str, Any], system_prompt: str, user_prompt: str) -> Any:
        result["llm_calls"] += 1
        return self.provider.generate_json(system_prompt, user_prompt)

    # -- the main entry point -----------------------------------------------
    def parse(self, jd_text: Any, job_name: Any = "job") -> dict[str, Any]:
        """Parse one job description. Never raises for LLM or data problems (only ``LLMConfigError``
        from the provider is allowed to propagate)."""
        result = self._blank(job_name)
        if not result["job_name"]:
            return self._finish(result, "invalid_input",
                                "job_name must be 1-60 characters: letters, digits, '.', '_' or '-'.")
        if not isinstance(jd_text, str):
            return self._finish(result, "invalid_input", "The job description must be text.")
        if not jd_text.strip():
            return self._finish(result, "empty_jd", "The job description is empty.")
        text = jd_text[:MAX_JD_CHARS]
        result["jd"] = {"characters": len(text), "truncated": len(jd_text) > MAX_JD_CHARS}

        try:
            raw = self._call(result, prompts.extraction_system_prompt(), prompts.extraction_user_prompt(text))
        except LLMCallError as exc:
            return self._finish(result, "llm_error", str(exc))
        items = raw.get("skills") if isinstance(raw, dict) else None
        if not isinstance(items, list):
            return self._finish(result, "parse_error", "The model's answer had no 'skills' list.")

        found, unmapped = self._collect(items, squash(text), result["rejected"])
        self._resolve_unmapped(result, text, job_name, found, unmapped)
        result["skills"] = [{key: skill[key] for key in _SKILL_KEYS} for skill in found.values()]
        return self._finish(result)

    # -- step 1: tiering and mapping (no LLM involved) -----------------------
    @staticmethod
    def _tier(item: dict[str, Any], jd_squashed: str) -> tuple[str, str, list[str]]:
        signal = item.get("signal")
        signal = signal.strip().lower() if isinstance(signal, str) else ""
        evidence = item.get("evidence")
        evidence = evidence.strip() if isinstance(evidence, str) else ""
        verified = bool(evidence) and squash(evidence) in jd_squashed
        snippets = [evidence[:MAX_EVIDENCE_CHARS]] if verified else []  # unverifiable evidence is dropped
        if signal == "required":
            if verified:
                return "must_have", "explicit_cue", snippets
            return "nice_to_have", "downgraded_evidence_not_found", snippets
        if signal == "preferred":
            return "nice_to_have", "explicit_cue", snippets
        return "nice_to_have", "default_no_signal", snippets

    @staticmethod
    def _merge(table: dict[str, dict[str, Any]], key: str, fresh: dict[str, Any]) -> None:
        """Add a mention; for a repeated skill the strongest tier wins (must_have beats nice_to_have)."""
        current = table.get(key)
        if current is None:
            table[key] = fresh
            return
        if _rank(fresh["tier"], fresh["tier_basis"]) > _rank(current["tier"], current["tier_basis"]):
            current["tier"], current["tier_basis"] = fresh["tier"], fresh["tier_basis"]
        for snippet in fresh["evidence"]:
            if snippet not in current["evidence"] and len(current["evidence"]) < MAX_EVIDENCE_ITEMS:
                current["evidence"].append(snippet)
        for written in fresh["claimed_as"]:
            if written not in current["claimed_as"]:
                current["claimed_as"].append(written)

    def _collect(self, items: list[Any], jd_squashed: str, rejected: list[dict[str, str]]
                 ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        found: dict[str, dict[str, Any]] = {}
        unmapped: dict[str, dict[str, Any]] = {}
        for item in items:
            written = item.get("as_written") if isinstance(item, dict) else None
            if not isinstance(written, str) or not written.strip():
                rejected.append({"term": str(item)[:100], "reason": "malformed_item"})
                continue
            written = written.strip()
            if not contains_term(jd_squashed, written):  # whole-term match: "Java" is not inside "JavaScript"
                rejected.append({"term": written, "reason": "not_in_jd"})  # the model made it up
                continue
            tier, basis, snippets = self._tier(item, jd_squashed)
            pieces = [p.strip() for p in _SPLIT.split(_BRACKETS.sub("", written)) if p.strip()]
            if not pieces:
                rejected.append({"term": written, "reason": "empty_term"})
                continue
            hint = item.get("name") if isinstance(item.get("name"), str) else None

            def mention(**extra: Any) -> dict[str, Any]:
                return {"tier": tier, "tier_basis": basis, "evidence": list(snippets),
                        "claimed_as": [written], **extra}

            for piece in pieces:
                outcome = self.taxonomy.normalize_many([piece])  # also splits "HTML/CSS" when all parts match
                for match in outcome["matched"]:
                    self._add(found, unmapped, match["skill"], mention(
                        skill=match["skill"], category=match["category"], match="taxonomy"), rejected, True)
                for unknown in outcome["unknown"]:
                    term = str(unknown["input"])
                    self._add(found, unmapped, make_key(term), mention(
                        term=term, key=make_key(term), suggested_name=hint), rejected, False)
                for _bad in outcome["invalid"]:
                    rejected.append({"term": piece, "reason": "invalid_term"})
        return found, unmapped

    def _add(self, found: dict, unmapped: dict, key: str, fresh: dict[str, Any],
             rejected: list[dict[str, str]], is_found: bool) -> None:
        table = found if is_found else unmapped
        if key not in table and len(found) + len(unmapped) >= MAX_SKILLS:
            rejected.append({"term": fresh["claimed_as"][0], "reason": "over_skill_cap"})
            return
        self._merge(table, key, fresh)

    # -- step 2: unmapped terms -> guarded growth (one batched LLM call) ----------
    def _resolve_unmapped(self, result: dict[str, Any], jd_text: str, job_name: str,
                          found: dict[str, dict[str, Any]], unmapped: dict[str, dict[str, Any]]) -> None:
        if not unmapped:
            return
        terms = list(unmapped.values())
        proposals: dict[str, Any] = {}
        blanket_reason: Optional[str] = None
        if not self.auto_add:
            blanket_reason = "auto-add disabled"
        else:
            try:
                answer = self._call(result, prompts.proposal_system_prompt(),
                                    prompts.proposal_user_prompt([t["term"] for t in terms]))
            except LLMCallError:
                blanket_reason = "proposal call failed"
            else:
                listing = answer.get("proposals") if isinstance(answer, dict) else None
                if isinstance(listing, list):
                    for proposal in listing:
                        if isinstance(proposal, dict) and isinstance(proposal.get("term"), str):
                            proposals.setdefault(make_key(proposal["term"]), proposal)
                else:
                    blanket_reason = "proposal answer was unusable"

        auto_added = result["taxonomy_changes"]["auto_added"]
        queued: list[dict[str, Any]] = []
        for entry in terms:
            if blanket_reason is not None:
                decision = {"action": "queue", "reason": blanket_reason,
                            "suggested_name": entry["suggested_name"], "suggested_category": None}
            elif entry["key"] not in proposals:
                decision = {"action": "queue", "reason": "no proposal returned for this term",
                            "suggested_name": entry["suggested_name"], "suggested_category": None}
            else:
                decision = evaluate_proposal(proposals[entry["key"]], entry["term"], jd_text,
                                             self.taxonomy, len(auto_added))
            if decision["action"] != "queue":
                meta = {"source_jd": job_name, "evidence": (entry["evidence"] or [entry["term"]])[0]}
                outcome = apply_auto_add(decision, self.learned_path, meta)
                if outcome["applied"]:
                    self.taxonomy = load_jd_taxonomy(self.learned_path)  # later terms see the addition
                    self._record_auto_add(result, found, entry, decision)
                    continue
                decision = {"action": "queue", "reason": outcome["reason"],
                            "suggested_name": decision.get("name"), "suggested_category": decision.get("category")}
            queued.append({"term": entry["term"], "reason": decision["reason"],
                           "suggested_name": decision.get("suggested_name") or entry["suggested_name"],
                           "suggested_category": decision.get("suggested_category")})
            result["unmapped"].append({
                "term": entry["term"], "tier": entry["tier"], "tier_basis": entry["tier_basis"],
                "evidence": entry["evidence"], "disposition": "queued", "reason": decision["reason"]})
        result["taxonomy_changes"]["queued"] = [{"term": q["term"], "reason": q["reason"]} for q in queued]
        if queued:
            try:
                add_to_queue([{**q, "source_jd": job_name} for q in queued], self.queue_path)
            except (QueueError, OSError) as exc:
                result["message"] = f"The review queue could not be updated: {exc}"

    def _record_auto_add(self, result: dict[str, Any], found: dict[str, dict[str, Any]],
                         entry: dict[str, Any], decision: dict[str, Any]) -> None:
        is_skill = decision["action"] == "auto_add_skill"
        skill_id = decision["name"] if is_skill else decision["skill"]
        record = self.taxonomy.get(skill_id) or {"category": decision.get("category")}
        self._merge(found, skill_id, {
            "skill": skill_id, "category": record["category"], "tier": entry["tier"],
            "tier_basis": entry["tier_basis"], "evidence": list(entry["evidence"]),
            "claimed_as": list(entry["claimed_as"]), "match": "auto_added"})
        change = {"type": "skill" if is_skill else "alias", "skill": skill_id, "term": entry["term"],
                  "confidence": decision["confidence"]}
        if is_skill:
            change.update(category=decision["category"], aliases=decision["aliases"])
        else:
            change["alias"] = decision["alias"]
        result["taxonomy_changes"]["auto_added"].append(change)
