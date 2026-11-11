import { useEffect, useId, useRef } from "react";
import type { KeyboardEvent } from "react";
import { Button } from "../components/Button";
import { RichText } from "./RichText";
import type { ReviewItem } from "./storage";
import { languageLabel } from "./format";

const FOCUSABLE = 'a[href], button:not([disabled]), textarea, input, select, [tabindex]:not([tabindex="-1"])';

type Props = { items: ReviewItem[]; onClose: () => void };

/**
 * What the student will see: the approved questions in order, in the student theme (applied to the panel only).
 * Never shows Why this question, the Edited pill, reference notes or any recruiter button.
 * Focus is kept inside; Escape and a click on the dim area close it.
 */
export function PreviewModal({ items, onClose }: Props) {
  const titleId = useId();
  const panelRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    closeRef.current?.focus();
  }, []);

  const keys = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") {
      event.stopPropagation();
      onClose();
      return;
    }
    if (event.key !== "Tab") return;
    const nodes = Array.from(panelRef.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? []);
    if (nodes.length === 0) return;
    const first = nodes[0];
    const last = nodes[nodes.length - 1];
    const active = document.activeElement;
    if (event.shiftKey && (active === first || !panelRef.current?.contains(active))) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && (active === last || !panelRef.current?.contains(active))) {
      event.preventDefault();
      first.focus();
    }
  };

  return (
    <div className="modal-dim" data-testid="modal-dim" onClick={(event) => { if (event.target === event.currentTarget) onClose(); }} onKeyDown={keys}>
      <div ref={panelRef} className="modal-panel" data-mode="student" role="dialog" aria-modal="true" aria-labelledby={titleId}>
        <h2 id={titleId} className="modal-panel__title">What the student will see</h2>
        <p className="modal-panel__note t-caption">The full student screen, with the editor and timer, comes in a later step.</p>
        <ol className="preview-list">
          {items.map((item, index) => (
            <li key={item.question.id} className="preview-item">
              <h3 className="preview-item__no t-caption">Question {index + 1}</h3>
              <RichText text={item.text} className="preview-item__text t-body" />
              {item.question.starter_code.trim() !== "" ? (
                <pre className="code__box t-code" tabIndex={0} aria-label={`Starter code, ${languageLabel(item.question.language)}`}>
                  <code>{item.question.starter_code.replace(/\s+$/, "")}</code>
                </pre>
              ) : null}
            </li>
          ))}
        </ol>
        <div className="modal-panel__foot">
          <Button variant="secondary" ref={closeRef} onClick={onClose}>Close</Button>
        </div>
      </div>
    </div>
  );
}
