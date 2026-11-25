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
| `DROP_REASON_CODES` | 18 codes | The closed set of drop reasons. |
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

`result.dropped` is `[{skill, tier, repo, path, attempts, reasons, causes, shape_fields}]`: metadata and codes only, never code, question text
or model output. `attempts` is 1 or 2; `reasons` holds the codes of the FINAL failed attempt, then the first attempt's
codes if different, without duplicates. `causes` (additive) names why a `bad_json` answer was unusable, from the
closed set `config.ANSWER_CAUSES` (see "Bad JSON and file matching"). `counts.dropped` equals the length of the list. The reason codes are a closed
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
| `multiple_tasks` | `multiple_tasks` (the question offers alternatives) |
| `bug_hint_in_starter` | `bug_hint_in_starter` (a comment the model added to a fix starter points at the bug and could not be removed) |

`skipped_skills` lists only skills that ended with ZERO questions. A skill that has a question and also a dropped attempt
appears only in `dropped`; a skill whose attempts all failed keeps `question_failed` in `skipped_skills` and has its
`dropped` entries. Each drop writes one log line with codes only, for example
`question dropped skill=SQL reasons=unknown_sql_name causes=none shape_fields=none`. The replace route returns the same `dropped` list for its own
failed attempts (also as `error.dropped` of a `409 no_replacement_available`).

## Bad JSON and file matching

A live run for a candidate with 7 repos wrote 1 question (`too_few`): all 7 other questions were dropped as `bad_json`
after 2 attempts, the files did not always fit the skill (Flask from `app.js`, CSS from `cleaning.py`, an HTML question
from a Python file), and the review screen said "Only 1 question(s)" while it showed 2. This section is what was found
(read-only, before any change) and what was changed. Nothing in the locked logic changed: filter then rank, no AI in
file selection, the model only writes the question, system checks, one repair attempt, then drop, reuse the scan.
`schema_version` stays 1; every new field is additive.

### What was found (Step 0)

- **Where an answer is parsed.** The model's reply text was parsed by the provider itself: the frozen
  `GeminiProvider._parse_answer` and `GroqProvider._parse_answer` each did a strict `json.loads` and raised an
  `LLMCallError` ("... returned no usable answer", "... answer was not valid JSON", "... JSON but not an object").
  `writer.write_question` treats an error whose text matches `_ANSWER_SHAPE` as the check `invalid_json`; `writer.check`
  adds `invalid_json` (the answer is not an object), `wrong_types` (a required field is missing or the wrong type, or
  more than 8 symbols) and `kind_invalid`. All three checks map to the drop code `bad_json`, so `bad_json` hid at least
  four different causes (nothing returned, cut off, prose or fences around the object, wrong shape).
- **The Groq request body.** `model`, `messages`, `temperature: 0` and `response_format: {"type": "json_object"}` (JSON
  mode). **No output cap and no reasoning setting were sent**, so Groq's own defaults applied. An empty `content` was
  "no usable answer"; a cut-off reply was just broken JSON ("not valid JSON"): `finish_reason` was ignored, so a cut-off
  could not be told from garbage. `gpt-oss-120b` is a reasoning model and its reasoning tokens count against the output
  limit, which is a plausible cause of empty or cut-off replies; this is NOT proven (no live call is made here), which is
  why this step only makes the cause visible.
- **Which model the repair used.** The same one: a run kept one current model (`_RunProvider.model`) and only changed it
  when a call failed, so the repair attempt asked the model that had just failed the checks.
- **How a skill is matched to files.** `select.rank_for_skill` asks `Signals.count(skill, language, text)`: the file's
  language, the skill's import lines, and a whole-word match of the skill name or a longer alias in the code (comments
  removed). Strings count, so `"Flask"` inside `app.js` or `"CSS"` inside `cleaning.py` made those files candidates.
  The extension was only used to pick the language (`files.language_of`), never to ask whether the file can show the
  skill.

