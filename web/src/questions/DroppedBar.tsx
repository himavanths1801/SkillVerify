import { useEffect, useRef } from "react";
import { Button } from "../components/Button";
import type { FocusRequest } from "./QuestionCard";

type Props = {
  id: string;
  number: number;
  focus: FocusRequest;
  /** This bar's replacement is being written. */
  writing: boolean;
  /** Another replacement is running: this bar's button waits. */
  waiting: boolean;
  /** A quiet note or an error line under the buttons. */
  note: { kind: "quiet" | "error"; text: string } | null;
  onRestore: () => void;
  onReplace: () => void;
};

/** The muted bar a dropped question turns into. */
export function DroppedBar({ id, number, focus, writing, waiting, note, onRestore, onReplace }: Props) {
  const restoreRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (focus && focus.id === id && focus.target === "restore") restoreRef.current?.focus();
  }, [focus, id]);
  return (
    <div className="dropped" role="group" aria-label={`Question ${number} dropped`}>
      <div className="dropped__row">
        <div>
          <div className="t-label dropped__no">Question {number}</div>
          <div className="t-body dropped__text">Question dropped</div>
        </div>
        <div className="dropped__actions">
          <button type="button" ref={restoreRef} className="text-btn text-btn--link t-body" onClick={onRestore}>Approve again</button>
          <Button variant="secondary" disabled={writing || waiting} aria-busy={writing} onClick={onReplace}>
            {writing ? (
              <>
                <span className="spinner spinner--small" aria-hidden="true" />
                Writing...
              </>
            ) : (
              "Generate replacement"
            )}
          </Button>
        </div>
      </div>
      {note ? (
        <p className={`dropped__note t-caption${note.kind === "error" ? " dropped__note--error" : ""}`} role={note.kind === "error" ? "alert" : "status"}>
          {note.text}
        </p>
      ) : null}
    </div>
  );
}
