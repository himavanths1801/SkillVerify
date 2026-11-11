"""Score a candidate against a job description (Step 5: Repo Fingerprint Score). Offline.

Usage (from the project root):
    python examples\\run_score.py --jd output\\backend_jdparsed.json --cv output\\cv_cvparsed.json ^
        --github output\\ghost_techstack_ext.json [--out PATH] [--dry-run] [--allow-github-mismatch]
        [--must-weight N] [--nice-weight N] [--sensitivity]

Reads three JSON files saved by the earlier steps. Nothing is fetched and no API key is needed.
The output is evidence for a human interviewer, not a hiring decision.
Exit codes: 0 = status ok; 1 = any other status or unreadable/invalid input; 2 = configuration problem.
"""
import argparse
import json
import sys
from pathlib import Path

# Allow running this script directly without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skillverify.github_validator import save_result_to_json  # noqa: E402
from skillverify.scoring import ScoringConfigError, score_candidate  # noqa: E402
from skillverify.taxonomy import TaxonomyError  # noqa: E402

OUTPUT_DIR = Path("output")
MAX_INPUT_BYTES = 5_000_000


def load_json_object(path, what):
    """(dict, None) or (None, friendly message). Never echoes file contents."""
    file_path = Path(path)
    try:
        if not file_path.is_file():
            return None, f"The {what} file was not found (or is not a file)."
        if file_path.stat().st_size > MAX_INPUT_BYTES:
            return None, f"The {what} file is larger than {MAX_INPUT_BYTES // 1_000_000} MB."
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, f"The {what} file could not be read as JSON."
    if not isinstance(data, dict):
        return None, f"The {what} file must contain a JSON object."
    return data, None


def fmt(value):
    return "n/a" if value is None else f"{value}"


def print_summary(result, show_sensitivity):
    score, counts, github = result["score"], result["counts"], result["github"]
    print(f"Job: {result['job_name'] or '-'}   CV: {result['cv_label'] or '-'}   "
          f"GitHub: {github['username'] or github['cv_username'] or 'not found'}")
    print(f"Status: {result['status']}" + (f" ({result['message']})" if result["message"] else ""))
    print(f"Repo Fingerprint Score: {fmt(score['repo_fingerprint'])}   "
          f"(claim coverage {fmt(score['claim_coverage'])}, checkable only {fmt(score['checkable_only'])}, "
          f"verifiable share {fmt(score['verifiable_share'])})")
    print(f"Points: {score['points_earned']} of {score['points_possible']}")
    print(f"JD skills: {counts['jd_skills']} ({counts['must_have']} must have, {counts['nice_to_have']} nice to have)   "
          f"claimed: {counts['claimed']}   confirmed: {counts['confirmed']}   unconfirmed: {counts['unconfirmed']}   "
          f"not claimed: {counts['not_claimed']}   not detectable: {counts['not_detectable']}")
    if result["hidden_strengths"]:
        print("Hidden strengths (on GitHub, not claimed on the CV): "
              + ", ".join(f"{h['skill']} ({h['confidence']})" for h in result["hidden_strengths"]))
    if result["flags"]:
        print("Flags: " + ", ".join(result["flags"]))
    if show_sensitivity and result["sensitivity"]:
        print("Ratio check (must-have : nice-to-have):")
        for label, value in result["sensitivity"].items():
            print(f"  {label:<5} {fmt(value)}")
    print("Evidence for the interviewer, not a hiring decision.")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Score a candidate against a job description (offline).")
    parser.add_argument("--jd", required=True, help="JSON saved by run_jd_parse.py")
    parser.add_argument("--cv", required=True, help="JSON saved by run_cv_parse.py")
    parser.add_argument("--github", help="JSON saved by run_tech_scan_ext.py (the validator result with tech_stack)")
    parser.add_argument("--out", help="output file (must end in .json)")
    parser.add_argument("--dry-run", action="store_true", help="print only; write no file")
    parser.add_argument("--allow-github-mismatch", action="store_true",
                        help="score even if the GitHub account differs from the one in the CV")
    parser.add_argument("--must-weight", type=float, help="must-have weight for this run only")
    parser.add_argument("--nice-weight", type=float, help="nice-to-have weight for this run only")
    parser.add_argument("--sensitivity", action="store_true", help="also print the ratio table")
    args = parser.parse_args(argv)

    if args.out is not None and not args.out.lower().endswith(".json"):
        print("--out must end in .json")
        return 1
    weights = {}
    if args.must_weight is not None:
        weights["must_have"] = args.must_weight
    if args.nice_weight is not None:
        weights["nice_to_have"] = args.nice_weight

    inputs = {}
    for key, path, what in (("jd", args.jd, "JD"), ("cv", args.cv, "CV"), ("github", args.github, "GitHub")):
        if path is None:
            inputs[key] = None
            continue
        data, problem = load_json_object(path, what)
        if problem:
            print(problem)
            return 1
        inputs[key] = data

    try:
        result = score_candidate(inputs["jd"], inputs["cv"], inputs["github"], weights=weights or None,
                                 allow_github_mismatch=args.allow_github_mismatch,
                                 include_sensitivity=args.sensitivity)
    except ScoringConfigError as err:
        print(f"Configuration problem: {err}")
        return 2
    except TaxonomyError as err:
        print(f"Taxonomy problem: {err}")
        return 2

    print_summary(result, args.sensitivity)
    if not args.dry_run and result["status"] != "invalid_input":
        # job_name and cv_label were validated ([A-Za-z0-9._-]{1,60}) inside score_candidate before being echoed.
        target = Path(args.out) if args.out else OUTPUT_DIR / f"{result['cv_label']}__{result['job_name']}_score.json"
        print(f"Full JSON saved to {save_result_to_json(result, target)}")
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
