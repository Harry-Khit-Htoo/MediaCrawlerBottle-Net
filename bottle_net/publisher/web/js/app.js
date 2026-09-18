// Bottle Net - Video Publisher: browser controller.
// Talks only to the local Bottle Net service (/api/...), never to YouTube or
// Facebook directly, and never handles passwords or OAuth tokens.

import * as V from "./views.js";
import { isVideoFile, localParts } from "./format.js";

const TOKEN = document.querySelector('meta[name="bn-token"]').content;
const main = document.getElementById("main");
const navEl = document.getElementById("nav");
const toastsEl = document.getElementById("toasts");
const dialog = document.getElementById("dialog");
const dialogBody = document.getElementById("dialog-body");

const LIVE_ROUTES = new Set(["dashboard", "queue", "history", "scheduler", "accounts", "videos"]);
const state = {
  route: "",
  params: new URLSearchParams(),
  settings: null,
  accounts: [],
  templates: [],
  html: "",
  calendar: null,
  historyFilter: "all",
  videoSearch: "",
  videoStatus: "",
  waitingFor: null,
  waitingSince: 0,
  shownNotifications: new Set(),
  uploadVideo: null,
  uploadThumbnail: null,
  capturing: new Set(),
};

// ------------------------------------------------------------------- helpers

async function api(method, path, body, { raw, headers = {} } = {}) {
  const init = { method, credentials: "same-origin", headers: { "X-Bottle-Net-Token": TOKEN, ...headers } };
  if (raw !== undefined) {
    init.body = raw;
  } else if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const response = await fetch(path, init);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(data.error || `Request failed (HTTP ${response.status})`);
    Object.assign(error, { code: data.code, status: response.status, data });
    throw error;
  }
  return data;
}

function tz() {
  return state.settings?.timezone || "Asia/Bangkok";
}

function toast(level, title, message = "", extra = {}) {
  const wrapper = document.createElement("div");
  wrapper.innerHTML = V.toastView({ level, title, message, ...extra });
  const el = wrapper.firstElementChild;
  toastsEl.prepend(el);
  if (level !== "error") setTimeout(() => el.remove(), 8000);
}

function showError(error) {
  toast("error", "Something went wrong", error.message || String(error));
}