### Fix 1: tolerant parsing and a safe cause for every bad answer

`skillverify_questions/answer.py` reads a reply the same way for Groq and for the Gemini provider the question writer
uses (`gemini_questions.QuestionGemini`, the frozen provider with only its reply reading replaced; the reading chain keeps
the plain one). Plain code, no `eval`, nothing repaired beyond this:

1. surrounding code fences (three backticks, with or without `json`) are removed;
2. a text that is JSON as a whole is used as it is (an object is the answer; a top-level array or scalar is
   `wrong_shape`);
3. otherwise the text before the first `{` is dropped and the first balanced `{...}` (strings and escapes respected) is
   parsed.

If that still fails, the cause is one of a closed set (`config.ANSWER_CAUSES`):

| Cause | When |
|---|---|
| `empty_response` | the provider returned no text |
| `cut_off` | the provider said the output limit was reached (Groq `finish_reason: "length"`, Gemini `finishReason: "MAX_TOKENS"`) and nothing usable came, or the braces never balance |
| `not_json` | text without a JSON object (or a balanced `{...}` that is not JSON) |
| `wrong_shape` | JSON, but not an object, or a required field is missing or has the wrong type (also `kind` not `write_code` or `fix_code`) |

A complete object is used even if the provider said the limit was reached. The error messages keep the words the writer
already recognises, so an unusable reply is still a repairable `bad_json`.

`result.dropped[]` entries keep `reasons` (the code stays `bad_json`) and gain `causes`: the codes of the final failed
attempt, then the first attempt's if different, no duplicates (an empty list when the drop was not about the answer's
shape). The drop log line gains them: `question dropped skill=Python reasons=bad_json causes=cut_off` (`causes=none` when
empty). Only codes are logged and saved, never the reply.

**Output cap.** `QUESTIONS_MAX_OUTPUT_TOKENS` (range 500 to 8000) is sent to Groq as `max_completion_tokens`, the
documented cap. The prompt said the default should be "the value used today"; today **nothing** is sent, so the default
is `0`, meaning "send no cap" (Groq's own default, exactly as before). A missing value is `0`; a non-numeric or
out-of-range value is also `0` with one plain log line that names the setting and never shows the value. It is shown
under `limits` in `GET /api/health`. JSON mode (`response_format: json_object`) was already used: Groq's documentation
lists it for `openai/gpt-oss-120b` (read from the documentation, not checked live). Groq's documentation also lists a
`reasoning_effort` setting (`low`, `medium`, `high`) for the gpt-oss models; it is NOT sent and no default was raised:
if drops show `causes=cut_off` or `empty_response`, that and a higher cap are the two things to try, with evidence.

### Fix 2: the repair goes to a different model

When the first attempt for a question fails a check (including `bad_json`), the single repair attempt goes to a
different chain entry than the first attempt whenever another entry is ready (not resting). Only when no other entry is
ready does it use the same one. The next question starts on the first ready entry again. This is not a handover (nothing
failed), so `meta.fallbacks` does not change; handover, pacing and rests from the previous build are unchanged (a repair
call that hits a limit hands over like any call). `meta.models_used` counts the model that wrote the final accepted
question. One log line per repair elsewhere: `repair on another model from=<a> to=<b>` (model names only).

### Fix 3: skills only match files they can show

`config.SKILL_EXTENSIONS` maps a skill name (lower case, as in the taxonomy) to the extensions of files that can show it
(for example `flask` to `.py`; `react` to `.jsx .tsx .js .ts`; `html` to `.html .htm .jsx .tsx .vue .svelte .ejs`).
`signals.extension_allowed(skill, path)` is the plain check; `select.rank_for_skill` applies it, so a file whose
extension is not allowed is not a candidate for that skill (and never reaches the model, not even as a reserve). A skill
with no remaining file is skipped with the existing reason `no_usable_file`.

**Skills that are not in the map are unrestricted**, exactly as before: the map is deliberately conservative (a wrong
entry would hide real evidence), SQL keeps its own rules, and a new skill needs an entry only when there is evidence of
the problem. `python`, `pandas` and `numpy` also allow `.ipynb`, because notebooks are where that work usually lives and
the file filter already treats a notebook as Python. To extend: add a lower-case key and a tuple of extensions to
`SKILL_EXTENSIONS`; nothing else changes.

### Fix 4: the "only N" message matches the screen

A replacement is saved beside the run, so the saved result still says how many questions the run itself wrote. When
replacements exist, the result the API returns (and only that; the saved original is never rewritten) gets
`counts.questions` = the run's questions + the replacements, `status` recomputed from that count (`too_few` becomes `ok`
at the minimum) and `message` rebuilt with the same sentence and the right N ("Only N question(s) could be written from
the candidate's code. A session needs at least 3."; `null` when `ok`). It applies after a restart too (the replacements
are read from the saved run), and a regenerate starts again from zero. The server cannot know which questions the
recruiter removed on the screen, so "effective" means run questions plus replacements.

