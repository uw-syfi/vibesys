/**
 * Which idea an attempt belongs to, counted the way the run screen counts ideas (in the order they
 * began), so the chat can say "idea 4" for a steer that reached an attempt's turn. The wire names
 * the attempt; the run trace knows its idea.
 */
import type {CandidateTree} from '@vibesys/run-state/candidates';

/** The number of the idea `attemptId` belongs to, or null when the trace holds no such attempt. */
export function ideaOfAttempt(
  candidates: CandidateTree['candidates'],
): (attemptId: string) => number | null {
  return attemptId => {
    const index = candidates.findIndex(
      idea => idea.id === attemptId || idea.attempts.some(attempt => attempt.span === attemptId),
    );
    return index < 0 ? null : index + 1;
  };
}
