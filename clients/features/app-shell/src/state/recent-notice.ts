/**
 * What the dashboard says when saved runs could not be read (`SkippedRecent`): how many, and the first
 * few reasons, each naming the key that is wrong. Pure wording; the file is never changed.
 */
import type {SkippedRecent} from '@vibesys/runs';

/** How many reasons the notice spells out; the rest are counted. */
export const NOTICE_REASONS = 3;

export interface RecentNotice {
  readonly title: string;
  readonly body: string;
}

/** The notice for `skipped`, or null when every saved run was readable. */
export function recentNotice(skipped: readonly SkippedRecent[]): RecentNotice | null {
  if (skipped.length === 0) return null;
  const shown = skipped.slice(0, NOTICE_REASONS).map(entry => entry.reason);
  const more = skipped.length - shown.length;
  const reasons = more > 0 ? [...shown, `+${more} more`] : shown;
  return {
    title: `${skipped.length} saved ${skipped.length === 1 ? 'run' : 'runs'} skipped`,
    body: reasons.join('; '),
  };
}
