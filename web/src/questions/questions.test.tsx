import { act, cleanup, fireEvent, render, renderHook, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../app/App";
import { getQuestions, replaceQuestion, startQuestions, retryQuestions } from "../api/client";
import { POLL_MS } from "../api/useAnalysis";
import { useQuestions } from "../api/useQuestions";
import { fixtureA, fixtureB, metaB } from "../score/fixtures";
import { ScoreView } from "../score/ScoreView";
import { ThemeProvider } from "../ThemeProvider";
import { groupSkipped, languageLabel, normalise, stageText } from "./format";
import * as F from "./fixtures";
import { RichText } from "./RichText";

// ---- a fake server behind a mocked fetch -------------------------------------------------------------------------
type Reply = { status?: number; body: unknown } | Error;
type Seq = Reply | Promise<Reply> | Reply[]; // a list is used in order, the last one repeats

type Server = {
  analysis?: Seq;
  questions?: Seq;
  start?: Seq;
  retry?: Seq;
  replace?: Seq;
};

function pick(seq: Seq | undefined, counters: Map<unknown, number>): Reply | Promise<Reply> {
  if (seq === undefined) return { status: 404, body: { message: "not found" } };
  if (!Array.isArray(seq)) return seq;
  const n = counters.get(seq) ?? 0;
  counters.set(seq, n + 1);
  return seq[Math.min(n, seq.length - 1)];
}

const ok = (body: unknown): Reply => ({ status: 200, body });

function install(server: Server) {
  const counters = new Map<unknown, number>();
  const calls: { method: string; path: string; body: string | undefined }[] = [];
  const fake = vi.fn(async (url: string, init?: RequestInit) => {
    const path = new URL(url).pathname;
    const method = (init?.method ?? "GET").toUpperCase();
    calls.push({ method, path, body: typeof init?.body === "string" ? init.body : undefined });
    let reply: Reply | Promise<Reply>;
    if (path === "/api/analyses/j1" && method === "GET") reply = pick(server.analysis ?? ok(F.ANALYSIS), counters);
    else if (path === "/api/analyses/j1/questions" && method === "GET") reply = pick(server.questions, counters);
    else if (path === "/api/analyses/j1/questions" && method === "POST") reply = pick(server.start, counters);
    else if (path === "/api/analyses/j1/questions/retry") reply = pick(server.retry, counters);
    else if (path === "/api/analyses/j1/questions/replace") reply = pick(server.replace, counters);
    else reply = { status: 404, body: { message: "not found" } };
    reply = await reply;
    if (reply instanceof Error) throw reply;
    return new Response(JSON.stringify(reply.body), { status: reply.status ?? 200, headers: { "Content-Type": "application/json" } });
  });
  vi.stubGlobal("fetch", fake);
  return { fake, calls, count: (method: string, path: string) => calls.filter((c) => c.method === method && c.path === path).length };
}

const done = (run: typeof F.RUN_OK): Server => ({ questions: ok(F.statusDone(run)) });

function renderAt(url = "/recruiter/questions/j1") {
  return render(
    <ThemeProvider>
      <MemoryRouter initialEntries={[url]}><App /></MemoryRouter>
    </ThemeProvider>,
  );
}

async function ready(server: Server = done(F.RUN_OK), url?: string) {
  const net = install(server);
  renderAt(url);
  await screen.findByText(/^(\d+ questions?, \d+ approved|0 questions)$/);
  return net;
}

beforeEach(() => {
  window.sessionStorage.clear();
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  window.sessionStorage.clear();
});

const cards = () => screen.queryAllByRole("article");
const card = (n: number) => cards()[n - 1];
const counter = () => document.querySelector(".review__counter")!.textContent;
const approveBtn = (n: number) => screen.getByRole("button", { name: `Approve question ${n}` });
const tick = (n: number) => screen.getByRole("button", { name: `Approved. Press to undo approval of question ${n}` });
const create = () => screen.getByRole("button", { name: "Create Assessment" });
const preview = () => screen.getByRole("button", { name: "Preview as student" });

// =============================================================================================================
describe("API client", () => {
  it("calls the four question routes with the right method and body", async () => {
    const net = install({ questions: ok(F.statusIdle), start: ok(F.statusQueued), retry: ok(F.statusQueued), replace: [F.replaceOk(), F.replaceOk()] });
    await getQuestions("j1");
    await startQuestions("j1");
    await retryQuestions("j1");
    const withAfter = await replaceQuestion("j1", "q_1");
    const without = await replaceQuestion("j1");
    expect(net.calls.map((c) => `${c.method} ${c.path}`)).toEqual([
      "GET /api/analyses/j1/questions", "POST /api/analyses/j1/questions", "POST /api/analyses/j1/questions/retry",
      "POST /api/analyses/j1/questions/replace", "POST /api/analyses/j1/questions/replace",
    ]);
    expect(net.calls[3].body).toBe(JSON.stringify({ after: "q_1" }));
    expect(net.calls[4].body).toBeUndefined();
    expect(withAfter.ok && without.ok).toBe(true);
  });

  it("keeps fallback_available and stage on an error, and turns a thrown fetch into `network`", async () => {
    install({ replace: { status: 503, body: { error: { code: "ai_rate_limited", message: "m", retryable: true, fallback_available: true, stage: "Scoring" } } } });
    const failed = await replaceQuestion("j1");
    expect(failed).toEqual({ ok: false, error: { code: "ai_rate_limited", message: "m", retryable: true, fallback_available: true, stage: "Scoring" } });
    install({ replace: new TypeError("offline") });
    const offline = await replaceQuestion("j1");
    expect(!offline.ok && offline.error.code).toBe("network");
  });
});

// =============================================================================================================
describe("useQuestions", () => {
  const advance = async (ms: number) => {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(ms);
    });
  };

  beforeEach(() => vi.useFakeTimers());

  it("starts an idle run, then polls every 1500 ms and stops when it is done", async () => {
    const net = install({
      questions: [ok(F.statusIdle), ok(F.statusRunning("Reading files")), ok(F.statusDone(F.RUN_OK))],
      start: ok(F.statusQueued),
    });
    const { result } = renderHook(() => useQuestions("j1"));
    await advance(0);
    expect(net.count("POST", "/api/analyses/j1/questions")).toBe(1);
    expect(result.current.status?.state).toBe("queued");
    await advance(POLL_MS - 1);
    expect(net.count("GET", "/api/analyses/j1/questions")).toBe(1);
    await advance(1);
    expect(result.current.status?.state).toBe("running");
    await advance(POLL_MS);
    expect(result.current.status?.state).toBe("done");
    await advance(POLL_MS * 5);
    expect(net.count("GET", "/api/analyses/j1/questions")).toBe(3); // no more polls after done
  });

  it("does not start a run that is already running or done", async () => {
    const net = install({ questions: ok(F.statusDone(F.RUN_OK)) });
    renderHook(() => useQuestions("j1"));
    await advance(0);
    expect(net.count("POST", "/api/analyses/j1/questions")).toBe(0);
  });

  it("a 409 'already started' just reads the run again", async () => {
    const started = { status: 409, body: { error: { code: "questions_already_started", message: "x", retryable: false } } };
    const net = install({ questions: [ok(F.statusIdle), ok(F.statusRunning("Choosing code"))], start: started });
    const { result } = renderHook(() => useQuestions("j1"));
    await advance(0);
    expect(net.count("POST", "/api/analyses/j1/questions")).toBe(1);
    expect(result.current.status?.state).toBe("running");
    expect(result.current.status?.error).toBeNull();
  });

  it("another start error is a failed run", async () => {
    install({ questions: ok(F.statusIdle), start: { status: 409, body: { error: { code: "analysis_not_ready", message: "x", retryable: false } } } });
    const { result } = renderHook(() => useQuestions("j1"));
    await advance(0);
    expect(result.current.status?.state).toBe("failed");
    expect(result.current.status?.error?.code).toBe("analysis_not_ready");
    install({ questions: ok(F.statusIdle), start: new TypeError("offline") });
    const second = renderHook(() => useQuestions("j1"));
    await advance(0);
    expect(second.result.current.status?.error?.code).toBe("network");
  });

  it("stops polling when it fails and when the screen is left", async () => {
    const net = install({ questions: [ok(F.statusRunning("Choosing code")), ok(F.statusFailed(F.E_TIMEOUT))] });
    const first = renderHook(() => useQuestions("j1"));
    await advance(POLL_MS * 6);
    expect(first.result.current.status?.state).toBe("failed");
    expect(net.count("GET", "/api/analyses/j1/questions")).toBe(2);
    first.unmount();
    const other = install({ questions: ok(F.statusRunning("Choosing code")) });
    const second = renderHook(() => useQuestions("j1"));
    await advance(POLL_MS * 2);
    const before = other.count("GET", "/api/analyses/j1/questions");
    second.unmount();
    await advance(POLL_MS * 5);
    expect(other.count("GET", "/api/analyses/j1/questions")).toBe(before);
  });

  it("tolerates one failed poll and reports `network` on the second in a row", async () => {
    install({ questions: [ok(F.statusRunning("Choosing code")), new TypeError("offline"), ok(F.statusRunning("Reading files")), new TypeError("a"), new TypeError("b")] });
    const { result } = renderHook(() => useQuestions("j1"));
    await advance(0);
    await advance(POLL_MS); // the failed poll: still running
    expect(result.current.status?.state).toBe("running");
    await advance(POLL_MS); // recovered
    expect(result.current.status?.stage).toBe("Reading files");
    await advance(POLL_MS); // one miss again
    expect(result.current.status?.state).toBe("running");
    await advance(POLL_MS); // second miss in a row
    expect(result.current.status?.state).toBe("failed");
    expect(result.current.status?.error?.code).toBe("network");
  });

  it("a 404 is 'not found'", async () => {
    install({ questions: { status: 404, body: { message: "That analysis was not found." } } });
    const { result } = renderHook(() => useQuestions("j1"));
    await advance(0);
    expect(result.current.notFound).toBe(true);
    expect(result.current.status).toBeNull();
  });

  it("asks for nothing while the id is null", async () => {
    const net = install({});
    renderHook(() => useQuestions(null));
    await advance(POLL_MS * 2);
    expect(net.fake).not.toHaveBeenCalled();
  });

  it("retry asks the server to retry, then polls again; a network failure only polls again", async () => {
    const net = install({ questions: [ok(F.statusFailed(F.E_TIMEOUT)), ok(F.statusQueued)], retry: ok(F.statusQueued) });
    const first = renderHook(() => useQuestions("j1"));
    await advance(0);
    act(() => first.result.current.retry());
    await advance(0);
    expect(net.count("POST", "/api/analyses/j1/questions/retry")).toBe(1);
    expect(first.result.current.status?.state).toBe("queued");
    first.unmount();
    const offline = install({ questions: [new TypeError("a"), new TypeError("b"), ok(F.statusQueued)], retry: ok(F.statusQueued) });
    const second = renderHook(() => useQuestions("j1"));
    await advance(POLL_MS + 10);
    expect(second.result.current.status?.error?.code).toBe("network");
    act(() => second.result.current.retry());
    await advance(0);
    expect(offline.count("POST", "/api/analyses/j1/questions/retry")).toBe(0);
    expect(second.result.current.status?.state).toBe("queued");
  });
});

