/**
 * The runs the user attached to recently, and what the views derive from a host's listing
 * (`HostListing`) and those runs: each recent run's status, and the project directories to offer.
 * Pure functions of pushed data; the host is never asked again for them.
 */
import {type HostKey, type HostListing, instanceObservation, newestServerOf} from '@vibesys/hosts';
import type {RunStart} from './run-start.js';

/** A run the user attached to, as the views list it. */
export interface RecentRun {
  readonly host: HostKey;
  readonly hostLabel: string;
  readonly project: string;
  readonly task: string | null;
  readonly runId: string;
  /** Last known server instance, retained for diagnostics after registry tombstones expire. */
  readonly instanceId: string | null;
  readonly attachedAt: number;
  /** What the app started it with, when the app started it; absent or null otherwise. */
  readonly start?: RunStart | null;
}

/** An entry of the saved recent runs that could not be read, so the list leaves it out. */
export interface SkippedRecent {
  /** Its place in the saved list. */
  readonly index: number;
  /** Why, naming the key that is wrong (`recent.json: runs[16].task must be a task name`). */
  readonly reason: string;
}

/**
 * Stable identity of one logical recent run.
 *
 * Run IDs are scoped to a project, not to a host. JSON tuple encoding keeps the three validated
 * fields unambiguous and gives both persistence and renderers one spelling of that fact.
 */
export function recentRunKey(run: Pick<RecentRun, 'host' | 'project' | 'runId'>): string {
  return JSON.stringify([run.host, run.project, run.runId]);
}

/** What the views say about a recent run. */
export type RecentStatus =
  /** A live server drives it; attach to `instanceId`. */
  | {readonly kind: 'live'; readonly instanceId: string; readonly status: string}
  /** Its server ended; `status` is the phase it ended in, when the host still remembers it. */
  | {
      readonly kind: 'ended';
      readonly instanceId: string | null;
      readonly status: string | null;
      readonly reason: string | null;
    }
  /** The host cannot be asked now, or has not answered yet. */
  | {readonly kind: 'unknown'};

/** `run`'s status in its host's `listing` (undefined: no listing yet). */
export function recentStatus(run: RecentRun, listing: HostListing | undefined): RecentStatus {
  if (listing?.kind !== 'listed') return {kind: 'unknown'};
  const live = newestServerOf(listing.live, run.runId, run.project);
  if (live !== undefined) return {kind: 'live', instanceId: live.id, status: live.status};
  if (run.instanceId !== null) {
    const observed = instanceObservation(listing, run.instanceId);
    if (observed.kind === 'unverified' || observed.kind === 'unreadable') {
      return {kind: 'unknown'};
    }
  } else if (listing.unverified.length > 0 || listing.unreadable.length > 0) {
    // V2 recent entries predate durable instance ids. Any uncertain row could be this run's
    // current server, so absence cannot prove that the run ended.
    return {kind: 'unknown'};
  }
  // A run resumed and ended again has several tombstones: its latest server says how it ended.
  const ended = newestServerOf(listing.ended, run.runId, run.project);
  return {
    kind: 'ended',
    instanceId: ended?.id ?? run.instanceId,
    status: ended?.status ?? null,
    reason: ended?.reason ?? null,
  };
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

/**
 * Every project directory the app has met on `host`, each once: those of its live servers, its
 * ended ones the registry still keeps, then its recent runs. A machine lists runs project by
 * project, so these are the projects a question across runs asks of it.
 */
export function knownProjects(
  listing: HostListing | undefined,
  recent: readonly RecentRun[],
  host: HostKey,
): string[] {
  const projects = new Set<string>();
  if (listing?.kind === 'listed') {
    for (const run of [...listing.live, ...listing.ended]) projects.add(run.projectRoot);
  }
  for (const run of recent) if (run.host === host) projects.add(run.project);
  projects.delete('');
  return [...projects];
}
