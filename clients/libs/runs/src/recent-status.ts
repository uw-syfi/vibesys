/**
 * The runs the user attached to recently, and what the views derive from a host's listing
 * (`HostListing`) and those runs: each recent run's status, and the project directories to offer.
 * Pure functions of pushed data; the host is never asked again for them.
 */
import type {HostKey, HostListing, HostRun} from '@vibesys/hosts';

/** A run the user attached to, as the views list it. */
export interface RecentRun {
  readonly host: HostKey;
  readonly hostLabel: string;
  readonly project: string;
  readonly task: string | null;
  readonly runId: string;
  readonly attachedAt: number;
}

/** What the views say about a recent run. */
export type RecentStatus =
  /** A live server drives it; attach to `instanceId`. */
  | {readonly kind: 'live'; readonly instanceId: string; readonly status: string}
  /** Its server ended; `status` is the phase it ended in, when the host still remembers it. */
  | {readonly kind: 'ended'; readonly status: string | null; readonly reason: string | null}
  /** The host cannot be asked now, or has not answered yet. */
  | {readonly kind: 'unknown'};

/** `run`'s status in its host's `listing` (undefined: no listing yet). */
export function recentStatus(run: RecentRun, listing: HostListing | undefined): RecentStatus {
  if (listing?.kind !== 'listed') return {kind: 'unknown'};
  const live = listing.live.find(
    entry => entry.runId === run.runId && entry.projectRoot === run.project,
  );
  if (live !== undefined) return {kind: 'live', instanceId: live.id, status: live.status};
  // A run resumed and ended again has several tombstones: its latest server says how it ended.
  const ended = listing.ended
    .filter(entry => entry.runId === run.runId)
    .reduce<HostRun | undefined>(
      (latest, entry) =>
        latest === undefined || entry.startedAt > latest.startedAt ? entry : latest,
      undefined,
    );
  return {kind: 'ended', status: ended?.status ?? null, reason: ended?.reason ?? null};
}

/**
 * The project directories to offer on `host`: those of its live servers, then those of its recent
 * runs (most recent first), each once.
 */
export function suggestProjects(
  listing: HostListing | undefined,
  recent: readonly RecentRun[],
  host: HostKey,
): string[] {
  const projects: string[] = [];
  const add = (project: string): void => {
    if (project !== '' && !projects.includes(project)) projects.push(project);
  };
  if (listing?.kind === 'listed') for (const run of listing.live) add(run.projectRoot);
  for (const run of recent) if (run.host === host) add(run.project);
  return projects;
}
