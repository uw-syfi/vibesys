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

const dayDate = new Intl.DateTimeFormat('en-US', {
  timeZone: ZONE,
  weekday: 'short',
  month: 'short',
  day: 'numeric',
});

/** `Tue Nov 3` in Seattle. */
function dateOf(atMs: number): string {
  const part = new Map(dayDate.formatToParts(atMs).map(each => [each.type, each.value]));
  return `${part.get('weekday')} ${part.get('month')} ${part.get('day')}`;
}

const wallClock = new Intl.DateTimeFormat('en-US', {
  timeZone: ZONE,
  hourCycle: 'h23',
  year: 'numeric',
  month: 'numeric',
  day: 'numeric',
  hour: 'numeric',
  minute: 'numeric',
  second: 'numeric',
});

/** How far Seattle's clock is from UTC at a moment, in milliseconds (negative: behind). */
export function zoneOffsetMs(atMs: number): number {
  const part = new Map(wallClock.formatToParts(atMs).map(each => [each.type, Number(each.value)]));
  const shown = Date.UTC(
    part.get('year') ?? 1970,
    (part.get('month') ?? 1) - 1,
    part.get('day') ?? 1,
    part.get('hour') ?? 0,
    part.get('minute') ?? 0,
    part.get('second') ?? 0,
  );
  return shown - Math.floor(atMs / 1000) * 1000;
}

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
  if (spanMs < DAY_MS) return clock.format(atMs);
  // A tick at midnight is a day: the date alone, not a clock time that is the same on every label.
  return (atMs + zoneOffsetMs(atMs)) % DAY_MS === 0 ? dateOf(atMs) : dayClock.format(atMs);
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
