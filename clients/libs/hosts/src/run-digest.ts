/**
 * What a run's journal says at a glance, as the sidebar's rows read it: the helper's digest
 * (`runs.list`'s `digest`, feature `run_digest`) in the app's own terms, with when the run began.
 * It is how a run's ending is known once the registry's tombstone has expired (a day).
 */
import type {
  IdeaVerdict,
  RunAttention,
  RunClosing,
  RunDigest,
  RunSummary,
} from '@vibesys/contracts/host-helper';
import type {RunEnd} from './listing.js';
import {type EndLook, endedLook} from './run-look.js';

/**
 * What every screen says of a run its host holds no record of (a digest left it out, a summary
 * refused it): the one wording, which offers no Retry because asking again cannot find it.
 */
export const RUN_GONE_WORDS = 'this run is gone';

/** One idea of a run on its tile's chart: its result against the input and how it came out. */
export interface IdeaPoint {
  /** An improvement over the input (the digest's wording), null before the idea has a reading. */
  readonly ratio: number | null;
  readonly verdict: IdeaVerdict;
  /**
   * When the idea's result landed, in milliseconds, by the journal's clock; null when the journal
   * gave no readable time (or the helper predates it). The time a kept idea became the best.
   */
  readonly at: number | null;
}

/**
 * What a run's tile draws (`runs.list`'s `dashboard`, feature `run_dashboard`): the run page's
 * chart as points in the order results landed, and why the run needs its user, if it does.
 */
export interface RunDashboardEntry {
  /** At most 64; the oldest ideas that are not kept are left out first. */
  readonly points: readonly IdeaPoint[];
  /** The ideas left out of `points`, so the ideas the run tried are `points.length + pointsOmitted`. */
  readonly pointsOmitted: number;
  /** The ideas at the end that failed, none passing since. */
  readonly failedStreak: number;
  /** The journal's last event, in milliseconds; null when it has none. */
  readonly lastEventAt: number | null;
  readonly attention: RunAttention;
}

/** One run's digest, or why it could not be read. */
export type RunDigestEntry =
  | {
      readonly kind: 'read';
      /** When the run was created, in milliseconds. */
      readonly createdAt: number;
      /** The ideas the run tried; null when its journal predates the run trace and records none. */
      readonly ideas: number | null;
      /** The adopted result as a multiple of the input, as the run page headlines it; null when the run adopted nothing. */
      readonly adoptedRatio: number | null;
      /** While the run works, before any adoption: the best gate that passed, as the run page headlines it; else null. */
      readonly bestSoFarRatio: number | null;
      readonly durationSeconds: number | null;
      /** The run's own ending record's outcome, when its journal has one that names it. */
      readonly outcome: NonNullable<RunEnd>['outcome'] | null;
      readonly closing: RunClosing | null;
      /** Whether the journal says the run is over (an ending record or a terminal event). */
      readonly over: boolean;
      /** Core's account of how the run ended, from its ending record; null or absent when it has none. */
      readonly endSummary?: string | null;
      /** The tile's data; null when the host's VibeSys does not offer `run_dashboard`. */
      readonly dashboard?: RunDashboardEntry | null;
    }
  | {readonly kind: 'unreadable'; readonly createdAt: number; readonly message: string};

/** The answer to asking for digests: one per run the host knows among those asked, or no support. */
export type RunDigestsReply =
  | {readonly kind: 'digests'; readonly runs: ReadonlyMap<string, RunDigestEntry>}
  | {readonly kind: 'unsupported'};

function dashboardOf(summary: RunSummary, digest: RunDigest): RunDashboardEntry | null {
  const {dashboard} = digest;
  if (dashboard === undefined || dashboard === null) return null;
  return {
    points: dashboard.points.map(({ratio, verdict, at}) => ({
      ratio,
      verdict,
      at: at === undefined || at === null ? null : Date.parse(at),
    })),
    pointsOmitted: dashboard.points_omitted,
    failedStreak: dashboard.failed_streak,
    lastEventAt: dashboard.last_event_at === null ? null : Date.parse(dashboard.last_event_at),
    attention: summary.attention ?? 'none',
  };
}

/** `text` unless it is absent or blank. */
function nonEmpty(text: string | undefined): string | null {
  return text === undefined || text.trim() === '' ? null : text;
}

function read(summary: RunSummary, digest: RunDigest, createdAt: number): RunDigestEntry {
  const {ending, closing} = digest;
  return {
    kind: 'read',
    createdAt,
    ideas: digest.ideas,
    adoptedRatio: digest.adopted_ratio,
    bestSoFarRatio: digest.best_so_far_ratio,
    durationSeconds: digest.duration_s,
    outcome: ending?.outcome ?? null,
    closing,
    over: ending !== null || closing !== null,
    endSummary: nonEmpty(ending?.summary),
    dashboard: dashboardOf(summary, digest),
  };
}

/** The entries of the runs of `summaries` that carry a digest, by run id. */
export function digestEntries(summaries: readonly RunSummary[]): Map<string, RunDigestEntry> {
  const entries = new Map<string, RunDigestEntry>();
  for (const summary of summaries) {
    const {digest} = summary;
    if (digest === undefined || digest === null) continue;
    const createdAt = Date.parse(summary.created_at);
    entries.set(
      summary.run_id,
      digest.kind === 'digest'
        ? read(summary, digest, createdAt)
        : {kind: 'unreadable', createdAt, message: digest.message},
    );
  }
  return entries;
}

/**
 * How a run the journal says is over ended: its ending record's outcome, else its terminal event
 * (failed, interrupted, finished); null when the journal does not say.
 */
export function digestLook(entry: Extract<RunDigestEntry, {kind: 'read'}>): EndLook | null {
  if (entry.outcome !== null) {
    return endedLook({
      phase: 'completed',
      end: {outcome: entry.outcome, reason: '', resumable: false},
    });
  }
  switch (entry.closing) {
    case 'finished':
      return endedLook({phase: 'completed', end: null});
    case 'failed':
      return endedLook({phase: 'failed', end: null});
    case 'interrupted':
      return endedLook({phase: 'stopped', end: null});
    default:
      return null;
  }
}

/** One run of a project as the host lists it: what an all-runs list needs before its digest is read. */
export interface ListedRun {
  readonly runId: string;
  /** The run's own name, as the host gives it; the run id names a run listed without one. */
  readonly name?: string;
  /** The task it ran, or null when the run has no record of one (it stopped at start-up). */
  readonly task: string | null;
  /** When the run was created, in milliseconds. */
  readonly createdAt: number;
}

/** The runs of `summaries`, newest first (ties by run id, so the order is total). */
export function listedRuns(summaries: readonly RunSummary[]): ListedRun[] {
  return summaries
    .map(summary => ({
      runId: summary.run_id,
      name: summary.display_name,
      task: summary.task ?? null,
      createdAt: Date.parse(summary.created_at),
    }))
    .sort((left, right) => right.createdAt - left.createdAt || (left.runId < right.runId ? 1 : -1));
}
