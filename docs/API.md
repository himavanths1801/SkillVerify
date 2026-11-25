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
{"status": "ok", "gemini_key_set": true, "groq_key_set": true, "github_token_set": true,
 "model_chain": ["gemini-3.6-flash", "gemini-3.5-flash-lite"],
 "models": [{"model": "gemini-3.6-flash", "state": "cooling_down", "seconds_left": 87},
            {"model": "gemini-3.5-flash-lite", "state": "ready", "seconds_left": 0}],
 "reading_model_chain": ["gemini-3.6-flash", "gemini-3.5-flash-lite"],
 "reading_models": [{"model": "gemini-3.6-flash", "state": "cooling_down", "seconds_left": 87},
                    {"model": "gemini-3.5-flash-lite", "state": "ready", "seconds_left": 0}],
 "questions_model_chain": ["groq:openai/gpt-oss-120b", "gemini-3.6-flash", "gemini-3.5-flash-lite"],
 "questions_models": [{"model": "groq:openai/gpt-oss-120b", "state": "ready", "seconds_left": 0},
                      {"model": "gemini-3.6-flash", "state": "cooling_down", "seconds_left": 87},
                      {"model": "gemini-3.5-flash-lite", "state": "ready", "seconds_left": 0}],
 "limits": {"analysis_timeout_seconds": 360, "questions_timeout_seconds": 360, "scan_max_repos": 30,
            "questions_min_gap_seconds": 3, "questions_max_wait_seconds": 25,
            "reading_min_gap_seconds": 2, "reading_max_wait_seconds": 20, "questions_max_output_tokens": 0},
 "storage": {"enabled": true}}
