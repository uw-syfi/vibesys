/**
 * How a session to a host proves it is alive. A link is alive while it delivers bytes; a link that
 * delivers none for a while is probed (a cheap request whose reply is bytes too), and one that
 * stays silent through the probe is lost. This is the one place that decides "the link is dead";
 * how long a phase of dialing may take is a different question (`gateway-policy.ts`), because a
 * slow transfer is alive and must never be mistaken for a dead link, nor a dead link for a slow
 * transfer.
 *
 * Pure arithmetic over windows of equal length: the shell counts the bytes it heard in each window
 * and applies the answer, so a property test covers every history without a clock.
 */

/** The length of one window; a window that heard nothing is silent. */
export const LIVENESS_WINDOW_MS = 5_000;

/** What to do when a window ends. */
type LivenessAction = 'none' | 'probe' | 'lost';

export interface LivenessStep {
  /** Consecutive silent windows so far, this one included. */
  readonly silent: number;
  readonly action: LivenessAction;
}

/** Windows of silence in a row that end the session: the first is probed, the second is lost. */
export const SILENT_WINDOWS_TO_LOSE = 2;

/**
 * The step after a window that ended having heard something (`heard`) or nothing, following
 * `silent` silent windows in a row.
 */
export function afterWindow(heard: boolean, silent: number): LivenessStep {
  if (heard) return {silent: 0, action: 'none'};
  const next = silent + 1;
  return {silent: next, action: next >= SILENT_WINDOWS_TO_LOSE ? 'lost' : 'probe'};
}

/**
 * The longest a link may be silent before it is declared lost: the last bytes may land at the
 * start of a window, and two whole silent windows must then pass. Every silence of this length or
 * more is detected, so the shell and a test both read this bound from here.
 */
export const SILENCE_BOUND_MS = (SILENT_WINDOWS_TO_LOSE + 1) * LIVENESS_WINDOW_MS;
