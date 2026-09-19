// View functions: data in, HTML string out. No DOM access, so they are
// unit-tested with Node (tests/web). Every value from the server is escaped.

import {
  PLATFORM_NAMES, esc, formatBytes, formatDate, formatDateTime, formatDuration, formatTime,
  localDateKey, monthGrid, statusIcon, statusLabel,
} from "./format.js";

export const NAV = [
  ["dashboard", "Dashboard"],
  ["videos", "Videos"],
  ["scheduler", "Scheduler"],
  ["queue", "Upload Queue"],
  ["history", "History"],
  ["accounts", "Accounts"],
  ["settings", "Settings"],
];

const CANCELLABLE = ["scheduled", "queued", "retrying", "missed", "uploading"];
const RETRYABLE = ["failed", "cancelled", "missed"];
// Bottle Net's website (docs/ on GitHub Pages); GitHub redirects it to the custom domain once one is set.
const SITE_URL = "https://harry-khit-htoo.github.io/MediaCrawlerBottle-Net/";

function safeLink(url, text) {
  return /^https:\/\//.test(url || "")
    ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(text)}</a>`
    : esc(text);
}

function statusChip(status) {
  return `<span class="status status-${esc(status)}"><span aria-hidden="true">${statusIcon(status)}</span> ${esc(statusLabel(status))}</span>`;
}

export function navView(active) {
  return NAV.map(([id, label]) =>
    `<a href="#/${id}" class="nav-link${id === active ? " active" : ""}"${id === active ? ' aria-current="page"' : ""}>${esc(label)}</a>`,
  ).join("");
}

// ------------------------------------------------------------------ accounts

export function accountCard(account) {
  const name = esc(account.name);
  let body;
  if (account.connected) {
    const label = account.platform === "facebook" ? "Page" : "Account";
    body = `<p class="account-state ok">✓ Connected</p>
      <p>${label}: <strong>${esc(account.display_name)}</strong></p>
      ${account.detail && account.platform === "youtube" ? `<p class="muted">Channel: ${esc(account.detail)}</p>` : ""}
      <button type="button" class="btn" data-action="disconnect" data-platform="${esc(account.platform)}">Disconnect</button>`;
  } else if ((account.pending_pages || []).length) {
    const options = account.pending_pages.map((page, i) =>
      `<label class="choice"><input type="radio" name="facebook-page" value="${esc(page.id)}"${i === 0 ? " checked" : ""}> ${esc(page.name)}</label>`).join("");
    body = `<p class="account-state">Choose the Page to publish to</p>
      <fieldset class="page-choice"><legend class="sr-only">Facebook Page</legend>${options}</fieldset>
      <button type="button" class="btn primary" data-action="select-page">Use this Page</button>`;
  } else if (!account.configured) {
    body = `<p class="account-state">Not connected</p>
      <p class="muted">Setup required: ${esc(account.setup_hint)}</p>
      <p class="muted">Redirect URI to register: <code>${esc(account.redirect_uri)}</code></p>
      <button type="button" class="btn primary" disabled>Connect ${name}</button>`;
  } else {
    body = `<p class="account-state">Not connected</p>
      <button type="button" class="btn primary" data-action="connect" data-platform="${esc(account.platform)}">Connect ${name}</button>`;
  }
  return `<section class="card account-card" aria-labelledby="acct-${esc(account.platform)}">
    <h2 id="acct-${esc(account.platform)}">${name}</h2>${body}</section>`;
}

export function accountsView(accounts, { waiting = null } = {}) {
  return `<h1>Connected Accounts</h1>
    <p class="muted">You sign in on Google's and Facebook's own websites. Bottle Net never asks for or stores your password.</p>
    ${waiting ? `<p class="notice" role="status">Waiting for you to finish signing in to ${esc(PLATFORM_NAMES[waiting])} in the browser tab that opened…</p>` : ""}
    <div class="grid two">${accounts.map(accountCard).join("")}</div>
    <p class="muted" id="accounts-policies">Bottle Net uses YouTube API Services. By connecting YouTube you agree to the
      ${safeLink("https://www.youtube.com/t/terms", "YouTube Terms of Service")}. See the
      ${safeLink(`${SITE_URL}privacy.html`, "Bottle Net Privacy Policy")},
      ${safeLink(`${SITE_URL}terms.html`, "Bottle Net Terms of Service")} and
      ${safeLink("https://policies.google.com/privacy", "Google Privacy Policy")}.
      You can remove access at any time with Disconnect or in your
      ${safeLink("https://myaccount.google.com/permissions", "Google security settings")}.</p>`;
}

// ----------------------------------------------------------------- dashboard

export function platformIcons(job) {
  return job.platform_jobs.map((pj) =>
    `<span class="pj-icon status-${esc(pj.status)}" title="${esc(PLATFORM_NAMES[pj.platform])}: ${esc(statusLabel(pj.status))}">${esc(PLATFORM_NAMES[pj.platform])} ${statusIcon(pj.status)}</span>`,
  ).join(" ");
}

export function dashboardView(data) {
  const tz = data.timezone;
  const s = data.stats;
  const next = data.next
    ? `${esc(formatDateTime(data.next.scheduled_at, tz))} · ${esc(data.next.title || data.next.video)}`
    : "Nothing scheduled";
  const stat = (label, value, id) => `<div class="stat" id="${id}"><span class="stat-value">${esc(value)}</span><span class="stat-label">${esc(label)}</span></div>`;
  const accounts = data.accounts.map((a) =>
    `<li><span>${esc(a.name)}</span> ${a.connected ? '<span class="ok">✓ Connected</span>' : '<a href="#/accounts">Not connected</a>'}</li>`).join("");
  const today = data.today.length
    ? data.today.map((job) => `<li class="schedule-row">
        <span class="time">${esc(formatTime(job.scheduled_at || job.created_at, tz))}</span>
        <span class="name">${esc(job.title || job.video)}</span>
        ${statusChip(job.status)}</li>`).join("")
    : '<li class="muted">Nothing scheduled for today.</li>';
  return `<header class="page-header"><div><p class="eyebrow">Bottle Net</p><h1>Video Publisher</h1></div>
      <p class="muted">Timezone: ${esc(tz)}</p></header>
    ${data.scheduler_running ? "" : '<p class="notice warning" role="alert">The scheduler is not running in this window (another Bottle Net window is running it). Scheduled uploads still run there.</p>'}
    <div class="stats">
      ${stat("Scheduled today", s.scheduled_today, "stat-scheduled")}
      ${stat("Uploaded today", s.uploaded_today, "stat-uploaded")}
      ${stat("Pending uploads", s.pending, "stat-pending")}
      ${stat("Failed uploads", s.failed, "stat-failed")}
      ${stat("Connected accounts", `${s.connected} / ${data.accounts.length}`, "stat-connected")}
    </div>
    <div class="grid two">
      <section class="card"><h2>Connected Accounts</h2><ul class="plain">${accounts}</ul></section>
      <section class="card"><h2>Next scheduled video</h2><p>${next}</p></section>
    </div>
    <section class="card"><h2>Today's Schedule</h2><ul class="plain schedule">${today}</ul></section>
    <section class="card"><h2>Quick Actions</h2>
      <div class="actions"><a class="btn primary" href="#/upload">Upload Video</a>
      <a class="btn" href="#/upload?when=schedule">Schedule Video</a></div></section>`;
}

// ---------------------------------------------------------------- jobs/queue

export function pjActions(pj) {
  const buttons = [];
  if (pj.status === "missed") {
    buttons.push(`<button type="button" class="btn small primary" data-action="retry" data-pj="${pj.id}">Publish now</button>`);
  } else if (RETRYABLE.includes(pj.status)) {
    buttons.push(`<button type="button" class="btn small primary" data-action="retry" data-pj="${pj.id}">Retry ${esc(PLATFORM_NAMES[pj.platform])}</button>`);
  }
  if (CANCELLABLE.includes(pj.status) && !pj.cancel_requested) {
    buttons.push(`<button type="button" class="btn small" data-action="cancel-pj" data-pj="${pj.id}">Cancel</button>`);
  }
  if (pj.status === "completed") {
    buttons.push(`<button type="button" class="btn small" data-action="publish-again" data-pj="${pj.id}">Publish Again</button>`);
  }
  return buttons.join(" ");
}

export function pjLine(pj, tz) {
  const name = PLATFORM_NAMES[pj.platform];
  let detail = "";
  if (pj.status === "uploading") {
    detail = `<progress max="100" value="${Number(pj.progress) || 0}" aria-label="${esc(name)} upload progress"></progress>
      <span class="muted">${Math.round(Number(pj.progress) || 0)}%${pj.cancel_requested ? " · cancelling…" : ""}</span>`;
  } else if (pj.status === "completed") {
    const id = pj.remote_post_id || pj.remote_id;
    detail = `<span class="muted">ID: ${safeLink(pj.remote_url, id)}</span>`;
    if (pj.warnings?.length) detail += `<p class="warning-text">${esc(pj.warnings.join(" "))}</p>`;
  } else if (pj.status === "retrying") {
    detail = `<span class="muted">Next attempt at ${esc(formatTime(pj.due_at, tz))} (attempt ${pj.attempts + 1})</span>`;
  }
  const error = ["failed", "retrying", "missed", "cancelled"].includes(pj.status) && pj.last_error
    ? `<p class="error-text">${esc(pj.last_error)}</p>` : "";
  return `<li class="pj pj-${esc(pj.status)}" data-pj-id="${pj.id}">
      <div class="pj-main"><strong>${esc(name)}</strong> ${statusChip(pj.status)} ${detail}</div>
      ${error}<div class="pj-actions">${pjActions(pj)}</div></li>`;
}

export function jobCard(job, tz) {
  const when = job.scheduled_at
    ? `Scheduled: ${esc(formatDateTime(job.scheduled_at, tz))}${job.schedule_mode === "platform" ? " (published by the platform)" : ""}`
    : `Publish now · added ${esc(formatDateTime(job.created_at, tz))}`;
  const editable = job.platform_jobs.every((pj) => ["scheduled", "missed", "cancelled"].includes(pj.status))
    && job.platform_jobs.some((pj) => pj.status !== "cancelled") && job.scheduled_at;
  const cancellable = job.platform_jobs.some((pj) => CANCELLABLE.includes(pj.status) && !pj.cancel_requested);
  const actions = [
    editable ? `<button type="button" class="btn small" data-action="edit-job" data-job="${job.id}">Edit</button>` : "",
    cancellable ? `<button type="button" class="btn small danger" data-action="cancel-job" data-job="${job.id}">Cancel upload</button>` : "",
  ].join(" ");
  return `<article class="card job-card" data-job-id="${job.id}" aria-label="${esc(job.video)}">
      <header class="job-header"><div><h2>${esc(job.video)}</h2><p class="muted">${esc(job.title)}</p></div>${statusChip(job.status)}</header>
      <p class="muted">Job #${job.id} · ${when}</p>
      <ul class="plain pj-list">${job.platform_jobs.map((pj) => pjLine(pj, tz)).join("")}</ul>
      <div class="actions">${actions}</div></article>`;
}

export function queueView(jobs, tz) {
  return `<h1>Upload Queue</h1>
    <p class="muted">Active uploads and uploads from the last 24 hours. Each platform is uploaded and retried independently.</p>
    ${jobs.length ? jobs.map((job) => jobCard(job, tz)).join("") : '<p class="empty">The queue is empty. <a href="#/upload">Upload a video</a>.</p>'}`;
}

export const HISTORY_FILTERS = [
  ["all", "All"], ["youtube", "YouTube"], ["facebook", "Facebook"],
  ["completed", "Completed"], ["failed", "Failed"], ["pending", "Pending"],
];

export function historyView(rows, filter, tz) {
  const filters = HISTORY_FILTERS.map(([id, label]) =>
    `<button type="button" class="chip${id === filter ? " active" : ""}" data-action="history-filter" data-filter="${id}" aria-pressed="${id === filter}">${esc(label)}</button>`).join("");
  const body = rows.map((row) => {
    const when = row.completed_at || row.scheduled_at || row.created_at;
    // The platform is its own column, so show the explanation without the "X upload failed." headline.
    const parts = String(row.last_error || "").split(/\n\n/);
    const reason = parts.length > 1 ? parts[1] : parts[0];
    return `<tr>
      <td class="video-cell" title="${esc(row.video)}">${esc(row.video)}</td><td>${esc(PLATFORM_NAMES[row.platform])}</td>
      <td class="nowrap">${esc(formatDate(when, tz))}</td><td class="nowrap">${esc(formatTime(when, tz))}</td>
      <td>${statusChip(row.status)}</td>
      <td>${row.remote_id ? safeLink(row.remote_url, row.remote_post_id || row.remote_id) : ""}</td>
      <td class="error-cell">${["failed", "retrying", "missed"].includes(row.status) ? esc(reason) : ""}</td>
      <td>${row.retry_count}</td>
      <td>${RETRYABLE.includes(row.status) ? `<button type="button" class="btn small" data-action="retry" data-pj="${row.id}">Retry</button>` : ""}</td></tr>`;
  }).join("");
  return `<h1>History</h1>
    <div class="filters" role="group" aria-label="Filter history">${filters}</div>
    ${rows.length ? `<div class="table-wrap"><table>
      <thead><tr><th scope="col">Video</th><th scope="col">Platform</th><th scope="col">Date</th><th scope="col">Time</th>
      <th scope="col">Status</th><th scope="col">Platform ID</th><th scope="col">Error</th><th scope="col">Retries</th><th scope="col"><span class="sr-only">Actions</span></th></tr></thead>
      <tbody>${body}</tbody></table></div>` : '<p class="empty">No uploads match this filter.</p>'}`;
}

// -------------------------------------------------------------------- videos

export function videoCard(video, tz) {
  const thumb = video.thumbnail_url
    ? `<img src="${esc(video.thumbnail_url)}" alt="" loading="lazy">`
    : '<span class="thumb-placeholder" aria-hidden="true">▶</span>';
  return `<article class="card video-card" data-video-id="${video.id}">
      <button type="button" class="thumb" data-action="preview" data-video="${video.id}" aria-label="Preview ${esc(video.filename)}">${thumb}</button>
      <h2 class="video-name">${esc(video.filename)}</h2>
      <p class="muted">Duration: ${esc(formatDuration(video.duration))} · Size: ${esc(formatBytes(video.size))}</p>
      <p class="muted">Added ${esc(formatDate(video.created_at, tz))}${video.source === "download" ? " · downloaded" : ""}</p>
      <p>${statusChip(video.status === "published" ? "completed" : video.status === "not published" ? "unknown" : video.status)}
        ${video.published_to.length ? `<span class="muted">on ${esc(video.published_to.map((p) => PLATFORM_NAMES[p]).join(", "))}</span>` : ""}</p>
      ${video.exists ? "" : '<p class="error-text">The file is missing from disk.</p>'}
      <div class="actions">
        <a class="btn small primary" href="#/upload?video=${video.id}&amp;when=schedule">Schedule</a>
        <a class="btn small" href="#/upload?video=${video.id}">Publish</a>
        <button type="button" class="btn small danger" data-action="delete-video" data-video="${video.id}">Delete</button>
      </div></article>`;
}

export const VIDEO_FILTERS = [["", "All"], ["not published", "Not published"], ["scheduled", "Scheduled"], ["published", "Published"], ["failed", "Failed"]];

export function downloadRow(task) {
  const state = task.status === "downloading"
    ? `<progress max="100" value="${Number(task.progress) || 0}" aria-label="Download progress"></progress>`
    : task.status === "completed"
      ? `<span class="ok">✓ Added to the library</span> <a class="btn small primary" href="#/upload?video=${task.video_id}&amp;when=schedule">Schedule</a>`
      : `<span class="error-text">${esc(task.error)}</span>`;
  return `<li class="download-row"><span class="url">${esc(task.url)}</span> ${state}</li>`;
}

export function videosView({ videos, search = "", status = "", downloads = [], tz }) {
  const options = VIDEO_FILTERS.map(([value, label]) => `<option value="${esc(value)}"${value === status ? " selected" : ""}>${esc(label)}</option>`).join("");
  return `<header class="page-header"><h1>Videos</h1>
      <div class="actions"><a class="btn primary" href="#/upload">Upload / Import video</a>
      <button type="button" class="btn" data-action="import-downloads">Import Bottle Net downloads</button></div></header>
    <section class="card"><h2>Download &amp; Schedule</h2>
      <p class="muted">Download a TikTok or Facebook video with Bottle Net, then schedule it. Only publish videos you own or have permission to publish.</p>
      <form id="download-form" class="inline-form"><label for="download-url" class="sr-only">Video URL</label>
      <input id="download-url" type="url" placeholder="https://www.tiktok.com/@user/video/…" required>
      <button type="submit" class="btn">Download</button></form>
      ${downloads.length ? `<ul class="plain">${downloads.map(downloadRow).join("")}</ul>` : ""}</section>
    <div class="toolbar"><label for="video-search" class="sr-only">Search videos</label>
      <input id="video-search" type="search" placeholder="Search videos" value="${esc(search)}">
      <label for="video-filter" class="sr-only">Filter videos</label><select id="video-filter">${options}</select></div>
    ${videos.length ? `<div class="video-grid">${videos.map((v) => videoCard(v, tz)).join("")}</div>`
      : '<p class="empty">No videos yet. Upload a video or import your Bottle Net downloads.</p>'}`;
}

// -------------------------------------------------------------- upload form

export function uploadFormView({ video = null, accounts = [], templates = [], settings, defaults }) {
  const byPlatform = Object.fromEntries(accounts.map((a) => [a.platform, a]));
  const platformBox = (platform) => {
    const account = byPlatform[platform] || {};
    const connected = Boolean(account.connected);
    return `<label class="choice${connected ? "" : " disabled"}"><input type="checkbox" name="platform" value="${platform}"${connected ? " checked" : " disabled"}>
      ${esc(PLATFORM_NAMES[platform])}${platform === "facebook" ? " Page" : ""}
      ${connected ? `<span class="muted">(${esc(account.display_name)})</span>` : ' <a href="#/accounts">Connect first</a>'}</label>`;
  };
  const videoSection = video
    ? `<div class="selected-video"><video controls preload="metadata" src="${esc(video.preview_url)}" aria-label="Preview of ${esc(video.filename)}"></video>
        <p><strong>${esc(video.filename)}</strong> · ${esc(formatDuration(video.duration))} · ${esc(formatBytes(video.size))}</p>
        <button type="button" class="btn small" data-action="change-video">Choose another video</button></div>`
    : `<div class="dropzone" id="dropzone"><p class="drop-title">Drag &amp; Drop Video Here</p><p>or</p>
        <label class="btn primary" for="video-file">Choose File</label>
        <input id="video-file" type="file" accept="video/*,.mkv" class="visually-hidden-input">
        <p class="muted" id="upload-status" role="status" aria-live="polite"></p></div>`;
  const templateOptions = templates.map((t) => `<option value="${t.id}">${esc(t.name)}</option>`).join("");
  const privacy = ["public", "unlisted", "private"].map((p) =>
    `<option value="${p}"${p === settings.youtube_privacy ? " selected" : ""}>${p[0].toUpperCase() + p.slice(1)}</option>`).join("");
  const schedule = defaults.when === "schedule";
  return `<h1>Upload Video</h1>
    <form id="upload-form" novalidate>
      <fieldset class="card"><legend>Video</legend>${videoSection}</fieldset>
      <fieldset class="card"><legend>Details</legend>
        ${templates.length ? `<label for="template-select">Template</label>
          <select id="template-select"><option value="">No template</option>${templateOptions}</select>` : ""}
        <label for="upload-title">Title</label>
        <input id="upload-title" name="title" type="text" maxlength="255" required value="${esc(defaults.title || "")}">
        <label for="upload-description">Description</label>
        <textarea id="upload-description" name="description" rows="5">${esc(defaults.description || "")}</textarea>
        <label for="upload-tags">Tags <span class="muted">(comma separated)</span></label>
        <input id="upload-tags" name="tags" type="text" placeholder="video, tutorial, technology" value="${esc(defaults.tags || "")}">
        <p class="muted">Placeholders: {filename}, {date}, {time}</p>
        <label for="thumbnail-file">Thumbnail <span class="muted">(optional, JPEG or PNG)</span></label>
        <input id="thumbnail-file" type="file" accept="image/jpeg,image/png">
      </fieldset>
      <fieldset class="card"><legend>Publish To</legend>${platformBox("youtube")}${platformBox("facebook")}
        <label for="upload-privacy">YouTube privacy</label><select id="upload-privacy">${privacy}</select>
        <label class="choice"><input type="checkbox" id="made-for-kids"> This video is made for kids (YouTube)</label>
      </fieldset>
      <fieldset class="card"><legend>Publishing</legend>
        <label class="choice"><input type="radio" name="when" value="now"${schedule ? "" : " checked"}> Publish Now</label>
        <label class="choice"><input type="radio" name="when" value="schedule"${schedule ? " checked" : ""}> Schedule</label>
        <div class="schedule-fields"${schedule ? "" : " hidden"}>
          <div class="row"><div><label for="upload-date">Date</label><input id="upload-date" type="date" value="${esc(defaults.date)}"></div>
          <div><label for="upload-time">Time</label><input id="upload-time" type="time" value="${esc(defaults.time)}"></div></div>
          <p class="muted">Timezone: ${esc(settings.timezone)}</p>
          <label class="choice"><input type="checkbox" id="platform-scheduling"> Upload now and let YouTube/Facebook publish at this time (works even if Bottle Net is closed)</label>
        </div>
      </fieldset>
      ${video && video.source === "download" ? `<div class="card notice warning"><label class="choice"><input type="checkbox" id="rights-confirmed" required>
        I own this downloaded video or have permission to publish it.</label></div>` : ""}
      <p class="error-text" id="form-error" role="alert"></p>
      <div class="actions"><button type="submit" class="btn primary" id="submit-upload"${video ? "" : " disabled"}>${schedule ? "Schedule Video" : "Publish Now"}</button></div>
    </form>`;
}

// ----------------------------------------------------------------- calendar

export function calendarView({ year, month, jobs, tz, todayKey }) {
  const monthName = new Intl.DateTimeFormat("en-GB", { month: "long", year: "numeric", timeZone: "UTC" })
    .format(new Date(Date.UTC(year, month - 1, 1)));
  const byDay = {};
  for (const job of jobs) {
    const key = localDateKey(job.scheduled_at || job.created_at, tz);
    (byDay[key] ||= []).push(job);
  }
  const heads = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map((d) => `<div class="cal-head" role="columnheader">${d}</div>`).join("");
  const cells = monthGrid(year, month).map((day) => {
    const items = (byDay[day.key] || []).sort((a, b) => (a.scheduled_at || a.created_at).localeCompare(b.scheduled_at || b.created_at));
    const chips = items.map((job) => {
      const movable = Boolean(job.scheduled_at) && job.platform_jobs.every((pj) => ["scheduled", "missed", "cancelled"].includes(pj.status));
      return `<button type="button" class="cal-job status-${esc(job.status)}" data-action="open-job" data-job="${job.id}"${movable ? ' draggable="true"' : ""}
        aria-label="${esc(formatTime(job.scheduled_at || job.created_at, tz))} ${esc(job.title || job.video)}, ${esc(statusLabel(job.status))}">
        <span class="time">${esc(formatTime(job.scheduled_at || job.created_at, tz))}</span> ${esc(job.title || job.video)} <span aria-hidden="true">${statusIcon(job.status)}</span></button>`;
    }).join("");
    return `<div class="cal-day${day.inMonth ? "" : " other"}${day.key === todayKey ? " today" : ""}" data-date="${day.key}" role="gridcell">
      <div class="cal-day-head"><span class="cal-num">${day.day}</span>
      <a class="cal-add" href="#/upload?when=schedule&amp;date=${day.key}" aria-label="Schedule a video on ${day.key}">+</a></div>${chips}</div>`;
  }).join("");
  return `<header class="page-header"><h1>Scheduler</h1>
      <div class="actions"><button type="button" class="btn" data-action="month" data-delta="-1" aria-label="Previous month">‹</button>
      <h2 class="month-title">${esc(monthName)}</h2>
      <button type="button" class="btn" data-action="month" data-delta="1" aria-label="Next month">›</button>
      <button type="button" class="btn" data-action="month" data-delta="0">Today</button>
      <a class="btn primary" href="#/upload?when=schedule">Schedule Video</a></div></header>
    <p class="muted">Timezone: ${esc(tz)}. Drag an upload to another day to move it (the time stays the same), or select it to edit.</p>
    <div class="calendar" role="grid" aria-label="${esc(monthName)}">${heads}${cells}</div>`;
}

export function jobDialogView(job, tz) {
  const editable = Boolean(job.scheduled_at) && job.platform_jobs.every((pj) => ["scheduled", "missed", "cancelled"].includes(pj.status));
  const date = job.scheduled_at ? localDateKey(job.scheduled_at, tz) : "";
  const time = job.scheduled_at ? formatTime(job.scheduled_at, tz) : "";
  const form = editable ? `<form id="job-edit-form" data-job="${job.id}">
      <label for="edit-title">Title</label><input id="edit-title" type="text" value="${esc(job.title)}" required>
      <label for="edit-description">Description</label><textarea id="edit-description" rows="4">${esc(job.description)}</textarea>
      <label for="edit-tags">Tags</label><input id="edit-tags" type="text" value="${esc(job.tags.join(", "))}">
      <div class="row"><div><label for="edit-date">Date</label><input id="edit-date" type="date" value="${esc(date)}"></div>
      <div><label for="edit-time">Time</label><input id="edit-time" type="time" value="${esc(time)}"></div></div>
      <p class="error-text" id="edit-error" role="alert"></p>
      <div class="actions"><button type="submit" class="btn primary">Save changes</button></div></form>` : "";
  return `<h2 id="dialog-title">${esc(job.title || job.video)}</h2>
    <p class="muted">${esc(job.video)} · Job #${job.id}${job.scheduled_at ? ` · ${esc(formatDateTime(job.scheduled_at, tz))}` : ""}</p>
    <ul class="plain pj-list">${job.platform_jobs.map((pj) => pjLine(pj, tz)).join("")}</ul>
    ${form}
    <div class="actions">${job.platform_jobs.some((pj) => CANCELLABLE.includes(pj.status)) ? `<button type="button" class="btn danger" data-action="cancel-job" data-job="${job.id}">Cancel upload</button>` : ""}
    <button type="button" class="btn" data-action="close-dialog">Close</button></div>`;
}

// ----------------------------------------------------------------- settings

export function templateRow(template) {
  return `<li class="template-row"><strong>${esc(template.name)}</strong>
    <span class="muted">${esc(template.title)}</span>
    <button type="button" class="btn small" data-action="edit-template" data-template="${template.id}">Edit</button>
    <button type="button" class="btn small danger" data-action="delete-template" data-template="${template.id}">Delete</button></li>`;
}

export function settingsView({ settings, timezones, templates }) {
  const tzOptions = timezones.map((tz) => `<option value="${esc(tz)}"${tz === settings.timezone ? " selected" : ""}>${esc(tz)}</option>`).join("");
  const missed = Object.entries(settings.missed_policies).map(([value, label]) =>
    `<option value="${esc(value)}"${value === settings.missed_policy ? " selected" : ""}>${esc(label)}</option>`).join("");
  const credential = (platform, name) => {
    const c = settings.credentials[platform];
    return `<p>${esc(name)} API credentials: ${c.configured ? '<span class="ok">✓ configured</span>' : '<span class="warning-text">not configured</span>'}</p>
      ${c.configured ? "" : `<p class="muted">${esc(c.setup_hint)}</p>`}`;
  };
  const check = (id, key, label) => `<label class="choice"><input type="checkbox" id="${id}" data-setting="${key}"${settings[key] ? " checked" : ""}> ${esc(label)}</label>`;
  return `<h1>Settings</h1>
    <form id="settings-form" novalidate>
      <fieldset class="card"><legend>General</legend>
        <label for="set-timezone">Timezone</label><select id="set-timezone" data-setting="timezone">${tzOptions}</select></fieldset>
      <fieldset class="card"><legend>Upload</legend>
        <label for="set-attempts">Maximum attempts (first try + automatic retries)</label>
        <input id="set-attempts" type="number" min="1" max="10" data-setting="max_attempts" value="${settings.max_attempts}">
        <label for="set-delays">Retry delays in minutes (comma separated)</label>
        <input id="set-delays" type="text" data-setting="retry_delays" value="${esc(settings.retry_delays.map((s) => s / 60).join(", "))}">
        <label for="set-missed">If Bottle Net was not running at a scheduled time</label>
        <select id="set-missed" data-setting="missed_policy">${missed}</select>
        <label for="set-grace">Still publish uploads that are up to this many minutes late</label>
        <input id="set-grace" type="number" min="0" max="1440" data-setting="missed_grace_minutes" value="${settings.missed_grace_minutes}">
        <label for="set-privacy">Default YouTube privacy</label>
        <select id="set-privacy" data-setting="youtube_privacy">${["public", "unlisted", "private"].map((p) => `<option value="${p}"${p === settings.youtube_privacy ? " selected" : ""}>${p}</option>`).join("")}</select>
        ${check("set-hashtags", "facebook_tags_as_hashtags", "Add tags to Facebook descriptions as hashtags")}</fieldset>
      <fieldset class="card"><legend>YouTube</legend>${credential("youtube", "YouTube")}<p><a href="#/accounts">Manage the connected account</a></p></fieldset>
      <fieldset class="card"><legend>Facebook</legend>${credential("facebook", "Facebook")}<p><a href="#/accounts">Manage the connected Page</a></p></fieldset>
      <fieldset class="card"><legend>Storage</legend>
        <label for="set-video-dir">Video directory <span class="muted">(empty = default)</span></label>
        <input id="set-video-dir" type="text" data-setting="video_directory" value="${esc(settings.video_directory)}" placeholder="${esc(settings.effective_video_directory)}">
        <p class="muted">Data folder: ${esc(settings.data_directory)}<br>Log file: ${esc(settings.log_file)}<br>Tokens are kept in: ${esc(settings.token_storage)}</p></fieldset>
      <fieldset class="card"><legend>Notifications</legend>
        ${check("set-notify-ok", "notify_completed", "Upload completed")}
        ${check("set-notify-fail", "notify_failed", "Upload failed")}
        ${check("set-browser", "browser_notifications", "Also show desktop notifications (browser)")}</fieldset>
      <p class="error-text" id="settings-error" role="alert"></p>
      <div class="actions"><button type="submit" class="btn primary">Save settings</button></div>
    </form>
    <section class="card"><h2>Metadata templates</h2>
      <p class="muted">Reusable titles, descriptions and tags. Placeholders: {filename}, {date}, {time}.</p>
      <ul class="plain">${templates.map(templateRow).join("") || '<li class="muted">No templates yet.</li>'}</ul>
      <form id="template-form"><input type="hidden" id="template-id" value="">
        <label for="template-name">Template name</label><input id="template-name" type="text" required>
        <label for="template-title">Title</label><input id="template-title" type="text" placeholder="{filename}">
        <label for="template-description">Description</label><textarea id="template-description" rows="3"></textarea>
        <label for="template-tags">Tags</label><input id="template-tags" type="text" placeholder="tutorial, technology">
        <div class="actions"><button type="submit" class="btn">Save template</button></div></form></section>`;
}

// --------------------------------------------------------------------- toast

export function toastView(notification) {
  const icon = { success: "✓", error: "⚠", warning: "⚠", info: "ℹ" }[notification.level] || "ℹ";
  const retry = notification.level === "error" && notification.platform_job_id
    ? `<button type="button" class="btn small" data-action="retry" data-pj="${notification.platform_job_id}">Retry</button>` : "";
  return `<div class="toast toast-${esc(notification.level)}" role="${notification.level === "error" ? "alert" : "status"}">
      <p class="toast-title"><span aria-hidden="true">${icon}</span> ${esc(notification.title)}</p>
      ${notification.message ? `<p class="toast-message">${esc(notification.message)}</p>` : ""}
      <div class="actions">${retry}<button type="button" class="btn small" data-action="dismiss-toast">Dismiss</button></div></div>`;
}
