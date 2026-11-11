import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../app/App";
import type { JobMeta, ScoreResult, SkillRow } from "../api/types";
import { ThemeProvider } from "../ThemeProvider";
import { fixtureA, fixtureB, fixtureC, metaA, metaB, metaC } from "./fixtures";
import {
  DEFAULT_SUGGESTED_CUTOFF, buildNotes, downloadName, effectiveCutoff, evidenceOf, formatPoints, formatScore, humanize,
} from "./format";
import { ScoreView } from "./ScoreView";

const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value));
const wrap = (node: React.ReactNode, url = "/recruiter/score/j1") => (
  <ThemeProvider><MemoryRouter initialEntries={[url]}>{node}</MemoryRouter></ThemeProvider>
);
const renderView = (result: ScoreResult, meta?: JobMeta | null) => render(wrap(<ScoreView result={result} meta={meta} />));
const row = (over: Partial<SkillRow>): SkillRow => ({ ...clone(fixtureA.skills[0]), ...over });

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new TypeError("offline"))));
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const rows = () => within(screen.getByRole("table")).getAllByRole("row").slice(1, -1); // without the header and the total
const cells = (r: HTMLElement) => within(r).getAllByRole("cell").map((c) => c.textContent);

// =============================================================================================================
describe("formatting helpers", () => {
  it("humanizes names", () => {
    expect(humanize("Data_Analyst")).toBe("Data Analyst");
    expect(humanize("priya-sample")).toBe("Priya sample");
    expect(humanize("__x__y")).toBe("X y");
    expect(humanize("")).toBe("");
  });
  it("formats scores and points without a trailing .0", () => {
    expect(formatScore(44.4)).toBe("44.4");
    expect(formatScore(70)).toBe("70");
    expect(formatScore(null)).toBe("n/a");
    expect(formatPoints(3.0)).toBe("3");
    expect(formatPoints(1.5)).toBe("1.5");
    expect(formatPoints(0)).toBe("0");
    expect(formatPoints(null)).toBe("n/a");
  });
  it("the default cut-off is one exported constant, 35", () => {
    expect(DEFAULT_SUGGESTED_CUTOFF).toBe(35);
    expect(effectiveCutoff(true, "80")).toBe(35);
    expect(effectiveCutoff(false, "80")).toBe(80);
    expect(effectiveCutoff(false, "150")).toBe(100);
    expect(effectiveCutoff(false, "-5")).toBe(0);
    expect(effectiveCutoff(false, "")).toBe(35);
    expect(effectiveCutoff(false, "abc")).toBe(35);
  });
  it("names the download after the CV and the job", () => {
    expect(downloadName(fixtureA)).toBe("priya_sample__Data_Analyst_score.json");
  });
  it.each<[string, Partial<SkillRow>, string]>([
    ["confirmed", { claimed: true, github_status: "found", reason: "confirmed" }, "found"],
    ["weak evidence", { claimed: true, github_status: "found", reason: "weak_evidence" }, "look"],
    ["not found", { claimed: true, github_status: "not_found", reason: "not_found_on_github" }, "look"],
    ["scan incomplete", { claimed: true, github_status: "not_found_scan_incomplete", reason: "scan_incomplete" }, "look"],
    ["unconfirmed", { claimed: true, github_status: "found", reason: "unconfirmed" }, "look"],
    ["not detectable (reason)", { claimed: true, github_status: null, reason: "not_detectable" }, "unchecked"],
    ["not detectable (status)", { claimed: true, github_status: "not_detectable_from_github", reason: "confirmed" }, "unchecked"],
    ["not claimed", { claimed: false, github_status: "not_found", reason: "not_claimed" }, "unclaimed"],
    ["not claimed but not detectable", { claimed: false, github_status: "not_detectable_from_github", reason: "not_claimed" }, "unchecked"],
    ["no GitHub evidence", { claimed: true, github_status: null, reason: "no_github_evidence" }, "unchecked"],
    ["something new", { claimed: true, github_status: "weird", reason: "brand_new" }, "unchecked"],
  ])("evidence badge: %s", (_name, over, expected) => {
    expect(evidenceOf(row(over))).toBe(expected);
  });
});