function parseHash() {
  const hash = location.hash.replace(/^#\/?/, "") || "dashboard";
  const [route, query] = hash.split("?");
  return { route: route || "dashboard", params: new URLSearchParams(query || "") };
}

function defaultSchedule() {
  // Next full hour in the configured timezone.
  const next = new Date(Date.now() + 3600 * 1000);
  const p = localParts(next.toISOString(), tz());
  const date = state.params.get("date") || `${p.year}-${String(p.month).padStart(2, "0")}-${String(p.day).padStart(2, "0")}`;
  return { date, time: `${p.hour}:00` };
}

function focusKey(el) {
  if (!el || el === document.body) return null;
  if (el.id) return `#${CSS.escape(el.id)}`;
  const d = el.dataset || {};
  if (d.action) return `[data-action="${d.action}"]${d.pj ? `[data-pj="${d.pj}"]` : ""}${d.job ? `[data-job="${d.job}"]` : ""}${d.video ? `[data-video="${d.video}"]` : ""}${d.filter ? `[data-filter="${d.filter}"]` : ""}`;
  return null;
}

/** Replace the page content if it changed; returns true when it did. */
function setContent(html, { keepFocus = false } = {}) {
  if (html === state.html) return false;
  const key = keepFocus ? focusKey(document.activeElement) : null;
  state.html = html;
  main.innerHTML = html;
  if (key) main.querySelector(key)?.focus();
  return true;
}

// ---------------------------------------------------------------- rendering

async function loadView(route) {
  switch (route) {
    case "dashboard":
      return V.dashboardView(await api("GET", "/api/dashboard"));
    case "videos": {
      const query = new URLSearchParams({ search: state.videoSearch, status: state.videoStatus });
      const [videos, downloads] = await Promise.all([api("GET", `/api/videos?${query}`), api("GET", "/api/downloads")]);
      queueThumbnailCapture(videos);
      return V.videosView({ videos, downloads, search: state.videoSearch, status: state.videoStatus, tz: tz() });
    }
    case "scheduler": {
      if (!state.calendar) {
        const p = localParts(new Date().toISOString(), tz());
        state.calendar = { year: p.year, month: p.month };
      }
      const { year, month } = state.calendar;
      const from = new Date(Date.UTC(year, month - 1, 1) - 7 * 86400000).toISOString();
      const to = new Date(Date.UTC(year, month, 1) + 8 * 86400000).toISOString();
      const jobs = await api("GET", `/api/jobs?${new URLSearchParams({ from, to })}`);
      const p = localParts(new Date().toISOString(), tz());
      const todayKey = `${p.year}-${String(p.month).padStart(2, "0")}-${String(p.day).padStart(2, "0")}`;
      return V.calendarView({ year, month, jobs, tz: tz(), todayKey });
    }
    case "queue":
      return V.queueView(await api("GET", "/api/jobs"), tz());
    case "history":
      return V.historyView(await api("GET", `/api/history?filter=${state.historyFilter}`), state.historyFilter, tz());
    case "accounts":
      state.accounts = await api("GET", "/api/accounts");
      return V.accountsView(state.accounts, { waiting: state.waitingFor });
    case "settings": {
      const [settings, timezones, templates] = await Promise.all([
        api("GET", "/api/settings"), api("GET", "/api/timezones"), api("GET", "/api/templates")]);
      state.settings = settings;
      state.templates = templates;
      return V.settingsView({ settings, timezones, templates });
    }
    case "upload": {
      const [accounts, templates] = await Promise.all([api("GET", "/api/accounts"), api("GET", "/api/templates")]);
      state.accounts = accounts;
      state.templates = templates;
      const videoId = state.params.get("video");
      state.uploadVideo = videoId ? await api("GET", `/api/videos/${encodeURIComponent(videoId)}`) : null;
      state.uploadThumbnail = null;
      const when = state.params.get("when") === "schedule" || state.params.get("date") ? "schedule" : "now";
      return V.uploadFormView({
        video: state.uploadVideo, accounts, templates, settings: state.settings,
        defaults: { when, ...defaultSchedule(), title: state.uploadVideo ? "{filename}" : "" },
      });
    }
    default:
      return '<h1>Not found</h1><p><a href="#/dashboard">Go to the dashboard</a></p>';
  }
}

async function render({ refresh = false } = {}) {
  const { route, params } = parseHash();
  const navigated = route !== state.route || params.toString() !== state.params.toString();
  if (refresh && navigated) return;
  state.route = route;
  state.params = params;
  navEl.innerHTML = V.navView(route === "upload" ? "videos" : route);
  try {
    const html = await loadView(route);
    if (parseHash().route !== route) return;
    // Keep keyboard focus on re-renders; move it to the heading only on navigation.
    const replaced = setContent(html, { keepFocus: !navigated });
    if (navigated) {
      main.querySelector("h1")?.setAttribute("tabindex", "-1");
      main.querySelector("h1")?.focus({ preventScroll: true });
    }
    if (replaced && route === "upload") wireUploadForm();
  } catch (error) {
    if (!refresh) {
      const box = document.createElement("div");
      box.innerHTML = '<h1>Could not load this page</h1><p class="error-text"></p>';
      box.querySelector("p").textContent = error.message;
      setContent(box.innerHTML);
    }
  }
}

// ------------------------------------------------------------- upload page

function wireUploadForm() {
  const form = document.getElementById("upload-form");
  const dropzone = document.getElementById("dropzone");
  const fileInput = document.getElementById("video-file");
  if (dropzone) {
    ["dragenter", "dragover"].forEach((type) => dropzone.addEventListener(type, (e) => {
      e.preventDefault();
      dropzone.classList.add("dragging");
    }));
    ["dragleave", "drop"].forEach((type) => dropzone.addEventListener(type, () => dropzone.classList.remove("dragging")));
    dropzone.addEventListener("drop", (e) => {
      e.preventDefault();
      if (e.dataTransfer.files.length) uploadVideoFile(e.dataTransfer.files[0]);
    });
  }
  fileInput?.addEventListener("change", () => fileInput.files.length && uploadVideoFile(fileInput.files[0]));

  form.querySelectorAll('input[name="when"]').forEach((radio) => radio.addEventListener("change", () => {
    const schedule = form.querySelector('input[name="when"]:checked').value === "schedule";
    form.querySelector(".schedule-fields").hidden = !schedule;
    document.getElementById("submit-upload").textContent = schedule ? "Schedule Video" : "Publish Now";
  }));

  document.getElementById("template-select")?.addEventListener("change", (e) => {
    const template = state.templates.find((t) => String(t.id) === e.target.value);
    if (!template) return;
    document.getElementById("upload-title").value = template.title;
    document.getElementById("upload-description").value = template.description;
    document.getElementById("upload-tags").value = template.tags.join(", ");
  });

  document.getElementById("thumbnail-file").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    state.uploadThumbnail = null;
    if (!file) return;
    if (!["image/jpeg", "image/png"].includes(file.type) || file.size > 5 * 1024 * 1024) {
      toast("error", "Thumbnail not accepted", "Choose a JPEG or PNG image smaller than 5 MB.");
      e.target.value = "";
      return;
    }
    try {
      state.uploadThumbnail = (await api("POST", "/api/images", undefined, { raw: file })).name;
    } catch (error) {
      showError(error);
    }
  });

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    submitUpload(false);
  });
}

