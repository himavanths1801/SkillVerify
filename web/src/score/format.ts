import type { JobMeta, SkillRow, ScoreResult } from "../api/types";

/**
 * The suggested cut-off shown on the score bar while "Auto threshold" is on.
 * A placeholder until several candidates exist to take a spread from; it is not derived from any data yet.
 */
export const DEFAULT_SUGGESTED_CUTOFF = 35;

/** Statuses of `score_candidate` for which the figures may be missing. */
export const NON_OK_STATUSES = ["invalid_input", "no_jd_skills", "no_github_evidence", "github_user_not_found", "github_unavailable", "github_mismatch"];

/** "Data_Analyst" -> "Data Analyst", "priya-sample" -> "Priya sample". */
export function humanize(value: string): string {
  const text = value.replace(/[_-]+/g, " ").trim();
  return text === "" ? "" : text.charAt(0).toUpperCase() + text.slice(1);
}

/** 44.4 -> "44.4", 70 -> "70", null -> "n/a". */
export function formatScore(value: number | null | undefined): string {
  return value === null || value === undefined ? "n/a" : String(value);
}

/** 3 -> "3", 1.5 -> "1.5", 3.0 -> "3": no trailing ".0". */
export function formatPoints(value: number | null | undefined): string {
  return value === null || value === undefined ? "n/a" : String(Number(value.toFixed(2)));
}

export const tierLabel = (tier: SkillRow["tier"]): string => (tier === "must_have" ? "Must-have" : "Nice-to-have");

export type Evidence = "found" | "look" | "unchecked" | "unclaimed";

/** The evidence badge of a row, from the real `reason` / `github_status` values of score.py. */
export function evidenceOf(row: SkillRow): Evidence {
  if (row.reason === "not_detectable" || row.github_status === "not_detectable_from_github") return "unchecked";
  if (row.reason === "not_claimed") return "unclaimed";
  if (row.claimed && row.reason === "confirmed") return "found";
  if (row.claimed && ["weak_evidence", "not_found_on_github", "scan_incomplete", "unconfirmed"].includes(row.reason)) return "look";
  return "unchecked"; // anything unexpected, such as no_github_evidence
}

/** The cut-off for the bar: the default while Auto is on, else the typed number kept within 0-100. */
export function effectiveCutoff(auto: boolean, typed: string): number {
  if (auto) return DEFAULT_SUGGESTED_CUTOFF;
  const number = Number(typed);
  if (typed.trim() === "" || Number.isNaN(number)) return DEFAULT_SUGGESTED_CUTOFF;
  return Math.min(100, Math.max(0, number));
}

export const barPercent = (value: number | null | undefined): number =>
  value === null || value === undefined ? 0 : Math.min(100, Math.max(0, value));

const FLAG_NOTES: Record<string, (result: ScoreResult) => string> = {
  jd_has_unmapped_terms: (r) =>
    `${r.jd_unmapped_terms} term${r.jd_unmapped_terms === 1 ? "" : "s"} in the job description could not be matched to a known skill.`,
  scan_incomplete: () => "Only some repositories were scanned.",
  github_username_mismatch: () => "The GitHub username entered is different from the one in the CV.",
  github_supplied_without_cv_link: () => "The GitHub username was entered by hand; the CV has no GitHub link.",
  github_ambiguous_in_cv: () => "More than one GitHub account appears in the CV.",
  weak_evidence_used: () => "Some skills show only a small trace in the code.",
  mostly_unverifiable: () => "Most of the job's skills can't be checked from code.",
  cv_keyword_stuffing_signal: () => "The CV repeats skill words unusually often.",
  cv_truncated: () => "The CV was long, so only the first part was read.",
  no_must_have_skills: () => "The job description has no clearly required skills.",
  tech_scan_missing: () => "The code scan returned no results, so some skills could not be checked.",
  no_github_file: () => "No GitHub data was used for this score.",
};

/** CV warnings worth a note. Contact warnings and ones already covered by a flag are left out. */
const WARNING_NOTES: Record<string, string | null> = {
  no_github_found: "No GitHub link was found in the CV.",
  name_not_verified: "The candidate's name could not be confirmed in the CV text.",
  github_ambiguous: null,
  no_email_found: null,
  no_phone_found: null,
  truncated: null,
  many_skills_little_context: null,
};

/** One plain sentence per item. Unknown flags are ignored; unknown CV warnings are shown humanized. */
export function buildNotes(result: ScoreResult, meta?: JobMeta | null): string[] {
  const notes: string[] = [];
  if (result.skills.some((row) => row.reason === "not_detectable" || row.github_status === "not_detectable_from_github")) {
    notes.push("Some skills can't be checked from code, so they get half credit.");
  }
  for (const flag of result.flags) {
    const note = FLAG_NOTES[flag]?.(result);
    if (note && !notes.includes(note)) notes.push(note);
  }
  for (const warning of result.cv_warnings) {
    const note = warning in WARNING_NOTES ? WARNING_NOTES[warning] : `${humanize(warning)}.`;
    if (note && !notes.includes(note)) notes.push(note);
  }
  if (meta?.fallback_used) {
    notes.push(`A backup AI model (${meta.model ?? "unknown"}) read the documents because the main one was busy. `
      + "Results can differ slightly between models.");
  }
  return notes;
}

export const downloadName = (result: ScoreResult): string => `${result.cv_label}__${result.job_name}_score.json`;
