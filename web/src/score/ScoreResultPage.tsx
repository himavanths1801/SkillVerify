import { Link, useNavigate, useParams } from "react-router-dom";
import { retryAnalysis } from "../api/client";
import { useAnalysis } from "../api/useAnalysis";
import { Banner } from "../components/Banner";
import { Button } from "../components/Button";
import { Card } from "../components/Card";
import { bannerText } from "../upload/errors";
import { STEPS } from "../app/steps";
import { ScoreView } from "./ScoreView";

function Skeleton() {
  return (
    <div className="score" role="status" aria-busy="true" aria-label="Loading the result">
      <div className="score__head">
        <div>
          <h1 className="t-heading" tabIndex={-1}>Score</h1>
          <div className="skeleton skeleton--line" />
        </div>
      </div>
      <div className="score__top">
        <Card className="skeleton-card skeleton-card--tall"><div className="skeleton skeleton--block" /></Card>
        <div className="score__metrics">
          {[0, 1, 2].map((n) => <Card key={n} className="skeleton-card"><div className="skeleton skeleton--block" /></Card>)}
        </div>
      </div>
      <Card className="skeleton-card"><div className="skeleton skeleton--block" /></Card>
      <Card className="skeleton-card skeleton-card--tall"><div className="skeleton skeleton--block" /></Card>
    </div>
  );
}

/** `/recruiter/score/:id`: loads the job (so a refresh works while the server still has it) and shows the right state. */
export function ScoreResultPage() {
  const { id = null } = useParams();
  const navigate = useNavigate();
  const { status, restart } = useAnalysis(id);

  if (status?.state === "done" && status.result) return <ScoreView result={status.result} meta={status.meta} analysisId={id ?? undefined} />;

  if (status?.state === "failed" && status.error) {
    const error = status.error;
    if (error.code === "unknown") {
      return (
        <Card className="placeholder">
          <h1 className="t-heading" tabIndex={-1}>We can't find this analysis</h1>
          <p className="t-body">It may have expired, or the server was restarted. <Link to={STEPS[0].path}>Go to Upload</Link> to run it again.</p>
        </Card>
      );
    }
    const text = bannerText(error);
    const retry = async () => {
      if (error.code !== "network" && id) await retryAnalysis(id);
      restart();
    };
    return (
      <div className="score">
        <h1 className="t-heading" tabIndex={-1}>Score</h1>
        <Banner title={text.title} message={text.message} onRetry={text.retry ? () => void retry() : undefined} />
        <div><Button variant="secondary" onClick={() => navigate(STEPS[0].path)}>Back to upload</Button></div>
      </div>
    );
  }

  return <Skeleton />;
}