## Shape detail, normalising and one task

A live run wrote 2 of a possible 5 questions. Four drops were `bad_json` with the cause `wrong_shape` (which field was
wrong was not recorded), two were a "fix" question whose starter code was not changed from the file or a question that
was too long or too short, and one kept question asked for three tasks joined by "or" and "alternatively". Free-tier
models vary, so the goal is fewer avoidable drops and clearer reasons, not zero drops. The checks stay as strict as
before: nothing here lets an invented symbol, call, file, URL or SQL name through.

### What was found (Step 0)

- **Where `wrong_shape` is decided.** `writer.check` (the first lines). The answer must be a JSON object; `question`,
  `starter_code`, `file` and `reference_notes` must be strings and `symbols` a list of strings (`wrong_types`); `kind`
  must be `write_code` or `fix_code` (`kind_invalid`); more than 8 symbols is also `wrong_types`. All of these map to the
  drop code `bad_json` and the cause `wrong_shape`. A top-level array or scalar is `wrong_shape` already in
  `answer.parse_answer`. `failing_example` is NOT part of this: a fix question with a missing or unusable example fails
  its own check `fix_without_example`.
- **The answer format the writer asks for** (`writer.SYSTEM_PROMPT`): JSON only, exactly
  `{"kind": "write_code" | "fix_code", "question": string, "starter_code": string, "file": string, "symbols": [string],
  "reference_notes": string, "failing_example": {"input", "expected", "actual"} | null}`. The model is NOT asked for a
  `language`: the question's language is set by code from the file's extension (so a `language` the model volunteers is
  never used).
- **What the repair attempt told the model.** It already listed the failed checks: `build_prompt` added "Your previous
  answer failed these checks" and one line per check name with a fixed sentence from `PROBLEM_TEXT` (never the model's
  text). This change extends that table and those lines instead of adding a second mechanism.
- **Where the length, "not modified" and URL checks are decided.** `bad_length` = `question_length` (60 to 700
  characters), `starter_too_long` (more than 40 lines or 2500 characters) and `notes_too_long` (more than 800
  characters). `fix_not_modified` (`_starter_problem`): the starter code is a verbatim excerpt of the file (every
  non-blank line appears in it), or fewer than half of its lines are in the file. `url_or_email`: a URL or an e-mail
  address in the question, starter code, notes or failing example.

### Fix 1: which field was wrong

An answer that is `wrong_shape` records WHICH fields failed, as names from the closed set `config.SHAPE_FIELDS` (`kind`,
`question`, `starter_code`, `file`, `symbols`, `reference_notes`). `dropped[]` entries gain `shape_fields` (empty when the
cause is not `wrong_shape`): the final attempt's names, then the first attempt's, in the set's order, no duplicates. The
drop log line gains `shape_fields=...` (`none` when empty). Names only, never a value.

