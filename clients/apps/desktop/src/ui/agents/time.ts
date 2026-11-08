/** Spans and costs as the agent screens write them. Moments are in `display-time.ts`. */

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
