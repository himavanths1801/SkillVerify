import { createContext, useContext, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import type { Mode } from "./lib/tokens";

/** How long a mode change fades colours (matches the 1200 ms in tokens.css). */
export const FADE_MS = 1200;

type ThemeContextValue = { mode: Mode; setMode: (mode: Mode) => void };
const ThemeContext = createContext<ThemeContextValue | null>(null);

/**
 * Sets data-mode on <body>, which switches every token at once. A change of mode (not the first render) also adds
 * .mode-fading to <body> for the length of the fade, so colours transition over 1200 ms; tokens.css turns the
 * transition off under prefers-reduced-motion.
 */
export function ThemeProvider({ initialMode = "recruiter", children }: { initialMode?: Mode; children: ReactNode }) {
  const [mode, setMode] = useState<Mode>(initialMode);
  const first = useRef(true);

  useLayoutEffect(() => {
    document.body.setAttribute("data-mode", mode);
    if (first.current) {
      first.current = false;
      return;
    }
    document.body.classList.add("mode-fading");
    const timer = setTimeout(() => document.body.classList.remove("mode-fading"), FADE_MS + 100);
    return () => clearTimeout(timer);
  }, [mode]);

  const value = useMemo(() => ({ mode, setMode }), [mode]);
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeContextValue {
  const context = useContext(ThemeContext);
  if (!context) throw new Error("useTheme must be used inside a ThemeProvider");
  return context;
}
