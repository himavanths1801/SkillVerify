"""Repo Fingerprint Score (SkillVerify, Step 5).

How well a candidate's CV claims, checked against their real GitHub work, match one job description.
A pure, offline calculation: no LLM, no network, no GitHub call. It only reads the dicts that Steps 1-4
saved, so the same inputs always give the same score.

THE LOCKED FORMULA (a custom original model; do not rename it or add terms to it):

    Repo Fingerprint Score = ( sum of  weight x credit ) / ( sum of weight ) x 100     over every JD skill

    weight : must_have = 3, nice_to_have = 1
    credit : 1.0 claimed on the CV AND confirmed by GitHub evidence
             0.5 claimed on the CV but NOT confirmed
             0.0 not claimed on the CV

Everything else (the second figures, the flags, the hidden strengths) is reported NEXT TO the score and
never changes it. This module produces a score and an evidence trail for a human interviewer. It never
produces a hiring verdict: there is no pass/fail, no threshold and no "recommended" label anywhere.
"""
from __future__ import annotations

import copy
import math
import re
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Iterable, Optional

from skillverify.github_validator import is_valid_username
from skillverify.jd.growth import load_jd_taxonomy
from skillverify.tech_scanner import compare_claimed_skills

# ============================================================================
# SCORING WEIGHTS — edit these values to experiment with different must-have to nice-to-have ratios
# ============================================================================
MUST_HAVE_WEIGHT = 3                 # a must-have skill counts this many times as much ...
NICE_TO_HAVE_WEIGHT = 1              # ... as a nice-to-have skill (default ratio 3:1)
CREDIT_CONFIRMED = 1.0               # claimed on the CV AND confirmed by GitHub evidence
CREDIT_CLAIMED_UNCONFIRMED = 0.5     # claimed on the CV, GitHub does not confirm it
CREDIT_NOT_CLAIMED = 0.0             # not claimed on the CV
WEAK_EVIDENCE_CREDIT = 0.5           # claimed, GitHub shows only a trace / weak signal (see Part C)
NOT_DETECTABLE_CREDIT = 0.5          # claimed, but GitHub CANNOT show this kind of skill (Excel, Tableau ...).
                                     # Set to None to leave such skills out of the score completely.
# ============================================================================

MAX_WEIGHT = 1000
MAX_EXTRA_NAMES = 50
MAX_TOP_LANGUAGES = 5
MAX_WARNINGS = 30
MOSTLY_UNVERIFIABLE_BELOW = 50  # percent of the total weight that GitHub can check
_NAME = re.compile(r"[A-Za-z0-9._-]{1,60}")
_TIERS = ("must_have", "nice_to_have")
_CONFIG_KEYS = ("must_have", "nice_to_have", "credit_confirmed", "credit_claimed_unconfirmed",
                "credit_not_claimed", "weak_evidence_credit", "not_detectable_credit")
_TENTH = Decimal("0.1")
_TECH_UNUSABLE = ("skipped", "error")  # tech-scan statuses that mean "no scan results"


class ScoringConfigError(Exception):
    """The weights or credits are not usable (not a number, not positive, out of range ...)."""


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------
def _is_real_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _weight(name: str, value: Any) -> Decimal:
    if not _is_real_number(value) or not 0 < value <= MAX_WEIGHT:
        raise ScoringConfigError(f"{name} must be a number greater than 0 and at most {MAX_WEIGHT}.")
    return Decimal(str(value))


def _credit(name: str, value: Any) -> Decimal:
    if not _is_real_number(value) or not 0 <= value <= 1:
        raise ScoringConfigError(f"{name} must be a number from 0 to 1.")
    return Decimal(str(value))