// =============================================================================================================
describe("fixture A (nothing confirmed)", () => {
  it("shows the header, the score and the three small cards from the data", () => {
    renderView(fixtureA);
    expect(screen.getByRole("heading", { level: 1, name: "Data Analyst" })).toBeInTheDocument();
    expect(screen.getByText("Priya sample")).toBeInTheDocument();
    expect(screen.getByText("octocat", { selector: ".handle" })).toBeInTheDocument();
    expect(screen.getByText("Repo fingerprint score")).toBeInTheDocument();
    expect(screen.getByTestId("fingerprint")).toHaveTextContent("44.4");
    expect(screen.getByText("out of 100")).toBeInTheDocument();
    expect(screen.getByText("How much of the job's required skills the CV claims and the GitHub code confirms.")).toBeInTheDocument();
    expect(screen.getByText("Shows where the evidence sits. It is not a decision.")).toBeInTheDocument();
    for (const [title, value, text] of [
      ["Claim coverage", "88.9", "Share of the job's skill weight the CV claims."],
      ["Checkable only", "46.2", "The score counting only skills that code can show."],
      ["Verifiable share", "72.2", "Share of the job's skill weight that GitHub can check at all."],
    ]) {
      const card = screen.getByRole("heading", { name: title }).closest(".sv-card") as HTMLElement;
      expect(within(card).getByText(value)).toBeInTheDocument();
      expect(within(card).getByText(text)).toBeInTheDocument();
      expect(within(card).getByRole("img", { name: `${title} ${value} out of 100` })).toBeInTheDocument();
    }
  });

  it("lists every skill in API order with tier, claimed, evidence and points", () => {
    renderView(fixtureA);
    const body = rows();
    expect(body).toHaveLength(8);
    expect(body.map((r) => within(r).getByRole("rowheader").textContent)).toEqual(fixtureA.skills.map((s) => s.skill));
    expect(cells(body[0])).toEqual(["Must-have", "Yes", "Needs a look", "1.5 of 3"]);
    expect(cells(body[3])).toEqual(["Must-have", "Yes", "Not checked", "1.5 of 3"]); // Tableau
    expect(cells(body[5])).toEqual(["Nice-to-have", "No", "Not checked", "0 of 1"]); // Git
    expect(cells(body[6])).toEqual(["Nice-to-have", "No", "Not claimed", "0 of 1"]); // Snowflake
    expect(cells(body[7])).toEqual(["Nice-to-have", "Yes", "Not checked", "0.5 of 1"]); // Statistics
    const foot = screen.getByRole("table").querySelector("tfoot")!;
    expect(foot).toHaveTextContent("Points earned");
    expect(foot).toHaveTextContent("8 of 18");
  });

  it("counts the badges", () => {
    renderView(fixtureA);
    const table = within(screen.getByRole("table"));
    expect(table.getAllByText("Needs a look")).toHaveLength(4);
    expect(table.getAllByText("Not checked")).toHaveLength(3);
    expect(table.getAllByText("Not claimed")).toHaveLength(1);
    expect(table.queryByText("Found in code")).toBeNull();
  });

  it("every badge has an icon and text, with the right icon", () => {
    renderView(fixtureA);
    const iconOf = (text: string) => screen.getAllByText(text, { selector: ".sv-badge__text" })[0].parentElement!.querySelector("svg")!;
    expect(iconOf("Needs a look")).toHaveAttribute("data-icon", "eye");
    expect(iconOf("Not checked")).toHaveAttribute("data-icon", "dash");
    expect(iconOf("Not claimed")).toHaveAttribute("data-icon", "circle");
    expect(iconOf("Scan complete")).toHaveAttribute("data-icon", "check");
    for (const badge of document.querySelectorAll(".sv-badge")) {
      expect(badge.querySelector("svg")).not.toBeNull();
      expect(badge.textContent?.trim()).not.toBe("");
    }
  });

  it("has proper table semantics", () => {
    renderView(fixtureA);
    const table = screen.getByRole("table", { name: "Per-skill breakdown" });
    expect(table.querySelector("caption")).toHaveClass("sr-only");
    const headers = within(table).getAllByRole("columnheader");
    expect(headers.map((h) => h.textContent)).toEqual(["Skill", "Tier", "Claimed on CV", "GitHub evidence", "Points"]);
    headers.forEach((h) => expect(h).toHaveAttribute("scope", "col"));
    within(table).getAllByRole("rowheader").forEach((h) => expect(h).toHaveAttribute("scope", "row"));
  });

  it("shows the GitHub scan card", () => {
    renderView(fixtureA);
    const card = screen.getByRole("heading", { name: "GitHub scan" }).closest(".sv-card") as HTMLElement;
    expect(within(card).getByText("octocat")).toHaveClass("t-code");
    expect(within(card).getByText("6 of 6 repositories scanned")).toBeInTheDocument();
    expect(within(card).getAllByRole("listitem").map((li) => li.textContent)).toEqual(["CSS", "HTML", "JavaScript"]);
    expect(within(card).getByText("Scan complete")).toBeInTheDocument();
    expect(within(card).queryByText("Only some repositories were scanned.")).toBeNull();
  });

  it("shows the empty hidden-strengths state and the CV extras", () => {
    renderView(fixtureA);
    const hidden = screen.getByRole("heading", { name: "Hidden strengths" }).closest(".sv-card") as HTMLElement;
    expect(within(hidden).getByText("No extra skills found in the code.")).toBeInTheDocument();
    const extras = screen.getByRole("heading", { name: "Also on the CV" }).closest(".sv-card") as HTMLElement;
    expect(within(extras).getAllByRole("listitem").map((li) => li.textContent)).toEqual(fixtureA.cv_extra_skills.names);
  });

  it("shows the notes: half credit and the unmapped term", () => {
    renderView(fixtureA);
    const notes = screen.getByRole("heading", { name: "Notes" }).closest(".sv-card") as HTMLElement;
    expect(within(notes).getAllByRole("listitem").map((li) => li.textContent)).toEqual([
      "Some skills can't be checked from code, so they get half credit.",
      "1 term in the job description could not be matched to a known skill.",
    ]);
    expect(notes.querySelector('[data-icon="info"]')).not.toBeNull();
  });

  it("never shows a verdict word", () => {
    const { container } = renderView(fixtureA);
    expect(container.textContent).not.toMatch(/\b(hire|reject|pass|fail|strong candidate|weak candidate)/i);
  });
});

