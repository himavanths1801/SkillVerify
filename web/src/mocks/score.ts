// Mock of the Phase 1 scoring output (skillverify/scoring/score.py). Only the keys the web app reads are typed here.
// The skills list is abbreviated to four rows (8 of 10 points); the score block holds the fixed sample totals (8 of 18 points).

export type Tier = "must_have" | "nice_to_have";
export type GithubStatus = "confirmed" | "not_confirmed" | "not_detectable_from_github";

export type SkillRow = {
  skill: string;
  tier: Tier;
  claimed: boolean;
  github_status: GithubStatus;
  points: number;
  weight: number;
};

export type ScoreResult = {
  status: string;
  job_name: string;
  cv_label: string;
  github: { username: string };
  score: {
    repo_fingerprint: number;
    claim_coverage: number;
    checkable_only: number;
    verifiable_share: number;
    points_earned: number;
    points_possible: number;
  };
  skills: SkillRow[];
  hidden_strengths: string[];
  flags: string[];
};

export const scoreMock: ScoreResult = {
  status: "ok",
  job_name: "Data Analyst",
  cv_label: "Priya Sample",
  github: { username: "octocat" },
  score: {
    repo_fingerprint: 44.4,
    claim_coverage: 88.9,
    checkable_only: 46.2,
    verifiable_share: 72.2,
    points_earned: 8,
    points_possible: 18,
  },
  skills: [
    { skill: "Python", tier: "must_have", claimed: true, github_status: "confirmed", points: 3, weight: 3 },
    { skill: "SQL", tier: "must_have", claimed: true, github_status: "not_confirmed", points: 1.5, weight: 3 },
    { skill: "Tableau", tier: "nice_to_have", claimed: false, github_status: "not_detectable_from_github", points: 0.5, weight: 1 },
    { skill: "Pandas", tier: "must_have", claimed: true, github_status: "confirmed", points: 3, weight: 3 },
  ],
  hidden_strengths: ["Jupyter Notebook"],
  flags: [],
};