def _resolve_config(weights: Optional[dict[str, Any]]) -> dict[str, Optional[Decimal]]:
    """The weights and credits for ONE call: the module constants (read now, so edits and monkeypatching
    are honoured), overridden by ``weights``. Everything is validated; Decimal keeps the sums exact."""
    values: dict[str, Any] = {
        "must_have": MUST_HAVE_WEIGHT, "nice_to_have": NICE_TO_HAVE_WEIGHT,
        "credit_confirmed": CREDIT_CONFIRMED, "credit_claimed_unconfirmed": CREDIT_CLAIMED_UNCONFIRMED,
        "credit_not_claimed": CREDIT_NOT_CLAIMED, "weak_evidence_credit": WEAK_EVIDENCE_CREDIT,
        "not_detectable_credit": NOT_DETECTABLE_CREDIT,
    }
    if weights is not None:
        if not isinstance(weights, dict):
            raise ScoringConfigError("weights must be a dict.")
        unknown = sorted(str(k) for k in weights if k not in _CONFIG_KEYS)
        if unknown:
            raise ScoringConfigError(f"Unknown weight name(s): {', '.join(unknown)[:80]}.")
        values.update(weights)
    config: dict[str, Optional[Decimal]] = {
        "must_have": _weight("must_have", values["must_have"]),
        "nice_to_have": _weight("nice_to_have", values["nice_to_have"]),
    }
    for key in _CONFIG_KEYS[2:6]:
        config[key] = _credit(key, values[key])
    nd = values["not_detectable_credit"]
    config["not_detectable_credit"] = None if nd is None else _credit("not_detectable_credit", nd)
    return config


def _number(value: Optional[Decimal]) -> Any:
    """A Decimal as a plain int (when whole) or float, for JSON."""
    if value is None:
        return None
    return int(value) if value == value.to_integral_value() else float(value)


