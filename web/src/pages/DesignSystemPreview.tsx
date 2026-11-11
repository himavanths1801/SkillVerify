import { useState } from "react";
import type { ReactNode } from "react";
import { Banner } from "../components/Banner";
import { Button } from "../components/Button";
import { Card } from "../components/Card";
import { FileChip } from "../components/FileChip";
import { Input } from "../components/Input";
import { Select } from "../components/Select";
import { StatusBadge } from "../components/StatusBadge";
import type { BadgeVariant } from "../components/StatusBadge";
import { Timer } from "../components/Timer";
import { Toggle } from "../components/Toggle";
import { tokens } from "../lib/tokens";
import type { Mode } from "../lib/tokens";

/** The colour palette rows: [variable, name in the recruiter panel, name in the student panel]. */
const PALETTE: [string, string, string][] = [
  ["--page", "Page", "Page"],
  ["--surface", "Card", "Panel"],
  ["--divider", "Divider", "Line"],
  ["--fill", "Neutral fill", "Quiet fill"],
  ["--text", "Text", "Text"],
  ["--text-2", "Text 2", "Text 2"],
  ["--accent", "Accent", "Accent (lagoon)"],
  ["--accent-pressed", "Accent pressed", "Accent pressed"],
  ["--accent-wash", "Accent wash", "Accent wash"],
  ["--on-accent-wash", "On accent wash", "On accent wash"],
  ["--gold-wash", "Gold wash", "Gold wash"],
  ["--gold-accent", "Gold accent", "Gold accent"],
  ["--gold-text", "Gold text", "Gold text"],
  ["--err-wash", "Error wash", "Rose wash"],
  ["--err-accent", "Error accent", "Error accent"],
  ["--err-text", "Error text", "Error text"],
  ["--input-border", "Input border", "Input border"],
  ["--toggle-off", "Toggle off", "Toggle off"],
  ["--disabled", "Disabled", "Disabled"],
];

/** The type rows of each mode: [class, label, token prefix]. */
const TYPE_ROWS: Record<Mode, [string, string, string][]> = {
  recruiter: [
    ["t-score", "Score", "--t-score"],
    ["t-heading", "Heading", "--t-heading"],
    ["t-title", "Title", "--t-title"],
    ["t-body", "Body", "--t-body"],
    ["t-label", "Caps label", "--t-label"],
    ["t-code", "Code", "--t-code"],
  ],
  student: [
    ["t-heading", "Heading", "--t-heading"],
    ["t-question", "Question", "--t-question"],
    ["t-body", "Body", "--t-body"],
    ["t-caption", "Caption", "--t-caption"],
    ["t-timer", "Timer", "--t-timer"],
    ["t-code", "Code", "--t-code"],
  ],
};

const BADGES: BadgeVariant[] = ["found", "gold", "neutral", "ready", "notstarted"];

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <Card className="pv-section">
      <h3 className="t-label pv-section__title">{title}</h3>
      {children}
    </Card>
  );
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="pv-row">
      <span className="t-caption pv-row__label">{label}</span>
      <div className="pv-row__content">{children}</div>
    </div>
  );
}

function sampleFor(mode: Mode, cls: string): ReactNode {
  const samples: Record<Mode, Record<string, string>> = {
    recruiter: {
      "t-score": "82", "t-heading": "Candidate evidence", "t-title": "Python skill claims",
      "t-body": "Claimed on the CV and confirmed in 3 repositories.", "t-label": "Skill claims", "t-code": "analysis.py",
    },
    student: {
      "t-heading": "Take your time", "t-question": "How would you explain what this function returns?",
      "t-body": "Explain it the way you would to a classmate.", "t-caption": "Question 2 of 4", "t-timer": "42:10",
      "t-code": "def total(items):",
    },
  };
  return samples[mode][cls];
}

