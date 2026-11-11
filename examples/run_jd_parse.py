"""Parse a job description into a tiered, taxonomy-mapped skill list (Step 3).

Usage (from the project root):
    python examples/run_jd_parse.py --file examples/sample_jds/data_analyst.txt
    python examples/run_jd_parse.py --text "Must have Python. Nice to have Docker."
    type job.txt | python examples/run_jd_parse.py --name my_job
Options: --name JOBNAME, --dry-run (write nothing at all), --no-auto-add, --model NAME

Needs GEMINI_API_KEY (environment variable or .env file). The job description text is
sent to Google: do not use confidential descriptions unless your plan's terms allow it.
Saves output/<jobname>_jdparsed.json. Exit codes: 0 ok, 1 failed or invalid input,
2 configuration problem.
"""
import argparse
import re
import sys
from pathlib import Path

# Allow running this script directly without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skillverify.github_validator import save_result_to_json  # noqa: E402
from skillverify.jd import GeminiProvider, JDParser, LLMConfigError  # noqa: E402
from skillverify.taxonomy import TaxonomyError  # noqa: E402

OUTPUT_DIR = Path("output")
QUEUE_DISPLAY = "output/taxonomy_review_queue.json"


def default_provider(model):
    return GeminiProvider(model=model)


def name_from_file(path):
    """A safe default job name from a file name: invalid characters become '_'."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", Path(path).stem)[:60]
    return cleaned or "job"


def print_summary(result, dry_run):
    counts = result["counts"]
    print(f"Job: {result['job_name']}   (provider: {result['provider']['name']}, "
          f"model: {result['provider']['model']})")
    print(f"Skills: {counts['skills']} ({counts['must_have']} must have, {counts['nice_to_have']} nice to have), "
          f"unmapped: {counts['unmapped']}, rejected: {counts['rejected']}, LLM calls: {result['llm_calls']}")
    for tier, title in (("must_have", "Must have"), ("nice_to_have", "Nice to have")):
        names = [s["skill"] for s in result["skills"] if s["tier"] == tier]
        print(f"{title} ({len(names)}): " + (", ".join(names) if names else "-"))
    for item in result["unmapped"]:
        print(f"  not in taxonomy: {item['term']} ({item['tier']}): {item['reason']}")
    for item in result["rejected"]:
        print(f"  rejected: {item['term']}: {item['reason']}")
    if result["jd"]["truncated"]:
        print("Note: the job description was too long and was cut.")
    where = "dry run: nothing was written" if dry_run else f"see {QUEUE_DISPLAY}"
    print(f"{counts['auto_added']} skills auto-added, {counts['queued']} flagged for review ({where})")
    if result["message"]:
        print(f"Note: {result['message']}")


def main(argv=None, provider_factory=default_provider, stdin=None):
    parser = argparse.ArgumentParser(description="Parse a job description into tiered, taxonomy-mapped skills.")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--file", help="path of a text file with the job description")
    source.add_argument("--text", help="the job description as text")
    parser.add_argument("--name", help="job name for the output file (letters, digits, . _ -)")
    parser.add_argument("--dry-run", action="store_true", help="parse and print; write no file at all")
    parser.add_argument("--no-auto-add", action="store_true", help="never add skills to the taxonomy automatically")
    parser.add_argument("--model", help="Gemini model name (default: GEMINI_MODEL or the built-in default)")
    args = parser.parse_args(argv)
    stdin = stdin if stdin is not None else sys.stdin

    # 1. Read and check the input first, so bad input never needs an API key.
    if args.file is not None:
        try:
            text = Path(args.file).read_text(encoding="utf-8")
        except (OSError, ValueError):
            print(f"Could not read the file: {args.file}")
            return 1
        default_name = name_from_file(args.file)
    elif args.text is not None:
        text, default_name = args.text, "job"
    elif not stdin.isatty():
        text, default_name = stdin.read(), "job"
    else:
        print("No job description given. Use --file, --text, or pipe text in.")
        return 1
    if not text.strip():
        print("The job description is empty. Nothing was sent or saved.")
        return 1
    job_name = args.name if args.name is not None else default_name

    # 2. Configure the provider and parser.
    try:
        provider = provider_factory(args.model)
        job_parser = JDParser(provider, auto_add=not args.no_auto_add, persist=not args.dry_run)
    except LLMConfigError as err:
        print(f"Configuration problem: {err}")
        return 2
    except TaxonomyError as err:
        print(f"Taxonomy problem: {err}")
        return 2

    # 3. Parse, print, save.
    try:
        with job_parser:
            result = job_parser.parse(text, job_name)
        if result["status"] != "ok":
            print(f"Could not parse the job description ({result['status']}): {result['message']}")
            return 1
        print_summary(result, args.dry_run)
        if not args.dry_run:
            out = save_result_to_json(result, OUTPUT_DIR / f"{result['job_name']}_jdparsed.json")
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