// =============================================================================================================
describe("states of the screen", () => {
  it("idle starts the run and shows the loading state", async () => {
    const net = install({ questions: ok(F.statusIdle), start: ok(F.statusQueued) });
    renderAt();
    expect(await screen.findByText("Choosing code...")).toBeInTheDocument();
    await waitFor(() => expect(net.count("POST", "/api/analyses/j1/questions")).toBe(1));
  });

  it.each([
    ["Choosing code", "Choosing code..."],
    ["Reading files", "Reading files..."],
    ["Writing questions", "Writing questions from the candidate's code..."],
    ["Checking questions", "Checking questions..."],
  ])("loading shows the step text for %s", async (stage, text) => {
    install({ questions: ok(F.statusRunning(stage)) });
    renderAt();
    expect(await screen.findByText(text)).toBeInTheDocument();
    expect(screen.getByText("This can take a minute or two.")).toBeInTheDocument();
    expect(counter()).toBe("Generating questions");
    expect(screen.getByRole("heading", { level: 1, name: "Priya Raman" })).toBeInTheDocument();
    expect(screen.getByText("Data Analyst")).toBeInTheDocument();
    expect(document.querySelector(".review__score")).toHaveTextContent("Score 44.4");
    expect(document.querySelectorAll(".skeleton-card").length).toBe(3);
    expect(create()).toBeDisabled();
    expect(preview()).toBeDisabled();
  });

  it("step text falls back to the first step for an unknown stage", () => {
    expect(stageText("Something else")).toBe("Choosing code...");
    expect(stageText(null)).toBe("Choosing code...");
  });

  it("while the analysis itself loads there is a skeleton and nothing else is asked for", async () => {
    const net = install({ analysis: new Promise<Reply>(() => {}) });
    renderAt();
    expect(await screen.findByText("This can take a minute or two.")).toBeInTheDocument();
    expect(net.count("GET", "/api/analyses/j1/questions")).toBe(0);
  });

  it.each([
    ["ai_rate_limited with a backup", F.E_RATE_FALLBACK, ["The AI service's request limit was reached.", "Retry will use a backup AI model."], true],
    ["ai_rate_limited without a backup", F.E_RATE_NONE, ["The AI service's request limit was reached.", "Wait a little while and try again."], true],
    ["ai_all_limited", F.E_ALL_LIMITED, ["All available AI models have reached their limits.", "Wait a few minutes and try again."], true],
    ["timeout", F.E_TIMEOUT, ["This is taking too long.", "Please try again."], true],
    ["github_unavailable", F.E_GITHUB, ["GitHub could not be reached or its request limit was reached.", "Wait a few minutes and try again."], true],
    ["internal_error", F.E_INTERNAL, ["Something went wrong on the server.", "Please try again."], true],
  ])("a failed run (%s) shows the banner over the skeletons", async (_name, error, [title, message], retry) => {
    install({ questions: ok(F.statusFailed(error)) });
    renderAt();
    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText(title)).toBeInTheDocument();
    expect(within(alert).getByText(message)).toBeInTheDocument();
    expect(!!within(alert).queryByRole("button", { name: "Retry" })).toBe(retry);
    expect(document.querySelectorAll(".skeleton-card").length).toBe(3);
    expect(counter()).toBe("Generating questions");
    expect(create()).toBeDisabled();
  });

  it("analysis_not_ready says so, has no Retry and links back to Score", async () => {
    install({ questions: ok(F.statusFailed(F.E_NOT_READY)) });
    renderAt();
    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText("The analysis isn't finished yet.")).toBeInTheDocument();
    expect(within(alert).queryByRole("button", { name: "Retry" })).toBeNull();
    expect(screen.getByRole("link", { name: "Back to score" })).toHaveAttribute("href", "/recruiter/score");
  });

  it("a non-retryable failure has no Retry button", async () => {
    install({ questions: ok(F.statusFailed({ ...F.E_TIMEOUT, retryable: false })) });
    renderAt();
    const alert = await screen.findByRole("alert");
    expect(within(alert).queryByRole("button", { name: "Retry" })).toBeNull();
  });

  it("Retry asks the server to retry and resumes loading", async () => {
    const net = install({ questions: [ok(F.statusFailed(F.E_TIMEOUT)), ok(F.statusRunning("Reading files"))], retry: ok(F.statusQueued) });
    renderAt();
    fireEvent.click(await screen.findByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Reading files...")).toBeInTheDocument();
    expect(net.count("POST", "/api/analyses/j1/questions/retry")).toBe(1);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("a network failure shows the network banner", async () => {
    install({ questions: new TypeError("offline") });
    renderAt();
    expect(await screen.findByText("Can't reach the SkillVerify server.", {}, { timeout: 4000 })).toBeInTheDocument();
  });

  it("not found: a 404 for the questions, a missing analysis and an unfinished analysis all show the calm card", async () => {
    install({ questions: { status: 404, body: { message: "gone" } } });
    renderAt();
    expect(await screen.findByText("That analysis was not found. It may have expired.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Back to upload" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Upload" })).toBeInTheDocument();
    cleanup();
    const missing = install({ analysis: { status: 404, body: { message: "gone" } } });
    renderAt();
    expect(await screen.findByText("That analysis was not found. It may have expired.")).toBeInTheDocument();
    expect(missing.count("GET", "/api/analyses/j1/questions")).toBe(0);
    cleanup();
    install({ analysis: ok({ ...F.ANALYSIS, state: "failed", result: null, error: { code: "ai_busy", message: "x", retryable: true } }) });
    renderAt();
    expect(await screen.findByText("That analysis was not found. It may have expired.")).toBeInTheDocument();
  });

  it("the bare /recruiter/questions asks to start from an analysis", async () => {
    install({});
    renderAt("/recruiter/questions");
    expect(screen.getByText("Start from an analysis to review questions.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Go to Upload" })).toHaveAttribute("href", "/recruiter/upload");
  });

  it("status none shows the server message, 0 questions, Back to score and disabled buttons", async () => {
    install(done(F.RUN_NONE));
    renderAt();
    expect(await screen.findByText(F.RUN_NONE.message!)).toBeInTheDocument();
    expect(counter()).toBe("0 questions");
    expect(create()).toBeDisabled();
    expect(preview()).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Back to score" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Data Analyst" })).toBeInTheDocument();
  });

  it("the top bar shows step 3 active and steps 1 and 2 done", async () => {
    await ready();
    const items = within(screen.getByRole("navigation", { name: "Progress" })).getAllByRole("listitem");
    expect(items[2]).toHaveAttribute("aria-current", "step");
    expect(items[0]).toHaveTextContent("(completed)");
    expect(items[1]).toHaveTextContent("(completed)");
  });
});

