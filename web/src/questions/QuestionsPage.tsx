import { Link, useNavigate, useParams } from "react-router-dom";
import { useAnalysis } from "../api/useAnalysis";
import { useQuestions } from "../api/useQuestions";
import type { ApiError } from "../api/types";
import { STEPS } from "../app/steps";
import { Banner } from "../components/Banner";
import { Button } from "../components/Button";
import { Card } from "../components/Card";
import { formatScore, humanize } from "../score/format";
import { bannerText } from "../upload/errors";
import { BottomBar, Header } from "./Chrome";
import { stageText } from "./format";
import { ReviewScreen } from "./ReviewScreen";

function SkeletonCards() {
  return (
    <>
      {[0, 1, 2].map((n) => (
        <Card key={n} className="skeleton-card qskeleton">
          <div className="skeleton skeleton--tag" />
          <div className="skeleton skeleton--line-full" />
          <div className="skeleton skeleton--line-most" />
          <div className="skeleton skeleton--code" />
          <div className="qskeleton__buttons">
            <div className="skeleton skeleton--button" />
            <div className="skeleton skeleton--button" />
          </div>
        </Card>
      ))}
    </>
  );
}

type Who = { name: string; jobTitle: string; score: string };

/** Queued, running (or failed): the header, the step text and skeleton cards. A failure puts a Banner above. */
function Waiting({ who, stage, error, onRetry }: { who: Who | null; stage: string | null; error?: ApiError | null; onRetry?: () => void }) {
  const text = error ? bannerText(error) : null;
  return (
    <div className="review" role={error ? undefined : "status"} aria-busy={error ? undefined : true}>
      {error && text ? (
        <>
          <Banner title={text.title} message={text.message} onRetry={text.retry && error.retryable ? onRetry : undefined} />
          {error.code === "analysis_not_ready" ? <p className="t-body"><Link to="/recruiter/score">Back to score</Link></p> : null}
        </>
      ) : null}
      {who ? <Header name={who.name} jobTitle={who.jobTitle} score={who.score} counter="Generating questions" /> : null}
      <div className="review__step">
        <span className="spinner" aria-hidden="true" />
        <div>
          <p className="t-body">{stageText(stage)}</p>
          <p className="t-caption review__quiet">This can take a minute or two.</p>
        </div>
      </div>
      <SkeletonCards />
      <BottomBar previewDisabled createDisabled />
    </div>
  );
}

export function NotFoundCard() {
  const navigate = useNavigate();
  return (
    <Card className="placeholder">
      <h1 className="t-heading" tabIndex={-1}>Question review</h1>
      <p className="t-body">That analysis was not found. It may have expired.</p>
      <Button variant="secondary" onClick={() => navigate(STEPS[0].path)}>Back to upload</Button>
    </Card>
  );
}

/** `/recruiter/questions` without an analysis id. */
export function QuestionsStartPage() {
  return (
    <Card className="placeholder">
      <h1 className="t-heading" tabIndex={-1}>Question review</h1>
      <p className="t-body">Start from an analysis to review questions.</p>
      <p className="t-body"><Link to={STEPS[0].path}>Go to Upload</Link></p>
    </Card>
  );
}

/** `/recruiter/questions/:id`: loads the analysis (for the header) and the question run, and shows the right state. */
export function QuestionsPage() {
  const { id = null } = useParams();
  const analysis = useAnalysis(id);
  const a = analysis.status;
  const ready = a?.state === "done" && a.result !== null;
  const questions = useQuestions(ready ? id : null);

  if (a === null) return <Waiting who={null} stage={null} />;
  if (!ready || a.result === null || id === null) return <NotFoundCard />;
  const result = a.result;
  const who: Who = {
    name: a.meta?.candidate_name || humanize(result.cv_label),
    jobTitle: a.meta?.job_title || humanize(result.job_name),
    score: formatScore(result.score.repo_fingerprint),
  };

  const q = questions.status;
  if (questions.notFound) return <NotFoundCard />;
  if (q === null || q.state === "idle" || q.state === "queued" || q.state === "running") {
    return <Waiting who={who} stage={q?.stage ?? null} />;
  }
  if (q.state === "failed" || q.result === null) {
    const error: ApiError = q.error ?? { code: "internal_error", message: "", retryable: true };
    return <Waiting who={who} stage={q.stage} error={error} onRetry={questions.retry} />;
  }
  return <ReviewScreen key={`${id}:${q.result.generated_at}`} id={id} run={q.result} {...who} />;
}