// =============================================================================================================
describe("fixture B (stronger)", () => {
  it("renders its own numbers, badges, total and hidden strengths", () => {
    renderView(fixtureB);
    expect(screen.getByTestId("fingerprint")).toHaveTextContent("69.4");
    expect(screen.getByText("80.8")).toBeInTheDocument();
    const table = within(screen.getByRole("table"));
    expect(table.getAllByText("Found in code")).toHaveLength(3);
    expect(table.getAllByText("Needs a look")).toHaveLength(1);
    expect(cells(rows()[0])).toEqual(["Must-have", "Yes", "Found in code", "3 of 3"]);
    expect(screen.getByRole("table").querySelector("tfoot")).toHaveTextContent("12.5 of 18");
    const hidden = screen.getByRole("heading", { name: "Hidden strengths" }).closest(".sv-card") as HTMLElement;
    expect(within(hidden).getAllByRole("listitem").map((li) => li.textContent)).toEqual(["Jupyter Notebook", "Flask"]);
    expect(within(hidden).queryByText("No extra skills found in the code.")).toBeNull();
    expect(screen.getByRole("img", { name: "69.4 out of 100, suggested cut-off 35" })).toBeInTheDocument();
  });

  it("draws everything from the data, not from sample values", () => {
    const other = clone(fixtureB);
    other.job_name = "Backend-engineer";
    other.cv_label = "arjun_k";
    other.github.username = "ghost";
    other.score.repo_fingerprint = 12.3;
    other.score.claim_coverage = 55.5;
    other.score.points_earned = 4.25;
    other.score.points_possible = 20;
    other.skills = [row({ skill: "Rust", points: 2.5, weight: 5, reason: "confirmed", github_status: "found" })];
    other.github.languages_top = ["Rust"];
    other.hidden_strengths = [{ skill: "Go", confidence: "weak" }];
    renderView(other);
    expect(screen.getByRole("heading", { level: 1, name: "Backend engineer" })).toBeInTheDocument();
    expect(screen.getByText("Arjun k")).toBeInTheDocument();
    expect(screen.getByTestId("fingerprint")).toHaveTextContent("12.3");
    expect(screen.getByText("55.5")).toBeInTheDocument();
    expect(screen.getByText("2.5 of 5")).toBeInTheDocument();
    expect(screen.getByRole("table").querySelector("tfoot")).toHaveTextContent("4.25 of 20");
    expect(screen.getByRole("img", { name: "12.3 out of 100, suggested cut-off 35" })).toBeInTheDocument();
    for (const gone of ["44.4", "69.4", "88.9", "Priya", "Data Analyst", "Pandas", "Jupyter Notebook"]) expect(screen.queryByText(new RegExp(gone))).toBeNull();
    expect(screen.getByText("Go")).toBeInTheDocument();
  });
});

