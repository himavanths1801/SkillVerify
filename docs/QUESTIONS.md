# SkillVerify question generator (Phase 2, backend)

After an analysis is `done`, the Question review screen needs 3 to 5 coding questions. `skillverify_questions/` builds them
from the candidate's real GitHub code, and every question carries a provable "why this question" record. The routes are
in `docs/API.md` ("Questions"). The website part is a later step.

## The locked logic (do not redesign)

1. **Filter, then rank.** Junk files (vendored, generated, test, tiny, secret-bearing, non-code) are dropped. What is
   left is ranked by how much real logic it has.
2. **No Gemini in file selection.** Plain code picks the files: skill matching through the existing skill taxonomy,
   filtering, ranking and a plain tie-break. If no file shows a skill, that skill is skipped. Gemini never chooses and
   never fills in for a missing file.
3. **Gemini only writes the question** from the chosen file. The system then checks it against the real file. If the
   check fails, Gemini gets ONE more try (with the list of failed checks). If it fails again the question is dropped and
   its skill is recorded as skipped.
4. **Reuse the GitHub scan.** Nothing is scanned again. Only the file contents that are needed are fetched.

Skill priority: the JD's must-have skills first (heavier weight first, then the API order), then nice-to-have if there is
room. Maximum 5 questions, minimum 3 for a session; below 3 is reported (`too_few` / `none`), never padded.

## How this was built on the existing code

- **Score rows (`skillverify/scoring/score.py`).** `skills[]` has `skill`, `category`, `tier`, `tier_basis`, `weight`,
  `claimed`, `github_status`, `confidence`, `basis`, `credit`, `points`, `reason`. The real **tier values are
  `must_have` and `nice_to_have`** (weights 3 and 1). `github_status` comes from the scanner's `compare_claimed_skills`:
  `found`, `not_found`, `not_found_scan_incomplete`, `not_detectable_from_github`, `not_in_taxonomy`, or `null` when the
  GitHub evidence could not be used. `confidence` is `strong`/`medium`/`weak`/`null`; `reason` is one of `confirmed`,
  `weak_evidence`, `unconfirmed`, `not_found_on_github`, `scan_incomplete`, `not_detectable`, `not_claimed`,
  `no_github_evidence`. `tier_basis` is free text from the JD parser and is not used here.
- **Taxonomy.** `skillverify/data/skills.json` (165 skills) plus `skills_extra.json` (YOLO, PyCharm, VS Code), loaded by
  `load_extended_taxonomy()` exactly as the analysis pipeline does. A skill has `id`, `category`, `aliases` and
  `github_languages`.
- **GitHub validator and scanner.** `GitHubValidator.validate()` returns `repos[]` records (`name`, `is_fork`,
  `counted`, `primary_language`, `languages`, `last_commit_date`: the newest of the user's commits in the window, else
  `pushed_at`) and a `summary`. Forks are listed with `counted: false` and never inspected; a repo that failed has an
  `error` field. The tech scanner's result (`tech_stack`) has `skills[]` (each with the `repos` that evidence it and
  `examples`) and `per_repo[]` with each repo's `default_branch`. The token is `GITHUB_TOKEN`, read from the environment
  or `.env` with python-dotenv (`GitHubConfigError` when missing); it is only sent in a header.
- **Gemini and the pipeline.** `GeminiProvider.generate_json(system, user)` is JSON-oriented (it asks Gemini for
  `application/json` and returns a dict, or raises `LLMCallError`), so no text parsing is needed and the provider is
  untouched. A run builds the provider once from `deps.provider_factory(model)`, wraps it in `RetryingProvider` (quiet
  retries for 500/502/503/504 and network problems; 429 is never retried), and takes the model from the shared
  `ModelChain` (`api/models.py`). `api/jobs.py` keeps each analysis in memory with private `intermediates`
  (`jd`, `cv`, `github`).
- **Decision for "reuse the scan": the preferred path.** The pipeline already keeps the scan on the in-memory job:
  `intermediates["github"]` is the validator result with the scan's `tech_stack` merged in. The question code reads
  that private attribute (it never appears in `result`, `meta`, any response or any log), so no repo listing call and no
  scan is repeated. The only GitHub calls are one recursive tree call per considered repo and the file contents.
  The fallback (listing repos again) was not needed.

## The package

