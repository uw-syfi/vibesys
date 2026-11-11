/**
 * The renderer-safe facts the host's generic run index supplies. The core adapter selects these
 * fields from `RunEntry`/`RunHead`; screens never receive the generated messages themselves.
 */

/** What the latest committed lifecycle, or phase-one liveness when there is no lifecycle, says. */
export type GenericRunState =
  | 'preparing'
  | 'starting'
  | 'running'
  | 'held'
  | 'parking'
  | 'stopping'
  | 'parked'
  | 'finished'
  | 'failed'
  | 'stopped'
  | 'alive'
  | 'interrupted'
  | 'ended'
  | 'unknown';

/** Which Runs-list section the typed state can support. Unknown is neither live nor ended. */
export type GenericRunActivity = 'live' | 'ended' | 'unknown';

/** The headline objective present in the writer's latest head. */
export interface GenericRunObjective {
  readonly name: string;
  readonly unit: string;
  readonly bestValue: number | null;
}

/** One generic run, addressed by the key of the host's run-index topic. */
export interface GenericRunEntry {
  readonly runId: string;
  readonly projectRoot: string | null;
  readonly state: GenericRunState;
  readonly activity: GenericRunActivity;
  readonly objective: GenericRunObjective | null;
  readonly costUsd: number | null;
  readonly needsUser: boolean;
}

/** Why there is no safe generic run-index snapshot to show. */
export type GenericRunIndexUnavailable =
  | 'stale'
  | 'unsupported'
  | 'over-limit'
  | 'source-failed'
  | 'internal'
  | 'invalid'
  | 'transport';

/**
 * The generic run index at one atomic publication boundary. A stale ready value is the last whole
 * snapshot; a partial replacement snapshot is never represented here.
 */
export type GenericRunIndex =
  | {readonly _tag: 'Dormant'}
  | {readonly _tag: 'Loading'}
  | {
      readonly _tag: 'Ready';
      readonly freshness: 'current' | 'stale';
      readonly runs: ReadonlyMap<string, GenericRunEntry>;
    }
  | {readonly _tag: 'Unavailable'; readonly reason: GenericRunIndexUnavailable};

export const DORMANT_GENERIC_RUN_INDEX: GenericRunIndex = {_tag: 'Dormant'};
