// Screenshot of the Score screen header for fixture A with a real candidate name and job title (API faked in the browser).
// Usage: npm run dev (in another terminal), then: node scripts/screenshot-fix-header.mjs
import { existsSync, readFileSync } from "node:fs";
import { chromium } from "playwright-core";

const BASE = process.env.WEB_URL ?? "http://localhost:5173";
const fixtures = JSON.parse(readFileSync(new URL("../src/score/fixtures.json", import.meta.url), "utf8"));
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
const browser = await chromium.launch({ executablePath });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
await page.route("http://localhost:8000/**", (route) =>
  route.fulfill({ status: 200, contentType: "application/json", headers: { "access-control-allow-origin": "*" },
    body: JSON.stringify({ id: "j1", state: "done", stage: "Done", result: fixtures.a, error: null,
      meta: { candidate_name: "Priya Raman", job_title: "Data Analyst" } }) }));
await page.goto(`${BASE}/recruiter/score/j1`, { waitUntil: "networkidle" });
await page.evaluate(() => document.fonts.ready);
await page.waitForTimeout(1500);
await page.screenshot({ path: "preview-fix-header.png", clip: { x: 0, y: 0, width: 1440, height: 340 } });
console.log("saved preview-fix-header.png");
await browser.close();
