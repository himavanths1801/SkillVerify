"""Run the GitHub Validator against the real GitHub API.

Usage (from the project root):
    python examples/run_validator.py <github-username> [--days 180] [--include-forks]
Requires GITHUB_TOKEN (environment variable or .env file).
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

OUTPUT_DIR = Path("output")


def output_path_for(username: str):
    """Return output/<username>.json, or None if the username is not valid.

    Only a validated username (letters, digits, hyphens) is ever used in the
    filename, so slashes or dots can never escape the output folder."""
    if not is_valid_username(username):
        return None
    return OUTPUT_DIR / f"{username}.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a GitHub user's real activity.")
    parser.add_argument("username")
    parser.add_argument("--days", type=int, default=180, help="activity window in days")
    parser.add_argument("--include-forks", action="store_true", help="count forked repos too")
    args = parser.parse_args()

    out_path = output_path_for(args.username)
    if out_path is None:
        print("Invalid GitHub username. Nothing was fetched or saved.")
        return 1

    try:
        validator = GitHubValidator(window_days=args.days, include_forks=args.include_forks)
    except GitHubConfigError as err:
        print(f"Configuration problem: {err}")
        return 2

    result = validator.validate(args.username)
    print(f"Status: {result['status']}")
    if result["message"]:
        print(f"Message: {result['message']}")

    if result["profile"]:
        p = result["profile"]
        print(f"Profile: {p['name']} ({p['url']}), created {p['created_at']}")

    s = result["summary"]
    print(f"Repos: {s['total_public_repos']} public, {s['counted_repos']} counted, "
          f"{s['forked_repos_excluded']} forks excluded")
    print(f"Commits in last {result['window_days']} days: {s['total_commits_in_window']} "
          f"across {s['active_repos_in_window']} active repos")
    print(f"Most recent activity: {s['most_recent_activity_date']}")
    total = sum(s["languages_combined"].values())
    if total:
        print("Top languages:")
        for lang, size in list(s["languages_combined"].items())[:8]:
            print(f"  {lang:<15}{size * 100 / total:5.1f}%  ({size:,} bytes)")

    out = save_result_to_json(result, out_path)
    print(f"Full JSON saved to {out}")
    return 0 if result["status"] in ("ok", "no_public_repos") else 1


if __name__ == "__main__":
    sys.exit(main())