// =============================================================================================================
describe("the score bar and the cut-off", () => {
  it("has an aria-label, a dot at the score and a dashed tick at the cut-off", () => {
    renderView(fixtureA);
    expect(screen.getByRole("img", { name: "44.4 out of 100, suggested cut-off 35" })).toBeInTheDocument();
    expect(screen.getByTestId("score-dot")).toHaveStyle({ left: "44.4%" });
    expect(screen.getByTestId("cutoff-tick")).toHaveStyle({ left: "35%" });
    expect(screen.getByText("Suggested cut-off 35")).toBeInTheDocument();
    expect(screen.getByText("0")).toBeInTheDocument();
    expect(screen.getByText("100")).toBeInTheDocument();
  });

  it("Auto threshold is on by default with no input; off reveals a Cut-off input that moves the tick", () => {
    renderView(fixtureA);
    const toggle = screen.getByRole("switch", { name: "Auto threshold" });
    expect(toggle).toHaveAttribute("aria-checked", "true");
    expect(screen.queryByLabelText("Cut-off")).toBeNull();
    fireEvent.click(toggle);
    const input = screen.getByLabelText("Cut-off");
    expect(input).toHaveAttribute("type", "number");
    expect(input).toHaveAttribute("min", "0");
    expect(input).toHaveAttribute("max", "100");
    fireEvent.change(input, { target: { value: "60" } });
    expect(screen.getByTestId("cutoff-tick")).toHaveStyle({ left: "60%" });
    expect(screen.getByRole("img", { name: "44.4 out of 100, suggested cut-off 60" })).toBeInTheDocument();
    expect(screen.getByText("Suggested cut-off 60")).toBeInTheDocument();
    fireEvent.change(input, { target: { value: "150" } });
    expect(screen.getByTestId("cutoff-tick")).toHaveStyle({ left: "100%" });
    fireEvent.change(input, { target: { value: "" } });
    expect(screen.getByTestId("cutoff-tick")).toHaveStyle({ left: "35%" });
    fireEvent.change(input, { target: { value: "70" } });
    fireEvent.click(toggle); // back to Auto: the default again
    expect(screen.getByTestId("cutoff-tick")).toHaveStyle({ left: "35%" });
    expect(screen.queryByLabelText("Cut-off")).toBeNull();
  });
});

// =============================================================================================================
describe("backup model note", () => {
  const SENTENCE = "A backup AI model (gemini-3.5-flash-lite) read the documents because the main one was busy. "
    + "Results can differ slightly between models.";
  const backup = { ...metaA, model: "gemini-3.5-flash-lite", fallback_used: true };

  it("appears in the Notes card only when fallback_used is true", () => {
    renderView(fixtureA, backup);
    const card = screen.getByRole("heading", { name: "Notes" }).closest(".sv-card") as HTMLElement;
    expect(within(card).getByText(SENTENCE)).toBeInTheDocument();
  });

  it("is absent when fallback_used is false, or meta is missing or has no model fields", () => {
    for (const meta of [{ ...metaA, model: "gemini-3.6-flash", fallback_used: false }, metaA, null, undefined]) {
      expect(buildNotes(fixtureA, meta).some((note) => note.includes("backup AI model"))).toBe(false);
    }
  });

  it("is the only note when nothing else needs saying, and the Notes card then exists", () => {
    const quiet = clone(fixtureB);
    quiet.flags = [];
    quiet.cv_warnings = [];
    quiet.skills = quiet.skills.filter((r) => r.reason !== "not_detectable" && r.github_status !== "not_detectable_from_github");
    expect(buildNotes(quiet, backup)).toEqual([SENTENCE]);
    renderView(quiet, backup);
    expect(screen.getByText(SENTENCE)).toBeInTheDocument();
  });

  it("shows nothing about it for fallback_used false", () => {
    renderView(fixtureA, { ...metaA, fallback_used: false });
    expect(screen.queryByText(/backup AI model/)).toBeNull();
  });
});

