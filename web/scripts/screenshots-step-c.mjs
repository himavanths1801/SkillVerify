// Step C screenshots of the Upload screen at 1440 px, with the API faked inside the browser (no server needed).
// Usage: npm run dev (in another terminal), then: node scripts/screenshots-step-c.mjs
import { existsSync } from "node:fs";
import { chromium } from "playwright-core";

const BASE = process.env.WEB_URL ?? "http://localhost:5173";
const candidates = [
  process.env.BROWSER_PATH,
  "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
  "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
].filter(Boolean);
const executablePath = candidates.find((path) => existsSync(path));
if (!executablePath) {
  console.error("No Edge or Chrome found. Set BROWSER_PATH.");
  process.exit(1);
}

const kb = (n) => Buffer.alloc(n * 1024, "x");
const JD = { name: "data-analyst-jd.pdf", mimeType: "application/pdf", buffer: kb(184) };
const CV = { name: "priya-sample-cv.pdf", mimeType: "application/pdf", buffer: kb(212) };
const failed = (code, message, retryable) => ({
  id: "j1", state: "failed", stage: "Reading the CV", result: null, error: { code, message, retryable },
});

/** Fakes the API: POST answers {id}, GET answers `status`. */
async function fakeApi(page, status) {
  await page.route("http://localhost:8000/**", (route) => {
    const request = route.request();
    const json = (body, code = 200) =>
      route.fulfill({ status: code, contentType: "application/json", headers: { "access-control-allow-origin": "*" }, body: JSON.stringify(body) });
    if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: { "access-control-allow-origin": "*", "access-control-allow-headers": "*" } });
    if (request.method() === "POST") return json({ id: "j1" }, 202);
    return json(status);
  });
}

async function scene(browser, name, run, status = null) {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  if (status) await fakeApi(page, status);
  await page.goto(`${BASE}/recruiter/upload`, { waitUntil: "networkidle" });
  await page.evaluate(() => document.fonts.ready);
  await run(page);
  await page.waitForTimeout(1500);
  await page.screenshot({ path: `preview-step-c-${name}.png`, fullPage: true });
  console.log(`saved preview-step-c-${name}.png`);
  await page.close();
}

const fill = async (page) => {
  await page.setInputFiles('input[aria-label="Job description file"]', JD);
  await page.setInputFiles('input[aria-label="Candidate CV file"]', CV);
};
const analyze = async (page) => {
  await fill(page);
  await page.getByRole("button", { name: "Analyze" }).click();
};

const browser = await chromium.launch({ executablePath });
await scene(browser, "empty", async () => {});
await scene(browser, "ready", fill);
await scene(browser, "field-errors", async (page) => {
  await page.setInputFiles('input[aria-label="Job description file"]', { name: "notes.exe", mimeType: "application/octet-stream", buffer: kb(1) });
  await page.setInputFiles('input[aria-label="Candidate CV file"]', { name: "big-cv.pdf", mimeType: "application/pdf", buffer: Buffer.alloc(5 * 1024 * 1024 + 1024) });
});
await scene(browser, "analyzing", analyze, { id: "j1", state: "running", stage: "Checking GitHub", result: null, error: null });
await scene(browser, "ai_busy", analyze, failed("ai_busy", "x", true));
await scene(browser, "github_user_not_found", async (page) => {
  await fill(page);
  await page.getByLabel("GitHub username (optional)").fill("priya-sample");
  await page.getByRole("button", { name: "Analyze" }).click();
}, failed("github_user_not_found", "x", false));
await scene(browser, "github_unavailable", analyze, failed("github_unavailable", "x", true));
await scene(browser, "cv_unreadable", analyze, failed("cv_unreadable", "x", false));
await browser.close();