```

`storage.enabled` says whether saved results are on (see "Model chain and saved results"). Booleans only for the keys (`groq_key_set` too); a key or token value is never returned. `reading_model_chain` and
`reading_models` are the chain that reads the job description and the CV (see "Reading chain"); `model_chain` and
`models` are the same chain under their older names; `questions_model_chain` and `questions_models` are the
question writer's chain, which can start with Groq (see "Groq first for questions"). `limits` holds numbers only (see "Time limits
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
- `model` (filled as soon as a reading model is picked): the model that read the CV (or, before the CV is read, the
  job description), or `null` if none could be chosen. `fallback_used` is `true` when either document was read by a
  model that is not the first model of the reading chain, whether read now or served from the saved readings. Both are
  kept after the job is `done` and replaced when the job is retried.
- `readers`: `{"jd": {"model": "gemini-3.6-flash", "cached": false}, "cv": {"model": "gemini-3.5-flash-lite",
  "cached": true}}`, which model read each document and whether the reading came from the saved readings with no model
  call (`model` is then the model that read it the first time). An entry is `null` until that document is reached.
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
| `POST /api/analyses/{id}/questions` | Starts a run. Optional body `{"regenerate": true}` (see "Model chain and saved results"). `202` with the status object; a new run whose account, score, asked skills and writer version match a saved set is `done` at once (`meta.cached`). `409 analysis_not_ready` (unknown id, analysis not `done`, or result status not `ok`); `409 questions_already_started` (a run is queued, running, done or saved: GET it instead, or send `regenerate`); `422` for a body that is not empty or `{"regenerate": true|false}`. |
| `GET /api/analyses/{id}/questions` | The status object. `404` for an unknown analysis; `state: "idle"` when no run was started. |
| `POST /api/analyses/{id}/questions/retry` | Only for a `failed` run whose error is retryable; `202` with the status object, otherwise `409 {"message"}`. It reuses the cached selection and file contents, so GitHub is only read again if GitHub was what failed. It picks the next model of the question chain by the usual chain rules. |
| `POST /api/analyses/{id}/questions/replace` | Synchronous. Optional body `{"after": "<dropped question id>"}` (context only; at most 64 characters). Writes ONE new question from the next unused reserve file and returns `{"question": {...}, "skipped_skills": [...], "dropped": [...]}` (`dropped` lists this call's own failed attempts). `409 no_replacement_available` when no other file is left (the error object then carries `dropped` if attempts failed); `409 analysis_not_ready` before the run is done; `422 invalid_input` for a bad body; `503` with an error object for Gemini failures (`ai_rate_limited` with `fallback_available`, `ai_all_limited`, `ai_busy` ...). The server never uses a file that was already used in the run, whatever the recruiter dropped. |

Status object: `{"id", "state": "idle" | "queued" | "running" | "done" | "failed", "stage", "result", "error",
"meta": {"model", "fallback_used", "models_used", "fallbacks"}}` (plus `cached` and `cached_from` for a saved set).
Stages, in order: "Choosing code", "Reading files", "Writing questions", "Checking questions". `meta.model` is the model
that wrote the MOST questions (the earlier entry of the chain on a tie), `meta.fallback_used` is true when that is not the
first entry, `meta.models_used` is `{model: questions written}` and `meta.fallbacks` counts the handovers from one model
to the next (see "Model chain and saved results").

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
  Replacement questions (see `.../questions/replace`) are saved beside the run, so the result returned by
  `GET .../questions` counts them: `counts.questions`, `status` and `message` (same sentence, the right N) follow the
  run's questions plus its replacements, e.g. a `too_few` run becomes `ok` when the count reaches 3. The saved
  original is not rewritten, and a restart gives the same corrected result.
- `why.top_ranked` is true only for the rank-1 file of the skill (false for reserve picks and replacements; the
  `reason` sentence says "highest-ranked" only when it is true). For a SQL question `language` is always `sql`.
- A `fix_code` question also has `failing_example` `{"input", "expected", "actual"}` (short plain strings, at most 120
  characters) and its `question` text ends with the sentence `Example: {input} should give {expected}, but the starter
  code gives {actual}.` appended by code. `write_code` questions have neither (additive, `schema_version` stays 1).
- `dropped` (additive, `schema_version` stays 1): one entry per question that failed its checks twice, with
  `attempts` (1 or 2) and `reasons`, a list of codes from the closed set `bad_json`, `bad_length`, `wrong_file`,
  `unknown_file_reference`, `unknown_symbol`, `unknown_call`, `url_or_email`, `secret_like`, `duplicate`,
  `fix_without_starter`, `not_sql_starter`, `unknown_sql_name`, `no_real_sql_in_file`, `fix_without_example`,
  `fix_example_vacuous`, `fix_not_modified`, `multiple_tasks`, `bug_hint_in_starter` (final attempt first). Each entry also has `causes` (additive): for a
  `bad_json` drop, the codes `empty_response`, `cut_off`, `not_json`, `wrong_shape` that say why the answer was
  unusable (final attempt first, no duplicates; an empty list otherwise), and `shape_fields` (additive): for a
  `wrong_shape` answer, the NAMES of the fields that were wrong (`kind`, `question`, `starter_code`, `file`,
  `symbols`, `reference_notes`; empty otherwise). Metadata and codes only. `counts.hints_stripped` (additive, always present) is how many comments the model added to the starter code of fix
  questions that were removed by code (a fix starter must not point at its own bug).
  `counts.starter_built` and `counts.starter_empty` (additive) count the write questions whose starter code was cut from
  the real file by code (a short excerpt of the file around the symbol the question names, with the body of an
  existing target replaced by a placeholder line) and those left without one (unsupported language or no safe cut).
  `counts.normalised` (additive) is how many
  harmless answer variants (a differently spelled `kind`, a `null` starter code, ...) were fixed before the checks. `counts.dropped` equals its length. `skipped_skills` lists only skills with zero questions.
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

**Gemini: handover first, quiet retries as the last resort, then the error.** While another reading model is ready, a
temporary failure or a rate limit rests that model and the same call goes to the next one (see "Reading chain"). The
quiet retries below apply to the LAST model that is ready. The provider already retries HTTP 500/502/503/504 twice (2 s, 6 s). On top
of that the API retries temporary failures (HTTP 500/502/503/504 or a network problem) after 5, 10 and 20 seconds
(`GEMINI_OUTER_RETRY_DELAYS` in `api/settings.py`). While it retries the job stays `running` with the normal stage, so
the user sees nothing. If every retry fails the error is `ai_busy`. HTTP 429 is not retried in place (`ai_rate_limited` only when no model is left to take the call).

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

## Model chain and saved results

### What was found (Step 0, by reading the code)

- **How the questions chain picks and rests models before this change.** `ModelChain.pick()` returned the first entry that
  was neither cooling down nor unavailable. A limit rested an entry for 120 s (`RATE_LIMIT_COOLDOWN_SECONDS`), or 1800 s
  when its previous outcome was also a limit (`REPEATED_LIMIT_COOLDOWN_SECONDS`); a model Gemini did not know rested for
  3600 s (`MODEL_UNAVAILABLE_SECONDS`). The Groq provider fell through to the next entry **inside** a run (429, a refused
  key, a failed call, a too-large request). **A rate limit ended a whole run in exactly one place:** a Gemini entry that
  answered HTTP 429 (or a temporary failure) raised out of `_RunProvider.generate_json`, `QuestionManager._run` turned it
  into the job error `ai_rate_limited` / `ai_busy`, and the questions already written were thrown away. That is how
  "Groq limited, then Gemini 3.6 limited" ended a run while Gemini 3.5 and flash-lite were ready: only the *next* run
  would have used them. A Gemini 404 on its first call was the only in-run switch for Gemini.
- **The scoring chain (job description and CV reading) does not hand over mid-run.** `pipeline._start_ai` only moves on to
  the next entry when the FIRST job-description call says the model is unknown (404). A 429 anywhere in the analysis ends
  that analysis with `ai_rate_limited` and the next analysis uses the next model. This is unchanged: the analysis chain is
  Phase 1 reading code and is out of scope here.
- **Where results lived.** Finished analyses were only in `JobManager._jobs` (an in-memory dict: at most 20 jobs, dropped
  after one hour) and finished question runs only in `QuestionManager._jobs`, so every restart lost them. GitHub file
  contents are fetched in `skillverify_questions.service.prepare` through `GitHubFiles.file_text`, one call per file, and
  were kept only inside that run's `Prepared` object (for a retry or a replacement of the same run): a new run fetched
  them all again.

### Every model hands over to the next one

In one questions run, any entry of the chain (Groq or Gemini) that answers a **rate limit** or a **temporary failure**
(HTTP 5xx, a network problem) is rested (the usual 120 s / 1800 s; a provider's own retry-after replaces the default
only when it is *shorter*) and the SAME write is made again on the next entry that is not resting. The run is never ended
while at least one entry is ready. The write in progress does not count as a failed attempt and does not use up the one
repair attempt (the writer never sees the handover). The other rules are unchanged: a refused Groq key sets Groq aside for
the run, a request too large for Groq goes to the next entry for that one request, a Gemini model that returns 404 on
its first call is set aside for 3600 s, and an unusable answer is the writer's `bad_json` + one repair.

When **every** entry is resting: if the shortest remaining cooldown is at most `QUESTIONS_MAX_WAIT_SECONDS` and the run
still has time, the run waits that long and continues. Otherwise it ends with the usual `ai_rate_limited` (or
`ai_all_limited` when it never got started) and **keeps the questions already written**: they are saved as a `partial`
run and a retry continues from them (the files already used stay used; nothing is written twice).

Each handover writes one log line with model names and a code only, for example
`handover from=groq:openai/gpt-oss-120b to=gemini-3.6-flash reason=rate_limited` (`reason` is `rate_limited`,
`temporary`, `not_found`, `key_rejected` or `too_large`). A wait writes `waiting seconds=5 for a model to be ready`.

### Pacing

At least `QUESTIONS_MIN_GAP_SECONDS` pass between two calls to the **same** model (the free per-minute limits belong to
the key, so the pause is shared by every run). A handover to another model does not wait. The pause is skipped when
waiting would pass the questions time limit, and the normal limit logic applies.

### Saved results (SQLite)

Saved results use one SQLite file, `skillverify.sqlite3`, through the standard `sqlite3` module (no new dependency),
written under a lock and in transactions. The folder is `SKILLVERIFY_DATA_DIR`, default `api/_data/` (created when
needed, and **never committed**: the owner adds `api/_data/` to `.gitignore`). The file is opened the first time it is
needed, not when the app is imported. If it cannot be opened or is corrupt, ONE plain log line is written, storage
switches itself off, the app keeps working in memory only, and no request fails because of it
(`GET /api/health` shows `"storage": {"enabled": false}`).

| What | Saved when | Used for |
|---|---|---|
| Finished analyses: id, `result`, `meta`, created time, and a small public repo list for the question writer (repo names, languages, last commit dates, default branches, which repo shows which skill) | the analysis is `done` with result status `ok` (running and failed analyses are not saved) | a restart no longer wipes them; they are loaded when first asked for |
| Finished question runs: the `result` and `meta` the API returns, keyed by the analysis id, with the question-set fingerprint | the run is `done` (`ok`, `too_few` or `none`) | a restart restores them |
| A `partial` run: the questions written so far, the error, the fingerprint | a run ended early by limits and wrote at least one question | a retry continues from it, also after a restart |
| Replacements written by the replace route (with the `after` id) | each replacement | files already used stay used after a restart |
| GitHub file texts, redacted: key = repo full name + path + the repo's last push date | a file is fetched from GitHub | a later run reads them from the cache: no GitHub call |
| Parsed model answers for the CV and the job description (model name + the JSON answer), keyed by a SHA-256 of the redacted text sent + a prompt version | a document is read by a model | a repeat of the same document needs no model call (see "Reading chain") |

Nothing else is stored: no API key, token or header, no CV or job description text (the saved readings hold only the parsed answer under a hash), no prompt. The
only data beyond what the API returns is the **file texts of the candidate's public repositories** (after the secret
redaction the question writer already applies) and the small public repo list above. Delete the folder to remove all of it.

**Question-set cache.** A new questions run for ANOTHER analysis is served at once, without GitHub or model calls, when a
saved set has the same fingerprint: the GitHub username (lower case), the score's `repo_fingerprint`, the list of
(skill, tier) pairs asked about, and `WRITER_SETTINGS_VERSION` (a constant in `skillverify_questions/config.py`, bumped
when the prompt or the checks change). The status shows `meta.cached: true` and `meta.cached_from` (the analysis id the
set was written for); `schema_version` stays 1. A changed fingerprint or writer version is a miss.

**Regenerate.** `POST /api/analyses/{id}/questions` with `{"regenerate": true}` skips the question-set cache for that run
(the file cache is still used) and overwrites the saved run when it finishes; a failed regeneration never wipes a good
saved run. Without it, a run that already exists (also one loaded from disk) is returned by GET, and a second POST is
`409 questions_already_started`. A run that is queued or running cannot be regenerated. The replace route keeps working
and records the replacement in the saved run; the saved `result` itself is not rewritten (the recruiter's screen keeps its
own copy of replacements, see the web step).

**File cache.** Keyed by repo full name, path and the repo's last push date (the validator's `last_commit_date`): a changed
date is a miss. Files over the existing size limit (`MAX_FILE_BYTES`) are not cached; at most 2000 files are kept and the
oldest are dropped first. Hits and misses are logged as counts only (`file cache hits=12 misses=0`).

**Retention.** When the server starts, saved analyses and question runs older than `SAVED_RESULTS_DAYS` days are deleted.

### New settings

| Setting (environment or `.env`) | Default | Allowed | What it does |
|---|---|---|---|
| `QUESTIONS_MIN_GAP_SECONDS` | 3 | 0 to 30 | minimum time between two calls to the same model |
| `QUESTIONS_MAX_WAIT_SECONDS` | 25 | 0 to 120 | the longest wait when every model is resting (0 switches waiting off) |
| `QUESTIONS_MAX_OUTPUT_TOKENS` | empty (`0`: no cap sent) | 500 to 8000 | the most tokens Groq may answer a question with (`max_completion_tokens`); empty sends nothing, as before. Shown as `questions_max_output_tokens` under `limits` in health |
| `SKILLVERIFY_DATA_DIR` | `api/_data/` | a folder | where the saved results live |
| `SAVED_RESULTS_DAYS` | 14 | 1 to 90 | retention of saved analyses and question runs |

The three numbers are read like the other limits (lazily; a missing, non-numeric or out-of-range value uses the default
with one log line that names the setting and never shows the value). `GET /api/health` shows `questions_min_gap_seconds`
and `questions_max_wait_seconds` under `limits`.

## Groq first for questions

The **question writer** (the questions run and the replacement route) has its own model chain that can mix two providers.
Groq is the primary; the Gemini chain is the fallback behind it. Job description and CV reading stay on Gemini only (see
`docs/QUESTIONS.md`, "Which calls use which provider").

**Configuration** (environment or `.env`, read once, lazily):

- `GROQ_API_KEY`: when empty or missing, every Groq entry is skipped silently and the app behaves exactly as with Gemini
  only.
- `QUESTIONS_MODEL_CHAIN`: comma-separated entries, same parsing rules as `GEMINI_MODEL_CHAIN`. A plain name is a Gemini
  model; a `groq:` prefix is a Groq model, for example `groq:openai/gpt-oss-120b,gemini-3.6-flash`. Invalid names are
  dropped with one log line that counts them (the text is not logged); duplicates are removed; at most 8 entries. A
  custom chain that ends up empty (for example only `groq:` entries and no key) falls back to the Gemini chain.
- Unset: `groq:openai/gpt-oss-120b` first, then the Gemini chain exactly as it is for the analysis (so
  `GEMINI_MODEL_CHAIN` still decides the Gemini entries).

**Rules** (in memory; one state per entry, `ready` or `cooling_down` with seconds left, shown by `GET /api/health`):

| Groq answers | What happens |
|---|---|
| HTTP 429 | That entry rests for the `Retry-After` seconds Groq sent when that is **shorter** than the usual cooldown (at least 1 s); otherwise the usual 120 s (1800 s when repeated). The run continues **in the same run** with the next entry. |
| 5xx, timeout, network problem | One quiet retry after 2 s. If it fails again, that entry is rested (120 s) and the next entry answers. |
| HTTP 401 or 403 | Groq is unusable: all Groq entries are skipped for the rest of the run and the next entry answers. The run never fails because of the Groq key. With no other entry the error is `ai_not_configured`. Groq is not rested, so a fixed key works on the next run. |
| HTTP 413, or a "request too large" reply | This one request is too big for Groq's per-minute token limit. The next entry answers **this request only**; Groq is not rested and is tried first again for the next request. |
| Empty reply, or a reply that is not a JSON object | The reply is read tolerantly first (code fences and text around the object are removed). If it is still unusable it counts as `bad_json` like Gemini replies, with a cause (`empty_response`, `cut_off`, `not_json`, `wrong_shape`) in `dropped[].causes`. The writer's one repair attempt goes to a **different ready entry** (the same one only when no other is ready), then a drop with reason `bad_json`. No provider change is counted. |

Gemini entries hand over the same way (see "Model chain and saved results"): a Gemini 429 or temporary failure rests that
entry and the next one answers, and a model Gemini does not know (404 on its first call) is set aside. When no entry is
left and the shortest cooldown is above `QUESTIONS_MAX_WAIT_SECONDS`, the run fails with `ai_rate_limited`
(`fallback_available: false`) and keeps what it wrote; when every entry is resting at the start of a run, it fails with
`ai_all_limited`, as before.

`meta.model` of a questions run is the provider-qualified entry that produced the result (`groq:openai/gpt-oss-120b`, or
a plain Gemini name), and `meta.fallback_used` is `true` when that is not the first entry of the chain. The key is never
returned, logged or put into an error.

## Reading chain

Reading the job description and the CV now has the same protection as the question writer: model handover, pacing, saved
results and a privacy guard. Nothing in `skillverify/*` changed; the parsers take any object with `generate_json`, so
`api/reading.py` wraps the models and is handed to them as their provider.

### What was found (Step 0, by reading the code; no real call was made)

- `api/pipeline.py` built ONE Gemini provider per run (`RetryingProvider(GeminiProvider(model))`) and passed the same
  object to `JDParser` and then to `CVParser`. Each parser makes ONE model call per document (`auto_add=False`, so no
  proposal call).
- The scoring chain moved on only when the FIRST job description call said the model is unknown (HTTP 404). A 429 ended
  the analysis with `ai_rate_limited` and rested the model for the NEXT run; a 503 was retried quietly 3 times (5, 10,
  20 s) on the same model and then ended as `ai_busy`. So in a live run a slow first attempt, then two limits, then a
  third attempt on the smallest model needed three user presses, and a different model could read each attempt.
- The question writer already had everything needed: `ModelChain` (per-entry cooldowns 120 s / 1800 s repeated / 3600 s
  unknown, a provider's Retry-After used only when shorter), `GroqProvider`, `RetryingProvider`, and the process-wide
  `Pacer` (a minimum gap per model name). The reading step reuses `ModelChain`, `RetryingProvider`, the Groq classes and
  the very same `Pacer` instance (so reading and question calls to one model queue behind each other).
- Nothing needed frozen code. One thing is deliberately NOT done: `ModelChain` instances are separate for reading
  (`deps.chain`) and for questions (`deps.questions_chain`), so a Gemini model rested by the question writer is not
  known to the reading chain and the other way round. Sharing them would change the question writer's order and cooldown
  behaviour, which this step must not do. The pacing gap IS shared.

### Handover

`ReadingProvider` (one per document) makes each call like this:

1. **Saved reading?** The exact text is looked up (see "Same input, same reading"). A hit answers with no model call
   and no pick, so even a server with every model resting can finish a repeated analysis.
2. Otherwise it takes the first reading-chain entry that is ready, spaced from the previous call to that model by
   `READING_MIN_GAP_SECONDS`, and calls it.
3. A **rate limit (HTTP 429)** or a **temporary failure (HTTP 500/502/503/504 or a network problem)** rests that entry
   (120 s, or 1800 s if its previous outcome was also a limit; a Retry-After is used only when SHORTER than that) and
   the SAME call, with the same text, goes to the next ready entry. A handover logs one line with codes and model names
   only: `reading handover doc=cv from=gemini-3.6-flash to=gemini-3.5-flash-lite reason=rate_limited`. The reasons are
   `rate_limited`, `temporary`, `not_found`, `key_rejected` and `too_large` (the last two only for a `groq:` entry,
   except `not_found`, which is for Gemini).
4. A model Gemini does not know (HTTP 404 on its first answer of the analysis) is set aside for an hour and the call
   goes to the next entry; a 404 after that model already answered stays `ai_not_configured`. A rejected Groq key sets
   Groq aside for that document; a request too large for Groq goes to the next entry without resting Groq.
5. **Every entry resting:** if the shortest rest is at most `READING_MAX_WAIT_SECONDS` AND the wait ends before the
   analysis limit, the reading waits (`reading waiting doc=jd seconds=5 for a model to be ready`) and continues, at
   most 3 times in a row. Otherwise the call ends with the usual code: `ai_rate_limited` (with `fallback_available`
   false, since nothing is ready), `ai_busy`, or `ai_all_limited` when nothing was even tried because every entry was
   already resting when the analysis started.
6. **Unusable output is not a handover.** A model that answers with JSON the parser cannot use (no `skills` list) goes to
   the parser, whose own rules apply (`jd_unreadable` / `cv_unreadable`); nothing is rested and nothing is saved, so a
   try again reads the document again.
7. **Quiet retries** of a temporary failure (5, 10, 20 s for Gemini; one 2 s retry for Groq) are kept only when no other
   entry is ready: while a neighbour is ready the failure hands over instead of waiting.

Each document is read by the first ready entry, so a handover on the job description usually carries the CV with it, but
the two documents can be read by different models. `meta.model` is the model that read the CV and `meta.fallback_used`
is true when either document was read by a model other than the first entry; the Score screen's note about a backup
model keeps using those two fields. `meta.readers` (see the route above) says who read what.

### Pacing and the time limit

`READING_MIN_GAP_SECONDS` (default 2) is the least time between two calls to the same model name; calls to different
models never wait for each other. A pause that would pass the analysis limit (`ANALYSIS_TIMEOUT_SECONDS`, 360 s unless
you changed it) is skipped, and so is a handover wait (point 5). The clock and the sleep are the injectable ones of
`api/pacing.py`, so tests run on a fake clock. `timings` still records every stage, a failed stage included, and the
job still ends with one of the existing error codes. (A single model call can take as long as its own request timeout;
the job-level limit that already existed still ends the analysis with `timeout` in that case.)

### Which models read: `READING_MODEL_CHAIN`

Same format as `QUESTIONS_MODEL_CHAIN`: comma-separated, a plain name is a Gemini model, a `groq:` prefix is a Groq model
(dropped without a `GROQ_API_KEY`); invalid names are dropped with one log line that counts them. **Unset, the reading
chain is the current Gemini chain (`GEMINI_MODEL_CHAIN`, else `GEMINI_MODEL` + the default) and Groq is NOT in it.** Groq
reads only if the owner adds a `groq:` entry. This step makes NO claim that Groq reads a CV or a job description as well
as Gemini: the owner must first compare the two live, on real documents (for example by running the same CV and job
description once with each and comparing the skills found and the score), before putting Groq in this setting.

### Same input, same reading

Each model answer that the parsers can use (a JSON object with a `skills` list) is saved in the existing SQLite store
(table `reading_cache`) under a key: the SHA-256 of the system prompt, the exact text that was sent (after the removal
below) and `READING_PROMPT_VERSION` (`api/reading.py`; bump it when reading changes in a way the prompts do not show).
A repeat of the same document, even from another analysis or after a restart, is answered from the store with no model
call. The saved value is the model name and the model's JSON answer, never the text that was sent. The parsers' own
deterministic steps (finding the contacts and the GitHub username, checking every quote against the text, mapping to the
taxonomy, tiering) run again every time on the real text, so the same input always gives the same parsed result and the
same score. A changed text, a changed prompt or a new `READING_PROMPT_VERSION` is a miss. At most 500 readings are kept
(oldest dropped first) and they are deleted with the other saved results after `SAVED_RESULTS_DAYS`. With saved results
off (`storage.enabled` false) every run reads again, as before.

### Privacy: e-mail addresses and phone numbers

Before any CV text goes to any model, `api/reading.py` removes every e-mail address and phone number from it (the CV
parser's own detectors; fixed placeholders `[EMAIL]` and `[PHONE]`). The name, the links and the skills stay. The key of
a saved reading is computed AFTER this, so the cache never sees the raw text and two CVs that differ only in their
contact lines are the same reading. Job descriptions are not changed.

**What the frozen parser still does, and why it is kept.** The CV parser finds the e-mail addresses, phone numbers and
the GitHub username itself, in memory, from the real text (`candidate.emails`, `candidate.phones`, the
`no_email_found` / `no_phone_found` warnings that reach the score as `cv_warnings`, and `github.username`). Removing the
contacts before the parser would change those fields and so the score's flags, which this step must not do, and the
GitHub username may be found next to the contact lines. So the real text stays inside the process (as it did), the
parser's own redaction already replaced the contacts in the prompt (as it did), and the API layer now guarantees it a
second time at the point where text leaves, and in the cache key. The parsed contacts live only in the kept
intermediates (memory); they are not returned, not logged and not saved (a saved analysis holds the result, the meta
and a small public repo list, as before).

**For the Upload screen (a website change for later, not done here):** "Your CV is sent to an AI service to read your
skills, but your phone number and e-mail address are removed first; the readings are kept on this server for up to 14
days so the same CV gives the same result." (If Groq is added to the reading chain, name Groq there as well.)

### New settings

| Setting (environment or `.env`) | Default | Allowed | What it does |
|---|---|---|---|
| `READING_MODEL_CHAIN` | the Gemini chain | model names, `groq:` prefix allowed | models that read the CV and the job description |
| `READING_MIN_GAP_SECONDS` | 2 | 0 to 30 | minimum time between two calls to the same model |
| `READING_MAX_WAIT_SECONDS` | 20 | 0 to 60 | the longest wait when every reading model is resting (0 switches waiting off) |

The two numbers are read like the other limits (lazily; a missing, non-numeric or out-of-range value uses the default
with one log line that names the setting and never shows the value) and are shown under `limits` in the health route.

## Gemini model chain

Different Gemini models have separate quotas, so the API keeps an ordered **chain** of models. The first is the main
model. *(Superseded for reading: since the "Reading chain" step each document is read by the first ready model and a limit
hands the same call over inside the run. The rules below describe the cooldown bookkeeping, which is unchanged.)*

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