| Module | What it does |
|---|---|
| `config.py` | Every tunable constant (below). |
| `files.py` | Path filter, language detection, generated-header and notebook handling, logic score, names defined in a file, secret redaction. |
| `signals.py` | Skill to detection patterns, built from the taxonomy and the scanner's signal map. |
| `select.py` | Candidate skills, repo choice, tree filter, ranking, selection, reserve. Pure. |
| `github_files.py` | Tree and file contents through an injectable client (`requests`). Plain errors, no retries. |
| `reasons.py` | The fixed "why" sentences. |
| `writer.py` | The Gemini prompt, the call, the checks and the single repair attempt. |
| `service.py` | Orchestration, stages, the cached selection, replacement. |

### Selection

- **Candidate skills.** Only the JD's skills with `github_status == "found"`. The others are recorded in
  `skipped_skills`: `not_detectable_from_github` is `not_checkable_from_code`; everything else (`not_found`, incomplete
  scan, no evidence) is `not_found_in_code`. A skill outside the JD is never invented.
- **Repos.** At most `MAX_REPOS_CONSIDERED` counted, non-fork, error-free repos: first those that the scan says evidence
  the needed skills (most skills first; a skill written in a language is also evidenced by that language in the repo),
  then the newest `last_commit_date`, then the name.
- **Files.** One recursive tree call per repo. The junk filter runs on the paths and sizes before anything is fetched.
  Survivors are fetched biggest first (path alphabetical on a tie), at most `MAX_FILES_PER_REPO_FETCHED` per repo and
  `MAX_FILE_FETCHES` in total. Notebooks are reduced to their code cells. After fetching: a file whose first lines say
  "auto-generated", "generated by" or "do not edit", or with fewer than `MIN_LOGIC_LINES` logic lines, is dropped, and
  anything that looks like a secret is redacted before the text is stored or sent.
- **Skill matching.** A file shows a skill when `signals.py` finds a signal line:
  imports and package names from the scanner's own signal map (Python and JavaScript), the use of the module or its
  alias, SQL keywords (SQL files) and embedded queries, `git` commands (shell), the file being written in the skill's
  language, and a whole-word match on the canonical name (case-sensitive) or on aliases of at least `MIN_ALIAS_CHARS`
  letters. Comments never count. The signal-line count is the second ranking key; for a language skill it is the number
  of definitions (at least 1).
- **Ranking.** Logic score, then (tie-break, in this order) more signal lines, repo pushed more recently, repo name and
  path alphabetical. No randomness: the same inputs always give the same selection.
- **Selection.** Each skill in priority order takes its best file that no earlier skill took, up to `MAX_QUESTIONS`.
  Skills with no usable file are skipped (`no_usable_file`); skills left out because the limit was reached are
  `not_enough_room`. The **reserve** is ordered: the best unused file of each skill that got no question, then each
  asked skill's next-best files, then the other files of the skills that got none.
- **Dropped questions.** A question that fails its checks twice is dropped (`question_failed`) and the next reserve file
  is tried so the target count can still be met. At most `MAX_QUESTIONS + MAX_REPLACEMENT_CANDIDATES` questions are
  attempted in a run. If a skill finally gets a question from another file it is no longer listed as skipped.

### Constants (`skillverify_questions/config.py`)

