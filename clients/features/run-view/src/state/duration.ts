const SECONDS_PER_MINUTE = 60;
const SECONDS_PER_HOUR = 3600;

/**
 * A compact elapsed time: `45s`, `4m12s`, `1h03m`. Whole seconds, floored; a negative or
 * non-finite span reads as `0s`. Seconds are dropped from an hour on, where they are noise.
 */
export function formatElapsed(milliseconds: number): string {
  const total = Number.isFinite(milliseconds) ? Math.max(0, Math.floor(milliseconds / 1000)) : 0;
  if (total < SECONDS_PER_MINUTE) return `${total}s`;
  const minutes = Math.floor(total / SECONDS_PER_MINUTE);
  if (total < SECONDS_PER_HOUR) return `${minutes}m${pad2(total % SECONDS_PER_MINUTE)}s`;
  return `${Math.floor(total / SECONDS_PER_HOUR)}h${pad2(minutes % SECONDS_PER_MINUTE)}m`;
}

function pad2(value: number): string {
  return String(value).padStart(2, '0');
}
