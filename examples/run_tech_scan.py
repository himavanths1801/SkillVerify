"""Scan a GitHub user's repos for libraries, SQL and tools (Step 1 extension).

Usage (from the project root):
    python examples/run_tech_scan.py <github-username> [--max-repos 40]
        [--claimed "Python, JavaScript, Pandas, YOLO, SQL, PyCharm, REST APIs"]

Needs GITHUB_TOKEN (environment variable or .env file). Saves the validator
output plus a `tech_stack` key to output/<username>_techstack.json; it never
touches output/<username>.json (that file belongs to run_validator.py).
Exit codes: 0 ok, 1 invalid username or failed run, 2 configuration problem.
"""
import argparse
import sys
from pathlib import Path

# Allow running this script directly without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skillverify import (  # noqa: E402
    GitHubConfigError,
    GitHubValidator,
    is_valid_username,
    save_result_to_json,
)
from skillverify.tech_scanner import (  # noqa: E402
    TechScanner,
    TechScannerError,
    compare_claimed_skills,
)

OUTPUT_DIR = Path("output")


def output_path_for(username: str):
    """Return output/<username>_techstack.json, or None if the username is not valid.

    Only a validated username (letters, digits, hyphens) is used in the filename,
    so slashes or dots can never escape the output folder."""
    if not is_valid_username(username):
        return None
    return OUTPUT_DIR / f"{username}_techstack.json"


def print_validator_headline(result):
    s = result["summary"]
    print(f"Repos: {s['total_public_repos']} public, {s['counted_repos']} counted, "
          f"{s['forked_repos_excluded']} forks excluded")
    total = sum(s["languages_combined"].values())
    if total:
        print("Top languages (GitHub statistics):")
        for lang, size in list(s["languages_combined"].items())[:5]:
            print(f"  {lang:<15}{size * 100 / total:5.1f}%")


def print_tech_stack(tech):
    print(f"\nTech stack scan: {tech['status']}"
          + (f" ({tech['message']})" if tech["message"] else ""))
    print(f"Repos scanned: {tech['repos_scanned']}, not scanned: {len(tech['repos_not_scanned'])}, "
          f"failed: {tech['repos_failed']}, API calls used: {tech['api_calls_used']}")
    if tech["complete"]:
        print("Scan complete: a skill that is not listed was not found.")
    else:
        print("Scan INCOMPLETE (" + ", ".join(tech["incomplete_reasons"] or ["see message"])
              + "): a skill that is not listed is UNKNOWN, not 'not found'.")

    for level in ("strong", "medium", "weak"):
        items = [s for s in tech["skills"] if s["confidence"] == level and s["in_taxonomy"]]
        if items:
            print(f"\n{level.capitalize()} evidence:")
            for s in items:
                print(f"  {s['label']:<28}{len(s['repos'])} repo(s)  [{', '.join(s['evidence_types'])}]")
    tools = [s for s in tech["skills"] if not s["in_taxonomy"]]
    if tools:
        print("\nTools (not in the skill taxonomy yet):")
        for s in tools:
            print(f"  {s['label']:<28}{s['confidence']}, {len(s['repos'])} repo(s)")
    if tech["unmapped_packages"]:
        print("\nPackages not in the signal map (top 10):")
        for row in tech["unmapped_packages"][:10]:
            print(f"  {row['name']:<28}{row['ecosystem']}, {row['repos']} repo(s)")


def source_text(source):
    """One evidence source as short text; a share under 1% is marked (trace)."""
    trace = " (trace)" if source.get("trace") else ""
    if source["type"] == "language_stats":
        return f"language stats {source['percent']}%{trace}"
    if source["type"] == "language_stats_notebook":
        return f"language stats (notebook) {source['percent']}%{trace}"
    if source["type"] == "python_source_files":
        return f"{source['files']} Python file(s){trace}"
    if source["type"] == "inferred":
        return "inferred from " + ", ".join(source["from"])
    return source["type"]


def claim_line(entry):
    name = ", ".join(dict.fromkeys(str(c) for c in entry["claimed"]))
    status = entry["status"]
    if status == "found":
        sources = ", ".join(source_text(s) for s in entry["sources"])
        return f"  [found, {entry['confidence']}] {name}: {sources}"
    messages = {
        "not_found": "not found (scan was complete)",
        "not_found_scan_incomplete": "not found so far, but the scan was incomplete",
        "not_detectable_from_github": "cannot be checked from GitHub",
        "not_in_taxonomy": "not in the skill taxonomy",
    }
    if status == "not_found" and entry.get("basis") == "language_stats_only":
        # The status is right (GitHub's language statistics are complete) but "scan was complete" would be false.
        return f"  [{status}] {name}: not found in GitHub language statistics (the scan itself was incomplete)"
    return f"  [{status}] {name}: {messages[status]}"


def split_claimed(text):
    return [part.strip() for part in text.split(",") if part.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan a GitHub user's repos for libraries, SQL and tools.")
    parser.add_argument("username")
    parser.add_argument("--max-repos", type=int, default=40, help="most repos to scan (newest first)")
    parser.add_argument("--claimed", help="comma-separated skills claimed on the CV")
    args = parser.parse_args()

    out_path = output_path_for(args.username)
    if out_path is None:
        print("Invalid GitHub username. Nothing was fetched or saved.")
        return 1

    try:
        validator = GitHubValidator()
        scanner = TechScanner(validator, max_repos=args.max_repos)
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
        print_validator_headline(result)

        tech = scanner.scan(result)
        print_tech_stack(tech)
        combined = {**result, "tech_stack": tech}  # a copy: the validator result is untouched
        save_result_to_json(combined, out_path)
        print(f"\nFull JSON saved to {out_path}")

        if args.claimed:
            print("\nClaimed skills vs evidence:")
            for entry in compare_claimed_skills(split_claimed(args.claimed), result, tech, scanner.taxonomy,
                                                detectable=scanner.detectable_skills(),
                                                scan_skills=scanner.scannable_skills()):
                print(claim_line(entry))
            print("(Demo only, not the Repo Fingerprint Score.)")
        return 1 if tech["status"] == "error" else 0
    except Exception as err:  # noqa: BLE001 - never show a traceback (or anything secret)
        print(f"Unexpected problem ({type(err).__name__}). Nothing more was saved.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
