import type { Ref } from "react";
import { Button } from "../components/Button";
import { Card } from "../components/Card";

type HeaderProps = { name: string; jobTitle: string; score: string; counter: string };

/** The header card: who, which job, the score and the counter (an aria-live region, so changes are announced). */
export function Header({ name, jobTitle, score, counter }: HeaderProps) {
  return (
    <Card className="review__head">
      <div>
        <h1 className="t-heading" tabIndex={-1}>{name}</h1>
        <div className="review__who t-body">
          <span>{jobTitle}</span>
          <span className="review__score"><span className="t-label">Score</span> <span className="t-title">{score}</span></span>
        </div>
      </div>
      <p className="review__counter t-title" aria-live="polite">{counter}</p>
    </Card>
  );
}

type BarProps = {
  previewDisabled: boolean;
  createDisabled: boolean;
  onPreview?: () => void;
  onCreate?: () => void;
  previewRef?: Ref<HTMLButtonElement>;
};

export function BottomBar({ previewDisabled, createDisabled, onPreview, onCreate, previewRef }: BarProps) {
  return (
    <div className="review__bar">
      <Button variant="secondary" ref={previewRef} disabled={previewDisabled} onClick={onPreview}>Preview as student</Button>
      <Button disabled={createDisabled} onClick={onCreate}>Create Assessment</Button>
    </div>
  );
}