// =============================================================================================================
describe("the cards", () => {
  it("shows the three questions with kind pills, numbering, starter code and the why box", async () => {
    await ready();
    expect(cards()).toHaveLength(3);
    expect(counter()).toBe("3 questions, 0 approved");
    expect(within(card(1)).getByText("Question 1")).toBeInTheDocument();
    expect(within(card(1)).getByText("Fix the bug")).toBeInTheDocument();
    expect(within(card(2)).getByText("Write code")).toBeInTheDocument();
    expect(card(1).querySelector(".why__skill")).toHaveTextContent("Python");
    expect(card(3).querySelector(".why__skill")).toHaveTextContent("SQL");
    const why = within(card(1)).getByRole("region", { name: "Why this question" });
    expect(within(why).getByText("calendar-utils")).toBeInTheDocument();
    expect(within(why).getByText("libs/dates.py")).toBeInTheDocument();
    expect(within(why).getByText("Must-have")).toBeInTheDocument();
    expect(within(why).getByText(F.qFix.why.reason)).toBeInTheDocument();
    expect(within(why).getByText("Only you see this")).toBeInTheDocument();
  });

  it("never shows reference notes anywhere", async () => {
    await ready();
    expect(document.body.textContent).not.toContain("SECRET-NOTES");
  });

  it("shows the Nice-to-have tag and the server's sentence unchanged", async () => {
    await ready(done(F.RUN_FOUR));
    const why = within(card(4)).getByRole("region", { name: "Why this question" });
    expect(within(why).getByText("Nice-to-have")).toBeInTheDocument();
    expect(within(why).getByText(F.qRedis.why.reason)).toBeInTheDocument();
  });

  it("renders inline code, the numbered list and plain paragraphs", async () => {
    await ready();
    const text = card(2).querySelector(".qcard__text")!;
    expect(text.querySelectorAll("ol > li")).toHaveLength(4);
    expect(text.querySelector("code")).toHaveTextContent("getlist(self, key, type=None)");
    expect(within(card(1) as HTMLElement).getAllByText("_dim").length).toBeGreaterThan(0);
  });

  it("starter code: label, language, first 8 lines, Show all / Show fewer, omitted when empty", async () => {
    await ready();
    const block = card(2).querySelector(".code__box")!;
    expect(within(card(2)).getByText("Starter code")).toBeInTheDocument();
    expect(card(2).querySelector(".code__lang")).toHaveTextContent("Python");
    expect(block.textContent!.split("\n")).toHaveLength(8);
    const more = within(card(2)).getByRole("button", { name: "Show all 14 lines" });
    expect(more).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(more);
    expect(block.textContent!.split("\n")).toHaveLength(14);
    const fewer = within(card(2)).getByRole("button", { name: "Show fewer" });
    expect(fewer).toHaveAttribute("aria-expanded", "true");
    fireEvent.click(fewer);
    expect(block.textContent!.split("\n")).toHaveLength(8);
    expect(within(card(1)).queryByRole("button", { name: /Show all/ })).toBeNull(); // 6 lines: no button
    cleanup();
    const noStarter = { ...F.RUN_OK, questions: [{ ...F.qSql, starter_code: "" }, F.qWrite, F.qFix] };
    await ready(done(noStarter));
    expect(within(card(1)).queryByText("Starter code")).toBeNull();
  });

  it("the code block keeps ligatures off, scrolls sideways and keeps its spaces", async () => {
    await ready();
    const css = (await import("../styles/questions.css?raw")).default as string;
    expect(css).toMatch(/\.code__box[^}]*white-space:\s*pre/);
    expect(css).toMatch(/\.code__box[^}]*overflow-x:\s*auto/);
    expect(css).toMatch(/\.code__box[^}]*tab-size:\s*4/);
    expect(css).toMatch(/\.code__box[^}]*font-variant-ligatures:\s*none/);
    expect(card(1).querySelector(".code__box")).toBeInTheDocument();
  });

  it("language labels", () => {
    expect(languageLabel("python")).toBe("Python");
    expect(languageLabel("sql")).toBe("SQL");
    expect(languageLabel("typescript")).toBe("TypeScript");
    expect(languageLabel("javascript")).toBe("JavaScript");
    expect(languageLabel("bash")).toBe("Bash");
    expect(languageLabel("ruby")).toBe("Ruby");
    expect(languageLabel("")).toBe("");
  });
});

