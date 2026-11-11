"""Skill taxonomy and normalizer (SkillVerify, Step 2).

One shared vocabulary for the whole project: a list of canonical skill names
(``skills.json``) plus a ``Taxonomy`` class that turns messy text such as
"React.js" or "k8s" into those names.

Design rule: accuracy beats coverage. We only ever match on an *exact*
normalized key (or the same key after stripping a trailing version number).
Close spellings are offered as ``suggestions`` but are never matched
automatically, because a wrong match would silently corrupt scores.

Standard library only. This module knows nothing about Step 1.
"""
from __future__ import annotations

import difflib
import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

CATEGORIES = (
    "language", "frontend", "backend", "database", "cloud_devops",
    "data_ml", "mobile", "testing", "tools", "concept",
)
MAX_INPUT_LENGTH = 200
MAX_SUGGESTIONS = 3
SUGGESTION_CUTOFF = 0.8
DEFAULT_SKILLS_PATH = Path(__file__).resolve().parent / "data" / "skills.json"

# Characters stripped from the *ends* of text (bullets, list dashes, commas ...).
# "+" and "#" are deliberately absent: C++ and C# depend on them.
_SURROUNDING = "•·▪◦●‣⁃*-–—_,.;:!?\"'`()[]{}<>|~\\/ \t\r\n"
# Characters removed from the *inside* of text. Zero-width characters are
# common in text extracted from PDFs.
_SEPARATORS = re.compile(r"[\s.\-_/​‌‍﻿]+")
# "python 3.11", "python3", "java 17", "python 3.11+": base text, optional
# separator, then a version number at the very end.
_VERSION = re.compile(r"^(.+?)([\s\-_]*)\d+(?:\.\d+)*\+?$")
# Names this short with digits glued on ("C2", "Go2", "R2") are too ambiguous to
# strip: unknown is better than a wrong guess. "R 4.2" (with a separator) is fine.
SHORT_NAME_LIMIT = 2
# Separators for compound items such as "HTML/CSS", "HTML & CSS", "HTML and CSS".
_COMPOUND_SPLIT = re.compile(r"\s*/\s*|\s*&\s*|\s+and\s+", re.IGNORECASE)


class TaxonomyError(Exception):
    """Raised when the skills file is missing, malformed, or inconsistent."""


# --------------------------------------------------------------------------
# Normalization helpers (pure functions)
# --------------------------------------------------------------------------
def _clean(text: str) -> str:
    """NFKC-normalize (fixes full-width characters, non-breaking spaces),
    lowercase, trim, and strip surrounding bullets/punctuation."""
    return unicodedata.normalize("NFKC", text).lower().strip().strip(_SURROUNDING)


def _key(clean_text: str) -> str:
    """Comparison key: 'Node.js', 'node js', 'Node-JS' and 'nodejs' all give 'nodejs'."""
    return _SEPARATORS.sub("", clean_text)


def make_key(text: str) -> str:
    """Public helper: the comparison key for any text (empty string if nothing is left)."""
    return _key(_clean(text))


