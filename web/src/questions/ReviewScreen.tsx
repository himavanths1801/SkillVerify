import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { replaceQuestion } from "../api/client";
import type { QuestionsResult, SkippedSkill } from "../api/types";
import { Button } from "../components/Button";
import { Card } from "../components/Card";
import { Icon } from "../components/Icon";
import { bannerText } from "../upload/errors";
import { BottomBar, Header } from "./Chrome";
import { DroppedBar } from "./DroppedBar";
import { MIN_APPROVED, plural } from "./format";
import { PreviewModal } from "./PreviewModal";
import { QuestionCard } from "./QuestionCard";
import type { FocusRequest } from "./QuestionCard";
import { SkillsCard } from "./SkillsCard";
import { loadReview, matchesRun, saveAssessment, saveReview } from "./storage";
import type { ReviewItem } from "./storage";

const NO_REPLACEMENT = "There is no other code in the candidate's repos to ask about.";
const EXTRA = "extra"; // the id used for "Generate another question"

type Note = { kind: "quiet" | "error"; text: string };
type Props = {
  id: string;
  run: QuestionsResult;
  name: string;
  jobTitle: string;
  score: string;
};

function initial(id: string, run: QuestionsResult): { items: ReviewItem[]; skipped: SkippedSkill[] } {
  const runIds = run.questions.map((q) => q.id);
  const stored = loadReview(id);
  if (stored && matchesRun(stored, runIds)) return { items: stored.items, skipped: stored.skipped };
  return {
    items: run.questions.map((question) => ({ question, status: "pending" as const, text: question.question })),
    skipped: run.skipped_skills,
  };
}

