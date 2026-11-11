import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../app/App";
import { NETWORK_ERROR, createAnalysis, getAnalysis, retryAnalysis } from "../api/client";
import { POLL_MS } from "../api/useAnalysis";
import { fixtureA } from "../score/fixtures";
import { ThemeProvider } from "../ThemeProvider";
import { bannerText } from "./errors";
import { STAGES, stageIndex } from "./ProgressCard";
import { FILE_SIZE_MESSAGE, FILE_TYPE_MESSAGE, USERNAME_MESSAGE, buildFormData, formatKb, validateFile, validateUsername } from "./validation";

// ---- a fake server behind a mocked fetch -------------------------------------------------------------------------
type Reply = { status?: number; body: unknown } | Error;
type Route = (method: string, path: string, init?: RequestInit) => Reply;

function installFetch(route: Route) {
  const fake = vi.fn(async (url: string, init?: RequestInit) => {
    const path = new URL(url).pathname;
    const reply = route((init?.method ?? "GET").toUpperCase(), path, init);
    if (reply instanceof Error) throw reply;
    return new Response(JSON.stringify(reply.body), { status: reply.status ?? 200, headers: { "Content-Type": "application/json" } });
  });
  vi.stubGlobal("fetch", fake);
  return fake;
}

const job = (state: string, stage: string, extra: object = {}) => ({ id: "j1", state, stage, result: null, error: null, ...extra });
const failedJob = (code: string, message = "api message", retryable = true) =>
  job("failed", "Reading the CV", { error: { code, message, retryable } });
const RESULT = fixtureA;

/** A server where POST /api/analyses answers {id:"j1"} and GET answers the given statuses in order (the last repeats). */
function server(statuses: object[], extra?: Route) {
  let next = 0;
  return installFetch((method, path, init) => {
    const other = extra?.(method, path, init);
    if (other) return other;
    if (method === "POST" && path === "/api/analyses") return { status: 202, body: { id: "j1" } };
    if (method === "GET" && path === "/api/analyses/j1") return { body: statuses[Math.min(next++, statuses.length - 1)] };
    return { status: 404, body: { message: "not found" } };
  });
}

const calls = (fake: ReturnType<typeof installFetch>, method: string, path: string) =>
  fake.mock.calls.filter(([url, init]) => new URL(url).pathname === path && ((init as RequestInit | undefined)?.method ?? "GET") === method);

// ---- rendering helpers -------------------------------------------------------------------------------------------
function renderUpload(url = "/recruiter/upload") {
  return render(
    <ThemeProvider>
      <MemoryRouter initialEntries={[url]}>
        <App />
      </MemoryRouter>
    </ThemeProvider>,
  );
}

const file = (name: string, size = 1024) => {
  const made = new File(["x"], name, { type: "application/octet-stream" });
  Object.defineProperty(made, "size", { value: size });
  return made;
};
const choose = (label: string, picked: File) => fireEvent.change(screen.getByLabelText(label), { target: { files: [picked] } });
const chooseJd = (name = "data-analyst-jd.pdf", size?: number) => choose("Job description file", file(name, size));
const chooseCv = (name = "priya-sample-cv.pdf", size?: number) => choose("Candidate CV file", file(name, size));
const analyze = () => screen.getByRole("button", { name: "Analyze" });
const flush = async (ms = 0) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });
async function submitValid() {
  chooseJd();
  chooseCv();
  fireEvent.click(analyze());
  await flush();
}
const steps = () => screen.getAllByRole("listitem").filter((li) => li.className.includes("progress-step"));
const stateOf = (li: HTMLElement) => /progress-step--(\w+)/.exec(li.className)![1];

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal("fetch", vi.fn(() => { throw new Error("a test forgot to install a fake server"); }));
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

