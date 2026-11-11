import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import tokensCss from "./styles/tokens.css?raw";
import { FADE_MS, ThemeProvider, useTheme } from "./ThemeProvider";

function Switcher() {
  const { mode, setMode } = useTheme();
  return (
    <>
      <span data-testid="mode">{mode}</span>
      <button onClick={() => setMode("student")}>student</button>
      <button onClick={() => setMode("recruiter")}>recruiter</button>
    </>
  );
}

describe("ThemeProvider", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    document.body.removeAttribute("data-mode");
    document.body.className = "";
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("sets data-mode on <body> (recruiter by default) without starting a fade", () => {
    render(<ThemeProvider><Switcher /></ThemeProvider>);
    expect(document.body).toHaveAttribute("data-mode", "recruiter");
    expect(document.body).not.toHaveClass("mode-fading");
  });

  it("starts in the given mode", () => {
    render(<ThemeProvider initialMode="student"><Switcher /></ThemeProvider>);
    expect(document.body).toHaveAttribute("data-mode", "student");
    expect(screen.getByTestId("mode")).toHaveTextContent("student");
  });

  it("switching mode changes data-mode and fades for 1200 ms", () => {
    render(<ThemeProvider><Switcher /></ThemeProvider>);
    act(() => screen.getByText("student").click());
    expect(document.body).toHaveAttribute("data-mode", "student");
    expect(document.body).toHaveClass("mode-fading");
    act(() => vi.advanceTimersByTime(FADE_MS - 1));
    expect(document.body).toHaveClass("mode-fading");
    act(() => vi.advanceTimersByTime(200));
    expect(document.body).not.toHaveClass("mode-fading");
    act(() => screen.getByText("recruiter").click());
    expect(document.body).toHaveAttribute("data-mode", "recruiter");
  });

  it("FADE_MS matches the 1200 ms written in tokens.css", () => {
    expect(FADE_MS).toBe(1200);
    expect(tokensCss).toContain("1200ms ease-in-out");
  });

  it("useTheme outside a provider is an error", () => {
    const quiet = vi.spyOn(console, "error").mockImplementation(() => undefined);
    expect(() => render(<Switcher />)).toThrow(/ThemeProvider/);
    quiet.mockRestore();
  });
});
