/** The figures the Runs dashboard words its tiles and cards with. */

/** A multiple of the base with its unit: `1.42×`. */
export function ratioWord(ratio: number): string {
  return `${ratio.toFixed(2)}×`;
}

/** A span in its two largest units: `45s`, `52m`, `1h 05m`. */
export function durationWord(seconds: number): string {
  const whole = Math.max(0, Math.round(seconds));
  if (whole < 60) return `${whole}s`;
  const minutes = Math.floor(whole / 60);
  if (minutes < 60) return `${minutes}m`;
  return `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, '0')}m`;
}

export function ideasWord(count: number): string {
  return count === 1 ? '1 idea' : `${count} ideas`;
}