`config.FAILING_EXAMPLE_FIELDS` (`failing_example.input`, `.expected`, `.actual`) are NOT in `shape_fields`: a bad example
keeps its own drop code (`fix_without_example`), so changing it to `bad_json` would change existing behaviour. The part
names appear in the repair hint of that check instead (see Fix 4).

### Fix 2: normalising harmless variants

Before the checks, `writer.normalise_answer` fixes only these differences (closed tables in `config.py`), and the result
then goes through ALL the existing checks unchanged:

| Field | Accepted variants |
|---|---|
| `kind` | any case; spaces, underscores and hyphens count the same: `write_code`, `write code`, `write-code`, `code` become `write_code`; `fix_code`, `fix code`, `fix the bug`, `bug fix`, `fix` become `fix_code`. Anything else stays and fails `kind_invalid`. |
| `language` | `js` and `node`, `nodejs` become `javascript`, `ts` becomes `typescript`, `py` becomes `python` (any case). Any other name is left exactly as given. The value is not used (the language comes from the file). |
| `starter_code` | `null` or missing becomes an empty string; a list of strings is joined with newlines (a fix question still needs real starter code). |
| aliases | `question` also from `question_text` or `prompt`; `starter_code` also from `starter` or `code`; `reference_notes` also from `notes` or `solution_notes`. Only when the canonical field is absent (a canonical field that is present, even `null`, is never replaced); the first alias present wins. |
| whitespace | `question`, `reference_notes` and `file` are trimmed. `starter_code` keeps its indentation. |

Nothing is invented: a missing `question` or `reference_notes` still fails (and `shape_fields` names it), and a plain-text
answer is never turned into a question. Each step that fires is a code from `config.NORMALISE_CODES`
(`kind_normalised`, `language_normalised`, `starter_code_defaulted`, `starter_code_joined`, `alias_question`,
`alias_starter_code`, `alias_reference_notes`, `whitespace_trimmed`). They are kept only in one log line,
`answer normalised skill=Python codes=kind_normalised,language_normalised`, and counted in `result.counts.normalised`
(the number of steps that fired in the run). No content is ever stored.

### Fix 3: one task per question

The writer prompt says the question asks for exactly ONE task (one function, component or change) and must never offer
alternatives ("or", "alternatively", "either", "or else"). A new system check `multiple_tasks` (drop code
`multiple_tasks`, so 17 codes at that point) rejects a question text that matches `config.ALTERNATIVE_PATTERNS`: "alternatively",
"as an alternative", "or else", "or instead", and "either ... or <write|add|implement|modify|create|build|fix|refactor|
change|update|rewrite|extend>". It does not match ordinary sentences such as "true or false", "null or undefined",
"positive or negative" or "either way". It runs with the other checks and uses the normal repair attempt and drop path.
`WRITER_SETTINGS_VERSION` is now 2 (the prompt and the checks changed), so a saved question set made by the old writer is
not reused for the same account.

### Fix 4: the repair hint says what failed