// =============================================================================================================
describe("notes", () => {
  const withFlags = (flags: string[], extra: Partial<ScoreResult> = {}) => ({ ...clone(fixtureB), flags, ...extra }) as ScoreResult;

  it.each([
    ["scan_incomplete", "Only some repositories were scanned."],
    ["github_username_mismatch", "The GitHub username entered is different from the one in the CV."],
    ["github_supplied_without_cv_link", "The GitHub username was entered by hand; the CV has no GitHub link."],
    ["github_ambiguous_in_cv", "More than one GitHub account appears in the CV."],
    ["weak_evidence_used", "Some skills show only a small trace in the code."],
    ["mostly_unverifiable", "Most of the job's skills can't be checked from code."],
    ["cv_keyword_stuffing_signal", "The CV repeats skill words unusually often."],
    ["cv_truncated", "The CV was long, so only the first part was read."],
    ["no_must_have_skills", "The job description has no clearly required skills."],
    ["tech_scan_missing", "The code scan returned no results, so some skills could not be checked."],
    ["no_github_file", "No GitHub data was used for this score."],
  ])("flag %s gives one plain sentence", (flag, sentence) => {
    expect(buildNotes(withFlags([flag]))).toContain(sentence);
  });

  it("the unmapped-terms note uses the count and the right plural", () => {
    expect(buildNotes(withFlags(["jd_has_unmapped_terms"], { jd_unmapped_terms: 2 }))).toContain("2 terms in the job description could not be matched to a known skill.");
    expect(buildNotes(withFlags(["jd_has_unmapped_terms"], { jd_unmapped_terms: 1 }))).toContain("1 term in the job description could not be matched to a known skill.");
  });

  it("ignores unknown flags, and shows no Notes card when there is nothing to say", () => {
    const quiet = withFlags(["something_new"]);
    quiet.skills = quiet.skills.filter((s) => s.reason === "confirmed");
    expect(buildNotes(quiet)).toEqual([]);
    renderView(quiet);
    expect(screen.queryByRole("heading", { name: "Notes" })).toBeNull();
  });

  it("includes CV warnings: known ones as sentences, contact ones hidden, unknown ones humanized", () => {
    const notes = buildNotes(withFlags([], { cv_warnings: ["no_github_found", "no_email_found", "no_phone_found", "truncated", "odd_new_warning"] }));
    expect(notes).toContain("No GitHub link was found in the CV.");
    expect(notes).toContain("Odd new warning.");
    expect(notes.some((n) => /email|phone/i.test(n))).toBe(false);
  });

  it("an incomplete scan shows the Needs a look badge and the note in the scan card", () => {
    const partial = withFlags(["scan_incomplete"]);
    partial.github.scan_complete = false;
    partial.github.repos_scanned = 4;
    renderView(partial);
    const card = screen.getByRole("heading", { name: "GitHub scan" }).closest(".sv-card") as HTMLElement;
    expect(within(card).getByText("Needs a look")).toBeInTheDocument();
    expect(within(card).getByText("4 of 6 repositories scanned")).toBeInTheDocument();
    expect(within(card).getByText("Only some repositories were scanned.")).toBeInTheDocument();
    expect(within(card).queryByText("Scan complete")).toBeNull();
  });

  it("shows up to 12 CV extras and then +N more", () => {
    const many = clone(fixtureB);
    many.cv_extra_skills = { count: 15, names: Array.from({ length: 15 }, (_, i) => `Skill${i + 1}`) };
    renderView(many);
    const extras = screen.getByRole("heading", { name: "Also on the CV" }).closest(".sv-card") as HTMLElement;
    const items = within(extras).getAllByRole("listitem").map((li) => li.textContent);
    expect(items).toHaveLength(13);
    expect(items[11]).toBe("Skill12");
    expect(items[12]).toBe("+3 more");
  });
});