// =============================================================================================================
describe("validation helpers", () => {
  it("accepts .pdf .docx .txt (any case) and rejects the rest", () => {
    for (const name of ["a.pdf", "a.DOCX", "My CV.Txt"]) expect(validateFile({ name, size: 10 })).toBeNull();
    for (const name of ["a.doc", "a.exe", "noextension", "a.pdf.exe"]) expect(validateFile({ name, size: 10 })).toBe(FILE_TYPE_MESSAGE);
  });
  it("allows up to 5 MB and rejects more", () => {
    expect(validateFile({ name: "a.pdf", size: 5 * 1024 * 1024 })).toBeNull();
    expect(validateFile({ name: "a.pdf", size: 5 * 1024 * 1024 + 1 })).toBe(FILE_SIZE_MESSAGE);
  });
  it("checks GitHub usernames", () => {
    for (const ok of ["", "  ", "octocat", "a", "a-b", "A1-b2", "a".repeat(39), " octocat "]) expect(validateUsername(ok)).toBeNull();
    for (const bad of ["-a", "a-", "a b", "a_b", "a".repeat(40), "a--", "x/y", "é"]) expect(validateUsername(bad)).toBe(USERNAME_MESSAGE);
  });
  it("formats sizes in KB", () => {
    expect(formatKb(184 * 1024)).toBe("184 KB");
    expect(formatKb(10)).toBe("1 KB");
  });
  it("builds FormData with exactly one of jd_file / jd_text and no empty username", () => {
    const jd = file("jd.pdf"), cv = file("cv.pdf");
    const asFile = buildFormData({ jdMode: "file", jdFile: jd, jdText: "ignored", cvFile: cv, username: "  " });
    expect([...asFile.keys()].sort()).toEqual(["cv_file", "jd_file"]);
    const asText = buildFormData({ jdMode: "text", jdFile: jd, jdText: "Must have Python", cvFile: cv, username: " octocat " });
    expect([...asText.keys()].sort()).toEqual(["cv_file", "github_username", "jd_text"]);
    expect(asText.get("jd_text")).toBe("Must have Python");
    expect(asText.get("github_username")).toBe("octocat");
  });
  it("maps stage text to a step index", () => {
    expect(STAGES).toHaveLength(5);
    expect(stageIndex("Reading the CV", "running")).toBe(1);
    expect(stageIndex("Reading the CV", "queued")).toBe(-1);
    expect(stageIndex("Analyzing, this can take a moment.", "running")).toBe(-1);
    expect(stageIndex("Done", "done")).toBe(5);
  });
});

// =============================================================================================================
describe("the empty and ready screen", () => {
  it("shows the title, the line, both cards and the notes", () => {
    renderUpload();
    expect(screen.getByRole("heading", { level: 1, name: "Upload" })).toBeInTheDocument();
    expect(screen.getByText("Add the job description and the candidate's CV. SkillVerify checks the CV's claims against their GitHub code.")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Job description" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Candidate CV" })).toBeInTheDocument();
    expect(screen.getAllByText(/Drop a PDF, Word or text file, or/)).toHaveLength(2);
    expect(screen.getAllByRole("button", { name: /browse/ })).toHaveLength(2);
    expect(screen.getByText("PDF, Word or text. Up to 5 MB each.")).toBeInTheDocument();
    expect(screen.getByLabelText("GitHub username (optional)")).toBeInTheDocument();
    expect(screen.getByText("Leave empty to use the one found in the CV.")).toBeInTheDocument();
    expect(screen.getByText("The CV, the job description and short excerpts of the candidate's public code are sent to Google's Gemini.")).toBeInTheDocument();
  });

  it("Analyze needs a job description AND a CV", () => {
    renderUpload();
    expect(analyze()).toBeDisabled();
    chooseJd();
    expect(analyze()).toBeDisabled();
    chooseCv();
    expect(analyze()).toBeEnabled();
  });

  it("a CV alone is not enough, and removing a file disables Analyze again", () => {
    renderUpload();
    chooseCv();
    expect(analyze()).toBeDisabled();
    chooseJd();
    fireEvent.click(screen.getByRole("button", { name: "Remove priya-sample-cv.pdf" }));
    expect(analyze()).toBeDisabled();
    expect(screen.getByLabelText("Candidate CV file")).toBeInTheDocument(); // the drop zone is back
  });

  it("a chosen file becomes a chip row with a mono name, the size and a labelled remove button", () => {
    renderUpload();
    chooseJd("data-analyst-jd.pdf", 184 * 1024);
    expect(screen.getByText("data-analyst-jd.pdf")).toBeInTheDocument();
    expect(screen.getByText("184 KB")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Remove data-analyst-jd.pdf" })).toBeInTheDocument();
    expect(screen.getByText("data-analyst-jd.pdf").closest(".sv-chip")).toHaveClass("t-code");
  });

  it("paste mode: the toggle switches to a text area, and only non-empty text counts", () => {
    renderUpload();
    const toggle = screen.getByRole("switch", { name: "Paste text" });
    expect(toggle).toHaveAttribute("aria-checked", "false"); // off = Upload file
    chooseCv();
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-checked", "true");
    const area = screen.getByPlaceholderText("Paste the job description here");
    expect(analyze()).toBeDisabled();
    fireEvent.change(area, { target: { value: "   \n " } });
    expect(analyze()).toBeDisabled();
    fireEvent.change(area, { target: { value: "Must have Python" } });
    expect(analyze()).toBeEnabled();
    fireEvent.click(toggle); // back to file mode: the pasted text no longer counts
    expect(analyze()).toBeDisabled();
  });
});

