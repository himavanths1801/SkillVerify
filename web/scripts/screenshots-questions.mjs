// Question review screenshots at 1440 px, with the API faked inside the browser (no server needed).
// Usage: npm run dev (in another terminal), then: node scripts/screenshots-questions.mjs
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

const browser = await chromium.launch({ executablePath });

// The fixtures of the screen, read from the dev server so the shots use exactly the test data.
const loader = await browser.newPage();
await loader.goto(BASE, { waitUntil: "load" });
const F = await loader.evaluate(async () => JSON.parse(JSON.stringify(await import("/src/questions/fixtures.ts"))));
await loader.close();

const json = (status, body) => ({
  status, contentType: "application/json", headers: { "access-control-allow-origin": "*" }, body: JSON.stringify(body),
});

/** One shot. `server` answers the questions routes; `act` clicks around before the picture. */
async function shot(name, { questions, replace, act, full = true }) {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  await page.route("http://localhost:8000/**", async (route) => {
    const url = new URL(route.request().url());
    const method = route.request().method();
    if (url.pathname === "/api/analyses/j1") return route.fulfill(json(200, F.ANALYSIS));
    if (url.pathname.endsWith("/questions/replace")) {
      const reply = replace ?? replaceOk;
      if (reply === "never") return new Promise(() => {});
      return route.fulfill(json(reply.status, reply.body));
    }
    if (url.pathname.endsWith("/questions") && method === "GET") return questions === "never" ? new Promise(() => {}) : route.fulfill(json(200, questions));
    return route.fulfill(json(404, { message: "not found" }));
  });
  await page.goto(`${BASE}/recruiter/questions/j1`, { waitUntil: "load" });
  await page.evaluate(() => document.fonts.ready);
  await page.waitForTimeout(1500);
  if (act) {
    await act(page);
    await page.waitForTimeout(400);
  }
  await page.screenshot({ path: `preview-questions-${name}.png`, fullPage: full });
  console.log(`saved preview-questions-${name}.png`);
  await page.close();
}

const status = (state, stage, extra) => ({ id: "j1", state, stage, result: null, error: null, ...extra });
const done = (run) => status("done", "Checking questions", { result: run });
const failed = (error) => status("failed", "Writing questions", { error });
const running = (stage) => status("running", stage);
const replaceOk = { status: 200, body: { question: F.qReplacement, skipped_skills: F.SKIPPED.slice(0, 2), dropped: [] } };
const approve = (n) => (page) => page.getByRole("button", { name: `Approve question ${n}` }).click();
const card = (page, n) => page.getByRole("article").nth(n - 1);

await shot("main", {
  questions: done(F.RUN_FOUR),
  act: async (page) => {
    for (const n of [1, 2, 3]) await approve(n)(page);
    await card(page, 4).getByRole("button", { name: "Edit" }).click();
    const area = page.getByRole("textbox", { name: "Question 4 text" });
    await area.fill((await area.inputValue()) + " Keep the call atomic.");
    await card(page, 4).getByRole("button", { name: "Done" }).click();
  },
});
await shot("editing", {
  questions: done(F.RUN_FOUR),
  act: async (page) => {
    await approve(1)(page);
    await card(page, 2).getByRole("button", { name: "Edit" }).click();
  },
});
await shot("dropped-replacing", {
  questions: done(F.RUN_FOUR),
  replace: "never",
  act: async (page) => {
    await approve(1)(page);
    await card(page, 3).getByRole("button", { name: "Drop question" }).click();
    await page.getByRole("button", { name: "Generate replacement" }).click();
  },
});
await shot("below-minimum", {
  questions: done(F.RUN_OK),
  act: async (page) => {
    await approve(1)(page);
    await approve(2)(page);
    await card(page, 3).getByRole("button", { name: "Drop question" }).click();
  },
});
await shot("too-few", { questions: done(F.RUN_TOO_FEW) });
await shot("empty", { questions: done(F.RUN_NONE) });
await shot("error", { questions: failed(F.E_RATE_FALLBACK) });
await shot("loading", { questions: running("Writing questions") });
await shot("preview", {
  full: false, // the dim area is fixed to the window, so a window-sized picture shows it correctly
  questions: done(F.RUN_OK),
  act: async (page) => {
    for (const n of [1, 2, 3]) await approve(n)(page);
    await page.getByRole("button", { name: "Preview as student" }).click();
    await page.evaluate(() => { document.querySelector(".modal-dim").scrollTop = 0; });
  },
});
await shot("skills", {
  questions: done(F.RUN_FOUR),
  act: async (page) => {
    for (const n of [1, 2, 3]) await approve(n)(page);
    await page.getByRole("button", { name: /Skills without a question/ }).click();
  },
});
await browser.close();
