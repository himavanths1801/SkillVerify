// Full-page screenshot of the design-system preview, using playwright-core and the Edge/Chrome already on this machine
// (no browser download). Usage: npm run dev (in another terminal), then: npm run screenshot
import { existsSync } from "node:fs";
import { chromium } from "playwright-core";

const url = process.argv[2] ?? "http://localhost:5173/";
const out = process.argv[3] ?? "preview-step-a.png";
const candidates = [
  process.env.BROWSER_PATH,
  "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
  "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
].filter(Boolean);
const executablePath = candidates.find((path) => existsSync(path));
if (!executablePath) {
  console.error("No Edge or Chrome found. Set BROWSER_PATH to a browser executable, or open the dev server in a browser at 1440px wide.");
  process.exit(1);
}

const browser = await chromium.launch({ executablePath });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
await page.goto(url, { waitUntil: "networkidle" });
await page.evaluate(() => document.fonts.ready);
const waitMs = Number(process.argv[4] ?? 0); // optional: let the mode fade finish before the shot
if (waitMs > 0) await page.waitForTimeout(waitMs);
console.log(`body background-color: ${await page.evaluate(() => getComputedStyle(document.body).backgroundColor)}`);
await page.screenshot({ path: out, fullPage: true });
const info = await page.evaluate(() => ({
  fonts: [...document.fonts].filter((f) => f.status === "loaded").map((f) => `${f.family} ${f.weight}`),
  height: document.documentElement.scrollHeight,
}));
console.log(`Saved ${out} (${info.height}px tall). Loaded fonts: ${info.fonts.join(", ")}`);
await browser.close();