function uploadVideoFile(file) {
  const status = document.getElementById("upload-status");
  if (!isVideoFile(file.name)) {
    status.textContent = "This is not a supported video file (MP4, MOV, WEBM, MKV, …).";
    return;
  }
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/videos/upload");
  xhr.setRequestHeader("X-Bottle-Net-Token", TOKEN);
  xhr.setRequestHeader("X-Filename", encodeURIComponent(file.name));
  xhr.upload.onprogress = (e) => {
    if (e.lengthComputable) status.textContent = `Adding ${file.name}… ${Math.round((100 * e.loaded) / e.total)}%`;
  };
  xhr.onerror = () => { status.textContent = "The upload to Bottle Net failed. Try again."; };
  xhr.onload = async () => {
    let data = {};
    try { data = JSON.parse(xhr.responseText); } catch { /* keep empty */ }
    if (xhr.status !== 201) {
      status.textContent = data.error || "The video could not be added.";
      return;
    }
    status.textContent = "Added. Preparing preview…";
    await capturePreview(data).catch(() => {});
    const params = new URLSearchParams(state.params);
    params.set("video", data.id);
    location.hash = `#/upload?${params}`;
  };
  status.textContent = `Adding ${file.name}…`;
  xhr.send(file);
}

function collectUpload(publishAgain) {
  const form = document.getElementById("upload-form");
  const when = form.querySelector('input[name="when"]:checked').value;
  return {
    video_id: state.uploadVideo?.id,
    title: document.getElementById("upload-title").value,
    description: document.getElementById("upload-description").value,
    tags: document.getElementById("upload-tags").value,
    thumbnail: state.uploadThumbnail,
    platforms: [...form.querySelectorAll('input[name="platform"]:checked')].map((el) => el.value),
    when,
    date: document.getElementById("upload-date").value,
    time: document.getElementById("upload-time").value,
    timezone: tz(),
    platform_scheduling: document.getElementById("platform-scheduling").checked,
    privacy: document.getElementById("upload-privacy").value,
    made_for_kids: document.getElementById("made-for-kids").checked,
    rights_confirmed: document.getElementById("rights-confirmed")?.checked ?? false,
    publish_again: publishAgain,
  };
}

async function submitUpload(publishAgain) {
  const errorEl = document.getElementById("form-error");
  const button = document.getElementById("submit-upload");
  errorEl.textContent = "";
  button.disabled = true;
  try {
    const job = await api("POST", "/api/jobs", collectUpload(publishAgain));
    toast("success", job.scheduled_at ? "Video scheduled" : "Upload started", job.title);
    location.hash = "#/queue";
  } catch (error) {
    if (error.code === "duplicate") {
      errorEl.innerHTML = "";
      errorEl.textContent = error.message;
      const again = document.createElement("button");
      again.type = "button";
      again.className = "btn small";
      again.textContent = "Publish Again";
      again.addEventListener("click", () => {
        if (confirm("This video was already published there. Publish it again anyway?")) submitUpload(true);
      });
      errorEl.append(" ", again);
    } else {
      errorEl.textContent = error.message;
    }
  } finally {
    button.disabled = false;
  }
}

