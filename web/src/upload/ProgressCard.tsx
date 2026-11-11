import { forwardRef } from "react";
import { Card } from "../components/Card";
import { Icon } from "../components/Icon";

/** The five steps of the analysis, in the exact words the API sends as `stage`. */
export const STAGES = [
  "Reading the job description",
  "Reading the CV",
  "Checking GitHub",
  "Scanning repositories",
  "Scoring",
] as const;

/** Index of the step in progress: -1 while queued (or for a stage we do not know), STAGES.length when Done. */
export function stageIndex(stage: string, state: string): number {
  if (state === "queued") return -1;
  if (stage === "Done") return STAGES.length;
  return (STAGES as readonly string[]).indexOf(stage);
}

type Props = { stage: string; state: string };

export const ProgressCard = forwardRef<HTMLHeadingElement, Props>(function ProgressCard({ stage, state }, ref) {
  const current = stageIndex(stage, state);
  return (
    <Card className="progress-card">
      <h2 ref={ref} tabIndex={-1} className="t-title">Analyzing, this can take a moment.</h2>
      <ol className="progress-list" aria-label="Progress of the analysis">
        {STAGES.map((label, index) => {
          const status = index < current ? "done" : index === current ? "current" : "pending";
          return (
            <li key={label} className={`progress-step progress-step--${status}`} aria-current={status === "current" ? "step" : undefined}>
              <span className="progress-step__mark" aria-hidden="true">
                {status === "done" ? <Icon name="check" size={14} /> : null}
              </span>
              <span className="t-body">{label}</span>
              {status === "done" ? <span className="sr-only"> (done)</span> : null}
            </li>
          );
        })}
      </ol>
    </Card>
  );
});