// =============================================================================================================
describe("client-side validation", () => {
  it("rejects an unsupported file type with the message and a red dashed zone", () => {
    renderUpload();
    chooseJd("notes.exe");
    expect(screen.getByText(FILE_TYPE_MESSAGE)).toBeInTheDocument();
    expect(document.querySelector(".dropzone")).toHaveAttribute("data-invalid", "true");
    expect(screen.queryByText("notes.exe")).toBeNull();
    chooseCv();
    expect(analyze()).toBeDisabled(); // no JD was accepted
  });

  it("rejects a file above 5 MB, per zone, and clears the error on a good file", () => {
    renderUpload();
    chooseJd("big.pdf", 5 * 1024 * 1024 + 1);
    chooseCv("cv.exe");
    expect(screen.getByText(FILE_SIZE_MESSAGE)).toBeInTheDocument();
    expect(screen.getByText(FILE_TYPE_MESSAGE)).toBeInTheDocument();
    chooseJd("ok.pdf");
    expect(screen.queryByText(FILE_SIZE_MESSAGE)).toBeNull();
    expect(screen.getByText(FILE_TYPE_MESSAGE)).toBeInTheDocument();
  });

  it("a dropped file is checked the same way", () => {
    renderUpload();
    const zone = document.querySelector(".dropzone")!;
    fireEvent.dragOver(zone);
    expect(zone).toHaveAttribute("data-dragging", "true");
    fireEvent.drop(zone, { dataTransfer: { files: [file("jd.docx")] } });
    expect(screen.getByText("jd.docx")).toBeInTheDocument();
  });

  it("a bad username shows the message on blur and blocks the request", async () => {
    const fake = server([job("done", "Done", { result: RESULT })]);
    renderUpload();
    chooseJd();
    chooseCv();
    const input = screen.getByLabelText("GitHub username (optional)");
    fireEvent.change(input, { target: { value: "-bad-" } });
    fireEvent.blur(input);
    expect(screen.getByText(USERNAME_MESSAGE)).toBeInTheDocument();
    fireEvent.click(analyze());
    await flush();
    expect(input).toHaveFocus();
    expect(fake).not.toHaveBeenCalled();
    fireEvent.change(input, { target: { value: "octocat" } });
    expect(screen.queryByText(USERNAME_MESSAGE)).toBeNull();
  });
});