// =============================================================================================================
describe("header: real candidate name and job title", () => {
  it("fixture A shows meta.job_title as the title and meta.candidate_name as the subline", () => {
    renderView(fixtureA, metaA);
    expect(screen.getByRole("heading", { level: 1, name: "Data Analyst" })).toBeInTheDocument();
    expect(screen.getByText("Priya Raman")).toBeInTheDocument();
    expect(screen.queryByText(humanize(fixtureA.cv_label))).toBeNull();
  });

  it("fixture B shows the same real values", () => {
    renderView(fixtureB, metaB);
    expect(screen.getByRole("heading", { level: 1, name: "Data Analyst" })).toBeInTheDocument();
    expect(screen.getByText("Priya Raman")).toBeInTheDocument();
  });

  it("fixture C with both values null falls back to the humanised file-name labels", () => {
    renderView(fixtureC, metaC);
    expect(screen.getByRole("heading", { level: 1, name: "Data Analyst" })).toBeInTheDocument();
    expect(screen.getByText(humanize(fixtureA.cv_label))).toBeInTheDocument();
    expect(screen.queryByText("Priya Raman")).toBeNull();
  });

  it("each value falls back on its own, and an old job without meta still renders", () => {
    const view = renderView(fixtureA, { candidate_name: null, job_title: "Backend Developer (Python)" });
    expect(screen.getByRole("heading", { level: 1, name: "Backend Developer (Python)" })).toBeInTheDocument();
    expect(screen.getByText(humanize(fixtureA.cv_label))).toBeInTheDocument();
    view.unmount();
    renderView(fixtureA, { candidate_name: "Priya Raman", job_title: null });
    expect(screen.getByRole("heading", { level: 1, name: "Data Analyst" })).toBeInTheDocument();
    expect(screen.getByText("Priya Raman")).toBeInTheDocument();
    cleanup();
    renderView(fixtureA);
    expect(screen.getByText(humanize(fixtureA.cv_label))).toBeInTheDocument();
  });

  it("the page passes the job's meta through, and the download name still uses cv_label and job_name", async () => {
    const body = { id: "j1", state: "done", stage: "Done", result: fixtureA, error: null, meta: metaA };
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(body), { status: 200 })));
    render(wrap(<App />, "/recruiter/score/j1"));
    expect(await screen.findByRole("heading", { level: 1, name: "Data Analyst" })).toBeInTheDocument();
    expect(screen.getByText("Priya Raman")).toBeInTheDocument();
    expect(downloadName(fixtureA)).toBe("priya_sample__Data_Analyst_score.json");
  });
});