| Constant | Value | Meaning |
|---|---|---|
| `MAX_QUESTIONS` / `MIN_QUESTIONS` | 5 / 3 | Most questions in a session / the fewest for `ok`. |
| `MAX_REPLACEMENT_CANDIDATES` | 3 | Extra files tried when a question is dropped or a replacement is requested. |
| `MAX_REPOS_CONSIDERED` | 5 | Repos whose trees are listed. |
| `MAX_FILES_PER_REPO_FETCHED` | 8 | File contents fetched per repo. |
| `MAX_FILE_FETCHES` | 40 | File contents fetched in total. |
| `MAX_FILE_BYTES` / `MIN_FILE_BYTES` | 200 000 / 400 | File size window (checked from the tree, before fetching). |
| `MIN_LOGIC_LINES` | 15 | Fewer non-blank, non-comment lines means "tiny". |
| `TOP_FILES_PER_SKILL` | 3 | Best files kept per skill (the first is used, the rest feed the reserve). |
| `LOGIC_LINES_CAP`, `DEFINITION_WEIGHT`, `DEFINITIONS_CAP`, `CONTROL_FLOW_WEIGHT`, `CONTROL_FLOW_CAP` | 300, 4, 40, 2, 60 | Logic score = min(lines, 300) + 4 x min(definitions, 40) + 2 x min(control flow, 60). The caps stop a huge file from winning on size. |
| `MAX_PROMPT_CODE_LINES` | 250 | Lines of the file shown to Gemini (the checks use the whole file). |
| `MAX_QUESTION_ATTEMPTS` | 2 | Gemini calls per question: the first try and one repair. |
| `QUESTION_MIN_CHARS` / `QUESTION_MAX_CHARS` | 60 / 700 | Question length. |
| `STARTER_MAX_LINES` / `STARTER_MAX_CHARS` | 40 / 2500 | Starter code size. |
| `NOTES_MAX_CHARS`, `MAX_SYMBOLS`, `DUPLICATE_PREFIX_CHARS` | 800, 8, 80 | Notes length, symbols per question, duplicate key length. |
| `SKIP_FOLDERS` | see the file | Vendored, build and test folders, matched on a folder name at any depth, ignoring case. Includes `libs`, `external`, `extern`, `deps`, `third-party`, `thirdparty`, `vendored` (copies of other people's code kept inside a repo). Plain `lib` is deliberately NOT listed: many projects keep their own real source there. A name that only contains the word (`libs_helper`) is not excluded. These files are dropped before any content is fetched. |
| `GENERATED_NAME_PATTERNS`, `TEST_NAME_PATTERNS`, `SECRET_NAME_PATTERNS` | see the file | File name rules. |
| `GENERATED_HEADER_LINES`, `GENERATED_HEADER` | 5, regex | Lines scanned for a "generated" notice. |
| `EXTENSION_LANGUAGE` | `.py .ipynb .sql .js .jsx .ts .tsx .java .kt .c .h .cpp .cs .go .rs .rb .php .r .sh .scala` | The only code extensions, and the language shown to the student (the model never chooses it). |
| `LINE_COMMENT`, `BLOCK_COMMENT_LANGUAGES` | per language | What counts as a comment. |
| `MIN_ALIAS_CHARS` | 4 | Shorter aliases are not matched as plain words. |
| `MIN_EMBEDDED_SQL_STATEMENTS` | 2 | Real statements a non-`.sql` file needs to show SQL. |
| `SQL_NON_NAMES` | keywords and functions | Words never checked as table or column names. |
| `DROP_REASON_CODES` | 16 codes | The closed set of drop reasons. |
| `FAILING_EXAMPLE_MAX_CHARS`, `EXAMPLE_SENTENCE` | 120, see the file | Limits of a fix question's failing example and the sentence code appends. |
| `AMBIGUOUS_EXTENSIONS`, `PRODUCT_NAMES_WITH_EXTENSION` | see the file | Names such as `Node.js` or `obj.c` that look like file names but are not. |

## The "why" record

Every question has `why`: `{repo, path, skill, tier, claimed, top_ranked, reason, symbols}`. `top_ranked` is true only
when the chosen file is rank 1 for that skill among all files that showed the skill in this run; it is false for anything
from the reserve (a skill's second-best file, any later pick, and every replacement). `reason` is one of six fixed
sentences (the first three in `config.py`, the last three in `reasons.py`; chosen by `reasons.reason_for`), never written
by Gemini:

`top_ranked` true:
- must-have and claimed: `Asked because the CV claims {skill} and this is the highest-ranked file in the repos that uses it.`
- nice-to-have: `Asked because the job lists {skill} as a nice-to-have and this is the highest-ranked file in the repos that uses it.`
- must-have, not claimed: `Asked because the job requires {skill} and this is the highest-ranked file using it, although the CV does not list it.`

`top_ranked` false:
- must-have and claimed: `Asked because the CV claims {skill} and this file shows it in the candidate's repos.`
- nice-to-have: `Asked because the job lists {skill} as a nice-to-have and this file shows it in the candidate's repos.`
- must-have, not claimed: `Asked because the job requires {skill} and this file shows it, although the CV does not list it.`

## SQL questions

- **The file must really contain SQL.** A `.sql` file always qualifies. Any other file shows the skill SQL only with at
  least `MIN_EMBEDDED_SQL_STATEMENTS` (2) real statements inside string or template literals (detector:
  `signals.embedded_sql_statements`). A real statement is a verb plus its clause: `SELECT ... FROM`, `INSERT INTO`,
  `UPDATE ... SET`, `DELETE FROM`, `CREATE TABLE` or `JOIN ... ON`. A bare `SELECT 1`, a function called `query(` and a
  keyword in a comment do not count. In a one-line quoted string a lower-case statement also needs a SQL-looking shape
  (`*`, `,`, `.`, `(`, `=`, `$`, `?`, `where`, `values`, `group by`, `order by`) so English prose does not count; multi-line
  text (backticks, triple quotes) is trusted. A file that does not qualify drops out of the SQL ranking and the next
  one is used. A connection-pool file (a pool, a `query()` wrapper, one `SELECT 1`) does not qualify.
- **SQL questions are SQL.** The system sets `language` to `sql` (the model never chooses it). `starter_code` must be
  SQL only; for a `fix_code` question built on an embedded query the starter is the statement itself. A starter
  containing `const `, `def `, `function `, `await ` or `import ` is rejected (`not_sql_starter`).
- **Names must exist.** Every identifier used as a table or column (anything in backticks in the question that looks
  like a name, and the words after `FROM`, `JOIN`, `INTO`, `UPDATE`, `TABLE` in the starter code) must appear as a whole
  word, ignoring case, in the chosen file or another fetched file of the same repo (`unknown_sql_name`). Not checked:
  SQL keywords and common functions (`config.SQL_NON_NAMES`), placeholders (`$1`, `?`, `:name`), names defined in the
  starter (aliases, CTEs), and names the question asks the candidate to create ("create/add/new/called/named ..."
  before the name). `reference_notes` is not checked for names. The fetched file texts of each repo are kept from the
  selection step; nothing more is fetched.
- Asking about a non-`.sql` file without enough real statements fails `no_real_sql_in_file`.
- A failed SQL check follows the normal rule: one repair attempt with the failed checks by name, then drop.

## Fix questions

A bug cannot be proven by plain code, so a `fix_code` question must carry a concrete, checkable symptom. The model
returns `failing_example: {input, expected, actual}` (required for `fix_code`; null or absent for `write_code`, where it
is ignored). The prompt says: the bug must change observable behaviour (a wrong return value, wrong output or an
exception; a style change, rename or equivalent code is not a bug); describe it as being in the starter code below,
never in the candidate's own code; the starter is the real code with exactly one small deliberate change.

Checks (same repair rule: one more attempt with the failed checks by name, then drop):

- `fix_without_example`: `failing_example` missing, not an object, a value that is not a string, empty after trimming,
  over 120 characters, or containing a line break.
- `fix_example_vacuous`: `expected` equals `actual` after trimming and lower-casing.
- `fix_not_modified`: every non-blank line of the starter (ignoring leading/trailing spaces) is in the real file, so
  nothing was changed; or, for a starter of two or more non-blank lines, fewer than half of its lines are in the file
  (a rewrite, not a small change). A one-line starter only needs to differ from the file: the "at least half" rule
  cannot hold for a single changed statement.
- The example values also pass the URL/email and secret checks.

After validation, code appends ONE sentence to the question: `Example: {input} should give {expected}, but the starter
code gives {actual}.` Write questions never get it. A fix question's object also has `failing_example`
(`{input, expected, actual}`, trimmed); write questions have no such key. The recruiter can still edit the whole question
text. `reference_notes` is unchanged and recruiter-only. `schema_version` stays 1 (additive).

## Dropped questions in the result

`result.dropped` is `[{skill, tier, repo, path, attempts, reasons}]`: metadata and codes only, never code, question text
or model output. `attempts` is 1 or 2; `reasons` holds the codes of the FINAL failed attempt, then the first attempt's
codes if different, without duplicates. `counts.dropped` equals the length of the list. The reason codes are a closed
set (`config.DROP_REASON_CODES`), each mapped from the failed check:

| Code | Checks |
|---|---|
| `bad_json` | `invalid_json`, `wrong_types`, `kind_invalid` |
| `bad_length` | `question_length`, `starter_too_long`, `notes_too_long` |
| `wrong_file` | `file_mismatch` |
| `unknown_file_reference` | `unknown_file_mentioned` |
| `unknown_symbol` | `symbol_not_in_file`, `symbols_missing` |
| `unknown_call` | `function_not_in_file` |
| `url_or_email`, `secret_like`, `duplicate` | the check of the same name |
| `fix_without_starter` | `starter_code_missing` |
| `not_sql_starter`, `unknown_sql_name`, `no_real_sql_in_file` | the SQL checks above |
| `fix_without_example`, `fix_example_vacuous`, `fix_not_modified` | the fix-question checks above |

`skipped_skills` lists only skills that ended with ZERO questions. A skill that has a question and also a dropped attempt
appears only in `dropped`; a skill whose attempts all failed keeps `question_failed` in `skipped_skills` and has its
`dropped` entries. Each drop writes one log line with codes only, for example
`question dropped skill=SQL reasons=unknown_sql_name`. The replace route returns the same `dropped` list for its own
failed attempts (also as `error.dropped` of a `409 no_replacement_available`).

## Writing and checking a question

The candidate's file text is untrusted data: it is wrapped in `<<<CANDIDATE_FILE_BEGIN>>>` / `<<<CANDIDATE_FILE_END>>>`,
the delimiter text (any run of three angle brackets) is removed from the file text, and the model is told to ignore any
instructions inside the code. The model returns JSON only:
`{kind: "write_code" | "fix_code", question, starter_code, file, symbols, reference_notes}`.

All of these checks must pass (a failed check is named in the repair call):

1. **Shape:** a JSON object with the right types (`invalid_json`, `wrong_types`); `kind` is one of the two values
   (`kind_invalid`); `question` is 60 to 700 characters (`question_length`); `starter_code` at most 40 lines and 2500
   characters (`starter_too_long`); a `fix_code` question has starter code (`starter_code_missing`); `reference_notes`
   at most 800 characters (`notes_too_long`).
2. **File:** `file` equals the chosen path exactly (`file_mismatch`). Every file-like name in the question, the starter
   code and the notes must be the chosen file or exist in that repo's file tree (`unknown_file_mentioned`).
3. **Names:** every name in `symbols` is a whole word in the real file and, for Python that parses, a real `def`,
   `class` or assignment target (`symbol_not_in_file`); every `name(` in the question appears as a word in the file
   (`function_not_in_file`); `symbols` may be empty only when the file has no detectable definitions
   (`symbols_missing`).
4. **Safety:** no URLs or email addresses (`url_or_email`); nothing the secret redactor would change (`secret_like`).
5. **No duplicate** of an earlier question in the run: the same normalised first 80 characters (`duplicate`).
6. **SQL questions only:** the SQL rules above (`no_real_sql_in_file`, `not_sql_starter`, `unknown_sql_name`).
7. **Fix questions only** (`kind` = `fix_code`): see "Fix questions" below.

## Recruiter-only fields

`why` and `reference_notes` are for the recruiter. The student-visible fields are only **`question`, `language` and
`starter_code`**. The student endpoints (a later step) must build their answer from those three fields and must never
include `why` or `reference_notes`.

## Privacy

The candidate's code excerpts (up to 250 lines per question, after secret redaction) are sent to Google's Gemini to write
the questions. The Upload screen's privacy line must be updated to say so in the web step. Logs carry only analysis ids,
stage names, counts and error codes: never file text, question text, model output or keys.

## Manual live test (curl)

Run the API (`uvicorn api.app:app --port 8000`) with `GEMINI_API_KEY` and `GITHUB_TOKEN` set. Never paste a key into a
command. First an analysis:

```
curl -s -X POST http://localhost:8000/api/analyses -F "jd_file=@jd.pdf" -F "cv_file=@cv.pdf"
curl -s http://localhost:8000/api/analyses/<id>
```

When its `state` is `done` and `result.status` is `ok`, start the questions and poll:

```
curl -s -X POST http://localhost:8000/api/analyses/<id>/questions
curl -s http://localhost:8000/api/analyses/<id>/questions
```

If it failed with a retryable error, retry (the selection and file contents are reused), or ask for one more question:

```
curl -s -X POST http://localhost:8000/api/analyses/<id>/questions/retry
curl -s -X POST http://localhost:8000/api/analyses/<id>/questions/replace -H "Content-Type: application/json" -d "{\"after\": \"q_1a2b3c4d\"}"
```
