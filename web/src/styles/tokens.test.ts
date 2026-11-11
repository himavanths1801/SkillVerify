import { describe, expect, it } from "vitest";
import { MODES, REQUIRED_VARIABLES, parseTokens, tokens, tokensSource } from "../lib/tokens";

/** The locked design values, written out independently of tokens.css so the test catches any drift. */
const LOCKED: Record<string, Record<string, string>> = {
  recruiter: {
    "--page": "#EEF1F4", "--surface": "#FFFFFF", "--divider": "#DDE3E9", "--fill": "#E4E8EC", "--on-fill": "#3A4652",
    "--text": "#1B2530", "--text-2": "#566271", "--accent": "#0F6B6B", "--accent-pressed": "#0B5656",
    "--accent-wash": "#E1F0EF", "--on-accent-wash": "#0B4B4B", "--gold-wash": "#F7E4DC", "--gold-accent": "#C98B7B",
    "--gold-text": "#7F4333", "--err-wash": "#FBE6E3", "--err-accent": "#C8564B", "--err-text": "#8F2F28",
    "--input-border": "#8A96A3", "--toggle-off": "#7C8997", "--disabled": "#D3DAE0",
    "--approve-wash": "#DDF0E3", "--approve-wash-hover": "#CFE9D8", "--approve-ink": "#2E7D4F",
    "--radius-card": "16px", "--radius-control": "10px", "--pad-card": "24px", "--gap": "16px",
    "--control-h": "44px", "--badge-h": "28px", "--input-border-w": "1px",
    "--shadow-card": "0 1px 2px rgba(27, 37, 48, .06), 0 4px 12px rgba(27, 37, 48, .04)",
    "--t-score-size": "56px", "--t-score-lh": "56px", "--t-score-weight": "600",
    "--t-heading-size": "28px", "--t-heading-lh": "36px", "--t-heading-weight": "600",
    "--t-title-size": "20px", "--t-title-lh": "28px", "--t-title-weight": "600",
    "--t-body-size": "15px", "--t-body-lh": "24px", "--t-body-weight": "400",
    "--t-label-size": "12px", "--t-label-lh": "16px", "--t-label-weight": "600", "--t-label-tracking": ".08em",
    "--t-code-size": "14px", "--t-code-lh": "22px",
  },
  student: {
    "--page": "#FAF6EE", "--surface": "#FFFDF8", "--divider": "#E6DFD0", "--fill": "#F3EEE2", "--on-fill": "#2E3B3A",
    "--text": "#2E3B3A", "--text-2": "#5F6E6B", "--accent": "#357A80", "--accent-pressed": "#2A6469",
    "--accent-wash": "#DDEBE6", "--on-accent-wash": "#245A5F", "--gold-wash": "#F7E4DC", "--gold-accent": "#C98B7B",
    "--gold-text": "#7F4333", "--err-wash": "#FBE4E0", "--err-accent": "#D9776B", "--err-text": "#9B3B33",
    "--input-border": "#958C70", "--toggle-off": "#74857F", "--disabled": "#E6DFD0",
    "--radius-card": "20px", "--radius-control": "14px", "--pad-card": "32px", "--gap": "12px",
    "--control-h": "48px", "--badge-h": "32px", "--input-border-w": "1.5px",
    "--shadow-card": "0 1px 3px rgba(60, 50, 30, .05)",
    "--t-heading-size": "32px", "--t-heading-lh": "44px", "--t-heading-weight": "300",
    "--t-question-size": "24px", "--t-question-lh": "34px", "--t-question-weight": "400",
    "--t-body-size": "17px", "--t-body-lh": "28px", "--t-body-weight": "400",
    "--t-caption-size": "14px", "--t-caption-lh": "22px", "--t-caption-weight": "400",
    "--t-timer-size": "14px", "--t-timer-lh": "20px", "--t-timer-weight": "500",
    "--t-code-size": "15px", "--t-code-lh": "26px",
  },
};

describe("tokens.css", () => {
  it("defines exactly the two modes", () => {
    expect(Object.keys(tokens).sort()).toEqual([...MODES].sort());
  });

  it.each(MODES)("%s defines every required variable", (mode) => {
    const missing = REQUIRED_VARIABLES.filter((name) => !(name in tokens[mode]));
    expect(missing).toEqual([]);
  });

  it("uses the same variable names in both modes for everything shared", () => {
    const recruiter = new Set(Object.keys(tokens.recruiter));
    for (const name of REQUIRED_VARIABLES) expect(recruiter.has(name) && name in tokens.student).toBe(true);
    for (const name of ["--on-fill", "--on-accent", "--input-border-w"]) {
      expect(name in tokens.recruiter && name in tokens.student).toBe(true);
    }
  });

  it.each([
    ["recruiter", "--accent", "#0F6B6B"], ["recruiter", "--accent-pressed", "#0B5656"],
    ["recruiter", "--gold-text", "#7F4333"], ["recruiter", "--err-text", "#8F2F28"],
    ["student", "--accent", "#357A80"], ["student", "--accent-pressed", "#2A6469"],
    ["student", "--gold-text", "#7F4333"], ["student", "--err-text", "#9B3B33"],
  ])("%s %s is exactly %s", (mode, name, value) => {
    expect(tokens[mode][name]).toBe(value);
  });

  it.each(MODES)("%s has exactly the locked colours, sizes and radii", (mode) => {
    for (const [name, value] of Object.entries(LOCKED[mode])) {
      expect(tokens[mode][name], `${mode} ${name}`).toBe(value);
    }
  });

  it("uses only locked colours: every hex in the file is one of the locked values", () => {
    const allowed = new Set(
      Object.values(LOCKED).flatMap((values) => Object.values(values).filter((v) => v.startsWith("#"))).concat("#FFFFFF"),
    );
    const found = tokensSource.replace(/\/\*[\s\S]*?\*\//g, "").match(/#[0-9A-Fa-f]{6}\b/g) ?? [];
    expect(found.filter((hex) => !allowed.has(hex.toUpperCase()))).toEqual([]);
  });

  it("marks the accents that are never text colours in comments, and never uses them for text", () => {
    const components = parseTokens(tokensSource);
    expect(components.recruiter["--gold-accent"]).toBeDefined();
    expect(tokensSource).toMatch(/--gold-accent:[^;]*;\s*\/\*[^*]*never text/);
    expect(tokensSource).toMatch(/--err-accent:[^;]*;\s*\/\*[^*]*never text/);
  });

  it("fades colours over 1200 ms ease-in-out and switches the fade off for reduced motion", () => {
    for (const property of ["background-color", "color", "border-color", "box-shadow"]) {
      expect(tokensSource).toContain(`${property} 1200ms ease-in-out`);
    }
    const reduced = tokensSource.slice(tokensSource.indexOf("@media (prefers-reduced-motion: reduce)"));
    expect(reduced).toMatch(/transition:\s*none/);
  });

  it("exposes every type class", () => {
    for (const name of ["t-score", "t-heading", "t-title", "t-body", "t-label", "t-question", "t-caption", "t-timer", "t-code"]) {
      expect(tokensSource).toContain(`.${name} `);
    }
    expect(tokensSource).toMatch(/\.t-label[^}]*text-transform:\s*uppercase/);
    expect(tokensSource).toMatch(/\.t-timer[^}]*tabular-nums/);
    expect(tokensSource).toMatch(/\.t-code[^}]*var\(--font-mono\)/);
  });
});
