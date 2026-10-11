/**
 * A host's runs, as its registry lists them, and the pure projection of what a host's helper
 * reports into that model.
 */
import type {InstanceList, LiveInstanceRecord, RunPhase} from '@vibesys/contracts/host-helper';
import type {ConnectionStatus} from './connection-status.js';
import type {HostKey} from './host.js';

/** One run a host's registry lists: a live server, or an ended one kept as a tombstone. */
export interface HostRun {
  /** The registry id of the server. */
  readonly id: string;
  readonly projectRoot: string;
  readonly runId: string;
  /** The run's phase (`RunStatus`), with its startup stage while starting. */
  readonly status: string;
  readonly startedAt: number;
  /** Why the run failed, when it did. */
  readonly reason: string | null;
}

/** A host's registry as the views show it, or why it cannot be read now. */
export type HostListing =
  | {
      readonly kind: 'listed';
      readonly host: HostKey;
      readonly live: readonly HostRun[];
      readonly ended: readonly HostRun[];
      /** Records this app cannot read (another VibeSys version), by id. */
      readonly unreadable: readonly string[];
    }
  | {
      readonly kind: 'unavailable';
      readonly host: HostKey;
      readonly connection: ConnectionStatus;
    };

const STAGES: Readonly<Record<NonNullable<RunPhase['stage']>, string>> = {
  resolving: 'Resolving',
  building_image: 'Building image',
  starting_container: 'Starting container',
};

/**
 * A run's phase in words: a starting run's stage and progress (`Building image 12/30: RUN ...`, so a
 * long first image build is visible), otherwise its status.
 */
export function instancePhaseText(phase: RunPhase): string {
  if (phase.status !== 'starting' || phase.stage == null) return phase.status;
  const stage = STAGES[phase.stage];
  const progress = phase.progress;
  if (progress == null) return stage;
  const count = `${progress.done}/${progress.total ?? '?'}`;
  return progress.label == null || progress.label === ''
    ? `${stage} ${count}`
    : `${stage} ${count}: ${progress.label}`;
}

function hostRun(record: LiveInstanceRecord): HostRun {
  const {phase} = record;
  return {
    id: record.id,
    projectRoot: record.project_root,
    runId: record.run_id,
    status: instancePhaseText(phase),
    startedAt: record.started_at,
    reason: phase.reason == null ? null : phase.reason.message,
  };
}

/** `listing` from `host`'s registry, as the views list it. */
export function hostListing(host: HostKey, listing: InstanceList): HostListing {
  return {
    kind: 'listed',
    host,
    live: listing.instances.map(hostRun),
    ended: (listing.ended ?? []).map(hostRun),
    unreadable: [
      ...(listing.unreadable ?? []).map(entry => entry.id),
      ...(listing.unverified ?? []),
    ].sort(),
  };
}

/** The live record of `instanceId` in `listing`, if there is one. */
export function liveRecord(listing: InstanceList, instanceId: string): LiveInstanceRecord | null {
  return listing.instances.find(record => record.id === instanceId) ?? null;
}
