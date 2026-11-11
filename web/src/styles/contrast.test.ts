import { describe, expect, it } from "vitest";
import { contrastRatio } from "../lib/contrast";
import { MODES, tokens } from "../lib/tokens";

const WHITE = "#FFFFFF";

/** [foreground variable | "white", background variable]. All text pairs must reach 4.5:1 (WCAG AA). */
const REQUIRED_PAIRS: [string, string][] = [
  ["--text", "--page"],
  ["--text-2", "--page"],
  ["--text", "--surface"],
  ["--gold-text", "--gold-wash"],
  ["--err-text", "--err-wash"],
  ["--on-accent-wash", "--accent-wash"],
  ["white", "--accent"],
];

/** Further text pairs the base components actually use. */
const COMPONENT_PAIRS: [string, string][] = [
  ["--text-2", "--surface"],
  ["--accent", "--surface"],       // secondary button text
  ["--on-fill", "--fill"],         // file chip, neutral badge, timer
  ["--err-text", "--fill"],        // timer, low-time state
  ["--text", "--fill"],
  ["white", "--accent-pressed"],   // primary button hover / pressed
  ["--on-accent-wash", "--accent-wash"],
  ["--accent-pressed", "--accent-wash"], // secondary button pressed
];

function ratio(mode: string, foreground: string, background: string): number {
  const fg = foreground === "white" ? WHITE : tokens[mode][foreground];
  return contrastRatio(fg, tokens[mode][background]);
}

describe("WCAG contrast (text needs at least 4.5:1)", () => {
  for (const mode of MODES) {
    it.each(REQUIRED_PAIRS)(`${mode}: %s on %s`, (foreground, background) => {
      expect(ratio(mode, foreground, background), `${mode} ${foreground} on ${background}`).toBeGreaterThanOrEqual(4.5);
    });
    it.each(COMPONENT_PAIRS)(`${mode} components: %s on %s`, (foreground, background) => {
      expect(ratio(mode, foreground, background), `${mode} ${foreground} on ${background}`).toBeGreaterThanOrEqual(4.5);
    });
  }

  it("computes known ratios correctly", () => {
    expect(contrastRatio("#000000", "#FFFFFF")).toBeCloseTo(21, 5);
    expect(contrastRatio("#FFFFFF", "#FFFFFF")).toBeCloseTo(1, 5);
    expect(contrastRatio("#777777", "#FFFFFF")).toBeCloseTo(4.48, 1);
  });

  it("rejects colours that are not #RRGGBB", () => {
    expect(() => contrastRatio("red", "#FFFFFF")).toThrow();
  });

  it("never uses the border/icon-only accents as a text colour in the component styles", async () => {
    const css = (await import("./components.css?raw")).default as string;
    // Rules that set a text colour (`color:`), except those that style an icon, which is allowed to use the accents.
    const textRules = css.split("\n").filter((line) => /(^|[\s;{])color:/.test(line) && !/icon|chevron/.test(line.split("{")[0]));
    expect(textRules.length).toBeGreaterThan(5);
    for (const line of textRules) expect(line).not.toMatch(/--gold-accent|--err-accent/);
    // ...and the icon exceptions are exactly the two documented ones.
    const iconRules = css.split("\n").filter((line) => /color:\s*var\(--(gold|err)-accent\)/.test(line));
    expect(iconRules.map((line) => line.split("{")[0].trim())).toEqual([".sv-badge--gold .sv-badge__icon", ".sv-banner__icon"]);
  });
});
