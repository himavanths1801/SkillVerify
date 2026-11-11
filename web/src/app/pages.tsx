import { Link } from "react-router-dom";
import { Card } from "../components/Card";
import { scoreMock } from "../mocks/score";
import { DesignSystemPreview } from "../pages/DesignSystemPreview";
import { PlaceholderCard } from "./PlaceholderCard";
import { STEPS } from "./steps";
import { useModeRoute } from "./useModeRoute";

export function ScorePage() {
  const { score, github } = scoreMock;
  return (
    <PlaceholderCard title="Score" description="The Repo Fingerprint Score for the sample candidate will appear here." next={STEPS[2].path}>
      <div className="mock-lines t-body">
        <p>{scoreMock.cv_label} for {scoreMock.job_name} (GitHub: {github.username}, status: {scoreMock.status})</p>
        <p>Repo Fingerprint Score: {score.repo_fingerprint}</p>
        <p>Claim coverage: {score.claim_coverage}</p>
        <p>Checkable only: {score.checkable_only}</p>
        <p>Verifiable share: {score.verifiable_share}</p>
        <p>Points: {score.points_earned} of {score.points_possible}</p>
        {scoreMock.skills.map((row) => (
          <p key={row.skill}>
            {row.skill}: {row.tier}, claimed {row.claimed ? "yes" : "no"}, GitHub {row.github_status}, {row.points} of {row.weight} points
          </p>
        ))}
        <p>Hidden strengths: {scoreMock.hidden_strengths.join(", ") || "none"}</p>
        <p>Flags: {scoreMock.flags.join(", ") || "none"}</p>
      </div>
    </PlaceholderCard>
  );
}

export function StudentQuestionPage() {
  return <PlaceholderCard title="Take your time" description="The question and the answer box will appear here." next={STEPS[2].path} />;
}

export function NotFoundPage() {
  return (
    <Card className="placeholder">
      <h1 className="t-heading" tabIndex={-1}>Page not found</h1>
      <p className="t-body">That page does not exist. <Link to={STEPS[0].path}>Go back to Upload</Link>.</p>
    </Card>
  );
}

export function DesignSystemPage() {
  useModeRoute("recruiter");
  return <DesignSystemPreview />;
}