/** The review itself: the question cards, the dropped bars, the notes, the skills list and the bottom bar. */
export function ReviewScreen({ id, run, name, jobTitle, score }: Props) {
  const navigate = useNavigate();
  const start = useMemo(() => initial(id, run), [id, run]);
  const [items, setItems] = useState<ReviewItem[]>(start.items);
  const [skipped, setSkipped] = useState<SkippedSkill[]>(start.skipped);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [replacing, setReplacing] = useState<string | null>(null);
  const [notes, setNotes] = useState<Record<string, Note>>({});
  const [focus, setFocus] = useState<FocusRequest>(null);
  const [preview, setPreview] = useState(false);
  const previewRef = useRef<HTMLButtonElement>(null);
  const focusCount = useRef(0);

  useEffect(() => {
    saveReview(id, { runIds: run.questions.map((q) => q.id), items, skipped });
  }, [id, run, items, skipped]);

  const live = items.filter((item) => item.status !== "dropped");
  const approved = items.filter((item) => item.status === "approved");
  const hasDropped = items.some((item) => item.status === "dropped");
  const invalidDraft = approved.some((item) => drafts[item.question.id] !== undefined && drafts[item.question.id].trim() === "");
  const canCreate = approved.length >= MIN_APPROVED && !invalidDraft;

  const ask = (target: "heading" | "restore", qid: string) => {
    focusCount.current += 1;
    setFocus({ id: qid, target, n: focusCount.current });
  };
  const patch = (qid: string, change: Partial<ReviewItem>) =>
    setItems((list) => list.map((item) => (item.question.id === qid ? { ...item, ...change } : item)));
  const dropDraft = (qid: string) =>
    setDrafts((current) => {
      const { [qid]: _gone, ...rest } = current;
      return rest;
    });
  const setNote = (key: string, note: Note | null) =>
    setNotes((current) => {
      const { [key]: _gone, ...rest } = current;
      return note ? { ...rest, [key]: note } : rest;
    });

  const approve = (item: ReviewItem) => {
    const qid = item.question.id;
    const draft = drafts[qid];
    if (draft !== undefined && item.status !== "approved") {
      if (draft.trim() === "") return; // the empty-text error is already shown under the area
      patch(qid, { text: draft, status: "approved" });
      dropDraft(qid);
      return;
    }
    patch(qid, { status: item.status === "approved" ? "pending" : "approved" });
  };
  const done = (item: ReviewItem) => {
    const draft = drafts[item.question.id];
    if (draft === undefined || draft.trim() === "") return;
    patch(item.question.id, { text: draft });
    dropDraft(item.question.id);
  };
  const drop = (item: ReviewItem) => {
    dropDraft(item.question.id);
    patch(item.question.id, { status: "dropped" });
    ask("restore", item.question.id);
  };
  const restore = (item: ReviewItem) => patch(item.question.id, { status: "approved" });

  const replace = async (key: string, after?: string) => {
    if (replacing !== null) return;
    setReplacing(key);
    setNote(key, null);
    const answer = await replaceQuestion(id, after);
    setReplacing(null);
    if (!answer.ok) {
      if (answer.error.code === "no_replacement_available") setNote(key, { kind: "quiet", text: NO_REPLACEMENT });
      else setNote(key, { kind: "error", text: bannerText(answer.error).title });
      return;
    }
    const fresh: ReviewItem = { question: answer.value.question, status: "pending", text: answer.value.question.question };
    setItems((list) => (after === undefined ? [...list, fresh] : list.map((item) => (item.question.id === after ? fresh : item))));
    setSkipped(answer.value.skipped_skills);
    ask("heading", fresh.question.id);
  };

  const create = () => {
    saveAssessment(id, approved.map((item) => ({
      id: item.question.id, text: item.text, language: item.question.language, starter_code: item.question.starter_code,
    })));
    navigate(`/recruiter/assessment/${id}`);
  };
  const closePreview = () => {
    setPreview(false);
    setTimeout(() => previewRef.current?.focus(), 0);
  };

  const showNote = live.length < MIN_APPROVED || (hasDropped && approved.length < MIN_APPROVED);
  const noteKey = notes[EXTRA];
  const counter = run.status === "none" ? "0 questions" : `${plural(live.length, "question", "questions")}, ${approved.length} approved`;

  if (run.status === "none" && items.length === 0) {
    return (
      <div className="review">
        <Header name={name} jobTitle={jobTitle} score={score} counter="0 questions" />
        <Card className="review__empty">
          <p className="t-body">{run.message ?? "No code was found that shows the job's skills, so no questions could be written."}</p>
          <div><Button variant="secondary" onClick={() => navigate(`/recruiter/score/${id}`)}>Back to score</Button></div>
        </Card>
        <BottomBar previewDisabled createDisabled />
      </div>
    );
  }

  return (
    <div className="review">
      <Header name={name} jobTitle={jobTitle} score={score} counter={counter} />
      {items.map((item, index) => {
        const qid = item.question.id;
        return item.status === "dropped" ? (
          <DroppedBar
            key={qid}
            id={qid}
            number={index + 1}
            focus={focus}
            writing={replacing === qid}
            waiting={replacing !== null && replacing !== qid}
            note={notes[qid] ?? null}
            onRestore={() => restore(item)}
            onReplace={() => void replace(qid, qid)}
          />
        ) : (
          <QuestionCard
            key={qid}
            item={item}
            number={index + 1}
            draft={drafts[qid]}
            focus={focus}
            onEdit={() => setDrafts((current) => ({ ...current, [qid]: item.text }))}
            onDraft={(text) => setDrafts((current) => ({ ...current, [qid]: text }))}
            onDone={() => done(item)}
            onCancel={() => dropDraft(qid)}
            onApprove={() => approve(item)}
            onDrop={() => drop(item)}
          />
        );
      })}

      {showNote ? (
        <div className="info-banner" role="status">
          <Icon name="info" size={22} className="info-banner__icon" />
          <div className="info-banner__body">
            <p className="t-body">
              {hasDropped
                ? "A session needs at least 3 questions. Approve a dropped question again or generate a replacement."
                : run.message || "A session needs at least 3 questions."}
            </p>
            {!hasDropped ? (
              <div className="info-banner__actions">
                <Button variant="secondary" disabled={replacing !== null} aria-busy={replacing === EXTRA} onClick={() => void replace(EXTRA)}>
                  {replacing === EXTRA ? (<><span className="spinner spinner--small" aria-hidden="true" />Writing...</>) : "Generate another question"}
                </Button>
                {noteKey ? (
                  <p className="t-caption info-banner__note" role={noteKey.kind === "error" ? "alert" : "status"}>{noteKey.text}</p>
                ) : null}
              </div>
            ) : null}
          </div>
        </div>
      ) : null}

      <SkillsCard skipped={skipped} />

      <BottomBar
        previewRef={previewRef}
        previewDisabled={approved.length < 1}
        createDisabled={!canCreate}
        onPreview={() => setPreview(true)}
        onCreate={create}
      />
      {preview ? <PreviewModal items={approved} onClose={closePreview} /> : null}
    </div>
  );
}
