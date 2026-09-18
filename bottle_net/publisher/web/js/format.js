// Pure formatting helpers shared by the views (no DOM access, testable in Node).

const HTML_ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

/** Escape text for safe use in HTML content and attribute values. */
export function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => HTML_ESCAPES[ch]);
}

export const PLATFORM_NAMES = { youtube: "YouTube", facebook: "Facebook" };

export const STATUS_LABELS = {
  scheduled: "Scheduled",
  queued: "Queued",
  uploading: "Uploading",
  retrying: "Retrying",
  missed: "Missed",
  completed: "Published",
  failed: "Failed",
  cancelled: "Cancelled",
  partial: "Partly published",
  unknown: "Unknown",
};

export const STATUS_ICONS = {
  scheduled: "⏳",
  queued: "⏳",
  uploading: "⬆",
  retrying: "↻",
  missed: "⚠",
  completed: "✓",
  failed: "✗",
  cancelled: "⊘",
  partial: "⚠",
  unknown: "?",
};

export function statusLabel(status) {
  return STATUS_LABELS[status] || status;
}

export function statusIcon(status) {
  return STATUS_ICONS[status] || "•";
}

export function formatBytes(bytes) {
  if (bytes == null || Number.isNaN(bytes)) return "unknown";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = Number(bytes);
  let unit = 0;
  while (value >= 1000 && unit < units.length - 1) {
    value /= 1000;
    unit += 1;
  }
  return unit === 0 ? `${value} B` : `${value.toFixed(value >= 100 ? 0 : 1)} ${units[unit]}`;
}

export function formatDuration(seconds) {
  if (seconds == null || Number.isNaN(seconds)) return "--:--";
  const total = Math.round(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return h ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}

/** Date/time parts of an ISO instant in the given IANA timezone. */
export function localParts(iso, timeZone) {
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  }).formatToParts(new Date(iso));
  const get = (type) => parts.find((p) => p.type === type)?.value ?? "";
  return { year: +get("year"), month: +get("month"), day: +get("day"), hour: get("hour"), minute: get("minute") };
}

/** "YYYY-MM-DD" of an instant in a timezone. */
export function localDateKey(iso, timeZone) {
  const p = localParts(iso, timeZone);
  return `${p.year}-${String(p.month).padStart(2, "0")}-${String(p.day).padStart(2, "0")}`;
}

export function formatTime(iso, timeZone) {
  if (!iso) return "";
  const p = localParts(iso, timeZone);
  return `${p.hour}:${p.minute}`;
}

export function formatDate(iso, timeZone) {
  if (!iso) return "";
  return new Intl.DateTimeFormat("en-GB", { timeZone, day: "numeric", month: "short", year: "numeric" })
    .format(new Date(iso));
}

export function formatDateTime(iso, timeZone) {
  if (!iso) return "";
  return `${formatDate(iso, timeZone)} ${formatTime(iso, timeZone)}`;
}

/** Same placeholders as the server: {filename}, {date}, {time}. */
export function renderTemplate(text, { filename, date, time }) {
  const stem = String(filename || "").replace(/\.[^.]+$/, "");
  return String(text || "")
    .replaceAll("{filename}", stem)
    .replaceAll("{date}", date || "")
    .replaceAll("{time}", time || "");
}

/** Days of a month grid (Monday first), with leading/trailing days of neighbouring months. */
export function monthGrid(year, month) {
  const first = new Date(Date.UTC(year, month - 1, 1));
  const offset = (first.getUTCDay() + 6) % 7; // Monday = 0
  const start = new Date(Date.UTC(year, month - 1, 1 - offset));
  const days = [];
  for (let i = 0; i < 42; i += 1) {
    const d = new Date(start.getTime() + i * 86400000);
    days.push({
      key: d.toISOString().slice(0, 10),
      day: d.getUTCDate(),
      inMonth: d.getUTCMonth() === month - 1,
    });
  }
  // Drop a trailing week that belongs entirely to the next month.
  return days.slice(35).every((d) => !d.inMonth) ? days.slice(0, 35) : days;
}

export const VIDEO_EXTENSIONS = [".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".wmv", ".3gp", ".mpeg", ".mpg"];

export function isVideoFile(name) {
  const lower = String(name || "").toLowerCase();
  return VIDEO_EXTENSIONS.some((ext) => lower.endsWith(ext));
}