// =============================================================================================================
describe("submitting", () => {
  it("sends jd_file + cv_file and leaves out an empty username", async () => {
    const fake = server([job("queued", "Analyzing, this can take a moment.")]);
    renderUpload();
    await submitValid();
    const [url, init] = calls(fake, "POST", "/api/analyses")[0];
    expect(url).toBe("http://localhost:8000/api/analyses");
    const body = (init as RequestInit).body as FormData;
    expect([...body.keys()].sort()).toEqual(["cv_file", "jd_file"]);
    expect((body.get("jd_file") as File).name).toBe("data-analyst-jd.pdf");
  });

  it("sends jd_text (never jd_file) in paste mode, with the typed username", async () => {
    const fake = server([job("queued", "Analyzing, this can take a moment.")]);
    renderUpload();
    fireEvent.click(screen.getByRole("switch", { name: "Paste text" }));
    fireEvent.change(screen.getByPlaceholderText("Paste the job description here"), { target: { value: "Must have Python" } });
    chooseCv();
    fireEvent.change(screen.getByLabelText("GitHub username (optional)"), { target: { value: "octocat" } });
    fireEvent.click(analyze());
    await flush();
    const body = (calls(fake, "POST", "/api/analyses")[0][1] as RequestInit).body as FormData;
    expect([...body.keys()].sort()).toEqual(["cv_file", "github_username", "jd_text"]);
    expect(body.get("jd_text")).toBe("Must have Python");
    expect(body.get("github_username")).toBe("octocat");
  });

  it("while analyzing the form is read-only and the progress card heading has focus", async () => {
    server([job("running", "Reading the CV")]);
    renderUpload();
    await submitValid();
    expect(screen.getByRole("heading", { name: "Analyzing, this can take a moment." })).toHaveFocus();
    expect(screen.queryByRole("button", { name: /^Remove / })).toBeNull();
    expect(screen.getByLabelText("GitHub username (optional)")).toBeDisabled();
    expect(analyze()).toBeDisabled();
    expect(screen.getByRole("switch", { name: "Paste text" })).toBeDisabled();
    expect(screen.getByText("data-analyst-jd.pdf")).toBeInTheDocument(); // the chips stay, without remove buttons
  });

  it("while analyzing, pasted text is read-only too", async () => {
    server([job("running", "Reading the CV")]);
    renderUpload();
    fireEvent.click(screen.getByRole("switch", { name: "Paste text" }));
    const area = screen.getByPlaceholderText("Paste the job description here");
    fireEvent.change(area, { target: { value: "Must have Python" } });
    chooseCv();
    fireEvent.click(analyze());
    await flush();
    expect(area).toHaveAttribute("readonly");
  });
});

