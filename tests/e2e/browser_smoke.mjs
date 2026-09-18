// Real-browser smoke test of the Video Publisher GUI (optional, not run by pytest).
//
// Drives Microsoft Edge / Google Chrome through the DevTools protocol like a
// user: uploads a video file, schedules it, retries and cancels uploads,
// edits a job from the calendar, disconnects/connects an account, saves
// settings - and fails on any JavaScript error.
//
// Usage (against a running Bottle Net GUI with connected test accounts):
//   BN_LAUNCH_URL="http://127.0.0.1:8765/?key=..." BN_BROWSER="C:/.../msedge.exe" \
//   BN_SAMPLE_VIDEO="C:/videos/sample.mp4" node tests/e2e/browser_smoke.mjs

import { spawn } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const LAUNCH_URL = process.env.BN_LAUNCH_URL;
const BROWSER = process.env.BN_BROWSER;
const SAMPLE = process.env.BN_SAMPLE_VIDEO;
const PORT = 9333;
if (!LAUNCH_URL || !BROWSER || !SAMPLE) {
  console.error("Set BN_LAUNCH_URL, BN_BROWSER and BN_SAMPLE_VIDEO.");
  process.exit(2);
}

const browser = spawn(BROWSER, [
  "--headless=new", `--remote-debugging-port=${PORT}`, `--user-data-dir=${mkdtempSync(join(tmpdir(), "bn-e2e-"))}`,
  "--window-size=1360,1000", "--no-first-run", "about:blank",
], { stdio: "ignore" });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function devtoolsTarget() {
  for (let i = 0; i < 50; i += 1) {
    try {
      return await (await fetch(`http://127.0.0.1:${PORT}/json/new?about:blank`, { method: "PUT" })).json();
    } catch { await sleep(200); }
  }
  throw new Error("browser did not start");
}

const target = await devtoolsTarget();
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((resolve) => ws.addEventListener("open", resolve, { once: true }));
let nextId = 1;
const pending = new Map();
const jsErrors = [];
ws.addEventListener("message", (event) => {
  const msg = JSON.parse(event.data);
  if (msg.id && pending.has(msg.id)) {
    pending.get(msg.id)(msg);
    pending.delete(msg.id);
  } else if (msg.method === "Runtime.exceptionThrown") {
    jsErrors.push(msg.params.exceptionDetails.exception?.description || msg.params.exceptionDetails.text);
  } else if (msg.method === "Runtime.consoleAPICalled" && msg.params.type === "error") {
    jsErrors.push(msg.params.args.map((a) => a.value ?? a.description).join(" "));
  }
});
function cdp(method, params = {}) {
  const id = nextId++;
  ws.send(JSON.stringify({ id, method, params }));
  return new Promise((resolve, reject) => pending.set(id, (m) => (m.error ? reject(new Error(m.error.message)) : resolve(m.result))));
}
async function js(expression) {
  const result = await cdp("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description || "evaluation failed");
  return result.result.value;
}
async function waitFor(expression, what, timeout = 15000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    if (await js(expression).catch(() => false)) return;
    await sleep(200);
  }
  throw new Error(`timed out waiting for: ${what}`);
}
const API = `(path, init = {}) => fetch(path, { ...init, headers: { "X-Bottle-Net-Token": document.querySelector('meta[name="bn-token"]').content, "Content-Type": "application/json", ...(init.headers || {}) } }).then((r) => r.json())`;
const results = [];
async function step(name, fn) {
  try {
    await fn();
    results.push(["PASS", name]);
  } catch (error) {
    results.push(["FAIL", `${name}: ${error.message}`]);
  }
}

await cdp("Page.enable");
await cdp("Runtime.enable");
await cdp("DOM.enable");

await step("sign in with the launch link and show the dashboard", async () => {
  await cdp("Page.navigate", { url: `${LAUNCH_URL}#/dashboard` });
  await waitFor(`document.querySelector("main h1")?.textContent === "Video Publisher"`, "dashboard heading");
  await js(`window.confirm = () => true; window.__opened = []; window.open = (u) => { window.__opened.push(u); return null; }; true`);
});

let videoId = null;
await step("upload a video file and capture its thumbnail and duration in the browser", async () => {
  await js(`location.hash = "#/upload?when=schedule"; true`);
  await waitFor(`!!document.querySelector("#video-file")`, "file input");
  const { root } = await cdp("DOM.getDocument", { depth: -1 });
  const { nodeId } = await cdp("DOM.querySelector", { nodeId: root.nodeId, selector: "#video-file" });
  await cdp("DOM.setFileInputFiles", { nodeId, files: [SAMPLE] });
  await waitFor(`/video=\\d+/.test(location.hash) && !!document.querySelector(".selected-video video")`, "uploaded video", 60000);
  videoId = await js(`Number(location.hash.match(/video=(\\d+)/)[1])`);
  const video = await js(`(${API})("/api/videos/${videoId}")`);
  if (!video.thumbnail_url || !(video.duration > 0)) throw new Error(`thumbnail/duration missing: ${JSON.stringify(video)}`);
});

