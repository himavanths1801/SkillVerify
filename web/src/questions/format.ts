import type { SkippedSkill } from "../api/types";

export const MAX_QUESTION_CHARS = 1500;
export const COUNTER_FROM = MAX_QUESTION_CHARS - 100;
export const MIN_APPROVED = 3;
export const PREVIEW_LINES = 8;

const LANGUAGES: Record<string, string> = {
  python: "Python", sql: "SQL", typescript: "TypeScript", javascript: "JavaScript", bash: "Bash",
};

/** python -> Python, sql -> SQL, ...; anything else is capitalised. */
export function languageLabel(language: string): string {
  const known = LANGUAGES[language.toLowerCase()];
  if (known) return known;
  return language.length === 0 ? "" : language[0].toUpperCase() + language.slice(1);
}

/** Trim and collapse whitespace, so a re-flowed question does not count as edited. */
export function normalise(text: string): string {
  return text.trim().replace(/\s+/g, " ");
}

export function kindLabel(kind: string): string {
  return kind === "fix_code" ? "Fix the bug" : "Write code";
}

export const STAGE_TEXT: Record<string, string> = {
  "Choosing code": "Choosing code...",
  "Reading files": "Reading files...",
  "Writing questions": "Writing questions from the candidate's code...",
  "Checking questions": "Checking questions...",
};

/** The step text under the header while the run is going. An unknown or missing stage reads like the first step. */
export function stageText(stage: string | null | undefined): string {
  return (stage && STAGE_TEXT[stage]) || STAGE_TEXT["Choosing code"];
}

export const REASON_GROUPS: { reason: string; label: string }[] = [
  { reason: "not_found_in_code", label: "Not found in the candidate's code" },
  { reason: "not_checkable_from_code", label: "Can't be checked from code" },
  { reason: "no_usable_file", label: "No suitable code file found" },
  { reason: "question_failed", label: "A question couldn't be written" },
  { reason: "not_enough_room", label: "Not enough room in the session" },
];

/** The skipped skills grouped by reason in a fixed order; reasons we do not know go under "Other". */
export function groupSkipped(skipped: SkippedSkill[]): { label: string; skills: string[] }[] {
  const known = new Set(REASON_GROUPS.map((g) => g.reason));
  const groups = REASON_GROUPS.map((g) => ({
    label: g.label,
    skills: skipped.filter((s) => s.reason === g.reason).map((s) => s.skill),
  }));
  groups.push({ label: "Other", skills: skipped.filter((s) => !known.has(s.reason)).map((s) => s.skill) });
  return groups.filter((g) => g.skills.length > 0);
}

export function plural(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`;
}
