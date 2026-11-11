"""Extended taxonomy (SkillVerify extras).

Adds three tools (YOLO, PyCharm, VS Code) as real skills on top of the base
taxonomy, WITHOUT touching ``skills.json`` or the default ``Taxonomy.load()``.
Also turns the scanner's label-only items for those tools into proper skill items.

Standard library only. This module may import ``skillverify.taxonomy``; nothing
old imports it.
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Optional

from skillverify.taxonomy import Taxonomy, TaxonomyError

DEFAULT_EXTRA_SKILLS_PATH = Path(__file__).resolve().parent / "data" / "skills_extra.json"


def load_extended_taxonomy(
    extra_path: Optional[str | Path] = None,
    base_path: Optional[str | Path] = None,
) -> Taxonomy:
    """Base taxonomy plus the extra skills file.

    Raises ``TaxonomyError`` if either file is bad, if an extra skill id repeats a
    base id (case-insensitive; the Taxonomy constructor does not check ids that
    way), or if an alias collides (the constructor refuses that itself).
    """
    base = Taxonomy.load(base_path)
    extra = Taxonomy.load(extra_path if extra_path is not None else DEFAULT_EXTRA_SKILLS_PATH)
    base_ids = {skill["id"].strip().lower(): skill["id"] for skill in base.skills}
    for skill in extra.skills:
        clash = base_ids.get(skill["id"].strip().lower())
        if clash is not None:
            raise TaxonomyError(
                f"Extra skill id '{skill['id']}' duplicates base skill id '{clash}' (case-insensitive).")
    return Taxonomy(base.skills + extra.skills)


def promote_label_items(tech: Any, taxonomy: Taxonomy) -> Any:
    """Copy of a scan result in which label-only tools the taxonomy knows become skill items.

    Never mutates ``tech`` and never raises. A label is promoted only if the
    taxonomy matches it and no other item already has that skill id. The copy gets
    ``promoted_labels`` (sorted list of the labels promoted).
    """
    result = copy.deepcopy(tech)
    if not isinstance(result, dict) or not isinstance(result.get("skills"), list):
        return result
    taken = {i["skill"] for i in result["skills"] if isinstance(i, dict) and i.get("skill")}
    promoted: list[str] = []
    for entry in result["skills"]:
        if not isinstance(entry, dict) or entry.get("in_taxonomy") or not isinstance(entry.get("label"), str):
            continue
        match = taxonomy.normalize(entry["label"])
        if match["status"] != "matched" or match["skill"] in taken:
            continue
        entry.update(skill=match["skill"], in_taxonomy=True, category=match["category"])
        taken.add(match["skill"])
        promoted.append(entry["label"])
    result["promoted_labels"] = sorted(promoted)
    return result


def extended_detectable(scanner: Any) -> set[str]:
    """Skills the extended setup can check: the scanner's set plus YOLO.

    YOLO comes from the ultralytics / yolov5 packages, so "not found" is meaningful
    when the scan is complete. PyCharm and VS Code stay OUT on purpose: their folders
    (.idea/, .vscode/) are often gitignored, so absence proves nothing.
    """
    return scanner.detectable_skills() | {"YOLO"}
