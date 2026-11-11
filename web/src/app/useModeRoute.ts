import { useLayoutEffect } from "react";
import { useTheme } from "../ThemeProvider";
import type { Mode } from "../lib/tokens";

/** Sets the colour mode while the calling route is on screen. The fade itself lives in ThemeProvider. */
export function useModeRoute(mode: Mode) {
  const { setMode } = useTheme();
  useLayoutEffect(() => setMode(mode), [mode, setMode]);
}