// Capture a thumbnail frame and the duration in the browser (no FFmpeg needed).
function capturePreview(video) {
  return new Promise((resolve, reject) => {
    const el = document.createElement("video");
    el.muted = true;
    el.preload = "auto";
    el.src = video.preview_url;
    const fail = () => {
      const duration = Number.isFinite(el.duration) ? el.duration : null;
      if (duration === null) return reject(new Error("preview unavailable"));
      api("POST", `/api/videos/${video.id}/preview`, undefined, { raw: new Blob([]), headers: { "X-Duration": String(duration) } })
        .then(resolve, reject);
    };
    el.addEventListener("error", fail, { once: true });
    el.addEventListener("loadedmetadata", () => { el.currentTime = Math.min(1, (el.duration || 2) / 4); }, { once: true });
    el.addEventListener("seeked", () => {
      try {
        const canvas = document.createElement("canvas");
        const width = 480;
        canvas.width = width;
        canvas.height = Math.round((width * (el.videoHeight || 9)) / (el.videoWidth || 16));
        canvas.getContext("2d").drawImage(el, 0, 0, canvas.width, canvas.height);
        canvas.toBlob((blob) => {
          if (!blob) return fail();
          api("POST", `/api/videos/${video.id}/preview`, undefined, { raw: blob, headers: { "X-Duration": String(el.duration) } })
            .then(resolve, reject);
        }, "image/jpeg", 0.8);
      } catch {
        fail();
      }
    }, { once: true });
  });
}

function queueThumbnailCapture(videos) {
  const missing = videos.filter((v) => v.exists && !v.thumbnail_url && !state.capturing.has(v.id)).slice(0, 3);
  for (const video of missing) {
    state.capturing.add(video.id);
    capturePreview(video).then(() => state.route === "videos" && render({ refresh: true })).catch(() => {});
  }
}

// ------------------------------------------------------------------ actions

async function openJob(jobId) {
  const job = await api("GET", `/api/jobs/${jobId}`);
  dialogBody.innerHTML = V.jobDialogView(job, tz());
  if (!dialog.open) dialog.showModal();
  document.getElementById("job-edit-form")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await api("PATCH", `/api/jobs/${jobId}`, {
        title: document.getElementById("edit-title").value,
        description: document.getElementById("edit-description").value,
        tags: document.getElementById("edit-tags").value,
        date: document.getElementById("edit-date").value,
        time: document.getElementById("edit-time").value,
      });
      dialog.close();
      toast("success", "Upload updated");
      render({ refresh: false });
    } catch (error) {
      document.getElementById("edit-error").textContent = error.message;
    }
  });
}

function openPreview(videoId) {
  dialogBody.innerHTML = `<h2 id="dialog-title">Preview</h2>
    <video controls autoplay src="/media/videos/${encodeURIComponent(videoId)}" class="preview-player"></video>
    <div class="actions"><button type="button" class="btn" data-action="close-dialog">Close</button></div>`;
  dialog.showModal();
}

