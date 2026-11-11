"""Tech scan with the EXTENDED setup: YOLO / PyCharm / VS Code as real skills + strict SQL detection.

Usage (from the project root):
    python examples/run_tech_scan_ext.py <github-username> [--max-repos 40]
        [--claimed "Python, YOLO, VS Code, PyCharm, SQL, REST APIs"]

Same as run_tech_scan.py, but with the extended taxonomy and the strict SQL scanner.
Saves output/<username>_techstack_ext.json (never the other demos' files).
Exit codes: 0 ok, 1 invalid username or failed run, 2 configuration problem.
"""
import argparse
import sys
from pathlib import Path

# The project root (for `skillverify`) and this folder (for `run_tech_scan`) must be importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import run_tech_scan as base_demo  # noqa: E402
from skillverify import (  # noqa: E402
    GitHubConfigError,
    GitHubValidator,
    is_valid_username,
    save_result_to_json,
)
from skillverify.sql_strict import StrictSqlTechScanner  # noqa: E402
from skillverify.taxonomy import TaxonomyError  # noqa: E402
from skillverify.taxonomy_extra import (  # noqa: E402
    extended_detectable,
    load_extended_taxonomy,
    promote_label_items,
)
from skillverify.tech_scanner import TechScannerError, compare_claimed_skills  # noqa: E402

OUTPUT_DIR = Path("output")


def output_path_for(username: str):
    """Return output/<username>_techstack_ext.json, or None if the username is not valid."""
    if not is_valid_username(username):
        return None
    return OUTPUT_DIR / f"{username}_techstack_ext.json"


def ext_claim_line(entry):
    """Claim line labelled with the canonical skill, e.g. 'Docker (claimed as "Python and Docker")'."""
    skill = entry.get("skill")
    if not skill:
        return base_demo.claim_line(entry)  # no skill id: keep the claimed text
    texts = list(dict.fromkeys(str(c) for c in entry["claimed"]))
    if len(texts) == 1 and texts[0].lower() == skill.lower():
        label = skill
    else:
        label = f'{skill} (claimed as "{", ".join(texts)}")'
    return base_demo.claim_line({**entry, "claimed": [label]})


def main() -> int:
    parser = argparse.ArgumentParser(description="Tech scan with the extended taxonomy and strict SQL detection.")
    parser.add_argument("username")
    parser.add_argument("--max-repos", type=int, default=40, help="most repos to scan (newest first)")
    parser.add_argument("--claimed", help="comma-separated skills claimed on the CV")
    args = parser.parse_args()

    out_path = output_path_for(args.username)
    if out_path is None:
        print("Invalid GitHub username. Nothing was fetched or saved.")
        return 1

    try:
        taxonomy = load_extended_taxonomy()
    except TaxonomyError as err:
        print(f"Taxonomy problem: {err}")
        return 2
    try:
        validator = GitHubValidator()
        scanner = StrictSqlTechScanner(validator, taxonomy=taxonomy, max_repos=args.max_repos)
    except GitHubConfigError as err:
        print(f"Configuration problem: {err}")
        return 2
    except TechScannerError as err:
        print(f"Signal file problem: {err}")
        return 2

    try:
        result = validator.validate(args.username)
        print(f"Validator status: {result['status']}")
        if result["message"]:
            print(f"Message: {result['message']}")
        if result["status"] != "ok":
            return 0 if result["status"] == "no_public_repos" else 1
        base_demo.print_validator_headline(result)

        tech = promote_label_items(scanner.scan(result), taxonomy)
        print(f"(Extended taxonomy: {len(taxonomy.skills)} skills; strict SQL detector.)")
        base_demo.print_tech_stack(tech)
        if tech.get("promoted_labels"):
            print("Tools promoted to skills: " + ", ".join(tech["promoted_labels"]))
        save_result_to_json({**result, "tech_stack": tech}, out_path)  # a copy: `result` is untouched
        print(f"\nFull JSON saved to {out_path}")

        if args.claimed:
            print("\nClaimed skills vs evidence:")
            entries = compare_claimed_skills(
                base_demo.split_claimed(args.claimed), result, tech, taxonomy,
                detectable=extended_detectable(scanner), scan_skills=scanner.scannable_skills())
            for entry in entries:
                print(ext_claim_line(entry))
            print("(Demo only, not the Repo Fingerprint Score.)")
        return 1 if tech["status"] == "error" else 0
    except Exception as err:  # noqa: BLE001 - never show a traceback (or anything secret)
        print(f"Unexpected problem ({type(err).__name__}). Nothing more was saved.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