// =============================================================================================================
describe("the safe text renderer", () => {
  it("splits paragraphs, keeps line breaks, makes inline code and an ordered list", () => {
    const { container } = render(<RichText text={"First `a` line\nsecond line\n\nNext paragraph:\n1. one\n2. two `x`\n\nAfter"} />);
    expect(container.querySelectorAll("p")).toHaveLength(3);
    expect(container.querySelectorAll("br")).toHaveLength(1);
    expect(container.querySelectorAll("code")).toHaveLength(2);
    expect(container.querySelectorAll("ol > li")).toHaveLength(2);
  });
  it("starts a list at its own number", () => {
    const { container } = render(<RichText text={"3. three\n4. four"} />);
    expect(container.querySelector("ol")).toHaveAttribute("start", "3");
  });
  it("keeps HTML literal and never injects it", () => {
    const { container } = render(<RichText text={"Use <script>alert(1)</script> and <b>bold</b> and `<i>x</i>`"} />);
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("b")).toBeNull();
    expect(container.textContent).toContain("<script>alert(1)</script>");
    expect(container.querySelector("code")?.textContent).toBe("<i>x</i>");
  });
  it("leaves an unclosed backtick as text", () => {
    const { container } = render(<RichText text={"a ` b"} />);
    expect(container.querySelector("code")).toBeNull();
    expect(container.textContent).toBe("a ` b");
  });
});

// =============================================================================================================
describe("approving", () => {
  it("Approve becomes the green tick and back, with the right names and counter", async () => {
    await ready();
    fireEvent.click(approveBtn(1));
    const t = tick(1);
    expect(t).toHaveAttribute("aria-pressed", "true");
    expect(t).toHaveAttribute("title", "Undo approval");
    expect(counter()).toBe("3 questions, 1 approved");
    expect(screen.queryByRole("button", { name: "Approve question 1" })).toBeNull();
    fireEvent.click(t);
    expect(approveBtn(1)).toBeInTheDocument();
    expect(counter()).toBe("3 questions, 0 approved");
  });

  it("uses the approve tokens only for the tick", async () => {
    const css = (await import("../styles/questions.css?raw")).default as string;
    const all = import.meta.glob("../styles/*.css", { query: "?raw", import: "default", eager: true }) as Record<string, string>;
    const users = Object.entries(all).filter(([, text]) => /var\(--approve-/.test(text));
    expect(users.map(([name]) => name)).toEqual(["../styles/questions.css"]);
    const lines = css.split("\n").filter((l) => /--approve-/.test(l));
    expect(lines.length).toBeGreaterThan(0);
    expect(css.slice(css.indexOf(".tick {"))).toContain("--approve-ink");
  });

  it("Create Assessment is enabled exactly when 3 are approved", async () => {
    await ready();
    expect(create()).toBeDisabled();
    fireEvent.click(approveBtn(1));
    fireEvent.click(approveBtn(2));
    expect(create()).toBeDisabled();
    fireEvent.click(approveBtn(3));
    expect(create()).toBeEnabled();
    fireEvent.click(tick(3));
    expect(create()).toBeDisabled();
  });

  it("Preview is enabled from 1 approved", async () => {
    await ready();
    expect(preview()).toBeDisabled();
    fireEvent.click(approveBtn(2));
    expect(preview()).toBeEnabled();
  });

  it("Create Assessment stores the approved questions (with edited text) and goes to the placeholder", async () => {
    await ready();
    fireEvent.click(within(card(1)).getByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Question 1 text" }), { target: { value: "My own text for the first question" } });
    fireEvent.click(within(card(1)).getByRole("button", { name: "Done" }));
    for (const n of [1, 2, 3]) fireEvent.click(approveBtn(n));
    fireEvent.click(create());
    expect(await screen.findByRole("heading", { level: 1, name: "Assessment created." })).toBeInTheDocument();
    expect(screen.getByText("The student screen comes in a later step.")).toBeInTheDocument();
    const stored = JSON.parse(window.sessionStorage.getItem("skillverify.assessment.j1")!);
    expect(stored).toEqual([
      { id: "q_aaaaaaa1", text: "My own text for the first question", language: "python", starter_code: F.qFix.starter_code },
      { id: "q_aaaaaaa2", text: F.qWrite.question, language: "python", starter_code: F.qWrite.starter_code },
      { id: "q_aaaaaaa3", text: F.qSql.question, language: "sql", starter_code: F.qSql.starter_code },
    ]);
    const steps = within(screen.getByRole("navigation", { name: "Progress" })).getAllByRole("listitem");
    expect(steps.every((s) => s.getAttribute("aria-current") !== "step")).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Back to question review" }));
    expect(await screen.findByText(/3 questions, 3 approved/)).toBeInTheDocument(); // the stored review is used
  });

  it("Create Assessment still works when storage is blocked", async () => {
    await ready();
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("blocked"); });
    for (const n of [1, 2, 3]) fireEvent.click(approveBtn(n));
    fireEvent.click(create());
    expect(await screen.findByRole("heading", { level: 1, name: "Assessment created." })).toBeInTheDocument();
  });
});

