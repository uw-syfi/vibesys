/**
 * A stretch of work as the dashboard writes it: seconds below an hour (`15m 00s`, `45s`), hours and
 * minutes from an hour on (`5h 13m`). The one place the Ideas list's agent time and the stats row's
 * agent and running time are worded, so one length never reads two ways.
 */

/** `seconds` as `45s`, `15m 00s`, `5h 13m`; a negative or non-finite length reads as none. */
export function workedWords(seconds: number): string {
  const whole = Number.isFinite(seconds) ? Math.max(0, Math.floor(seconds)) : 0;
  if (whole < 60) return `${whole}s`;
  const minutes = Math.floor(whole / 60);
  if (minutes < 60) return `${minutes}m ${String(whole % 60).padStart(2, '0')}s`;
  return `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, '0')}m`;
}