let jobId = null;
await step("schedule the video for YouTube and Facebook", async () => {
  await js(`document.querySelector("#upload-title").value = "E2E Scheduled"; document.querySelector("#upload-tags").value = "e2e, test"; document.querySelector("#submit-upload").click(); true`);
  await waitFor(`location.hash === "#/queue" && document.body.innerText.includes("E2E Scheduled")`, "job in the queue");
  jobId = await js(`[...document.querySelectorAll(".job-card")].find((c) => c.innerText.includes("E2E Scheduled")).dataset.jobId`);
});

await step("cancel one platform of the scheduled job", async () => {
  await js(`document.querySelector('.job-card[data-job-id="${jobId}"] [data-action="cancel-pj"]').click(); true`);
  await waitFor(`document.querySelector('.job-card[data-job-id="${jobId}"]')?.innerText.includes("Cancelled")`, "cancelled status");
});

await step("retry a failed upload from the queue", async () => {
  const hasFailed = await js(`!!document.querySelector('.pj-failed [data-action="retry"]')`);
  if (!hasFailed) return;
  const pj = await js(`document.querySelector('.pj-failed [data-action="retry"]').dataset.pj`);
  await js(`document.querySelector('.pj-failed [data-action="retry"]').click(); true`);
  await waitFor(`!document.querySelector('.pj[data-pj-id="${pj}"]')?.classList.contains("pj-failed")`, "retried upload", 20000);
});

await step("edit a scheduled upload from the calendar", async () => {
  await js(`location.hash = "#/scheduler"; true`);
  await waitFor(`!!document.querySelector(".calendar")`, "calendar");
  await js(`[...document.querySelectorAll('.cal-job')].find((b) => b.dataset.job === "${jobId}").click(); true`);
  await waitFor(`document.querySelector("#dialog").open && !!document.querySelector("#job-edit-form")`, "edit dialog");
  await js(`document.querySelector("#edit-title").value = "E2E Edited"; document.querySelector("#job-edit-form").requestSubmit(); true`);
  await waitFor(`!document.querySelector("#dialog").open && document.body.innerText.includes("E2E Edited")`, "saved edit");
});

await step("month navigation", async () => {
  const before = await js(`document.querySelector(".month-title").textContent`);
  await js(`document.querySelector('[data-action="month"][data-delta="1"]').click(); true`);
  await waitFor(`document.querySelector(".month-title").textContent !== ${JSON.stringify(before)}`, "next month");
  await js(`document.querySelector('[data-action="month"][data-delta="0"]').click(); true`);
  await waitFor(`document.querySelector(".month-title").textContent === ${JSON.stringify(before)}`, "back to today");
});

await step("disconnect and reconnect YouTube (opens Google's OAuth page)", async () => {
  await js(`location.hash = "#/accounts"; true`);
  await waitFor(`!!document.querySelector('[data-action="disconnect"][data-platform="youtube"]')`, "disconnect button");
  await js(`document.querySelector('[data-action="disconnect"][data-platform="youtube"]').click(); true`);
  await waitFor(`!!document.querySelector('[data-action="connect"][data-platform="youtube"]')`, "connect button");
  await js(`document.querySelector('[data-action="connect"][data-platform="youtube"]').click(); true`);
  await waitFor(`window.__opened.some((u) => u.startsWith("https://accounts.google.com/o/oauth2/v2/auth?"))`, "Google sign-in opened");
  const passwordFields = await js(`document.querySelectorAll('input[type="password"]').length`);
  if (passwordFields) throw new Error("password field found");
});

await step("save settings", async () => {
  await js(`location.hash = "#/settings"; true`);
  await waitFor(`!!document.querySelector("#set-attempts")`, "settings form");
  await js(`document.querySelector("#set-attempts").value = "5"; document.querySelector("#settings-form").requestSubmit(); true`);
  await waitFor(`document.body.innerText.includes("Settings saved")`, "saved toast");
  const settings = await js(`(${API})("/api/settings")`);
  if (settings.max_attempts !== 5) throw new Error("setting not stored");
  await js(`(${API})("/api/settings", { method: "PUT", body: JSON.stringify({ max_attempts: 4 }) })`);
});

await step("history filters", async () => {
  await js(`location.hash = "#/history"; true`);
  await waitFor(`!!document.querySelector('[data-filter="failed"]')`, "filters");
  await js(`document.querySelector('[data-filter="failed"]').click(); true`);
  await waitFor(`document.querySelector('[data-filter="failed"]').getAttribute("aria-pressed") === "true"`, "failed filter active");
});

await step("keyboard: skip link and focus moves to the page heading", async () => {
  await js(`location.hash = "#/videos"; true`);
  await waitFor(`document.activeElement?.tagName === "H1"`, "focused heading");
  if (!(await js(`!!document.querySelector(".skip-link")`))) throw new Error("no skip link");
});

await step("no JavaScript errors", async () => {
  if (jsErrors.length) throw new Error(jsErrors.join(" | "));
});

for (const [status, name] of results) console.log(`${status}  ${name}`);
ws.close();
browser.kill();
process.exit(results.some(([status]) => status === "FAIL") ? 1 : 0);
