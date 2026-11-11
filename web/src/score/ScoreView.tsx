import { useId, useState } from "react";
import { useNavigate } from "react-router-dom";
import type { JobMeta, ScoreResult, SkillRow } from "../api/types";
import { Button } from "../components/Button";
import { Card } from "../components/Card";
import { Icon } from "../components/Icon";
import { Input } from "../components/Input";
import { StatusBadge } from "../components/StatusBadge";
import { Toggle } from "../components/Toggle";
import { STEPS } from "../app/steps";
import {
  NON_OK_STATUSES, barPercent, buildNotes, downloadName, effectiveCutoff, evidenceOf, formatPoints, formatScore,
  humanize, tierLabel,
} from "./format";

const MAX_CV_EXTRA_CHIPS = 12;

function Bar({ value, label, thin }: { value: number | null; label: string; thin?: boolean }) {
  const text = `${label} ${formatScore(value)}${value === null ? "" : " out of 100"}`;
  return (
    <div className={`bar${thin ? " bar--thin" : ""}`} role="img" aria-label={text}>
      <div className="bar__fill" style={{ width: `${barPercent(value)}%` }} />
    </div>
  );
}

/** The 0-100 score bar: a filled part, a dot at the score and a dashed tick at the cut-off. */
export function ScoreBar({ value, cutoff }: { value: number | null; cutoff: number }) {
  const label = `${formatScore(value)}${value === null ? "" : " out of 100"}, suggested cut-off ${cutoff}`;
  return (
    <div className="scorebar">
      <div className="scorebar__track" role="img" aria-label={label}>
        <div className="scorebar__fill" style={{ width: `${barPercent(value)}%` }} />
        {value !== null ? <div className="scorebar__dot" data-testid="score-dot" style={{ left: `${barPercent(value)}%` }} /> : null}
        <div className="scorebar__tick" data-testid="cutoff-tick" style={{ left: `${cutoff}%` }} />
      </div>
      <div className="scorebar__labels t-label" aria-hidden="true">
        <span>0</span>
        <span className="scorebar__cutoff" style={{ left: `${cutoff}%` }}>Suggested cut-off {cutoff}</span>
        <span>100</span>
      </div>
    </div>
  );
}

function Metric({ title, value, text }: { title: string; value: number | null; text: string }) {
  return (
    <Card className="metric">
      <div className="metric__head">
        <h3 className="metric__title t-body">{title}</h3>
        <span className="metric__value t-title">{formatScore(value)}</span>
      </div>
      <p className="t-caption metric__text">{text}</p>
      <Bar value={value} label={title} thin />
    </Card>
  );
}

function EvidenceBadge({ row }: { row: SkillRow }) {
  switch (evidenceOf(row)) {
    case "found":
      return <StatusBadge variant="found">Found in code</StatusBadge>;
    case "look":
      return <StatusBadge variant="gold" />;
    case "unclaimed":
      return <StatusBadge variant="notstarted">Not claimed</StatusBadge>;
    default:
      return <StatusBadge variant="neutral" />;
  }
}

function Chips({ items, extra }: { items: string[]; extra?: string }) {
  return (
    <ul className="chips">
      {items.map((item) => <li key={item} className="chip t-caption">{item}</li>)}
      {extra ? <li className="chip chip--more t-caption">{extra}</li> : null}
    </ul>
  );
}

