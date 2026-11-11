# Tech Stack Scanner (Step 1 extension)

## What it adds
Step 1 reports GitHub's language percentages (Python 60%, HTML 20% ...). A CV also claims
libraries and tools: pandas, scikit-learn, Flask, MongoDB, SQL, PyCharm. None of these appear
as "languages". The scanner looks inside the candidate's repos for evidence of them and reports
what it found, where, and how strong the evidence is. It does **no scoring** and makes **no
hiring judgement**. It never changes the validator's output or its language percentages.

```python
from skillverify import GitHubValidator
from skillverify.tech_scanner import TechScanner, compare_claimed_skills

validator = GitHubValidator()
result = validator.validate("some-user")
scanner = TechScanner(validator)
tech = scanner.scan(result)                 # never raises
combined = {**result, "tech_stack": tech}   # add to a COPY; `result` stays untouched
```
Command line: `python examples/run_tech_scan.py <username> --claimed "Python, Pandas, YOLO, REST APIs"`
(saves `output/<username>_techstack.json`).

## Evidence types and confidence
| evidence type | meaning | confidence |
|---|---|---|
| `imported_in_code` | a mapped package is imported in a source file that was read | strong |
| `embedded_query` | a conservative match for a real SQL statement inside a string literal | strong |
| `declared_dependency` | listed in a dependency file, or a `pip install` line in a notebook | medium |
| `sql_file` | the repo contains `.sql` files | medium |
| `file_signal` | a tool's config file exists (GitHub Actions, Jenkinsfile, `.gitlab-ci.yml`, compose file, `pom.xml`, `build.gradle`) | medium |
| `ide_folder` | `.idea/` or `.vscode/` is committed | weak |

A skill's confidence is the strongest of its evidence types. Tools with no taxonomy skill
(YOLO, PyCharm, JetBrains IDE, VS Code) are reported as label-only items (`skill: null`,
`in_taxonomy: false`) until the taxonomy gets them. Skill ids are always the canonical ids from `skills.json`.

## What is read, and how
- **Dependency files** (at most 10 per repo, shallowest first): `requirements*.txt/.in`, `Pipfile`,
  `pyproject.toml`, `environment.yml`, `setup.py` (a regex only, never executed), `package.json`.
- **Source files** (`.py .ipynb .js .jsx .ts .tsx .mjs`): imports, and SQL inside string literals.
  Notebooks: code cells only, never outputs. Folders such as `node_modules/`, `venv/`, `dist/`, `build/`
  and `*.min.js` are never read.
- **File list only** (no fetch): `.sql` files, CI/build files, `.idea/`, `.vscode/`.
- SQL in a lowercase or mixed-case string needs extra proof (a clause word after the table name, `*`,
  or a placeholder), so prose such as "Please select an option from the list" is not matched.
  Comments are never tested.
- Everything fetched is untrusted text: parsed with simple regexes and JSON/TOML readers, never executed,
  never written to disk.
- Only repos the validator counted are scanned (no forks, no failed repos), newest first.

## Caps, and what `complete` means
Defaults: 40 repos, 20 source files per repo, 400 source-file reads in total (shared fairly between
repos), files over 400,000 bytes skipped, `api_calls_used` reports every call made.

`complete` is `True` only if every eligible repo was scanned without problems. It is `False` when the
repo cap was reached, a rate limit or bad token stopped the scan (`status: "partial"`, earlier findings
are kept), a repo failed, a file tree was truncated by GitHub, or the file budget ran out
(see `incomplete_reasons`).

**A skill missing from `skills` means "not found" only when `complete` is `True`. When `complete` is
`False` it means "unknown".** Even when complete, source files beyond the per-repo cap are not read
(`per_repo[].source_files_skipped` tells you how many), so "not found" means "not found in dependency
files, file names, and the source files that were read".

## Adding a signal
Edit `skillverify/data/tech_signals.json`: add an entry with a `label`, the canonical `skills` it proves,
and any of `python_packages` (names as in dependency files), `python_imports`, `js_packages`.
Leave `skills` empty (or use `also_report_as`) for a label-only tool. Run `python -m pytest`.
The loader refuses the file with a `TechScannerError` if a skill id is not in the taxonomy or if a package
name appears in two entries. Package names are compared PEP 503-normalised (Python packages) or
lowercase (Python imports, JavaScript packages).

