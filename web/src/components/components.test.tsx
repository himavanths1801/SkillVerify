import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import componentsCss from "../styles/components.css?raw";
import { Banner } from "./Banner";
import { Button } from "./Button";
import { Card } from "./Card";
import { FileChip } from "./FileChip";
import { Icon } from "./Icon";
import { Input } from "./Input";
import { Select } from "./Select";
import { StatusBadge } from "./StatusBadge";
import type { BadgeVariant } from "./StatusBadge";
import { LOW_TIME_SECONDS, Timer, formatTime } from "./Timer";
import { Toggle } from "./Toggle";

describe("Button", () => {
  it("fires onClick when enabled", async () => {
    const onClick = vi.fn();
    render(<Button onClick={onClick}>Save</Button>);
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it("does NOT fire onClick when disabled (mouse or keyboard)", async () => {
    const onClick = vi.fn();
    render(<Button disabled onClick={onClick}>Save</Button>);
    const button = screen.getByRole("button", { name: "Save" });
    expect(button).toBeDisabled();
    await userEvent.click(button);
    button.focus();
    await userEvent.keyboard("{Enter}{ }");
    expect(onClick).not.toHaveBeenCalled();
  });

  it("has primary and secondary variants and defaults to type=button", () => {
    render(<><Button>One</Button><Button variant="secondary">Two</Button></>);
    expect(screen.getByText("One")).toHaveClass("sv-btn--primary");
    expect(screen.getByText("Two")).toHaveClass("sv-btn--secondary");
    expect(screen.getByText("One")).toHaveAttribute("type", "button");
  });

  it("can show a forced preview state without a pointer", () => {
    render(<Button forceState="hover">Hover</Button>);
    expect(screen.getByText("Hover")).toHaveAttribute("data-force", "hover");
  });
});

describe("Toggle", () => {
  function Harness({ start = false }: { start?: boolean }) {
    const [on, setOn] = useState(start);
    return <Toggle checked={on} onChange={setOn} label="Show scores" />;
  }

  it("toggles with Space and with Enter from the keyboard", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const toggle = screen.getByRole("switch", { name: "Show scores" });
    expect(toggle).toHaveAttribute("aria-checked", "false");
    await user.tab();
    expect(toggle).toHaveFocus();
    await user.keyboard(" ");
    expect(toggle).toHaveAttribute("aria-checked", "true");
    await user.keyboard("{Enter}");
    expect(toggle).toHaveAttribute("aria-checked", "false");
  });

  it("toggles on click and reports the new value", async () => {
    const onChange = vi.fn();
    render(<Toggle checked={true} onChange={onChange} label="On" />);
    await userEvent.click(screen.getByRole("switch"));
    expect(onChange).toHaveBeenCalledWith(false);
  });

  it("does nothing when disabled", async () => {
    const onChange = vi.fn();
    render(<Toggle checked={false} onChange={onChange} label="Off" disabled />);
    await userEvent.click(screen.getByRole("switch"));
    expect(onChange).not.toHaveBeenCalled();
  });
});

describe("StatusBadge", () => {
  const cases: [BadgeVariant, string][] = [
    ["found", "Found"], ["gold", "Needs a look"], ["neutral", "Not checked"], ["ready", "Ready"], ["notstarted", "Not started"],
  ];

  it.each(cases)("%s always renders an icon AND its text", (variant, text) => {
    const { container } = render(<StatusBadge variant={variant} />);
    expect(screen.getByText(text)).toBeInTheDocument();
    expect(container.querySelector("svg")).not.toBeNull();
    expect(container.querySelector("svg")).toHaveAttribute("aria-hidden", "true"); // the text carries the meaning
    expect(container.firstElementChild).toHaveClass(`sv-badge--${variant}`);
  });

  it("uses the eye icon for the gold badge and lets the text be overridden", () => {
    const { container } = render(<StatusBadge variant="gold">Check this</StatusBadge>);
    expect(container.querySelector('[data-icon="eye"]')).not.toBeNull();
    expect(screen.getByText("Check this")).toBeInTheDocument();
  });
});

describe("Banner", () => {
  it("is an alert with an icon, title and message", () => {
    const { container } = render(<Banner title="Could not read the file" message="Try another copy." />);
    const alert = screen.getByRole("alert");
    expect(within(alert).getByText("Could not read the file")).toBeInTheDocument();
    expect(within(alert).getByText("Try another copy.")).toBeInTheDocument();
    expect(container.querySelector('[data-icon="alert"]')).not.toBeNull();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });

  it("fires Retry", async () => {
    const onRetry = vi.fn();
    render(<Banner title="Failed" onRetry={onRetry} />);
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });
});

describe("Timer", () => {
  it("exports the 300 second threshold from one place", () => {
    expect(LOW_TIME_SECONDS).toBe(300);
  });

  it.each([300, 299, 60, 0])("at %i seconds shows the dot and the word 'left'", (seconds) => {
    render(<Timer secondsLeft={seconds} />);
    expect(screen.getByTestId("timer-dot")).toBeInTheDocument();
    expect(screen.getByRole("timer")).toHaveTextContent("left");
    expect(screen.getByRole("timer")).toHaveClass("sv-timer--low");
  });

  it.each([301, 600, 2530])("at %i seconds shows neither the dot nor 'left', but a clock icon", (seconds) => {
    const { container } = render(<Timer secondsLeft={seconds} />);
    expect(screen.queryByTestId("timer-dot")).toBeNull();
    expect(screen.getByRole("timer")).not.toHaveTextContent("left");
    expect(container.querySelector('[data-icon="clock"]')).not.toBeNull();
  });

  it("formats the time like the design sheet", () => {
    const { rerender } = render(<Timer secondsLeft={2530} />);
    expect(screen.getByRole("timer")).toHaveTextContent("42:10");
    rerender(<Timer secondsLeft={299} />);
    expect(screen.getByRole("timer")).toHaveTextContent("4:59 left");
    expect(formatTime(5)).toBe("0:05");
    expect(formatTime(-3)).toBe("0:00");
    expect(formatTime(61.9)).toBe("1:01");
  });

  it("keeps the pulse slow and stops it under reduced motion, with no other animation of the dot", () => {
    const dotRule = /\.sv-timer__dot\s*\{[^}]*\}/.exec(componentsCss)![0];
    expect(dotRule).toMatch(/animation:\s*sv-timer-pulse 1\.8s ease-in-out infinite/);
    const keyframes = /@keyframes sv-timer-pulse\s*\{[\s\S]*?\n\}/.exec(componentsCss)![0];
    expect(keyframes).toMatch(/opacity:\s*1/);
    expect(keyframes).toMatch(/opacity:\s*\.4/);
    const start = componentsCss.indexOf("@media (prefers-reduced-motion: reduce)");
    expect(start).toBeGreaterThan(-1);
    const block = componentsCss.slice(start);
    expect(block).toMatch(/\.sv-timer__dot\s*\{\s*animation:\s*none;\s*\}/);
    const animations = [...block.matchAll(/animation:\s*([^;}]+)/g)].map((match) => match[1].trim());
    expect(animations).toEqual(["none"]); // the only animation declaration under reduced motion is `none`
    expect(block).not.toContain("sv-timer-pulse");
  });
});

