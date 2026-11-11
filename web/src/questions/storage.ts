import type { Question, SkippedSkill } from "../api/types";

export type ItemStatus = "pending" | "approved" | "dropped";
export type ReviewItem = { question: Question; status: ItemStatus; text: string };
export type StoredReview = { runIds: string[]; items: ReviewItem[]; skipped: SkippedSkill[] };

const reviewKey = (id: string) => `skillverify.review.${id}`;
const assessmentKey = (id: string) => `skillverify.assessment.${id}`;

function isStored(value: unknown): value is StoredReview {
  if (typeof value !== "object" || value === null) return false;
  const v = value as Record<string, unknown>;
  return Array.isArray(v.runIds) && Array.isArray(v.items) && Array.isArray(v.skipped);
}

/** The saved review of this tab, or null (nothing saved, unreadable, or storage blocked). */
export function loadReview(id: string): StoredReview | null {
  try {
    const raw = window.sessionStorage.getItem(reviewKey(id));
    if (!raw) return null;
    const parsed: unknown = JSON.parse(raw);
    return isStored(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

export function saveReview(id: string, review: StoredReview): void {
  try {
    window.sessionStorage.setItem(reviewKey(id), JSON.stringify(review));
  } catch {
    // storage blocked or full: the screen still works, it just will not survive a refresh
  }
}

/** What the next step (the student screen) needs: id, text, language and starter code of each approved question. */
export type AssessmentQuestion = { id: string; text: string; language: string; starter_code: string };

export function saveAssessment(id: string, questions: AssessmentQuestion[]): void {
  try {
    window.sessionStorage.setItem(assessmentKey(id), JSON.stringify(questions));
  } catch {
    // see above
  }
}

/** A stored review only counts when it belongs to this very run (same question ids in the same order). */
export function matchesRun(stored: StoredReview, runIds: string[]): boolean {
  return stored.runIds.length === runIds.length && stored.runIds.every((id, i) => id === runIds[i]);
}
