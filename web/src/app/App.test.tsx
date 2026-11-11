import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { scoreMock } from "../mocks/score";
import { ThemeProvider } from "../ThemeProvider";
import { App } from "./App";

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new TypeError("offline")))); // no real network in tests
});
afterEach(() => {
  vi.unstubAllGlobals();
});

function renderAt(url: string) {
  return render(
    <ThemeProvider>
      <MemoryRouter initialEntries={[url]}>
        <App />
      </MemoryRouter>
    </ThemeProvider>,
  );
}

describe("routes", () => {
  it.each([
    ["/recruiter/upload", "Upload"],
    ["/recruiter/score", "Score"],
    ["/recruiter/questions", "Question review"],
    ["/student/question", "Take your time"],
  ])("%s renders its heading, and only one h1", (url, heading) => {
    renderAt(url);
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(screen.getByRole("heading", { level: 1, name: heading })).toBeInTheDocument();
    expect(screen.getByRole("main")).toBeInTheDocument();
    expect(screen.getByRole("banner")).toBeInTheDocument();
  });

  it("/design-system renders the Step A preview in recruiter mode", () => {
    renderAt("/design-system");
    expect(screen.getByRole("region", { name: "recruiter design system" })).toBeInTheDocument();
    expect(document.body).toHaveAttribute("data-mode", "recruiter");
  });

  it("/ redirects to the upload screen", () => {
    renderAt("/");
    expect(screen.getByRole("heading", { level: 1, name: "Upload" })).toBeInTheDocument();
  });

  it("an unknown route shows a calm 404 with a link back, and no step is marked current", () => {
    renderAt("/nowhere");
    expect(screen.getByRole("heading", { level: 1, name: "Page not found" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Go back to Upload" })).toHaveAttribute("href", "/recruiter/upload");
    const items = within(screen.getByRole("navigation", { name: "Progress" })).getAllByRole("listitem");
    expect(items.some((item) => item.hasAttribute("aria-current"))).toBe(false);
  });
});

describe("mode switching", () => {
  it("student routes set student mode, recruiter routes set recruiter mode", () => {
    const student = renderAt("/student/question");
    expect(document.body).toHaveAttribute("data-mode", "student");
    student.unmount();
    renderAt("/recruiter/score");
    expect(document.body).toHaveAttribute("data-mode", "recruiter");
  });

  it("the preview link switches to student mode, and Continue goes back to recruiter mode", async () => {
    const user = userEvent.setup();
    renderAt("/recruiter/upload");
    await user.click(screen.getByRole("link", { name: /Preview as student/ }));
    expect(document.body).toHaveAttribute("data-mode", "student");
    expect(document.body).toHaveClass("mode-fading");
    await user.click(screen.getByRole("button", { name: "Continue" }));
    expect(document.body).toHaveAttribute("data-mode", "recruiter");
  });
});

describe("student layout", () => {
  it("shows the caption and the timer, with no navigation", () => {
    renderAt("/student/question");
    expect(screen.getByText("Question 1 of 4")).toBeInTheDocument();
    expect(screen.getByRole("timer")).toHaveTextContent("42:10");
    expect(screen.queryByRole("navigation")).toBeNull();
  });

  it("?t=290 shows the low-time state: dot and 'left'", () => {
    renderAt("/student/question?t=290");
    expect(screen.getByRole("timer")).toHaveTextContent("4:50 left");
    expect(screen.getByTestId("timer-dot")).toBeInTheDocument();
  });

  it("counts down every second", () => {
    vi.useFakeTimers();
    try {
      renderAt("/student/question?t=290");
      act(() => vi.advanceTimersByTime(3000));
      expect(screen.getByRole("timer")).toHaveTextContent("4:47 left");
    } finally {
      vi.useRealTimers();
    }
  });

  it("ignores a bad ?t value", () => {
    renderAt("/student/question?t=abc");
    expect(screen.getByRole("timer")).toHaveTextContent("42:10");
  });
});

describe("recruiter layout", () => {
  it("shows the product name and marks the current step on each route", () => {
    const steps: [string, string][] = [["/recruiter/upload", "Upload"], ["/recruiter/score", "Score"], ["/recruiter/questions", "Question review"], ["/recruiter/score/abc", "Score"]];
    for (const [url, label] of steps) {
      const { unmount } = renderAt(url);
      expect(screen.getByText("SkillVerify")).toBeInTheDocument();
      const items = within(screen.getByRole("navigation", { name: "Progress" })).getAllByRole("listitem");
      expect(items).toHaveLength(3);
      const current = items.filter((item) => item.getAttribute("aria-current") === "step");
      expect(current).toHaveLength(1);
      expect(current[0]).toHaveTextContent(label);
      unmount();
    }
  });

  it("Continue moves through the placeholder steps", async () => {
    const user = userEvent.setup();
    renderAt("/recruiter/score");
    await user.click(screen.getByRole("button", { name: "Continue" }));
    expect(screen.getByRole("heading", { level: 1, name: "Question review" })).toBeInTheDocument();
  });
});

describe("accessibility", () => {
  it("has a skip link to the main landmark", () => {
    renderAt("/recruiter/upload");
    expect(screen.getByRole("link", { name: "Skip to content" })).toHaveAttribute("href", "#main");
    expect(screen.getByRole("main")).toHaveAttribute("id", "main");
  });

  it("moves focus to the h1 after a route change", async () => {
    const user = userEvent.setup();
    renderAt("/recruiter/score");
    await user.click(screen.getByRole("button", { name: "Continue" }));
    expect(screen.getByRole("heading", { level: 1, name: "Question review" })).toHaveFocus();
  });
});

describe("score mock and screen", () => {
  it("has every field of the scoring output that the app reads", () => {
    expect(Object.keys(scoreMock).sort()).toEqual(["cv_label", "flags", "github", "hidden_strengths", "job_name", "score", "skills", "status"]);
    expect(scoreMock.github.username).toBe("octocat");
    expect(Object.keys(scoreMock.score).sort()).toEqual(["checkable_only", "claim_coverage", "points_earned", "points_possible", "repo_fingerprint", "verifiable_share"]);
    expect(scoreMock.score).toMatchObject({ repo_fingerprint: 44.4, claim_coverage: 88.9, checkable_only: 46.2, verifiable_share: 72.2, points_earned: 8, points_possible: 18 });
    expect(scoreMock.skills.map((s) => s.skill)).toEqual(["Python", "SQL", "Tableau", "Pandas"]);
    expect(scoreMock.skills.map((s) => [s.tier, s.claimed, s.github_status, s.points, s.weight])).toEqual([
      ["must_have", true, "confirmed", 3, 3],
      ["must_have", true, "not_confirmed", 1.5, 3],
      ["nice_to_have", false, "not_detectable_from_github", 0.5, 1],
      ["must_have", true, "confirmed", 3, 3],
    ]);
    expect(scoreMock.skills.map((s) => s.skill)).toEqual(["Python", "SQL", "Tableau", "Pandas"]);
    for (const row of scoreMock.skills) expect(Object.keys(row).sort()).toEqual(["claimed", "github_status", "points", "skill", "tier", "weight"]);
  });

  it("the score screen shows the mock as plain text", () => {
    renderAt("/recruiter/score");
    expect(screen.getByText(/Priya Sample for Data Analyst/)).toBeInTheDocument();
    expect(screen.getByText("Repo Fingerprint Score: 44.4")).toBeInTheDocument();
    expect(screen.getByText("Points: 8 of 18")).toBeInTheDocument();
  });

  it("uses no verdict words anywhere", () => {
    for (const url of ["/recruiter/upload", "/recruiter/score", "/recruiter/questions", "/student/question", "/nowhere"]) {
      const { container, unmount } = renderAt(url);
      expect(container.textContent).not.toMatch(/\b(hire|reject|pass|fail|strong candidate|weak candidate)/i);
      unmount();
    }
  });
});
