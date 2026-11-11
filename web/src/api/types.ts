// Types for the SkillVerify API (api/) and the scoring output of skillverify/scoring/score.py.

/** `network` and `unknown` are made by the web client; every other code comes from the API. */
export type ApiErrorCode =
  | "invalid_input" | "jd_unreadable" | "cv_unreadable" | "github_user_not_found" | "github_unavailable"
  | "ai_busy" | "ai_rate_limited" | "ai_all_limited" | "ai_not_configured" | "timeout" | "internal_error"
  | "analysis_not_ready" | "questions_already_started" | "no_replacement_available"
  | "network" | "unknown";

export type ApiError = {
  code: ApiErrorCode | (string & {});
  message: string;
  retryable: boolean;
  /** Only on `ai_rate_limited`: true when the next run would use a backup model. */
  fallback_available?: boolean;
  /** Only on `timeout`: the fixed stage name that ran out of time. */
  stage?: string;
};

export type JobState = "queued" | "running" | "done" | "failed";

/** Display values for the Score header. Plain strings or null; older jobs may not have the field at all. */
export type JobMeta = {
  candidate_name: string | null;
  job_title: string | null;
  /** The Gemini model that read the documents, and whether it was a backup (not the first of the chain). */
  model?: string | null;
  fallback_used?: boolean;
  /** How many repositories the scan looked at. `repos_total` and `repos_used` can be null. */
  scan?: { repo_cap: number; repos_total: number | null; repos_used: number | null; capped: boolean } | null;
  /** Seconds per finished stage, by stage name. */
  timings?: Record<string, number>;
};

export type JobStatus = {
  id: string;
  state: JobState;
  stage: string;
  result: ScoreResult | null;
  error: ApiError | null;
  meta?: JobMeta;
};

export type Tier = "must_have" | "nice_to_have";

export type SkillRow = {
  skill: string;
  category: string;
  tier: Tier;
  tier_basis: string;
  weight: number;
  claimed: boolean;
  /** found | not_found | not_found_scan_incomplete | not_detectable_from_github, or null without GitHub evidence */
  github_status: string | null;
  confidence: string | null;
  basis: string | null;
  credit: number | null;
  points: number | null;
  /** confirmed | weak_evidence | unconfirmed | not_found_on_github | scan_incomplete | not_detectable | not_claimed | no_github_evidence */
  reason: string;
};

export type GithubBlock = {
  username: string | null;
  cv_username: string | null;
  username_matches_cv: boolean | null;
  validator_status: string | null;
  tech_scan_status: string | null;
  scan_complete: boolean | null;
  counted_repos: number | null;
  repos_scanned: number | null;
  total_commits_in_window: number | null;
  active_repos_in_window: number | null;
  languages_top: string[];
};

export type TierTotals = { skills: number; weight: number; points: number };

export type ScoreResult = {
  schema_version: number;
  /** ok, or e.g. no_github_evidence, github_mismatch, github_user_not_found, github_unavailable, no_jd_skills, invalid_input */
  status: string;
  message: string;
  generated_at: string;
  job_name: string;
  cv_label: string;
  weights: {
    must_have: number;
    nice_to_have: number;
    credit_confirmed: number;
    credit_claimed_unconfirmed: number;
    credit_not_claimed: number;
    weak_evidence_credit: number;
    not_detectable_credit: number;
  };
  github: GithubBlock;
  score: {
    repo_fingerprint: number | null;
    claim_coverage: number | null;
    checkable_only: number | null;
    verifiable_share: number | null;
    points_earned: number;
    points_possible: number;
    by_tier: Record<Tier, TierTotals>;
  };
  skills: SkillRow[];
  hidden_strengths: { skill: string; confidence: string }[];
  cv_extra_skills: { count: number; names: string[] };
  jd_unmapped_terms: number;
  cv_warnings: string[];
  flags: string[];
  sensitivity: Record<string, number | null> | null;
  counts: {
    jd_skills: number;
    must_have: number;
    nice_to_have: number;
    claimed: number;
    confirmed: number;
    unconfirmed: number;
    not_claimed: number;
    not_detectable: number;
    hidden_strengths: number;
  };
};

// ---- the question generator (docs/API.md, docs/QUESTIONS.md) ------------------------------------------------------

export type QuestionKind = "write_code" | "fix_code";

export type Question = {
  id: string;
  kind: QuestionKind;
  question: string;
  language: string;
  starter_code: string;
  /** Recruiter-only. The review screen never shows it. */
  reference_notes: string;
  failing_example?: { input: string; expected: string; actual: string };
  /** Recruiter-only. */
  why: {
    repo: string;
    path: string;
    skill: string;
    tier: Tier;
    claimed: boolean;
    top_ranked: boolean;
    reason: string;
    symbols: string[];
  };
};

export type SkippedSkill = { skill: string; tier: Tier | string; reason: string };

export type QuestionsResult = {
  schema_version: number;
  status: "ok" | "too_few" | "none";
  message: string | null;
  generated_at: string;
  limits: { min: number; max: number };
  questions: Question[];
  skipped_skills: SkippedSkill[];
  dropped: { skill: string; tier: string; repo: string; path: string; attempts: number; reasons: string[] }[];
  counts: Record<string, number>;
};

export type QuestionsState = "idle" | "queued" | "running" | "done" | "failed";

export type QuestionsStatus = {
  id: string;
  state: QuestionsState;
  stage: string | null;
  result: QuestionsResult | null;
  error: ApiError | null;
  meta?: { model: string | null; fallback_used: boolean };
};

export type ReplaceResponse = { question: Question; skipped_skills: SkippedSkill[]; dropped: QuestionsResult["dropped"] };