function Panel({ mode }: { mode: Mode }) {
  const t = tokens[mode];
  const [on, setOn] = useState(true);
  const [off, setOff] = useState(false);
  const [email, setEmail] = useState("");
  const isStudent = mode === "student";

  return (
    <section className="pv-panel" data-mode={mode} aria-label={`${mode} design system`}>
      <header className="pv-panel__head">
        <p className="t-label pv-eyebrow">{isStudent ? "Student mode" : "Recruiter mode"}</p>
        {isStudent ? (
          <>
            <h2 className="t-heading">Take your time</h2>
            <p className="t-caption pv-muted">Question 2 of 4</p>
          </>
        ) : (
          <>
            <div className="t-score">82</div>
            <h2 className="t-heading">Candidate evidence</h2>
            <p className="t-body pv-muted">Python skill claims</p>
          </>
        )}
      </header>

      <Section title="Colour palette">
        <div className="pv-swatches">
          {PALETTE.map(([variable, recruiterName, studentName]) => (
            <div className="pv-swatch" key={variable}>
              <span className="pv-swatch__chip" style={{ background: `var(${variable})` }} />
              <span className="t-caption pv-swatch__name">{isStudent ? studentName : recruiterName}</span>
              <code className="t-code pv-swatch__hex">{t[variable]}</code>
            </div>
          ))}
        </div>
      </Section>

      <Section title="Type scale">
        {TYPE_ROWS[mode].map(([cls, label, prefix]) => (
          <div className="pv-type" key={cls}>
            <span className="t-caption pv-type__spec">
              {label} {t[`${prefix}-size`].replace("px", "")}/{t[`${prefix}-lh`].replace("px", "")}
              {t[`${prefix}-weight`] ? `/${t[`${prefix}-weight`]}` : ""}
            </span>
            <span className={cls}>{sampleFor(mode, cls)}</span>
          </div>
        ))}
      </Section>

      <Section title="Buttons">
        <Row label="Primary">
          <Button>Default</Button>
          <Button forceState="hover">Hover</Button>
          <Button forceState="pressed">Pressed</Button>
          <Button disabled>Disabled</Button>
          <Button forceState="focus">Focus-visible</Button>
        </Row>
        <Row label="Secondary">
          <Button variant="secondary">Default</Button>
          <Button variant="secondary" forceState="hover">Hover</Button>
          <Button variant="secondary" forceState="pressed">Pressed</Button>
          <Button variant="secondary" disabled>Disabled</Button>
          <Button variant="secondary" forceState="focus">Focus-visible</Button>
        </Row>
      </Section>

      <Section title="Fields">
        <div className="pv-grid">
          <Input label={isStudent ? "Your name" : "Candidate name"} placeholder="Type here" helper="Shown on the report." />
          <Input label="Focused" defaultValue="priya.sample" forceFocus helper="This is the focused look." />
          <Input label="Email" value={email} onChange={(event) => setEmail(event.target.value)} placeholder="name@example.com" />
          <Select label="Job description" defaultValue="backend" helper="Pick one.">
            <option value="backend">Backend engineer</option>
            <option value="data">Data analyst</option>
          </Select>
        </div>
        <Row label="File chips">
          <FileChip name="cv_priya.pdf" />
          <FileChip name="backend_jd.txt" onRemove={() => undefined} />
        </Row>
        <Row label="Toggles">
          <Toggle checked={on} onChange={setOn} label="On" />
          <Toggle checked={off} onChange={setOff} label="Off" />
        </Row>
      </Section>

      <Section title="Status badges">
        <Row label="Variants">
          {BADGES.map((variant) => (
            <StatusBadge key={variant} variant={variant} />
          ))}
        </Row>
      </Section>

      <Section title="Cards">
        <Card>
          {isStudent ? (
            <>
              <p className="t-question">How would you explain what this function returns?</p>
              <p className="t-caption pv-muted">Question 2 of 4</p>
            </>
          ) : (
            <>
              <div className="t-score">82</div>
              <p className="t-title">Candidate evidence</p>
              <p className="t-body pv-muted">Python skill claims</p>
            </>
          )}
        </Card>
      </Section>

      <Section title="Error and time">
        <Banner
          title="We could not read this file"
          message="The file looks empty or damaged. Try another copy."
          onRetry={() => undefined}
        />
        {isStudent ? (
          <Row label="Timer">
            <Timer secondsLeft={2530} />
            <Timer secondsLeft={299} />
          </Row>
        ) : null}
      </Section>

      <Section title="Shape tokens">
        <div className="pv-shapes">
          <div className="pv-shape" style={{ borderRadius: "var(--radius-card)" }}>
            <span className="t-caption">Card radius {t["--radius-card"]}</span>
          </div>
          <div className="pv-shape" style={{ borderRadius: "var(--radius-control)" }}>
            <span className="t-caption">Control radius {t["--radius-control"]}</span>
          </div>
        </div>
        <ul className="t-caption pv-facts">
          <li>Card padding {t["--pad-card"]}</li>
          <li>Gap {t["--gap"]}</li>
          <li>Control height {t["--control-h"]}</li>
          <li>Badge height {t["--badge-h"]}</li>
          <li>Input border {t["--input-border-w"]} {t["--input-border"]}</li>
        </ul>
        <div className="pv-shadow">
          <Card>
            <span className="t-caption">Card shadow</span>
          </Card>
          <code className="t-code pv-shadow__value">{t["--shadow-card"]}</code>
        </div>
      </Section>
    </section>
  );
}

export function DesignSystemPreview() {
  return (
    <main className="pv-page">
      <Panel mode="recruiter" />
      <Panel mode="student" />
    </main>
  );
}
