import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { tokens } from "../lib/tokens";
import { DesignSystemPreview } from "./DesignSystemPreview";

describe("DesignSystemPreview", () => {
  it("shows both modes at once, each in its own data-mode wrapper", () => {
    const { container } = render(<DesignSystemPreview />);
    const panels = container.querySelectorAll("section[data-mode]");
    expect([...panels].map((p) => p.getAttribute("data-mode"))).toEqual(["recruiter", "student"]);
  });

  it("uses the sample text of the design sheet", () => {
    render(<DesignSystemPreview />);
    const recruiter = screen.getByRole("region", { name: "recruiter design system" });
    const student = screen.getByRole("region", { name: "student design system" });
    expect(within(recruiter).getAllByText("82").length).toBeGreaterThan(0);
    expect(within(recruiter).getAllByText("Candidate evidence").length).toBeGreaterThan(0);
    expect(within(recruiter).getAllByText("Python skill claims").length).toBeGreaterThan(0);
    expect(within(student).getAllByText("Take your time").length).toBeGreaterThan(0);
    expect(within(student).getAllByText("How would you explain what this function returns?").length).toBeGreaterThan(0);
    expect(within(student).getAllByText("Question 2 of 4").length).toBeGreaterThan(0);
  });

  it("lists every colour with its real hex value from tokens.css", () => {
    render(<DesignSystemPreview />);
    for (const mode of ["recruiter", "student"] as const) {
      const panel = screen.getByRole("region", { name: `${mode} design system` });
      for (const variable of ["--accent", "--accent-pressed", "--gold-text", "--err-text", "--page"]) {
        expect(within(panel).getAllByText(tokens[mode][variable]).length).toBeGreaterThan(0);
      }
    }
  });

  it("shows every component state, the timers only in the student panel, and the shape tokens", () => {
    render(<DesignSystemPreview />);
    const recruiter = screen.getByRole("region", { name: "recruiter design system" });
    const student = screen.getByRole("region", { name: "student design system" });
    for (const panel of [recruiter, student]) {
      for (const state of ["Default", "Hover", "Pressed", "Disabled", "Focus-visible"]) {
        expect(within(panel).getAllByRole("button", { name: state }).length).toBe(2); // primary row and secondary row
      }
      for (const text of ["Found", "Needs a look", "Not checked", "Ready", "Not started"]) {
        expect(within(panel).getByText(text)).toBeInTheDocument();
      }
      expect(within(panel).getByRole("alert")).toBeInTheDocument();
      expect(within(panel).getByRole("button", { name: "Retry" })).toBeInTheDocument();
      expect(within(panel).getAllByRole("switch")).toHaveLength(2);
      expect(within(panel).getByText("cv_priya.pdf")).toBeInTheDocument();
      expect(within(panel).getByText(/Card radius/)).toBeInTheDocument();
    }
    expect(within(recruiter).queryByRole("timer")).toBeNull();
    const timers = within(student).getAllByRole("timer");
    expect(timers.map((t) => t.textContent)).toEqual(["42:10", "4:59 left"]);
  });

  it("the type scale lists the exact size/line-height/weight of each mode", () => {
    render(<DesignSystemPreview />);
    const recruiter = screen.getByRole("region", { name: "recruiter design system" });
    const student = screen.getByRole("region", { name: "student design system" });
    expect(within(recruiter).getByText("Score 56/56/600")).toBeInTheDocument();
    expect(within(recruiter).getByText("Caps label 12/16/600")).toBeInTheDocument();
    expect(within(student).getByText("Heading 32/44/300")).toBeInTheDocument();
    expect(within(student).getByText("Question 24/34/400")).toBeInTheDocument();
    expect(within(student).getByText("Timer 14/20/500")).toBeInTheDocument();
  });
});
