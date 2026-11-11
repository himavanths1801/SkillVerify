import tokensCss from "../styles/tokens.css?raw";

export type Mode = "recruiter" | "student";
export const MODES: Mode[] = ["recruiter", "student"];

/** The variables every mode must define (the locked list). */
export const REQUIRED_VARIABLES = [
  "--page", "--surface", "--divider", "--fill", "--text", "--text-2", "--accent", "--accent-pressed",
  "--accent-wash", "--on-accent-wash", "--gold-wash", "--gold-accent", "--gold-text", "--err-wash",
  "--err-accent", "--err-text", "--input-border", "--toggle-off", "--disabled", "--radius-card",
  "--radius-control", "--pad-card", "--gap", "--control-h", "--badge-h", "--shadow-card",
] as const;

/** Parse `[data-mode="x"] { --name: value; }` blocks of a CSS string into { mode: { --name: value } }. */
export function parseTokens(css: string): Record<string, Record<string, string>> {
  const withoutComments = css.replace(/\/\*[\s\S]*?\*\//g, "");
  const result: Record<string, Record<string, string>> = {};
  const block = /\[data-mode="([a-z]+)"\]\s*\{([^}]*)\}/g;
  for (const match of withoutComments.matchAll(block)) {
    const variables: Record<string, string> = {};
    for (const declaration of match[2].matchAll(/(--[a-z0-9-]+)\s*:\s*([^;]+);/g)) {
      variables[declaration[1]] = declaration[2].trim().replace(/\s+/g, " ");
    }
    result[match[1]] = { ...(result[match[1]] ?? {}), ...variables };
  }
  return result;
}

/** The tokens of the real tokens.css file: the single source of truth, also used by the preview page. */
export const tokens = parseTokens(tokensCss);
export const tokensSource = tokensCss;