const actions = {
  async connect(el) {
    const platform = el.dataset.platform;
    const { auth_url: authUrl } = await api("POST", `/api/accounts/${platform}/connect`);
    state.waitingFor = platform;
    state.waitingSince = Date.now();
    window.open(authUrl, "_blank", "noopener");
    render({ refresh: true });
  },
  async disconnect(el) {
    const name = el.dataset.platform === "youtube" ? "YouTube" : "Facebook";
    if (!confirm(`Disconnect ${name}? Scheduled ${name} uploads will fail until you connect again.`)) return;
    await api("POST", `/api/accounts/${el.dataset.platform}/disconnect`);
    toast("info", `${name} disconnected`);
  },
  async "select-page"() {
    const choice = document.querySelector('input[name="facebook-page"]:checked');
    if (!choice) return;
    await api("POST", "/api/accounts/facebook/page", { page_id: choice.value });
    state.waitingFor = null;
    toast("success", "Facebook Page connected");
  },
  async retry(el) {
    await api("POST", `/api/platform-jobs/${el.dataset.pj}/retry`);
    toast("info", "Retrying upload");
  },
  async "cancel-pj"(el) {
    const result = await api("POST", `/api/platform-jobs/${el.dataset.pj}/cancel`);
    toast("info", result.result === "cancelling" ? "Cancelling upload…" : "Upload cancelled");
  },
  async "cancel-job"(el) {
    if (!confirm("Cancel this upload on all platforms that have not finished yet?")) return;
    await api("POST", `/api/jobs/${el.dataset.job}/cancel`);
    if (dialog.open) dialog.close();
    toast("info", "Upload cancelled");
  },
  async "run-now"(el) {
    await api("POST", `/api/jobs/${el.dataset.job}/run-now`);
    toast("info", "Publishing now");
  },
  async "publish-again"(el) {
    if (!confirm("This video is already published there. Publish it again as a new post?")) return;
    await api("POST", `/api/platform-jobs/${el.dataset.pj}/publish-again`);
    toast("success", "Publishing again");
  },
  async "edit-job"(el) { await openJob(el.dataset.job); },
  async "open-job"(el) { await openJob(el.dataset.job); },
  "close-dialog"() { dialog.close(); },
  "dismiss-toast"(el) { el.closest(".toast")?.remove(); },
  async "history-filter"(el) {
    state.historyFilter = el.dataset.filter;
    state.html = "";
  },
  async month(el) {
    const delta = Number(el.dataset.delta);
    if (delta === 0) {
      state.calendar = null;
    } else {
      let { year, month } = state.calendar;
      month += delta;
      if (month < 1) { month = 12; year -= 1; }
      if (month > 12) { month = 1; year += 1; }
      state.calendar = { year, month };
    }
  },
  async preview(el) { openPreview(el.dataset.video); },
  async "delete-video"(el) {
    if (!confirm("Remove this video from the library?")) return;
    const deleteFile = confirm("Also delete the video file from disk? (Only files Bottle Net stored itself are deleted.)");
    await api("DELETE", `/api/videos/${el.dataset.video}?delete_file=${deleteFile ? 1 : 0}`);
    toast("info", "Video removed");
  },
  async "import-downloads"() {
    const result = await api("POST", "/api/videos/import-downloads");
    toast("success", `Imported ${result.added.length} video(s)`, `From ${result.folder}`);
  },
  "change-video"() {
    const params = new URLSearchParams(state.params);
    params.delete("video");
    location.hash = `#/upload?${params}`;
  },
  async "edit-template"(el) {
    const template = state.templates.find((t) => String(t.id) === el.dataset.template);
    if (!template) return;
    document.getElementById("template-id").value = template.id;
    document.getElementById("template-name").value = template.name;
    document.getElementById("template-title").value = template.title;
    document.getElementById("template-description").value = template.description;
    document.getElementById("template-tags").value = template.tags.join(", ");
    document.getElementById("template-name").focus();
  },
  async "delete-template"(el) {
    if (!confirm("Delete this template?")) return;
    await api("DELETE", `/api/templates/${el.dataset.template}`);
    state.html = "";
  },
};

document.addEventListener("click", async (e) => {
  const el = e.target.closest("[data-action]");
  if (!el || !actions[el.dataset.action]) return;
  e.preventDefault();
  try {
    await actions[el.dataset.action](el);
    if (!["close-dialog", "dismiss-toast", "edit-template", "change-video", "preview"].includes(el.dataset.action)) {
      await render({ refresh: false });
    }
  } catch (error) {
    showError(error);
  }
});

// Forms that live inside re-rendered pages use delegated submit handlers.
document.addEventListener("submit", async (e) => {
  const form = e.target;
  try {
    if (form.id === "download-form") {
      e.preventDefault();
      const input = document.getElementById("download-url");
      await api("POST", "/api/downloads", { url: input.value });
      toast("info", "Download started", input.value);
      await render();
    } else if (form.id === "settings-form") {
      e.preventDefault();
      await saveSettings(form);
    } else if (form.id === "template-form") {
      e.preventDefault();
      const id = document.getElementById("template-id").value;
      const body = {
        name: document.getElementById("template-name").value,
        title: document.getElementById("template-title").value,
        description: document.getElementById("template-description").value,
        tags: document.getElementById("template-tags").value,
      };
      await api(id ? "PUT" : "POST", id ? `/api/templates/${id}` : "/api/templates", body);
      toast("success", "Template saved");
      state.html = "";
      await render();
    }
  } catch (error) {
    const target = form.querySelector(".error-text");
    if (target) target.textContent = error.message; else showError(error);
  }
});

