// Fixtures for the Question review screen, in the shape of docs/API.md and docs/QUESTIONS.md. Fictional data only.
import type { ApiError, JobStatus, Question, QuestionsResult, QuestionsStatus, ReplaceResponse, SkippedSkill } from "../api/types";
import { fixtureA, metaA } from "../score/fixtures";

const DIM = `_DIM = (0, 31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
def _leap(y):
    return y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)

def _dim(y, m):
    return _DIM[m]`;

const QUERY_PARAMS = `class QueryParams:
    def __init__(self, pairs=None):
        self._data = {}
        for key, value in (pairs or []):
            self._data.setdefault(key, []).append(value)

    def get(self, key, default=None):
        values = self._data.get(key)
        return values[-1] if values else default

    def getlist(self, key, type=None):
        """Return every value stored for key."""
        # TODO
        pass`;

/** A fix_code question with a failing-example sentence (appended by the server) and starter code. */
export const qFix: Question = {
  id: "q_aaaaaaa1",
  kind: "fix_code",
  question:
    "In `libs/dates.py`, the `_dim` helper returns the number of days in a month. The starter code below contains a bug. " +
    "Fix `_dim` using `_leap(y)` so that February has 29 days in a leap year. Example: `_dim(2024, 2)` should give 29, but the starter code gives 28.",
  language: "python",
  starter_code: DIM,
  reference_notes: "SECRET-NOTES-FIX: add one day to February when _leap(y).",
  failing_example: { input: "_dim(2024, 2)", expected: "29", actual: "28" },
  why: {
    repo: "calendar-utils", path: "libs/dates.py", skill: "Python", tier: "must_have", claimed: true, top_ranked: true,
    reason: "Asked because the CV claims Python and this is the highest-ranked file in the repos that uses it.",
    symbols: ["_dim"],
  },
};

/** A write_code question with a numbered list and a 14-line starter. */
export const qWrite: Question = {
  id: "q_aaaaaaa2",
  kind: "write_code",
  question:
    "Implement `getlist(self, key, type=None)` on the `QueryParams` class:\n" +
    "1. If `key` is missing, return `[]`.\n2. Return every value stored for `key`, in the order it was added.\n" +
    "3. If `type` is given, call it on each value before returning it.\n4. Never change the stored values.",
  language: "python",
  starter_code: QUERY_PARAMS,
  reference_notes: "SECRET-NOTES-WRITE: copy the list before converting.",
  why: {
    repo: "web-toolkit", path: "toolkit/params.py", skill: "Python", tier: "must_have", claimed: true, top_ranked: true,
    reason: "Asked because the CV claims Python and this is the highest-ranked file in the repos that uses it.",
    symbols: ["QueryParams.getlist"],
  },
};

/** A SQL question with a one-line comment starter. */
export const qSql: Question = {
  id: "q_aaaaaaa3",
  kind: "write_code",
  question:
    "Write a query that returns the total revenue for each month of 2024, including months with no orders. " +
    "Use the `orders` and `calendar` tables and return the columns `month` and `revenue`, sorted by month.",
  language: "sql",
  starter_code: "-- Write your SQL query here",
  reference_notes: "SECRET-NOTES-SQL: LEFT JOIN from the calendar.",
  why: {
    repo: "sales-analytics", path: "sql/monthly_revenue.sql", skill: "SQL", tier: "must_have", claimed: true, top_ranked: true,
    reason: "Asked because the CV claims SQL and this is the highest-ranked file in the repos that uses it.",
    symbols: [],
  },
};

/** A nice-to-have question, used as the fourth card and as replacement material. */
export const qRedis: Question = {
  id: "q_aaaaaaa4",
  kind: "write_code",
  question:
    "In `cache/session_store.py`, write `touch(client, session_id, ttl)` so that an existing session key stays alive for `ttl` more seconds. " +
    "A missing key must be left alone. Return `True` if the key was extended and `False` otherwise.",
  language: "python",
  starter_code: "def touch(client, session_id, ttl):\n    # TODO\n    pass",
  reference_notes: "SECRET-NOTES-REDIS: EXPIRE only when EXISTS.",
  why: {
    repo: "session-service", path: "cache/session_store.py", skill: "Redis", tier: "nice_to_have", claimed: false, top_ranked: false,
    reason: "Asked because the job lists Redis as a nice-to-have and this file shows it in the candidate's repos.",
    symbols: ["touch"],
  },
};