// =============================================================================================================
describe("fixture C (not ok, null scores)", () => {
  it("shows the rose-gold banner with the message, n/a figures and the same layout", () => {
    renderView(fixtureC);
    expect(screen.getByRole("status")).toHaveTextContent("The GitHub account has no public repositories to check.");
    expect(screen.getByTestId("fingerprint")).toHaveTextContent("n/a");
    expect(screen.queryByText("out of 100")).toBeNull();
    for (const title of ["Claim coverage", "Checkable only", "Verifiable share"]) {
      const card = screen.getByRole("heading", { name: title }).closest(".sv-card") as HTMLElement;
      expect(within(card).getByText("n/a")).toBeInTheDocument();
    }
    expect(screen.getByRole("img", { name: "n/a, suggested cut-off 35" })).toBeInTheDocument();
    expect(screen.queryByTestId("score-dot")).toBeNull();
    expect(cells(rows()[0])).toEqual(["Must-have", "Yes", "Not checked", "n/a"]);
    expect(cells(rows()[2])).toEqual(["Nice-to-have", "No", "Not claimed", "0 of 1"]);
    expect(screen.getByRole("table").querySelector("tfoot")).toHaveTextContent("0 of 10");
    expect(screen.getByRole("heading", { name: "GitHub scan" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Continue to question review" })).toBeInTheDocument();
    expect(screen.queryByText("Some skills can't be checked from code, so they get half credit.")).toBeNull();
  });

  it("uses a generic sentence when the message is empty", () => {
    const quiet = clone(fixtureC);
    quiet.message = "";
    renderView(quiet);
    expect(screen.getByRole("status")).toHaveTextContent("A full score could not be calculated for this candidate.");
  });

  it("falls back to the CV's GitHub handle in the header", () => {
    const noUser = clone(fixtureC);
    noUser.github.username = null;
    renderView(noUser);
    expect(screen.getByText("octocat", { selector: ".handle" })).toBeInTheDocument();
    noUser.github.cv_username = null;
    cleanup();
    renderView(noUser);
    expect(document.querySelector(".handle")).toBeNull();
  });
});

// =============================================================================================================
describe("Download JSON and Continue", () => {
  it("downloads the result as a Blob with the right file name, sending nothing", async () => {
    const blobs: Blob[] = [];
    URL.createObjectURL = vi.fn((blob: Blob) => (blobs.push(blob), "blob:test"));
    URL.revokeObjectURL = vi.fn();
    const names: string[] = [];
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      names.push(this.download);
    });
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    renderView(fixtureA);
    fireEvent.click(screen.getByRole("button", { name: "Download JSON" }));
    expect(names).toEqual(["priya_sample__Data_Analyst_score.json"]);
    expect(blobs).toHaveLength(1);
    expect(blobs[0].type).toBe("application/json");
    expect(JSON.parse(await blobs[0].text())).toEqual(fixtureA);
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:test");
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});

// =============================================================================================================
describe("the page: loading, failed, unknown and done", () => {
  const reply = (status: number, body: unknown) => vi.fn(async () => new Response(JSON.stringify(body), { status }));
  const renderPage = () => render(wrap(<App />, "/recruiter/score/j1"));

  it("shows skeleton cards while loading, under the Score heading and the top bar", async () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => undefined)));
    renderPage();
    expect(screen.getByRole("status", { name: "Loading the result" })).toHaveAttribute("aria-busy", "true");
    expect(screen.getByRole("heading", { level: 1, name: "Score" })).toBeInTheDocument();
    expect(document.querySelectorAll(".skeleton-card").length).toBeGreaterThan(3);
    expect(screen.getByRole("navigation", { name: "Progress" }).querySelector('[aria-current="step"]')).toHaveTextContent("Score");
  });

  it("shows the real screen when the job is done, and Continue goes to /recruiter/questions/<id>", async () => {
    const answer = vi.fn(async (url: string) => {
      const questions = new URL(url).pathname.endsWith("/questions");
      const body = questions
        ? { id: "j1", state: "queued", stage: "Choosing code", result: null, error: null }
        : { id: "j1", state: "done", stage: "Done", result: fixtureB, error: null };
      return new Response(JSON.stringify(body), { status: 200 });
    });
    vi.stubGlobal("fetch", answer);
    renderPage();
    expect(await screen.findByRole("heading", { level: 1, name: "Data Analyst" })).toBeInTheDocument();
    expect(screen.getByTestId("fingerprint")).toHaveTextContent("69.4");
    fireEvent.click(screen.getByRole("button", { name: "Continue to question review" }));
    expect(await screen.findByText("Generating questions")).toBeInTheDocument();
    expect(answer.mock.calls.some(([url]) => new URL(url).pathname === "/api/analyses/j1/questions")).toBe(true);
  });

  it("a failed job shows the error banner and Back to upload", async () => {
    vi.stubGlobal("fetch", reply(200, { id: "j1", state: "failed", stage: "", result: null, error: { code: "ai_busy", message: "x", retryable: true } }));
    renderPage();
    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText("The AI service is busy right now.")).toBeInTheDocument();
    expect(within(alert).getByRole("button", { name: "Retry" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Back to upload" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Upload" })).toBeInTheDocument();
  });

  it("a failed job that cannot be retried has no Retry button", async () => {
    vi.stubGlobal("fetch", reply(200, { id: "j1", state: "failed", stage: "", result: null, error: { code: "cv_unreadable", message: "x", retryable: false } }));
    renderPage();
    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText("We couldn't read the CV.")).toBeInTheDocument();
    expect(within(alert).queryByRole("button", { name: "Retry" })).toBeNull();
  });

  it("Retry asks the server to retry and polls again", async () => {
    let retried = false;
    const fake = vi.fn(async (url: string) => {
      if (String(url).endsWith("/retry")) {
        retried = true;
        return new Response(JSON.stringify({ id: "j1" }), { status: 202 });
      }
      return new Response(JSON.stringify(retried
        ? { id: "j1", state: "done", stage: "Done", result: fixtureA, error: null }
        : { id: "j1", state: "failed", stage: "", result: null, error: { code: "timeout", message: "x", retryable: true } }));
    });
    vi.stubGlobal("fetch", fake);
    renderPage();
    fireEvent.click(within(await screen.findByRole("alert")).getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Data Analyst" })).toBeInTheDocument();
  });

  it("an unknown id (404) shows a calm message with a link to Upload", async () => {
    vi.stubGlobal("fetch", reply(404, { message: "That analysis was not found. It may have expired." }));
    renderPage();
    expect(await screen.findByRole("heading", { level: 1, name: "We can't find this analysis" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Go to Upload" })).toHaveAttribute("href", "/recruiter/upload");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("an unreachable server shows the network banner after two failed polls", async () => {
    vi.useFakeTimers();
    renderPage();
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
    const alert = screen.getByRole("alert");
    expect(within(alert).getByText("Can't reach the SkillVerify server.")).toBeInTheDocument();
    expect(within(alert).getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });
});