async function saveSettings(form) {
  const changes = {};
  for (const el of form.querySelectorAll("[data-setting]")) {
    const key = el.dataset.setting;
    if (el.type === "checkbox") changes[key] = el.checked;
    else if (el.type === "number") changes[key] = Number.parseInt(el.value, 10);
    else if (key === "retry_delays") changes[key] = el.value.split(",").map((v) => Math.round(Number(v.trim()) * 60)).filter((v) => !Number.isNaN(v));
    else changes[key] = el.value;
  }
  state.settings = await api("PUT", "/api/settings", changes);
  if (changes.browser_notifications && "Notification" in window && Notification.permission === "default") {
    await Notification.requestPermission();
  }
  toast("success", "Settings saved");
  state.html = "";
  await render();
}

// Search and filter on the Videos page.
document.addEventListener("input", (e) => {
  if (e.target.id === "video-search") {
    state.videoSearch = e.target.value;
    clearTimeout(state.searchTimer);
    state.searchTimer = setTimeout(() => render({ refresh: true }), 250);
  }
});
document.addEventListener("change", (e) => {
  if (e.target.id === "video-filter") {
    state.videoStatus = e.target.value;
    render({ refresh: true });
  }
});

// Calendar drag and drop: move a scheduled upload to another day.
document.addEventListener("dragstart", (e) => {
  const chip = e.target.closest?.(".cal-job[draggable]");
  if (chip) e.dataTransfer.setData("text/plain", chip.dataset.job);
});
document.addEventListener("dragover", (e) => {
  if (e.target.closest?.(".cal-day")) e.preventDefault();
});
document.addEventListener("drop", async (e) => {
  const day = e.target.closest?.(".cal-day");
  const jobId = e.dataTransfer?.getData("text/plain");
  if (!day || !jobId) return;
  e.preventDefault();
  try {
    await api("PATCH", `/api/jobs/${jobId}`, { date: day.dataset.date });
    toast("success", "Upload moved", day.dataset.date);
    await render();
  } catch (error) {
    showError(error);
  }
});

// ------------------------------------------------------------ live updates

async function pollNotifications() {
  const unread = await api("GET", "/api/notifications?unread=1").catch(() => []);
  const fresh = unread.filter((n) => !state.shownNotifications.has(n.id));
  for (const n of fresh.reverse()) {
    state.shownNotifications.add(n.id);
    // Older notifications stay on the dashboard/history instead of flooding the screen.
    if (Date.now() - new Date(n.created_at).getTime() > 2 * 60 * 1000) continue;
    toast(n.level, n.title, n.message || "", { platform_job_id: n.platform_job_id });
    while (toastsEl.children.length > 4) toastsEl.lastElementChild.remove();
    if (state.settings?.browser_notifications && "Notification" in window
        && Notification.permission === "granted" && document.hidden) {
      new Notification(n.title, { body: n.message || "" });
    }
  }
  if (fresh.length) await api("POST", "/api/notifications/read", { ids: fresh.map((n) => n.id) }).catch(() => {});
}

async function tick() {
  await pollNotifications();
  if (state.waitingFor && Date.now() - state.waitingSince > 5 * 60 * 1000) state.waitingFor = null;
  if (state.waitingFor) {
    const accounts = await api("GET", "/api/accounts").catch(() => []);
    const account = accounts.find((a) => a.platform === state.waitingFor);
    if (account && (account.connected || account.pending_pages?.length)) state.waitingFor = null;
  }
  const editing = main.contains(document.activeElement) && ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName);
  if (LIVE_ROUTES.has(state.route) && !dialog.open && !editing) await render({ refresh: true });
}

window.addEventListener("hashchange", () => render());
dialog.addEventListener("close", () => { dialogBody.innerHTML = ""; });

(async function start() {
  try {
    state.settings = await api("GET", "/api/settings");
  } catch (error) {
    main.innerHTML = "<h1>Bottle Net is not reachable</h1><p></p>";
    main.querySelector("p").textContent = error.message;
    return;
  }
  await render();
  setInterval(() => tick().catch(() => {}), 2500);
}());
