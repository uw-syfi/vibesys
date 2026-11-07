/**
 * Moments and spans as the agent screens write them. Moments are told in Seattle time, whatever
 * the zone of the machine the app runs on, because that is the zone of the people who read them.
 */

const ZONE = 'America/Los_Angeles';
const DAY_MS = 86_400_000;

const clock = new Intl.DateTimeFormat('en-US', {
  timeZone: ZONE,
  hour: 'numeric',
  minute: '2-digit',
});
const clockSeconds = new Intl.DateTimeFormat('en-US', {
  timeZone: ZONE,
  hour: 'numeric',
  minute: '2-digit',
  second: '2-digit',
});
const dayClock = new Intl.DateTimeFormat('en-US', {
  timeZone: ZONE,
  weekday: 'short',
  hour: 'numeric',
  minute: '2-digit',
});

/** `2:40 AM` in Seattle. */
export const formatTime = (atMs: number): string => clock.format(atMs);

/** `Mon 2:40 AM` in Seattle, for moments that may be on different days. */
export const formatDayTime = (atMs: number): string => dayClock.format(atMs);

/**
 * The label of a tick on an axis over `spanMs`: seconds when the axis is minutes long, the day when
 * it is days long.
 */
export function axisLabel(atMs: number, spanMs: number): string {
  if (spanMs < 600_000) return clockSeconds.format(atMs);
  return spanMs < DAY_MS ? clock.format(atMs) : dayClock.format(atMs);
}

/** `14s`, `3m 05s`, `2h 10m`: how long something took, in figures that line up. */
export function formatSpan(ms: number): string {
  const seconds = Math.max(0, Math.round(ms / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${String(seconds % 60).padStart(2, '0')}s`;
  return `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, '0')}m`;
}

/** `$1.12`, or an empty string when the host reported no cost. */
export const formatUsd = (usd: number | null): string => (usd === null ? '' : `$${usd.toFixed(2)}`);