// =============================================================================================================
describe("progress and polling", () => {
  it("maps the stage to the five steps and finishes on the score route", async () => {
    const fake = server([
      job("queued", "Analyzing, this can take a moment."),
      job("running", "Reading the CV"),
      job("running", "Scanning repositories"),
      job("done", "Done", { result: RESULT }),
    ]);
    renderUpload();
    await submitValid();
    expect(steps().map((li) => li.textContent?.replace(" (done)", ""))).toEqual([...STAGES]);
    expect(steps().map(stateOf)).toEqual(["pending", "pending", "pending", "pending", "pending"]); // queued: all pending

    await flush(POLL_MS);
    expect(steps().map(stateOf)).toEqual(["done", "current", "pending", "pending", "pending"]);
    expect(steps()[1]).toHaveAttribute("aria-current", "step");

    await flush(POLL_MS);
    expect(steps().map(stateOf)).toEqual(["done", "done", "done", "current", "pending"]);

    await flush(POLL_MS);
    await flush(0); // /recruiter/score/j1 loads the job
    expect(screen.getByRole("heading", { level: 1, name: "Data Analyst" })).toBeInTheDocument();
    expect(screen.getByTestId("fingerprint")).toHaveTextContent("44.4");
    expect(screen.getByRole("navigation", { name: "Progress" }).querySelector('[aria-current="step"]')).toHaveTextContent("Score");
    const before = calls(fake, "GET", "/api/analyses/j1").length;
    await flush(POLL_MS * 5);
    expect(calls(fake, "GET", "/api/analyses/j1").length).toBe(before); // polling stopped on done
  });

  it("polls every 1500 ms while running, and stops on failed", async () => {
    const fake = server([job("running", "Reading the job description"), job("running", "Reading the job description"), failedJob("ai_busy")]);
    renderUpload();
    await submitValid();
    expect(calls(fake, "GET", "/api/analyses/j1")).toHaveLength(1);
    await flush(POLL_MS - 1);
    expect(calls(fake, "GET", "/api/analyses/j1")).toHaveLength(1);
    await flush(1);
    expect(calls(fake, "GET", "/api/analyses/j1")).toHaveLength(2);
    await flush(POLL_MS);
    expect(calls(fake, "GET", "/api/analyses/j1")).toHaveLength(3);
    await flush(POLL_MS * 5);
    expect(calls(fake, "GET", "/api/analyses/j1")).toHaveLength(3);
  });

  it("stops polling when the screen goes away", async () => {
    const fake = server([job("running", "Reading the CV")]);
    const view = renderUpload();
    await submitValid();
    await flush(POLL_MS);
    const before = calls(fake, "GET", "/api/analyses/j1").length;
    view.unmount();
    await flush(POLL_MS * 5);
    expect(calls(fake, "GET", "/api/analyses/j1").length).toBe(before);
  });

  it("tolerates one failed poll but reports `network` after two in a row", async () => {
    let polls = 0;
    server([], (method, path) => {
      if (method === "GET" && path === "/api/analyses/j1") {
        polls += 1;
        if (polls === 2) return { body: job("running", "Reading the CV") };
        if (polls === 1 || polls >= 3) return new TypeError("offline");
      }
      return undefined as unknown as Reply;
    });
    renderUpload();
    await submitValid(); // poll 1 fails: tolerated, still analyzing
    expect(screen.getByRole("heading", { name: "Analyzing, this can take a moment." })).toBeInTheDocument();
    await flush(POLL_MS); // poll 2 works
    await flush(POLL_MS); // poll 3 fails (one in a row): tolerated
    expect(screen.queryByRole("alert")).toBeNull();
    await flush(POLL_MS); // poll 4 fails (two in a row)
    expect(screen.getByText("Can't reach the SkillVerify server.")).toBeInTheDocument();
  });
});