The one repair attempt lists each failed check with a fixed sentence from `PROBLEM_TEXT` (a closed table, one entry per
check; never the model's own text). The sentences were extended, for example `fix_not_modified`: "the starter code must
differ from the file: change at least one line to introduce the bug"; `question_length`: "keep the question between the
allowed lengths"; `multiple_tasks`: "ask for exactly one task, with no alternatives"; `unknown_symbol`, `unknown_call`,
`unknown_file_reference` and `url_or_email`: "Only name things that appear in the file; do not include URLs or e-mail
addresses". For `wrong_types` and `kind_invalid` the line ends with `(fields: ...)` naming the failed fields, and for
`fix_without_example` it names the unusable `failing_example.*` parts. The repair is still the single repair attempt.

## Starter code for write questions

A live run produced 5 questions, all `write_code`, all with an EMPTY `starter_code` ("In app.js, add a debounce to the
search input listener."): the student does not have `app.js`, so the editor opened blank and the question referred to
code they could not see. A fix question already carries real starter code from the file; a write question now gets a
short, real excerpt of the file too.

### What was found (Step 0)

- **`fix_code` today.** The model writes `starter_code`: the real code with one small deliberate change. `writer.check`
  requires it to be non-empty (`starter_code_missing`), at most 40 lines and 2500 characters (`starter_too_long`), a
  modified excerpt (`fix_not_modified`: a verbatim excerpt of the file, or fewer than half of its lines in the file, is
  rejected) and free of secrets, URLs and e-mail addresses (`secret_like`, `url_or_email`, which look at the question,
  the starter, the notes and the failing example).
- **`write_code` today.** The prompt never asked the model for a starter for a write question, and the checks only
  applied the length, secret and URL checks to whatever it wrote: nothing related it to the file, so an invented or
  empty starter passed. In practice it was empty.
- **Where the inputs are.** `service._try` has the fetched file text (`pick.file.text`, already redacted) and the language
  (`pick.file.language`), and the accepted question has the model's `symbols`. So an excerpt can be cut by code.

### The change: a real excerpt, cut by code

For every accepted `write_code` question that is not a SQL question, `skillverify_questions/excerpt.py` builds
`starter_code` from the real file, verbatim, around the symbol the question names. The model does not write these lines;
the prompt now tells it to leave `starter_code` empty for write questions and to name the function, class or variable it
means in `symbols`.

- **The target is an existing function or method** (for example "implement `parse` in `BaseScraper`" and `parse`
  exists): its signature stays and its WHOLE body (a docstring too) becomes one placeholder line in the file's own
  comment style, `# TODO: implement` (Python) or `// TODO: implement` (JavaScript, TypeScript, Java). The excerpt starts
  with the file's import lines (at most 10, dropped first when the room is short) and the enclosing class header, plus the
  members of the class before the target that fit (Python; nearest first). A brace-language method also gets the closing
  braces of its class.
- **Anything else** (a variable, a class, an area, something the question will add): the largest enclosing block that fits
  the limit, for the place where the symbol is used that shows the most context, followed by one closing line
  `# TODO: add your code here` (`//` in JS, TS and Java). A new method, handler or hook is not in the file, so it is not in
  the excerpt.
- **Limits.** At most the existing 40 lines and 2500 characters; whole lines only, indentation exactly as in the file; a
  block is never cut in the middle (a block that does not fit is replaced by the next smaller enclosing block). A line with
  a URL, an e-mail address, a redacted secret or anything the secret redactor would change is never included: the
  excerpt stops before it (a smaller block is used) or is dropped.
- **No answer is shown.** If a target function exists but its body cannot be replaced safely (a one-line body, an arrow
  function without a block, a function nested in a function, brackets the scanner cannot balance), nothing is shown for it
  at all, not even as an "area".
- **Languages.** Python (parsed with the standard library, exact line ranges), JavaScript, TypeScript and Java (a small
  scanner that skips comments, strings, template literals and text blocks and gives up when brackets do not balance). Any
  other language, or a file where a safe cut is not possible, leaves `starter_code` empty exactly as before.
  **A question is never dropped, and a run never fails, only because no excerpt could be built.**
- **The model's own starter.** If it returned one, it is used only when no excerpt can be built AND every line of it is a
  line of the file, a blank line, a comment or a placeholder line (and it is safe and within the limits). Otherwise it is
  replaced by the excerpt, or by an empty string.
- **Fix and SQL questions behave exactly as today** (the new code does not touch them).

The placeholder lines come from the closed table `config.STARTER_PLACEHOLDERS` (`implement` and `add`, one line per
language). Replacement questions use the same rules.

### Counts and the log line

`result.counts` gains `starter_built` (write questions whose starter was cut from the file) and `starter_empty` (write
questions with no starter); a starter that is the model's own is counted in neither. They survive a resume. One log line
per accepted write question: `starter question=q_1a2b3c4d lines=12 source=code` (`source` is `code`, `model` or `none`;
an id, a number and a word, never any text). The excerpt itself is public repo text and is only ever in the question's
`starter_code`, exactly like the starter code of a fix question.

`WRITER_SETTINGS_VERSION` is now 3, so a saved question set made by the old writer is not reused. The review screen
already shows `starter_code` with its show-all control; nothing in `web/` changed.

## No bug hints in fix starters

A live fix-the-bug question for a React file (`Chat.jsx`, `handleSend`) had `// BUG: wrong event name emitted` inside its
starter code: the model added a comment that points straight at the bug, so the student had nothing to find. A fix
starter must look like the real file with ONE bug planted in the code itself, with no comment that explains, labels or
hints at it.

### What was found (Step 0)

- **How a fix starter is built and checked.** The model writes the whole `starter_code`: the real code with one small
  deliberate change. `writer.check` requires it to be non-empty (`starter_code_missing` -> drop code
  `fix_without_starter`), at most 40 lines and 2500 characters, and not a verbatim excerpt (`fix_not_modified`), and it
  runs the secret and URL checks over it with the rest of the answer (`secret_like`, `url_or_email`). The file-match rule
  is inside `fix_not_modified`.
- **How lines are compared with the real file.** `_starter_problem` strips every line and checks it against the set of
  stripped, non-blank lines of the real file: if every starter line is in the file, nothing was changed; if fewer than half
  of the lines are in the file, too much was changed. Before this change comments were ordinary text in that comparison, so
  an added comment counted as "a changed line" and could make an unmodified starter look modified.
- **Comment styles the starter-excerpt code already understood.** `excerpt.py` (write questions) masks `//` and
  `/* */` comments and the contents of strings, template literals and Java text blocks for JavaScript, TypeScript and
  Java, and uses the Python parser for Python. It never removed or inspected comments; it only skipped them when matching
  brackets. The new `comments.py` is a separate, more careful scanner for comments themselves.

### The change

Before the checks run, every `fix_code` starter is compared with the real file, and every comment in the starter that is
NOT in the real file is removed by code (`comments.strip_added_comments`):

- a whole-line comment (the line disappears), a trailing comment (the code on the line stays), a block comment (also over
  several lines), a JSX `{/* ... */}` comment (its braces go too) and an HTML `<!-- -->` comment;
- a comment that exists in the real file stays as it is (compared without its markers and case; a comment of the starter
  that is part of a longer comment of the file also counts as existing);
- docstrings are strings, not comments, and are never touched.

The scanner is conservative. Python: `#` outside strings. JavaScript, TypeScript, Java and the other C-like languages
(`c`, `cpp`, `csharp`, `go`, `rust`, `kotlin`, `scala`): `//` and `/* */`, plus `<!-- -->` in JS and TS. Strings,
template literals (also over several lines) and, for JS and TS, regular expressions are skipped, so `//` or `#` inside
them is never a comment. Any other language (shell, Ruby, R, PHP, SQL) is left exactly as before. When the scanner cannot
tell safely what a line is (an apostrophe in JSX text, an unterminated string, a regular expression that does not close, a
block comment that never closes), the line is left alone.

**Hint words.** `config.HINT_WORDS` is a small closed list (`bug`, `buggy`, `wrong`, `incorrect`, `mistake`,
`error here`, `fix this`, `fixme`, `should be`, `off-by-one`, `off by one`, `broken`, `typo`; whole words, any case). If
a line that was left alone for the reason above is not a line of the real file and has a hint word after a comment
marker, the question is dropped with the new drop reason `bug_hint_in_starter` (the closed set is now 18 codes). A line
like that without a hint word is left as it is. No comment text is echoed anywhere.

**Order of checks.** Strip first, then the existing checks run on the cleaned starter. `fix_not_modified` compares the
starter WITHOUT comments with the real file WITHOUT comments, so a starter that differs only by an added or removed
comment is "not modified" (and a real code change plus an added hint comment is kept, with the comment removed and the bug
intact).

**Prompt and repair.** The writer prompt says fix starter code is the real file lines with the bug planted in the code
itself, with no comments added or changed, no markers such as BUG, FIXME or TODO and no explanation inside the code (the
explanation belongs only in `reference_notes`). The repair hint table has a fixed sentence for the new code: "remove any
comment from the starter code that explains or points at the bug". The repair is still the single repair attempt.

**Counts and logs.** `result.counts.hints_stripped` (always present, 0 when none) is the number of comments removed from
the starters of the questions that were kept; it survives a resume. One log line per affected question:
`starter question=q_1a2b3c4d hints_stripped=2` (an id and a number). Write-code and SQL questions are not touched.
`WRITER_SETTINGS_VERSION` is now 4, so a saved question set made by the old writer is not reused; replacement questions
use the same rules. The review screen needed no change.

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

## Which calls use which provider

| Call | Provider |
|---|---|
| Writing and repairing a question (`POST /questions`, retry) | `QUESTIONS_MODEL_CHAIN`: Groq first, then Gemini |
| The replacement route (`POST /questions/replace`) | the same chain |
| Reading the job description (JD Parser) | Gemini only |
| Reading the CV (CV Parser) | Gemini only |

The JD and CV parsers are Phase 1 code (`skillverify/`) that builds a `GeminiProvider` itself, so they stay on Gemini.
The writer talks to whichever provider answers through one small interface, `generate_json(system, user) -> dict`
(prompt in, JSON out), implemented by `GeminiProvider` and by `skillverify_questions/groq_provider.py`. Selection, the
validation checks, the SQL and fix-code rules and the one-repair rule do not know which provider answered. How Groq is
called and how its failures move the run to Gemini is described in `docs/API.md`, "Groq first for questions". Groq's
default model `openai/gpt-oss-120b` is listed in Groq's model documentation with JSON Object Mode (read from the
documentation, not checked with a live call); the request uses temperature 0 and `response_format: json_object`.

## Handover, pacing and saved sets

- **Handover.** Every model of the question chain hands over to the next one on a rate limit or a temporary failure; the
  write in progress is made again on the next model with the same inputs and does not use the one repair attempt. A run
  is only ended when every model is resting for longer than `QUESTIONS_MAX_WAIT_SECONDS`, and then the questions already
  written are saved as a `partial` run that a retry continues from. `meta.models_used` and `meta.fallbacks` say who wrote
  what; `meta.model` is the model that wrote the most questions.
- **Pacing.** `QUESTIONS_MIN_GAP_SECONDS` between two calls to the same model.
- **Saved sets.** A finished set is saved with a fingerprint (GitHub username, score, the (skill, tier) pairs asked about,
  `WRITER_SETTINGS_VERSION`). **Bump `WRITER_SETTINGS_VERSION` in `skillverify_questions/config.py` whenever the prompt, the
  checks or the selection rules change**, so old sets are not served for new rules.
- Details, settings and what is stored: `docs/API.md`, "Model chain and saved results".

## Recruiter-only fields

`why` and `reference_notes` are for the recruiter. The student-visible fields are only **`question`, `language` and
`starter_code`**. The student endpoints (a later step) must build their answer from those three fields and must never
include `why` or `reference_notes`.

## Privacy

The candidate's code excerpts (up to 250 lines per question, after secret redaction) are sent to the model that writes
the questions: Groq when `GROQ_API_KEY` is set (it is the primary), otherwise, or when Groq falls back, Google's Gemini.
The Upload screen's privacy line must name both providers. Logs carry only analysis ids,
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
