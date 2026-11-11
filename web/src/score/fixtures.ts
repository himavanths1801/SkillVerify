// Test fixtures for the Score screen, in the exact shape of skillverify/scoring/score.py.
// They live in a JSON file so the screenshot script can read the same data.
import type { JobMeta, ScoreResult } from "../api/types";
import data from "./fixtures.json";

/** A: Priya / octocat, nothing confirmed (44.4 / 88.9 / 46.2 / 72.2, 8 of 18). */
export const fixtureA = data.a as unknown as ScoreResult;
/** B: a stronger result (69.4 / 88.9 / 80.8 / 72.2, 12.5 of 18). */
export const fixtureB = data.b as unknown as ScoreResult;
/** C: a non-ok result (no GitHub evidence) with null scores. */
export const fixtureC = data.c as unknown as ScoreResult;

/** The `meta` the API sends beside the result: A and B show a real name and title, C has neither (file-name fallback). */
export const metaA: JobMeta = { candidate_name: "Priya Raman", job_title: "Data Analyst" };
export const metaB: JobMeta = { candidate_name: "Priya Raman", job_title: "Data Analyst" };
export const metaC: JobMeta = { candidate_name: null, job_title: null };