// =============================================================================================================
describe("errors", () => {
  const TABLE: [string, string, string, boolean][] = [
    ["ai_busy", "The AI service is busy right now.", "Please try again.", true],
    ["ai_rate_limited", "The AI service's request limit was reached.", "Wait a little while and try again.", true],
    ["ai_all_limited", "All available AI models have reached their limits.", "Wait a few minutes and try again.", true],
    ["ai_not_configured", "The AI service is not set up on this server.", "Check the server configuration.", false],
    ["github_user_not_found", "That GitHub username was not found.", "Check the spelling and try again.", false],
    ["github_unavailable", "GitHub could not be reached or its request limit was reached.", "Wait a few minutes and try again.", true],
    ["cv_unreadable", "We couldn't read the CV.", "Try a different file.", false],
    ["jd_unreadable", "We couldn't read the job description.", "Try a different file or paste the text.", false],
    ["timeout", "This is taking too long.", "Please try again.", true],
    ["internal_error", "Something went wrong on the server.", "Please try again.", true],
  ];

  it.each(TABLE)("%s shows its exact title and body, with Retry only where listed", async (code, title, body, retry) => {
    server([failedJob(code, "server text that must not be used", retry)]);
    renderUpload();
    await submitValid();
    const alert = screen.getByRole("alert");
    expect(within(alert).getByText(title)).toBeInTheDocument();
    expect(within(alert).getByText(body)).toBeInTheDocument();
    expect(within(alert).queryByRole("button", { name: "Retry" }) !== null).toBe(retry);
    expect(screen.queryByText("server text that must not be used")).toBeNull();
    // the form is usable again
    expect(analyze()).toBeEnabled();
    expect(screen.getByLabelText("GitHub username (optional)")).toBeEnabled();
    expect(screen.queryByRole("heading", { name: "Analyzing, this can take a moment." })).toBeNull();
  });

  it("a rate limit with a backup model available says Retry will use it (on screen too)", async () => {
    server([job("failed", "Reading the CV", { error: { code: "ai_rate_limited", message: "x", retryable: true, fallback_available: true } })]);
    renderUpload();
    await submitValid();
    const alert = screen.getByRole("alert");
    expect(within(alert).getByText("The AI service's request limit was reached.")).toBeInTheDocument();
    expect(within(alert).getByText("Retry will use a backup AI model.")).toBeInTheDocument();
    expect(within(alert).queryByText("Wait a little while and try again.")).toBeNull();
    expect(within(alert).getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });

  it("a rate limit with fallback_available false or missing says to wait", () => {
    for (const extra of [{ fallback_available: false }, {}]) {
      const text = bannerText({ code: "ai_rate_limited", message: "x", retryable: true, ...extra });
      expect(text).toEqual({ title: "The AI service's request limit was reached.", message: "Wait a little while and try again.", retry: true });
    }
  });

  it("the banner (not the field) gets focus after an error", async () => {
    server([failedJob("ai_busy")]);
    renderUpload();
    await submitValid();
    expect(screen.getByRole("alert").parentElement).toHaveFocus();
  });

  it("github_user_not_found gives the username field a red border and focuses it", async () => {
    server([failedJob("github_user_not_found", "x", false)]);
    renderUpload();
    chooseJd();
    chooseCv();
    fireEvent.change(screen.getByLabelText("GitHub username (optional)"), { target: { value: "priya-sample" } });
    fireEvent.click(analyze());
    await flush();
    const input = screen.getByLabelText("GitHub username (optional)");
    expect(input).toHaveClass("sv-input--error");
    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(input).toHaveFocus();
    expect(input).toHaveValue("priya-sample");
  });

  it("invalid_input shows the API's message, without Retry", async () => {
    server([], () => ({ status: 422, body: { error: { code: "invalid_input", message: "The CV file is required.", retryable: false } } }));
    renderUpload();
    await submitValid();
    const alert = screen.getByRole("alert");
    expect(within(alert).getByText("The CV file is required.")).toBeInTheDocument();
    expect(within(alert).queryByRole("button", { name: "Retry" })).toBeNull();
  });

  it("an unknown code uses the API message under 'Something went wrong.'", async () => {
    server([failedJob("brand_new_code", "A new kind of problem.", true)]);
    renderUpload();
    await submitValid();
    const alert = screen.getByRole("alert");
    expect(within(alert).getByText("Something went wrong.")).toBeInTheDocument();
    expect(within(alert).getByText("A new kind of problem.")).toBeInTheDocument();
    expect(within(alert).queryByRole("button", { name: "Retry" })).toBeNull();
  });

  it("a plain {message} error body (such as a 404) is shown as an unknown error", async () => {
    server([], () => ({ status: 503, body: { message: "Please wait." } }));
    renderUpload();
    await submitValid();
    expect(within(screen.getByRole("alert")).getByText("Please wait.")).toBeInTheDocument();
  });

  it("a thrown fetch shows the network banner, and Retry submits again", async () => {
    let up = false;
    const fake = installFetch((method, path) => {
      if (!up) return new TypeError("Failed to fetch");
      if (method === "POST" && path === "/api/analyses") return { status: 202, body: { id: "j1" } };
      return { body: job("running", "Reading the CV") };
    });
    renderUpload();
    await submitValid();
    const alert = screen.getByRole("alert");
    expect(within(alert).getByText("Can't reach the SkillVerify server.")).toBeInTheDocument();
    expect(within(alert).getByText("Make sure it is running, then try again.")).toBeInTheDocument();
    expect(calls(fake, "POST", "/api/analyses")).toHaveLength(1);
    up = true;
    fireEvent.click(within(alert).getByRole("button", { name: "Retry" }));
    await flush();
    expect(calls(fake, "POST", "/api/analyses")).toHaveLength(2); // re-submitted
    expect(screen.getByRole("heading", { name: "Analyzing, this can take a moment." })).toBeInTheDocument();
  });

  it("Retry calls the retry route and resumes the analyzing state", async () => {
    let retried = false;
    const fake = installFetch((method, path) => {
      if (method === "POST" && path === "/api/analyses") return { status: 202, body: { id: "j1" } };
      if (method === "POST" && path === "/api/analyses/j1/retry") {
        retried = true;
        return { status: 202, body: { id: "j1" } };
      }
      return { body: retried ? job("running", "Reading the CV") : failedJob("ai_busy") };
    });
    renderUpload();
    await submitValid();
    fireEvent.click(within(screen.getByRole("alert")).getByRole("button", { name: "Retry" }));
    await flush();
    expect(calls(fake, "POST", "/api/analyses/j1/retry")).toHaveLength(1);
    expect(calls(fake, "POST", "/api/analyses")).toHaveLength(1); // not re-submitted
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByRole("heading", { name: "Analyzing, this can take a moment." })).toBeInTheDocument();
    expect(steps().map(stateOf)[1]).toBe("current");
  });

  it("a refused retry (409) is shown, without Retry", async () => {
    installFetch((method, path) => {
      if (method === "POST" && path === "/api/analyses") return { status: 202, body: { id: "j1" } };
      if (method === "POST" && path === "/api/analyses/j1/retry") return { status: 409, body: { message: "Only a retryable analysis can be retried." } };
      return { body: failedJob("ai_busy") };
    });
    renderUpload();
    await submitValid();
    fireEvent.click(within(screen.getByRole("alert")).getByRole("button", { name: "Retry" }));
    await flush();
    const alert = screen.getByRole("alert");
    expect(within(alert).getByText("Only a retryable analysis can be retried.")).toBeInTheDocument();
    expect(within(alert).queryByRole("button", { name: "Retry" })).toBeNull();
  });

  it("bannerText has a row for every listed code", () => {
    expect(TABLE.every(([code, title]) => bannerText({ code, message: "", retryable: false }).title === title)).toBe(true);
    expect(bannerText(NETWORK_ERROR)).toMatchObject({ title: "Can't reach the SkillVerify server.", retry: true });
  });
});