function downloadJson(result: ScoreResult) {
  const blob = new Blob([JSON.stringify(result, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = downloadName(result);
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

/** The real Score screen, drawn from a `ScoreResult`. */
export function ScoreView({ result, meta, analysisId }: { result: ScoreResult; meta?: JobMeta | null; analysisId?: string }) {
  const navigate = useNavigate();
  const [auto, setAuto] = useState(true);
  const [typed, setTyped] = useState("");
  const cutoffId = useId();
  const cutoff = effectiveCutoff(auto, typed);
  const { score, github } = result;
  const handle = github.username ?? github.cv_username;
  const notOk = result.status !== "ok";
  const notes = buildNotes(result, meta);
  const extraNames = result.cv_extra_skills.names;
  const extraTotal = Math.max(result.cv_extra_skills.count, extraNames.length);
  const hidden = result.hidden_strengths.map((h) => h.skill);
  const scanned = github.repos_scanned !== null && github.counted_repos !== null;
  const capped = meta?.scan?.capped === true;
  const scanText = capped
    ? `Newest ${meta?.scan?.repos_used} of ${meta?.scan?.repos_total} repositories scanned.`
    : scanned ? `${github.repos_scanned} of ${github.counted_repos} repositories scanned` : "No repositories scanned";

  return (
    <div className="score">
      <div className="score__head">
        <div>
          <h1 className="t-heading" tabIndex={-1}>{meta?.job_title || humanize(result.job_name)}</h1>
          <div className="score__who t-body">
            <span>{meta?.candidate_name || humanize(result.cv_label)}</span>
            {handle ? (
              <span className="handle t-code">
                <Icon name="branch" size={16} />
                {handle}
              </span>
            ) : null}
          </div>
        </div>
        <Button variant="secondary" onClick={() => downloadJson(result)}>Download JSON</Button>
      </div>

      {notOk && NON_OK_STATUSES.includes(result.status) ? (
        <div className="info-banner" role="status">
          <Icon name="info" size={22} className="info-banner__icon" />
          <p className="t-body">{result.message || "A full score could not be calculated for this candidate."}</p>
        </div>
      ) : null}

      <div className="score__top">
        <Card className="score-card">
          <span className="t-label score-card__label">Repo fingerprint score</span>
          <div className="score-card__value">
            <span className="t-score" data-testid="fingerprint">{formatScore(score.repo_fingerprint)}</span>
            {score.repo_fingerprint !== null ? <span className="t-caption score-card__of">out of 100</span> : null}
          </div>
          <p className="t-body score-card__text">How much of the job's required skills the CV claims and the GitHub code confirms.</p>
          <ScoreBar value={score.repo_fingerprint} cutoff={cutoff} />
          <p className="t-caption score-card__note">Shows where the evidence sits. It is not a decision.</p>
          <Toggle checked={auto} onChange={setAuto} label="Auto threshold" />
          {!auto ? (
            <div className="cutoff-input">
              <Input
                id={cutoffId}
                label="Cut-off"
                type="number"
                min={0}
                max={100}
                value={typed}
                placeholder="35"
                onChange={(event) => setTyped(event.target.value)}
              />
            </div>
          ) : null}
        </Card>
        <div className="score__metrics">
          <Metric title="Claim coverage" value={score.claim_coverage} text="Share of the job's skill weight the CV claims." />
          <Metric title="Checkable only" value={score.checkable_only} text="The score counting only skills that code can show." />
          <Metric title="Verifiable share" value={score.verifiable_share} text="Share of the job's skill weight that GitHub can check at all." />
        </div>
      </div>

      <Card className="scan-card">
        <div className="scan-card__head">
          <h2 className="t-title">GitHub scan</h2>
          {github.scan_complete === true ? (
            <StatusBadge variant="neutral" icon="check">Scan complete</StatusBadge>
          ) : github.scan_complete === false ? (
            <StatusBadge variant="gold" />
          ) : (
            <StatusBadge variant="neutral" />
          )}
        </div>
        <div className="scan-card__row t-body">
          <span><span className="t-label">Username</span> <span className="t-code">{handle ?? "n/a"}</span></span>
          <span>{scanText}</span>
          {github.languages_top.length > 0 ? (
            <span className="scan-card__langs">
              <span className="t-label">Top languages</span>
              <Chips items={github.languages_top} />
            </span>
          ) : null}
        </div>
        {github.scan_complete === false ? <p className="t-caption scan-card__note">Only some repositories were scanned.</p> : null}
      </Card>

      <Card className="skills-card">
        <h2 className="t-title">Per-skill breakdown</h2>
        <table className="skills-table t-body">
          <caption className="sr-only">Per-skill breakdown</caption>
          <thead>
            <tr>
              <th scope="col" className="t-label">Skill</th>
              <th scope="col" className="t-label">Tier</th>
              <th scope="col" className="t-label">Claimed on CV</th>
              <th scope="col" className="t-label">GitHub evidence</th>
              <th scope="col" className="t-label skills-table__num">Points</th>
            </tr>
          </thead>
          <tbody>
            {result.skills.map((row) => (
              <tr key={row.skill}>
                <th scope="row" className="skills-table__skill">{row.skill}</th>
                <td>{tierLabel(row.tier)}</td>
                <td>{row.claimed ? "Yes" : "No"}</td>
                <td><EvidenceBadge row={row} /></td>
                <td className="skills-table__num">
                  {row.points === null ? "n/a" : `${formatPoints(row.points)} of ${formatPoints(row.weight)}`}
                </td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr>
              <th scope="row" colSpan={4}>Points earned</th>
              <td className="skills-table__num skills-table__total t-title">
                {formatPoints(score.points_earned)} of {formatPoints(score.points_possible)}
              </td>
            </tr>
          </tfoot>
        </table>
      </Card>

      <div className="score__pair">
        <Card>
          <h2 className="t-title">Hidden strengths</h2>
          {hidden.length > 0 ? <Chips items={hidden} /> : <p className="t-body score__empty">No extra skills found in the code.</p>}
        </Card>
        <Card>
          <h2 className="t-title">Also on the CV</h2>
          {extraNames.length > 0 ? (
            <Chips
              items={extraNames.slice(0, MAX_CV_EXTRA_CHIPS)}
              extra={extraTotal > MAX_CV_EXTRA_CHIPS ? `+${extraTotal - MAX_CV_EXTRA_CHIPS} more` : undefined}
            />
          ) : (
            <p className="t-body score__empty">No other skills were found on the CV.</p>
          )}
        </Card>
      </div>

      {notes.length > 0 ? (
        <Card className="notes-card">
          <h2 className="t-title notes-card__title"><Icon name="info" size={20} className="notes-card__icon" />Notes</h2>
          <ul className="notes t-body">
            {notes.map((note) => <li key={note}>{note}</li>)}
          </ul>
        </Card>
      ) : null}

      <div className="score__next">
        <Button onClick={() => navigate(analysisId ? `${STEPS[2].path}/${encodeURIComponent(analysisId)}` : STEPS[2].path)}>Continue to question review</Button>
      </div>
    </div>
  );
}
