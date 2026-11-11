"""Try the skill taxonomy from the command line.

Usage (from the project root):
    python examples/run_taxonomy.py "JS" "React.js" "Postgres" "Pyhton"
    python examples/run_taxonomy.py --github-json output/torvalds.json

The second form only READS an existing Step 1 output file; it never calls GitHub.
"""
import argparse
import json
import sys
from pathlib import Path

# Allow running this script directly without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skillverify.taxonomy import Taxonomy, TaxonomyError  # noqa: E402


def show_texts(tax, texts):
    for text in texts:
        result = tax.normalize(text)
        if result["status"] == "matched":
            print(f"{text!r:<22} -> {result['skill']}  [{result['category']}, {result['match_type']}]")
        elif result["status"] == "unknown":
            hint = ", ".join(result["suggestions"]) or "none"
            print(f"{text!r:<22} -> UNKNOWN  (did you mean: {hint}?)")
        else:
            print(f"{text!r:<22} -> INVALID INPUT")


def show_github_json(tax, path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        languages = data["summary"]["languages_combined"]
    except FileNotFoundError:
        print(f"File not found: {path}")
        return 1
    except (json.JSONDecodeError, UnicodeDecodeError):
        print(f"{path} is not valid JSON.")
        return 1
    except (KeyError, TypeError):
        print(f"{path} does not look like a validator output (no summary.languages_combined).")
        return 1

    # A hand-edited or wrong file must never produce a traceback.
    if not isinstance(languages, dict):
        print(f"{path} does not look like a validator output (languages_combined is not a dict).")
        return 1
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in languages.values()):
        print(f"{path} does not look like a validator output (languages_combined values are not numbers).")
        return 1

    mapped = tax.map_github_languages(languages)
    total = sum(languages.values()) or 1
    print(f"GitHub languages in {path} mapped to canonical skills:")
    for skill_id, size in sorted(mapped["skills"].items(), key=lambda kv: kv[1], reverse=True):
        print(f"  {skill_id:<15}{size * 100 / total:5.1f}%  ({size:,} bytes)")
    unmapped = mapped["unmapped_languages"]
    print("Unmapped languages: " + (", ".join(unmapped) if unmapped else "none"))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Normalize skill names with the SkillVerify taxonomy.")
    parser.add_argument("texts", nargs="*", help="skill names to normalize")
    parser.add_argument("--github-json", metavar="PATH", help="a Step 1 output file to map")
    args = parser.parse_args()
    if not args.texts and not args.github_json:
        parser.print_usage()
        return 1

    try:
        tax = Taxonomy.load()
    except TaxonomyError as err:
        print(f"Taxonomy problem: {err}")
        return 2

    if args.texts:
        show_texts(tax, args.texts)
    if args.github_json:
        return show_github_json(tax, args.github_json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