## Known limits
- Java dependencies are not parsed (only the `pom.xml` / `build.gradle` file signals).
- `.idea/` and `.vscode/` are often gitignored, so their absence proves nothing. Never read it as "does not use PyCharm".
- A declared dependency does not prove the code uses it (hence medium, not strong).
- A tiny language share can be GitHub misclassification (for example MATLAB).
- Concepts such as REST APIs, Agile or Machine Learning cannot be detected; `compare_claimed_skills` reports
  them as `not_detectable_from_github`, never "not found".
- Imports inside Python docstrings are ignored, but an import mentioned in an ordinary string can still match.
- Tools missing from the taxonomy (YOLO, PyCharm, VS Code) are label-only until the taxonomy gets them.

## For the future scoring step
Use each skill's `confidence` and the scan's `complete` flag, and never treat "missing while incomplete"
as a failed claim. `compare_claimed_skills` is a demo helper, not the Repo Fingerprint Score.

## Follow-up fixes
A live run on a real account (4 repos) led to four small changes:

1. **Python and notebooks.** GitHub labels notebook code "Jupyter Notebook", never "Python", so a
   notebook-heavy candidate looked like they had no Python. Each `per_repo` record now has
   `python_files_read` (always present; it counts `.py` files and notebooks that were fetched,
   parsed, and contain some non-blank code). `compare_claimed_skills` uses two extra sources, only
   for Python: `language_stats_notebook` (GitHub's "Jupyter Notebook" bytes and percent) and
   `python_source_files` (`{"files": N}`, strong). The normal `language_stats` source is unchanged.
   No "Python" item is added to `skills`.
2. **Trace amounts are weak.** A language-statistics source below `MIN_LANGUAGE_SHARE_PERCENT`
   (1.0%) gets `"trace": true` and counts as weak, because a few stray files can be GitHub
   misclassification. The status is still `found`. At or above 1.0% nothing changes. A skill's
   confidence is still the best among its sources.
3. **`@types/` packages** in `package.json` are no longer listed in `unmapped_packages` (Python packages
   are unaffected, and a mapped `@types/...` name would still match).
4. **Wording.** A skill that is confirmable only from language statistics (for example Rust) and that
   the scan cannot add evidence for is reported as `not_found` even when the scan is incomplete,
   because the language statistics are already complete. This applies only when the validator result
   actually contained language statistics. Skills the scan can add evidence for (the signal map, SQL,
   Python) keep `not_found_scan_incomplete`. `TechScanner.scannable_skills()` and the optional
   `scan_skills` argument of `compare_claimed_skills` describe that set.

For the future scoring step: a `trace` source is weak evidence, and "Jupyter Notebook" bytes count toward Python.

## Follow-up 2
Three more changes after live runs on real accounts:

1. **Python file counts cannot hide a trace.** If language statistics exist and the combined share of
   `Python` plus `Jupyter Notebook` is below `MIN_LANGUAGE_SHARE_PERCENT` (1.0%), the
   `python_source_files` source counts as weak and gets `"trace": true`. With a real share (or with no
   language statistics at all) it stays strong. A skill's confidence is still the best among its sources.
2. **`basis` key.** `compare_claimed_skills` entries with status `not_found`, `not_found_scan_incomplete` or
   `not_detectable_from_github` now say why: `full_scan` (not found and the scan was complete),
   `language_stats_only` (not found in GitHub's language statistics, which are complete even when the
   scan itself was not), `incomplete_scan`, or `not_checkable`. `found` and `not_in_taxonomy` entries have
   no `basis`.
3. **Inferred evidence.** `skillverify/data/tech_implications.json` lists concepts that real evidence implies
   (for example Express.js implies Node.js, scikit-learn implies Machine Learning). If a claimed skill has
   no direct evidence and an implier is present in the scan, the entry is `found` with a source
   `{"type": "inferred", "from": [...], "repos": N}`. It is medium confidence at best (weak if every implier
   is weak), never strong, never replaces direct evidence, and appears only in `compare_claimed_skills`,
   never in `scan()` output. If no implier is present nothing changes: absence of an implier proves
   nothing. To add a row, add `{"skill": ..., "implied_by": [...]}`; the loader refuses unknown skill
   ids, duplicate skills, empty lists, and a skill implied by itself. Keep implications to cases that
   are genuinely certain (do not add pandas or NumPy as implying Machine Learning).

For the scoring step: treat `inferred` as weaker than direct evidence, and remember that everything here is
evidence from the candidate's repos, not proof of who wrote the code.
