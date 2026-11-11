// Step D screenshots of the Score screen at 1440 px, with the API faked inside the browser (no server needed).
// Usage: npm run dev (in another terminal), then: node scripts/screenshots-step-d.mjs
import { existsSync, readFileSync } from "node:fs";
import { chromium } from "playwright-core";

const BASE = process.env.WEB_URL ?? "http://localhost:5173";
const fixtures = JSON.parse(readFileSync(new URL("../src/score/fixtures.json", import.meta.url), "utf8"));
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

async function shot(browser, name, answer, waitUntil = "networkidle") {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  await page.route("http://localhost:8000/**", (route) => answer(route));
  await page.goto(`${BASE}/recruiter/score/j1`, { waitUntil });
  await page.evaluate(() => document.fonts.ready);
  await page.waitForTimeout(1500);
  await page.screenshot({ path: `preview-step-d-${name}.png`, fullPage: true });
  console.log(`saved preview-step-d-${name}.png`);
  await page.close();
}
const meta = { candidate_name: "Priya Raman", job_title: "Data Analyst" };
const done = (result, withMeta = null) => (route) =>
  route.fulfill({ status: 200, contentType: "application/json", headers: { "access-control-allow-origin": "*" },
    body: JSON.stringify({ id: "j1", state: "done", stage: "Done", result, error: null, ...(withMeta ? { meta: withMeta } : {}) }) });

const browser = await chromium.launch({ executablePath });
await shot(browser, "a", done(fixtures.a, meta));
await shot(browser, "b", done(fixtures.b));
await shot(browser, "c", done(fixtures.c));
await shot(browser, "loading", () => new Promise(() => {}), "load"); // never answers: the skeleton stays
await browser.close();
