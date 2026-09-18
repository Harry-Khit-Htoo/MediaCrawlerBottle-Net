// GUI view tests (run with: node --test tests/web). They exercise the real
// rendering code used in the browser (bottle_net/publisher/web/js/views.js).
import assert from "node:assert/strict";
import { test } from "node:test";

import * as F from "../../bottle_net/publisher/web/js/format.js";
import * as V from "../../bottle_net/publisher/web/js/views.js";

const TZ = "Asia/Bangkok";
const settings = { timezone: TZ, youtube_privacy: "public" };
const connected = [
  { platform: "youtube", name: "YouTube", configured: true, connected: true, display_name: "creator@example.com", detail: "My Channel" },
  { platform: "facebook", name: "Facebook", configured: true, connected: true, display_name: "My Facebook Page", detail: "Facebook Page" },
];
const video = { id: 7, filename: "Morning.mp4", size: 18000000, duration: 42, preview_url: "/media/videos/7", source: "upload" };

function pj(overrides = {}) {
  return {
    id: 1, platform: "youtube", status: "scheduled", progress: 0, attempts: 0, retry_count: 0, last_error: null,
    due_at: "2026-09-19T13:00:00+00:00", remote_id: null, remote_post_id: null, remote_url: null, warnings: [],
    cancel_requested: false, ...overrides,
  };
}

function job(overrides = {}) {
  return {
    id: 1001, video: "morning.mp4", title: "Morning Video", description: "", tags: [], status: "scheduled",
    scheduled_at: "2026-09-19T13:00:00+00:00", created_at: "2026-09-19T05:00:00+00:00", schedule_mode: "local",
    platform_jobs: [pj(), pj({ id: 2, platform: "facebook" })], ...overrides,
  };
}

const count = (html, needle) => html.split(needle).length - 1;

// --------------------------------------------------------------- upload form

