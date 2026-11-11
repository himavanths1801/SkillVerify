import { useEffect, useId, useRef } from "react";
import type { ChangeEvent, KeyboardEvent } from "react";
import { Button } from "../components/Button";
import { Card } from "../components/Card";
import { Icon } from "../components/Icon";
import { CodeBlock } from "./CodeBlock";
import { COUNTER_FROM, MAX_QUESTION_CHARS, kindLabel, normalise } from "./format";
import { RichText } from "./RichText";
import type { ReviewItem } from "./storage";

export type FocusRequest = { id: string; target: "heading" | "restore"; n: number } | null;

type Props = {
  item: ReviewItem;
  number: number;
  /** The text being edited, or `undefined` when the card is not in edit mode. */
  draft: string | undefined;
  focus: FocusRequest;
  onEdit: () => void;
  onDraft: (text: string) => void;
  onDone: () => void;
  onCancel: () => void;
  onApprove: () => void;
  onDrop: () => void;
};

function Why({ item }: { item: ReviewItem }) {
  const why = item.question.why;
  return (
    <section className="why" aria-label="Why this question">
      <div className="why__head">
        <h3 className="t-label why__title">Why this question</h3>
        <span className="why__private t-caption">Only you see this</span>
      </div>
      <div className="why__row">
        <span className="why__repo t-code"><Icon name="repo" size={16} />{why.repo}</span>
        <span className="t-code why__path">{why.path}</span>
      </div>
      <div className="why__row">
        <span className="chip t-caption why__skill">{why.skill}</span>
        <span className={`tier tier--${why.tier === "must_have" ? "must" : "nice"} t-label`}>
          {why.tier === "must_have" ? "Must-have" : "Nice-to-have"}
        </span>
      </div>
      <p className="why__reason t-caption">{why.reason}</p>
    </section>
  );
}

export function QuestionCard({ item, number, draft, focus, onEdit, onDraft, onDone, onCancel, onApprove, onDrop }: Props) {
  const { question, status, text } = item;
  const editing = draft !== undefined;
  const approved = status === "approved";
  const edited = normalise(text) !== normalise(question.question);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const areaRef = useRef<HTMLTextAreaElement>(null);
  const editRef = useRef<HTMLButtonElement>(null);
  const wasEditing = useRef(false);
  const areaId = useId();
  const hintId = `${areaId}-hint`;
  const errorId = `${areaId}-error`;
  const empty = editing && draft.trim() === "";

  useEffect(() => {
    if (editing && !wasEditing.current) areaRef.current?.focus();
    if (!editing && wasEditing.current) editRef.current?.focus();
    wasEditing.current = editing;
  }, [editing]);

  useEffect(() => {
    if (focus && focus.id === question.id && focus.target === "heading") headingRef.current?.focus();
  }, [focus, question.id]);

  useEffect(() => {
    const area = areaRef.current;
    if (!area || area.scrollHeight === 0) return;
    area.style.height = "auto";
    area.style.height = `${area.scrollHeight}px`;
  }, [draft, editing]);

  const change = (event: ChangeEvent<HTMLTextAreaElement>) => onDraft(event.target.value.slice(0, MAX_QUESTION_CHARS));
  const keys = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      onCancel();
    }
  };
  const showCounter = editing && draft.length >= COUNTER_FROM;

  return (
    <Card className="qcard" role="article" aria-labelledby={`${areaId}-h`}>
      <div className="qcard__head">
        <h2 id={`${areaId}-h`} className="t-label qcard__no" tabIndex={-1} ref={headingRef}>Question {number}</h2>
        <span className="pill t-label">{kindLabel(question.kind)}</span>
        {edited ? (
          <span className="pill pill--edited t-label"><Icon name="pencil" size={12} />Edited</span>
        ) : null}
      </div>

      {editing ? (
        <div className="qcard__edit">
          <textarea
            ref={areaRef}
            className="sv-textarea qcard__area t-body"
            rows={4}
            aria-label={`Question ${number} text`}
            aria-invalid={empty}
            aria-describedby={`${hintId}${empty ? ` ${errorId}` : ""}`}
            value={draft}
            maxLength={MAX_QUESTION_CHARS}
            onChange={change}
            onKeyDown={keys}
          />
          {empty ? (
            <p id={errorId} className="field-error t-caption"><Icon name="alert" size={16} />A question can't be empty.</p>
          ) : null}
          <div className="qcard__hintrow">
            <p id={hintId} className="qcard__hint t-caption">Only the question text can be edited.</p>
            {showCounter ? <span className="qcard__count t-caption" aria-live="polite">{draft.length} / {MAX_QUESTION_CHARS}</span> : null}
          </div>
        </div>
      ) : (
        <RichText text={text} className="qcard__text t-body" />
      )}

      {question.starter_code.trim() !== "" ? <CodeBlock code={question.starter_code} language={question.language} collapsible /> : null}

      <Why item={item} />

      <div className="qcard__buttons">
        <button type="button" className="text-btn qcard__drop t-body" onClick={onDrop}>Drop question</button>
        <div className="qcard__right">
          {editing ? (
            <Button variant="secondary" onClick={onDone}>Done</Button>
          ) : (
            <Button variant="secondary" ref={editRef} onClick={onEdit}>Edit</Button>
          )}
          {approved ? (
            <button
              type="button"
              className="tick"
              aria-pressed="true"
              aria-label={`Approved. Press to undo approval of question ${number}`}
              title="Undo approval"
              onClick={onApprove}
            >
              <Icon name="check" size={18} />
            </button>
          ) : (
            <Button aria-label={`Approve question ${number}`} onClick={onApprove}>Approve</Button>
          )}
        </div>
      </div>
    </Card>
  );
}
