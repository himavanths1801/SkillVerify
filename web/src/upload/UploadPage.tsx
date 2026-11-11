import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { createAnalysis, retryAnalysis } from "../api/client";
import type { ApiError } from "../api/types";
import { useAnalysis } from "../api/useAnalysis";
import { Banner } from "../components/Banner";
import { Button } from "../components/Button";
import { Card } from "../components/Card";
import { Icon } from "../components/Icon";
import { Input } from "../components/Input";
import { Toggle } from "../components/Toggle";
import { DropZone } from "./DropZone";
import { bannerText } from "./errors";
import { ProgressCard } from "./ProgressCard";
import { buildFormData, hasJobDescription, validateFile, validateUsername } from "./validation";

const USERNAME_ID = "github-username";

export function UploadPage() {
  const navigate = useNavigate();
  const [jdMode, setJdMode] = useState<"file" | "text">("file");
  const [jdFile, setJdFile] = useState<File | null>(null);
  const [jdText, setJdText] = useState("");
  const [cvFile, setCvFile] = useState<File | null>(null);
  const [username, setUsername] = useState("");
  const [jdError, setJdError] = useState<string | null>(null);
  const [cvError, setCvError] = useState<string | null>(null);
  const [usernameError, setUsernameError] = useState<string | null>(null);

  const [jobId, setJobId] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<ApiError | null>(null);
  const { status, restart } = useAnalysis(jobId);

  const headingRef = useRef<HTMLHeadingElement>(null);
  const bannerRef = useRef<HTMLDivElement>(null);

  const current = jobId !== null && status?.id === jobId ? status : null;
  const polling = jobId !== null && (current === null || current.state === "queued" || current.state === "running");
  const analyzing = submitting || polling;
  const failure: ApiError | null = submitError ?? (current?.state === "failed" ? current.error : null);
  const usernameFlagged = usernameError !== null || failure?.code === "github_user_not_found";

  useEffect(() => {
    if (jobId !== null && current?.state === "done") navigate(`/recruiter/score/${jobId}`);
  }, [jobId, current, navigate]);

  useEffect(() => {
    if (analyzing) headingRef.current?.focus();
  }, [analyzing]);

  useEffect(() => {
    if (!failure) return;
    if (failure.code === "github_user_not_found") document.getElementById(USERNAME_ID)?.focus();
    else bannerRef.current?.focus();
  }, [failure]);

  const chooseFile = (file: File, setFile: (f: File | null) => void, setError: (e: string | null) => void) => {
    const problem = validateFile(file);
    setError(problem);
    if (!problem) setFile(file);
  };

  async function submit() {
    const problem = validateUsername(username);
    setUsernameError(problem);
    if (problem) {
      document.getElementById(USERNAME_ID)?.focus();
      return;
    }
    setSubmitError(null);
    setJobId(null);
    setSubmitting(true);
    const answer = await createAnalysis(buildFormData({ jdMode, jdFile, jdText, cvFile, username }));
    setSubmitting(false);
    if (!answer.ok) setSubmitError(answer.error);
    else setJobId(answer.value.id);
  }

  async function retry() {
    if (!failure) return;
    if (failure.code === "network" || jobId === null) {
      await submit();
      return;
    }
    const answer = await retryAnalysis(jobId);
    if (!answer.ok) {
      setSubmitError(answer.error);
      return;
    }
    setSubmitError(null);
    restart();
  }

  const banner = !analyzing && failure ? bannerText(failure) : null;
  const canAnalyze = !analyzing && hasJobDescription({ jdMode, jdFile, jdText }) && cvFile !== null;

  return (
    <div className="upload">
      <h1 className="t-heading" tabIndex={-1}>Upload</h1>
      <p className="t-body upload__lead">
        Add the job description and the candidate's CV. SkillVerify checks the CV's claims against their GitHub code.
      </p>

      {analyzing ? <ProgressCard ref={headingRef} stage={current?.stage ?? ""} state={current?.state ?? "queued"} /> : null}
      {banner ? (
        <div ref={bannerRef} tabIndex={-1} className="banner-slot">
          <Banner title={banner.title} message={banner.message} onRetry={banner.retry ? () => void retry() : undefined} />
        </div>
      ) : null}

      <div className="upload__cards">
        <Card>
          <div className="card-head">
            <h2 className="t-title">Job description</h2>
            <div className="mode-toggle t-body" data-active={jdMode}>
              <span className="mode-toggle__file">Upload file</span>
              <Toggle
                checked={jdMode === "text"}
                onChange={(on) => {
                  setJdMode(on ? "text" : "file");
                  setJdError(null);
                }}
                label="Paste text"
                disabled={analyzing}
              />
            </div>
          </div>
          {jdMode === "file" ? (
            <DropZone
              label="Job description file"
              file={jdFile}
              error={jdError}
              locked={analyzing}
              onFile={(file) => chooseFile(file, setJdFile, setJdError)}
              onRemove={() => setJdFile(null)}
            />
          ) : (
            <textarea
              className="sv-textarea t-body"
              aria-label="Job description text"
              placeholder="Paste the job description here"
              value={jdText}
              readOnly={analyzing}
              onChange={(event) => setJdText(event.target.value)}
            />
          )}
        </Card>
        <Card>
          <div className="card-head">
            <h2 className="t-title">Candidate CV</h2>
          </div>
          <DropZone
            label="Candidate CV file"
            file={cvFile}
            error={cvError}
            locked={analyzing}
            onFile={(file) => chooseFile(file, setCvFile, setCvError)}
            onRemove={() => setCvFile(null)}
          />
        </Card>
      </div>

      <p className="t-caption upload__note">PDF, Word or text. Up to 5 MB each.</p>

      <div className="username-field">
        <Input
          id={USERNAME_ID}
          label="GitHub username (optional)"
          helper="Leave empty to use the one found in the CV."
          value={username}
          disabled={analyzing}
          className={usernameFlagged ? "sv-input--error" : ""}
          aria-invalid={usernameFlagged ? true : undefined}
          autoComplete="off"
          spellCheck={false}
          onChange={(event) => {
            setUsername(event.target.value);
            setUsernameError(null);
          }}
          onBlur={() => setUsernameError(validateUsername(username))}
        />
        {usernameError ? (
          <p className="field-error t-caption">
            <Icon name="alert" size={16} />
            {usernameError}
          </p>
        ) : null}
      </div>

      <div className="upload__submit">
        <Button disabled={!canAnalyze} onClick={() => void submit()}>Analyze</Button>
        <p className="t-caption upload__note">The CV, the job description and short excerpts of the candidate's public code are sent to Google's Gemini.</p>
      </div>
    </div>
  );
}
