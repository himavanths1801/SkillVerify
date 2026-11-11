# SkillVerify API

A small FastAPI layer that runs the real Phase 1 pipeline for one candidate:

```
JD text/file -> JD parser (Gemini) -> CV file -> CV parser (Gemini)
             -> GitHub validate + extended tech scan -> score_candidate -> JSON
```

The `result` it returns is exactly what `score_candidate` produces (the same shape as `web/src/mocks/score.ts`).
It is evidence for a human interviewer, never a hiring decision. The API is live only: no demo fallback, no saved
results, no score from partial data.

**Privacy.** The CV and job description text are sent to Google (Gemini) for parsing, exactly as in Phase 1; use only
documents you may process. Results live in memory only (at most 20 jobs, dropped after 1 hour). Nothing is written to
`output/` or to the taxonomy files; uploaded files are held in memory and touch disk only in a temporary folder while
the text is extracted, which is deleted straight away.

## Install and start

```bash
pip install -r requirements.txt -r requirements-cv.txt -r requirements-api.txt
```

Put `GEMINI_API_KEY` and `GITHUB_TOKEN` in `.env` or the environment (as in Phase 1), then, from the project root:

```bash
uvicorn api.app:app --port 8000
```

CORS allows only `http://localhost:5173` and `http://127.0.0.1:5173` (GET and POST, no credentials).

## Routes

### `GET /api/health`

```json
{"status": "ok", "gemini_key_set": true, "github_token_set": true,
 "model_chain": ["gemini-3.6-flash", "gemini-3.5-flash-lite"],
 "models": [{"model": "gemini-3.6-flash", "state": "cooling_down", "seconds_left": 87},
            {"model": "gemini-3.5-flash-lite", "state": "ready", "seconds_left": 0}],
 "limits": {"analysis_timeout_seconds": 360, "questions_timeout_seconds": 360, "scan_max_repos": 30}}
```

