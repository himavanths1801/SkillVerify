"""Parse a CV (PDF, Word or text) into claimed skills, projects, education and contacts (Step 4).

Usage (from the project root):
    python examples/run_cv_parse.py --file path/to/cv.pdf
    python examples/run_cv_parse.py --file examples/sample_cvs/cv_data_analyst.txt
    python examples/run_cv_parse.py --text "Skills: Python, SQL ..."
Options: --name LABEL, --dry-run (write nothing at all), --no-auto-add, --model NAME

Needs GEMINI_API_KEY (environment variable or .env file) and `pip install -r requirements-cv.txt`.
Privacy: the CV text (including name and education) is sent to Google with emails and phone numbers
replaced; the saved JSON contains personal data. Use only CVs you may process.
Saves output/<label>_cvparsed.json. Exit codes: 0 ok, 1 failed or invalid input, 2 configuration problem.
"""
import argparse
import re
import sys
from pathlib import Path

# Allow running this script directly without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skillverify.cv import CVExtractError, CVParser, extract_text  # noqa: E402
from skillverify.cv.parser import label_from_path  # noqa: E402
from skillverify.github_validator import save_result_to_json  # noqa: E402
from skillverify.jd import GeminiProvider, LLMConfigError  # noqa: E402
from skillverify.taxonomy import TaxonomyError  # noqa: E402

OUTPUT_DIR = Path("output")
QUEUE_DISPLAY = "output/taxonomy_review_queue.json"


def default_provider(model):
    return GeminiProvider(model=model)


def found(flag):
    return "found" if flag else "not found"


def print_summary(result, dry_run):
    """A short summary. Email and phone are only reported as found / not found, never printed."""
    counts, source, github = result["counts"], result["source"], result["github"]
    pages = f", {source['pages']} page(s)" if source["pages"] else ""
    print(f"CV: {result['label']}   (format: {source['format']}{pages}, {source['characters']} characters)")
    print(f"name: {found(result['candidate']['name'])}   email: {found(result['candidate']['emails'])}"
          f"   phone: {found(result['candidate']['phones'])}")
    if github["username"]:
        print(f"github: {github['username']} ({github['status']}, from {github['source']})")
    else:
        print("github: not found")
    print(f"skills: {counts['skills']}   projects: {counts['projects']}   education: {counts['education']}"
          f"   unmapped: {counts['unmapped']}   rejected: {counts['rejected']}   LLM calls: {result['llm_calls']}")
    if result["skills"]:
        print("claimed skills: " + ", ".join(s["skill"] for s in result["skills"]))
    for project in result["projects"]:
        print(f"  project: {project['name']} [{', '.join(project['technologies']) or '-'}]")
    for item in result["unmapped"]:
        print(f"  not in taxonomy: {item['term']}: {item['reason']}")
    if result["warnings"]:
        print("warnings: " + ", ".join(result["warnings"]))
    where = "dry run: nothing was written" if dry_run else f"see {QUEUE_DISPLAY}"
    print(f"{counts['auto_added']} skills auto-added, {counts['queued']} flagged for review ({where})")
    if result["message"]:
        print(f"Note: {result['message']}")


def main(argv=None, provider_factory=default_provider, stdin=None):
    parser = argparse.ArgumentParser(description="Parse a CV into claimed skills, projects and contacts.")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--file", help="path of a PDF, Word (.docx) or text CV")
    source.add_argument("--text", help="the CV as text")
    parser.add_argument("--name", help="label for the output file (letters, digits, . _ -)")
    parser.add_argument("--dry-run", action="store_true", help="parse and print; write no file at all")
    parser.add_argument("--no-auto-add", action="store_true", help="never add skills to the taxonomy automatically")
    parser.add_argument("--model", help="Gemini model name (default: GEMINI_MODEL or the built-in default)")
    args = parser.parse_args(argv)
    stdin = stdin if stdin is not None else sys.stdin

    # 1. Read and check the input first, so a bad file never needs an API key.
    extracted = None
    if args.file is not None:
        try:
            extracted = extract_text(args.file)
        except CVExtractError as err:
            print(f"Could not read the CV ({err.code}): {err.message}")
            return 1
        text, default_label = extracted["text"], label_from_path(args.file)
    elif args.text is not None:
        text, default_label = args.text, "cv"
    elif not stdin.isatty():
        text, default_label = stdin.read(), "cv"
    else:
        print("No CV given. Use --file, --text, or pipe text in.")
        return 1
    if not text.strip():
        print("The CV is empty. Nothing was sent or saved.")
        return 1
    label = args.name if args.name is not None else default_label

    # 2. Configure the provider and parser.
    try:
        provider = provider_factory(args.model)
        cv_parser = CVParser(provider, auto_add=not args.no_auto_add, persist=not args.dry_run)
    except LLMConfigError as err:
        print(f"Configuration problem: {err}")
        return 2
    except TaxonomyError as err:
        print(f"Taxonomy problem: {err}")
        return 2

    # 3. Parse, print, save.
    try:
        with cv_parser:
            if extracted is not None:
                info = {"format": extracted["format"], "pages": extracted["pages"],
                        "characters": extracted["characters"], "truncated": extracted["truncated"],
                        "links_found": len(extracted["links"])}
                result = cv_parser.parse_text(text, label, extracted["links"], info)
            else:
                result = cv_parser.parse_text(text, label)
        if result["status"] != "ok":
            print(f"Could not parse the CV ({result['status']}): {result['message']}")
            return 1
        print_summary(result, args.dry_run)
        if not args.dry_run:
            out = save_result_to_json(result, OUTPUT_DIR / f"{result['label']}_cvparsed.json")
            print(f"Full JSON saved to {out}")
        return 0
    except LLMConfigError as err:
        print(f"Configuration problem: {err}")
        return 2
    except Exception as err:  # noqa: BLE001 - never show a traceback (or anything secret)
        print(f"Unexpected problem ({type(err).__name__}). Nothing more was saved.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