test("upload form has every field and never a password field", () => {
  const html = V.uploadFormView({ video, accounts: connected, templates: [], settings, defaults: { when: "schedule", date: "2026-09-19", time: "20:00" } });
  for (const id of ["upload-title", "upload-description", "upload-tags", "thumbnail-file", "upload-date", "upload-time", "upload-privacy"]) {
    assert.ok(html.includes(`id="${id}"`), id);
  }
  assert.ok(html.includes('name="platform" value="youtube" checked'));
  assert.ok(html.includes('name="platform" value="facebook" checked'));
  assert.ok(html.includes('value="schedule" checked'));
  assert.ok(html.includes('value="2026-09-19"') && html.includes('value="20:00"'));
  assert.ok(html.includes(">Schedule Video<"));
  assert.ok(html.includes('<video controls preload="metadata" src="/media/videos/7"'));
  assert.equal(/type=["']password/i.test(html), false);
  assert.equal(/password/i.test(html), false);
});

test("upload form without a video shows the drop zone and disables submit", () => {
  const html = V.uploadFormView({ video: null, accounts: connected, templates: [], settings, defaults: { when: "now", date: "", time: "" } });
  assert.ok(html.includes("Drag &amp; Drop Video Here"));
  assert.ok(html.includes('id="video-file"'));
  assert.ok(html.includes('id="submit-upload" disabled'));
  assert.ok(html.includes(">Publish Now<"));
  assert.ok(html.includes('class="schedule-fields" hidden'));
});

test("platforms that are not connected cannot be selected", () => {
  const accounts = [connected[0], { platform: "facebook", name: "Facebook", configured: true, connected: false }];
  const html = V.uploadFormView({ video, accounts, templates: [], settings, defaults: { when: "now", date: "", time: "" } });
  assert.ok(html.includes('value="facebook" disabled'));
  assert.ok(html.includes('<a href="#/accounts">Connect first</a>'));
});

test("downloaded videos require a rights confirmation", () => {
  const html = V.uploadFormView({ video: { ...video, source: "download" }, accounts: connected, templates: [], settings, defaults: { when: "now", date: "", time: "" } });
  assert.ok(html.includes('id="rights-confirmed" required'));
});

test("templates appear in the upload form", () => {
  const html = V.uploadFormView({ video, accounts: connected, templates: [{ id: 3, name: "Tutorial" }], settings, defaults: { when: "now", date: "", time: "" } });
  assert.ok(html.includes('<option value="3">Tutorial</option>'));
});

// ----------------------------------------------------------------- accounts

test("connected account shows the account and a Disconnect button", () => {
  const html = V.accountCard(connected[0]);
  assert.ok(html.includes("✓ Connected"));
  assert.ok(html.includes("Account: <strong>creator@example.com</strong>"));
  assert.ok(html.includes('data-action="disconnect" data-platform="youtube"'));
  const page = V.accountCard(connected[1]);
  assert.ok(page.includes("Page: <strong>My Facebook Page</strong>"));
});

test("not connected account offers Connect", () => {
  const html = V.accountCard({ platform: "facebook", name: "Facebook", configured: true, connected: false, pending_pages: [] });
  assert.ok(html.includes("Not connected"));
  assert.ok(html.includes('data-action="connect" data-platform="facebook">Connect Facebook</button>'));
});

test("unconfigured account explains setup and cannot connect", () => {
  const html = V.accountCard({ platform: "youtube", name: "YouTube", configured: false, connected: false,
    setup_hint: "Set BOTTLE_NET_YOUTUBE_CLIENT_ID", redirect_uri: "http://127.0.0.1:8765/oauth/youtube/callback" });
  assert.ok(html.includes("Setup required: Set BOTTLE_NET_YOUTUBE_CLIENT_ID"));
  assert.ok(html.includes("<button type=\"button\" class=\"btn primary\" disabled>Connect YouTube</button>"));
});

test("Facebook Page selection", () => {
  const html = V.accountCard({ platform: "facebook", name: "Facebook", configured: true, connected: false,
    pending_pages: [{ id: "111", name: "My Page" }, { id: "222", name: "Other" }] });
  assert.equal(count(html, 'name="facebook-page"'), 2);
  assert.ok(html.includes('data-action="select-page"'));
});

test("accounts page says passwords are never requested", () => {
  assert.ok(V.accountsView(connected).includes("never asks for or stores your password"));
  assert.ok(V.accountsView(connected, { waiting: "youtube" }).includes("finish signing in to YouTube"));
});

// ------------------------------------------------------------- job statuses

test("scheduled platform jobs can be cancelled", () => {
  const html = V.jobCard(job(), TZ);
  assert.equal(count(html, 'data-action="cancel-pj"'), 2);
  assert.ok(html.includes('data-action="cancel-job" data-job="1001"'));
  assert.ok(html.includes("Scheduled: 19 Sept 2026 20:00") || html.includes("Scheduled: 19 Sep 2026 20:00"));
  assert.equal(count(html, 'data-action="retry"'), 0);
});

test("failed platform job shows the reason and a Retry button, the other stays published", () => {
  const html = V.jobCard(job({ status: "partial", platform_jobs: [
    pj({ status: "completed", remote_id: "abc123", remote_url: "https://www.youtube.com/watch?v=abc123" }),
    pj({ id: 2, platform: "facebook", status: "failed", attempts: 1, last_error: "Facebook upload failed.\n\nTemporary API error" }),
  ] }), TZ);
  assert.ok(html.includes("✓</span> Published"));
  assert.ok(html.includes("✗</span> Failed"));
  assert.ok(html.includes("Temporary API error"));
  assert.ok(html.includes('data-action="retry" data-pj="2">Retry Facebook</button>'));
  assert.ok(html.includes('data-action="publish-again" data-pj="1"'));
  assert.ok(html.includes('<a href="https://www.youtube.com/watch?v=abc123"'));
  assert.ok(html.includes("Partly published"));
});

test("uploading shows progress; retrying shows the next attempt", () => {
  const uploading = V.pjLine(pj({ status: "uploading", progress: 82 }), TZ);
  assert.ok(uploading.includes('<progress max="100" value="82"'));
  assert.ok(uploading.includes("82%"));
  const retrying = V.pjLine(pj({ status: "retrying", attempts: 1, due_at: "2026-09-19T13:01:00+00:00", last_error: "timed out" }), TZ);
  assert.ok(retrying.includes("Next attempt at 20:01 (attempt 2)"));
  assert.ok(retrying.includes('data-action="cancel-pj"'));
});

test("missed jobs offer Publish now (once) and Cancel", () => {
  const html = V.jobCard(job({ status: "missed", platform_jobs: [pj({ status: "missed", last_error: "Bottle Net was not running" })] }), TZ);
  assert.ok(html.includes('data-action="retry" data-pj="1">Publish now</button>'));
  assert.equal(count(html, "Publish now"), 1);
  assert.equal(html.includes("Retry YouTube"), false);
  assert.ok(html.includes('data-action="cancel-pj"'));
});

test("non-https platform links are not rendered as links", () => {
  const html = V.pjLine(pj({ status: "completed", remote_id: "x", remote_url: "javascript:alert(1)" }), TZ);
  assert.equal(html.includes("javascript:"), false);
});

test("user text is escaped", () => {
  const html = V.jobCard(job({ title: '<img src=x onerror="alert(1)">', video: "<script>.mp4" }), TZ);
  assert.equal(html.includes("<script>"), false);
  assert.equal(html.includes("<img src=x"), false);
  assert.ok(html.includes("&lt;script&gt;.mp4"));
});

// ------------------------------------------------------------ queue/history

test("history table and filters", () => {
  const rows = [
    { ...pj({ status: "completed", remote_id: "abc", remote_url: "https://youtu.be/abc", completed_at: "2026-09-19T01:00:00+00:00" }), video: "Morning.mp4", job_id: 1 },
    { ...pj({ id: 2, platform: "facebook", status: "failed", retry_count: 3, last_error: "Permission" }), video: "Test.mp4", job_id: 2, scheduled_at: "2026-09-18T01:00:00+00:00" },
  ];
  const html = V.historyView(rows, "failed", TZ);
  for (const label of ["All", "YouTube", "Facebook", "Completed", "Failed", "Pending"]) assert.ok(html.includes(`>${label}</button>`), label);
  assert.ok(html.includes('data-filter="failed" aria-pressed="true"'));
  assert.ok(html.includes("<th scope=\"col\">Platform ID</th>"));
  assert.ok(html.includes("Morning.mp4") && html.includes("Test.mp4"));
  assert.ok(html.includes("<td>3</td>"));
  assert.ok(html.includes('<td class="error-cell">Permission</td>'));
  assert.ok(html.includes('data-action="retry" data-pj="2"'));
  assert.ok(V.historyView([], "all", TZ).includes("No uploads match"));
});

test("empty queue", () => {
  assert.ok(V.queueView([], TZ).includes("The queue is empty"));
});

// ---------------------------------------------------------------- scheduler

test("calendar places jobs on the local day and allows moving scheduled jobs", () => {
  const jobs = [
    job({ id: 1, title: "Morning", scheduled_at: "2026-09-19T01:00:00+00:00" }), // 08:00 on the 19th
    job({ id: 2, title: "Evening", scheduled_at: "2026-09-19T13:00:00+00:00" }), // 20:00 on the 19th
    job({ id: 3, title: "Late", scheduled_at: "2026-09-19T20:00:00+00:00", status: "completed",
      platform_jobs: [pj({ status: "completed" })] }), // 03:00 on the 20th
  ];
  const html = V.calendarView({ year: 2026, month: 9, jobs, tz: TZ, todayKey: "2026-09-19" });
  assert.ok(html.includes("September 2026"));
  const day19 = html.split('data-date="2026-09-19"')[1].split('data-date="2026-09-20"')[0];
  assert.ok(day19.includes("08:00</span> Morning") && day19.includes("20:00</span> Evening"));
  assert.ok(day19.indexOf("Morning") < day19.indexOf("Evening"));
  const day20 = html.split('data-date="2026-09-20"')[1].split('data-date="2026-09-21"')[0];
  assert.ok(day20.includes("03:00</span> Late"));
  assert.ok(html.includes('data-job="1" draggable="true"'));
  assert.equal(html.includes('data-job="3" draggable'), false); // finished uploads cannot move
  assert.ok(html.includes('class="cal-day today"'));
  assert.ok(html.includes('href="#/upload?when=schedule&amp;date=2026-09-19"'));
});

test("job dialog allows editing and cancelling a scheduled upload", () => {
  const html = V.jobDialogView(job({ description: "d", tags: ["a", "b"] }), TZ);
  assert.ok(html.includes('id="job-edit-form"'));
  assert.ok(html.includes('id="edit-date" type="date" value="2026-09-19"'));
  assert.ok(html.includes('id="edit-time" type="time" value="20:00"'));
  assert.ok(html.includes('value="a, b"'));
  assert.ok(html.includes('data-action="cancel-job"'));
  const done = V.jobDialogView(job({ platform_jobs: [pj({ status: "completed" })] }), TZ);
  assert.equal(done.includes("job-edit-form"), false);
});

// ---------------------------------------------------------------- dashboard

test("dashboard shows accounts, today's schedule, statistics and quick actions", () => {
  const html = V.dashboardView({
    timezone: TZ, scheduler_running: true, accounts: connected,
    stats: { scheduled_today: 3, uploaded_today: 1, pending: 4, failed: 1, connected: 2 },
    next: job({ title: "Afternoon" }),
    today: [job({ title: "Morning", status: "completed", scheduled_at: "2026-09-19T01:00:00+00:00" }), job({ title: "Afternoon", scheduled_at: "2026-09-19T06:00:00+00:00" })],
  });
  for (const text of ["Video Publisher", "Scheduled today", "Uploaded today", "Pending uploads", "Failed uploads",
    "Connected accounts", "Next scheduled video", "Today's Schedule", "Upload Video", "Schedule Video"]) assert.ok(html.includes(text), text);
  assert.ok(html.includes("08:00</span>") && html.includes("13:00</span>"));
  assert.ok(html.includes("2 / 2"));
});

test("videos page", () => {
  const html = V.videosView({ videos: [{ ...video, created_at: "2026-09-19T01:00:00+00:00", thumbnail_url: null, status: "not published", published_to: [], exists: true }], tz: TZ, downloads: [{ id: 1, url: "https://www.tiktok.com/@a/video/1", status: "completed", video_id: 7 }] });
  assert.ok(html.includes("Duration: 00:42") && html.includes("Size: 18.0 MB"));
  assert.ok(html.includes('href="#/upload?video=7&amp;when=schedule">Schedule</a>'));
  assert.ok(html.includes('data-action="delete-video" data-video="7"'));
  assert.ok(html.includes("Download &amp; Schedule"));
  assert.ok(html.includes("✓ Added to the library"));
});

test("settings never display secrets", () => {
  const html = V.settingsView({
    settings: { timezone: TZ, max_attempts: 4, retry_delays: [60, 300, 900], missed_policy: "hold", missed_policies: { hold: "Ask me" },
      missed_grace_minutes: 10, youtube_privacy: "public", facebook_tags_as_hashtags: true, video_directory: "",
      effective_video_directory: "C:/data/videos", data_directory: "C:/data", log_file: "C:/data/logs/publisher.log",
      token_storage: "operating system credential store", notify_completed: true, notify_failed: true, browser_notifications: false,
      credentials: { youtube: { configured: true, setup_hint: "" }, facebook: { configured: false, setup_hint: "Set BOTTLE_NET_FACEBOOK_APP_ID" } } },
    timezones: ["Asia/Bangkok", "UTC"], templates: [{ id: 1, name: "Tutorial", title: "{filename}" }] });
  assert.ok(html.includes('<option value="Asia/Bangkok" selected>'));
  assert.ok(html.includes('value="1, 5, 15"'));
  assert.ok(html.includes("✓ configured") && html.includes("not configured"));
  assert.ok(html.includes("Tutorial"));
  assert.equal(/password|secret/i.test(html), false);
});

test("toasts offer Retry for failed uploads", () => {
  const html = V.toastView({ level: "error", title: "Facebook upload failed", message: "Temporary API error", platform_job_id: 5 });
  assert.ok(html.includes('role="alert"') && html.includes("⚠") && html.includes('data-action="retry" data-pj="5"'));
  assert.ok(V.toastView({ level: "success", title: "Video uploaded successfully" }).includes("✓"));
});

// ------------------------------------------------------------------ format

test("format helpers", () => {
  assert.equal(F.formatBytes(18000000), "18.0 MB");
  assert.equal(F.formatDuration(42), "00:42");
  assert.equal(F.formatDuration(3725), "1:02:05");
  assert.equal(F.localDateKey("2026-09-19T20:00:00Z", TZ), "2026-09-20");
  assert.equal(F.formatTime("2026-09-19T13:00:00Z", TZ), "20:00");
  assert.equal(F.renderTemplate("{filename} {date} {time}", { filename: "a.b.mp4", date: "2026-09-19", time: "20:00" }), "a.b 2026-09-19 20:00");
  const grid = F.monthGrid(2026, 9);
  assert.equal(grid[0].key, "2026-08-31"); // Monday before 1 September 2026 (a Tuesday)
  assert.equal(grid.length % 7, 0);
  assert.ok(F.isVideoFile("clip.MP4") && !F.isVideoFile("notes.txt"));
  assert.equal(F.esc(`<a href="x">'&`), "&lt;a href=&quot;x&quot;&gt;&#39;&amp;");
});