// =============================================================================================================
describe("the API client", () => {
  it("uses the base URL and never logs", async () => {
    const log = vi.spyOn(console, "log").mockImplementation(() => undefined);
    const fake = installFetch(() => ({ body: { id: "x" } }));
    const form = new FormData();
    form.append("jd_text", "secret job text");
    expect(await createAnalysis(form)).toEqual({ ok: true, value: { id: "x" } });
    expect(fake.mock.calls[0][0]).toBe("http://localhost:8000/api/analyses");
    expect(log).not.toHaveBeenCalled();
    log.mockRestore();
  });

  it("returns the network error when fetch throws", async () => {
    installFetch(() => new TypeError("Failed to fetch"));
    expect(await getAnalysis("j1")).toEqual({ ok: false, error: NETWORK_ERROR });
    expect(await retryAnalysis("j1")).toEqual({ ok: false, error: NETWORK_ERROR });
  });

  it("turns error bodies into ApiError objects", async () => {
    installFetch((_m, path) => (path.endsWith("/a") ? { status: 422, body: { error: { code: "invalid_input", message: "m", retryable: false } } } : { status: 404, body: { message: "gone" } }));
    expect(await getAnalysis("a")).toEqual({ ok: false, error: { code: "invalid_input", message: "m", retryable: false } });
    expect(await getAnalysis("b")).toEqual({ ok: false, error: { code: "unknown", message: "gone", retryable: false } });
  });

  it("copes with an answer that is not JSON", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("<html>", { status: 502 })));
    expect(await getAnalysis("j1")).toEqual({ ok: false, error: { code: "unknown", message: "The request did not work.", retryable: false } });
    vi.stubGlobal("fetch", vi.fn(async () => new Response("[1]", { status: 200 })));
    expect((await getAnalysis("j1")).ok).toBe(false);
  });
});