describe("Input, Select, FileChip, Card", () => {
  it("Input connects its label and helper text", () => {
    render(<Input label="Candidate name" helper="Shown on the report." />);
    const input = screen.getByLabelText("Candidate name");
    expect(input).toHaveAccessibleDescription("Shown on the report.");
  });

  it("Input works without helper text and can show the focused look", async () => {
    render(<Input label="Email" forceFocus />);
    const input = screen.getByLabelText("Email");
    expect(input).not.toHaveAttribute("aria-describedby");
    expect(input).toHaveAttribute("data-force", "focus");
    await userEvent.type(input, "a@example.com");
    expect(input).toHaveValue("a@example.com");
  });

  it("Select is a labelled native select with options", async () => {
    render(
      <Select label="Job description" helper="Pick one." defaultValue="a">
        <option value="a">Alpha</option>
        <option value="b">Beta</option>
      </Select>,
    );
    const select = screen.getByLabelText("Job description");
    expect(select).toHaveAccessibleDescription("Pick one.");
    await userEvent.selectOptions(select, "b");
    expect(select).toHaveValue("b");
  });

  it("Select works without helper text", () => {
    render(<Select label="Mode" forceFocus><option>One</option></Select>);
    expect(screen.getByLabelText("Mode")).not.toHaveAttribute("aria-describedby");
  });

  it("FileChip shows the name with an icon, and the remove button only when asked", async () => {
    const onRemove = vi.fn();
    const { rerender, container } = render(<FileChip name="cv_priya.pdf" />);
    expect(screen.getByText("cv_priya.pdf")).toBeInTheDocument();
    expect(container.querySelector('[data-icon="file"]')).not.toBeNull();
    expect(screen.queryByRole("button")).toBeNull();
    expect(container.firstElementChild).toHaveClass("t-code"); // the mono font
    rerender(<FileChip name="cv_priya.pdf" onRemove={onRemove} />);
    await userEvent.click(screen.getByRole("button", { name: "Remove cv_priya.pdf" }));
    expect(onRemove).toHaveBeenCalledTimes(1);
  });

  it("Card renders its children and merges classes", () => {
    render(<Card className="extra">Inside</Card>);
    expect(screen.getByText("Inside")).toHaveClass("sv-card", "extra");
  });
});

describe("accessibility rules", () => {
  it("every icon is decorative (aria-hidden) and every icon-only control has an aria-label", () => {
    const { container } = render(
      <>
        <Icon name="check" />
        <FileChip name="a.pdf" onRemove={() => undefined} />
        <Banner title="x" onRetry={() => undefined} />
      </>,
    );
    container.querySelectorAll("svg").forEach((svg) => expect(svg).toHaveAttribute("aria-hidden", "true"));
    container.querySelectorAll("button").forEach((button) => {
      const named = (button.textContent ?? "").trim() !== "" || button.hasAttribute("aria-label");
      expect(named).toBe(true);
    });
  });

  it("the focus-visible ring is defined once for every interactive element", () => {
    for (const selector of [".sv-btn:focus-visible", ".sv-input:focus-visible", ".sv-select:focus-visible", ".sv-toggle:focus-visible", ".sv-chip__remove:focus-visible"]) {
      expect(componentsCss).toContain(selector);
    }
    expect(componentsCss).toMatch(/outline:\s*2px solid var\(--accent\)/);
  });

  it("reaches interactive controls with the keyboard in order", async () => {
    const user = userEvent.setup();
    act(() => undefined);
    render(<><Button>First</Button><Toggle checked={false} onChange={() => undefined} label="Second" /><Input label="Third" /></>);
    await user.tab();
    expect(screen.getByText("First")).toHaveFocus();
    await user.tab();
    expect(screen.getByRole("switch")).toHaveFocus();
    await user.tab();
    expect(screen.getByLabelText("Third")).toHaveFocus();
  });
});