// =============================================================================================================
describe("editing", () => {
  const edit = (n: number) => fireEvent.click(within(card(n)).getByRole("button", { name: "Edit" }));
  const area = (n: number) => screen.getByRole("textbox", { name: `Question ${n} text` }) as HTMLTextAreaElement;
  const pill = (n: number) => within(card(n)).queryByText("Edited");

  it("Edit opens the raw text in a text area, focuses it, and shows Done and Approve and the hint", async () => {
    await ready();
    edit(1);
    expect(area(1)).toHaveFocus();
    expect(area(1).value).toBe(F.qFix.question);
    expect(area(1).rows).toBeGreaterThanOrEqual(4);
    expect(within(card(1)).getByRole("button", { name: "Done" })).toBeInTheDocument();
    expect(within(card(1)).getByRole("button", { name: "Approve question 1" })).toBeInTheDocument();
    expect(within(card(1)).queryByRole("button", { name: "Edit" })).toBeNull();
    expect(within(card(1)).getByText("Only the question text can be edited.")).toBeInTheDocument();
    expect(within(card(1)).getByText("Starter code")).toBeInTheDocument();
    expect(within(card(1)).queryByRole("textbox", { name: /starter/i })).toBeNull(); // starter code stays read-only
  });

  it("Done saves the text, leaves approval as it was, and moves focus back to Edit", async () => {
    await ready();
    fireEvent.click(approveBtn(1));
    edit(1);
    fireEvent.change(area(1), { target: { value: "A different question text that is long enough." } });
    fireEvent.click(within(card(1)).getByRole("button", { name: "Done" }));
    expect(within(card(1)).getByText("A different question text that is long enough.")).toBeInTheDocument();
    expect(tick(1)).toBeInTheDocument(); // editing an approved question keeps it approved
    expect(within(card(1)).getByRole("button", { name: "Edit" })).toHaveFocus();
    expect(pill(1)).toBeInTheDocument();
    edit(2);
    fireEvent.click(within(card(2)).getByRole("button", { name: "Done" }));
    expect(approveBtn(2)).toBeInTheDocument(); // still pending
  });

  it("Approve while editing saves and approves in one press", async () => {
    await ready();
    edit(2);
    fireEvent.change(area(2), { target: { value: "Rewrite getlist so it returns a tuple instead." } });
    fireEvent.click(within(card(2)).getByRole("button", { name: "Approve question 2" }));
    expect(within(card(2)).getByText("Rewrite getlist so it returns a tuple instead.")).toBeInTheDocument();
    expect(tick(2)).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Question 2 text" })).toBeNull();
    expect(counter()).toBe("3 questions, 1 approved");
  });

  it("Escape discards the draft and returns focus to Edit", async () => {
    const user = userEvent.setup();
    await ready();
    edit(1);
    fireEvent.change(area(1), { target: { value: "Discard me please, I am a draft." } });
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("textbox", { name: "Question 1 text" })).toBeNull();
    expect(within(card(1)).queryByText("Discard me please, I am a draft.")).toBeNull();
    expect(within(card(1)).getByRole("button", { name: "Edit" })).toHaveFocus();
    expect(pill(1)).toBeNull();
  });

  it("an empty or whitespace-only text cannot be saved or approved", async () => {
    await ready();
    edit(1);
    fireEvent.change(area(1), { target: { value: "   \n " } });
    expect(within(card(1)).getByText("A question can't be empty.")).toBeInTheDocument();
    expect(area(1)).toHaveAttribute("aria-invalid", "true");
    fireEvent.click(within(card(1)).getByRole("button", { name: "Done" }));
    expect(area(1)).toBeInTheDocument();
    fireEvent.click(within(card(1)).getByRole("button", { name: "Approve question 1" }));
    expect(area(1)).toBeInTheDocument();
    expect(counter()).toBe("3 questions, 0 approved");
    fireEvent.change(area(1), { target: { value: "Fixed it, now it has text." } });
    expect(within(card(1)).queryByText("A question can't be empty.")).toBeNull();
  });

  it("an approved question with an invalid draft blocks Create Assessment", async () => {
    await ready();
    for (const n of [1, 2, 3]) fireEvent.click(approveBtn(n));
    expect(create()).toBeEnabled();
    edit(1);
    fireEvent.change(area(1), { target: { value: "" } });
    expect(create()).toBeDisabled();
    fireEvent.change(area(1), { target: { value: "Back to something." } });
    expect(create()).toBeEnabled();
  });

  it("limits the text to 1500 characters and shows a counter only in the last 100", async () => {
    await ready();
    edit(1);
    fireEvent.change(area(1), { target: { value: "x".repeat(1399) } });
    expect(within(card(1)).queryByText(/\/ 1500/)).toBeNull();
    fireEvent.change(area(1), { target: { value: "x".repeat(1400) } });
    expect(within(card(1)).getByText("1400 / 1500")).toBeInTheDocument();
    fireEvent.change(area(1), { target: { value: "y".repeat(2000) } });
    expect(area(1).value).toHaveLength(1500);
    expect(area(1)).toHaveAttribute("maxlength", "1500");
    expect(within(card(1)).getByText("1500 / 1500")).toBeInTheDocument();
  });

  it("the Edited pill appears and disappears with the normalised comparison", async () => {
    await ready();
    expect(pill(1)).toBeNull();
    edit(1);
    fireEvent.change(area(1), { target: { value: F.qFix.question + " More." } });
    fireEvent.click(within(card(1)).getByRole("button", { name: "Done" }));
    expect(pill(1)).toBeInTheDocument();
    edit(1);
    fireEvent.change(area(1), { target: { value: "  " + F.qFix.question.replace(/ /g, "  ") + "\n" } }); // only whitespace differs
    fireEvent.click(within(card(1)).getByRole("button", { name: "Done" }));
    expect(pill(1)).toBeNull();
    expect(normalise("  a \n b ")).toBe("a b");
  });

  it("dropping a question that is being edited discards the draft", async () => {
    await ready();
    edit(1);
    fireEvent.change(area(1), { target: { value: "draft" } });
    fireEvent.click(within(card(1)).getByRole("button", { name: "Drop question" }));
    expect(screen.queryByRole("textbox")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Approve again" }));
    expect(within(card(1)).getByText(/In/)).toBeInTheDocument();
    expect(pill(1)).toBeNull();
  });
});

