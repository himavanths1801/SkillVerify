import { useId, useState } from "react";
import type { SkippedSkill } from "../api/types";
import { Card } from "../components/Card";
import { Icon } from "../components/Icon";
import { groupSkipped } from "./format";

/** Skills that got no question, grouped by why. Collapsed by default; hidden when there are none. */
export function SkillsCard({ skipped }: { skipped: SkippedSkill[] }) {
  const [open, setOpen] = useState(false);
  const panelId = useId();
  if (skipped.length === 0) return null;
  return (
    <Card className="skills-without">
      <button type="button" className="skills-without__head t-title" aria-expanded={open} aria-controls={panelId} onClick={() => setOpen(!open)}>
        <span>Skills without a question ({skipped.length})</span>
        <Icon name="chevron-down" size={20} className={`skills-without__chevron${open ? " is-open" : ""}`} />
      </button>
      {open ? (
        <div id={panelId} className="skills-without__body">
          {groupSkipped(skipped).map((group) => (
            <div key={group.label} className="skills-without__group">
              <p className="t-body skills-without__label">{group.label}</p>
              <ul className="chips">
                {group.skills.map((skill) => <li key={skill} className="chip t-caption">{skill}</li>)}
              </ul>
            </div>
          ))}
        </div>
      ) : null}
    </Card>
  );
}