def _percent(points: Decimal, possible: Decimal) -> Optional[float]:
    """points / possible x 100, rounded HALF UP to 1 decimal place."""
    if possible == 0:
        return None
    return float((points * 100 / possible).quantize(_TENTH, rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------
# input checks
# --------------------------------------------------------------------------
def _problem_with(result: Any, kind: str, name_key: str, tiered: bool) -> Optional[str]:
    """A short description of what is wrong with a saved JD/CV result, or None. Never echoes data."""
    if not isinstance(result, dict):
        return f"The {kind} result is not a JSON object."
    if type(result.get("schema_version")) is not int or result["schema_version"] != 1:
        return f"The {kind} result has an unsupported schema_version."
    if result.get("status") != "ok":
        shown = result.get("status") if isinstance(result.get("status"), str) else "missing"
        return f"The {kind} parse result is not usable (status={shown[:30]})."
    if not isinstance(result.get("skills"), list):
        return f"The {kind} result has no skills list."
    for entry in result["skills"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("skill"), str) or not entry["skill"].strip():
            return f"A {kind} skill entry has no skill name."
        if tiered and entry.get("tier") not in _TIERS:
            return f"A {kind} skill entry has an invalid tier."
    name = result.get(name_key)
    if not isinstance(name, str) or _NAME.fullmatch(name) is None:
        return f"The {kind} result has an invalid {name_key} (letters, digits, '.', '_' and '-' only)."
    return None


def _clean_user(value: Any) -> Optional[str]:
    return value.strip().lower() if isinstance(value, str) and is_valid_username(value.strip()) else None


def _jd_skills(jd: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """One entry per JD skill, in JD order; when a skill repeats the higher tier wins."""
    skills: dict[str, dict[str, Any]] = {}
    for entry in jd["skills"]:
        name = entry["skill"].strip()
        current = skills.get(name)
        if current is None:
            skills[name] = {"skill": name, "category": str(entry.get("category") or ""), "tier": entry["tier"],
                            "tier_basis": str(entry.get("tier_basis") or "")}
        elif entry["tier"] == "must_have" and current["tier"] != "must_have":
            current.update(tier="must_have", tier_basis=str(entry.get("tier_basis") or ""))
    return skills


# --------------------------------------------------------------------------
# which GitHub evidence is usable
# --------------------------------------------------------------------------
def _github_block(github: Any, cv_user: Optional[str]) -> dict[str, Any]:
    block: dict[str, Any] = {
        "username": None, "cv_username": cv_user, "username_matches_cv": None, "validator_status": None,
        "tech_scan_status": None, "scan_complete": None, "counted_repos": None, "repos_scanned": None,
        "total_commits_in_window": None, "active_repos_in_window": None, "languages_top": []}
    if not isinstance(github, dict):
        return block
    status = github.get("status")
    block["username"] = _clean_user(github.get("username"))
    block["validator_status"] = status if isinstance(status, str) else None
    summary = github.get("summary") if isinstance(github.get("summary"), dict) else {}
    for key in ("counted_repos", "total_commits_in_window", "active_repos_in_window"):
        value = summary.get(key)
        block[key] = value if isinstance(value, int) and not isinstance(value, bool) else None
    languages = summary.get("languages_combined")
    if isinstance(languages, dict):
        ranked = sorted(((k, v) for k, v in languages.items() if isinstance(k, str) and _is_real_number(v)),
                        key=lambda kv: -kv[1])
        block["languages_top"] = [k for k, _ in ranked[:MAX_TOP_LANGUAGES]]
    tech = github.get("tech_stack")
    if isinstance(tech, dict):
        block["tech_scan_status"] = tech.get("status") if isinstance(tech.get("status"), str) else None
        # A scan that was skipped or failed is incomplete, whatever its "complete" field claims.
        block["scan_complete"] = tech.get("complete") is True and tech.get("status") not in _TECH_UNUSABLE
        repos = tech.get("repos_scanned")
        block["repos_scanned"] = repos if isinstance(repos, int) and not isinstance(repos, bool) else None
    return block


def _evidence_state(github: Any, cv: dict[str, Any], allow_mismatch: bool, flags: list[str],
                    block: dict[str, Any]) -> tuple[str, str]:
    """(status, message): "ok" when the GitHub evidence can be used for this CV."""
    if github is None:
        flags.append("no_github_file")
        return "no_github_evidence", "No GitHub evidence file was supplied, so the GitHub-confirmed score cannot be calculated."
    if not isinstance(github, dict):
        return "github_unavailable", "The GitHub evidence file is not usable."
    status = github.get("status")
    if status == "user_not_found":
        return "github_user_not_found", ("The GitHub account named in the CV does not exist or could not be found.")
    if status == "no_public_repos":
        return "no_github_evidence", "The GitHub account has no public repositories to check."
    summary = github.get("summary")
    if status != "ok" or not isinstance(summary, dict):
        return "github_unavailable", "The GitHub evidence could not be collected (rate limit, error or malformed file)."
    if summary.get("counted_repos") == 0:
        return "no_github_evidence", "The GitHub account has no counted repositories (all forks or failed)."

    # The evidence must belong to the GitHub account named in the CV.
    cv_github = cv.get("github") if isinstance(cv.get("github"), dict) else {}
    cv_user = block["cv_username"]
    raw_candidates = cv_github.get("candidates") if isinstance(cv_github.get("candidates"), list) else []
    candidates = [user for user in map(_clean_user, raw_candidates) if user]
    evidence_user = block["username"]
    if evidence_user is not None and evidence_user == cv_user:
        block["username_matches_cv"] = True
    elif evidence_user is not None and evidence_user in candidates:
        block["username_matches_cv"] = True
        flags.append("github_ambiguous_in_cv")
    else:
        block["username_matches_cv"] = False if cv_user is not None else None
        if not allow_mismatch:
            return "github_mismatch", ("The GitHub evidence does not belong to the account named in the CV "
                                       "(or the CV names none), so it was not used.")
        flags.append("github_username_mismatch" if cv_user is not None else "github_supplied_without_cv_link")
    tech = github.get("tech_stack")
    if not isinstance(tech, dict) or tech.get("status") in _TECH_UNUSABLE:
        flags.append("tech_scan_missing")
    return "ok", ""


# --------------------------------------------------------------------------
# credits
# --------------------------------------------------------------------------
def _credit_for(entry: Optional[dict[str, Any]], cfg: dict[str, Optional[Decimal]]) -> tuple[Optional[Decimal], str]:
    """Credit and reason for a CLAIMED skill, from the real compare_claimed_skills entry."""
    status = entry.get("status") if entry else None
    if status == "found":
        confidence = entry.get("confidence")
        if confidence in ("strong", "medium"):
            return cfg["credit_confirmed"], "confirmed"
        if confidence == "weak":
            return cfg["weak_evidence_credit"], "weak_evidence"
        return cfg["credit_claimed_unconfirmed"], "unconfirmed"
    if status == "not_found":
        return cfg["credit_claimed_unconfirmed"], "not_found_on_github"
    if status == "not_found_scan_incomplete":
        return cfg["credit_claimed_unconfirmed"], "scan_incomplete"
    if status == "not_detectable_from_github":
        return cfg["not_detectable_credit"], "not_detectable"
    return cfg["credit_claimed_unconfirmed"], "unconfirmed"


# --------------------------------------------------------------------------
# the public API
# --------------------------------------------------------------------------
def _blank(cfg: dict[str, Optional[Decimal]]) -> dict[str, Any]:
    return {
        "schema_version": 1, "status": "ok", "message": "",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "job_name": "", "cv_label": "",
        "weights": {"must_have": _number(cfg["must_have"]), "nice_to_have": _number(cfg["nice_to_have"]),
                    "credit_confirmed": _number(cfg["credit_confirmed"]),
                    "credit_claimed_unconfirmed": _number(cfg["credit_claimed_unconfirmed"]),
                    "credit_not_claimed": _number(cfg["credit_not_claimed"]),
                    "weak_evidence_credit": _number(cfg["weak_evidence_credit"]),
                    "not_detectable_credit": _number(cfg["not_detectable_credit"])},
        "github": _github_block(None, None),
        "score": {"repo_fingerprint": None, "claim_coverage": None, "checkable_only": None, "verifiable_share": None,
                  "points_earned": 0.0, "points_possible": 0.0,
                  "by_tier": {tier: {"skills": 0, "weight": 0, "points": 0.0} for tier in _TIERS}},
        "skills": [], "hidden_strengths": [], "cv_extra_skills": {"count": 0, "names": []},
        "jd_unmapped_terms": 0, "cv_warnings": [], "flags": [], "sensitivity": None,
        "counts": {"jd_skills": 0, "must_have": 0, "nice_to_have": 0, "claimed": 0, "confirmed": 0,
                   "unconfirmed": 0, "not_claimed": 0, "not_detectable": 0, "hidden_strengths": 0},
    }


def score_candidate(jd_result: Any, cv_result: Any, github_result: Any = None, *,
                    weights: Optional[dict[str, Any]] = None, taxonomy: Any = None,
                    allow_github_mismatch: bool = False, include_sensitivity: bool = False) -> dict[str, Any]:
    """Score one candidate against one job description. See docs/SCORING.md.

    Never raises for bad input data (the result has ``status: "invalid_input"``); raises only
    ``ScoringConfigError`` for bad weights and the taxonomy's ``TaxonomyError`` for a corrupt overlay.
    The inputs are never modified.
    """
    cfg = _resolve_config(weights)
    result = _blank(cfg)
    problem = _problem_with(jd_result, "JD", "job_name", True) or _problem_with(cv_result, "CV", "label", False)
    if problem:
        result.update(status="invalid_input", message=problem)
        return result
    jd, cv = copy.deepcopy(jd_result), copy.deepcopy(cv_result)
    github = copy.deepcopy(github_result)
    result["job_name"], result["cv_label"] = jd["job_name"], cv["label"]
    jd_skills = _jd_skills(jd)
    cv_skills = {entry["skill"].strip() for entry in cv["skills"]}
    flags: list[str] = []

    # --- information that needs no GitHub data -----------------------------------------------------
    unmapped = jd.get("unmapped")
    result["jd_unmapped_terms"] = len(unmapped) if isinstance(unmapped, list) else 0
    warnings = cv.get("warnings")
    result["cv_warnings"] = [w[:100] for w in warnings if isinstance(w, str)][:MAX_WARNINGS] if isinstance(warnings, list) else []
    extras: list[str] = []
    for entry in cv["skills"]:
        name = entry["skill"].strip()
        if name not in jd_skills and name not in extras:
            extras.append(name)
    result["cv_extra_skills"] = {"count": len(extras), "names": extras[:MAX_EXTRA_NAMES]}
    if result["jd_unmapped_terms"]:
        flags.append("jd_has_unmapped_terms")
    if "many_skills_little_context" in result["cv_warnings"]:
        flags.append("cv_keyword_stuffing_signal")  # reported only; it has no numeric effect
    if isinstance(cv.get("source"), dict) and cv["source"].get("truncated") is True:
        flags.append("cv_truncated")

    if not jd_skills:
        flags.append("no_must_have_skills")
        result.update(status="no_jd_skills", message="The job description has no skills to score.", flags=flags)
        return result
    if not any(s["tier"] == "must_have" for s in jd_skills.values()):
        flags.append("no_must_have_skills")

    # --- GitHub evidence ------------------------------------------------------------------------
    cv_github = cv.get("github") if isinstance(cv.get("github"), dict) else {}
    result["github"] = block = _github_block(github, _clean_user(cv_github.get("username")))
    status, message = _evidence_state(github, cv, allow_github_mismatch, flags, block)
    have_evidence = status == "ok"
    by_name: dict[str, dict[str, Any]] = {}
    if have_evidence:
        tech = github.get("tech_stack")
        if not isinstance(tech, dict) or tech.get("status") in _TECH_UNUSABLE:
            tech = {}  # no usable scan: the comparison then treats everything that needs it as incomplete
        if taxonomy is None:
            taxonomy = load_jd_taxonomy()  # includes learned skills; a corrupt overlay raises TaxonomyError
        compared = compare_claimed_skills(list(jd_skills), github, tech, taxonomy)
        by_name = {(e.get("skill") or e["claimed"][0]): e for e in compared}

    # --- per-skill credit ---------------------------------------------------------------------------
    rows: list[dict[str, Any]] = []
    hidden: list[dict[str, str]] = []
    claim_points = Decimal(0)
    totals = {"possible": Decimal(0), "earned": Decimal(0), "check_possible": Decimal(0), "check_earned": Decimal(0),
              "checkable_weight": Decimal(0), "all_weight": Decimal(0), "claim_possible": Decimal(0)}
    tiers = {tier: {"skills": 0, "weight": Decimal(0), "points": Decimal(0)} for tier in _TIERS}
    for name, jd_entry in jd_skills.items():
        weight = cfg[jd_entry["tier"]]
        claimed = name in cv_skills
        compare = by_name.get(name) if have_evidence else None
        if not claimed:
            credit, reason = cfg["credit_not_claimed"], "not_claimed"
        elif not have_evidence:
            credit, reason = None, "no_github_evidence"
        else:
            credit, reason = _credit_for(compare, cfg)
        points = None if credit is None else weight * credit
        github_status = compare.get("status") if compare else None
        if have_evidence and not claimed and github_status == "found":
            hidden.append({"skill": name, "confidence": str(compare.get("confidence"))})

        totals["all_weight"] += weight
        if claimed:
            claim_points += weight
        tiers[jd_entry["tier"]]["skills"] += 1
        if credit is not None:  # a left-out skill (not detectable, credit None) is in neither total nor points
            totals["possible"] += weight
            totals["earned"] += points
            tiers[jd_entry["tier"]]["weight"] += weight
            tiers[jd_entry["tier"]]["points"] += points
            if have_evidence and github_status != "not_detectable_from_github":
                totals["check_possible"] += weight
                totals["check_earned"] += points
        if have_evidence and github_status != "not_detectable_from_github":
            totals["checkable_weight"] += weight
        rows.append({
            "skill": name, "category": jd_entry["category"], "tier": jd_entry["tier"],
            "tier_basis": jd_entry["tier_basis"], "weight": _number(weight), "claimed": claimed,
            "github_status": github_status,
            "confidence": compare.get("confidence") if compare else None,
            "basis": compare.get("basis") if compare else None,
            "credit": None if credit is None else float(credit), "points": None if points is None else float(points),
            "reason": reason})

    # --- the figures ----------------------------------------------------------------------------------
    score = result["score"]
    score["claim_coverage"] = _percent(claim_points, totals["all_weight"])  # CV against JD only; GitHub ignored
    score["points_possible"] = float(totals["possible"] if have_evidence else totals["all_weight"])
    if have_evidence:
        score["repo_fingerprint"] = _percent(totals["earned"], totals["possible"])  # THE locked formula
        score["checkable_only"] = _percent(totals["check_earned"], totals["check_possible"])
        score["verifiable_share"] = _percent(totals["checkable_weight"], totals["all_weight"])
        score["points_earned"] = float(totals["earned"])
        if totals["possible"] == 0:
            message = "No skill of this job can be checked on GitHub, so there is no GitHub-confirmed score."
        elif score["verifiable_share"] is not None and score["verifiable_share"] < MOSTLY_UNVERIFIABLE_BELOW:
            flags.append("mostly_unverifiable")
    score["by_tier"] = {tier: {"skills": t["skills"], "weight": _number(t["weight"]), "points": float(t["points"])}
                        for tier, t in tiers.items()}

    reasons = [row["reason"] for row in rows]
    if have_evidence and (any(r == "scan_incomplete" for r in reasons) or block["scan_complete"] is not True):
        flags.append("scan_incomplete")
    if "weak_evidence" in reasons:
        flags.append("weak_evidence_used")
    result.update(status=status, message=message, skills=rows, hidden_strengths=hidden)
    result["flags"] = list(dict.fromkeys(flags))
    result["counts"] = {
        "jd_skills": len(rows), "must_have": sum(1 for r in rows if r["tier"] == "must_have"),
        "nice_to_have": sum(1 for r in rows if r["tier"] == "nice_to_have"),
        "claimed": sum(1 for r in rows if r["claimed"]), "confirmed": reasons.count("confirmed"),
        "unconfirmed": sum(1 for r in reasons if r in ("not_found_on_github", "scan_incomplete", "weak_evidence",
                                                       "unconfirmed")),
        "not_claimed": reasons.count("not_claimed"), "not_detectable": reasons.count("not_detectable"),
        "hidden_strengths": len(hidden)}
    if include_sensitivity:
        result["sensitivity"] = sensitivity(jd_result, cv_result, github_result, weights=weights, taxonomy=taxonomy,
                                            allow_github_mismatch=allow_github_mismatch)
    return result


def sensitivity(jd_result: Any, cv_result: Any, github_result: Any = None, ratios: Iterable[Any] = (1, 2, 3, 4, 5),
                **options: Any) -> dict[str, Optional[float]]:
    """How much the must-have to nice-to-have ratio matters: the same evidence re-scored with must-have weight
    ``r`` (nice-to-have weight 1) for each ``r``. Returns ``{"2:1": score, ...}``. It never alters the main score."""
    options.pop("include_sensitivity", None)
    base = dict(options.pop("weights", None) or {})
    table: dict[str, Optional[float]] = {}
    for ratio in ratios:
        run = score_candidate(jd_result, cv_result, github_result, weights={**base, "must_have": ratio,
                                                                           "nice_to_have": 1}, **options)
        table[f"{ratio}:1"] = run["score"]["repo_fingerprint"]
    return table