# --------------------------------------------------------------------------
# Taxonomy
# --------------------------------------------------------------------------
class Taxonomy:
    """Canonical skills plus lookups. Build one with ``Taxonomy.load()``."""

    def __init__(self, skills: list[dict[str, Any]]) -> None:
        self._skills: list[dict[str, Any]] = skills
        self._by_id: dict[str, dict[str, Any]] = {s["id"]: s for s in skills}
        # key -> (skill id, "canonical" | "alias")
        self._keys: dict[str, tuple[str, str]] = {}
        self._lang_to_skills: dict[str, list[str]] = {}
        self._build_indexes()

    # -- loading -----------------------------------------------------------
    @classmethod
    def load(cls, path: Optional[str | Path] = None) -> "Taxonomy":
        """Load and validate a skills file (default: skillverify/data/skills.json)."""
        file_path = Path(path) if path is not None else DEFAULT_SKILLS_PATH
        try:
            raw = json.loads(file_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise TaxonomyError(f"Skills file not found: {file_path}") from None
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise TaxonomyError(f"Skills file is not valid JSON ({file_path}): {exc}") from None
        except OSError as exc:
            raise TaxonomyError(f"Could not read skills file {file_path}: {exc}") from None
        return cls(cls._validate(raw))

    @staticmethod
    def _validate(raw: Any) -> list[dict[str, Any]]:
        """Check the file's shape and return clean skill records.

        Unknown extra fields are dropped on purpose, so later steps can add
        fields to the file without breaking this one.
        """
        entries = raw.get("skills") if isinstance(raw, dict) else None
        if not isinstance(entries, list) or not entries:
            raise TaxonomyError("Skills file has no skills (expected a non-empty 'skills' list).")

        skills: list[dict[str, Any]] = []
        seen_ids: dict[str, str] = {}
        for position, entry in enumerate(entries, start=1):
            if not isinstance(entry, dict):
                raise TaxonomyError(f"Skill #{position} is not an object.")
            skill_id = entry.get("id")
            if not isinstance(skill_id, str) or not skill_id.strip():
                raise TaxonomyError(f"Skill #{position} is missing an 'id'.")
            if entry.get("category") not in CATEGORIES:
                raise TaxonomyError(
                    f"Skill '{skill_id}' has missing or invalid category "
                    f"{entry.get('category')!r}; allowed: {', '.join(CATEGORIES)}."
                )
            lowered = skill_id.strip().lower()
            if lowered in seen_ids:
                raise TaxonomyError(f"Duplicate skill id (case-insensitive): '{seen_ids[lowered]}' and '{skill_id}'.")
            seen_ids[lowered] = skill_id

            record = {"id": skill_id.strip(), "category": entry["category"]}
            for field in ("aliases", "github_languages"):
                value = entry.get(field, [])
                if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                    raise TaxonomyError(f"Skill '{skill_id}': '{field}' must be a list of strings.")
                record[field] = list(value)
            skills.append(record)
        return skills

    def _build_indexes(self) -> None:
        """Index canonical ids first, then aliases, refusing any ambiguity."""
        for skill in self._skills:
            key = _key(_clean(skill["id"]))
            if not key:
                raise TaxonomyError(f"Skill id '{skill['id']}' is empty after normalization.")
            if key in self._keys:
                other = self._keys[key][0]
                raise TaxonomyError(f"Skill ids '{other}' and '{skill['id']}' normalize to the same key '{key}'.")
            self._keys[key] = (skill["id"], "canonical")

        for skill in self._skills:
            for alias in skill["aliases"]:
                key = _key(_clean(alias))
                if not key:
                    raise TaxonomyError(f"Alias {alias!r} of '{skill['id']}' is empty after normalization.")
                owner = self._keys.get(key)
                if owner is None:
                    self._keys[key] = (skill["id"], "alias")
                elif owner[0] != skill["id"]:
                    # The same text pointing at two skills would make matching a guess.
                    raise TaxonomyError(
                        f"Alias collision: {alias!r} (key '{key}') is claimed by "
                        f"'{owner[0]}' and '{skill['id']}'."
                    )

            for language in skill["github_languages"]:
                ids = self._lang_to_skills.setdefault(language.strip().lower(), [])
                if skill["id"] not in ids:
                    ids.append(skill["id"])

    # -- simple lookups ----------------------------------------------------
    @property
    def skills(self) -> list[dict[str, Any]]:
        """All skills, in file order."""
        return list(self._skills)

    def get(self, skill_id: str) -> Optional[dict[str, Any]]:
        """The skill record for an exact canonical id, or None."""
        return self._by_id.get(skill_id)

    # -- normalization -----------------------------------------------------
    def normalize(self, text: Any) -> dict[str, Any]:
        """Map text to a canonical skill. Never raises."""
        result: dict[str, Any] = {
            "input": text if isinstance(text, (str, int, float, bool, type(None))) else repr(text)[:MAX_INPUT_LENGTH],
            "status": "invalid_input",
            "skill": None,
            "category": None,
            "match_type": None,
        }
        if not isinstance(text, str) or len(text) > MAX_INPUT_LENGTH:
            return result
        clean = _clean(text)
        key = _key(clean)
        if not key:  # empty, whitespace, or only bullets/punctuation
            return result

        # 1. Exact key first, so names ending in digits (S3, EC2, HTML5) match as written.
        hit = self._keys.get(key)
        match_type = hit[1] if hit else None

        # 2. Otherwise try again without a trailing version number.
        if hit is None:
            version = _VERSION.match(clean)
            if version and re.search(r"[a-z]", version.group(1)):
                base_key = _key(_clean(version.group(1)))
                glued = version.group(2) == ""
                if not (glued and len(base_key) <= SHORT_NAME_LIMIT):
                    hit = self._keys.get(base_key)
                    match_type = "version_stripped" if hit else None

        if hit is not None:
            skill = self._by_id[hit[0]]
            result.update(status="matched", skill=skill["id"], category=skill["category"], match_type=match_type)
            return result

        # 3. Unknown. Suggestions are hints only; they are never auto-matched.
        result["status"] = "unknown"
        result["suggestions"] = self._suggest(key)
        return result

    def _suggest(self, key: str) -> list[str]:
        close = difflib.get_close_matches(key, list(self._keys), n=10, cutoff=SUGGESTION_CUTOFF)
        suggestions: list[str] = []
        for candidate in close:
            skill_id = self._keys[candidate][0]
            if skill_id not in suggestions:
                suggestions.append(skill_id)
        return suggestions[:MAX_SUGGESTIONS]

    def normalize_many(self, items: Iterable[Any]) -> dict[str, list[dict[str, Any]]]:
        """Normalize a list. Matched skills are de-duplicated (first-seen order)
        and remember every original input that led to them.

        Compound items such as "HTML/CSS" or "HTML & CSS" are split here (and
        only here), but only if the whole text is unknown and *every* part
        matches a skill. A split is never accepted partially.
        """
        if items is None:
            items = []
        elif isinstance(items, str):
            items = [items]

        matched: dict[str, dict[str, Any]] = {}
        unknown: list[dict[str, Any]] = []
        invalid: list[dict[str, Any]] = []

        def add_matched(result: dict[str, Any], original: Any) -> None:
            entry = matched.setdefault(
                result["skill"],
                {"skill": result["skill"], "category": result["category"], "inputs": []},
            )
            if original not in entry["inputs"][-1:]:  # "HTML/HTML" lists its input once
                entry["inputs"].append(original)

        for item in items:
            result = self.normalize(item)
            if result["status"] == "unknown":
                parts = self._split_compound(item)
                if parts:
                    for part in parts:
                        add_matched(part, result["input"])
                    continue
                unknown.append({"input": result["input"], "suggestions": result["suggestions"]})
            elif result["status"] == "matched":
                add_matched(result, result["input"])
            else:
                invalid.append({"input": result["input"]})
        return {"matched": list(matched.values()), "unknown": unknown, "invalid": invalid}

    def _split_compound(self, text: str) -> list[dict[str, Any]]:
        """Results for each part of "A/B", "A & B" or "A and B", or [] unless
        there are at least two parts and every one of them matches."""
        parts = [p for p in _COMPOUND_SPLIT.split(text)]
        if len(parts) < 2:
            return []
        results = [self.normalize(part) for part in parts]
        if all(r["status"] == "matched" for r in results):
            return results
        return []

    # -- GitHub language mapping (lookup only, no scoring) ------------------
    def github_language_to_skills(self, language_name: str) -> list[str]:
        """Canonical skill ids that count this GitHub language as evidence."""
        if not isinstance(language_name, str):
            return []
        return list(self._lang_to_skills.get(language_name.strip().lower(), []))

    def map_github_languages(self, languages_combined: Mapping[str, int]) -> dict[str, Any]:
        """Turn ``{"Python": 5393481, ...}`` into canonical skills with bytes.

        Bytes of several GitHub languages mapping to one skill are added up.
        Languages with no taxonomy skill are listed in ``unmapped_languages``.
        """
        if not isinstance(languages_combined, Mapping):
            raise TypeError("languages_combined must be a dict of {language: bytes}.")
        skills: dict[str, int] = {}
        unmapped: list[str] = []
        for language, size in languages_combined.items():
            ids = self.github_language_to_skills(language)
            if not ids:
                unmapped.append(language)
            for skill_id in ids:
                skills[skill_id] = skills.get(skill_id, 0) + size
        return {"skills": skills, "unmapped_languages": unmapped}

    def confirmable_via_languages(self, skill_id: str) -> bool:
        """True if GitHub language data can directly confirm this skill."""
        skill = self.get(skill_id)
        return bool(skill and skill["github_languages"])