// =============================================================================================================
describe("dropping and replacing", () => {
  const drop = (n: number) => fireEvent.click(within(card(n)).getByRole("button", { name: "Drop question" }));

  it("Drop turns the card into the muted bar, keeps numbering, moves focus, and counts it out", async () => {
    await ready(done(F.RUN_FOUR));
    drop(2);
    const bar = screen.getByRole("group", { name: "Question 2 dropped" });
    expect(within(bar).getByText("Question 2")).toBeInTheDocument();
    expect(within(bar).getByText("Question dropped")).toBeInTheDocument();
    expect(within(bar).getByRole("button", { name: "Approve again" })).toHaveFocus();
    expect(within(bar).getByRole("button", { name: "Generate replacement" })).toBeInTheDocument();
    expect(counter()).toBe("3 questions, 0 approved");
    expect(within(screen.getAllByRole("article")[1]).getByText("Question 3")).toBeInTheDocument(); // numbering is stable
  });

  it("Approve again restores the question as approved", async () => {
    await ready();
    drop(1);
    fireEvent.click(screen.getByRole("button", { name: "Approve again" }));
    expect(tick(1)).toBeInTheDocument();
    expect(counter()).toBe("3 questions, 1 approved");
  });

  it("Generate replacement shows Writing..., then puts the new question in the same place, pending, with focus", async () => {
    let release: (reply: Reply) => void = () => {};
    const waiting = new Promise<Reply>((resolve) => { release = resolve; });
    const net = await ready({ ...done(F.RUN_FOUR), replace: waiting });
    drop(2);
    const generate = screen.getByRole("button", { name: "Generate replacement" });
    fireEvent.click(generate);
    const busy = await screen.findByRole("button", { name: /Writing/ });
    expect(busy).toBeDisabled();
    fireEvent.click(busy);
    await act(async () => { release(F.replaceOk()); await waiting; });
    await waitFor(() => expect(screen.queryByRole("group", { name: /dropped/ })).toBeNull());
    expect(net.count("POST", "/api/analyses/j1/questions/replace")).toBe(1);
    expect(net.calls.find((c) => c.path.endsWith("/replace"))!.body).toBe(JSON.stringify({ after: "q_aaaaaaa2" }));
    expect(card(2).querySelector(".qcard__text")).toHaveTextContent(/expire_all/);
    expect(within(card(2)).getByText("Question 2")).toHaveFocus();
    expect(approveBtn(2)).toBeInTheDocument(); // starts as not approved
    expect(counter()).toBe("4 questions, 0 approved");
    expect(within(card(1)).getByText("Question 1")).toBeInTheDocument();
  });

  it("only one replacement runs at a time", async () => {
    let release: (reply: Reply) => void = () => {};
    const waiting = new Promise<Reply>((resolve) => { release = resolve; });
    const net = await ready({ ...done(F.RUN_FOUR), replace: waiting });
    drop(1);
    drop(3);
    const buttons = screen.getAllByRole("button", { name: "Generate replacement" });
    fireEvent.click(buttons[0]);
    await screen.findByRole("button", { name: /Writing/ });
    const other = screen.getByRole("button", { name: "Generate replacement" });
    expect(other).toBeDisabled();
    fireEvent.click(other);
    expect(net.count("POST", "/api/analyses/j1/questions/replace")).toBe(1);
    await act(async () => { release(F.replaceOk()); await waiting; });
    await waitFor(() => expect(screen.getByRole("button", { name: "Generate replacement" })).toBeEnabled());
  });

  it("a replacement replaces the Skills without a question list with the returned one", async () => {
    await ready({ ...done(F.RUN_FOUR), replace: F.replaceOk(F.qReplacement, [F.SKIPPED[5]]) });
    expect(screen.getByRole("button", { name: /Skills without a question \(6\)/ })).toBeInTheDocument();
    drop(2);
    fireEvent.click(screen.getByRole("button", { name: "Generate replacement" }));
    expect(await screen.findByRole("button", { name: /Skills without a question \(1\)/ })).toBeInTheDocument();
  });

  it("no_replacement_available shows a quiet note under the bar and the button is usable again", async () => {
    await ready({ ...done(F.RUN_FOUR), replace: F.replaceNone });
    drop(2);
    fireEvent.click(screen.getByRole("button", { name: "Generate replacement" }));
    expect(await screen.findByText("There is no other code in the candidate's repos to ask about.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Generate replacement" })).toBeEnabled();
    expect(screen.getByRole("group", { name: "Question 2 dropped" })).toBeInTheDocument();
  });

  it("another error shows the Upload-screen title for that code and the button is usable again", async () => {
    await ready({ ...done(F.RUN_FOUR), replace: F.replaceFails });
    drop(2);
    fireEvent.click(screen.getByRole("button", { name: "Generate replacement" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The AI service is busy right now.");
    expect(screen.getByRole("button", { name: "Generate replacement" })).toBeEnabled();
  });

  it("a network failure while replacing shows the network title", async () => {
    await ready({ ...done(F.RUN_FOUR), replace: new TypeError("offline") });
    drop(2);
    fireEvent.click(screen.getByRole("button", { name: "Generate replacement" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Can't reach the SkillVerify server.");
  });
});

// =============================================================================================================
describe("the info note and Generate another question", () => {
  it("with dropped bars and fewer than 3 approved: the dropped-bar sentence", async () => {
    await ready();
    fireEvent.click(within(card(1)).getByRole("button", { name: "Drop question" }));
    expect(screen.getByText("A session needs at least 3 questions. Approve a dropped question again or generate a replacement.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Generate another question" })).toBeNull();
  });

  it("no note when 3 or more are live and nothing is dropped, even with none approved", async () => {
    await ready();
    expect(screen.queryByText(/A session needs at least 3 questions/)).toBeNull();
  });

  it("with dropped bars the note goes away once 3 are approved", async () => {
    await ready(done(F.RUN_FOUR));
    fireEvent.click(within(card(4)).getByRole("button", { name: "Drop question" }));
    expect(screen.getByText(/Approve a dropped question again/)).toBeInTheDocument();
    for (const n of [1, 2, 3]) fireEvent.click(approveBtn(n));
    expect(screen.queryByText(/Approve a dropped question again/)).toBeNull();
  });

  it("too_few shows the server message and Generate another question, and adds the new question at the end", async () => {
    const net = await ready({ ...done(F.RUN_TOO_FEW), replace: F.replaceOk(F.qSql) });
    expect(counter()).toBe("2 questions, 0 approved");
    expect(screen.getByText(F.RUN_TOO_FEW.message!)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Generate another question" }));
    await waitFor(() => expect(cards()).toHaveLength(3));
    expect(net.calls.find((c) => c.path.endsWith("/replace"))!.body).toBeUndefined(); // no `after`
    expect(within(card(3)).getByText("Question 3")).toHaveFocus();
    expect(counter()).toBe("3 questions, 0 approved");
    expect(screen.queryByText(F.RUN_TOO_FEW.message!)).toBeNull(); // resolved: the note is gone
    expect(screen.queryByRole("button", { name: "Generate another question" })).toBeNull();
  });

  it("Generate another question: Writing... while busy, a quiet note on no_replacement_available inside the box", async () => {
    await ready({ ...done(F.RUN_TOO_FEW), replace: F.replaceNone });
    fireEvent.click(screen.getByRole("button", { name: "Generate another question" }));
    expect(await screen.findByText("There is no other code in the candidate's repos to ask about.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Generate another question" })).toBeEnabled();
    expect(cards()).toHaveLength(2);
  });

  it("Generate another question: another error shows the title for that code in the box", async () => {
    await ready({ ...done(F.RUN_TOO_FEW), replace: F.replaceFails });
    fireEvent.click(screen.getByRole("button", { name: "Generate another question" }));
    expect(await screen.findByText("The AI service is busy right now.")).toBeInTheDocument();
  });
});

// =============================================================================================================
describe("skills without a question", () => {
  it("is collapsed by default and groups the skills by reason when opened", async () => {
    await ready();
    const header = screen.getByRole("button", { name: /Skills without a question \(6\)/ });
    expect(header).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("Docker")).toBeNull();
    fireEvent.click(header);
    expect(header).toHaveAttribute("aria-expanded", "true");
    const group = (label: string) => screen.getByText(label).parentElement!;
    expect(within(group("Not found in the candidate's code")).getAllByRole("listitem").map((li) => li.textContent)).toEqual(["Docker", "Pytest", "Airflow"]);
    expect(within(group("Can't be checked from code")).getAllByRole("listitem").map((li) => li.textContent)).toEqual(["Git", "REST APIs"]);
    expect(within(group("No suitable code file found")).getAllByRole("listitem").map((li) => li.textContent)).toEqual(["Snowflake"]);
    fireEvent.click(header);
    expect(screen.queryByText("Docker")).toBeNull();
  });

  it("groups every known reason and puts unknown ones under Other", () => {
    const grouped = groupSkipped([
      { skill: "A", tier: "must_have", reason: "question_failed" }, { skill: "B", tier: "must_have", reason: "not_enough_room" },
      { skill: "C", tier: "must_have", reason: "something_new" },
    ]);
    expect(grouped).toEqual([
      { label: "A question couldn't be written", skills: ["A"] },
      { label: "Not enough room in the session", skills: ["B"] },
      { label: "Other", skills: ["C"] },
    ]);
  });

  it("is hidden when there are no skipped skills", async () => {
    await ready(done({ ...F.RUN_OK, skipped_skills: [] }));
    expect(screen.queryByRole("button", { name: /Skills without a question/ })).toBeNull();
  });
});

// =============================================================================================================
describe("Preview as student", () => {
  const open = async () => {
    await ready();
    fireEvent.click(approveBtn(1));
    fireEvent.click(approveBtn(3));
    fireEvent.click(preview());
    return screen.findByRole("dialog", { name: "What the student will see" });
  };

  it("shows only the approved questions, in order, in the student theme, without recruiter material", async () => {
    const dialog = await open();
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(dialog).toHaveAttribute("data-mode", "student");
    expect(document.body.getAttribute("data-mode")).toBe("recruiter"); // the page is not themed
    expect(within(dialog).getByText("The full student screen, with the editor and timer, comes in a later step.")).toBeInTheDocument();
    expect(within(dialog).getByText("Question 1")).toBeInTheDocument();
    expect(within(dialog).getByText("Question 2")).toBeInTheDocument();
    expect(within(dialog).queryByText("Question 3")).toBeNull();
    expect(dialog.textContent).toContain("Fix `_dim`".replace(/`/g, ""));
    expect(dialog.textContent).toContain("-- Write your SQL query here");
    expect(dialog.textContent).not.toContain("Question 2 text");
    expect(within(dialog).queryByText(/Why this question/)).toBeNull();
    expect(within(dialog).queryByText("Only you see this")).toBeNull();
    expect(within(dialog).queryByText("Edited")).toBeNull();
    expect(dialog.textContent).not.toContain("SECRET-NOTES");
    expect(dialog.textContent).not.toContain(F.qFix.why.reason);
    expect(within(dialog).queryByRole("button", { name: /Approve|Edit|Drop/ })).toBeNull();
    expect(dialog.querySelector(".code__box")!.textContent!.split("\n")).toHaveLength(F.qFix.starter_code.split("\n").length); // full length
  });

  it("shows the edited text, not the original", async () => {
    await ready();
    fireEvent.click(within(card(1)).getByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Question 1 text" }), { target: { value: "My edited wording only." } });
    fireEvent.click(within(card(1)).getByRole("button", { name: "Approve question 1" }));
    fireEvent.click(preview());
    const dialog = await screen.findByRole("dialog");
    expect(dialog.textContent).toContain("My edited wording only.");
  });

  it("closes with Close and returns focus to the Preview button", async () => {
    await open();
    expect(screen.getByRole("button", { name: "Close" })).toHaveFocus();
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    await waitFor(() => expect(preview()).toHaveFocus());
  });

  it("closes with Escape and with a click on the dimmed area, not on the panel", async () => {
    const user = userEvent.setup();
    await open();
    fireEvent.click(screen.getByRole("dialog"));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
    await waitFor(() => expect(preview()).toHaveFocus());
    fireEvent.click(preview());
    await screen.findByRole("dialog");
    fireEvent.click(screen.getByTestId("modal-dim"));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("keeps Tab inside the dialog", async () => {
    const user = userEvent.setup();
    const dialog = await open();
    const inside = () => dialog.contains(document.activeElement);
    for (let i = 0; i < 8; i += 1) {
      await user.tab();
      expect(inside()).toBe(true);
    }
    for (let i = 0; i < 8; i += 1) {
      await user.tab({ shift: true });
      expect(inside()).toBe(true);
    }
  });
});

// =============================================================================================================
describe("persistence in this tab", () => {
  const key = "skillverify.review.j1";

  it("saves the review, including replacements, and restores it on a new load", async () => {
    await ready({ ...done(F.RUN_FOUR), replace: F.replaceOk() });
    fireEvent.click(approveBtn(1));
    fireEvent.click(within(card(2)).getByRole("button", { name: "Drop question" }));
    fireEvent.click(screen.getByRole("button", { name: "Generate replacement" }));
    await waitFor(() => expect(card(2).querySelector(".qcard__text")).toHaveTextContent(/expire_all/));
    fireEvent.click(within(card(3)).getByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Question 3 text" }), { target: { value: "Edited text that persists." } });
    fireEvent.click(within(card(3)).getByRole("button", { name: "Done" }));
    const saved = JSON.parse(window.sessionStorage.getItem(key)!);
    expect(saved.items.map((i: { question: { id: string }; status: string }) => [i.question.id, i.status])).toEqual([
      ["q_aaaaaaa1", "approved"], ["q_bbbbbbb1", "pending"], ["q_aaaaaaa3", "pending"], ["q_aaaaaaa4", "pending"]]);
    cleanup();
    await ready(done(F.RUN_FOUR));
    expect(tick(1)).toBeInTheDocument();
    expect(card(2).querySelector(".qcard__text")).toHaveTextContent(/expire_all/);
    expect(within(card(3)).getByText("Edited text that persists.")).toBeInTheDocument();
    expect(within(card(3)).getByText("Edited")).toBeInTheDocument();
    expect(counter()).toBe("4 questions, 1 approved");
  });

  it("ignores a stored review whose question ids do not match the server's run", async () => {
    window.sessionStorage.setItem(key, JSON.stringify({
      runIds: ["q_old1", "q_old2"], items: [{ question: { ...F.qFix, id: "q_old1" }, status: "approved", text: "OLD" }], skipped: [],
    }));
    await ready();
    expect(counter()).toBe("3 questions, 0 approved");
    expect(document.body.textContent).not.toContain("OLD");
  });

  it("ignores unreadable stored data", async () => {
    window.sessionStorage.setItem(key, "{not json");
    await ready();
    expect(counter()).toBe("3 questions, 0 approved");
    window.sessionStorage.setItem(key, JSON.stringify({ nope: true }));
    cleanup();
    await ready();
    expect(counter()).toBe("3 questions, 0 approved");
  });

  it("works when storage is blocked (reads and writes throw)", async () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("blocked"); });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("blocked"); });
    await ready();
    fireEvent.click(approveBtn(1));
    expect(tick(1)).toBeInTheDocument();
    expect(counter()).toBe("3 questions, 1 approved");
  });

  it("a stored review is not used for a run that is not done", async () => {
    window.sessionStorage.setItem(key, JSON.stringify({ runIds: ["q_aaaaaaa1", "q_aaaaaaa2", "q_aaaaaaa3"], items: [], skipped: [] }));
    install({ questions: ok(F.statusRunning("Reading files")) });
    renderAt();
    expect(await screen.findByText("Reading files...")).toBeInTheDocument();
  });
});

// =============================================================================================================
describe("the counter", () => {
  it("is an aria-live region, reads 1 question for one, and tracks drops", async () => {
    await ready(done({ ...F.RUN_OK, questions: [F.qFix], status: "too_few", message: "Only 1 question(s) could be written from the candidate's code. A session needs at least 3." }));
    expect(document.querySelector(".review__counter")).toHaveAttribute("aria-live", "polite");
    expect(counter()).toBe("1 question, 0 approved");
  });
});

// =============================================================================================================
describe("Score screen changes", () => {
  const wrap = (node: React.ReactNode) => <ThemeProvider><MemoryRouter initialEntries={["/recruiter/score/j1"]}>{node}</MemoryRouter></ThemeProvider>;

  it("the scan card shows 'Newest N of T repositories scanned.' only when the scan was capped", () => {
    const scan = { repo_cap: 30, repos_total: 93, repos_used: 30, capped: true };
    render(wrap(<ScoreView result={fixtureB} meta={{ ...metaB, scan }} />));
    expect(screen.getByText("Newest 30 of 93 repositories scanned.")).toBeInTheDocument();
    expect(screen.queryByText(/\d+ of \d+ repositories scanned$/)).toBeNull();
    cleanup();
    render(wrap(<ScoreView result={fixtureB} meta={{ ...metaB, scan: { ...scan, capped: false } }} />));
    const plain = `${fixtureB.github.repos_scanned} of ${fixtureB.github.counted_repos} repositories scanned`;
    expect(screen.getByText(plain)).toBeInTheDocument();
    cleanup();
    render(wrap(<ScoreView result={fixtureB} meta={metaB} />));
    expect(screen.getByText(plain)).toBeInTheDocument();
    cleanup();
    render(wrap(<ScoreView result={fixtureA} meta={{ ...metaB, scan: null }} />));
    expect(screen.queryByText(/Newest/)).toBeNull();
  });

  it("Continue to question review goes to /recruiter/questions/<id>", () => {
    render(wrap(<App />));
    cleanup();
    render(
      <ThemeProvider>
        <MemoryRouter initialEntries={["/recruiter/score/j1"]}>
          <ScoreView result={fixtureB} meta={metaB} analysisId="j1" />
          <LocationProbe />
        </MemoryRouter>
      </ThemeProvider>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Continue to question review" }));
    expect(screen.getByTestId("probe")).toHaveTextContent("/recruiter/questions/j1");
  });
});

import { useLocation } from "react-router-dom";
function LocationProbe() {
  return <span data-testid="probe">{useLocation().pathname}</span>;
}