export const qReplacement: Question = {
  ...qRedis,
  id: "q_bbbbbbb1",
  question: "In `cache/session_store.py`, write `expire_all(client, prefix)` that removes every key that starts with `prefix`. Return how many keys were removed.",
  starter_code: "def expire_all(client, prefix):\n    pass",
};

export const SKIPPED: SkippedSkill[] = [
  { skill: "Docker", tier: "nice_to_have", reason: "not_found_in_code" },
  { skill: "Pytest", tier: "nice_to_have", reason: "not_found_in_code" },
  { skill: "Airflow", tier: "nice_to_have", reason: "not_found_in_code" },
  { skill: "Git", tier: "nice_to_have", reason: "not_checkable_from_code" },
  { skill: "REST APIs", tier: "nice_to_have", reason: "not_checkable_from_code" },
  { skill: "Snowflake", tier: "must_have", reason: "no_usable_file" },
];

function run(status: QuestionsResult["status"], questions: Question[], message: string | null, skipped = SKIPPED): QuestionsResult {
  return {
    schema_version: 1, status, message, generated_at: "2026-10-29T10:00:00+00:00", limits: { min: 3, max: 5 },
    questions, skipped_skills: skipped, dropped: [],
    counts: { candidates_skills: 5, repos_considered: 3, files_fetched: 6, files_ranked: 5, questions: questions.length, dropped: 0 },
  };
}

/** A finished run with 3 questions (the usual case). */
export const RUN_OK = run("ok", [qFix, qWrite, qSql], null);
/** A finished run with 4 questions (one more than the minimum). */
export const RUN_FOUR = run("ok", [qFix, qWrite, qSql, qRedis], null);
/** Two questions only. */
export const RUN_TOO_FEW = run(
  "too_few", [qFix, qWrite], "Only 2 question(s) could be written from the candidate's code. A session needs at least 3.");
/** No question at all. */
export const RUN_NONE = run("none", [], "No code was found that shows the job's skills, so no questions could be written.");

export const statusDone = (result: QuestionsResult): QuestionsStatus =>
  ({ id: "j1", state: "done", stage: "Checking questions", result, error: null, meta: { model: "model-a", fallback_used: false } });
export const statusIdle: QuestionsStatus = { id: "j1", state: "idle", stage: null, result: null, error: null };
export const statusQueued: QuestionsStatus = { id: "j1", state: "queued", stage: "Choosing code", result: null, error: null };
export const statusRunning = (stage: string): QuestionsStatus => ({ id: "j1", state: "running", stage, result: null, error: null });
export const statusFailed = (error: ApiError): QuestionsStatus => ({ id: "j1", state: "failed", stage: "Writing questions", result: null, error });

export const E_RATE_FALLBACK: ApiError = { code: "ai_rate_limited", message: "x", retryable: true, fallback_available: true };
export const E_RATE_NONE: ApiError = { code: "ai_rate_limited", message: "x", retryable: true, fallback_available: false };
export const E_ALL_LIMITED: ApiError = { code: "ai_all_limited", message: "x", retryable: true };
export const E_TIMEOUT: ApiError = { code: "timeout", message: "x", retryable: true, stage: "Writing questions" };
export const E_GITHUB: ApiError = { code: "github_unavailable", message: "x", retryable: true };
export const E_INTERNAL: ApiError = { code: "internal_error", message: "x", retryable: true };
export const E_NOT_READY: ApiError = { code: "analysis_not_ready", message: "x", retryable: false };

/** The analysis the header reads (name, job title and score come from here). */
export const ANALYSIS: JobStatus = { id: "j1", state: "done", stage: "Done", result: fixtureA, error: null, meta: metaA };

export const replaceOk = (question: Question = qReplacement, skipped: SkippedSkill[] = SKIPPED.slice(0, 2)): { status: number; body: ReplaceResponse } =>
  ({ status: 200, body: { question, skipped_skills: skipped, dropped: [] } });
export const replaceNone = {
  status: 409,
  body: { error: { code: "no_replacement_available", message: "There is no other code in the candidate's repos to ask about.", retryable: false } },
};
export const replaceFails = { status: 503, body: { error: { code: "ai_busy", message: "x", retryable: true } } };