Booleans only for the keys; a key or token value is never returned. `limits` holds numbers only (see "Time limits
and big accounts"). `state` is `ready`, `cooling_down` or
`unavailable`. See "Gemini model chain" below.

### `POST /api/analyses` (multipart form) -> `202 {"id": "<uuid>"}`

| Field | Notes |
|---|---|
| `jd_text` **or** `jd_file` | Exactly one. File: `.pdf`, `.docx` or `.txt`, up to 5 MB. Pasted text is limited to 1 MB (use a file for longer). |
| `cv_file` | Required. `.pdf`, `.docx` or `.txt`, up to 5 MB. |
| `github_username` | Optional. Used for the scan if given; else the username found in the CV; if neither exists the score runs without GitHub evidence, as `score_candidate` does (status `no_github_evidence`). A username you type is used even if the CV names another account; the score then carries the flag `github_username_mismatch`. |
| `job_name` | Optional, 1-60 characters of letters, digits, `.`, `_`, `-`. Default: the JD file name, else `job`. |

Bad input returns `422` with a plain message:

```json
{"error": {"code": "invalid_input", "message": "The CV file is required.", "retryable": false}}
```

### `GET /api/analyses/{id}`

```json
{"id": "...", "state": "running", "stage": "Scanning repositories", "result": null, "error": null,
 "meta": {"candidate_name": null, "job_title": "Junior Data Analyst", "model": "gemini-3.6-flash",
          "fallback_used": false, "scan": {"repo_cap": 30, "repos_total": 118, "repos_used": 30, "capped": true},
          "timings": {"Reading the job description": 3.2, "Checking GitHub": 41.2}}}
```

`meta` is a sibling of `result` (the `result` object is exactly what `score_candidate` returns and never contains it).
Both values are plain strings of at most 80 characters, or `null`. They start as `null`, are filled as each step
finishes, and are kept after the job is `done`. They are for display only and are never logged.

- `job_title` (filled after the job description is read): plain code on the JD text, no AI. Take the first non-empty
  line; cut it at the first ` - `, ` – `, ` — ` or ` | ` and keep the part before; trim spaces and trailing
  punctuation. It is used only if it has at most 80 characters and at most 10 words, has no full stop, and does not
  start with "about", "we ", "our ", "the ", "job description", "overview" or "description" (any capitalisation).
  Otherwise `null`. Examples: `Junior Data Analyst - Retail Analytics Team` -> `Junior Data Analyst`;
  `Backend Developer (Python)` stays as it is.
- `model` (filled when the run starts): the Gemini model this run uses for all its calls, or `null` if none could be
  chosen. `fallback_used` is `true` when that model is not the first model of the chain. Both are kept after the job
  is `done` and replaced when the job is retried.
- `scan` (filled when the scan stage starts, `null` before and when there is nothing to scan): how many repositories
  the analysis looked at. `repo_cap` is the `SCAN_MAX_REPOS` setting; `repos_total` is the number of eligible repos
  (not forks, not empty) before the cap; `repos_used` is how many the scan received; `capped` is `true` when
  `repos_total > repos_used`. `repos_total` and `repos_used` are `null` when the GitHub client cannot report them.
  The analysis `result` is NOT changed by this: for a capped account the Score screen's "N of M repositories scanned"
  reads "N of N" until a later small website change shows `meta.scan`.
- `timings`: `{ "<stage name>": seconds }`, rounded to 1 decimal, filled as each stage finishes (a failed or timed-out
  run also gets the stage that was running, with the time spent so far). Stage names only.
- `candidate_name` (filled after the CV is read): the name the CV parser checked against the CV text. If it is all
  capitals (`PRIYA RAMAN`) it becomes title case (`Priya Raman`); other capitalisation is left alone. The parser's
  unverified name hint is never used. No verified name means `null`.

`state` is `queued`, `running`, `done` or `failed`. `stage` is one plain sentence: "Analyzing, this can take a moment."
(queued), "Reading the job description", "Reading the CV", "Checking GitHub", "Scanning repositories", "Scoring",
"Done". When `done`, `result` holds the scoring JSON; when `failed`, `error` holds an error object. An unknown or
expired id returns `404 {"message": "..."}`. Up to 2 jobs run at once; others wait as `queued`. A job that runs longer
than `ANALYSIS_TIMEOUT_SECONDS` (default 360, 6 minutes) fails with `timeout`; its error object also has `stage`.

### `POST /api/analyses/{id}/retry` -> `202 {"id": "<same id>"}`

Only for a failed job whose error has `retryable: true`. It starts a fresh run from the same stored inputs. Any other
job returns `409 {"message": "..."}`.

### Questions: a sub-resource of a finished analysis

After an analysis is `done` (and its `result.status` is `ok`), the recruiter's Question review screen asks for 3 to 5
coding questions written from the candidate's own GitHub code. The engine is `skillverify_questions/`; the logic is in
`docs/QUESTIONS.md`. The analysis `result` is never changed: question data lives only under these routes, in memory,
and expires with the analysis.

| Route | What it does |
|---|---|
| `POST /api/analyses/{id}/questions` | Starts a run. `202` with the status object. `409 analysis_not_ready` (unknown id, analysis not `done`, or result status not `ok`); `409 questions_already_started` (a run is queued, running or done: GET it instead). |
| `GET /api/analyses/{id}/questions` | The status object. `404` for an unknown analysis; `state: "idle"` when no run was started. |
| `POST /api/analyses/{id}/questions/retry` | Only for a `failed` run whose error is retryable; `202` with the status object, otherwise `409 {"message"}`. It reuses the cached selection and file contents, so GitHub is only read again if GitHub was what failed. It picks the next Gemini model by the usual chain rules. |
| `POST /api/analyses/{id}/questions/replace` | Synchronous. Optional body `{"after": "<dropped question id>"}` (context only; at most 64 characters). Writes ONE new question from the next unused reserve file and returns `{"question": {...}, "skipped_skills": [...], "dropped": [...]}` (`dropped` lists this call's own failed attempts). `409 no_replacement_available` when no other file is left (the error object then carries `dropped` if attempts failed); `409 analysis_not_ready` before the run is done; `422 invalid_input` for a bad body; `503` with an error object for Gemini failures (`ai_rate_limited` with `fallback_available`, `ai_all_limited`, `ai_busy` ...). The server never uses a file that was already used in the run, whatever the recruiter dropped. |

Status object: `{"id", "state": "idle" | "queued" | "running" | "done" | "failed", "stage", "result", "error",
"meta": {"model", "fallback_used"}}`. Stages, in order: "Choosing code", "Reading files", "Writing questions",
"Checking questions". `meta.model` and `meta.fallback_used` mean the same as for analyses (one Gemini model per run).

`result` when `done`:

```json
{
  "schema_version": 1,
  "status": "ok",
  "message": null,
  "generated_at": "2026-10-29T10:00:00+00:00",
  "limits": {"min": 3, "max": 5},
  "questions": [
    {"id": "q_1a2b3c4d", "kind": "write_code", "question": "...", "language": "python", "starter_code": "",
     "reference_notes": "...",
     "why": {"repo": "orders-analysis", "path": "analysis.py", "skill": "Python", "tier": "must_have",
             "claimed": true, "top_ranked": true,
             "reason": "Asked because the CV claims Python and this is the highest-ranked file in the repos that uses it.",
             "symbols": ["clean_orders"]}}
  ],
  "skipped_skills": [{"skill": "Docker", "tier": "nice_to_have", "reason": "not_found_in_code"}],
  "dropped": [{"skill": "SQL", "tier": "must_have", "repo": "orders-analysis", "path": "api/src/stock.ts",
               "attempts": 2, "reasons": ["unknown_symbol", "unknown_sql_name"]}],
  "counts": {"candidates_skills": 3, "repos_considered": 1, "files_fetched": 4, "files_ranked": 4,
             "questions": 3, "dropped": 0}
}
```

- `status`: `ok` (3 or more questions, `message` null), `too_few` (1 or 2: "Only N question(s) could be written from the
  candidate's code. A session needs at least 3."), `none` (0: "No code was found that shows the job's skills, so no
  questions could be written."). Below 3 is reported honestly and never padded.
- `why.top_ranked` is true only for the rank-1 file of the skill (false for reserve picks and replacements; the
  `reason` sentence says "highest-ranked" only when it is true). For a SQL question `language` is always `sql`.
- A `fix_code` question also has `failing_example` `{"input", "expected", "actual"}` (short plain strings, at most 120
  characters) and its `question` text ends with the sentence `Example: {input} should give {expected}, but the starter
  code gives {actual}.` appended by code. `write_code` questions have neither (additive, `schema_version` stays 1).
- `dropped` (additive, `schema_version` stays 1): one entry per question that failed its checks twice, with
  `attempts` (1 or 2) and `reasons`, a list of codes from the closed set `bad_json`, `bad_length`, `wrong_file`,
  `unknown_file_reference`, `unknown_symbol`, `unknown_call`, `url_or_email`, `secret_like`, `duplicate`,
  `fix_without_starter`, `not_sql_starter`, `unknown_sql_name`, `no_real_sql_in_file`, `fix_without_example`,
  `fix_example_vacuous`, `fix_not_modified` (final attempt first). Metadata
  and codes only. `counts.dropped` equals its length. `skipped_skills` lists only skills with zero questions.
- `kind` is `write_code` or `fix_code`. Skip reasons are a closed set: `not_found_in_code`,
  `not_checkable_from_code`, `no_usable_file`, `question_failed`, `not_enough_room`.
- **Recruiter-only fields:** `why` and `reference_notes`. The student-visible fields are only `question`, `language`
  and `starter_code`. Any student-facing endpoint added later must build its answer from those three and must never
  include `why` or `reference_notes`.
- A Gemini 429 mid-run stops the run and fails it with `ai_rate_limited` (marking the model exactly like an analysis
  does); no partial result is returned. Errors never contain file text, code or model output.
- The candidate's code excerpts are sent to Google's Gemini to write the questions (secrets are redacted first). The
  Upload screen's privacy line must say so (a later web step).

## Errors

An error object is `{"code", "message", "retryable"}`; an `ai_rate_limited` error also has `fallback_available`
(`true` if the next run would use another model, `false` if none is ready), and a `timeout` error also has `stage`
(the fixed stage name that ran out of time; left out if no stage had started). The codes are a closed set:

| Code | When | Retryable |
|---|---|---|
| `invalid_input` | HTTP 422 only: the form was not valid | no |
| `jd_unreadable` | the job description file or the JD parser result could not be used | no |
| `cv_unreadable` | the CV file or the CV parser result could not be used | no |
| `github_user_not_found` | GitHub has no such user | no |
| `github_unavailable` | GitHub unreachable, request limit reached, or the scan failed | yes |
| `ai_busy` | Gemini kept failing temporarily, even after the retries | yes |
| `ai_rate_limited` | Gemini answered HTTP 429 | yes |
| `ai_all_limited` | every model in the chain is resting (rate limited or unknown); no Gemini call was made | yes |
| `analysis_not_ready` | questions: HTTP 409 only, the analysis is not `done`, unknown, or has no usable score (also: replace before the run is done) | no |
| `questions_already_started` | questions: HTTP 409 only, a run already exists (also: a replacement is already being written) | no |
| `no_replacement_available` | questions: HTTP 409 only, no other file is left to ask about | no |
| `ai_not_configured` | Gemini key missing or rejected, or the model name was unknown on a later call | no |
| `timeout` | the analysis (or a questions run) ran longer than its time limit; the error names the `stage` | yes |
| `internal_error` | anything unexpected | yes |

Messages never contain a traceback, file content, a path, a URL with a token, or exception text.

## How failures are handled

**GitHub: live, plain error.** GitHub runs normally and its real output feeds the score. Only if it fails does the job
fail, immediately, with no retries and no fallback: a missing user is `github_user_not_found`; a rate limit,
an unreachable GitHub, or a failed scan is `github_unavailable`. A partial score is never returned. (A scan that
finishes but is marked incomplete is not a failure: the score already accounts for incomplete scans, as in Phase 1.)

**Gemini: quiet retries, then the error.** The provider already retries HTTP 500/502/503/504 twice (2 s, 6 s). On top
of that the API retries temporary failures (HTTP 500/502/503/504 or a network problem) after 5, 10 and 20 seconds
(`GEMINI_OUTER_RETRY_DELAYS` in `api/settings.py`). While it retries the job stays `running` with the normal stage, so
the user sees nothing. If every retry fails the error is `ai_busy`. HTTP 429 is not retried (`ai_rate_limited`); instead the next run uses another model (below).

## Time limits and big accounts

### What was found (Step 0, by reading the code; no real call was made)

- **Time limits.** Before this change `api/settings.py` had one constant, `JOB_TIMEOUT_SECONDS = 6 * 60` (360 s), used by
  the analysis (`api/jobs.py`) and by the questions run (`api/questions.py`). Now there are two settings with the same
  default: `ANALYSIS_TIMEOUT_SECONDS` and `QUESTIONS_TIMEOUT_SECONDS`, default **360 each** (unchanged: the old value was
  already above 300, so it was not raised).
- **After a timeout the worker thread keeps running.** The run lives in a daemon thread; the manager waits for it with
  `thread.join(limit)`, and if it is still alive the job is marked `failed`/`timeout` and the thread is *abandoned*.
  Python cannot stop a thread and the frozen validator and scanner make blocking `requests` calls, so an abandoned run
  keeps calling GitHub (and Gemini) until it finishes or fails. Its late writes are ignored (the job is no longer
  running), so it cannot change a result, but it still uses GitHub's rate limit and one of the process's threads.
  Stopping it would need a cancel check inside the frozen code (for example a flag read in `GitHubValidator._get`).
- **How the frozen code lists repos.** `GitHubValidator.fetch_repos(username)` pages through
  `/users/{u}/repos?type=owner&per_page=100` and returns every public repo; `validate()` calls it and then, one repo
  after another, `_inspect_repo` for each: forks (`fork: true`) are skipped cheaply, every other repo costs a languages
  call plus up to 5 commit pages. There is no repo cap, no time budget and no client injection in the validator (it
  builds its own `requests.Session`); repos come back in GitHub's order. Empty repos are not excluded: they cost the
  calls and get a 409 = zero commits. `TechScanner.scan` takes the validator's *counted* repos without an error, sorts
  them newest first by `last_commit_date`, keeps the first `max_repos` (constructor parameter, default 40; the API passed
  40) and reports `repos_scanned`, `repos_not_scanned`, `scan_settings.max_repos` and `complete` / `incomplete_reasons`
  (`"repo cap reached"`). Per repo it makes about 3 calls plus up to `max_files_per_repo` (20) file reads, within a total
  budget of 400 file reads.
- **Stages and the slow one.** "Reading the job description", "Reading the CV" (Gemini), "Checking GitHub"
  (`validate`), "Scanning repositories" (`scan`), "Scoring" (local). For an account with over a hundred repos the two
  GitHub stages dominate: "Checking GitHub" makes 2 or more sequential calls for *every* non-fork repo, with no cap at
  all, and "Scanning repositories" then makes roughly 20 to 25 calls for each of up to 40 repos. Together they can pass
  the whole limit; which of the two runs out first depends on the account.

### The cap: method (a) plus (b)

- **(a) used for the scan.** The scanner's existing `max_repos` parameter now receives `SCAN_MAX_REPOS` (default 30,
  was 40).
- **(b) used for the validator.** (a) alone leaves "Checking GitHub" unbounded, so the API also wraps the `fetch_repos`
  method of the validator *instance* (`api/repocap.py`; nothing in `skillverify/` is edited). When an account has
  more than `SCAN_MAX_REPOS` eligible repos (not forks, not empty: GitHub `size` 0), the listing the validator works
  from holds only the newest `SCAN_MAX_REPOS` of them, by `pushed_at` newest first, name as the tie-break (a missing date
  is oldest). An account within the cap is listed exactly as before (forks and empty repos included), so its result
  does not change. Everything else of the validator passes through.
- Because the validator only sees the capped listing, a capped account's `result` describes those repos and its
  repository summary reads "N of N". `meta.scan` is what tells the truth (see above).

### Settings

| Setting (environment or `.env`) | Default | Allowed |
|---|---|---|
| `ANALYSIS_TIMEOUT_SECONDS` | 360 | 30 to 1800 |
| `QUESTIONS_TIMEOUT_SECONDS` | 360 | 30 to 1800 |
| `SCAN_MAX_REPOS` | 30 | 5 to 200 |

They are read once, lazily (importing the app never reads `.env`; real environment variables win). A missing,
non-numeric or out-of-range value uses the default and writes ONE plain log line that names the setting and never
shows the value. `GET /api/health` returns the three numbers under `limits`.

### Timings and the timeout error

`meta.timings` is measured with a monotonic clock. Each finished stage writes one log line, for example
`stage="Scanning repositories" seconds=41.2`. A `timeout` error keeps its code, `retryable: true` and message, and has
an extra `stage`. A timed-out questions run does the same with its own stage names ("Choosing code", "Reading files",
"Writing questions", "Checking questions"). A retry starts clean (`timings` and `scan` reset).

## Gemini model chain

Different Gemini models have separate quotas, so the API keeps an ordered **chain** of models. The first is the main
model. One run uses **one** model for all its Gemini calls (job description and CV), chosen when the run starts, so a
result is never a mix of models. The choice depends only on server state, so Retry and a fresh Analyze behave the same.

**Configuration**, read at start-up (the `.env` file is read too):

- `GEMINI_MODEL_CHAIN`: comma-separated model names in priority order, for example
  `gemini-3.6-flash,gemini-3.5-flash,gemini-3.5-flash-lite`.
- Otherwise `GEMINI_MODEL` (if set) goes first, followed by the default chain.
- Otherwise the default chain, `DEFAULT_MODEL_CHAIN` in `api/settings.py` (model names change).
- Names must match the provider's pattern (letters, digits, `.`, `_`, `-`, up to 100 characters); invalid entries are
  dropped with one log line that counts them (the text is not logged). Duplicates are removed, the order is kept, and
  the chain is capped at 8 models.

**Rules** (in memory only; they reset when the server restarts):

- A run that ends with a Gemini 429 rests that model for 120 s (`RATE_LIMIT_COOLDOWN_SECONDS`). If that model's
  previous outcome was also a 429 with no successful run in between, it rests for 1800 s
  (`REPEATED_LIMIT_COOLDOWN_SECONDS`): it is probably out of daily quota. The job fails with `ai_rate_limited` and
  `fallback_available`.
- Every run (new or retried) picks the first model that is not resting. When a rest ends, that model is eligible
  again and, being earlier in the chain, wins over the backups.
- A successful run clears the model's "limited in a row" count. Other errors (503 after the quiet retries, key errors,
  parser errors, GitHub errors) change nothing.
- If the **first** Gemini call of a run says the model does not exist (HTTP 404), that model is set aside for 3600 s
  (`MODEL_UNAVAILABLE_SECONDS`) and the run continues with the next model, without user action, trying each model at
  most once.
- If every model is resting, the job fails at once with `ai_all_limited` and no Gemini call is made.

**Choose your fallback models.** Run this by hand (it calls Gemini once with your key and prints only model names):

```
python -m api.tools.list_models
```

It prints one model name per line for the models that support `generateContent`. Put the ones you want in
`GEMINI_MODEL_CHAIN`, best first. It never prints your key or the raw answer.

## Manual live test

This calls the real Gemini and GitHub APIs (your keys, your limits). From the project root, with the server running:

```bash
curl -s -X POST http://localhost:8000/api/analyses \
  -F "jd_file=@examples/sample_jds/data_analyst.txt" \
  -F "cv_file=@sample_cvs_v2/cv1_priya_data_analyst.pdf" \
  -F "github_username=octocat"
```

It prints `{"id": "..."}`. Then poll (repeat until `state` is `done` or `failed`):

```bash
curl -s http://localhost:8000/api/analyses/<id>
```

Leave out `github_username` to use the one found in the CV. If the job failed with `"retryable": true`:

```bash
curl -s -X POST http://localhost:8000/api/analyses/<id>/retry
```
