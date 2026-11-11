// Screenshot of the Upload screen with the rate-limit banner, both body variants stacked in one image
// (API faked inside the browser, no server needed).
// Usage: npm run dev (in another terminal), then: node scripts/screenshot-fallback-banner.mjs
import { existsSync } from "node:fs";
import { chromium } from "playwright-core";

const BASE = process.env.WEB_URL ?? "http://localhost:5173";
const executablePath = [
  process.env.BROWSER_PATH,
  "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
  "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
].filter(Boolean).find((path) => existsSync(path));
if (!executablePath) {
  console.error("No Edge or Chrome found. Set BROWSER_PATH.");
  process.exit(1);
}

const kb = (n) => Buffer.alloc(n * 1024, "x");
const JD = { name: "data-analyst-jd.pdf", mimeType: "application/pdf", buffer: kb(184) };
const CV = { name: "priya-sample-cv.pdf", mimeType: "application/pdf", buffer: kb(212) };
const failed = (fallback_available) => ({
  id: "j1", state: "failed", stage: "Reading the CV", result: null,
  error: { code: "ai_rate_limited", message: "x", retryable: true, fallback_available },
});

async function banner(browser, status) {
  const page = await browser.newPage({ viewport: { width: 1440, height: 760 } });
  await page.route("http://localhost:8000/**", (route) => {
    const request = route.request();
    const headers = { "access-control-allow-origin": "*", "access-control-allow-headers": "*" };
    if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers });
    const post = request.method() === "POST";
    return route.fulfill({ status: post ? 202 : 200, contentType: "application/json", headers,
      body: JSON.stringify(post ? { id: "j1" } : status) });
  });
  await page.goto(`${BASE}/recruiter/upload`, { waitUntil: "networkidle" });
  await page.evaluate(() => document.fonts.ready);
  await page.setInputFiles('input[aria-label="Job description file"]', JD);
  await page.setInputFiles('input[aria-label="Candidate CV file"]', CV);
  await page.getByRole("button", { name: "Analyze" }).click();
  await page.getByRole("alert").waitFor();
  await page.waitForTimeout(1500);
  const image = await page.screenshot();
  await page.close();
  return image.toString("base64");
}

const browser = await chromium.launch({ executablePath });
const withBackup = await banner(browser, failed(true));
const without = await banner(browser, failed(false));
const sheet = await browser.newPage({ viewport: { width: 1440, height: 1520 } });
await sheet.setContent(`<body style="margin:0;background:#fff">
  <img style="display:block" src="data:image/png;base64,${withBackup}">
  <img style="display:block" src="data:image/png;base64,${without}"></body>`);
await sheet.screenshot({ path: "preview-fallback-banner.png" });
console.log("saved preview-fallback-banner.png");
await browser.close();
