/* Small formatters.
 *
 * All of them are pure and all of them are used in the transcript, so they
 * share one rule: never render a number the reader cannot act on. "1.2k
 * token" is a scale; "1,234" is noise in a 11px label.
 */

export function bytes(n) {
  const value = Number(n) || 0;
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

/** 840 → "0.8s", 12_400 → "12s", 74_000 → "1m14s" */
export function duration(ms) {
  const value = Math.max(0, Number(ms) || 0);
  if (value < 1000) return `${Math.round(value)}ms`;
  const seconds = value / 1000;
  if (seconds < 60) return `${seconds < 10 ? seconds.toFixed(1) : Math.round(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds % 60);
  return `${minutes}m${String(rest).padStart(2, "0")}s`;
}

export function tokens(n) {
  const value = Number(n) || 0;
  if (value < 1000) return String(value);
  if (value < 1_000_000) return `${(value / 1000).toFixed(value < 10_000 ? 1 : 0)}k`;
  return `${(value / 1_000_000).toFixed(1)}M`;
}

export function money(usd) {
  const value = Number(usd) || 0;
  if (!value) return "$0";
  if (value < 0.01) return `$${value.toFixed(4)}`;
  return `$${value.toFixed(2)}`;
}

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

export function timeAgo(iso) {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const delta = Date.now() - then;
  if (delta < MINUTE) return "刚刚";
  if (delta < HOUR) return `${Math.floor(delta / MINUTE)} 分钟前`;
  if (delta < DAY) return `${Math.floor(delta / HOUR)} 小时前`;
  if (delta < 2 * DAY) return "昨天";
  if (delta < 7 * DAY) return `${Math.floor(delta / DAY)} 天前`;
  const date = new Date(then);
  return `${date.getMonth() + 1}-${String(date.getDate()).padStart(2, "0")}`;
}

/** The sidebar's grouping: a flat list of 200 sessions is unreadable. */
export function dayBucket(iso) {
  if (!iso) return "更早";
  const then = new Date(iso);
  if (Number.isNaN(then.getTime())) return "更早";
  const now = new Date();
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const stamp = then.getTime();
  if (stamp >= startOfToday) return "今天";
  if (stamp >= startOfToday - DAY) return "昨天";
  if (stamp >= startOfToday - 7 * DAY) return "本周";
  return "更早";
}

export function clock(seconds) {
  const value = Math.max(0, Math.floor(Number(seconds) || 0));
  const minutes = Math.floor(value / 60);
  return `${String(minutes).padStart(2, "0")}:${String(value % 60).padStart(2, "0")}`;
}

export function truncate(text, limit = 60) {
  const value = String(text ?? "").replace(/\s+/g, " ").trim();
  return value.length > limit ? `${value.slice(0, limit - 1)}…` : value;
}
